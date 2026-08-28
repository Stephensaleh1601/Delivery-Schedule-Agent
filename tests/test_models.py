from datetime import date, time

import pytest
from pydantic import ValidationError

from dispatch_agent.models import Address, JobRecord, JobType, TimeWindow


def test_time_window_rejects_end_before_start():
    with pytest.raises(ValidationError):
        TimeWindow(start=time(14, 0), end=time(13, 0))


def test_time_window_overlap():
    a = TimeWindow(start=time(9, 0), end=time(12, 0))
    b = TimeWindow(start=time(11, 0), end=time(15, 0))
    c = TimeWindow(start=time(12, 0), end=time(15, 0))
    assert a.overlaps(b)
    assert not a.overlaps(c)


def test_address_rejects_bad_postal_code():
    with pytest.raises(ValidationError):
        Address(raw_text="123 Somewhere", postal_code="12A456")


def test_job_record_defaults():
    job = JobRecord(
        customer_name="Mrs Tan",
        address=Address(raw_text="1 Marina Blvd", postal_code="018956"),
        job_type=JobType.SOFA,
        availability=[TimeWindow(start=time(9, 0), end=time(12, 0))],
        delivery_date=date(2026, 8, 28),
        raw_message="hi",
    )
    assert job.duration_minutes == 60
    assert job.id
