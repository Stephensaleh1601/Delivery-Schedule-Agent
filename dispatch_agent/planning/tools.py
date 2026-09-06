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
    # Which tools this run may use, from the customer's intent. None means no scoping, for the
    # operational events (a readiness delay, the morning run) that are not a reply to anybody.
    allowed_tools: frozenset[str] | None = None
    # What `legal_actions` permitted for the step about to run. Narrower than `allowed_tools`,
    # which is the whole intent scope: this is the same set the model was offered.
    legal_now: frozenset[str] | None = None
    # Tools that have already succeeded in this run. Used to enforce once-only actions.
    succeeded: set[str] = field(default_factory=set)
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
    # Accepted and ignored. The run is already about one order, and refusing this produced a
    # failed step in the middle of a customer turn over information nobody was missing.
    order_id: str | None = None
    # Which slot was declined. Omitted means the customer declined the whole offer ("none of these
    # work"), and every window in it is excluded.
    slot_id: str | None = None


class ReplanArgs(_Args):
    delivery_date: Date
    reason: str = "replanned by the scheduling agent"


class MessageArgs(_Args):
    # Optional: the run is already about one order, and a model that omits it was producing a
    # validation failure in the middle of a customer turn for no information anybody lacked.
    order_id: str | None = None
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
        offer = offer_service.create_offer(
            ctx.repo,
            order,
            offerable,
            run_id=ctx.run_id,
            # A concrete new time from the customer can be checked even after the automatic
            # two-round cap. The cap limits our suggestions, not their ability to counter-propose.
            customer_initiated=bool(ctx.scratch.get("stated_windows")),
        )
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
        # Say how to succeed, not just that it failed. A bare "nothing to send" was repeated
        # verbatim in the digest, so the model read it, learned nothing, and called the same tool
        # the same way until the step budget ran out -- nine identical failures in one turn.
        if "search_delivery_policy" in ctx.succeeded:
            summary = (
                "This reply is yours to write. Call send_message again with `body` set to the "
                "answer, in your own words, using only the policy rules listed above."
            )
        else:
            summary = "No message has been prepared, so there is nothing to send."
        return ToolResult(ok=False, tool="send_message", error="nothing_to_send", summary=summary)
    # Linked to the run that produced it and, when this message is presenting an offer, to that
    # offer -- so the inspector under this bubble opens THESE calls after a refresh.
    message = offer_service.record_message(
        ctx.repo, args.order_id or (ctx.order.id if ctx.order else None), body,
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


# What each customer intent is allowed to do. The global registry says what the system CAN do;
# this says what answering THIS message may do, which is a much smaller thing.
#
# Without it, asking "why this timing?" ran eight tools -- it rejected the offer it was explaining,
# went looking for replacement customers, and raised a coordinator exception -- because every tool
# was reachable from every intent and only the rule decider ever consulted what had already been
# done. A question about a booking must not be able to change the booking.
INTENT_TOOLS: dict[str, frozenset[str]] = {
    # SIX actions, one per thing a coordinator actually does -- not sixteen small ones. The model
    # makes one choice per turn: which situation is this? Everything inside each one is fixed.
    #
    # Route scope is a property of the tool rather than an argument. `find_normal_slot` reads the
    # customer's own day, `find_requested_day_slot` the day they named, `find_fallback_options`
    # both. There is no scope to pass, so there is no scope to get wrong -- and the tool name in
    # the trace says which routes were searched.
    #
    # The granular tools are still registered and still used by the operational events (a
    # readiness delay, the morning run), which carry no intent and so are not scoped here.

    "explain": frozenset({"explain_offer", "send_message", "finish"}),
    "accept": frozenset({"confirm_offer", "send_message", "finish"}),
    "reject": frozenset({
        "record_rejection", "find_fallback_options", "escalate_booking", "send_message", "finish",
    }),
    "provide_availability": frozenset({
        "record_availability", "find_normal_slot", "find_requested_day_slot",
        "escalate_booking", "send_message", "finish",
    }),
    # A question about how delivery works. Read the policy, answer, stop.
    #
    # What is NOT here is the point: no record_availability, no offer, no rejection, no lock, no
    # route change, no dispatch. Someone asking what the windows are has not booked anything, and
    # this set is what makes that structurally true rather than something the prompt asks for.
    "policy_question": frozenset({
        "search_delivery_policy", "answer_from_policy", "escalate_booking", "send_message",
        "finish",
    }),
    "general_support": frozenset({"ask_clarification", "escalate_booking", "send_message", "finish"}),
    "unclear": frozenset({"ask_clarification", "escalate_booking", "send_message", "finish"}),
}

# Actions that must succeed at most once per run. A second `send_message` is a duplicate bubble in
# the customer's thread; a second `lock_appointment` is a second attempt to book something already
# booked. The live model did both -- three identical offer messages in one run -- because nothing
# stopped it, and the idempotent second lock only looked harmless.
ONCE_PER_RUN = frozenset({
    "send_message", "lock_appointment", "create_offer", "create_exception",
    "confirm_offer", "escalate_booking", "explain_offer",
    "find_normal_slot", "find_requested_day_slot", "find_fallback_options",
    # Asking the same question three times in one run is the same duplicate-action failure as
    # sending the same offer three times. It happened: the model called this, saw wording had been
    # prepared, called it again, and finished without ever sending any of it.
    "ask_clarification",
    # Same rule, same reason: a second offer in one run is a second set of choices in the
    # customer's thread, and whichever arrives last is the one they answer.
    "create_normal_offer", "create_alternative_offer",
})


def dispatch(action: str, arguments: dict | None, ctx: ToolContext) -> ToolResult:
    """Run one allow-listed action.

    Three separate refusals, each returning a failed result rather than raising -- which is what
    makes the guardrails observable: the run log shows what was refused, and nothing moved.
    """
    spec = TOOL_REGISTRY.get(action)
    if spec is None:
        return ToolResult(
            ok=False, tool=action, error="action_not_allowed",
            summary=f"Refused an action that is not on the approved list: {action!r}.",
        )

    # The state gate, enforced rather than merely offered. It used to be advisory: the schema the
    # model saw listed only legal actions, but nothing stopped one outside that list from running,
    # so a model that ignored the enum reached the tool anyway. It did -- `send_message` before any
    # wording existed, nine times in one turn, because the failure said what was wrong and never
    # what was allowed instead.
    if ctx.legal_now is not None and action not in ctx.legal_now:
        return ToolResult(
            ok=False, tool=action, error="not_legal_yet",
            summary=(
                f"Refused {action}: not permitted at this point in the run. Right now you may "
                f"call: {', '.join(sorted(ctx.legal_now))}."
            ),
        )

    if ctx.allowed_tools is not None and action not in ctx.allowed_tools:
        return ToolResult(
            ok=False, tool=action, error="not_allowed_for_intent",
            summary=(
                f"Refused {action}: answering this message may only use "
                f"{', '.join(sorted(ctx.allowed_tools))}."
            ),
        )

    if action in ONCE_PER_RUN and action in ctx.succeeded:
        return ToolResult(
            ok=False, tool=action, error="already_done",
            summary=f"Refused a second {action} in one run -- it has already succeeded.",
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
        result = spec.fn(args, ctx)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the coordinator, never swallowed
        return ToolResult(ok=False, tool=action, error=type(exc).__name__, summary=str(exc))

    if result.ok:
        ctx.succeeded.add(action)
    return result


# What each action is for, in the model's terms: when to reach for it, when not to, and what
# has to follow. Held here rather than in the system prompt because the permitted set changes
# every turn -- describing all sixteen up front would spend the prompt on actions that are not
# available, and leave the available ones undescribed.
TOOL_GUIDE: dict[str, str] = {
    "find_normal_slot": (
        "Searches the customer's OWN delivery day -- the one their address belongs to -- and "
        "offers the best proven slot on it. Use for a normal booking, including when they simply "
        "say they are flexible. Do not use after a rejection, or when they asked for a different "
        "day. Followed by send_message."
    ),
    "find_requested_day_slot": (
        "Searches ONLY the day the customer explicitly asked for, even when it is not theirs. Use "
        "when they named a day; record_availability first so the day is on file. If it will not "
        "take them, it says so and offers to check their own day. Followed by send_message."
    ),
    "find_fallback_options": (
        "Searches BOTH published routes and offers the calculated top three. Use only after the "
        "customer has turned an offer down. Fewer than three means escalate, not offer two. Never "
        "reorder what it returns. Followed by send_message."
    ),
    "confirm_offer": (
        "Books the slot the customer accepted. Use only on a clear acceptance of a specific "
        "option they were shown. Never read an acceptance as new availability."
    ),
    "explain_offer": (
        "Says why the offered times were chosen, from evidence already calculated. Read-only: it "
        "cannot reject, confirm or change anything. Followed by send_message."
    ),
    "escalate_booking": (
        "Hands the customer to a coordinator and tells them so. Use when nothing fits, fewer than "
        "three alternatives exist, or a person must decide. Followed by send_message."
    ),
    "record_availability": (
        "Use when the customer names a day or time they can receive a delivery. Not for "
        "questions, rejections or confirmations. Takes only an order id -- the times come from "
        "the parser. Next: check a route."
    ),
    "record_rejection": (
        "Use when the customer turns down a time they were offered. Excludes that WINDOW, not "
        "the whole day. Next: search for alternatives."
    ),
    "search_delivery_policy": (
        "Use whenever the customer asks about delivery days, available time windows, regional "
        "clusters, attendance requirements, unattended delivery, booking rules, route policies "
        "or driver policies. It searches the company delivery-policy knowledge base and returns "
        "relevant facts. It never changes an order, offer or route. Pass their question in "
        "`question`, in their own words. Next: answer_from_policy. If it finds nothing, "
        "escalate_booking instead."
    ),
    "answer_from_policy": (
        "Use straight after search_delivery_policy found something. Write the customer's reply "
        "yourself in `answer` -- your own plain sentences, built from ONLY the rules the search "
        "returned, answering what they actually asked. Then send_message."
    ),
    "retrieve_policy": (
        "Reads the written delivery rules. Use before touching a route so the reply can cite the "
        "policy rather than paraphrase it. Read-only."
    ),
    "get_existing_routes": (
        "Lists the published Friday and Saturday routes. Read-only. Use to see what exists "
        "before searching it."
    ),
    "find_insertion_options": (
        "Tests real insertion positions without changing any route. `scope` picks the workflow: "
        "'cluster' for a normal booking (their own delivery day only -- do not look at the other "
        "day, they did not ask), 'requested' when they explicitly named a day that is not "
        "theirs, 'both' only after they have rejected an offer and will consider anything. "
        "'requested' needs the day recorded first, and refuses rather than quietly searching "
        "their own day instead. 'cluster' needs nothing stated at all -- their day comes "
        "from their address, so a customer who just says 'any time' is a normal booking. "
        "Results come back already ranked; never reorder or re-select them."
    ),
    "create_normal_offer": (
        "Use after the search proves ONE option on their own -- or explicitly requested -- day. "
        "Creates a single offer. Must be followed by send_message."
    ),
    "create_alternative_offer": (
        "Use only after a rejection. Requires exactly three calculated options. Never invent, "
        "drop or reorder them; if there are fewer than three, escalate instead. Must be followed "
        "by send_message."
    ),
    "explain_choice": (
        "Answers 'why this time?' from evidence already calculated. Read-only: it must not "
        "reject, confirm or alter an offer. Must be followed by send_message."
    ),
    "ask_clarification": (
        "Use when the message is unclear. ONE specific question, never a guessed date. Once per "
        "run. Must be followed by send_message."
    ),
    "create_exception": (
        "Use when no route fits, fewer than three fallback options exist, or a person must "
        "decide. Must be followed by send_message."
    ),
    "send_message": (
        "Sends the reply the previous step prepared -- you do not write it. Exactly once, after "
        "an offer, explanation, clarification or escalation. Never end a customer turn without "
        "calling this."
    ),
    "lock_appointment": (
        "Use only when the customer clearly accepts one option they were offered. Needs the "
        "exact offer_id and slot_id from the digest. Never read an acceptance as new "
        "availability."
    ),
    "finish": "Ends the turn. Only after the customer has been sent a reply.",
}


def guide_for(actions: list[str]) -> str:
    """The descriptions for just the actions permitted right now."""
    lines = [f"- `{a}`: {TOOL_GUIDE[a]}" for a in actions if a in TOOL_GUIDE]
    return "\n".join(lines)


def allowed_actions() -> list[str]:
    return sorted(TOOL_REGISTRY)


# Actions whose whole point is to depend on a verified search having happened first. Offering
# before searching would be offering something nobody checked.
NEEDS_SEARCH = frozenset({"create_normal_offer", "create_alternative_offer"})


def legal_actions(state: dict, ctx: ToolContext) -> list[str]:
    """What the agent may do RIGHT NOW, given the booking state -- not the whole registry.

    This is the state gate. `dispatch()` has always refused an action that is out of scope, but
    the model was still shown every tool in the registry and left to work out which ones made
    sense; being offered `lock_appointment` before anybody accepted anything is an invitation to
    call it. The same set is now what the model sees and what dispatch enforces, so an illegal
    action is not a temptation the prompt has to talk it out of.

    Narrowing, never widening: the intent scope from INTENT_TOOLS still applies on top, and
    dispatch re-checks everything independently. A bug here can make the agent do less than it
    should; it cannot make it do something unsafe.
    """
    done = {a.tool for a in state.get("actions", []) if getattr(a, "ok", False)}
    scope = set(ctx.allowed_tools) if ctx.allowed_tools is not None else set(TOOL_REGISTRY)

    # Finishing is always available. An agent with no legal move must still be able to stop.
    legal = {"finish"} | (scope - ONCE_PER_RUN - done)

    # Once-per-run actions stay legal until they have actually succeeded.
    legal |= {action for action in scope & ONCE_PER_RUN if action not in ctx.succeeded}

    searched = bool(getattr(ctx.scratch.get("insertion"), "options", None))
    if not searched:
        legal -= NEEDS_SEARCH

    # -- prerequisites, enforced here rather than asked for in the prompt ---------------
    #
    # Each of these is an ordering the work genuinely has: you cannot search around a time the
    # customer gave until it is written down, and you cannot search around a rejection until the
    # rejection exists. A prompt can only ask; this decides.

    # What the customer just said must be on file before any search reads it. Otherwise the
    # search prices an order that still holds whatever it held before this message.
    if ctx.scratch.get("stated_windows") and "record_availability" not in ctx.succeeded:
        legal -= {"find_normal_slot", "find_requested_day_slot", "find_fallback_options"}

    # The fallback searches "everything except what they turned down" -- which is only true once
    # the rejection has been recorded and the declined window excluded.
    if "record_rejection" not in ctx.succeeded:
        legal.discard("find_fallback_options")

    # Nothing is booked that the customer did not accept. The tool refuses this too; hiding it
    # means the model is never shown a booking it could make by mistake.
    if ctx.accepted is None:
        legal -= {"lock_appointment", "confirm_offer"}
    elif "confirm_offer" in ctx.succeeded or "lock_appointment" in ctx.succeeded:
        # Confirmation already wrote the customer reply and published the route. There is no
        # second decision to make, even when an older event did not carry the narrow accept scope.
        return ["finish"]

    # A policy question is a short, ordered hand-off: retrieve facts, turn those facts into a
    # customer answer, then send it.  Leaving all four actions visible after the search let the
    # live model jump straight to ``send_message`` before any wording existed.  It then repeated
    # that empty send until the step limit.  The model still writes the answer; this gate only
    # prevents it from skipping the required hand-off between tools.
    if ctx.allowed_tools == INTENT_TOOLS["policy_question"]:
        attempted = {a.tool for a in state.get("actions", [])}
        if (
            "search_delivery_policy" not in attempted
            and "search_delivery_policy" not in ctx.succeeded
        ):
            return ["search_delivery_policy"]
        if (
            "search_delivery_policy" in ctx.succeeded
            and "answer_from_policy" not in ctx.succeeded
        ):
            return ["answer_from_policy"]
        if (
            "search_delivery_policy" not in ctx.succeeded
            and "escalate_booking" not in ctx.succeeded
        ):
            return ["escalate_booking"]

    # A message needs wording a tool prepared. Offering `send_message` with nothing written is how
    # a run ends with an empty bubble in the customer's thread.
    prepared = ctx.scratch.get("offer_message") or ctx.scratch.get("customer_message")
    if not prepared:
        legal.discard("send_message")

    # An answer must come from a rule. Before the search there is nothing to write from.
    if "search_delivery_policy" not in ctx.succeeded:
        legal.discard("answer_from_policy")

    # -- once there is an outcome, there is one move left ------------------------------
    #
    # A workflow has produced something to say. Leaving `escalate_booking` or a second search
    # available invites the model to keep working past the answer it already has -- and the
    # customer waits through every extra step for a reply that was ready.
    if prepared and "send_message" not in ctx.succeeded and "send_message" in scope:
        legal = {"send_message"}

    # And once they have been written to, the turn is over.
    if "send_message" in ctx.succeeded:
        legal = {"finish"}

    return sorted(legal)


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
from dispatch_agent.planning import (  # noqa: E402,F401
    insertion_tools,
    negotiation_tools as _negotiation_tools,
    workflows as _workflows,
)
