"""Recovering a delivery slot after a manufacturing delay.

The business-impact story: a delay does not just lose one delivery, it wastes the slot. These
tests check that the slot is actually recovered, and -- more importantly -- that recovering it
never costs anyone else their promised time or assumes a consent nobody gave.
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
    PlanningStatus,
    ReadinessStatus,
    TimeWindow,
)
from dispatch_agent.planning import plan_service, recovery_service
from dispatch_agent.planning.clock import PlanningClock

BASE = date(2026, 9, 2)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


def _w(start, end):
    return TimeWindow(start=time(*start), end=time(*end))


def _confirmed(repo, name, postal_code, day, window=((9, 0), (18, 0)), early=False, duration=45):
    job = JobRecord(
        customer_name=name,
        address=Address(raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)),
        job_type=JobType.SOFA,
        availability=[_w(*window)],
        locked_window=_w(*window),
        availability_options=[AvailabilityOption(date=day, window=_w(*window))],
        delivery_date=day,
        planning_status=PlanningStatus.CONFIRMED,
        status="approved",
        can_deliver_early=early,
        raw_message="seed",
        duration_minutes=duration,
    )
    repo.save_job(job)
    return job


def test_a_delayed_order_leaves_the_route_but_keeps_its_history(temp_db):
    """Delayed is not cancelled and not deleted -- the order still exists, it just cannot go out."""
    day = PlanningClock.horizon_dates()[0]
    delayed = _confirmed(temp_db, "Delayed", "018956", day)
    _confirmed(temp_db, "Fine", "486123", day)
    plan_service.replan_day(temp_db, day, reason="initial")

    outcome = recovery_service.mark_readiness(temp_db, delayed.id, ReadinessStatus.DELAYED)

    assert temp_db.get_job(delayed.id) is not None
    assert temp_db.get_job(delayed.id).planning_status is PlanningStatus.CONFIRMED
    assert delayed.id not in {s.job_id for s in outcome.plan.sequence.stops}
    assert outcome.plan.version == 2


def test_the_other_customers_keep_their_promised_windows(temp_db):
    """Removing a stop reshuffles the day. Nobody who was already promised a time may move."""
    day = PlanningClock.horizon_dates()[0]
    delayed = _confirmed(temp_db, "Delayed", "018956", day, window=((9, 0), (12, 0)))
    kept = _confirmed(temp_db, "Kept", "486123", day, window=((14, 0), (16, 0)))
    plan_service.replan_day(temp_db, day, reason="initial")

    outcome = recovery_service.mark_readiness(temp_db, delayed.id, ReadinessStatus.DELAYED)

    stop = next(s for s in outcome.plan.sequence.stops if s.job_id == kept.id)
    assert kept.locked_window.start <= stop.arrival_window.start
    assert stop.arrival_window.end <= kept.locked_window.end


def test_only_customers_who_opted_in_are_considered(temp_db):
    """Consent to an earlier delivery is never assumed from silence."""
    days = PlanningClock.horizon_dates()
    delayed = _confirmed(temp_db, "Delayed", "018956", days[0])
    _confirmed(temp_db, "Willing", "486123", days[2], early=True)
    _confirmed(temp_db, "Not asked", "489123", days[2], early=False)
    plan_service.replan_day(temp_db, days[0], reason="initial")

    outcome = recovery_service.mark_readiness(temp_db, delayed.id, ReadinessStatus.DELAYED)

    names = {c.job.customer_name for c in outcome.replacements}
    assert names == {"Willing"}


def test_replacements_are_ranked_by_the_same_scoring_as_a_normal_booking(temp_db):
    days = PlanningClock.horizon_dates()
    delayed = _confirmed(temp_db, "Delayed", "018956", days[0])
    _confirmed(temp_db, "Near", "018956", days[2], early=True)
    _confirmed(temp_db, "Far", "738099", days[2], early=True)
    plan_service.replan_day(temp_db, days[0], reason="initial")

    outcome = recovery_service.mark_readiness(temp_db, delayed.id, ReadinessStatus.DELAYED)

    assert [c.job.customer_name for c in outcome.replacements][0] == "Near"
    assert outcome.replacements[0].score <= outcome.replacements[1].score


def test_a_customer_already_scheduled_earlier_is_not_offered_a_later_slot(temp_db):
    """Recovery moves people forward, never backwards."""
    days = PlanningClock.horizon_dates()
    delayed = _confirmed(temp_db, "Delayed", "018956", days[2])
    _confirmed(temp_db, "Already earlier", "486123", days[0], early=True)
    plan_service.replan_day(temp_db, days[2], reason="initial")

    outcome = recovery_service.mark_readiness(temp_db, delayed.id, ReadinessStatus.DELAYED)

    assert outcome.replacements == []


def test_no_willing_customer_means_an_idle_slot_and_a_notification(temp_db):
    """An unrecoverable slot must be visible to the back office, not silently absorbed."""
    day = PlanningClock.horizon_dates()[0]
    delayed = _confirmed(temp_db, "Delayed", "018956", day)
    plan_service.replan_day(temp_db, day, reason="initial")

    outcome = recovery_service.mark_readiness(temp_db, delayed.id, ReadinessStatus.DELAYED)

    assert outcome.replacements == []
    assert any("idle" in n.message for n in temp_db.unread_notifications())


def test_at_most_two_replacements_are_pursued(temp_db):
    """The offer budget is bounded -- a delay must not turn into a phone-around."""
    days = PlanningClock.horizon_dates()
    delayed = _confirmed(temp_db, "Delayed", "018956", days[0])
    for i in range(5):
        _confirmed(temp_db, f"Willing {i}", "486123", days[2], early=True)
    plan_service.replan_day(temp_db, days[0], reason="initial")

    outcome = recovery_service.mark_readiness(temp_db, delayed.id, ReadinessStatus.DELAYED)

    assert len(outcome.replacements) <= recovery_service.MAX_REPLACEMENT_OFFERS


def test_marking_ready_again_puts_the_order_back_on_the_route(temp_db):
    day = PlanningClock.horizon_dates()[0]
    delayed = _confirmed(temp_db, "Delayed", "018956", day)
    plan_service.replan_day(temp_db, day, reason="initial")
    recovery_service.mark_readiness(temp_db, delayed.id, ReadinessStatus.DELAYED)

    outcome = recovery_service.mark_readiness(temp_db, delayed.id, ReadinessStatus.READY)

    assert delayed.id in {s.job_id for s in outcome.plan.sequence.stops}


# -- offering the freed slot ---------------------------------------------------


def test_recovery_offers_are_capped_separately_from_booking_rounds(temp_db):
    """A customer being asked whether they'd come forward has not used up a round of negotiating
    their own delivery date -- the two budgets are counted per purpose."""
    from dispatch_agent.models import OfferPurpose
    from dispatch_agent.planning import offer_service, recovery_service

    days = PlanningClock.horizon_dates()
    willing = _confirmed(temp_db, "Willing", "486123", days[3], early=True)

    offer, _msg, _ev = recovery_service.offer_freed_slot(temp_db, willing.id, days[0])
    assert offer.purpose is OfferPurpose.RECOVERY
    assert offer.round_number == 1

    # Asked once, not repeatedly.
    with pytest.raises(offer_service.OfferError) as exc:
        recovery_service.offer_freed_slot(temp_db, willing.id, days[1])
    assert exc.value.kind == "round_cap_reached"

    # And the booking budget was never touched.
    booking = [o for o in temp_db.offers_for_order(willing.id)
               if o.purpose is OfferPurpose.BOOKING]
    assert booking == []


def test_offered_and_locked_is_not_a_state_a_job_can_be_in(temp_db):
    """The invariant behind the recovery guard: OFFERED means nothing has been promised, so it
    cannot coexist with a locked window."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="cannot carry a locked_window"):
        JobRecord(
            customer_name="Contradiction",
            address=Address(raw_text="x", postal_code="018956",
                            coordinates=postal_code_to_coords("018956")),
            job_type=JobType.SOFA,
            availability=[_w((9, 0), (18, 0))],
            locked_window=_w((9, 0), (18, 0)),
            delivery_date=PlanningClock.horizon_dates()[0],
            planning_status=PlanningStatus.OFFERED,
            status="new",
            raw_message="test",
        )


