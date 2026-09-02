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
