"""The tools a natural-language conversation needs, on top of the booking ones.

Separate from `tools.py` only for size: these register into the same allow-list, obey the same
`extra="forbid"` argument validation, and are dispatched by the same guarded `dispatch()`. The
module is imported at the bottom of `tools.py` so importing either one gives the full registry.

The division of labour is unchanged and is the point. The model reads the customer's message and
picks a tool; these functions decide what is true. In particular:

- `record_availability` writes down only what the CUSTOMER said.
- `suggest_route_aware_windows` produces things WE are proposing, and writes nothing.

There is deliberately no path from the second into the first. That is what makes "never claim the
customer is available at a time they did not give" a property of the code rather than a rule someone
has to remember.
"""
from __future__ import annotations

from datetime import date as Date

from pydantic import Field

from dispatch_agent.models import PlanningStatus, TimeWindow
from dispatch_agent.planning import offer_service
from dispatch_agent.planning.tools import ToolContext, ToolResult, _Args, tool


class AvailabilityArgs(_Args):
    order_id: str
    # Windows the CUSTOMER stated, already resolved to concrete dates. The model identifies the
    # phrases; planning/language turns "next Tuesday" into a date against the Singapore planning
    # clock. A model asked to do that arithmetic produces a date a week out and nothing catches it.
    windows: list[dict] = Field(default_factory=list)
    # They have said this is their only possible time. Stops us counteroffering, for good.
    is_fixed: bool = False


class SuggestArgs(_Args):
    order_id: str
    # Dates the customer has already ruled out, which must not come back as suggestions.
    exclude_dates: list[Date] = Field(default_factory=list)


class ExplainArgs(_Args):
    order_id: str
    question: str = ""


class ClarifyArgs(_Args):
    order_id: str
    question: str


