"""The demo scenario: two published routes, two drivers, two customers.

Deliberately small. This exists to make one five-minute story work, not to look like a busy week.

**Real locations.** Every address is an actual Singapore building, resolved once from OneMap and
committed below with the address it came from. Deriving coordinates from the postal district
instead put several customers on one identical pixel with the one-minute drive-time floor between
them, which makes a map look broken and hides a newly inserted stop entirely. The coordinates are
primed into the geocode cache before seeding, so this stays offline and byte-identical every run
while still being genuinely real.

**The difficult customer's geography is load-bearing, not decorative.** The whole demo turns on
them getting exactly three alternatives, and that number is a property of where the seeded
stops are, not of the ranking code. They live in Toa Payoh, which is Central, so their cluster
day is Saturday. After they decline the Saturday morning offer:

    Saturday morning    rejected   the offer she declined
    Saturday afternoon  VALID      Thomson 3.9km, Cuscaden 6.7km
    Saturday evening    excluded   Jurong East 16.1km, Jurong West 20.8km -- nothing within 10km
    Friday morning      VALID      Serangoon NEX 3.4km
    Friday afternoon    VALID      Kovan 5.3km
    Friday evening      excluded   Loyang 18.2km, Woodlands 15.6km

Three. The two Friday options are the point of the demo: their region says Saturday, but the
Friday van already passes within a few kilometres of them, so a human dispatcher would fit them
in and so does this.

**Margins are deliberate.** The test suite measures in haversine and the recording may measure in
Google road distance, and the two disagree by enough to move a stop across a 10km line. Intended
anchors sit at 3-7km and intended non-anchors at 15km or more; nothing is seeded near the
boundary, because a demo that changes shape when the routing provider changes is not a demo.
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
from dispatch_agent.planning import plan_service
from dispatch_agent.planning.clock import PlanningClock
from dispatch_agent.planning.slots import AFTERNOON, EVENING, MORNING, DeliverySlot

# Resolved from OneMap's address database (onemap.gov.sg), committed so seeding needs no network.
# Every one is a distinct real building -- a test asserts no two seeded stops share a coordinate.
KNOWN_LOCATIONS: dict[str, tuple[float, float, str]] = {
    # -- Friday cluster: North, North-East, South, East --------------------------------
    "556083": (1.35077, 103.87230, "23 Serangoon Central, NEX"),
    "530201": (1.35780, 103.88365, "201 Hougang Street 21, Kovan City"),
    "538766": (1.37249, 103.89377, "90 Hougang Avenue 10, Hougang Mall"),
    "428769": (1.30336, 103.90469, "50 East Coast Road"),
    "469123": (1.33096, 103.94704, "22 Bedok Walk"),
    "460216": (1.32706, 103.93322, "216 Bedok North Street 1"),
    "529536": (1.35426, 103.94507, "10 Tampines Central 1, Tampines One"),
    "520101": (1.34085, 103.95143, "101 Simei Street 1"),
    "486123": (1.33368, 103.95050, "2 Changi South Lane"),
    "508988": (1.38356, 103.96978, "25 Loyang Crescent"),
    "730680": (1.43963, 103.80212, "680 Woodlands Avenue 6, Admiralty Place"),
    "760101": (1.43054, 103.82767, "101 Yishun Avenue 5"),
    "757713": (1.44820, 103.81950, "30 Sembawang Drive, Sun Plaza"),
    "098585": (1.26429, 103.82230, "1 HarbourFront Walk, VivoCity"),
    "090108": (1.27336, 103.82535, "108 Bukit Purmei Road"),
    # -- Saturday cluster: Central, City, West -----------------------------------------
    "573969": (1.35856, 103.83356, "22 Sin Ming Lane, Midview City"),
    "569933": (1.36939, 103.84848, "53 Ang Mo Kio Avenue 3, AMK Hub"),
    "307591": (1.31719, 103.84361, "101 Thomson Road, United Square"),
    "249715": (1.30465, 103.82491, "1 Cuscaden Road"),
    "318993": (1.34318, 103.85031, "998 Toa Payoh North"),
    "149544": (1.30443, 103.79679, "1 Commonwealth Lane"),
    "129588": (1.31497, 103.76427, "3155 Commonwealth Avenue West"),
    "018956": (1.28240, 103.85841, "10 Bayfront Avenue, Sands Expo"),
    "600101": (1.33701, 103.73879, "101 Jurong East Street 13"),
    "640690": (1.34119, 103.70671, "690 Jurong West Central 1"),
    "648886": (1.33945, 103.70669, "1 Jurong West Central 2, Jurong Point"),
    # -- The two customers the demo is about. Neither is a stop on either route. --------
    "408564": (1.32634, 103.89626, "10 Ubi Crescent, Ubi Techpark"),
}

# One driver per route. Static on purpose: with a single van per day there is nothing to assign,
# and a drivers table would be schema for a decision nobody makes.
DRIVERS: dict[str, dict[str, str]] = {
    "friday": {"id": "driver-fri", "name": "Ravi Kumaran", "phone": "9123 4567"},
    "saturday": {"id": "driver-sat", "name": "Siti Nurhaliza", "phone": "9234 5678"},
}


def driver_for(weekday: int) -> dict[str, str]:
    """Friday is 4, Saturday is 5 in date.weekday() terms."""
    return DRIVERS["friday"] if weekday == 4 else DRIVERS["saturday"]


# (name, postal code, slot, order type). The slot is what the customer was promised, so it is the
# locked window the solver must honour -- and it is what decides which window a nearby insertion
# lands in, which is how the "exactly three" scenario above is built.
FRIDAY_ROUTE: list[tuple[str, str, DeliverySlot, JobType]] = [
    ("Tan Wei Ming", "556083", MORNING, JobType.PET_FOOD_BOX),
    ("Nurul Aisyah", "428769", MORNING, JobType.PET_FOOD_BOX),
    ("Kumar Raj", "469123", MORNING, JobType.ONE_OFF_PET_ORDER),
    ("Chen Li Hua", "530201", AFTERNOON, JobType.PET_FOOD_BOX),
    ("Marcus Tan", "529536", AFTERNOON, JobType.PET_FOOD_BOX),
    ("Priya Nair", "460216", AFTERNOON, JobType.ONE_OFF_PET_ORDER),
    ("Farid Rahman", "508988", EVENING, JobType.PET_FOOD_BOX),
    ("Grace Wong", "730680", EVENING, JobType.PET_FOOD_BOX),
]

SATURDAY_ROUTE: list[tuple[str, str, DeliverySlot, JobType]] = [
    ("Lim Hui Ying", "573969", MORNING, JobType.PET_FOOD_BOX),
    ("Daniel Ng", "569933", MORNING, JobType.PET_FOOD_BOX),
    ("Anita Menon", "018956", MORNING, JobType.ONE_OFF_PET_ORDER),
    ("Siti Rahim", "307591", AFTERNOON, JobType.PET_FOOD_BOX),
    ("Mrs Devi", "249715", AFTERNOON, JobType.PET_FOOD_BOX),
    ("Mr Goh", "129588", AFTERNOON, JobType.ONE_OFF_PET_ORDER),
    ("Ms Farah", "600101", EVENING, JobType.PET_FOOD_BOX),
    ("Mr Iskandar", "640690", EVENING, JobType.PET_FOOD_BOX),
]

# The two customers the demo is about. Neither has stated a time yet -- they say it in the thread,
# in their own words, which is the whole point of the conversation.
BEST_CASE = ("Mrs Chua", "408564", JobType.PET_FOOD_BOX, "Monthly dog-food box")
DIFFICULT = ("Mr Rajan", "318993", JobType.PET_FOOD_BOX, "Cat-food subscription delivery")

PRODUCT_LABELS: dict[JobType, str] = {
    JobType.PET_FOOD_BOX: "Monthly dog-food box",
    JobType.ONE_OFF_PET_ORDER: "One-off cat-food order",
    JobType.OTHER: "Fresh pet food",
}


def _address(postal_code: str) -> Address:
    resolved = geocode_postal_code(postal_code)
    return Address(
        raw_text=KNOWN_LOCATIONS[postal_code][2],
        postal_code=postal_code,
        coordinates=resolved.coordinates,
        geocode_source=resolved.source,
        formatted_address=resolved.formatted_address,
    )


def _phone(name: str) -> str:
    return f"9{abs(hash(name)) % 10_000_000:07d}"


def _clear_existing() -> None:
    """Wipe the scenario, keep the caches.

    geocode_cache and drive_time_cache are deliberately untouched: they hold facts about the world
    (where a postal code is, how long a leg takes) that reseeding does not invalidate, and
    refetching them would make seeding need the network again.
    """
    tables = [
        "jobs", "day_sequences", "notifications", "appointment_offers", "route_plan_versions",
        "planning_events", "agent_runs", "messages", "coordinator_exceptions",
    ]
    conn = sqlite3.connect(settings.db_path)
    try:
        for table in tables:
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.Error:
                pass
        conn.commit()
    finally:
        conn.close()


def _confirmed(name, postal_code, day, slot: DeliverySlot, job_type: JobType) -> JobRecord:
    """A customer already on a published route, promised the slot they were given."""
    window = slot.window
    return JobRecord(
        customer_name=name,
        phone=_phone(name),
        address=_address(postal_code),
        job_type=job_type,
        availability=[window],
        locked_window=window,
        availability_options=[AvailabilityOption(date=day, window=window)],
        delivery_date=day,
        planning_status=PlanningStatus.CONFIRMED,
        status="approved",
        duration_minutes=DEFAULT_DURATION_MINUTES_BY_JOB_TYPE[job_type],
        raw_message="[seeded: confirmed booking]",
        notes=PRODUCT_LABELS[job_type],
    )


def _awaiting(name, postal_code, job_type: JobType, product: str) -> JobRecord:
    """A customer who has not said when yet. No date, no windows, nothing assumed.

    Deliberately empty availability: the demo begins when they type a time in their own words, and
    seeding one would answer the question the conversation exists to ask.
    """
    return JobRecord(
        customer_name=name,
        phone=_phone(name),
        address=_address(postal_code),
        job_type=job_type,
        planning_status=PlanningStatus.PENDING_AVAILABILITY,
        duration_minutes=DEFAULT_DURATION_MINUTES_BY_JOB_TYPE[job_type],
        raw_message="[seeded: awaiting availability]",
        notes=product,
    )


def publish_baseline_routes(repo: JobsRepository) -> list:
    """Publish v1 for both days, so a booking during the demo produces a visible v2.

    Both days must publish. A cycle needs its Friday and its Saturday to be routable -- if either
    fails, the coordination cycle does not exist and the agent escalates every customer.
    """
    published = []
    for day in PlanningClock.horizon_dates():
        if not plan_service.routable_jobs(repo, day):
            raise RuntimeError(f"{day} has no routable work; the demo needs both routes published")
        sequence = plan_service.solve_day(repo, day)
        plan_service.publish_plan_version(sequence, reason="Initial route for the day")
        published.append(day)
    return published


def seed() -> str:
    init_db()
    seed_cache(KNOWN_LOCATIONS)  # real coordinates, no network needed
    _clear_existing()
    repo = JobsRepository()

    friday, saturday = PlanningClock.horizon_dates()
    jobs: list[JobRecord] = []
    for day, route in ((friday, FRIDAY_ROUTE), (saturday, SATURDAY_ROUTE)):
        for name, postal_code, slot, job_type in route:
            jobs.append(_confirmed(name, postal_code, day, slot, job_type))

    for name, postal_code, job_type, product in (BEST_CASE, DIFFICULT):
        jobs.append(_awaiting(name, postal_code, job_type, product))

    for job in jobs:
        repo.save_job(job)

    published = publish_baseline_routes(repo)

    return (
        f"Seeded the demo relative to {PlanningClock.today()}:\n"
        f"  {friday:%a %d %b} -- {len(FRIDAY_ROUTE)} stops, driver {DRIVERS['friday']['name']}\n"
        f"  {saturday:%a %d %b} -- {len(SATURDAY_ROUTE)} stops, driver {DRIVERS['saturday']['name']}\n"
        f"  {BEST_CASE[0]} (East, so Friday) is waiting to say when\n"
        f"  {DIFFICULT[0]} (Central, so Saturday) is the customer who will decline\n"
        f"  published: {', '.join(str(d) for d in published)}\n"
    )


def main() -> str:
    summary = seed()
    print(summary)
    return summary


if __name__ == "__main__":
    main()
