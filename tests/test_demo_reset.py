"""The state the demo opens in.

The seeded database had confirmed deliveries and no published routes, so Daily Routes said
"Nothing published for this day" beside a list of five of them. That reads as a broken page rather
than an empty one -- and worse, with no v1 there is nothing for a booking to turn into a v2, so the
before/after comparison the whole demo turns on had nothing to compare against.
"""
from datetime import date

import pytest

from dispatch_agent import config
from dispatch_agent.db import JobsRepository
from dispatch_agent.planning import plan_service
from dispatch_agent.planning.clock import PlanningClock

BASE = date(2026, 9, 3)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


@pytest.fixture()
def seeded(temp_db, monkeypatch):
    """The demo scenario, in this test's own database."""
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import seed_test_clients

    seed_test_clients.seed()
    return JobsRepository()


def test_every_populated_day_opens_with_a_published_route(seeded):
    """The headline. A day with confirmed deliveries must have a v1 to show."""
    populated = [
        d for d in PlanningClock.horizon_dates() if plan_service.routable_jobs(seeded, d)
    ]
    assert populated, "the seed must put work on at least one horizon day"

    for day in populated:
        plan = seeded.active_plan(day)
        assert plan is not None, f"{day} has deliveries but no published route"
        assert plan.version == 1, f"{day} should open at v1, not v{plan.version}"
        assert plan.sequence.stops, f"{day}'s published route has no stops"


def test_both_cycle_days_are_published(seeded):
    """The replacement for "one day is deliberately empty", which was true of the old four-day
    horizon and is the opposite of what this demo needs.

    A coordination cycle is a Friday and a Saturday that BOTH carry a published route. If either
    is missing there is no cycle, `PlanningClock.coordination_cycle()` returns None, and every
    customer escalates to a human instead of being offered anything -- so a half-seeded database
    does not degrade the demo, it deletes it.
    """
    days = PlanningClock.horizon_dates()
    assert len(days) == 2

    for day in days:
        assert plan_service.routable_jobs(seeded, day), f"{day} has no work to route"
        assert seeded.active_plan(day) is not None, f"{day} was never published"

    assert PlanningClock.coordination_cycle(repo=seeded) is not None


def test_each_published_route_covers_that_day_s_confirmed_work(seeded):
    for day in PlanningClock.horizon_dates():
        plan = seeded.active_plan(day)
        if plan is None:
            continue
        expected = {job.id for job in plan_service.routable_jobs(seeded, day)}
        assert {stop.job_id for stop in plan.sequence.stops} == expected


def test_the_baseline_routes_carry_the_figures_the_before_panel_needs(seeded):
    """A v1 with no distance or completion time makes the before/after column read "—"."""
    for day in PlanningClock.horizon_dates():
        plan = seeded.active_plan(day)
        if plan is None:
            continue
        assert plan.sequence.round_trip_drive_minutes > 0, day
        assert plan.sequence.distance_recorded, day
        assert plan.sequence.completion_minutes > 0, day


