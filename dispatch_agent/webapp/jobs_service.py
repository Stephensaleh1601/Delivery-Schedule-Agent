"""Shared job-creation/-editing logic used by the plain REST API (main.py), the admin
dashboard's edit form (also main.py), and the WhatsApp-style chat booking flow (chat.py) -- one
validation path, so none of these front doors can drift apart.
"""
from __future__ import annotations

from datetime import date as Date, time as Time

from pydantic import BaseModel, Field

from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.geo.sanity import OutsideServiceAreaError, validate_delivery_location
from dispatch_agent.models import (
    DEFAULT_DURATION_MINUTES_BY_JOB_TYPE,
    Address,
    JobRecord,
    JobType,
    TimeWindow,
)


class JobSubmission(BaseModel):
    customer_name: str
    phone: str | None = None
    address_raw: str
    postal_code: str
    job_type: JobType = JobType.SOFA
    delivery_date: Date
    window_start: Time
    window_end: Time
    duration_minutes: int | None = Field(default=None, gt=0)
    notes: str | None = None


class JobSubmissionError(ValueError):
    """Raised for any business-rule validation failure -- callers turn this into an HTTP 400 or
    a chat reply asking the customer to try again."""


def _validated_fields(payload: JobSubmission) -> dict:
    if payload.window_end <= payload.window_start:
        raise JobSubmissionError("Preferred window end must be after start.")
    try:
        coordinates = postal_code_to_coords(payload.postal_code)
    except ValueError as exc:
        raise JobSubmissionError(str(exc)) from exc
    # Reject an out-of-area address here rather than letting it reach the router, which would
    # happily return a cross-border drive time for a job nobody can service. Every front door
    # (REST, chat booking, admin edit) goes through this function, so one check covers them all.
    try:
        validate_delivery_location(coordinates, label=f"Postal code {payload.postal_code}")
    except OutsideServiceAreaError as exc:
        raise JobSubmissionError(str(exc)) from exc

    return {
        "customer_name": payload.customer_name,
        "phone": payload.phone,
        "address": Address(raw_text=payload.address_raw, postal_code=payload.postal_code, coordinates=coordinates),
        "job_type": payload.job_type,
        "availability": [TimeWindow(start=payload.window_start, end=payload.window_end)],
        "duration_minutes": payload.duration_minutes or DEFAULT_DURATION_MINUTES_BY_JOB_TYPE[payload.job_type],
        "delivery_date": payload.delivery_date,
        "notes": payload.notes,
    }


def create_job_from_submission(payload: JobSubmission, raw_message: str) -> JobRecord:
    job = JobRecord(raw_message=raw_message, **_validated_fields(payload))
    JobsRepository().save_job(job)
    return job


def update_job_from_submission(job_id: str, payload: JobSubmission) -> JobRecord:
    repo = JobsRepository()
    existing = repo.get_job(job_id)
    if existing is None:
        raise JobSubmissionError(f"unknown job_id {job_id!r}")
    updated = existing.model_copy(update=_validated_fields(payload))
    repo.save_job(updated)
    return updated
