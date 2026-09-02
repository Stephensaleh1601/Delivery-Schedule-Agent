"""Pydantic schemas shared by the agents, the solver, the DB layer and the dashboard.

This is the one file everything else agrees on -- if you change a field here, grep for it
before touching anything downstream.
"""
from __future__ import annotations

import uuid
from datetime import date as Date, datetime, time as Time, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


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
    """DEPRECATED -- kept only so existing callers and stored rows keep working.

    `PlanningStatus` below is the authoritative lifecycle. Never assign to `JobRecord.status`
    directly: call `JobRecord.set_planning_status()`, which keeps this mirror in step. A model
    validator rejects any record where the two disagree, so the pair cannot silently drift.

    Delete this enum once nothing reads `job.status` (currently: reschedule.py, chat.py, the
    /api/jobs payload, and the seed script).
    """

    NEW = "new"
    SEQUENCED = "sequenced"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESCHEDULE_REQUESTED = "reschedule_requested"


class PlanningStatus(str, Enum):
    """Where an order is in the promise lifecycle. This is the authoritative state.

    The normal path is PENDING_PLANNING -> OFFERED -> CONFIRMED -> SEQUENCED -> DISPATCHED ->
    COMPLETED. PENDING_AVAILABILITY is for an order that arrived without usable windows;
    EXCEPTION is for one a coordinator must look at.
    """

    PENDING_AVAILABILITY = "pending_availability"
    PENDING_PLANNING = "pending_planning"
    OFFERED = "offered"
    CONFIRMED = "confirmed"
    SEQUENCED = "sequenced"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXCEPTION = "exception"


# Planning statuses at which a customer has been promised a specific window. The solver treats
# these jobs' locked_window as their only option, and a replan may never move them.
LOCKED_PLANNING_STATUSES = frozenset(
    {
        PlanningStatus.CONFIRMED,
        PlanningStatus.SEQUENCED,
        PlanningStatus.DISPATCHED,
        PlanningStatus.COMPLETED,
    }
)

# How the authoritative lifecycle projects onto the legacy enum. Several planning states share a
# legacy value because the old enum simply had no way to express them.
PLANNING_TO_LEGACY_STATUS: dict[PlanningStatus, JobStatus] = {
    PlanningStatus.PENDING_AVAILABILITY: JobStatus.NEW,
    PlanningStatus.PENDING_PLANNING: JobStatus.NEW,
    PlanningStatus.OFFERED: JobStatus.NEW,
    PlanningStatus.CONFIRMED: JobStatus.APPROVED,
    PlanningStatus.SEQUENCED: JobStatus.SEQUENCED,
    PlanningStatus.DISPATCHED: JobStatus.SEQUENCED,
    PlanningStatus.COMPLETED: JobStatus.SEQUENCED,
    PlanningStatus.CANCELLED: JobStatus.REJECTED,
    PlanningStatus.EXCEPTION: JobStatus.NEW,
}

# Used only by the migration, to give pre-existing rows a sensible planning status.
LEGACY_TO_PLANNING_STATUS: dict[JobStatus, PlanningStatus] = {
    JobStatus.NEW: PlanningStatus.PENDING_PLANNING,
    JobStatus.SEQUENCED: PlanningStatus.SEQUENCED,
    JobStatus.APPROVED: PlanningStatus.CONFIRMED,
    JobStatus.REJECTED: PlanningStatus.CANCELLED,
    JobStatus.RESCHEDULE_REQUESTED: PlanningStatus.PENDING_PLANNING,
}


class ReadinessStatus(str, Enum):
    """Whether the goods are actually available to deliver -- the mock ERP signal."""

    READY = "ready"
    DELAYED = "delayed"
    CANCELLED = "cancelled"


class OfferStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CLOSED = "closed"
    EXPIRED = "expired"


class PlanStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class PlanningEventType(str, Enum):
    NEW_ORDER = "new_order"
    CUSTOMER_ACCEPTED_OFFER = "customer_accepted_offer"
    CUSTOMER_REJECTED_OFFER = "customer_rejected_offer"
    CUSTOMER_CANCELLED = "customer_cancelled"
    MORNING_RUN = "morning_run"
    ORDER_READINESS_CHANGED = "order_readiness_changed"
    MANUAL_RETRY = "manual_retry"


class AgentRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STEP_LIMIT_REACHED = "step_limit_reached"


class MessageDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


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


class AvailabilityOption(BaseModel):
    """One date+window a customer says they could accept. The agent picks between these; it
    never invents a slot outside them."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    date: Date
    window: TimeWindow
    preference_rank: int = 1


class JobRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    customer_name: str
    phone: Optional[str] = None
    address: Address
    job_type: JobType
    # The window(s) the solver may use for a job that already has a date. Kept for compatibility
    # and for legacy single-slot bookings; an order still being planned carries
    # availability_options instead, so this can legitimately be empty.
    availability: list[TimeWindow] = Field(default_factory=list)
    # The 2-3 date+window choices a customer offered at booking, before any date was agreed.
    availability_options: list[AvailabilityOption] = Field(default_factory=list)
    # Set once a customer accepts an offered slot. A locked window is a promise: the solver
    # treats it as the job's SOLE window (see solver._effective_windows), so a replan can never
    # quietly slide a confirmed appointment onto one of the customer's other original options.
    locked_window: Optional[TimeWindow] = None
    duration_minutes: int = Field(gt=0, default=60)
    # None until a date is agreed -- this is the unscheduled pool the planner draws from.
    delivery_date: Optional[Date] = None
    planning_status: PlanningStatus = PlanningStatus.PENDING_PLANNING
    readiness_status: ReadinessStatus = ReadinessStatus.READY
    # Whether this customer said they'd take an earlier slot if one frees up. The recovery flow
    # only approaches customers who opted in.
    can_deliver_early: bool = False
    priority: int = 0
    status: JobStatus = JobStatus.NEW  # legacy mirror of planning_status -- see JobStatus
    raw_message: str
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def set_planning_status(self, new_status: PlanningStatus) -> "JobRecord":
        """The only supported way to change lifecycle state. Assigning to `planning_status` or
        `status` directly will trip the consistency validator on the next load."""
        self.planning_status = new_status
        self.status = PLANNING_TO_LEGACY_STATUS[new_status]
        self.updated_at = _utcnow()
        return self

    @property
    def is_locked(self) -> bool:
        return self.locked_window is not None and self.planning_status in LOCKED_PLANNING_STATUSES

    @model_validator(mode="after")
    def _states_cannot_contradict(self) -> "JobRecord":
        expected = PLANNING_TO_LEGACY_STATUS[self.planning_status]
        if self.status != expected:
            raise ValueError(
                f"status {self.status.value!r} contradicts planning_status "
                f"{self.planning_status.value!r} (expected {expected.value!r}) -- "
                f"use set_planning_status() rather than assigning either field"
            )
        if self.planning_status in LOCKED_PLANNING_STATUSES:
            if self.delivery_date is None or self.locked_window is None:
                raise ValueError(
                    f"a {self.planning_status.value} job must have both a delivery_date and a "
                    f"locked_window -- that pair IS the promise"
                )
        elif self.planning_status in (
            PlanningStatus.PENDING_AVAILABILITY,
            PlanningStatus.PENDING_PLANNING,
            PlanningStatus.OFFERED,
        ):
            if self.locked_window is not None:
                raise ValueError(
                    f"a {self.planning_status.value} job cannot carry a locked_window -- nothing "
                    f"has been promised yet"
                )
        # A dated job must give the solver something to work with. This replaces availability's
        # old min_length=1, which an undated order in the planning pool cannot satisfy.
        if self.delivery_date is not None and not (self.availability or self.locked_window):
            raise ValueError("a job with a delivery_date needs at least one availability window")
        return self


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


# -- Multi-day planning -------------------------------------------------------


class CandidateSlotEvaluation(BaseModel):
    """What one of a customer's proposed windows would actually cost the operation.

    Every number here comes from a real solve against real drive times. Infeasibility is a
    separate state, never a large score: `total_score` stays 0 and carries no sequence when
    `feasible` is false, and the validator below enforces that, so an impossible slot can never
    be ranked as merely expensive.
    """

    availability_option_id: str
    date: Date
    window: TimeWindow
    feasible: bool
    infeasible_reason: Optional[str] = None
    baseline_drive_minutes: int = 0
    proposed_drive_minutes: int = 0
    incremental_drive_minutes: int = 0
    day_opening_penalty_minutes: int = 0
    preference_penalty_minutes: int = 0
    overtime_penalty_minutes: int = 0
    total_score: int = 0
    proposed_sequence: Optional[DaySequence] = None

    @model_validator(mode="after")
    def _infeasible_carries_no_score(self) -> "CandidateSlotEvaluation":
        if not self.feasible:
            if not self.infeasible_reason:
                raise ValueError("an infeasible evaluation must say why")
            if self.total_score != 0 or self.proposed_sequence is not None:
                raise ValueError(
                    "an infeasible evaluation must not carry a score or a proposed sequence -- "
                    "rank on the `feasible` flag instead of encoding it as a large number"
                )
        return self


class OfferedSlot(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    availability_option_id: str
    date: Date
    window: TimeWindow
    score: int = 0


class AppointmentOffer(BaseModel):
    """One round of "here are the times we can do" put to a customer.

    `round_number` is what enforces the two-round cap: the limit is checked when an offer is
    created, not asked of the model in a prompt, so a model that ignores the instruction cannot
    quietly break the guardrail.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    order_id: str
    options: list[OfferedSlot] = Field(default_factory=list)
    round_number: int = 1
    status: OfferStatus = OfferStatus.PENDING
    accepted_slot_id: Optional[str] = None
    # The plan version produced when this offer was accepted. Replaying the acceptance returns
    # this instead of re-solving, which is what makes the endpoint idempotent.
    resulting_plan_id: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    responded_at: Optional[datetime] = None


