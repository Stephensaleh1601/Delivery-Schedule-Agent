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

import json
import re
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


# -- log sanitisation ---------------------------------------------------------

MAX_LOG_STRING = 300
MAX_LOG_ITEMS = 10
MAX_LOG_BYTES = 4096

# Keys carrying either personal detail or ANOTHER customer's day. A coordinator reading one
# order's activity must not thereby read a different customer's address, phone or schedule.
REDACTED_KEYS = frozenset(
    {
        "address", "raw_text", "postal_code", "phone", "coordinates", "lat", "lng",
        "proposed_sequence", "baseline_sequence", "stops", "arrival_window",
    }
)


def sanitise_for_log(value, _depth: int = 0):
    """Shrink a tool's arguments or result to something safe to persist and show.

    Applied where the log is WRITTEN rather than where it is served, deliberately: the leak then
    never reaches the database, no second consumer can rediscover it, and a tool added later is
    safe by default instead of safe by review.
    """
    if _depth > 4:
        return "..."
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in REDACTED_KEYS:
                out[key] = "[redacted]"
            else:
                out[key] = sanitise_for_log(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        trimmed = [sanitise_for_log(v, _depth + 1) for v in list(value)[:MAX_LOG_ITEMS]]
        if len(value) > MAX_LOG_ITEMS:
            trimmed.append(f"... {len(value) - MAX_LOG_ITEMS} more")
        return trimmed
    if isinstance(value, str):
        return value if len(value) <= MAX_LOG_STRING else value[:MAX_LOG_STRING] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitise_for_log(str(value), _depth)


# Anything shaped like a credential inside a provider exception. Bedrock and OpenAI errors quote
# the request they failed on, which carries keys, bearer tokens, account ids and ARNs -- and a
# fallback reason is persisted and then rendered in the inspector, so this is a leak with a UI.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(?:sk|rk)-[A-Za-z0-9_\-]{12,}"),      # OpenAI-style keys
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),            # AWS access key ids
    re.compile(r"(?i)\barn:aws[^\s\"']{6,}"),                # ARNs carry the account number
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|authorization|bearer)"
        r"\s*[=:]?\s*[^\s,;)\"']{8,}"
    ),
    re.compile(r"\b\d{12}\b"),                               # bare AWS account ids
]

MAX_FALLBACK_REASON = 300


def redact_secrets(text: str | None) -> str | None:
    """A provider failure, with anything credential-shaped removed.

    "No AWS credentials found" and "the model returned garbage" are different problems and the
    inspector has to be able to tell them apart -- but the exception that says so must not carry
    the credential itself into the database.
    """
    if not text:
        return text
    cleaned = str(text)
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    return cleaned[:MAX_FALLBACK_REASON]


def capped_for_log(value: dict) -> dict:
    """sanitise_for_log, plus a hard ceiling on the whole payload."""
    cleaned = sanitise_for_log(value)
    if not isinstance(cleaned, dict):
        return {"value": cleaned}
    if len(json.dumps(cleaned, default=str)) > MAX_LOG_BYTES:
        return {"_truncated": True, "keys": sorted(cleaned)}
    return cleaned


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
    # The slot the CUSTOMER accepted, set by the loop from the event and by nothing else. While
    # this is None, no appointment can be locked -- see lock_appointment.
    accepted: tuple[str, str] | None = None
    evaluations: list[CandidateSlotEvaluation] = field(default_factory=list)
    offer_id: str | None = None
    # The run these tools are executing inside. Carried on the context so anything a tool creates
    # -- an offer, a message -- can record which run produced it, instead of a later reader having
    # to guess from timestamps.
    run_id: str | None = None
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
    # Which slot was declined. Omitted means the customer declined the whole offer ("none of these
    # work"), and every window in it is excluded.
    slot_id: str | None = None


class ReplanArgs(_Args):
    delivery_date: Date
    reason: str = "replanned by the scheduling agent"


