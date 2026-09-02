"""Pydantic schemas shared by the agents, the solver, the DB layer and the dashboard.

This is the one file everything else agrees on -- if you change a field here, grep for it
before touching anything downstream.
"""
from __future__ import annotations

import uuid
from datetime import date as Date, datetime, time as Time, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobType(str, Enum):
    SOFA = "sofa"
    BED = "bed"
    CABINET = "cabinet"
    OTHER = "other"


# Default job duration by furniture type, used when a customer/coordinator doesn't specify one --
# heavier assembly work (cabinets) gets more time than a straightforward sofa drop-off.
DEFAULT_DURATION_MINUTES_BY_JOB_TYPE: dict[JobType, int] = {
    JobType.SOFA: 45,
    JobType.BED: 75,
    JobType.CABINET: 105,
    JobType.OTHER: 60,
}


class JobStatus(str, Enum):
    NEW = "new"
    SEQUENCED = "sequenced"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESCHEDULE_REQUESTED = "reschedule_requested"


class Coordinates(BaseModel):
    lat: float
    lng: float


class Address(BaseModel):
    raw_text: str
    postal_code: Optional[str] = None
    coordinates: Optional[Coordinates] = None

    @field_validator("postal_code")
    @classmethod
    def _six_digits(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and (not v.isdigit() or len(v) != 6):
            raise ValueError("Singapore postal codes are 6 digits")
        return v


class TimeWindow(BaseModel):
    start: Time
    end: Time

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: Time, info) -> Time:
        start = info.data.get("start")
        if start is not None and v <= start:
            raise ValueError("end must be after start")
        return v

    def overlaps(self, other: "TimeWindow") -> bool:
        return self.start < other.end and other.start < self.end


class JobRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    customer_name: str
    phone: Optional[str] = None
    address: Address
    job_type: JobType
    availability: list[TimeWindow] = Field(min_length=1)
    # Set once a customer accepts an offered slot. A locked window is a promise: the solver
    # treats it as the job's SOLE window (see solver._effective_windows), so a replan can never
    # quietly slide a confirmed appointment onto one of the customer's other original options.
    locked_window: Optional[TimeWindow] = None
    duration_minutes: int = Field(gt=0, default=60)
    delivery_date: Date
    status: JobStatus = JobStatus.NEW
    raw_message: str
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class StopAssignment(BaseModel):
    job_id: str
    sequence_index: int
    arrival_window: TimeWindow
    drive_minutes_from_prev: int = 0


class DaySequence(BaseModel):
    delivery_date: Date
    stops: list[StopAssignment]
    # Sum of drive_minutes_from_prev across stops: depot -> stop1 -> ... -> lastStop. This
    # deliberately EXCLUDES the drive home, because several callers already display and diff
    # this exact number. Use round_trip_drive_minutes for anything that compares the real cost
    # of a day -- without the return leg, appending a far-flung final stop looks free.
    total_drive_minutes: int
    return_drive_minutes: int = 0
    status: JobStatus = JobStatus.SEQUENCED
    generated_at: datetime = Field(default_factory=_utcnow)

    @property
    def round_trip_drive_minutes(self) -> int:
        return self.total_drive_minutes + self.return_drive_minutes


class OverrideLogEntry(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    delivery_date: Date
    job_id: str
    field_changed: str
    agent_value: str
    coordinator_value: str
    reason: Optional[str] = None
    timestamp: datetime = Field(default_factory=_utcnow)


class Notification(BaseModel):
    """A heads-up for the back office -- e.g. a customer rescheduled via the chat after a route
    was already generated for the affected date(s), so that plan is now stale."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    message: str
    dates: list[Date] = Field(default_factory=list)
    read: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


class RescheduleRequest(BaseModel):
    job_id: str
    new_date: Optional[Date] = None
    new_availability: Optional[list[TimeWindow]] = None
    raw_message: str


class ReschedulePlan(BaseModel):
    delivery_date: Date
    previous_sequence: DaySequence
    proposed_sequence: DaySequence
    affected_job_ids: list[str]