class RoutePlanVersion(BaseModel):
    """An immutable snapshot of one day's route. Plans are never edited in place -- a change
    creates a new version and supersedes the old one, so "what did we promise, and when did it
    change?" stays answerable."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    delivery_date: Date
    version: int = 1
    status: PlanStatus = PlanStatus.ACTIVE
    sequence: DaySequence
    reason_created: str = ""
    parent_plan_id: Optional[str] = None
    # Fingerprint of the stop ordering and arrival windows. A replan that changes nothing
    # returns the existing version rather than inflating the counter.
    content_hash: str = ""
    generated_at: datetime = Field(default_factory=_utcnow)


class PlanningEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    event_type: PlanningEventType
    order_id: Optional[str] = None
    affected_date: Optional[Date] = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)


class AgentActionLog(BaseModel):
    """One tool call the agent made. `reason_summary` is a short operational sentence for the
    coordinator -- never model reasoning."""

    step: int
    tool: str
    ok: bool
    # What the tool was called with and what it returned, both sanitised on the way in --
    # see planning/tools.sanitise_for_log.
    arguments: dict = Field(default_factory=dict)
    summary: str = ""
    reason_summary: str = ""
    error: Optional[str] = None
    data: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=_utcnow)


class AgentRunLog(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    event_id: str
    event_type: Optional[PlanningEventType] = None
    order_id: Optional[str] = None
    actions: list[AgentActionLog] = Field(default_factory=list)
    status: AgentRunStatus = AgentRunStatus.RUNNING
    final_summary: str = ""
    token_usage: Optional[dict] = None
    started_at: datetime = Field(default_factory=_utcnow)
    completed_at: Optional[datetime] = None


class CustomerMessage(BaseModel):
    """A message to or from a customer. `direction` is what makes "how many customers did we
    contact?" answerable from the data rather than guessed."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    order_id: Optional[str] = None
    direction: MessageDirection = MessageDirection.OUTBOUND
    channel: str = "chat"
    body: str = ""
    created_at: datetime = Field(default_factory=_utcnow)


class CoordinatorException(BaseModel):
    """Something the agent could not resolve safely on its own. Raised rather than guessed --
    notably when honouring every confirmed appointment is impossible."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    order_id: Optional[str] = None
    delivery_date: Optional[Date] = None
    kind: str = "unresolved"
    message: str = ""
    resolved: bool = False
    created_at: datetime = Field(default_factory=_utcnow)
