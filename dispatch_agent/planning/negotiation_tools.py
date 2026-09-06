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
    """Just the order. The windows are deliberately NOT an argument.

    They arrive on the ToolContext, already resolved by planning/language from the phrases the
    customer used. The model's job is to decide that this is the moment to write them down -- not
    to retype them. Asking it to hand back a nested {date, start, end} structure is asking it to
    re-key data it did not compute, and a live run duly reformatted "09:00-13:00" into something
    the parser rejected, three times, before giving up and leaving the customer with silence.

    It also closes the fabrication hole. There is no argument here through which a model could
    invent a window the customer never offered.
    """

    order_id: str


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
    from dispatch_agent.planning import conversation, offer_service
    from dispatch_agent.planning.language import StatedWindow

    order = ctx.repo.get_job(args.order_id)
    if order is None:
        return ToolResult(ok=False, tool="record_availability", error="unknown_order",
                          summary=f"No order {args.order_id}.")

    stated: list[StatedWindow] = []
    for i, raw in enumerate(ctx.scratch.get("stated_windows") or []):
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
            # A malformed entry here is our bug, not the model's -- these came from the parser.
            return ToolResult(ok=False, tool="record_availability", error="bad_window",
                              summary=f"Could not read window {i + 1}: {exc}")

    if not stated:
        return ToolResult(ok=False, tool="record_availability", error="no_windows",
                          summary="The customer's message gave no time we could use.")

    is_fixed = bool(ctx.scratch.get("timing_is_fixed"))

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
    if is_fixed:
        conversation.mark_timing_fixed(order)
    if order.planning_status is PlanningStatus.PENDING_AVAILABILITY:
        order.set_planning_status(PlanningStatus.PENDING_PLANNING)
    ctx.repo.save_job(order)
    ctx.order = order

    return ToolResult(
        ok=True, tool="record_availability",
        summary=(
            f"Noted {len(stated)} time(s) from {order.customer_name}: "
            + "; ".join(
                f"{offer_service.format_date(s.date)} "
                f"{s.window.start:%H:%M}-{s.window.end:%H:%M}"
                for s in stated
            )
            + (" -- they say it is their only option." if is_fixed else ".")
        ),
        data={
            "options": [
                {"date": o.date.isoformat(),
                 "start": o.window.start.strftime("%H:%M"),
                 "end": o.window.end.strftime("%H:%M"),
                 "preference_rank": o.preference_rank}
                for o in order.availability_options
            ],
            "timing_is_fixed": is_fixed,
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

    # An offer built from the insertion search carries its own evidence, and that is the thing
    # the customer is actually looking at. Preferred over a fresh evaluation: explaining from a
    # re-solve can quote figures that differ from the ones in the message they are asking about,
    # which is a worse answer than no answer.
    from_offer = _explain_from_offer(order, ctx)
    if from_offer is not None:
        return from_offer

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


def _explain_from_offer(order, ctx: ToolContext) -> ToolResult | None:
    """"Why this time?" answered from the offer on the table, with the evidence stored on it.

    Returns None when there is no such offer, so the caller falls through to the older
    evaluation-based explanation rather than this becoming the only path.
    """
    from dispatch_agent.planning import conversation, offer_service

    offer = conversation.open_offer(ctx.repo, order.id)
    if offer is None:
        return None
    slots = [s for s in offer.options if s.evidence is not None]
    if not slots:
        return None

    lines: list[str] = []
    if len(slots) == 1:
        e = slots[0].evidence
        lines.append(
            f"We are already delivering {e.anchor_distance_km}km from you that day, so fitting "
            f"you in adds only {e.added_distance_km}km and about {e.added_minutes} minutes of "
            f"driving."
        )
    else:
        lines.append("Each of those times is on a route we are already running near you:")
        for index, slot in enumerate(slots, start=1):
            e = slot.evidence
            # offer_service's own formatters: %-I is POSIX-only and raises on Windows, which is
            # the portability trap this codebase already hit once.
            when = f"{offer_service.format_date(slot.date)} {offer_service.format_window(slot.window)}"
            lines.append(
                f"{index}. {when} — nearest stop {e.anchor_distance_km}km away, "
                f"adds {e.added_distance_km}km."
            )
    lines.append(
        "None of them move anyone we have already promised a time -- we check that before "
        "offering."
    )

    explanation = " ".join(lines) if len(slots) == 1 else "\n".join(lines)
    ctx.scratch["customer_message"] = explanation
    return ToolResult(
        ok=True,
        tool="explain_choice",
        summary=f"Explained {len(slots)} offered time(s) from the measured insertion.",
        data={
            "offer_id": offer.id,
            "slots": [
                {
                    "date": s.date.isoformat(),
                    "anchor_distance_km": s.evidence.anchor_distance_km,
                    "added_distance_km": s.evidence.added_distance_km,
                    "added_minutes": s.evidence.added_minutes,
                    "promises_moved": 0,
                }
                for s in slots
            ],
        },
    )
