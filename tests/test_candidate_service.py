"""Candidate evaluation: does the agent price a customer's options the way the business would?

These are the tests that decide whether the product's central claim is true -- that the slot it
offers is chosen on real route cost rather than on whatever the customer happened to ask for
first.
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
    TimeWindow,
)
from dispatch_agent.planning.candidate_service import CandidateService
from dispatch_agent.planning.clock import PlanningClock

BASE = date(2026, 9, 2)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    """Pin the horizon so these assertions do not change meaning overnight."""
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


def _window(start, end):
    return TimeWindow(start=time(*start), end=time(*end))


def _confirmed(repo, name, postal_code, day, window=((9, 0), (18, 0)), duration=45):
    """A job already promised to a customer -- i.e. part of the day a new order must fit into."""
    job = JobRecord(
        customer_name=name,
        address=Address(raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)),
        job_type=JobType.SOFA,
        availability=[_window(*window)],
        locked_window=_window(*window),
        delivery_date=day,
        planning_status=PlanningStatus.CONFIRMED,
        status="approved",
        raw_message="seed",
        duration_minutes=duration,
    )
    repo.save_job(job)
    return job


def _order(name="New Order", postal_code="018956", options=(), duration=45):
    return JobRecord(
        customer_name=name,
        address=Address(raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)),
        job_type=JobType.SOFA,
        availability_options=list(options),
        raw_message="booked via chat",
        duration_minutes=duration,
    )


def _option(day, window=((9, 0), (18, 0)), rank=1):
    return AvailabilityOption(date=day, window=_window(*window), preference_rank=rank)


def test_options_outside_the_horizon_are_rejected_not_scored(temp_db):
    """A customer may not book tomorrow, and must be told why rather than quietly re-dated."""
    too_soon = BASE + timedelta(days=1)
    order = _order(options=[_option(too_soon)])

    result = CandidateService(repo=temp_db).evaluate_all(order)[0]

    assert result.feasible is False
    assert "bookings between" in result.infeasible_reason
    assert result.total_score == 0, "an out-of-horizon option must carry no score"


def test_every_horizon_date_is_bookable(temp_db):
    order = _order(options=[_option(d) for d in PlanningClock.horizon_dates()])
    results = CandidateService(repo=temp_db).evaluate_all(order)
    assert all(r.feasible for r in results), [r.infeasible_reason for r in results if not r.feasible]


def test_infeasible_option_is_flagged_rather_than_priced_out(temp_db):
    """An impossible window must not merely score badly -- it must be excluded from selection,
    which is what the feasible flag guarantees."""
    day = PlanningClock.horizon_dates()[0]
    order = _order(options=[_option(day, window=((9, 0), (9, 20)))], duration=180)

    result = CandidateService(repo=temp_db).evaluate_all(order)[0]

    assert result.feasible is False
    assert result.infeasible_reason
    assert result.proposed_sequence is None


def test_joining_a_busy_day_beats_opening_an_empty_one(temp_db):
    """The core economic claim. Both options are feasible; the one that adds a stop to a day the
    van is already working should win over the one that starts a whole new day."""
    busy_day, empty_day = PlanningClock.horizon_dates()[0], PlanningClock.horizon_dates()[1]
    for i, postal in enumerate(["018956", "018956", "018956"]):
        _confirmed(temp_db, f"Existing {i}", postal, busy_day)

    order = _order(postal_code="018956", options=[_option(busy_day), _option(empty_day)])
    ranked = CandidateService(repo=temp_db).evaluate_all(order)

    assert ranked[0].date == busy_day, (
        f"picked the empty day: busy={ranked[-1].total_score} empty={ranked[0].total_score}"
    )
    assert ranked[0].day_opening_penalty_minutes == 0
    assert ranked[1].day_opening_penalty_minutes == config.settings.day_opening_penalty_minutes


def test_a_nearby_stop_costs_less_than_a_distant_one(temp_db):
    """Incremental drive time must actually reflect geography, or the whole exercise is theatre."""
    day = PlanningClock.horizon_dates()[0]
    _confirmed(temp_db, "Anchor", "018956", day)

    near = CandidateService(repo=temp_db).evaluate(_order(postal_code="018956"), _option(day))
    far = CandidateService(repo=temp_db).evaluate(_order(postal_code="738099"), _option(day))

    assert near.incremental_drive_minutes < far.incremental_drive_minutes


def test_preference_breaks_ties_without_overriding_routing(temp_db):
    """A customer's ranking should decide between otherwise-equal days, and no more than that."""
    first, second = PlanningClock.horizon_dates()[0], PlanningClock.horizon_dates()[1]
    order = _order(options=[_option(first, rank=2), _option(second, rank=1)])

    ranked = CandidateService(repo=temp_db).evaluate_all(order)

    assert ranked[0].date == second, "both days are equivalent, so the preferred one should win"
    assert ranked[0].preference_penalty_minutes == 0
    assert ranked[1].preference_penalty_minutes == config.settings.preference_penalty_per_rank


def test_evaluation_never_persists_a_date_or_a_lock(temp_db):
    """Nothing is promised until a customer accepts. Evaluating options must leave the order
    exactly as unscheduled as it was."""
    day = PlanningClock.horizon_dates()[0]
    order = _order(options=[_option(day)])
    temp_db.save_job(order)

    CandidateService(repo=temp_db).evaluate_all(order)

    stored = temp_db.get_job(order.id)
    assert stored.delivery_date is None
    assert stored.locked_window is None
    assert stored.planning_status is PlanningStatus.PENDING_PLANNING


def test_each_day_baseline_is_solved_only_once(temp_db):
    """Three options on one date must not mean three baseline solves -- that is what makes
    evaluating a whole booking affordable."""
    day = PlanningClock.horizon_dates()[0]
    _confirmed(temp_db, "Anchor", "018956", day)
    service = CandidateService(repo=temp_db)

    solved_days = []
    original = service._solve
    service._solve = lambda jobs, d: (solved_days.append((d, len(jobs))), original(jobs, d))[1]

    order = _order(options=[
        _option(day, window=((9, 0), (12, 0))),
        _option(day, window=((12, 0), (15, 0))),
        _option(day, window=((15, 0), (18, 0))),
    ])
    service.evaluate_all(order)

    baselines = [d for d, n in solved_days if n == 1]
    assert len(baselines) == 1, f"solved the same day's baseline {len(baselines)} times"


def test_cancelled_jobs_do_not_shape_the_quoted_day(temp_db):
    """A cancelled order still sits in the table. It must not inflate what another customer is
    told their slot costs."""
    day = PlanningClock.horizon_dates()[0]
    cancelled = _confirmed(temp_db, "Gone", "738099", day)
    cancelled.set_planning_status(PlanningStatus.CANCELLED)
    cancelled.locked_window = None
    cancelled.delivery_date = day
    temp_db.save_job(cancelled)

    context = CandidateService(repo=temp_db).day_context(day)

    assert context.jobs == []
    assert context.is_empty
