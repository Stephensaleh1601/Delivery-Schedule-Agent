from datetime import date, time

import pytest

from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.geo.zones import SINGAPORE_CENTROID
from dispatch_agent.models import Address, JobRecord, JobType, TimeWindow
from dispatch_agent.solver import UnsolvableDayError, sequence_day


def _job(name, postal_code, start, end, duration_minutes=30):
    return JobRecord(
        customer_name=name,
        address=Address(raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)),
        job_type=JobType.DELIVERY,
        availability=[TimeWindow(start=time(*start), end=time(*end))],
        delivery_date=date(2026, 8, 28),
        raw_message="test",
        duration_minutes=duration_minutes,
    )


def test_sequences_all_jobs_within_availability():
    jobs = [
        _job("A", "018956", (9, 0), (12, 0)),
        _job("B", "119613", (10, 0), (14, 0)),
        _job("C", "238874", (13, 0), (18, 0)),
    ]
    sequence = sequence_day(jobs, date(2026, 8, 28), depot=SINGAPORE_CENTROID)
    assert len(sequence.stops) == len(jobs)
    stops_by_job = {s.job_id: s for s in sequence.stops}
    for job in jobs:
        stop = stops_by_job[job.id]
        window = job.availability[0]
        assert window.start <= stop.arrival_window.start <= window.end


def test_raises_when_no_feasible_order():
    jobs = [
        _job("A", "018956", (9, 0), (9, 15), duration_minutes=60),
        _job("B", "119613", (9, 0), (9, 15), duration_minutes=60),
    ]
    with pytest.raises(UnsolvableDayError):
        sequence_day(jobs, date(2026, 8, 28), depot=SINGAPORE_CENTROID)


def test_empty_day_returns_empty_sequence():
    sequence = sequence_day([], date(2026, 8, 28), depot=SINGAPORE_CENTROID)
    assert sequence.stops == []
    assert sequence.total_drive_minutes == 0
