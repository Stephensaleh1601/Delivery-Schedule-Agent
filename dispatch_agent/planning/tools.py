"""The only actions the scheduling agent may take.

The division of labour is the whole point of the architecture: the model chooses *which* action
and explains why; these functions decide what is actually true. Claude never computes a drive
time, a feasibility, or an appointment window -- it picks from this list and reads back typed
results.

Two properties matter more than the list itself:

- An action that is not in the registry is never executed. It comes back as a failed result, not
  an exception and not a guess.
- Arguments the model produced are validated against a Pydantic model with extra="forbid" before
  the tool runs, so a hallucinated field cannot ride along into a service call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from typing import Callable, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.models import (
    CandidateSlotEvaluation,
    JobRecord,
    MessageDirection,
    PlanningStatus,
    ReadinessStatus,
)
from dispatch_agent.planning import offer_service, plan_service
from dispatch_agent.planning.candidate_service import CandidateService
from dispatch_agent.planning.clock import PlanningClock
from dispatch_agent.solver import LockedPlanInfeasibleError, UnsolvableDayError
from dispatch_agent.webapp.jobs_service import JobSubmissionError


class ToolResult(BaseModel):
    """What a tool hands back to the loop -- and, verbatim, what the coordinator sees in the
    activity log. `summary` must therefore read as an operational fact, not model narration."""

    ok: bool
    tool: str
    summary: str = ""
    data: dict = Field(default_factory=dict)
    error: str | None = None


@dataclass
class ToolContext:
    """Everything the tools operate on. Passed in rather than constructed, so tests can drive the
    whole agent against a temp database and a fake routing client."""

    repo: JobsRepository
    routing_client: RoutingClient | None = None
    order: JobRecord | None = None
    evaluations: list[CandidateSlotEvaluation] = field(default_factory=list)
    offer_id: str | None = None
    scratch: dict = field(default_factory=dict)

    def candidate_service(self) -> CandidateService:
        return CandidateService(repo=self.repo, routing_client=self.routing_client)


class ToolSpec(NamedTuple):
    name: str
    args_model: type[BaseModel]
    fn: Callable[[BaseModel, ToolContext], ToolResult]


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def tool(name: str, args_model: type[BaseModel]):
    def decorate(fn):
        TOOL_REGISTRY[name] = ToolSpec(name, args_model, fn)
        return fn

    return decorate


class _Args(BaseModel):
    """Base for every tool's arguments. extra="forbid" is the point: a model that invents a
    field gets a validation failure it can see and retry, not a silent pass-through."""

    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class OrderArgs(_Args):
    order_id: str


class AcceptArgs(_Args):
    offer_id: str
    slot_id: str


class OfferIdArgs(_Args):
    offer_id: str


class ReplanArgs(_Args):
    delivery_date: Date
    reason: str = "replanned by the scheduling agent"


class MessageArgs(_Args):
    order_id: str
    body: str


class ExceptionArgs(_Args):
    message: str
    order_id: str | None = None
    delivery_date: Date | None = None
    kind: str = "unresolved"


class ReplacementArgs(_Args):
    delivery_date: Date


# -- tools --------------------------------------------------------------------


@tool("evaluate_slots", OrderArgs)
def evaluate_slots(args: OrderArgs, ctx: ToolContext) -> ToolResult:
    """Price every window the customer offered.

    All options in one call, deliberately. Evaluating them one at a time would burn the step
    budget on bookkeeping, and would also lose the shared per-date baseline that makes the whole
    thing cheap.
    """
    order = ctx.repo.get_job(args.order_id)
    if order is None:
        return ToolResult(ok=False, tool="evaluate_slots", error="unknown_order",
                          summary=f"No order {args.order_id}.")

    ctx.order = order
    ctx.evaluations = ctx.candidate_service().evaluate_all(order)
    feasible = [e for e in ctx.evaluations if e.feasible]
    first, last = PlanningClock.horizon()
    return ToolResult(
        ok=True,
        tool="evaluate_slots",
        summary=(
            f"Checked {len(ctx.evaluations)} requested window(s) against the plans for "
            f"{first} to {last}: {len(feasible)} can be delivered."
        ),
        data={
            "feasible_count": len(feasible),
            "options": [
                {
                    "date": e.date.isoformat(),
                    "feasible": e.feasible,
                    "score": e.total_score if e.feasible else None,
                    "reason": e.infeasible_reason,
                }
                for e in ctx.evaluations
            ],
        },
    )


@tool("create_offer", OrderArgs)
def create_offer(args: OrderArgs, ctx: ToolContext) -> ToolResult:
    order = ctx.order or ctx.repo.get_job(args.order_id)
    if order is None:
        return ToolResult(ok=False, tool="create_offer", error="unknown_order",
                          summary=f"No order {args.order_id}.")
    if not ctx.evaluations:
        return ToolResult(ok=False, tool="create_offer", error="no_evaluations",
                          summary="Cannot offer slots before evaluating them.")
    try:
        offer = offer_service.create_offer(ctx.repo, order, ctx.evaluations)
    except offer_service.OfferError as exc:
        return ToolResult(ok=False, tool="create_offer", error="no_feasible_slot", summary=str(exc))

    ctx.offer_id = offer.id
    ctx.scratch["offer_message"] = offer_service.offer_message(offer)
    slots = ", ".join(
        f"{offer_service.format_date(s.date)} {offer_service.format_window(s.window)}"
        for s in offer.options
    )
    return ToolResult(
        ok=True, tool="create_offer",
        summary=f"Offered {order.customer_name} {len(offer.options)} slot(s): {slots}.",
        data={"offer_id": offer.id, "round": offer.round_number},
    )


@tool("send_message", MessageArgs)
def send_message(args: MessageArgs, ctx: ToolContext) -> ToolResult:
    """Simulated send. Recorded with a direction so "how many customers did we contact?" is a
    query rather than a guess."""
    offer_service.record_message(ctx.repo, args.order_id, args.body, MessageDirection.OUTBOUND)
    return ToolResult(
        ok=True, tool="send_message",
        summary=f"Message sent to the customer ({len(args.body)} chars).",
        data={"order_id": args.order_id},
    )


@tool("lock_appointment", AcceptArgs)
def lock_appointment(args: AcceptArgs, ctx: ToolContext) -> ToolResult:
    """Turn an accepted slot into a protected promise, and republish the day."""
    try:
        outcome = offer_service.accept_offer(
            ctx.repo, args.offer_id, args.slot_id, routing_client=ctx.routing_client
        )
    except offer_service.OfferError as exc:
        return ToolResult(ok=False, tool="lock_appointment", error="acceptance_failed", summary=str(exc))

    if outcome.idempotent:
        return ToolResult(
            ok=True, tool="lock_appointment",
            summary="Already confirmed -- nothing further to do.",
            data={"idempotent": True},
        )
    return ToolResult(
        ok=True, tool="lock_appointment",
        summary=(
            f"{outcome.job.customer_name} confirmed for "
            f"{offer_service.format_date(outcome.job.delivery_date)}; window locked. "
            f"Plan v{outcome.plan.version} published."
        ),
        data={
            "order_id": outcome.job.id,
            "delivery_date": outcome.job.delivery_date.isoformat(),
            "plan_version": outcome.plan.version,
        },
    )


@tool("record_rejection", OfferIdArgs)
def record_rejection(args: OfferIdArgs, ctx: ToolContext) -> ToolResult:
    offer = offer_service.reject_offer(ctx.repo, args.offer_id)
    ctx.order = ctx.repo.get_job(offer.order_id)
    return ToolResult(
        ok=True, tool="record_rejection",
        summary="Customer turned the offered slots down; the order is back in planning.",
        data={"order_id": offer.order_id},
    )


@tool("replan_day", ReplanArgs)
def replan_day(args: ReplanArgs, ctx: ToolContext) -> ToolResult:
    try:
        plan = plan_service.replan_day(
            ctx.repo, args.delivery_date, reason=args.reason, routing_client=ctx.routing_client
        )
    except LockedPlanInfeasibleError as exc:
        # Never resolved by moving someone -- that is what the lock is for.
        return ToolResult(
            ok=False, tool="replan_day", error="locked_plan_infeasible",
            summary=f"Cannot replan {args.delivery_date} without breaking a confirmed appointment: {exc}",
            data={"locked_job_ids": exc.locked_job_ids, "blocking_job_id": exc.blocking_job_id},
        )
    except (UnsolvableDayError, JobSubmissionError) as exc:
        return ToolResult(ok=False, tool="replan_day", error="unsolvable", summary=str(exc))

    return ToolResult(
        ok=True, tool="replan_day",
        summary=(
            f"Replanned {args.delivery_date}: {len(plan.sequence.stops)} stops, "
            f"{plan.sequence.round_trip_drive_minutes} min driving. Plan v{plan.version}."
        ),
        data={"plan_version": plan.version, "plan_id": plan.id},
    )


@tool("find_ready_replacements", ReplacementArgs)
def find_ready_replacements(args: ReplacementArgs, ctx: ToolContext) -> ToolResult:
    """Customers who could fill a slot that just freed up.

    Only those who said an earlier delivery would suit them: consent is never assumed. Ranked by
    the same evaluator used for ordinary bookings, so a replacement is chosen on the same terms.
    """
    candidates = [
        job
        for job in ctx.repo.jobs_by_planning_status(
            PlanningStatus.CONFIRMED, PlanningStatus.SEQUENCED
        )
        if job.can_deliver_early
        and job.readiness_status is ReadinessStatus.READY
        and job.delivery_date is not None
        and job.delivery_date > args.delivery_date
    ]
    if not candidates:
        return ToolResult(
            ok=True, tool="find_ready_replacements",
            summary="No customers have agreed to an earlier delivery, so the slot stays empty.",
            data={"candidates": []},
        )

    service = ctx.candidate_service()
    ranked = []
    for job in candidates:
        option = next(
            (o for o in job.availability_options if o.date == args.delivery_date), None
        )
        if option is None:
            # They never offered this date. Quote their existing window on the freed-up day
            # rather than inventing one -- the offer still has to be accepted.
            from dispatch_agent.models import AvailabilityOption

            option = AvailabilityOption(
                date=args.delivery_date,
                window=job.locked_window or job.availability[0],
                preference_rank=1,
            )
        evaluation = service.evaluate(job, option)
        if evaluation.feasible:
            ranked.append((evaluation.total_score, job, evaluation))

    ranked.sort(key=lambda r: r[0])
    ctx.scratch["replacements"] = [
        {"order_id": job.id, "customer_name": job.customer_name, "score": score,
         "option_id": evaluation.availability_option_id}
        for score, job, evaluation in ranked
    ]
    ctx.evaluations = [evaluation for _, _, evaluation in ranked]
    if ranked:
        ctx.order = ranked[0][1]

    return ToolResult(
        ok=True, tool="find_ready_replacements",
        summary=(
            f"{len(ranked)} customer(s) could take the freed slot on {args.delivery_date}; "
            f"best is {ranked[0][1].customer_name}." if ranked
            else f"No feasible replacement for {args.delivery_date}."
        ),
        data={"candidates": ctx.scratch["replacements"]},
    )


@tool("create_exception", ExceptionArgs)
def create_exception(args: ExceptionArgs, ctx: ToolContext) -> ToolResult:
    """Hand the problem to a human. Always available, and always preferable to a guess."""
    exception = plan_service.raise_coordinator_exception(
        ctx.repo, args.message, kind=args.kind, order_id=args.order_id, delivery_date=args.delivery_date
    )
    return ToolResult(
        ok=True, tool="create_exception",
        summary=f"Escalated to a coordinator: {args.message}",
        data={"exception_id": exception.id},
    )


@tool("finish", NoArgs)
def finish(args: NoArgs, ctx: ToolContext) -> ToolResult:
    return ToolResult(ok=True, tool="finish", summary="Done.")


# -- dispatch -----------------------------------------------------------------


def dispatch(action: str, arguments: dict | None, ctx: ToolContext) -> ToolResult:
    """Run one allow-listed action.

    An unknown action returns a failed result rather than raising. That is what makes the
    guardrail observable: the run log shows the refusal, and nothing in the database moved.
    """
    spec = TOOL_REGISTRY.get(action)
    if spec is None:
        return ToolResult(
            ok=False, tool=action, error="action_not_allowed",
            summary=f"Refused an action that is not on the approved list: {action!r}.",
        )

    try:
        args = spec.args_model.model_validate(arguments or {})
    except ValidationError as exc:
        return ToolResult(
            ok=False, tool=action, error="invalid_arguments",
            summary=f"Arguments for {action} were not valid.",
            data={"detail": exc.errors(include_url=False)[:3]},
        )

    try:
        return spec.fn(args, ctx)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the coordinator, never swallowed
        return ToolResult(ok=False, tool=action, error=type(exc).__name__, summary=str(exc))


def allowed_actions() -> list[str]:
    return sorted(TOOL_REGISTRY)
