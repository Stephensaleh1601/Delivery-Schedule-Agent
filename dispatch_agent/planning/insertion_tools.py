"""The three tools that let the agent see the routes without being able to invent anything.

Registered into the same allow-list as everything else, validated the same way, dispatched by the
same guarded `dispatch()`. Imported at the bottom of `tools.py`, so importing either module gives
the whole registry.

The division of labour is the reason these exist as tools at all rather than as prompt context:

- `retrieve_policy` returns the written rule, with its ID, so an explanation can cite the policy
  instead of paraphrasing one the model half-remembers.
- `get_existing_routes` returns what is actually published. The model cannot ask about a day that
  does not exist, and cannot be told about one that does not.
- `find_insertion_options` does every calculation and returns the ranked answer. The model picks
  from that list and explains it; it never produces a distance, a feasibility or an ordering.

None of the three writes anything. That is what makes them safe to expose on a question.
"""
from __future__ import annotations

import re
from pathlib import Path

from dispatch_agent.planning import conversation, insertion
from dispatch_agent.planning.clock import PlanningClock
from dispatch_agent.planning.tools import OrderArgs, ToolContext, ToolResult, _Args, tool

POLICY_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "delivery-policy.md"

# The topics a caller may ask about, mapped to the `## heading` they live under. A closed set on
# purpose: an unknown topic is a failed result the model can see and correct, not an empty answer
# it might read as "there is no rule about that".
POLICY_TOPICS = ("cluster_days", "delivery_windows", "alternatives", "driver_dispatch")

MAX_ALTERNATIVES = 3


# Near-misses a model reaches for, mapped to the heading they meant. Forgiving on the way in and
# exact on the way out: a wrong topic used to cost two turns of the step budget -- one for
# omitting the field, one for guessing "cluster" -- and both showed up in the activity log as
# failures a judge would read as the agent floundering.
TOPIC_ALIASES = {
    "cluster": "cluster_days",
    "clusters": "cluster_days",
    "cluster_day": "cluster_days",
    "days": "cluster_days",
    "window": "delivery_windows",
    "windows": "delivery_windows",
    "delivery_window": "delivery_windows",
    "slots": "delivery_windows",
    "alternative": "alternatives",
    "alternative_slots": "alternatives",
    "fallback": "alternatives",
    "driver": "driver_dispatch",
    "dispatch": "driver_dispatch",
    # Asked while answering "why this time?" -- the rule being explained is the one about
    # which alternatives may be offered.
    "explain": "alternatives",
    "why": "alternatives",
    "reason": "alternatives",
}


class PolicyArgs(_Args):
    # Defaulted so omitting it costs a turn of nothing rather than a failed step. The cluster rule
    # is the one almost every conversation needs first.
    topic: str = "cluster_days"


class RoutesArgs(_Args):
    pass


def _policy_section(topic: str) -> str | None:
    """The body under `## <topic>`, up to the next `##`. Read fresh each call -- the file is small,
    and a cached copy would let the panel quote a rule the repository no longer contains."""
    if not POLICY_PATH.exists():
        return None
    text = POLICY_PATH.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(topic)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return match.group(1).strip() if match else None


def _policy_ids(section: str) -> list[str]:
    return re.findall(r"^### ([A-Z]+-\d+)", section, re.M)


@tool("retrieve_policy", PolicyArgs)
def retrieve_policy(args: PolicyArgs, ctx: ToolContext) -> ToolResult:
    """The written rule for one topic, with its policy IDs."""
    topic = args.topic.strip().lower().replace(" ", "_").replace("-", "_")
    topic = TOPIC_ALIASES.get(topic, topic)
    if topic not in POLICY_TOPICS:
        return ToolResult(
            ok=False,
            tool="retrieve_policy",
            error="unknown_topic",
            summary=f"No policy topic {args.topic!r}. Known topics: {', '.join(POLICY_TOPICS)}.",
        )

    section = _policy_section(topic)
    if section is None:
        return ToolResult(
            ok=False,
            tool="retrieve_policy",
            error="policy_unavailable",
            summary="The delivery policy could not be read.",
        )

    ids = _policy_ids(section)
    return ToolResult(
        ok=True,
        tool="retrieve_policy",
        summary=f"Read the {topic.replace('_', ' ')} policy ({', '.join(ids) or 'no rule ids'}).",
        data={"topic": topic, "policy_ids": ids, "text": section},
    )


