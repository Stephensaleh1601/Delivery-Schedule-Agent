"""Deterministic demo data, built relative to the planning clock.

Everything is positioned against PlanningClock.today() rather than hardcoded, because the
previous seed used fixed dates that quietly fell into the past and left the bookable horizon
empty. Set DEMO_BASE_DATE to pin the clock and this data lands identically every run.

The scenario is shaped for the demo:

- three of the four horizon dates already have work, one is deliberately empty, so there is a
  real routing reason to prefer one date over another;
- one existing day is geographically clustered, one is scattered;
- pending orders carry 2-3 windows, including one that cannot be served and one whose only
  choice would open the empty day;
- two customers have agreed to an earlier delivery, so a freed slot has somewhere to go;
- one confirmed order is the intended candidate for a readiness delay.
"""
from __future__ import annotations

import sqlite3
from datetime import time as Time

from dispatch_agent.config import settings
from dispatch_agent.db import JobsRepository, init_db
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.models import (
    DEFAULT_DURATION_MINUTES_BY_JOB_TYPE,
    Address,
    AvailabilityOption,
    JobRecord,
    JobType,
    PlanningStatus,
    TimeWindow,
)
from dispatch_agent.planning.clock import PlanningClock

# Postal sectors chosen so the clusters are genuinely apart on the map.
EAST = ["486123", "489123", "488123", "467123"]  # Bedok / Tampines / Changi
WEST = ["638123", "659123", "649123"]  # Jurong / Bukit Batok
CENTRAL = ["018956", "119613", "238874", "168123"]
NORTH = ["738099", "760123", "769123"]


def _window(start, end) -> TimeWindow:
    return TimeWindow(start=Time(*start), end=Time(*end))


def _clear_existing() -> None:
    """Wipe only the tables this script owns. Left in place rather than dropping the file so a
    developer's schema (and the drive-time cache) survives a reseed."""
    with sqlite3.connect(settings.db_path) as conn:
        for table in (
            "jobs", "day_sequences", "notifications", "appointment_offers",
            "route_plan_versions", "planning_events", "agent_runs", "messages",
            "coordinator_exceptions",
        ):
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.Error:
                pass


def _confirmed(name, postal, day, window, job_type=JobType.SOFA, early=False) -> JobRecord:
    """An appointment already promised to a customer -- the workload a new order must fit around."""
    return JobRecord(
        customer_name=name,
        phone=f"9{abs(hash(name)) % 10_000_000:07d}",
        address=Address(
            raw_text=f"Blk {postal[:3]} {name.split()[0]} Road",
            postal_code=postal,
            coordinates=postal_code_to_coords(postal),
        ),
        job_type=job_type,
        availability=[_window(*window)],
        locked_window=_window(*window),
        availability_options=[AvailabilityOption(date=day, window=_window(*window))],
        delivery_date=day,
        planning_status=PlanningStatus.CONFIRMED,
        status="approved",
        can_deliver_early=early,
        duration_minutes=DEFAULT_DURATION_MINUTES_BY_JOB_TYPE[job_type],
        raw_message="[seeded: confirmed booking]",
    )


def _pending(name, postal, options, job_type=JobType.SOFA, early=False) -> JobRecord:
    """An order awaiting planning: acceptable windows, but no date agreed and nothing promised."""
    return JobRecord(
        customer_name=name,
        phone=f"9{abs(hash(name)) % 10_000_000:07d}",
        address=Address(
            raw_text=f"Blk {postal[:3]} {name.split()[0]} Street",
            postal_code=postal,
            coordinates=postal_code_to_coords(postal),
        ),
        job_type=job_type,
        availability_options=[
            AvailabilityOption(date=day, window=_window(*window), preference_rank=rank)
            for rank, (day, window) in enumerate(options, start=1)
        ],
        planning_status=PlanningStatus.PENDING_PLANNING,
        can_deliver_early=early,
        duration_minutes=DEFAULT_DURATION_MINUTES_BY_JOB_TYPE[job_type],
        raw_message="[seeded: awaiting planning]",
    )


def seed() -> str:
    init_db()
    _clear_existing()
    repo = JobsRepository()
    d1, d2, d3, d4 = PlanningClock.horizon_dates()
    jobs: list[JobRecord] = []

    # d1 -- a tight eastern cluster. Adding an eastern stop here should be near-free.
    for i, postal in enumerate(EAST):
        jobs.append(_confirmed(f"Tan Wei{i}", postal, d1, ((9, 0), (18, 0))))
    # The intended subject of the readiness delay: mid-morning, so freeing it leaves usable room.
    jobs.append(_confirmed("Priya Nair", CENTRAL[0], d1, ((10, 0), (12, 30)), JobType.CABINET))

    # d2 -- scattered across the island, so it is already an expensive day.
    for i, postal in enumerate([WEST[0], NORTH[0], EAST[0], CENTRAL[1]]):
        jobs.append(_confirmed(f"Lim Hui{i}", postal, d2, ((9, 0), (18, 0)), JobType.BED))

    # d3 -- deliberately EMPTY. Any order sent here opens a whole new delivery day.

    # d4 -- a small western cluster, and the pool of customers happy to come forward.
    for i, postal in enumerate(WEST):
        jobs.append(
            _confirmed(f"Siti Rahim{i}", postal, d4, ((9, 0), (18, 0)), early=(i < 2))
        )

    # Pending orders awaiting planning.
    jobs += [
        # East-side: the busy eastern day should beat the empty one despite the preference order.
        _pending("Mrs Chua", EAST[1], [(d3, ((9, 0), (18, 0))), (d1, ((9, 0), (13, 0)))]),
        # Every window on the empty day -- the only way to serve them is to open it.
        _pending("Mr Rajan", NORTH[1], [(d3, ((9, 0), (12, 0))), (d3, ((14, 0), (18, 0)))]),
        # Disjoint windows on one day: must not be scheduled in the middle.
        _pending("Ms Wong", CENTRAL[2], [(d1, ((9, 0), (10, 30))), (d1, ((15, 0), (18, 0)))]),
        # A cabinet (105 min) against a 30-minute window: infeasible, and must be flagged as such.
        _pending("Mr Iskandar", CENTRAL[3], [(d2, ((9, 0), (9, 30))), (d4, ((13, 0), (18, 0)))],
                 JobType.CABINET),
        _pending("Mrs Devi", WEST[1], [(d4, ((9, 0), (13, 0))), (d2, ((13, 0), (18, 0)))], JobType.BED),
        _pending("Mr Goh", EAST[2], [(d1, ((13, 0), (18, 0))), (d4, ((9, 0), (18, 0)))], early=True),
        _pending("Ms Farah", NORTH[2], [(d2, ((9, 0), (12, 0))), (d3, ((9, 0), (18, 0)))]),
    ]

    for job in jobs:
        repo.save_job(job)

    confirmed = sum(1 for j in jobs if j.planning_status is PlanningStatus.CONFIRMED)
    pending = len(jobs) - confirmed
    return (
        f"Seeded {len(jobs)} orders relative to {PlanningClock.today()}:\n"
        f"  {confirmed} confirmed across {d1}, {d2}, {d4}\n"
        f"  {d3} deliberately left empty\n"
        f"  {pending} awaiting planning, each with 2 acceptable windows\n"
        f"  2 customers have agreed to an earlier delivery\n"
        f"  'Priya Nair' on {d1} is the intended readiness-delay subject"
    )


def main() -> str:
    summary = seed()
    print(summary)
    return summary


if __name__ == "__main__":
    main()