def test_a_recovery_offer_leaves_a_record_that_still_loads(temp_db):
    """Pydantic validates on load, not on assignment -- so treating a recovery target as an
    ordinary booking would write a contradictory row that only blows up when something next
    reads it. The guard in create_offer is what stops that being written at all."""
    from dispatch_agent.planning import recovery_service

    days = PlanningClock.horizon_dates()
    willing = _confirmed(temp_db, "Willing", "486123", days[3], early=True)

    recovery_service.offer_freed_slot(temp_db, willing.id, days[0])

    reloaded = temp_db.get_job(willing.id)  # would raise if the row were contradictory
    assert reloaded.planning_status is PlanningStatus.CONFIRMED
    assert reloaded.is_locked


def test_being_offered_a_freed_slot_does_not_unconfirm_the_customer(temp_db):
    """The landmine: create_offer sets OFFERED, but a recovery target is CONFIRMED holding a
    locked window -- and the model forbids that pair. Without the guard this raises, and without
    the validator it would silently drop them off their current route just for being asked."""
    from dispatch_agent.planning import recovery_service

    days = PlanningClock.horizon_dates()
    willing = _confirmed(temp_db, "Willing", "486123", days[2], early=True)
    original_date, original_window = willing.delivery_date, willing.locked_window

    recovery_service.offer_freed_slot(temp_db, willing.id, days[0])

    after = temp_db.get_job(willing.id)
    assert after.planning_status is PlanningStatus.CONFIRMED
    assert after.delivery_date == original_date
    assert after.locked_window == original_window
    assert after.is_locked