@tool("record_availability", AvailabilityArgs)
def record_availability(args: AvailabilityArgs, ctx: ToolContext) -> ToolResult:
    """Write down what the customer said they can do."""
    from dispatch_agent.planning import conversation
    from dispatch_agent.planning.language import StatedWindow

    order = ctx.repo.get_job(args.order_id)
    if order is None:
        return ToolResult(ok=False, tool="record_availability", error="unknown_order",
                          summary=f"No order {args.order_id}.")

    stated = []
    for i, raw in enumerate(args.windows):
        try:
            stated.append(
                StatedWindow(
                    date=Date.fromisoformat(str(raw["date"])),
                    window=TimeWindow(start=raw["start"], end=raw["end"]),
                    phrase=str(raw.get("phrase", "")),
                    preference_rank=int(raw.get("preference_rank", i + 1)),
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            return ToolResult(ok=False, tool="record_availability", error="bad_window",
                              summary=f"Could not read window {i + 1}: {exc}")

    if not stated:
        return ToolResult(ok=False, tool="record_availability", error="no_windows",
                          summary="No usable time was given, so nothing was recorded.")

    outside = conversation.horizon_complaint([s.date for s in stated])
    if outside:
        # A real answer, not an error to hide: the customer is told the range and why.
        ctx.scratch["customer_message"] = outside
        return ToolResult(
            ok=False, tool="record_availability", error="outside_horizon",
            summary="The date they asked for is outside the bookable window; explained the range.",
            data={"requested": [s.date.isoformat() for s in stated]},
        )

    order.availability_options = conversation.merge_availability(order, stated)
    if args.is_fixed:
        conversation.mark_timing_fixed(order)
    if order.planning_status is PlanningStatus.PENDING_AVAILABILITY:
        order.set_planning_status(PlanningStatus.PENDING_PLANNING)
    ctx.repo.save_job(order)
    ctx.order = order

    return ToolResult(
        ok=True, tool="record_availability",
        summary=(
            f"Noted {len(stated)} time(s) from {order.customer_name}"
            + (" -- they say it is their only option." if args.is_fixed else ".")
        ),
        data={
            "options": [
                {"date": o.date.isoformat(),
                 "start": o.window.start.strftime("%H:%M"),
                 "end": o.window.end.strftime("%H:%M"),
                 "preference_rank": o.preference_rank}
                for o in order.availability_options
            ],
            "timing_is_fixed": args.is_fixed,
        },
    )


@tool("suggest_route_aware_windows", SuggestArgs)
def suggest_route_aware_windows(args: SuggestArgs, ctx: ToolContext) -> ToolResult:
    """Days across the horizon where this delivery would fit an existing route well.

    Each suggestion is a real solved slot: the day is sequenced with the order inserted, and the
    window comes from the arrival OR-Tools chose. Nothing is a guess about the map, nothing is
    recorded as the customer's availability, and no other customer's name or address appears in
    the result -- these are questions to put to them, built from route-impact components.
    """
    from dispatch_agent.planning import negotiation

    order = ctx.order or ctx.repo.get_job(args.order_id)
    if order is None:
        return ToolResult(ok=False, tool="suggest_route_aware_windows", error="unknown_order",
                          summary=f"No order {args.order_id}.")

    suggestions = negotiation.route_aware_windows(
        order, ctx.candidate_service(), exclude_dates=set(args.exclude_dates)
    )
    ctx.scratch["suggestions"] = suggestions

    listed = "; ".join(
        f"{offer_service.format_date(s.date)} {offer_service.format_window(s.window)}"
        for s in suggestions
    )
    return ToolResult(
        ok=True, tool="suggest_route_aware_windows",
        summary=(
            f"Found {len(suggestions)} route-friendly alternative(s): {listed}"
            if suggestions
            else "No other day in the horizon fits this delivery."
        ),
        data={
            "alternatives": [
                {
                    "date": s.date.isoformat(),
                    "start": s.window.start.strftime("%H:%M"),
                    "end": s.window.end.strftime("%H:%M"),
                    # Components, never one combined number.
                    "added_drive_minutes": s.evaluation.incremental_drive_minutes,
                    "opens_new_day": s.evaluation.opens_empty_day,
                    "overtime_minutes": s.evaluation.overtime_penalty_minutes,
                    "reason": s.evaluation.customer_reason,
                    # Explicitly not availability. Nothing downstream may treat it as such.
                    "status": "tentative_suggestion",
                }
                for s in suggestions
            ]
        },
    )


@tool("explain_choice", ExplainArgs)
def explain_choice(args: ExplainArgs, ctx: ToolContext) -> ToolResult:
    """Answer "why this time?" from the solved route, not from a story.

    Every figure comes from an evaluation this run already produced. With nothing evaluated there
    is nothing honest to say, and the tool fails rather than improvising -- which is the whole
    difference between an explanation and a plausible sentence.
    """
    order = ctx.order or ctx.repo.get_job(args.order_id)
    if order is None:
        return ToolResult(ok=False, tool="explain_choice", error="unknown_order",
                          summary=f"No order {args.order_id}.")

    feasible = [e for e in ctx.evaluations if e.feasible]
    if not feasible:
        return ToolResult(
            ok=False, tool="explain_choice", error="nothing_evaluated",
            summary="No solved route to explain yet -- the timings have to be evaluated first.",
        )

    best = feasible[0]
    parts: list[str] = []
    if best.customer_reason:
        parts.append(best.customer_reason)
    if best.incremental_drive_minutes > 0:
        parts.append(
            f"Fitting you in adds about {best.incremental_drive_minutes} minutes of driving to "
            f"that day's route."
        )
    if best.opens_empty_day:
        parts.append("It also means sending a van out on a day with no other deliveries on it.")

    blocked = [e for e in ctx.evaluations if not e.feasible and e.infeasible_reason]
    if blocked:
        parts.append(f"The other time you asked about does not fit: {blocked[0].infeasible_reason}.")

    explanation = " ".join(parts) or "That is simply when the van can reach you."
    ctx.scratch["customer_message"] = explanation

    return ToolResult(
        ok=True, tool="explain_choice",
        summary="Explained the timing from the solved route.",
        data={
            "added_drive_minutes": best.incremental_drive_minutes,
            "opens_new_day": best.opens_empty_day,
            "overtime_minutes": best.overtime_penalty_minutes,
            "region": best.region,
            "stops_before": best.baseline_stop_count,
            "stops_after": best.proposed_stop_count,
        },
    )


@tool("ask_clarification", ClarifyArgs)
def ask_clarification(args: ClarifyArgs, ctx: ToolContext) -> ToolResult:
    """Ask one question rather than guess.

    The alternative is a confident wrong date, which costs a customer a delivery day. One question,
    and only one -- an agent that interrogates is as unusable as one that guesses.
    """
    ctx.scratch["customer_message"] = args.question
    return ToolResult(
        ok=True, tool="ask_clarification",
        summary=f"Asked the customer to clarify: {args.question}",
        data={"order_id": args.order_id},
    )
