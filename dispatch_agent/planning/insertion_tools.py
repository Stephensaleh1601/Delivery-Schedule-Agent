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

from dispatch_agent.planning import insertion
from dispatch_agent.planning.clock import PlanningClock
from dispatch_agent.planning.tools import OrderArgs, ToolContext, ToolResult, _Args, tool

POLICY_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "delivery-policy.md"

# The topics a caller may ask about, mapped to the `## heading` they live under. A closed set on
# purpose: an unknown topic is a failed result the model can see and correct, not an empty answer
# it might read as "there is no rule about that".
POLICY_TOPICS = ("cluster_days", "delivery_windows", "alternatives", "driver_dispatch")

MAX_ALTERNATIVES = 3


class PolicyArgs(_Args):
    topic: str


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
    topic = args.topic.strip().lower()
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

    found = insertion.search(
        ctx.repo,
        order,
        routing_client=ctx.routing_client,
        exclude=_declined(order, ctx),
    )
    ctx.scratch["insertion"] = found

    data = found.to_data()
    if not found.options:
        return ToolResult(
            ok=False,
            tool="find_insertion_options",
            error="no_safe_position",
            summary=(
                f"Compared {found.stops_checked} stops on {found.routes_checked} routes; nothing "
                f"within {data['anchor_radius_km']}km can take {order.customer_name} without "
                f"making someone else late."
            ),
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