@tool("get_existing_routes", RoutesArgs)
def get_existing_routes(args: RoutesArgs, ctx: ToolContext) -> ToolResult:
    """The published routes for this coordination cycle, keyed by date.

    Only active, published plans. A day with no route is not returned as an empty one -- the agent
    must not be able to treat "nothing published yet" as "a route with room on it".
    """
    cycle = PlanningClock.coordination_cycle(repo=ctx.repo)
    if cycle is None:
        return ToolResult(
            ok=False,
            tool="get_existing_routes",
            error="no_coordination_cycle",
            summary=(
                "There is no complete Friday and Saturday pair with published routes, so there is "
                "nothing to insert into. A coordinator needs to publish the week first."
            ),
        )

    routes = []
    for day in cycle.dates:
        plan = ctx.repo.active_plan(day)
        stops = []
        for stop in plan.sequence.stops:
            job = ctx.repo.get_job(stop.job_id)
            stops.append(
                {
                    "stop_number": stop.sequence_index,
                    "customer_name": job.customer_name if job else "(unknown)",
                    "postal_code": job.address.postal_code if job else None,
                    "arrival": f"{stop.arrival_window.start:%H:%M}",
                }
            )
        routes.append(
            {
                "date": day.isoformat(),
                "weekday": f"{day:%A}",
                "plan_id": plan.id,
                "version": plan.version,
                "stop_count": len(stops),
                "finishes_at": f"{_finish_time(plan)}",
                "stops": stops,
            }
        )

    total = sum(r["stop_count"] for r in routes)
    return ToolResult(
        ok=True,
        tool="get_existing_routes",
        summary=f"Loaded {len(routes)} published routes with {total} stops between them.",
        data={"routes": routes, "total_stops": total},
    )


def _finish_time(plan) -> str:
    minutes = plan.sequence.completion_minutes
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@tool("find_insertion_options", OrderArgs)
def find_insertion_options(args: OrderArgs, ctx: ToolContext) -> ToolResult:
    """Every safe place this customer could go on the published routes, ranked, best first.

    This is the tool that does the arithmetic. It compares the customer with every stop, keeps the
    ones within the anchor radius, tests before and after each, prices the detour, simulates the
    day to be sure nobody already booked becomes late, and returns the actual top three.

    The result is the answer, not a suggestion for the model to improve on. `create_offer` reads it
    from the scratch space rather than from anything the model retypes, so an option cannot be
    reworded, reordered or invented on the way to the customer.
    """
    order = ctx.repo.get_job(args.order_id)
    if order is None:
        return ToolResult(
            ok=False, tool="find_insertion_options", error="unknown_order",
            summary=f"No order {args.order_id}.",
        )
    if order.address.coordinates is None:
        return ToolResult(
            ok=False, tool="find_insertion_options", error="not_geocoded",
            summary=f"{order.customer_name}'s address has not been located yet.",
        )

    preferred = _preferred(order)
    found = insertion.search(
        ctx.repo,
        order,
        routing_client=ctx.routing_client,
        exclude=_declined(order, ctx),
        prefer=preferred,
        # "That's the only time I can do" turns the preference into a restriction. Offering a
        # different day then is not a helpful alternative -- it is not having listened, and the
        # policy says so (ALT-8).
        restrict_to=preferred if (preferred and conversation.is_only_option(order)) else None,
        on_phase=_reporter(order.id),
    )
    ctx.scratch["insertion"] = found

    data = found.to_data()
    if not found.options:
        # Two different failures, and telling them apart matters: one is "we cannot reach you",
        # the other is "we cannot reach you AT THE ONLY TIME YOU GAVE US". Reporting the second as
        # the first would have a coordinator looking for a routing problem that is not there.
        restricted = preferred and conversation.is_only_option(order)
        summary = (
            f"Compared {found.stops_checked} stops on {found.routes_checked} routes; nothing "
            f"within {data['anchor_radius_km']}km can take {order.customer_name} without making "
            f"someone else late."
        )
        if restricted:
            summary = (
                f"{order.customer_name} said their time is fixed, and no route passes within "
                f"{data['anchor_radius_km']}km of them in that window. Compared "
                f"{found.stops_checked} stops on {found.routes_checked} routes."
            )
        return ToolResult(
            ok=False,
            tool="find_insertion_options",
            error="fixed_time_unreachable" if restricted else "no_safe_position",
            summary=summary,
            data=data,
        )

    best = found.options[0]
    return ToolResult(
        ok=True,
        tool="find_insertion_options",
        summary=(
            f"Compared {found.stops_checked} stops on {found.routes_checked} routes, found "
            f"{found.anchors_within_radius} within {data['anchor_radius_km']}km, tested "
            f"{found.positions_tested} positions, and kept {len(found.options)}. Best: "
            f"{best.date:%A} {best.slot.label.lower()}, {best.added_distance_km}km extra."
        ),
        data=data,
    )


def _preferred(order) -> set:
    """(date, slot) pairs the customer actually asked for.

    Their stated availability, mapped onto the delivery windows it overlaps. Flagged on the
    options rather than used to filter: a customer who asks for a day we cannot serve should still
    be shown what we can do, and filtering here would leave them with nothing and no explanation.
    """
    from dispatch_agent.planning.slots import SLOTS

    wanted = set()
    for option in order.availability_options:
        for slot in SLOTS:
            # Any overlap counts. "Friday morning" is 09:00-13:00 to the parser and 10:00-14:00 to
            # the business, and a customer who says one plainly means the other.
            if option.window.start < slot.end and slot.start < option.window.end:
                wanted.add((option.date, slot.name))
    return wanted


