"""Six actions, one per thing a coordinator actually does.

The agent used to be asked which of sixteen small tools to call, five or six times a turn: read
the policy, load the routes, search them, offer, send. Every one of those was a fresh chance to
pick wrongly, and most were not decisions at all -- having searched, of course you offer.

So the model now makes ONE choice per turn: which of these six situations it is looking at.
Everything inside each one is fixed.

**Scope is structural, not an argument.** Which routes a search may read is a business rule, so it
is a property of the tool you called rather than a parameter you passed:

    find_normal_slot          their own cluster day, and nothing else
    find_requested_day_slot   the day they explicitly named, and nothing else
    find_fallback_options     both routes, only after a rejection

A model cannot pass the wrong scope here because there is no scope to pass. That also makes the
trace readable: the tool name says which routes were searched.
"""
from __future__ import annotations

from dispatch_agent.models import OfferPurpose
from dispatch_agent.planning import clusters, conversation, insertion, offer_service
from dispatch_agent.planning.clock import PlanningClock
from dispatch_agent.planning.tools import OrderArgs, ToolContext, ToolResult, _Args, tool

MAX_ALTERNATIVES = 3


class QuestionArgs(_Args):
    order_id: str
    question: str = ""


class ReasonArgs(_Args):
    order_id: str
    reason: str = ""


def _cycle_or_fail(ctx: ToolContext, name: str):
    cycle = PlanningClock.coordination_cycle(repo=ctx.repo)
    if cycle is None:
        return None, ToolResult(
            ok=False, tool=name, error="no_coordination_cycle",
            summary="No published Friday and Saturday pair to search.",
        )
    return cycle, None


def _search(ctx: ToolContext, order, dates, name: str, where: str) -> insertion.InsertionSearch:
    # Imported here, not at module scope: tools.py loads this module from its own footer, so a
    # top-level import of insertion_tools closes a cycle and breaks whichever of the three
    # happens to be imported first.
    from dispatch_agent.planning.insertion_tools import _declined, _preferred, _reporter

    found = insertion.search(
        ctx.repo,
        order,
        dates=dates,
        routing_client=ctx.routing_client,
        exclude=_declined(order, ctx),
        prefer=_preferred(order),
        restrict_to=(
            _preferred(order)
            if (_preferred(order) and conversation.is_only_option(order))
            else None
        ),
        on_phase=_reporter(order.id, where, clusters.placement_of(order).day_name),
    )
    ctx.scratch["insertion"] = found
    ctx.scratch["searched"] = {"tool": name, "dates": [d.isoformat() for d in dates]}
    return found


def _offer(ctx: ToolContext, order, found, purpose, name: str) -> ToolResult:
    """Turn a search result into an offer and the wording that goes with it."""
    try:
        offer = offer_service.offer_insertions(
            ctx.repo, order, found.options, purpose=purpose, run_id=ctx.run_id
        )
    except offer_service.OfferError as exc:
        return ToolResult(ok=False, tool=name, error=exc.kind, summary=str(exc),
                          data=found.to_data())

    ctx.offer_id = offer.id
    ctx.scratch["offer_message"] = offer_service.offer_message(offer)

    data = found.to_data()
    data["offer_id"] = offer.id
    data["offered"] = [
        {
            "slot_id": slot.id,
            "date": slot.date.isoformat(),
            "window": {
                "start": f"{slot.window.start:%H:%M}",
                "end": f"{slot.window.end:%H:%M}",
            },
            "added_distance_km": slot.evidence.added_distance_km if slot.evidence else None,
            "added_minutes": slot.evidence.added_minutes if slot.evidence else None,
            "anchor_stop_number": slot.evidence.anchor_stop_number if slot.evidence else None,
            "anchor_distance_km": slot.evidence.anchor_distance_km if slot.evidence else None,
            "promises_moved": 0,
        }
        for slot in offer.options
    ]
    slots = ", ".join(
        f"{offer_service.format_date(s.date)} {offer_service.format_window(s.window)}"
        for s in offer.options
    )
    return ToolResult(
        ok=True,
        tool=name,
        summary=(
            f"Compared {found.stops_checked} stops on {found.routes_checked} route(s), tested "
            f"{found.positions_tested} positions, kept {len(found.options)}. Offered: {slots}."
        ),
        data=data,
    )


@tool("find_normal_slot", OrderArgs)
def find_normal_slot(args: OrderArgs, ctx: ToolContext) -> ToolResult:
    """Search the customer's OWN delivery day and offer the best proven slot on it."""
    order = ctx.repo.get_job(args.order_id)
    if order is None:
        return ToolResult(ok=False, tool="find_normal_slot", error="unknown_order",
                          summary=f"No order {args.order_id}.")

    placement = clusters.placement_of(order)
    if placement.weekday is None:
        # Never quietly search both routes for someone we cannot place -- that offers a day their
        # region does not belong to and calls it their normal one.
        ctx.scratch["customer_message"] = (
            "Could you send me your 6-digit postal code? I need it to work out which of our "
            "delivery days covers you."
        )
        return ToolResult(
            ok=False, tool="find_normal_slot", error="unknown_location",
            summary="Cannot place this address in a delivery region; asked for the postal code.",
        )

    cycle, failure = _cycle_or_fail(ctx, "find_normal_slot")
    if failure:
        return failure

    dates = clusters.cluster_dates(order, cycle.dates)
    found = _search(ctx, order, dates, "find_normal_slot", "cluster")
    if not found.options:
        return ToolResult(
            ok=False, tool="find_normal_slot", error="no_safe_position",
            summary=(
                f"Nothing on the {placement.day_name} route can take {order.customer_name} "
                f"without making an existing delivery late."
            ),
            data=found.to_data(),
        )
    return _offer(ctx, order, found, OfferPurpose.BOOKING, "find_normal_slot")


