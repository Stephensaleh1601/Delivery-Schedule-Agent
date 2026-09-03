"""Declining a time without declining the day.

The behaviour under test is a distinction an experienced coordinator makes without thinking and a
scheduler usually gets wrong: "not 10 till 12" is not "not Friday". Friday keeps its other seven
hours, the day is re-solved around the hole, and a different window comes back.

What stops that becoming pestering is unchanged and tested here too -- the two-round cap, and the
rule that a time already turned down is never proposed again.
"""
from datetime import date, time, timedelta

import pytest

from dispatch_agent import config
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.models import (
    Address,
    AvailabilityOption,
    JobRecord,
    JobType,
    OfferStatus,
    TimeWindow,
)
from dispatch_agent.planning import offer_service
from dispatch_agent.planning.candidate_service import CandidateService
from dispatch_agent.planning.clock import PlanningClock

BASE = date(2026, 9, 2)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


def _w(start, end):
    return TimeWindow(start=time(*start), end=time(*end))


def _order(repo, options, postal_code="469123", duration=45):
    job = JobRecord(
        customer_name="Mrs Lee",
        address=Address(
            raw_text="Bedok", postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)
        ),
        job_type=JobType.SOFA,
        availability_options=list(options),
        raw_message="booked via chat",
        duration_minutes=duration,
    )
    repo.save_job(job)
    return job


def _offer(repo, order):
    return offer_service.create_offer(repo, order, CandidateService(repo=repo).evaluate_all(order))


def test_declining_a_window_keeps_the_rest_of_the_day(temp_db):
    """The headline. One date, all day free, first offer declined -- and the second offer is on the
    SAME date at a different time, because the customer never said the day was bad."""
    day = PlanningClock.horizon_dates()[0]
    order = _order(temp_db, [AvailabilityOption(date=day, window=_w((9, 0), (18, 0)))])
    first = _offer(temp_db, order)
    declined = first.options[0]

    offer_service.reject_offer(temp_db, first.id, slot_id=declined.id)
    second = _offer(temp_db, temp_db.get_job(order.id))

    assert second.options[0].date == day, "gave up on a day the customer is still free all of"
    assert not second.options[0].window.overlaps(declined.window)


def test_the_declined_time_is_never_proposed_again(temp_db):
    day = PlanningClock.horizon_dates()[0]
    order = _order(temp_db, [AvailabilityOption(date=day, window=_w((9, 0), (18, 0)))])
    first = _offer(temp_db, order)
    declined = first.options[0]

    offer_service.reject_offer(temp_db, first.id, slot_id=declined.id)
    second = _offer(temp_db, temp_db.get_job(order.id))

    assert all(s.window != declined.window for s in second.options)


def test_the_replacement_window_still_fits_the_job(temp_db):
    """A re-solve around a hole must not produce a window too tight to deliver in -- that would
    surface as "sorry, that slot was taken" after the customer had accepted it."""
    day = PlanningClock.horizon_dates()[0]
    order = _order(temp_db, [AvailabilityOption(date=day, window=_w((9, 0), (18, 0)))], duration=105)
    first = _offer(temp_db, order)

    offer_service.reject_offer(temp_db, first.id, slot_id=first.options[0].id)
    second = _offer(temp_db, temp_db.get_job(order.id))

    window = second.options[0].window
    width = (window.end.hour * 60 + window.end.minute) - (window.start.hour * 60 + window.start.minute)
    assert width >= 105 + config.settings.promise_min_slack_minutes


def test_declining_everything_rules_out_the_whole_option(temp_db):
    """"None of these work" excludes every window in the offer, not just the first."""
    days = PlanningClock.horizon_dates()[:2]
    order = _order(
        temp_db,
        [AvailabilityOption(date=d, window=_w((9, 0), (18, 0)), preference_rank=i + 1)
         for i, d in enumerate(days)],
    )
    first = _offer(temp_db, order)

    offer_service.reject_offer(temp_db, first.id)  # no slot_id -- the whole offer

    stored = temp_db.get_job(order.id)
    excluded = [w for o in stored.availability_options for w in o.excluded_windows]
    for slot in first.options:
        assert slot.window in excluded, f"{slot.date} {slot.window} was declined but not excluded"


def test_the_customer_is_not_asked_a_third_time(temp_db):
    """The cap is the thing that stops this being pestering, and it is enforced by counting rows
    rather than by asking a model to remember."""
    day = PlanningClock.horizon_dates()[0]
    order = _order(temp_db, [AvailabilityOption(date=day, window=_w((9, 0), (18, 0)))])

    first = _offer(temp_db, order)
    offer_service.reject_offer(temp_db, first.id, slot_id=first.options[0].id)
    second = _offer(temp_db, temp_db.get_job(order.id))
    offer_service.reject_offer(temp_db, second.id, slot_id=second.options[0].id)

    with pytest.raises(offer_service.OfferError) as exc:
        _offer(temp_db, temp_db.get_job(order.id))
    assert exc.value.kind == "round_cap_reached"


def test_a_day_with_nothing_left_reports_that_honestly(temp_db):
    """When the exclusions really do use up a date, the evaluation must say so rather than quietly
    proposing a window inside the time the customer ruled out."""
    day = PlanningClock.horizon_dates()[0]
    option = AvailabilityOption(
        date=day,
        window=_w((9, 0), (12, 0)),
        excluded_windows=[_w((9, 0), (12, 0))],
    )
    order = _order(temp_db, [option])

    evaluation = CandidateService(repo=temp_db).evaluate(order, option)

    assert not evaluation.feasible
    assert "ruled out" in evaluation.infeasible_reason
    assert evaluation.promise_window is None


def test_rejection_is_idempotent(temp_db):
    """A double-tap must not exclude the window twice or reopen a settled offer."""
    day = PlanningClock.horizon_dates()[0]
    order = _order(temp_db, [AvailabilityOption(date=day, window=_w((9, 0), (18, 0)))])
    first = _offer(temp_db, order)

    offer_service.reject_offer(temp_db, first.id, slot_id=first.options[0].id)
    offer_service.reject_offer(temp_db, first.id, slot_id=first.options[0].id)

    stored = temp_db.get_job(order.id)
    excluded = stored.availability_options[0].excluded_windows
    assert len(excluded) == 1
    assert temp_db.get_offer(first.id).status is OfferStatus.REJECTED
