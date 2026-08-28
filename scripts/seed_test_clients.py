"""Seed the database with 20 synthetic furniture-delivery clients across 5 weekdays, for
demoing/testing the dashboard and route planner without 20 real WhatsApp messages or LLM calls.

    python scripts/seed_test_clients.py

⚠️  Wipes ALL existing jobs, day sequences and notifications first -- not just previously-seeded
ones. Any booking made through the live chat or admin dashboard (including your own manual
testing) is deleted too. This gives a clean, known test dataset; it is not a safe thing to run
against a database anyone is actively using.

The first date gets 7 clients (all sofa -- the shortest job type) spread across genuinely
different parts of Singapore (Tampines, Bedok, Katong, Geylang, Macpherson, Toa Payoh, Hougang)
instead of clustering next to the depot -- verified against the real solver, since 10 stops
at that spread was NOT feasible: 10 x 45 min already uses 450 of the 540 minutes in a
09:00-18:00 day, leaving only ~90 minutes of total travel, and real drive times between
distinct neighbourhoods (11-30+ min each) blow through that budget in a handful of legs. 7
stops x 45 min leaves 225 minutes for travel, comfortably covering this spread. The remaining
13 clients are spread even more thinly (and diversely, including the far west and north) across
the other 4 dates, since 2-4 stops/day has no such constraint.
"""
from __future__ import annotations

import sqlite3
from datetime import date as Date, time as Time

from dispatch_agent.config import settings
from dispatch_agent.db import JobsRepository, init_db
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.models import DEFAULT_DURATION_MINUTES_BY_JOB_TYPE, Address, JobRecord, JobType, TimeWindow

DELIVERY_DATES = [
    Date(2026, 8, 28),  # Fri -- the busy day, 10 clients
    Date(2026, 8, 31),  # Mon
    Date(2026, 9, 1),  # Tue
    Date(2026, 9, 2),  # Wed
    Date(2026, 9, 3),  # Thu
]

# (name, phone, postal_code, address) -- 7 distinct districts spanning east through
# south-central Singapore. Verified feasible against the live solver at this spread; going
# wider (adding e.g. Punggol, Ang Mo Kio, or Toa Payoh + Jurong together) was not, even with a
# 30-second search budget -- see the module docstring for the time-budget math.
BUSY_DAY_CLIENTS = [
    ("Alicia Tan", "91234501", "511234", "Block 511 Tampines Central 1"),
    ("Benjamin Ong", "91234502", "461234", "Block 461 Bedok North Ave 3"),
    ("Cheryl Lim", "91234503", "421234", "Block 421 Joo Chiat Rd"),
    ("Daniel Wong", "91234504", "381234", "Block 381 Geylang Rd"),
    ("Evelyn Goh", "91234505", "341234", "Block 341 Macpherson Rd"),
    ("Farid Rahman", "91234506", "311234", "Block 311 Toa Payoh Lorong 1"),
    ("Grace Koh", "91234507", "531234", "Block 531 Hougang Ave 8"),
]

# (name, phone, postal_code, address) -- spread more thinly (4/3/3/3) across the other 4 dates,
# reaching further across the island (including the far west and north) since 3-4 stops/day has
# no same-day time-budget constraint the way the busy day does.
OTHER_CLIENTS = [
    ("Kavya Nair", "91234511", "389456", "Block 389 Geylang Rd"),
    ("Leon Teo", "91234512", "429876", "Block 429 Katong Park Rd"),
    ("Michelle Yeo", "91234513", "499001", "Block 499 Loyang Ave"),
    ("Nurul Aisyah", "91234514", "519234", "Block 519 Tampines St 11"),
    ("Owen Chan", "91234515", "608456", "Block 608 Jurong West St 65"),
    ("Priscilla Foo", "91234516", "699123", "Block 699 Lim Chu Kang Rd"),
    ("Ryan Kwek", "91234517", "779456", "Block 779 Upper Thomson Rd"),
    ("Sarah Lau", "91234518", "109045", "Block 109 Telok Blangah Rd"),
    ("Timothy Sim", "91234519", "209567", "Block 209 Farrer Park Rd"),
    ("Valerie Ang", "91234520", "309789", "Block 309 Novena Rd"),
    ("Hassan Ibrahim", "91234508", "760789", "Block 760 Yishun Ave 5"),
    ("Irene Chua", "91234509", "739456", "Block 739 Woodlands Dr 14"),
    ("Jason Ng", "91234510", "129876", "Block 129 Clementi Ave 3"),
]
OTHER_CLIENTS_PER_DATE = [4, 3, 3, 3]  # against DELIVERY_DATES[1:], sums to len(OTHER_CLIENTS)

JOB_TYPE_CYCLE = [JobType.SOFA, JobType.BED, JobType.CABINET, JobType.OTHER]
WINDOW_CYCLE = [
    (Time(9, 0), Time(12, 0)),
    (Time(10, 0), Time(14, 0)),
    (Time(13, 0), Time(18, 0)),
    (Time(9, 0), Time(18, 0)),
]


def _clear_existing() -> None:
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM day_sequences")
        conn.execute("DELETE FROM notifications")  # orphaned otherwise -- they'd reference jobs that no longer exist
        conn.commit()


def _make_job(name, phone, postal_code, address, job_type, start, end, delivery_date) -> JobRecord:
    return JobRecord(
        customer_name=name,
        phone=phone,
        address=Address(raw_text=address, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)),
        job_type=job_type,
        availability=[TimeWindow(start=start, end=end)],
        duration_minutes=DEFAULT_DURATION_MINUTES_BY_JOB_TYPE[job_type],
        delivery_date=delivery_date,
        raw_message="[seeded test data]",
    )


def main() -> None:
    init_db()
    _clear_existing()
    repo = JobsRepository()

    busy_date = DELIVERY_DATES[0]
    for name, phone, postal_code, address in BUSY_DAY_CLIENTS:
        # All sofa (shortest duration) with the widest availability window, so the solver has
        # maximum room to fit all 10 into one work day.
        job = _make_job(name, phone, postal_code, address, JobType.SOFA, Time(9, 0), Time(18, 0), busy_date)
        repo.save_job(job)

    other_dates = DELIVERY_DATES[1:]
    i = 0
    for date_index, count in enumerate(OTHER_CLIENTS_PER_DATE):
        for _ in range(count):
            name, phone, postal_code, address = OTHER_CLIENTS[i]
            job_type = JOB_TYPE_CYCLE[i % len(JOB_TYPE_CYCLE)]
            start, end = WINDOW_CYCLE[i % len(WINDOW_CYCLE)]
            job = _make_job(name, phone, postal_code, address, job_type, start, end, other_dates[date_index])
            repo.save_job(job)
            i += 1

    total = len(BUSY_DAY_CLIENTS) + len(OTHER_CLIENTS)
    print(f"Seeded {total} test clients: {len(BUSY_DAY_CLIENTS)} on {busy_date.isoformat()}, "
          f"the rest spread across {len(other_dates)} other dates.")


if __name__ == "__main__":
    main()
