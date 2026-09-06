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

from dispatch_agent.planning import clusters, conversation, insertion
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


class InsertionArgs(_Args):
    order_id: str
    # The WORKFLOW, not the dates. A model choosing "their normal day" is choosing a path; a
    # model choosing "2026-09-12" is doing arithmetic it has no business doing.
    scope: str = "cluster"


@tool("find_insertion_options", InsertionArgs)
def find_insertion_options(args: InsertionArgs, ctx: ToolContext) -> ToolResult:
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
    if clusters.placement_of(order).weekday is None:
        # Never fall through to both routes for an address we cannot place: that offers a day
        # their region does not belong to and presents it as their normal one.
        ctx.scratch["customer_message"] = (
            "Could you send me your 6-digit postal code? I need it to work out which of our "
            "delivery days covers you."
        )
        return ToolResult(
            ok=False, tool="find_insertion_options", error="unknown_location",
            summary="Cannot place this address in a delivery region; asked for the postal code.",
        )

    cycle = PlanningClock.coordination_cycle(repo=ctx.repo)
    if cycle is None:
        return ToolResult(
            ok=False, tool="find_insertion_options", error="no_coordination_cycle",
            summary="No published Friday and Saturday pair to search.",
        )

    # The model's `scope` is ignored. This path is reachable only by operational events now,
    # and which routes may be read is a business rule either way -- taking direction from an
    # argument was the thing that let it be got wrong.
    scope = _scope_for(order, ctx, cycle.dates)
    dates = clusters.resolve_scope(order, scope, cycle.dates)
    if scope == "requested" and not dates:
        return ToolResult(
            ok=False,
            tool="find_insertion_options",
            error="no_requested_day",
            summary=(
                "No requested day has been recorded for this customer yet. Call "
                "record_availability first, or use scope 'cluster' for their normal day."
            ),
        )
    placement = clusters.placement_of(order)
    ctx.scratch["insertion_scope"] = {
        "scope": scope,
        "region": placement.region,
        "normal_day": placement.day_name,
        "dates": [d.isoformat() for d in dates],
    }

    hard_filter = bool(
        preferred
        and (scope in {"cluster", "requested"} or conversation.is_only_option(order))
    )
    found = insertion.search(
        ctx.repo,
        order,
        dates=dates,
        routing_client=ctx.routing_client,
        exclude=_declined(order, ctx),
        prefer=preferred,
        # A normal/requested search must stay inside the time the customer gave us.  A fallback
        # after rejection may look wider, unless they explicitly said it was their only time.
        restrict_to=preferred if hard_filter else None,
        on_phase=_reporter(order.id, scope, placement.day_name),
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

    Their stated availability, mapped onto the delivery windows it overlaps.  Normal and
    explicitly requested searches use these pairs as a hard boundary.  A fallback search uses
    them only as preference evidence because the customer has asked for another option.
    """
    from dispatch_agent.planning.slots import SLOTS

    wanted = set()
    for option in order.availability_options:
        for slot in SLOTS:
            # Any overlap counts. A named part of the day now matches its published window
            # exactly, but "between 1 and 3" still straddles two, and both should qualify.
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


def _reporter(order_id: str, scope: str = "cluster", day_name: str = ""):
    """Turn the search's phase callbacks into live progress stages.

    Separate from the search itself so that module stays free of anything to do with a screen: it
    reports what it is doing, and this decides whether anyone is listening.
    """
    from dispatch_agent.agents import progress

    # The trace should say which routes are being looked at, because that is the difference
    # between the normal flow and the fallback and a judge should be able to see which one ran.
    where = (
        f"your normal {day_name} route"
        if scope == "cluster" and day_name
        else "the day you asked for"
        if scope == "requested"
        else "both published routes"
    )

    def report(key: str, label: str, reason: str, detail: str) -> None:
        if key == "nearby":
            label = f"Checking {where}"
        progress.stage(order_id, key, label, reason, tool="find_insertion_options")
        if detail:
            progress.finish_stage(order_id, key, detail=detail)

    return report


def _scope_for(order, ctx: ToolContext, cycle_dates) -> str:
    """Which routes this search may look at.

    The agent chooses the WORKFLOW by calling this tool; which routes that workflow is allowed to
    read is a business rule, and business rules are not judgement calls. Two facts decide it and
    both are already known:

    - A rejection is always a search of both routes. There is nowhere else for a declined
      customer to go, and a model that picked `cluster` here would re-offer the day they just
      turned down.
    - A day they explicitly named that is not their own is always a `requested` search. Answering
      about their normal day instead is the failure the rule exists to prevent.

    Anything else is a normal booking on their own day.
    """
    if ctx.scratch.get("intent") == "reject":
        return "both"

    named = clusters.requested_dates(order, cycle_dates)
    if named and any(clusters.is_off_cluster(order, day) for day in named):
        return "requested"

    return "cluster"


class PolicyQuestionArgs(_Args):
    # `order_id` is accepted and unused. Every other action takes one, so the model passes one
    # here too -- and with extra="forbid" that is a rejected call, not a tolerated field.
    order_id: str = ""
    question: str = ""


@tool("search_delivery_policy", PolicyQuestionArgs)
def search_delivery_policy(args: PolicyQuestionArgs, ctx: ToolContext) -> ToolResult:
    """The rules that answer a customer's question about how delivery works.

    Deliberately returns facts and not wording. `retrieve_policy` next to it takes a topic the
    agent already knows it needs; this takes the customer's question and finds the topic. Both
    return rule text with IDs, so an answer can be checked against the rule it claims to follow.

    Read-only, and the intent scope around it is what makes that structural: a run answering a
    question is never given a tool that could record availability, make an offer or move a route.
    """
    from dispatch_agent.planning import policy_kb

    question = (args.question or "").strip()
    if not question:
        return ToolResult(
            ok=False, tool="search_delivery_policy", error="no_question",
            summary="No question was given to look up.",
        )

    hits = policy_kb.search(question)
    if not hits:
        # A miss is a real finding, and the reply that follows must say so rather than reach for
        # the nearest rule. The flag is what lets `escalate_booking` say "I can't confirm that"
        # instead of its booking wording.
        ctx.scratch["policy_no_answer"] = question
        return ToolResult(
            ok=False, tool="search_delivery_policy", error="not_in_policy",
            summary=f"The delivery policy does not cover {question!r}.",
            data={"question": question, "policy_ids": [], "rules": []},
        )

    ctx.scratch["policy_hits"] = [r.to_dict() for r in hits]
    ids = [r.id for r in hits if r.id]
    # The IDs AND what they say. A trace line reading "Found WINDOW-1" makes a judge go and look
    # the rule up; naming it means the evidence and the reply can be compared on one screen.
    cited = ", ".join(f"{r.id} ({r.title})" if r.id else r.title for r in hits)
    return ToolResult(
        ok=True,
        tool="search_delivery_policy",
        summary=f"Answered from the policy: {cited}.",
        data={
            "question": question,
            "policy_ids": ids,
            "rules": [r.to_dict() for r in hits],
        },
    )


class PolicyAnswerArgs(_Args):
    order_id: str = ""
    answer: str = ""


@tool("answer_from_policy", PolicyAnswerArgs)
def answer_from_policy(args: PolicyAnswerArgs, ctx: ToolContext) -> ToolResult:
    """Put the model's own wording of a policy answer on the context, ready to send.

    This exists to make the policy path look like every other path. Everywhere else a tool
    prepares the customer's wording and `send_message` sends it; the policy path originally asked
    the model to break that habit and pass the text to `send_message` instead. It would not --
    nine identical `nothing_to_send` failures in one turn, every turn, because "the previous step
    prepares the wording" is the rule it had been taught everywhere else.

    So the wording is written into the argument the model is actually being asked for, and the
    step after it is the same `send_message` as always.

    The answer is the model's sentence, not a template: only it can turn two retrieved rules into
    a reply that addresses what was asked. What is NOT the model's is whether there were any
    rules -- this refuses unless a search succeeded first, so an answer can never be produced from
    nothing.
    """
    if "search_delivery_policy" not in ctx.succeeded:
        return ToolResult(
            ok=False, tool="answer_from_policy", error="nothing_retrieved",
            summary="Search the delivery policy first -- an answer must come from a rule.",
        )
    answer = (args.answer or "").strip()
    if not answer:
        return ToolResult(
            ok=False, tool="answer_from_policy", error="no_answer",
            summary="Pass the reply you want to send in `answer`.",
        )

    ctx.scratch["customer_message"] = answer
    cited = [r.get("id") for r in ctx.scratch.get("policy_hits", []) if r.get("id")]
    return ToolResult(
        ok=True,
        tool="answer_from_policy",
        summary=f"Replied using the knowledge base ({', '.join(cited) or 'no rule ids'}).",
        data={"policy_ids": cited, "answer": answer},
    )
