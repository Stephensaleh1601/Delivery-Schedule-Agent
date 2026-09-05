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
    """What is being delivered.

    The pet-food values are the live ones. The three furniture values below them are kept only so
    that rows written before the business changed still validate when they are read back -- this
    is a string enum persisted straight into SQLite, so removing a member turns old rows into
    validation errors rather than migrating them. Nothing customer-facing produces them any more.
    """

    PET_FOOD_BOX = "pet_food_box"
    ONE_OFF_PET_ORDER = "one_off_pet_order"
    OTHER = "other"

    # Legacy. Readable, never offered.
    SOFA = "sofa"
    BED = "bed"
    CABINET = "cabinet"


# The live job types a customer can be booked for. Excludes the legacy furniture values, so an
# intake schema or a UI dropdown built from this cannot offer one by accident.
BOOKABLE_JOB_TYPES: tuple[JobType, ...] = (
    JobType.PET_FOOD_BOX,
    JobType.ONE_OFF_PET_ORDER,
    JobType.OTHER,
)


# Default duration when nobody specifies one. Fresh pet food is a doorstep handover -- the driver
# is on a motorcycle and the customer is expecting them -- so these are minutes, not the hours a
# furniture delivery with assembly used to take.
DEFAULT_DURATION_MINUTES_BY_JOB_TYPE: dict[JobType, int] = {
    JobType.PET_FOOD_BOX: 10,
    JobType.ONE_OFF_PET_ORDER: 15,
    JobType.OTHER: 15,
    JobType.SOFA: 45,
    JobType.BED: 75,
    JobType.CABINET: 105,
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


class OfferPurpose(str, Enum):
    """Why an offer was made. Booking rounds and recovery approaches are capped separately -- a
    customer being asked whether they'd come forward has not used up a round of negotiating their
    own delivery date."""

    BOOKING = "booking"
    # The three-choice fallback, after a normal offer was declined. Separate from BOOKING because
    # the two carry different policies -- one proven option versus exactly three, escalate below
    # that -- and blending them would mean every offer had to satisfy both.
    ALTERNATIVE = "alternative"
    RECOVERY = "recovery"


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
    # Where `coordinates` came from: "onemap" and "google" are real building locations,
    # "district_centroid" is the ~1-2km fallback. Stored so the UI can mark an approximate pin
    # rather than implying a precision it does not have. Defaulted, so pre-existing rows -- which
    # were all centroids -- describe themselves correctly without a migration.
    geocode_source: str = "district_centroid"
    formatted_address: Optional[str] = None

    @property
    def precisely_located(self) -> bool:
        return self.geocode_source in ("onemap", "google")

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
    # Intervals inside `window` the customer has since turned down. A rejection applies to the time
    # we proposed, not to the whole day -- "not 10 till 12" is not "not Friday" -- so the option
    # survives with a hole in it and the day is re-solved around it. Stored rather than derived so
    # it outlives the round trip and is visible in the order's record.
    excluded_windows: list[TimeWindow] = Field(default_factory=list)
    preference_rank: int = 1

    def bookable_windows(self, min_width: int) -> list["TimeWindow"]:
        """What is left of this option once the rejections are carved out.

        Empty means the customer has ruled out every part of the day that could hold the job --
        which is a real answer, and the caller should move to another date rather than re-offering.
        """
        from dispatch_agent.planning.promise_window import subtract

        if not self.excluded_windows:
            return [self.window]
        return subtract(self.window, self.excluded_windows, min_width=min_width)


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
    # Distance for the leg into this stop. Read DaySequence.distance_recorded before trusting a
    # zero here -- plans published before distance existed carry 0.0 for every leg.
    distance_km_from_prev: float = 0.0


class DaySequence(BaseModel):
    delivery_date: Date
    stops: list[StopAssignment]
    # Sum of drive_minutes_from_prev across stops: depot -> stop1 -> ... -> lastStop. This
    # deliberately EXCLUDES the drive home, because several callers already display and diff
    # this exact number. Use round_trip_drive_minutes for anything that compares the real cost
    # of a day -- without the return leg, appending a far-flung final stop looks free.
    total_drive_minutes: int
    return_drive_minutes: int = 0
    return_distance_km: float = 0.0
    # False on every plan published before distance was recorded. This is what lets a UI print
    # "not recorded" rather than drawing a 0 km bar beside a real one.
    distance_recorded: bool = False
    status: JobStatus = JobStatus.SEQUENCED
    generated_at: datetime = Field(default_factory=_utcnow)

    @property
    def round_trip_drive_minutes(self) -> int:
        return self.total_drive_minutes + self.return_drive_minutes

    @property
    def total_distance_km(self) -> float:
        return round(sum(s.distance_km_from_prev for s in self.stops), 2)

    @property
    def round_trip_distance_km(self) -> float:
        return round(self.total_distance_km + self.return_distance_km, 2)

    @property
    def completion_minutes(self) -> int:
        """When the crew is back at the depot, as minutes since midnight.

        Derived rather than stored: it is a summary of the stops, and a stored copy could drift
        from them. This is also the expression scoring.overtime_minutes needs, so there is one
        definition of when a day ends rather than two.
        """
        if not self.stops:
            return 0
        last = self.stops[-1].arrival_window.end
        return last.hour * 60 + last.minute + self.return_drive_minutes

    @property
    def departure_minutes(self) -> int:
        """When the crew leaves the depot -- the first arrival, less the drive to it."""
        if not self.stops:
            return 0
        first = self.stops[0].arrival_window.start
        return first.hour * 60 + first.minute - self.stops[0].drive_minutes_from_prev

    @property
    def working_span_minutes(self) -> int:
        """Door to door: how long the crew is actually out for.

        The number the old scoring never looked at, and the reason a slot that added *one* driving
        minute could extend the day by nearly four hours. Driving is what a route costs the road;
        this is what it costs the people.
        """
        if not self.stops:
            return 0
        return self.completion_minutes - self.departure_minutes

    @property
    def service_minutes(self) -> int:
        """Time spent actually delivering, summed across the stops."""
        total = 0
        for stop in self.stops:
            start, end = stop.arrival_window.start, stop.arrival_window.end
            total += (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
        return total

    @property
    def idle_minutes(self) -> int:
        """Time the crew spends waiting: out of the depot, not driving, not delivering.

        Almost always the result of a promise made for later in the day than the route naturally
        reaches. It is invisible in every driving figure -- a van parked outside a customer's block
        for three hours has driven nowhere -- which is exactly why it has to be measured.
        """
        if not self.stops:
            return 0
        return max(0, self.working_span_minutes - self.round_trip_drive_minutes - self.service_minutes)


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
    # What the customer said they could do. NOT what we offer them -- see `promise_window`.
    window: TimeWindow
    # This order's own solved stop, [arrival, arrival + duration]. Safe to expose: it is the
    # candidate's own time, not another customer's.
    service_window: Optional[TimeWindow] = None
    # The narrow window we would actually put to the customer, derived from `service_window`.
    # None when infeasible. This is what gets offered and, on acceptance, locked.
    promise_window: Optional[TimeWindow] = None
    feasible: bool
    infeasible_reason: Optional[str] = None
    baseline_drive_minutes: int = 0
    proposed_drive_minutes: int = 0
    incremental_drive_minutes: int = 0
    # Round-trip kilometres before and after. Real distance, not a weight -- unlike total_score it
    # can be shown to anyone with its unit attached. 0.0 on plans published before distance existed.
    baseline_distance_km: float = 0.0
    proposed_distance_km: float = 0.0
    # The day before and after, as scalars. Deliberately NOT the baseline sequence itself, which
    # would carry other customers' stops into every response that quotes a price.
    baseline_stop_count: int = 0
    proposed_stop_count: int = 0
    baseline_completion_minutes: int = 0
    proposed_completion_minutes: int = 0
    # How long the crew is out, door to door, and how much of that is spent waiting. The figures
    # the old evaluation never carried -- which is how a slot that added one driving minute and
    # nearly four hours to the working day was presented as the efficient choice.
    baseline_span_minutes: int = 0
    proposed_span_minutes: int = 0
    baseline_idle_minutes: int = 0
    proposed_idle_minutes: int = 0
    opens_empty_day: bool = False
    preference_rank: int = 1
    # Where this stop landed in the solved day, and the region it landed in. Read off the sequence,
    # never inferred -- these are what let the agent say "we'll already be in the East" truthfully.
    region: Optional[str] = None
    route_position: int = 0
    route_stop_count: int = 0
    # Two renderings of the same facts. The customer one names no other customer by construction
    # (route_facts.customer_reason); the coordinator one may, because a dispatcher sees the day
    # anyway. Both are built deterministically from the solved route, with no model in the loop.
    customer_reason: Optional[str] = None
    coordinator_reason: Optional[str] = None
    day_opening_penalty_minutes: int = 0
    preference_penalty_minutes: int = 0
    overtime_penalty_minutes: int = 0
    idle_penalty_minutes: int = 0
    # A RANKING INDEX, not a duration and not a price. It mixes real driving minutes with artificial
    # penalties -- a 60-minute empty-day charge is a planning weight, nobody drives it. Never render
    # this to a customer or a coordinator with a time unit; show the components instead.
    #
    # `total_score` is kept for compatibility (OfferedSlot.score, the API, recovery ranking).
    # `operational_score` is the same index with the customer's preference taken back out, and it
    # is what candidates are actually ranked on: preference then breaks ties between operationally
    # comparable days instead of acting as a ten-minute-per-rank bribe against the route.
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
            if self.promise_window is not None or self.service_window is not None:
                raise ValueError("an infeasible evaluation cannot have a window to offer")
        return self

    @model_validator(mode="after")
    def _promise_is_offerable(self) -> "CandidateSlotEvaluation":
        """The promise must be something we can actually keep: inside what the customer offered,
        and wide enough to hold the whole job. Enforced on the model so it cannot be got wrong by
        a caller building one of these by hand."""
        if not self.feasible or self.promise_window is None or self.service_window is None:
            return self
        if not (self.window.start <= self.promise_window.start
                and self.promise_window.end <= self.window.end):
            raise ValueError(
                f"promised {self.promise_window.start:%H:%M}-{self.promise_window.end:%H:%M} but the "
                f"customer is only free {self.window.start:%H:%M}-{self.window.end:%H:%M}"
            )
        if not (self.promise_window.start <= self.service_window.start
                and self.service_window.end <= self.promise_window.end):
            raise ValueError("the promised window does not contain the job it is promising")
        return self


class InsertionEvidence(BaseModel):
    """Why this slot was offered, and where it came from.

    Carried on the slot rather than recomputed for display, for two reasons. A judge can check
    every figure against the route, and -- more importantly -- `source_plan_version` is what makes
    a stale acceptance detectable: if the day has been republished since we offered this, the
    tested position no longer means what it meant, and the customer must choose again rather than
    be inserted at a position that has moved.
    """

    source_plan_id: str
    source_plan_version: int
    anchor_name: str
    anchor_stop_number: int
    anchor_distance_km: float
    placement: str
    insert_position: int
    previous_stop: str
    next_stop: str
    added_distance_km: float
    added_minutes: int
    expected_arrival: Time
    finish_before: Time
    finish_after: Time


class OfferedSlot(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    availability_option_id: str
    # The narrow window put to the customer, derived from the solved arrival -- not the broad
    # availability the option came from.
    date: Date
    window: TimeWindow
    # Why this time, in the customer's terms. Built from the solved route by
    # route_facts.customer_reason and stored here so the message and the button can never disagree,
    # and so the reason survives a page refresh. Names no other customer, quotes no score.
    reason: Optional[str] = None
    score: int = 0
    # Present when the slot came from the insertion search. None for the older evaluation-based
    # path, which offers a whole solved day rather than a position within one.
    evidence: Optional[InsertionEvidence] = None


class AppointmentOffer(BaseModel):
    """One round of "here are the times we can do" put to a customer.

    `round_number` is what enforces the two-round cap: the limit is checked when an offer is
    created, not asked of the model in a prompt, so a model that ignores the instruction cannot
    quietly break the guardrail.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    order_id: str
    # The agent run that created this offer. The authoritative link between what the customer was
    # shown and the tool calls that chose it.
    run_id: Optional[str] = None
    options: list[OfferedSlot] = Field(default_factory=list)
    purpose: OfferPurpose = OfferPurpose.BOOKING
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
    # Who chose THIS step. Per-step rather than per-run because the fallback happens per decision:
    # a run can be part-model, part-standard-procedure, and a single run-level flag would have to
    # pick one of those and be wrong about the other.
    decider: str = "unknown"
    model_id: Optional[str] = None
    # Set when this particular step fell back. Redacted at the write site -- see
    # planning/tools.redact_secrets -- because provider exceptions carry keys and ARNs.
    fallback_reason: Optional[str] = None
    timestamp: datetime = Field(default_factory=_utcnow)


class AgentRunLog(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    event_id: str
    event_type: Optional[PlanningEventType] = None
    order_id: Optional[str] = None
    actions: list[AgentActionLog] = Field(default_factory=list)
    status: AgentRunStatus = AgentRunStatus.RUNNING
    final_summary: str = ""
    # Which provider actually chose the actions, and what it was told to fall back to. Nothing
    # recorded this before: the only trace of a fallback was a "[model unavailable...]" prefix
    # inside a string that gets truncated. An inspector that cannot say whether the model or the
    # standard procedure made these calls is decorative, so this is stored rather than inferred.
    decider: str = "unknown"
    model_id: Optional[str] = None
    # The exception that caused a fallback, kept so "no AWS credentials" is distinguishable from
    # "the model returned something unusable".
    decider_error: Optional[str] = None
    # Who READ the customer's message, which is a different decision from who chose the actions.
    # Kept separate because conflating them was honest only while the steps were rule-driven: with
    # a model choosing tools, overwriting `decider` with the reader's provider would claim the
    # steps were picked by whatever happened to parse the sentence.
    reader: Optional[str] = None
    reader_model_id: Optional[str] = None
    reader_error: Optional[str] = None
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
    # The run that produced this message, and the offer it is presenting. Stored rather than
    # inferred: joining on (order_id, created_at) proximity or "the newest run" puts the wrong
    # trace under a message the moment there are two, which after a page refresh is always.
    # Null on messages created outside an agent run -- a first-class case, not an error.
    run_id: Optional[str] = None
    offer_id: Optional[str] = None
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