def test_a_booking_turns_the_baseline_into_v2(seeded):
    """What the whole demo depends on: v1 exists, the booking makes v2, nobody moves."""
    from dispatch_agent.models import AvailabilityOption, PlanningStatus, TimeWindow
    from datetime import time
    from dispatch_agent.planning import offer_service
    from dispatch_agent.planning.candidate_service import CandidateService

    day = next(d for d in PlanningClock.horizon_dates() if seeded.active_plan(d))
    before = seeded.active_plan(day)

    # The seeded demo customers deliberately carry no availability at all -- they say when they
    # are free in the thread, in their own words. So this test states it for them.
    order = next(
        job for job in seeded.all_jobs()
        if job.planning_status.value in ("pending_availability", "pending_planning")
    )
    order.availability_options = [
        AvailabilityOption(date=day, window=TimeWindow(start=time(10, 0), end=time(21, 0)))
    ]
    order.set_planning_status(PlanningStatus.PENDING_PLANNING)
    seeded.save_job(order)

    evaluations = CandidateService(repo=seeded).evaluate_all(order)
    if not any(e.feasible for e in evaluations):
        pytest.skip("that day cannot take another stop")
    offer = offer_service.create_offer(seeded, order, evaluations)
    outcome = offer_service.accept_offer(seeded, offer.id, offer.options[0].id)

    after = seeded.active_plan(day)
    assert before.version == 1 and after.version == 2
    assert len(after.sequence.stops) == len(before.sequence.stops) + 1
    assert outcome.job.id in {s.job_id for s in after.sequence.stops}

    # Every promise that existed before still sits inside the window it was given.
    jobs = {j.id: j for j in seeded.jobs_for_date(day)}
    for stop in after.sequence.stops:
        job = jobs[stop.job_id]
        if job.id == outcome.job.id or not job.locked_window:
            continue
        assert job.locked_window.start <= stop.arrival_window.start
        assert stop.arrival_window.start <= job.locked_window.end


def test_reset_preserves_the_provider_caches(tmp_path, monkeypatch):
    """The caches are not demo data -- they are every geocode and drive time already paid for.

    Dropping them makes the next run slow, billable, and dependent on the network being up at the
    worst possible moment. They also live in tables created lazily by the modules that own them,
    so a fresh schema does not have them and a naive restore fails silently.
    """
    import sqlite3
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import reset_demo

    db = tmp_path / "demo.db"
    monkeypatch.setattr(config.settings, "db_path", str(db))
    from dispatch_agent.db import init_db
    from dispatch_agent.geo import geocoder, matrix_cache

    init_db(db)
    conn = sqlite3.connect(db)
    conn.executescript(geocoder.SCHEMA)
    conn.executescript(matrix_cache.SCHEMA)
    conn.execute(
        "INSERT INTO geocode_cache (postal_code, lat, lng, source, formatted_address, fetched_at)"
        " VALUES ('999999', 1.3, 103.8, 'google', 'sentinel', '2026-01-01T00:00:00')"
    )
    conn.execute(
        "INSERT INTO drive_time_cache (provider, o_lat, o_lng, d_lat, d_lng, minutes, km, fetched_at)"
        " VALUES ('google', 1, 2, 3, 4, 11, 2.5, '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    saved = reset_demo._read_caches(db)

    # Restored into a fresh file rather than the same one re-created. Identical code path, and it
    # avoids fighting Windows over a handle SQLite has not let go of yet -- which would make this
    # test flaky about the operating system rather than truthful about the caches.
    rebuilt = tmp_path / "rebuilt.db"
    init_db(rebuilt)
    restored = reset_demo._write_caches(rebuilt, saved)

    conn = sqlite3.connect(rebuilt)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM geocode_cache WHERE postal_code = '999999'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM drive_time_cache").fetchone()[0] == 1
    finally:
        conn.close()
    assert "geocode_cache" in restored and "drive_time_cache" in restored


def test_reset_endpoint_restores_the_two_clean_v1_routes(temp_db, monkeypatch):
    """The browser button must remove rehearsal data and restore the exact recording state."""
    from dispatch_agent.models import Address, JobRecord, JobType, PlanningStatus
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())
    repo = JobsRepository()
    repo.save_job(
        JobRecord(
            customer_name="Left over from rehearsal",
            phone="90000000",
            address=Address(raw_text="Test address", postal_code="408564"),
            job_type=JobType.PET_FOOD_BOX,
            planning_status=PlanningStatus.PENDING_AVAILABILITY,
            duration_minutes=10,
            raw_message="[test rehearsal data]",
        )
    )

    from dispatch_agent.webapp.main import app

    response = TestClient(app).post("/api/demo/reset")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["job_count"] == 18
    assert [(route["version"], route["stop_count"]) for route in body["routes"]] == [
        (1, 8),
        (1, 8),
    ]
    assert not any(job.customer_name == "Left over from rehearsal" for job in repo.all_jobs())
