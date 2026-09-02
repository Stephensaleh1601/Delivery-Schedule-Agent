"""Shared job-creation/-editing logic used by the plain REST API (main.py), the admin
dashboard's edit form (also main.py), and the WhatsApp-style chat booking flow (chat.py) -- one
validation path, so none of these front doors can drift apart.
"""
from __future__ import annotations

from datetime import date as Date, time as Time

from pydantic import BaseModel, Field

from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.geocoder import geocode_postal_code
from dispatch_agent.geo.sanity import OutsideServiceAreaError, validate_delivery_location
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


class AvailabilityChoice(BaseModel):
    """One date+window a customer says they could accept."""

    date: Date
    window_start: Time
    window_end: Time
    preference_rank: int = 1


class OrderSubmission(BaseModel):
    """A booking under the multi-day flow: no date is chosen, only acceptable windows.

    Two options are asked for on the form, but only one is required here. A customer messaging
    in free text may genuinely only give one, and refusing to record that would lose the order
    -- they are told their flexibility is limited instead.
    """

    customer_name: str
    phone: str | None = None
    address_raw: str
    postal_code: str
    job_type: JobType = JobType.SOFA
    availability: list[AvailabilityChoice] = Field(min_length=1)
    duration_minutes: int | None = Field(default=None, gt=0)
    can_deliver_early: bool = False
    notes: str | None = None


MIN_OPTIONS_ON_FORM = 2  # what the booking form asks for
MIN_OPTIONS_ACCEPTED = 1  # what the system will still record


class JobSubmissionError(ValueError):
    """Raised for any business-rule validation failure -- callers turn this into an HTTP 400 or
    a chat reply asking the customer to try again."""


def _validated_fields(payload: JobSubmission) -> dict:
    if payload.window_end <= payload.window_start:
        raise JobSubmissionError("Preferred window end must be after start.")
    try:
        located = geocode_postal_code(payload.postal_code)
    except ValueError as exc:
        raise JobSubmissionError(str(exc)) from exc
    coordinates = located.coordinates
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
        "address": Address(
            raw_text=payload.address_raw,
            postal_code=payload.postal_code,
            coordinates=coordinates,
            geocode_source=located.source,
            formatted_address=located.formatted_address,
        ),
        "job_type": payload.job_type,
        "availability": [TimeWindow(start=payload.window_start, end=payload.window_end)],
        "duration_minutes": payload.duration_minutes or DEFAULT_DURATION_MINUTES_BY_JOB_TYPE[payload.job_type],
        "delivery_date": payload.delivery_date,
        "notes": payload.notes,
    }


def create_job_from_submission(payload: JobSubmission, raw_message: str) -> JobRecord:
    """The legacy single-slot path: the customer named one date and window, and we take it.

    Kept working so the existing chat and admin UI keep functioning while the multi-day flow is
    built alongside. The chosen slot is recorded as an availability option too, so such an order
    is still legible to the planner.
    """
    fields = _validated_fields(payload)
    option = AvailabilityOption(
        date=fields["delivery_date"], window=fields["availability"][0], preference_rank=1
    )
    job = JobRecord(raw_message=raw_message, availability_options=[option], **fields)
    JobsRepository().save_job(job)
    return job


def create_order(payload: OrderSubmission, raw_message: str) -> JobRecord:
    """The multi-day path: an order with acceptable windows but no agreed date.

    Deliberately leaves delivery_date and locked_window unset. The order exists -- it has to,
    before feasibility can be evaluated -- but nothing has been promised, and the model's
    validator enforces that a pending order carries no lock.
    """
    if not payload.availability:
        raise JobSubmissionError("Please give us at least one date and time that works for you.")

    try:
        located = geocode_postal_code(payload.postal_code)
        validate_delivery_location(located.coordinates, label=f"Postal code {payload.postal_code}")
    except (ValueError, OutsideServiceAreaError) as exc:
        raise JobSubmissionError(str(exc)) from exc

    options = []
    for choice in payload.availability:
        if choice.window_end <= choice.window_start:
            raise JobSubmissionError("Each window's end time must be after its start time.")
        if not PlanningClock.is_within_horizon(choice.date):
            first, last = PlanningClock.horizon()
            raise JobSubmissionError(
                f"We can only take bookings between {first} and {last}. "
                f"{choice.date} is outside that."
            )
        options.append(
            AvailabilityOption(
                date=choice.date,
                window=TimeWindow(start=choice.window_start, end=choice.window_end),
                preference_rank=choice.preference_rank,
            )
        )

    job = JobRecord(
        customer_name=payload.customer_name,
        phone=payload.phone,
        address=Address(
            raw_text=payload.address_raw,
            postal_code=payload.postal_code,
            coordinates=located.coordinates,
            geocode_source=located.source,
            formatted_address=located.formatted_address,
        ),
        job_type=payload.job_type,
        availability_options=options,
        duration_minutes=payload.duration_minutes
        or DEFAULT_DURATION_MINUTES_BY_JOB_TYPE[payload.job_type],
        can_deliver_early=payload.can_deliver_early,
        planning_status=PlanningStatus.PENDING_PLANNING,
        raw_message=raw_message,
        notes=payload.notes,
    )
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