def test_a_customer_who_did_not_opt_in_is_never_approached(temp_db):
    from dispatch_agent.planning import offer_service, recovery_service

    days = PlanningClock.horizon_dates()
    unwilling = _confirmed(temp_db, "Not asked", "486123", days[2], early=False)

    with pytest.raises(offer_service.OfferError) as exc:
        recovery_service.offer_freed_slot(temp_db, unwilling.id, days[0])

    assert exc.value.kind == "no_consent"
    assert temp_db.offers_for_order(unwilling.id) == []
    assert temp_db.messages(unwilling.id) == []


def test_recovery_never_moves_a_customer_later(temp_db):
    from dispatch_agent.planning import offer_service, recovery_service

    days = PlanningClock.horizon_dates()
    early_already = _confirmed(temp_db, "Already early", "486123", days[0], early=True)

    with pytest.raises(offer_service.OfferError) as exc:
        recovery_service.offer_freed_slot(temp_db, early_already.id, days[2])
    assert exc.value.kind == "not_earlier"


def test_accepting_a_freed_slot_republishes_both_days(temp_db):
    """The move only happens when the customer accepts. Both days change: the one they joined and
    the one they left."""
    from dispatch_agent.planning import offer_service, recovery_service

    days = PlanningClock.horizon_dates()
    _confirmed(temp_db, "Stays put", "018956", days[0])
    willing = _confirmed(temp_db, "Willing", "486123", days[2], early=True)
    plan_service.replan_day(temp_db, days[0], reason="initial")
    plan_service.replan_day(temp_db, days[2], reason="initial")

    offer, _msg, _ev = recovery_service.offer_freed_slot(temp_db, willing.id, days[0])
    outcome = offer_service.accept_offer(temp_db, offer.id, offer.options[0].id)

    moved = temp_db.get_job(willing.id)
    assert moved.delivery_date == days[0]
    assert moved.is_locked
    assert outcome.vacated_date == days[2]
    assert outcome.vacated_plan is not None
    assert willing.id in {s.job_id for s in temp_db.active_plan(days[0]).sequence.stops}
    assert willing.id not in {s.job_id for s in temp_db.active_plan(days[2]).sequence.stops}


def test_the_other_customers_on_the_vacated_day_keep_their_windows(temp_db):
    from dispatch_agent.planning import offer_service, recovery_service

    days = PlanningClock.horizon_dates()
    willing = _confirmed(temp_db, "Willing", "486123", days[2], early=True)
    stayed = _confirmed(temp_db, "Stayed", "018956", days[2], window=((14, 0), (16, 0)))
    plan_service.replan_day(temp_db, days[2], reason="initial")

    offer, _msg, _ev = recovery_service.offer_freed_slot(temp_db, willing.id, days[0])
    offer_service.accept_offer(temp_db, offer.id, offer.options[0].id)

    plan = temp_db.active_plan(days[2])
    stop = next(s for s in plan.sequence.stops if s.job_id == stayed.id)
    assert stayed.locked_window.start <= stop.arrival_window.start
    assert stop.arrival_window.end <= stayed.locked_window.end


def test_the_freed_date_becomes_evaluable_for_that_customer(temp_db):
    """The synthetic option is persisted, so the offer's availability_option_id points at
    something real rather than dangling."""
    from dispatch_agent.planning import recovery_service

    days = PlanningClock.horizon_dates()
    willing = _confirmed(temp_db, "Willing", "486123", days[2], early=True)

    offer, _msg, _ev = recovery_service.offer_freed_slot(temp_db, willing.id, days[0])

    after = temp_db.get_job(willing.id)
    option_ids = {o.id for o in after.availability_options}
    assert offer.options[0].availability_option_id in option_ids
    assert any(o.date == days[0] for o in after.availability_options)
