"""Deterministic demo data at real Singapore addresses, positioned against the planning clock.

Two things this file is careful about.

**Real locations.** Every seeded customer sits at an actual building, resolved once from OneMap and
committed below with the address it came from. The alternative -- deriving coordinates from the
postal district -- put four customers on one identical pixel with the one-minute drive-time floor
between them, which makes a map look broken and hides a newly booked stop entirely. The coordinates
are primed into the geocode cache before seeding, so this stays offline and byte-identical every run
while still being genuinely real.

**Shape.** Positioned against `PlanningClock.today()`, not fixed dates, because the previous seed's
hardcoded dates fell into the past and left the bookable horizon empty. The scenario is:

- three of the four horizon dates carry work, one is deliberately empty;
- one day is a tight eastern cluster near the depot, one is scattered across the island;
- pending orders carry 2-3 windows, including one that cannot be served and one whose only choices
  would open the empty day;
- two customers have agreed to an earlier delivery, so a freed slot has somewhere to go;
- one confirmed order is the intended subject of the readiness delay.
"""
from __future__ import annotations

import sqlite3
from datetime import time as Time

from dispatch_agent.config import settings
from dispatch_agent.db import JobsRepository, init_db
from dispatch_agent.geo.geocoder import geocode_postal_code, seed_cache
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

# Resolved from OneMap's address database (onemap.gov.sg), committed so seeding needs no network.
# Every one is a distinct real building -- a test asserts no two seeded stops share a coordinate.
KNOWN_LOCATIONS: dict[str, tuple[float, float, str]] = {
    # East, close to the SUTD depot -- the tight cluster.
    "469123": (1.33096, 103.94704, "22 Bedok Walk"),
    "520101": (1.34085, 103.95143, "101 Simei Street 1"),
    "529536": (1.35426, 103.94507, "10 Tampines Central 1, Tampines One"),
    "486123": (1.33368, 103.95050, "2 Changi South Lane"),
    # Far from the eastern cluster, so freeing this slot returns real drive time.
    "018956": (1.28240, 103.85841, "10 Bayfront Avenue, Sands Expo"),
    # Island-wide.
    "640690": (1.34119, 103.70671, "690 Jurong West Central 1"),
    "730680": (1.43963, 103.80212, "680 Woodlands Avenue 6, Admiralty Place"),
    "508988": (1.38356, 103.96978, "25 Loyang Crescent"),
    "408564": (1.32634, 103.89626, "10 Ubi Crescent, Ubi Techpark"),
    # West cluster.
    "648886": (1.33945, 103.70669, "1 Jurong West Central 2, Jurong Point"),
    "600101": (1.33701, 103.73879, "101 Jurong East Street 13"),
    "129588": (1.31497, 103.76427, "3155 Commonwealth Avenue West"),
    # Pending orders, spread out.
    "249715": (1.30465, 103.82491, "1 Cuscaden Road"),
    "307591": (1.31719, 103.84361, "101 Thomson Road, United Square"),
    "760101": (1.43054, 103.82767, "101 Yishun Avenue 5"),
    "569933": (1.36939, 103.84848, "53 Ang Mo Kio Avenue 3, AMK Hub"),
    "149544": (1.30443, 103.79679, "1 Commonwealth Lane"),
    "318993": (1.34318, 103.85031, "998 Toa Payoh North"),
    "428769": (1.30336, 103.90469, "50 East Coast Road"),
    "757713": (1.44820, 103.81950, "30 Sembawang Drive, Sun Plaza"),
}


def _window(start, end) -> TimeWindow:
    return TimeWindow(start=Time(*start), end=Time(*end))


def _address(postal: str) -> Address:
    located = geocode_postal_code(postal)
    return Address(
        raw_text=located.formatted_address or f"Singapore {postal}",
        postal_code=postal,
        coordinates=located.coordinates,
        geocode_source=located.source,
        formatted_address=located.formatted_address,
    )