class MessageArgs(_Args):
    order_id: str
    # Optional, and ignored whenever a tool has already produced the wording. See send_message:
    # the customer-facing text is built deterministically from the solved route, and a model
    # rewriting it drops the reason it was built to carry.
    body: str | None = None


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
    """Put times to the customer -- theirs first, and ours only when there is a reason.

    The counteroffer policy lives here rather than in the model, because "is this worth arguing
    with the customer about" is a business rule with a number in it, not a judgement call. A time
    they asked for that we can serve is served; alternatives are added only when
    `negotiation.should_counteroffer` says the request is infeasible, causes overtime, opens an
    otherwise-empty day, or is beaten by a materially shorter route.
    """
    from dispatch_agent.planning import conversation, negotiation

    order = ctx.order or ctx.repo.get_job(args.order_id)
    if order is None:
        return ToolResult(ok=False, tool="create_offer", error="unknown_order",
                          summary=f"No order {args.order_id}.")
    if not ctx.evaluations:
        return ToolResult(ok=False, tool="create_offer", error="no_evaluations",
                          summary="Cannot offer slots before evaluating them.")

    requested = next((e for e in ctx.evaluations if e.feasible), None)
    suggestions = ctx.scratch.get("suggestions") or []
    decision = negotiation.should_counteroffer(
        requested, suggestions, customer_says_fixed=conversation.is_only_option(order)
    )

    offerable = list(ctx.evaluations)
    if decision.should_ask:
        # The suggestions' evaluations join the pool. They are still not the customer's
        # availability -- nothing is written to the order here; that happens only if one is
        # accepted (see offer_service.accept_offer).
        # Keyed on the window's endpoints, not the TimeWindow itself: it is a Pydantic model and
        # therefore unhashable, and building a set of them raises inside the tool -- which surfaces
        # as "we could not offer you anything" rather than as the type error it is.
        seen = {
            (e.date, e.promise_window.start, e.promise_window.end)
            for e in offerable
            if e.feasible and e.promise_window
        }
        for suggestion in suggestions:
            key = (suggestion.date, suggestion.window.start, suggestion.window.end)
            if key not in seen:
                offerable.append(suggestion.evaluation)

    try:
        offer = offer_service.create_offer(ctx.repo, order, offerable, run_id=ctx.run_id)
    except offer_service.OfferError as exc:
        return ToolResult(ok=False, tool="create_offer", error=exc.kind, summary=str(exc))

    ctx.offer_id = offer.id
    # Only apologise for the times we could not fit when there actually were some. Saying it to a
    # customer who gave us one workable time implies we turned down something they never offered.
    ctx.scratch["offer_message"] = offer_service.offer_message(
        offer,
        some_requests_unavailable=any(not e.feasible for e in ctx.evaluations),
    )
    slots = ", ".join(
        f"{offer_service.format_date(s.date)} {offer_service.format_window(s.window)}"
        for s in offer.options
    )
    return ToolResult(
        ok=True, tool="create_offer",
        summary=(
            f"Offered {order.customer_name} {len(offer.options)} slot(s): {slots}. "
            + (f"Suggested an alternative because {decision.reason}."
               if decision.should_ask
               else f"Honoured what they asked for -- {decision.reason}.")
        ),
        data={
            "offer_id": offer.id,
            "round": offer.round_number,
            "counteroffered": decision.should_ask,
            "counteroffer_reason": decision.kind,
        },
    )


@tool("send_message", MessageArgs)
def send_message(args: MessageArgs, ctx: ToolContext) -> ToolResult:
    """Send the wording the previous step produced.

    Simulated send, recorded with a direction so "how many customers did we contact?" is a query
    rather than a guess.

    The body is taken from the context, not from the model, whenever a tool has prepared one. A
    live run had gpt-4o-mini rewrite the offer into "Dear Mrs. Lee ... Best regards, The Delivery
    Team" -- fluent, and missing the route reason the message existed to carry ("we'll already be
    delivering in the East"). Customer-facing text is built from the solved route by
    planning/route_facts, and a model paraphrasing it is a model inventing the explanation.
    """
    prepared = ctx.scratch.get("offer_message") or ctx.scratch.get("customer_message")
    body = prepared or (args.body or "")
    if not body.strip():
        return ToolResult(ok=False, tool="send_message", error="nothing_to_send",
                          summary="No message has been prepared, so there is nothing to send.")
    # Linked to the run that produced it and, when this message is presenting an offer, to that
    # offer -- so the inspector under this bubble opens THESE calls after a refresh.
    message = offer_service.record_message(
        ctx.repo, args.order_id, body,
        MessageDirection.OUTBOUND, run_id=ctx.run_id, offer_id=ctx.offer_id,
    )
    return ToolResult(
        ok=True, tool="send_message",
        summary=(
            f"Message sent to the customer ({len(body)} chars)"
            + ("." if prepared else " -- no prepared wording, so the agent's own text was used.")
        ),
        data={"order_id": args.order_id, "message_id": message.id},
    )