def _declined(order, ctx: ToolContext) -> set:
    """(date, slot) pairs this customer has already turned down or ruled out.

    Read from what was persisted rather than from anything in this run's memory: a customer who
    declined a slot yesterday must not be offered it again today because a new run started with
    an empty scratch space.
    """
    from dispatch_agent.planning.slots import slot_containing

    declined = set()
    for option in order.availability_options:
        for window in option.excluded_windows:
            slot = slot_containing(window.start)
            if slot:
                declined.add((option.date, slot.name))
    for date_iso, slot_name in ctx.scratch.get("declined_slots", []):
        declined.add((date_iso, slot_name))
    return declined


class OfferArgs(_Args):
    order_id: str


def _offer(ctx: ToolContext, order_id: str, purpose, tool_name: str) -> ToolResult:
    """Shared body for the two offer tools.

    The options come from `ctx.scratch["insertion"]` -- what the search actually returned -- and
    never from the model's arguments. That is the structural half of "the LLM cannot reorder or
    replace the top three": there is no argument through which a different list could arrive.
    """
    from dispatch_agent.planning import offer_service

    order = ctx.repo.get_job(order_id)
    if order is None:
        return ToolResult(ok=False, tool=tool_name, error="unknown_order",
                          summary=f"No order {order_id}.")

    found = ctx.scratch.get("insertion")
    if found is None:
        return ToolResult(
            ok=False, tool=tool_name, error="nothing_searched",
            summary="Run find_insertion_options first -- there is nothing verified to offer.",
        )

    try:
        offer = offer_service.offer_insertions(
            ctx.repo, order, found.options, purpose=purpose, run_id=ctx.run_id
        )
    except offer_service.OfferError as exc:
        return ToolResult(ok=False, tool=tool_name, error=exc.kind, summary=str(exc))

    ctx.offer_id = offer.id
    ctx.scratch["offer_message"] = offer_service.offer_message(offer)
    slots = ", ".join(
        f"{offer_service.format_date(s.date)} {offer_service.format_window(s.window)}"
        for s in offer.options
    )
    return ToolResult(
        ok=True,
        tool=tool_name,
        summary=f"Offered {len(offer.options)}: {slots}.",
        data={
            "offer_id": offer.id,
            "purpose": offer.purpose.value,
            "slots": [
                {
                    "slot_id": s.id,
                    "date": s.date.isoformat(),
                    "window": {"start": f"{s.window.start:%H:%M}", "end": f"{s.window.end:%H:%M}"},
                    "reason": s.reason,
                    "added_distance_km": s.evidence.added_distance_km if s.evidence else None,
                    "added_minutes": s.evidence.added_minutes if s.evidence else None,
                    "anchor_stop_number": s.evidence.anchor_stop_number if s.evidence else None,
                    "insert_position": s.evidence.insert_position if s.evidence else None,
                    "source_plan_version": s.evidence.source_plan_version if s.evidence else None,
                }
                for s in offer.options
            ],
        },
    )


@tool("create_normal_offer", OfferArgs)
def create_normal_offer(args: OfferArgs, ctx: ToolContext) -> ToolResult:
    """One proven option on the customer's own cluster day.

    Proven, not assumed: belonging to Friday's region is not evidence that Friday can take you,
    so this offers the best position the search actually verified.
    """
    from dispatch_agent.models import OfferPurpose

    return _offer(ctx, args.order_id, OfferPurpose.BOOKING, "create_normal_offer")


@tool("create_alternative_offer", OfferArgs)
def create_alternative_offer(args: OfferArgs, ctx: ToolContext) -> ToolResult:
    """Exactly the three the search returned, in its order, after a normal offer was declined.

    Fewer than three is not a smaller version of this answer -- it is the case policy says to hand
    to a coordinator, and `offer_insertions` refuses rather than trimming.
    """
    from dispatch_agent.models import OfferPurpose

    return _offer(ctx, args.order_id, OfferPurpose.ALTERNATIVE, "create_alternative_offer")


def _reporter(order_id: str):
    """Turn the search's phase callbacks into live progress stages.

    Separate from the search itself so that module stays free of anything to do with a screen: it
    reports what it is doing, and this decides whether anyone is listening.
    """
    from dispatch_agent.agents import progress

    def report(key: str, label: str, reason: str, detail: str) -> None:
        progress.stage(order_id, key, label, reason, tool="find_insertion_options")
        if detail:
            progress.finish_stage(order_id, key, detail=detail)

    return report