def _clear_existing() -> None:
    """Wipe only the tables this script owns. The geocode and drive-time caches survive, so a
    reseed costs nothing and stays identical."""
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
    """An appointment already promised -- the workload a new order must fit around."""
    return JobRecord(
        customer_name=name,
        phone=f"9{abs(hash(name)) % 10_000_000:07d}",
        address=_address(postal),
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
    """An order awaiting planning: acceptable windows, no agreed date, nothing promised."""
    return JobRecord(
        customer_name=name,
        phone=f"9{abs(hash(name)) % 10_000_000:07d}",
        address=_address(postal),
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
    seed_cache(KNOWN_LOCATIONS)  # real coordinates, no network needed
    _clear_existing()
    repo = JobsRepository()
    d1, d2, d3, d4 = PlanningClock.horizon_dates()
    jobs: list[JobRecord] = []

    # d1 -- a tight eastern cluster minutes from the depot. Adding another eastern stop here is
    # genuinely cheap, and now visibly so: four distinct pins a few kilometres apart.
    for name, postal in [
        ("Tan Wei Ming", "469123"), ("Nurul Aisyah", "520101"),
        ("Kumar Raj", "529536"), ("Chen Li Hua", "486123"),
    ]:
        jobs.append(_confirmed(name, postal, d1, ((9, 0), (18, 0))))
    # The readiness-delay subject: a long cabinet job in Marina Bay, far from the eastern cluster,
    # so freeing her slot hands back real driving time rather than a rounding error.
    jobs.append(_confirmed("Priya Nair", "018956", d1, ((10, 0), (12, 30)), JobType.CABINET))

    # d2 -- four corners of the island, so this day is already expensive.
    for name, postal in [
        ("Lim Hui Ying", "640690"), ("Marcus Tan", "730680"),
        ("Farid Rahman", "508988"), ("Grace Wong", "408564"),
    ]:
        jobs.append(_confirmed(name, postal, d2, ((9, 0), (18, 0)), JobType.BED))

    # d3 -- deliberately EMPTY. Any order sent here opens a whole delivery day.

    # d4 -- a western cluster; the first two agreed to come forward if a slot frees up.
    for i, (name, postal) in enumerate([
        ("Siti Rahim", "648886"), ("Daniel Ng", "600101"), ("Anita Menon", "129588"),
    ]):
        jobs.append(_confirmed(name, postal, d4, ((9, 0), (18, 0)), early=(i < 2)))

    jobs += [
        # East-side: the busy eastern day should beat the empty one despite the preference order.
        _pending("Mrs Chua", "428769", [(d3, ((9, 0), (18, 0))), (d1, ((9, 0), (13, 0)))]),
        # Both windows on the empty day -- the only way to serve them is to open it.
        _pending("Mr Rajan", "757713", [(d3, ((9, 0), (12, 0))), (d3, ((14, 0), (18, 0)))]),
        # Disjoint windows on one day: must not be scheduled in the middle.
        _pending("Ms Wong", "307591", [(d1, ((9, 0), (10, 30))), (d1, ((15, 0), (18, 0)))]),
        # A cabinet (105 min) against a 30-minute window: infeasible, and must be flagged as such.
        _pending("Mr Iskandar", "249715", [(d2, ((9, 0), (9, 30))), (d4, ((13, 0), (18, 0)))],
                 JobType.CABINET),
        _pending("Mrs Devi", "149544", [(d4, ((9, 0), (13, 0))), (d2, ((13, 0), (18, 0)))], JobType.BED),
        _pending("Mr Goh", "318993", [(d1, ((13, 0), (18, 0))), (d4, ((9, 0), (18, 0)))], early=True),
        _pending("Ms Farah", "760101", [(d2, ((9, 0), (12, 0))), (d3, ((9, 0), (18, 0)))]),
    ]

    for job in jobs:
        repo.save_job(job)

    confirmed = sum(1 for j in jobs if j.planning_status is PlanningStatus.CONFIRMED)
    precise = sum(1 for j in jobs if j.address.precisely_located)
    return (
        f"Seeded {len(jobs)} orders relative to {PlanningClock.today()}:\n"
        f"  {confirmed} confirmed across {d1}, {d2}, {d4}\n"
        f"  {d3} deliberately left empty\n"
        f"  {len(jobs) - confirmed} awaiting planning, each with 2 acceptable windows\n"
        f"  2 customers have agreed to an earlier delivery\n"
        f"  {precise}/{len(jobs)} at real building coordinates\n"
        f"  'Priya Nair' on {d1} is the intended readiness-delay subject"
    )


def main() -> str:
    summary = seed()
    print(summary)
    return summary


if __name__ == "__main__":
    main()