@tool("lock_appointment", AcceptArgs)
def lock_appointment(args: AcceptArgs, ctx: ToolContext) -> ToolResult:
    """Turn an accepted slot into a protected promise, and republish the day.

    Refuses unless the customer actually accepted THIS slot. The check is here rather than in the
    prompt because a live run showed why: gpt-4o-mini called this on its own initiative while an
    offer was still unanswered, and once it succeeded -- booking a van to a customer who had been
    asked a question and had not yet replied. There is no wording that reliably prevents that, and
    the failure is invisible to the person it happens to.

    `ctx.accepted` is set by the loop from a CUSTOMER_ACCEPTED_OFFER event and by nothing else, so
    "the customer said yes" is a fact about the conversation rather than a claim by the model.
    """
    if ctx.accepted is None:
        return ToolResult(
            ok=False, tool="lock_appointment", error="customer_has_not_accepted",
            summary=(
                "Refused: the customer has not accepted anything. An offer they have not answered "
                "is a question, not a booking."
            ),
        )
    if ctx.accepted != (args.offer_id, args.slot_id):
        expected_offer, expected_slot = ctx.accepted
        return ToolResult(
            ok=False, tool="lock_appointment", error="wrong_slot",
            summary=(
                f"Refused: the customer accepted slot {expected_slot} of offer {expected_offer}, "
                f"not {args.slot_id} of {args.offer_id}."
            ),
        )

    try:
        outcome = offer_service.accept_offer(
            ctx.repo, args.offer_id, args.slot_id,
            routing_client=ctx.routing_client, run_id=ctx.run_id,
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
    """Note what the customer declined -- the time, not the day.

    The declined windows are carved out of the availability options they came from, so the next
    evaluation re-solves the same date around the hole instead of proposing the same slot again.
    """
    offer = offer_service.reject_offer(ctx.repo, args.offer_id, slot_id=args.slot_id)
    ctx.order = ctx.repo.get_job(offer.order_id)
    declined = [s for s in offer.options if args.slot_id is None or s.id == args.slot_id]
    excluded = ", ".join(
        f"{offer_service.format_date(s.date)} {offer_service.format_window(s.window)}"
        for s in declined
    )
    return ToolResult(
        ok=True, tool="record_rejection",
        summary=(
            f"Customer declined {excluded}. That time is excluded; the rest of the day is still "
            f"open, so it will be re-solved rather than dropped."
        ),
        data={
            "order_id": offer.order_id,
            "excluded_windows": [
                {
                    "date": s.date.isoformat(),
                    "start": s.window.start.strftime("%H:%M"),
                    "end": s.window.end.strftime("%H:%M"),
                }
                for s in declined
            ],
        },
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
        # Say what was wrong, in the summary -- which is the part the model reads back on its next
        # turn. "Arguments were not valid" told it nothing, so a live run repeated the identical
        # bad call six times and burned the whole step budget without ever learning that it had
        # written `message` where `body` was expected.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
            for e in exc.errors(include_url=False)[:3]
        )
        expected = ", ".join(spec.args_model.model_fields) or "no arguments"
        return ToolResult(
            ok=False, tool=action, error="invalid_arguments",
            summary=(
                f"Arguments for {action} were not valid ({problems}). "
                f"It takes exactly: {expected}."
            ),
            data={"expected": list(spec.args_model.model_fields)},
        )

    try:
        return spec.fn(args, ctx)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the coordinator, never swallowed
        return ToolResult(ok=False, tool=action, error=type(exc).__name__, summary=str(exc))


def allowed_actions() -> list[str]:
    return sorted(TOOL_REGISTRY)


def describe_arguments() -> dict[str, dict]:
    """Each action's argument shape, generated from the Pydantic model that validates it.

    The decision schema used to hand the model `arguments: {"type": "object"}` with no properties,
    so it had to guess field names -- and a live run guessed `message` for `body` and omitted
    `order_id` entirely, six times in a row, because extra="forbid" rejected each attempt without
    ever telling it what the right names were. Generated here rather than written out so a tool
    added later cannot drift from its own documentation.
    """
    described: dict[str, dict] = {}
    for name, spec in TOOL_REGISTRY.items():
        fields = {}
        for field_name, field in spec.args_model.model_fields.items():
            annotation = getattr(field.annotation, "__name__", str(field.annotation))
            fields[field_name] = {
                "type": annotation,
                "required": field.is_required(),
            }
        described[name] = fields
    return described


def render_argument_help() -> str:
    """The same thing as prompt text: one line per action, listing its arguments."""
    lines = []
    for name, fields in sorted(describe_arguments().items()):
        if not fields:
            lines.append(f"- {name}: no arguments")
            continue
        parts = [
            f"{f}" + ("" if spec["required"] else " (optional)")
            for f, spec in fields.items()
        ]
        lines.append(f"- {name}: {', '.join(parts)}")
    return "\n".join(lines)


# Registers the natural-language negotiation tools into TOOL_REGISTRY above. A bottom import
# deliberately: that module imports `tool`, `ToolResult` and `_Args` from here, so it can only be
# loaded once this module is fully defined. Importing either module now yields the whole allow-list,
# which matters because `allowed_actions()` is what the model is shown.
from dispatch_agent.planning import negotiation_tools as _negotiation_tools  # noqa: E402,F401
