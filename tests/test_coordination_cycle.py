"""The Friday/Saturday pair the agent coordinates, and why it is a pair rather than two dates.

The failure this file exists to prevent: advancing Friday and Saturday independently. If Friday
is unpublished and we simply take "the next published Friday", a customer can be offered this
week's Saturday alongside next week's Friday -- a cycle spanning eight days, in which the
fallback search compares two routes the driver never runs in the same week, and the "we'll
already be nearby" reasoning is a lie.
"""
from datetime import date as Date, time as Time

import pytest

from dispatch_agent import config
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.models import (
    Address,
    JobRecord,
    JobType,
    PlanningStatus,
    TimeWindow,
)
from dispatch_agent.planning import plan_service
from dispatch_agent.planning.clock import PlanningClock

WEDNESDAY = Date(2026, 9, 2)
THURSDAY = Date(2026, 9, 3)
FRIDAY = Date(2026, 9, 4)
SATURDAY = Date(2026, 9, 5)
NEXT_FRIDAY = Date(2026, 9, 11)
NEXT_SATURDAY = Date(2026, 9, 12)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", WEDNESDAY.isoformat())


def _publish(repo, day: Date, postal_code: str = "469123") -> None:
    """Give `day` an active published route with one confirmed stop on it."""
    job = JobRecord(
        customer_name=f"Anchor {day.isoformat()}",
        phone="91230000",
        address=Address(
            raw_text="anchor", postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)
        ),
        job_type=JobType.SOFA,
        delivery_date=day,
        availability=[TimeWindow(start=Time(10, 0), end=Time(14, 0))],
        locked_window=TimeWindow(start=Time(10, 0), end=Time(14, 0)),
        planning_status=PlanningStatus.CONFIRMED,
        status="approved",
        duration_minutes=15,
        raw_message="seeded anchor",
    )
    repo.save_job(job)
    plan_service.publish_plan_version(
        plan_service.solve_day(repo, day), reason="test baseline route"
    )


def test_pair_is_this_week_when_both_are_published(temp_db):
    _publish(temp_db, FRIDAY)
    _publish(temp_db, SATURDAY)

    cycle = PlanningClock.coordination_cycle(repo=temp_db)

    assert cycle is not None
    assert (cycle.friday, cycle.saturday) == (FRIDAY, SATURDAY)
    assert cycle.dates == [FRIDAY, SATURDAY]


def test_an_unpublished_friday_advances_the_whole_pair(temp_db):
    """The heart of it: Saturday is published and only two days away, but its Friday is not.

    Taking that Saturday would mean pairing it with next week's Friday. The pair advances
    together instead.
    """
    _publish(temp_db, SATURDAY)
    _publish(temp_db, NEXT_FRIDAY)
    _publish(temp_db, NEXT_SATURDAY)

    cycle = PlanningClock.coordination_cycle(repo=temp_db)

    assert (cycle.friday, cycle.saturday) == (NEXT_FRIDAY, NEXT_SATURDAY)
    assert SATURDAY not in cycle.dates


def test_an_unpublished_saturday_advances_the_whole_pair(temp_db):
    _publish(temp_db, FRIDAY)
    _publish(temp_db, NEXT_FRIDAY)
    _publish(temp_db, NEXT_SATURDAY)

    cycle = PlanningClock.coordination_cycle(repo=temp_db)

    assert (cycle.friday, cycle.saturday) == (NEXT_FRIDAY, NEXT_SATURDAY)


def test_a_friday_inside_the_notice_period_advances_the_whole_pair(monkeypatch, temp_db):
    """Today is Thursday, so Friday is one day away and fails the two-day notice.

    Saturday passes it -- and that is exactly the trap. The pair must advance to next week
    rather than keeping a Saturday whose Friday nobody can book.
    """
    monkeypatch.setattr(config.settings, "demo_base_date", THURSDAY.isoformat())
    _publish(temp_db, FRIDAY)
    _publish(temp_db, SATURDAY)
    _publish(temp_db, NEXT_FRIDAY)
    _publish(temp_db, NEXT_SATURDAY)

    cycle = PlanningClock.coordination_cycle(repo=temp_db)

    assert (cycle.friday, cycle.saturday) == (NEXT_FRIDAY, NEXT_SATURDAY)


def test_no_complete_pair_is_a_real_answer(temp_db):
    """Nothing published anywhere. The caller must escalate, not receive half a cycle."""
    assert PlanningClock.coordination_cycle(repo=temp_db) is None


def test_a_lone_published_friday_is_not_a_cycle(temp_db):
    _publish(temp_db, FRIDAY)

    assert PlanningClock.coordination_cycle(repo=temp_db) is None


def test_bookability_does_not_wait_for_a_published_route(temp_db):
    """The bootstrap paradox, pinned.

    A route is solved from the jobs assigned to its date, and a job is assigned by accepting an
    offer for that date. If a date had to be published before it could be offered, the first
    booking of any cycle would be impossible. So publication gates INSERTION, not bookability --
    which is why `horizon_dates` and `coordination_cycle` are different questions.
    """
    assert PlanningClock.horizon_dates() == [FRIDAY, SATURDAY]
    assert PlanningClock.is_within_horizon(FRIDAY)
    assert PlanningClock.coordination_cycle(repo=temp_db) is None


def test_horizon_dates_are_the_pair(temp_db):
    _publish(temp_db, FRIDAY)
    _publish(temp_db, SATURDAY)

    assert PlanningClock.horizon_dates() == [FRIDAY, SATURDAY]
    assert PlanningClock.horizon() == (FRIDAY, SATURDAY)


def test_only_the_cycle_dates_are_bookable(temp_db):
    assert PlanningClock.is_within_horizon(FRIDAY)
    assert PlanningClock.is_within_horizon(SATURDAY)
    # A weekday between them is not a delivery day at all.
    assert not PlanningClock.is_within_horizon(Date(2026, 9, 7))  # Monday
    # A Friday beyond the current cycle is a delivery day, but not this cycle's.
    assert not PlanningClock.is_within_horizon(NEXT_FRIDAY)
    # Yesterday, obviously.
    assert not PlanningClock.is_within_horizon(Date(2026, 9, 1))


def test_the_search_for_a_pair_is_bounded(temp_db, monkeypatch):
    """A repository with no published routes at all must terminate, not scan forever."""
    monkeypatch.setattr(config.settings, "cycle_search_weeks", 3)

    assert PlanningClock.coordination_cycle(repo=temp_db) is None