@tool("find_requested_day_slot", OrderArgs)
def find_requested_day_slot(args: OrderArgs, ctx: ToolContext) -> ToolResult:
    """Search ONLY the day the customer explicitly asked for, even if it is not their own."""
    order = ctx.repo.get_job(args.order_id)
    if order is None:
        return ToolResult(ok=False, tool="find_requested_day_slot", error="unknown_order",
                          summary=f"No order {args.order_id}.")

    cycle, failure = _cycle_or_fail(ctx, "find_requested_day_slot")
    if failure:
        return failure

    dates = clusters.requested_dates(order, cycle.dates)
    if not dates:
        return ToolResult(
            ok=False, tool="find_requested_day_slot", error="no_requested_day",
            summary="No specific day has been recorded for this customer yet.",
        )

    # If every day they named is their own, this is not an exception -- report it as the normal
    # check so the trace and the narration match what actually happened.
    off_cluster = any(clusters.is_off_cluster(order, day) for day in dates)
    found = _search(
        ctx, order, dates, "find_requested_day_slot", "requested" if off_cluster else "cluster"
    )
    if not found.options:
        # Say plainly that the day they asked for will not work. Substituting their normal day
        # without saying so is the failure the rule names.
        asked = ", ".join(offer_service.format_date(d) for d in dates)
        normal = clusters.placement_of(order).day_name
        ctx.scratch["customer_message"] = (
            f"I'm sorry -- we can't fit you in on {asked}; our van isn't passing close enough to "
            f"you that day. Your area is on the {normal} run, so I can look there instead if that "
            f"would work for you?"
        )
        return ToolResult(
            ok=False, tool="find_requested_day_slot", error="requested_day_unavailable",
            summary=f"{asked} cannot take this customer; offered to check {normal} instead.",
            data=found.to_data(),
        )
    return _offer(ctx, order, found, OfferPurpose.BOOKING, "find_requested_day_slot")


@tool("find_fallback_options", OrderArgs)
def find_fallback_options(args: OrderArgs, ctx: ToolContext) -> ToolResult:
    """Search BOTH published routes and offer the calculated top three. Only after a rejection."""
    order = ctx.repo.get_job(args.order_id)
    if order is None:
        return ToolResult(ok=False, tool="find_fallback_options", error="unknown_order",
                          summary=f"No order {args.order_id}.")

    cycle, failure = _cycle_or_fail(ctx, "find_fallback_options")
    if failure:
        return failure

    found = _search(ctx, order, list(cycle.dates), "find_fallback_options", "both")
    if len(found.options) < MAX_ALTERNATIVES:
        return ToolResult(
            ok=False, tool="find_fallback_options", error="too_few_alternatives",
            summary=(
                f"Only {len(found.options)} safe option(s) across both routes; policy requires "
                f"{MAX_ALTERNATIVES}."
            ),
            data=found.to_data(),
        )
    return _offer(ctx, order, found, OfferPurpose.ALTERNATIVE, "find_fallback_options")


@tool("confirm_offer", OrderArgs)
def confirm_offer(args: OrderArgs, ctx: ToolContext) -> ToolResult:
    """Book the slot the customer accepted. Refuses anything they did not actually accept."""
    from dispatch_agent.planning.tools import TOOL_REGISTRY

    if ctx.accepted is None:
        return ToolResult(
            ok=False, tool="confirm_offer", error="nothing_accepted",
            summary="No accepted slot on this turn -- nothing may be booked.",
        )
    offer_id, slot_id = ctx.accepted
    spec = TOOL_REGISTRY["lock_appointment"]
    result = spec.fn(spec.args_model(offer_id=offer_id, slot_id=slot_id), ctx)
    return result.model_copy(update={"tool": "confirm_offer"})


@tool("explain_offer", ReasonArgs)
def explain_offer(args: ReasonArgs, ctx: ToolContext) -> ToolResult:
    """Say why the offered times were chosen. Read-only -- changes nothing."""
    from dispatch_agent.planning.tools import TOOL_REGISTRY

    spec = TOOL_REGISTRY["explain_choice"]
    result = spec.fn(spec.args_model(order_id=args.order_id, question=args.reason), ctx)
    return result.model_copy(update={"tool": "explain_offer"})


@tool("escalate_booking", ReasonArgs)
def escalate_booking(args: ReasonArgs, ctx: ToolContext) -> ToolResult:
    """Hand the customer to a coordinator, and tell them so."""
    from dispatch_agent.planning import plan_service

    # Two escalations, two honest sentences. Reaching here from a question the policy does not
    # cover and apologising about delivery times answers something nobody asked.
    if ctx.scratch.get("policy_no_answer"):
        message = (
            "I'm sorry -- I can't confirm that one from our delivery policy. Let me have a "
            "coordinator follow up with you so you get a proper answer."
        )
    else:
        message = (
            "I'm sorry -- I couldn't find a delivery time that works without affecting another "
            "customer. One of our coordinators will call you shortly to sort it out personally."
        )
    ctx.scratch.setdefault("customer_message", message)
    plan_service.raise_coordinator_exception(
        ctx.repo,
        args.reason or "No safe delivery option; handed to a coordinator.",
        kind="needs_coordinator",
        order_id=args.order_id,
    )
    return ToolResult(
        ok=True, tool="escalate_booking",
        summary="Handed to a coordinator; the customer has been told a person will call.",
    )
