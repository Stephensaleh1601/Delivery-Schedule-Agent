"""Offers, locked appointments, and plan versioning -- driven as plain Python, no LLM, no HTTP.

This is where the product's central promise is verified: once a customer accepts a slot, nothing
the system does afterwards moves them. The end-to-end test at the bottom is the one that decides
whether the feature actually works.
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
    MessageDirection,
    OfferStatus,
    PlanStatus,
    PlanningStatus,
    TimeWindow,
)
from dispatch_agent.planning import offer_service, plan_service
from dispatch_agent.planning.candidate_service import CandidateService
from dispatch_agent.planning.clock import PlanningClock
from dispatch_agent.planning.offer_service import OfferError
from dispatch_agent.solver import LockedPlanInfeasibleError

BASE = date(2026, 9, 2)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


def _w(start, end):
    return TimeWindow(start=time(*start), end=time(*end))


def _order(repo, name="Mrs Tan", postal_code="018956", options=(), duration=45, early=False):
    job = JobRecord(
        customer_name=name,
        phone="91230000",
        address=Address(raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)),
        job_type=JobType.SOFA,
        availability_options=list(options),
        can_deliver_early=early,
        raw_message="booked via chat",
        duration_minutes=duration,
    )
    repo.save_job(job)
    return job


def _option(day, window=((9, 0), (18, 0)), rank=1):
    return AvailabilityOption(date=day, window=_w(*window), preference_rank=rank)


def _confirmed(repo, name, postal_code, day, window=((9, 0), (18, 0)), duration=45):
    job = JobRecord(
        customer_name=name,
        address=Address(raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)),
        job_type=JobType.SOFA,
        availability=[_w(*window)],
        locked_window=_w(*window),
        delivery_date=day,
        planning_status=PlanningStatus.CONFIRMED,
        status="approved",
        raw_message="seed",
        duration_minutes=duration,
    )
    repo.save_job(job)
    return job


# -- offers -------------------------------------------------------------------


def test_customer_is_offered_the_best_two_feasible_slots(temp_db):
    days = PlanningClock.horizon_dates()
    order = _order(temp_db, options=[_option(d) for d in days])

    evaluations = CandidateService(repo=temp_db).evaluate_all(order)
    offer = offer_service.create_offer(temp_db, order, evaluations)

    assert len(offer.options) == 2, "a customer should get a choice, not a single take-it-or-leave-it"
    assert offer.status is OfferStatus.SENT
    assert temp_db.get_job(order.id).planning_status is PlanningStatus.OFFERED


def test_a_single_feasible_option_is_offered_alone(temp_db):
    day = PlanningClock.horizon_dates()[0]
    order = _order(temp_db, options=[
        _option(day),
        _option(BASE + timedelta(days=1)),  # outside the horizon -- infeasible
    ])

    evaluations = CandidateService(repo=temp_db).evaluate_all(order)
    offer = offer_service.create_offer(temp_db, order, evaluations)

    assert len(offer.options) == 1
    assert "only one of your preferred times" in offer_service.offer_message(offer)


def test_no_feasible_option_raises_rather_than_inventing_a_slot(temp_db):
    """The system must never quote a time the customer did not offer, however convenient."""
    order = _order(temp_db, options=[_option(BASE + timedelta(days=1))])
    evaluations = CandidateService(repo=temp_db).evaluate_all(order)

    with pytest.raises(OfferError, match="can be fitted into the schedule"):
        offer_service.create_offer(temp_db, order, evaluations)


def test_offer_rounds_are_capped(temp_db):
    """Enforced by counting stored offers, not by asking a model to keep track -- a model that
    forgets must not be able to badger a customer indefinitely."""
    days = PlanningClock.horizon_dates()
    order = _order(temp_db, options=[_option(d) for d in days])
    service = CandidateService(repo=temp_db)

    for _ in range(offer_service.MAX_OFFER_ROUNDS):
        offer = offer_service.create_offer(temp_db, order, service.evaluate_all(order))
        offer_service.reject_offer(temp_db, offer.id)

    with pytest.raises(OfferError, match="escalating"):
        offer_service.create_offer(temp_db, order, service.evaluate_all(order))


def test_a_second_offer_does_not_repeat_a_rejected_slot(temp_db):
    days = PlanningClock.horizon_dates()
    order = _order(temp_db, options=[_option(d) for d in days])
    service = CandidateService(repo=temp_db)

    first = offer_service.create_offer(temp_db, order, service.evaluate_all(order))
    offer_service.reject_offer(temp_db, first.id)
    second = offer_service.create_offer(temp_db, order, service.evaluate_all(order))

    already = {s.availability_option_id for s in first.options}
    assert not any(s.availability_option_id in already for s in second.options)


def test_offer_message_hides_our_internal_reasoning(temp_db):
    """A customer must not be told their preference was operationally inconvenient."""
    days = PlanningClock.horizon_dates()
    order = _order(temp_db, options=[_option(d, rank=i + 1) for i, d in enumerate(days)])
    offer = offer_service.create_offer(temp_db, order, CandidateService(repo=temp_db).evaluate_all(order))

    text = offer_service.offer_message(offer).lower()
    for leak in ("score", "penalty", "incremental", "drive", "route", "optimis"):
        assert leak not in text, f"offer message leaks internal reasoning: {leak!r}"


# -- acceptance and locks -----------------------------------------------------


def test_accepting_locks_the_window_and_publishes_a_plan(temp_db):
    days = PlanningClock.horizon_dates()
    order = _order(temp_db, options=[_option(d) for d in days])
    offer = offer_service.create_offer(temp_db, order, CandidateService(repo=temp_db).evaluate_all(order))
    chosen = offer.options[0]

    outcome = offer_service.accept_offer(temp_db, offer.id, chosen.id)

    job = temp_db.get_job(order.id)
    assert job.planning_status is PlanningStatus.CONFIRMED
    assert job.delivery_date == chosen.date
    assert job.locked_window == chosen.window
    assert job.is_locked
    assert outcome.plan.status is PlanStatus.ACTIVE
    assert temp_db.active_plan(chosen.date).id == outcome.plan.id


def test_accepting_twice_does_not_create_a_second_plan_version(temp_db):
    """A double-tapped button must be harmless. Guarded by a conditional UPDATE, so two
    simultaneous clicks cannot both pass the check."""
    days = PlanningClock.horizon_dates()
    order = _order(temp_db, options=[_option(d) for d in days])
    offer = offer_service.create_offer(temp_db, order, CandidateService(repo=temp_db).evaluate_all(order))
    chosen = offer.options[0]

    first = offer_service.accept_offer(temp_db, offer.id, chosen.id)
    second = offer_service.accept_offer(temp_db, offer.id, chosen.id)

    assert second.idempotent is True
    assert len(temp_db.plan_versions(chosen.date)) == len(temp_db.plan_versions(first.plan.delivery_date))
    assert len(temp_db.plan_versions(chosen.date)) == 1


def test_accepting_closes_the_other_offered_slot(temp_db):
    days = PlanningClock.horizon_dates()
    order = _order(temp_db, options=[_option(d) for d in days])
    service = CandidateService(repo=temp_db)
    first = offer_service.create_offer(temp_db, order, service.evaluate_all(order))
    offer_service.reject_offer(temp_db, first.id)
    second = offer_service.create_offer(temp_db, order, service.evaluate_all(order))

    offer_service.accept_offer(temp_db, second.id, second.options[0].id)

    statuses = {o.id: o.status for o in temp_db.offers_for_order(order.id)}
    assert statuses[second.id] is OfferStatus.ACCEPTED
    assert statuses[first.id] is OfferStatus.REJECTED


def test_a_replan_cannot_move_a_confirmed_appointment(temp_db):
    """The promise, stated as a test. Adding more work to a day must never shift someone who
    already has a time."""
    day = PlanningClock.horizon_dates()[0]
    promised = _confirmed(temp_db, "Promised", "018956", day, window=((14, 0), (15, 0)), duration=45)
    for i in range(3):
        _confirmed(temp_db, f"Filler {i}", "486123", day, window=((9, 0), (18, 0)))

    plan = plan_service.replan_day(temp_db, day, reason="more work added")

    stop = next(s for s in plan.sequence.stops if s.job_id == promised.id)
    assert promised.locked_window.start <= stop.arrival_window.start
    assert stop.arrival_window.end <= promised.locked_window.end


def test_assert_locks_respected_catches_a_plan_that_would_move_someone(temp_db):
    """assert_locks_respected is the backstop for a solver regression that returns a
    plausible-looking plan rather than reporting infeasibility."""
    day = PlanningClock.horizon_dates()[0]
    job = _confirmed(temp_db, "Promised", "018956", day, window=((14, 0), (15, 0)))
    sequence = plan_service.solve_day(temp_db, day)

    # Simulate the regression: the same plan, but the customer moved to the morning.
    moved = sequence.model_copy(deep=True)
    moved.stops[0].arrival_window = _w((9, 0), (9, 45))

    with pytest.raises(LockedPlanInfeasibleError, match="was promised"):
        plan_service.assert_locks_respected(moved, {job.id: job})


# -- plan versioning ----------------------------------------------------------


def test_a_new_version_supersedes_the_old_one_which_stays_readable(temp_db):
    day = PlanningClock.horizon_dates()[0]
    _confirmed(temp_db, "First", "018956", day)
    v1 = plan_service.replan_day(temp_db, day, reason="initial plan")

    _confirmed(temp_db, "Second", "486123", day)
    v2 = plan_service.replan_day(temp_db, day, reason="second customer confirmed")

    versions = {p.version: p for p in temp_db.plan_versions(day)}
    assert versions[1].status is PlanStatus.SUPERSEDED
    assert versions[2].status is PlanStatus.ACTIVE
    assert len(versions[1].sequence.stops) == 1, "v1 must remain readable exactly as published"
    assert versions[2].parent_plan_id == v1.id
    assert versions[2].reason_created == "second customer confirmed"
    assert temp_db.active_plan(day).id == v2.id


def test_replanning_an_unchanged_day_does_not_bump_the_version(temp_db):
    """Otherwise a demo would reach "Plan v7" through no real changes and the history would be
    worthless."""
    day = PlanningClock.horizon_dates()[0]
    _confirmed(temp_db, "Only", "018956", day)

    first = plan_service.replan_day(temp_db, day, reason="initial")
    again = plan_service.replan_day(temp_db, day, reason="nothing actually changed")

    assert again.id == first.id
    assert len(temp_db.plan_versions(day)) == 1


# -- the whole path -----------------------------------------------------------


def test_end_to_end_booking_to_locked_route(temp_db):
    """product ready -> availability -> offer -> confirmation -> locked route.

    The path the brief says must be exercised before anyone claims this works.
    """
    days = PlanningClock.horizon_dates()
    # An existing clustered day, so there is a real routing reason to prefer one date.
    for i in range(3):
        _confirmed(temp_db, f"Existing {i}", "486123", days[0])
    plan_service.replan_day(temp_db, days[0], reason="existing bookings")

    # 1. Customer books with three acceptable windows, no date agreed yet.
    order = _order(temp_db, "Mrs Tan", "489123", options=[
        _option(days[0], window=((9, 0), (13, 0)), rank=2),
        _option(days[1], rank=1),
        _option(days[2], window=((14, 0), (18, 0)), rank=3),
    ])
    assert order.delivery_date is None and order.locked_window is None

    # 2. Every option is priced against the real route.
    evaluations = CandidateService(repo=temp_db).evaluate_all(order)
    assert all(e.proposed_sequence is not None for e in evaluations if e.feasible)

    # 3. The best two are offered.
    offer = offer_service.create_offer(temp_db, order, evaluations)
    offer_service.record_message(temp_db, order.id, offer_service.offer_message(offer))
    assert len(offer.options) == 2

    # 4. Customer accepts one; it becomes a locked promise.
    chosen = offer.options[0]
    outcome = offer_service.accept_offer(temp_db, offer.id, chosen.id)

    job = temp_db.get_job(order.id)
    assert job.planning_status is PlanningStatus.CONFIRMED
    assert job.is_locked

    # 5. The published route contains them, in their promised window, and nobody else moved.
    plan = temp_db.active_plan(chosen.date)
    assert plan.id == outcome.plan.id
    assert job.id in {s.job_id for s in plan.sequence.stops}
    for stop in plan.sequence.stops:
        stopped_job = temp_db.get_job(stop.job_id)
        if stopped_job.is_locked:
            assert stopped_job.locked_window.start <= stop.arrival_window.start
            assert stop.arrival_window.end <= stopped_job.locked_window.end

    # 6. The customer was actually told, and it is recorded.
    sent = [m for m in temp_db.messages(order.id) if m.direction is MessageDirection.OUTBOUND]
    assert len(sent) == 2, "customer should have received an offer and a confirmation"
