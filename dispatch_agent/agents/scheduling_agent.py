"""The scheduling agent: a bounded LangGraph loop that chooses approved tools.

    observe -> decide -> act -> decide -> ... -> finish

`observe` loads the business state once, for free. Every subsequent cycle is one model decision
followed by exactly one tool call, capped at MAX_TOOL_STEPS.

Two guards, deliberately: `step_count` is the real one, and produces a clean run log plus a
coordinator exception when it trips. LangGraph's own `recursion_limit` is a backstop for a
state-update bug that fails to increment the counter -- it must never be the normal path,
because it aborts the graph and discards the log.

What gets persisted is the decision and the typed tool result, never model reasoning. The
activity log a coordinator reads is the actual sequence of things that happened.
"""
from __future__ import annotations

from datetime import date as Date, datetime, timezone
from typing import Optional, Protocol, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field, field_validator

from dispatch_agent.agents.prompts import (
    SCHEDULING_DECISION_SYSTEM_PROMPT,
    action_decision_schema,
    render_state_digest,
)
from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.llm import LLMClient, build_llm_client
from dispatch_agent.config import settings
from dispatch_agent.models import (
    AgentActionLog,
    AgentRunLog,
    AgentRunStatus,
    MessageDirection,
    PlanningEvent,
    PlanningEventType,
)
from dispatch_agent.agents import progress
from dispatch_agent.planning import clusters, plan_service, tools
from dispatch_agent.planning.clock import PlanningClock

# The bound on one run. Raised from 6 when the conversation became natural-language: a single
# customer message can now legitimately need record_availability -> evaluate_slots ->
# suggest_route_aware_windows -> create_offer -> send_message -> finish, which is already six. The
# guard exists to stop a loop, not to make the longest honest path fail one step from the end.
MAX_TOOL_STEPS = 10


class ActionDecision(BaseModel):
    """One choice by the model.

    `action` is a plain string, NOT a Literal of the tool names. Constraining it here would make
    the "never execute an unknown action" guardrail unreachable -- Pydantic would reject the
    decision before dispatch ever saw it, turning a refusal we can observe and log into a parse
    error. The enum still goes to the model in the tool schema, where it steers generation.
    """

    action: str
    reason_summary: str = ""
    arguments: dict = Field(default_factory=dict)

    @field_validator("reason_summary")
    @classmethod
    def _short_and_clean(cls, value: str) -> str:
        # Collapsed and truncated so the persisted log stays a one-line operational note rather
        # than becoming a place model reasoning accumulates.
        return " ".join(str(value).split())[:240]


def _active_model_id() -> str:
    """The model actually configured, whichever provider is selected.

    This used to hardcode the Bedrock id, so a run driven by OpenAI reported a Claude model in the
    inspector -- a trace that names the wrong model is worse than one that names none.
    """
    if settings.llm_provider == "openai":
        return settings.openai_model
    return settings.bedrock_model_id


class DecisionAgent(Protocol):
    def decide(self, state: "SchedulingState", allowed: list[str]) -> ActionDecision: ...


class LLMDecisionAgent:
    def __init__(self, llm: LLMClient | None = None):
        # Built lazily. Constructing a Bedrock client validates credentials and an AWS profile,
        # and doing that in __init__ meant a misconfigured environment raised before the loop
        # could fall back -- turning a degraded run into a 500. Failure now happens inside
        # decide(), where it is caught and handled.
        self._llm = llm

    def _client(self) -> LLMClient:
        if self._llm is None:
            self._llm = build_llm_client()
        return self._llm

    def decide(self, state: "SchedulingState", allowed: list[str]) -> ActionDecision:
        raw = self._client().extract_structured(
            system=SCHEDULING_DECISION_SYSTEM_PROMPT,
            user=render_state_digest(state),
            tool_name="choose_next_action",
            tool_schema=action_decision_schema(allowed),
        )
        return ActionDecision.model_validate(raw)


class ScriptedDecisionAgent:
    """A fixed sequence of decisions, for tests. Returns `finish` once exhausted so a test that
    under-specifies cannot hang."""

    def __init__(self, decisions):
        self._queue = [
            d if isinstance(d, ActionDecision) else ActionDecision(**d) for d in decisions
        ]

    def decide(self, state, allowed) -> ActionDecision:
        if not self._queue:
            return ActionDecision(action="finish", reason_summary="nothing further to do")
        return self._queue.pop(0)


def _last_failed(state, tool_name: str) -> bool:
    """Whether the most recent attempt at `tool_name` came back failed."""
    attempts = [a for a in state.get("actions", []) if a.tool == tool_name]
    return bool(attempts) and not attempts[-1].ok


def _last_error(state, tool_name: str) -> str | None:
    attempts = [a for a in state.get("actions", []) if a.tool == tool_name]
    return attempts[-1].error if attempts else None



# The conversational path, step by step. Both customer intents walk it; only the first action and
# the offer at the end differ. It lives here rather than being inlined twice so that the standard
# procedure and the model cannot drift into following different policies -- a fallback that
# quietly does something else is worse than no fallback, because the log still says "completed".
def _insertion_sequence(event, done, state, topic: str, offer_action: str, scope: str = "cluster"):
    """The conversational path as one composite action, then the reply.

    Both customer intents walk it; only which search runs differs. Written once so the standard
    procedure and the model follow the same policy -- a fallback that quietly does something else
    is worse than no fallback, because the log still says "completed".
    """
    search = {
        "cluster": "find_normal_slot",
        "requested": "find_requested_day_slot",
        "both": "find_fallback_options",
    }[scope]

    if search not in done:
        return ActionDecision(
            action=search,
            reason_summary={
                "cluster": "Checking their normal delivery day.",
                "requested": "Checking the day they asked for.",
                "both": "Searching both published routes.",
            }[scope],
            arguments={"order_id": event.order_id},
        )

    if _last_failed(state, search) and "escalate_booking" not in done:
        return ActionDecision(
            action="escalate_booking",
            reason_summary="Nothing safe to offer; asking a coordinator to call.",
            arguments={"order_id": event.order_id, "reason": _last_error(state, search) or ""},
        )

    if "send_message" not in done:
        return ActionDecision(
            action="send_message",
            reason_summary="Sending the reply.",
            arguments={"order_id": event.order_id},
        )
    return ActionDecision(action="finish", reason_summary="Replied; waiting for the customer.")



def _policy_sentences(hits: list[dict]) -> list[str]:
    """A readable answer built from retrieved rules, for when the model is unavailable.

    Plainer than the model's wording, and that is the trade a fallback makes. What it must not do
    is say anything the rules do not: every sentence here is lifted from the rule text, and a
    Markdown table becomes the same pairs in prose rather than being dropped -- the windows and the
    region-to-day mapping are both tables, and they are the two most likely questions.
    """
    lines: list[str] = []
    for hit in hits[:2]:
        text = (hit.get("text") or "").split("*Enforced")[0]
        prose: list[str] = []
        pairs: list[str] = []
        for raw in text.splitlines():
            row = raw.strip()
            if not row:
                continue
            if row.startswith("|"):
                cells = [c.strip() for c in row.strip("|").split("|")]
                # Skip the header rule (`|---|---|`) and the header row itself.
                if all(set(c) <= set("-: ") for c in cells):
                    continue
                if len(cells) == 2 and cells[0] and cells[1]:
                    pairs.append(f"{cells[0]} {cells[1]}")
                continue
            prose.append(row)
        if pairs:
            # The first pair is the table's own header ("Window / Promised arrival"), which reads
            # as a label rather than a fact.
            lines.append("; ".join(pairs[1:] if len(pairs) > 1 else pairs) + ".")
        joined = " ".join(prose).strip()
        if joined:
            first = joined.split(". ")[0].strip().rstrip(".")
            if first:
                lines.append(first + ".")
    return lines or ["I could not find a written rule covering that."]


class RuleDecisionAgent:
    """A deterministic policy over the same state the model sees.

    Exists so a demo survives the LLM being unavailable: the flow still runs, and the run log
    says plainly that it fell back rather than pretending a model made the calls.
    """

    def decide(self, state, allowed) -> ActionDecision:
        event = state["event"]
        done = {a.tool for a in state.get("actions", [])}

        conversational = bool(event.payload.get("intent"))

        # A question about how delivery works. Handled before anything else because it is not a
        # booking at all: search the policy, then say what it found. The wording here is plainer
        # than the model's would be -- that is what a fallback is -- but it is built from the same
        # retrieved rules, so it can never claim something the policy does not say.
        if event.payload.get("intent") == "policy_question":
            if "search_delivery_policy" not in done:
                return ActionDecision(
                    action="search_delivery_policy",
                    reason_summary="Looking the question up in the delivery policy.",
                    arguments={
                        "order_id": event.order_id,
                        "question": event.payload.get("question")
                        or event.payload.get("note")
                        or "",
                    },
                )
            hits = state.get("policy_hits") or []
            if hits and "answer_from_policy" not in done:
                return ActionDecision(
                    action="answer_from_policy",
                    reason_summary="Answering from the retrieved policy rules.",
                    arguments={
                        "order_id": event.order_id,
                        "answer": " ".join(_policy_sentences(hits)),
                    },
                )
            if "send_message" not in done:
                if hits:
                    return ActionDecision(
                        action="send_message",
                        reason_summary="Sending the answer.",
                        arguments={"order_id": event.order_id},
                    )
                return ActionDecision(
                    action="escalate_booking",
                    reason_summary="The policy does not cover this question.",
                    arguments={
                        "order_id": event.order_id,
                        "reason": "Question not answered by the delivery policy.",
                    },
                )
            return ActionDecision(action="finish", reason_summary="The question has been answered.")

        if event.event_type is PlanningEventType.NEW_ORDER:
            # A conversational turn carries the windows the customer just stated. Writing them down
            # is the first thing that happens, so the evaluation below prices what they actually
            # said rather than whatever the order happened to hold before this message.
            stated = event.payload.get("stated_windows")
            if stated and "record_availability" not in done:
                return ActionDecision(
                    action="record_availability",
                    reason_summary="Noting the times the customer gave.",
                    # The windows are not passed here: the tool reads them off the context, where
                    # the deterministic parser put them. Nothing that writes a customer's stated
                    # availability accepts it as an argument.
                    arguments={"order_id": event.order_id},
                )
            if stated and _last_failed(state, "record_availability"):
                # Nothing usable was recorded -- an out-of-horizon date, or a window we could not
                # read. The tool has already put the explanation in `customer_message`; send that
                # and stop, rather than evaluating an order with nothing on it.
                if "send_message" not in done and state.get("customer_message"):
                    return ActionDecision(
                        action="send_message",
                        reason_summary="Explaining why that date cannot be booked.",
                        arguments={"order_id": event.order_id, "body": state["customer_message"]},
                    )
                return ActionDecision(action="finish",
                                      reason_summary="Waiting for a date we can actually book.")
            if conversational:
                # The standard procedure always takes the customer's own day. Distinguishing
                # an off-cluster request is the model's job on the live path; a fallback that has
                # to make that call as well is a fallback with its own failure modes.
                return _insertion_sequence(
                    event, done, state, topic="cluster_days",
                    offer_action="find_normal_slot", scope="cluster",
                )

            if "evaluate_slots" not in done:
                return ActionDecision(action="evaluate_slots",
                                      reason_summary="Checking which of the requested windows we can serve.",
                                      arguments={"order_id": event.order_id})
            # Look for a route-friendly alternative before offering. This is what lets the reply be
            # "Saturday works, but Tuesday we'll already be in your area" rather than a flat yes --
            # and whether any of it is put to the customer is decided by the counteroffer policy in
            # create_offer, not here. Skipped when they have said their timing is fixed: there is
            # nothing to ask, and solving the horizon to ask it anyway is waste.
            if (
                stated
                and not event.payload.get("is_fixed")
                and "suggest_route_aware_windows" not in done
            ):
                return ActionDecision(
                    action="suggest_route_aware_windows",
                    reason_summary="Checking whether another day suits the route better.",
                    arguments={"order_id": event.order_id},
                )
            if "create_offer" not in done:
                return ActionDecision(action="create_offer",
                                      reason_summary="Offering the workable slots to the customer.",
                                      arguments={"order_id": event.order_id})
            if _last_failed(state, "create_offer") and "create_exception" not in done:
                # There is nothing to send. Without this the next branch would message the
                # customer the literal fallback string below -- "we have some options for you"
                # to someone who is being offered none.
                return ActionDecision(
                    action="create_exception",
                    reason_summary="No workable window; asking a coordinator to call the customer.",
                    arguments={
                        "order_id": event.order_id,
                        "kind": "no_feasible_slot",
                        "message": "None of the windows this customer gave us can be served; needs a call.",
                    },
                )
            if _last_failed(state, "create_offer") and "send_message" not in done:
                # Escalating is not an answer to the person waiting. Handing the order to a
                # coordinator and saying nothing leaves them staring at a thread that stopped
                # replying -- which is how the round cap looked in a live run.
                round_cap = _last_error(state, "create_offer") == "round_cap_reached"
                return ActionDecision(
                    action="send_message",
                    reason_summary=(
                        "Asking for a concrete counter-proposal after the automatic options."
                        if round_cap
                        else "Telling the customer a colleague will take it from here."
                    ),
                    arguments={
                        "order_id": event.order_id,
                        "body": (
                            "I've shown the best automatic options I found. If you have another "
                            "day or time in mind, tell me and I'll check that exact time; otherwise "
                            "one of our team can help."
                            if round_cap
                            else "Sorry — I can't fit those times in. One of our team will call "
                                 "you shortly to sort out a slot that works."
                        ),
                    },
                )
            if "send_message" not in done and state.get("customer_message"):
                return ActionDecision(action="send_message",
                                      reason_summary="Sending the options to the customer.",
                                      arguments={"order_id": event.order_id,
                                                 "body": state["customer_message"]})
            return ActionDecision(action="finish", reason_summary="Offer sent; waiting on the customer.")

        if event.event_type is PlanningEventType.CUSTOMER_ACCEPTED_OFFER:
            if "lock_appointment" not in done:
                return ActionDecision(action="lock_appointment",
                                      reason_summary="Locking the slot the customer chose.",
                                      arguments={"offer_id": event.payload.get("offer_id"),
                                                 "slot_id": event.payload.get("slot_id")})
            return ActionDecision(action="finish", reason_summary="Appointment confirmed.")

        if event.event_type is PlanningEventType.CUSTOMER_REJECTED_OFFER:
            if "record_rejection" not in done:
                return ActionDecision(
                    action="record_rejection",
                    reason_summary="Excluding the time they turned down, not the whole day.",
                    arguments={"offer_id": event.payload.get("offer_id"),
                               "slot_id": event.payload.get("slot_id")},
                )
            if conversational:
                return _insertion_sequence(
                    event, done, state, topic="alternatives",
                    offer_action="find_fallback_options", scope="both",
                )

            if "evaluate_slots" not in done:
                return ActionDecision(
                    action="evaluate_slots",
                    reason_summary="Re-solving their dates around the excluded time.",
                    arguments={"order_id": event.order_id},
                )
            # Then look further afield. The rule is "re-solve the same day once, THEN consider
            # another" -- and the same-day re-solve often fails outright, because what is left of
            # the day after carving out the rejected window may not hold the job. Without this the
            # conversation ended at a coordinator exception the moment a customer said "not that
            # time" about a busy morning, which is the most ordinary thing a customer can say.
            if "suggest_route_aware_windows" not in done:
                return ActionDecision(
                    action="suggest_route_aware_windows",
                    reason_summary="Looking for another day that suits the route.",
                    arguments={"order_id": event.order_id},
                )
            if "create_offer" not in done:
                return ActionDecision(action="create_offer",
                                      reason_summary="Offering the next best slot.",
                                      arguments={"order_id": event.order_id})
            if _last_failed(state, "create_offer") and "create_exception" not in done:
                # Nothing left we can offer -- every window the customer gave us has now been
                # tried. Ending here would silently abandon the order, so hand it to a human.
                round_cap = _last_error(state, "create_offer") == "round_cap_reached"
                return ActionDecision(
                    action="create_exception",
                    reason_summary=(
                        "Automatic offer limit reached; keeping a coordinator available."
                        if round_cap
                        else "No remaining window works; asking a coordinator to call the customer."
                    ),
                    arguments={
                        "order_id": event.order_id,
                        "kind": "offer_round_cap" if round_cap else "no_remaining_slot",
                        "message": (
                            "Automatic options exhausted; customer may still give a concrete time."
                            if round_cap
                            else "Customer declined every slot we could offer; needs a call to agree a new time."
                        ),
                    },
                )
            if _last_failed(state, "create_offer") and "send_message" not in done:
                # The same silence as the new-order branch: escalating without telling them leaves
                # the customer watching a thread that simply stopped answering.
                round_cap = _last_error(state, "create_offer") == "round_cap_reached"
                return ActionDecision(
                    action="send_message",
                    reason_summary=(
                        "Asking for a concrete counter-proposal after the automatic options."
                        if round_cap
                        else "Telling the customer a colleague will take it from here."
                    ),
                    arguments={
                        "order_id": event.order_id,
                        "body": (
                            "I've shown the best automatic options I found. If you have another "
                            "day or time in mind, tell me and I'll check that exact time; otherwise "
                            "one of our team can help."
                            if round_cap
                            else "Sorry — I can't fit those times in. One of our team will call "
                                 "you shortly to sort out a slot that works."
                        ),
                    },
                )
            # Actually send it. Until the conversation was read back from the database this branch
            # got away with finishing here, because the frontend rendered the offer straight from
            # the response -- so the second round existed for the customer on screen and nowhere in
            # the thread, and vanished on refresh.
            if "send_message" not in done and state.get("customer_message"):
                return ActionDecision(
                    action="send_message",
                    reason_summary="Sending the new time to the customer.",
                    arguments={"order_id": event.order_id, "body": state["customer_message"]},
                )
            return ActionDecision(action="finish", reason_summary="Second offer sent.")

        if event.event_type is PlanningEventType.ORDER_READINESS_CHANGED:
            date_str = event.affected_date.isoformat() if event.affected_date else None
            if "replan_day" not in done:
                return ActionDecision(action="replan_day",
                                      reason_summary="Rebuilding the day without the delayed order.",
                                      arguments={"delivery_date": date_str,
                                                 "reason": "an order became unavailable"})
            if "find_ready_replacements" not in done:
                return ActionDecision(action="find_ready_replacements",
                                      reason_summary="Looking for a customer who would take the freed slot.",
                                      arguments={"delivery_date": date_str})
            return ActionDecision(action="finish", reason_summary="Recovery options identified.")

        if event.event_type is PlanningEventType.MANUAL_RETRY:
            intent = event.payload.get("intent")

            if intent == "explain":
                # The figures must come from a solve, so re-evaluate before answering. Explaining
                # from memory is how an agent ends up confidently quoting a route it no longer has.
                if "evaluate_slots" not in done:
                    return ActionDecision(
                        action="evaluate_slots",
                        reason_summary="Re-checking the route so the answer is the current one.",
                        arguments={"order_id": event.order_id},
                    )
                # The alternative is usually the thing being asked about -- "why Tuesday?" is a
                # question about a day we proposed, not about the day they requested.
                if "suggest_route_aware_windows" not in done:
                    return ActionDecision(
                        action="suggest_route_aware_windows",
                        reason_summary="Re-checking the alternative so the comparison is current.",
                        arguments={"order_id": event.order_id},
                    )
                if "explain_choice" not in done:
                    return ActionDecision(
                        action="explain_choice",
                        reason_summary="Answering from the solved route.",
                        arguments={"order_id": event.order_id,
                                   "question": event.payload.get("message", "")},
                    )
                if _last_failed(state, "explain_choice") and "ask_clarification" not in done:
                    return ActionDecision(
                        action="ask_clarification",
                        reason_summary="Nothing solved to explain; asking what they need.",
                        arguments={
                            "order_id": event.order_id,
                            "question": "Sorry -- which delivery time would you like me to explain?",
                        },
                    )
            elif intent == "general_support":
                # Not a scheduling question. Answer the one we can ("what's the new postal code?")
                # and hand the rest to a person -- but never touch the booking, which the intent
                # allow-list also enforces from the other side.
                if "ask_clarification" not in done:
                    return ActionDecision(
                        action="ask_clarification",
                        reason_summary=f"Customer asked about {event.payload.get('topic') or 'something else'}.",
                        arguments={
                            "order_id": event.order_id,
                            "question": event.payload.get("question")
                            or "A colleague will call you back about that.",
                        },
                    )
                if "create_exception" not in done:
                    return ActionDecision(
                        action="create_exception",
                        reason_summary="Passing a non-scheduling request to a coordinator.",
                        arguments={
                            "order_id": event.order_id,
                            "kind": f"customer_{event.payload.get('topic') or 'request'}",
                            "message": (
                                f"Customer asked about "
                                f"{event.payload.get('topic') or 'something outside scheduling'}: "
                                f"{event.payload.get('message', '')[:160]}"
                            ),
                        },
                    )
            elif "ask_clarification" not in done:
                # Unclear or unrelated. One question, never a guess: a wrong date costs the
                # customer a delivery day, and there is no way for them to see it coming.
                return ActionDecision(
                    action="ask_clarification",
                    reason_summary="Message unclear; asking one question rather than guessing.",
                    arguments={
                        "order_id": event.order_id,
                        "question": event.payload.get("question")
                        or "Sorry, I didn't catch that -- which day and roughly what time would suit you?",
                    },
                )

            if "send_message" not in done and state.get("customer_message"):
                return ActionDecision(
                    action="send_message",
                    reason_summary="Replying to the customer.",
                    arguments={"order_id": event.order_id, "body": state["customer_message"]},
                )
            return ActionDecision(action="finish", reason_summary="Replied.")

        return ActionDecision(action="finish", reason_summary="No action defined for this event.")


class SchedulingState(TypedDict, total=False):
    event: PlanningEvent
    order_id: Optional[str]
    affected_date: Optional[Date]
    horizon_start: Date
    horizon_end: Date
    order_summary: Optional[dict]
    actions: list[AgentActionLog]
    pending_decision: Optional[object]
    last_tool_result: Optional[dict]
    customer_message: Optional[str]
    # The policy rules the last search returned. Carried on the state, like customer_message, so
    # the standard-procedure decider can answer from them when the model is unavailable.
    policy_hits: Optional[list]
    step_count: int
    completed: bool
    error: Optional[str]
    # Set when a decision fell back to the standard procedure, so the run can say why.
    decider_error: Optional[str]
    # Who produced the decision currently pending, carried from decide to act so the step it
    # becomes records its own provider rather than the run's.
    step_provenance: Optional[dict]


def _observe_node(ctx: tools.ToolContext):
    """Load the business context. Not a tool step: fetching an order and the horizon is
    bookkeeping, and charging it against a six-step budget would leave no room to act."""

    def node(state: SchedulingState) -> SchedulingState:
        event = state["event"]
        first, last = PlanningClock.horizon()
        update: SchedulingState = {
            "horizon_start": first,
            "horizon_end": last,
            "order_id": event.order_id,
            "affected_date": event.affected_date,
            "actions": [],
            "step_count": 0,
        }
        if event.order_id:
            order = ctx.repo.get_job(event.order_id)
            if order is None:
                update["error"] = f"unknown order {event.order_id}"
            else:
                ctx.order = order
                # Which delivery day is theirs, so choosing a search scope is a reading task for
                # the model rather than a geography one.
                placement = clusters.placement_of(order)
                update["placement"] = {
                    "region": placement.region or "unknown",
                    "normal_day": placement.day_name,
                }
                update["order_summary"] = {
                    "customer_name": order.customer_name,
                    "job_type": order.job_type.value,
                    "duration_minutes": order.duration_minutes,
                    "planning_status": order.planning_status.value,
                    "options": [
                        {
                            "date": o.date.isoformat(),
                            "start": o.window.start.strftime("%H:%M"),
                            "end": o.window.end.strftime("%H:%M"),
                            "preference_rank": o.preference_rank,
                        }
                        for o in order.availability_options
                    ],
                }
        return update

    return node


def _decide_node(ctx: tools.ToolContext, decider: DecisionAgent, fallback: DecisionAgent | None):
    def node(state: SchedulingState) -> SchedulingState:
        primary = type(decider).__name__
        model_id = _active_model_id() if isinstance(decider, LLMDecisionAgent) else None

        # What is permissible right now, not the whole registry. The model is shown exactly
        # what dispatch will accept, so an illegal action is not something the prompt has to
        # argue it out of.
        legal = tools.legal_actions(state, ctx)
        # Carried to the act node, which is where dispatch reads it. Set the moment the set exists
        # and before any early return, so the set the model is offered and the set enforced
        # against its answer are always the same one.
        ctx.legal_now = frozenset(legal)

        # The model's turn is where most of the wall clock goes -- several round trips at a second
        # or so each. Leaving it unlabelled made the panel show tool steps of 0.03s adding up to
        # eleven seconds, which reads as the screen lying rather than the model thinking.
        if ctx.order is not None and legal != ["finish"]:
            progress.stage(
                ctx.order.id,
                f"decide-{state.get('step_count', 0)}",
                "Deciding what to do next",
                f"Choosing from {len(legal)} permitted actions",
                tool="(model)",
            )

        if legal == ["finish"]:
            # There was no choice to make. Asking a model to pick from a list of one and then
            # recording it as a decision would put a step in the activity log that claims a
            # judgement nobody exercised.
            return {
                "pending_decision": ActionDecision(
                    action="finish",
                    reason_summary="Nothing further is permitted in this run.",
                    arguments={},
                ),
                "step_provenance": {
                    "decider": "controller",
                    "model_id": None,
                    "fallback_reason": None,
                },
            }

        if legal == ["search_delivery_policy"]:
            # The customer already supplied the question. Asking the model to copy it into the
            # only available tool call adds no judgement and can lose the text entirely. That
            # happened for "So you can only do Saturday?": the search received only order_id,
            # found no query, and the customer was wrongly escalated. Preserve their exact words.
            event = state["event"]
            return {
                "pending_decision": ActionDecision(
                    action="search_delivery_policy",
                    reason_summary="Looking up the customer's question in the delivery policy.",
                    arguments={
                        "order_id": event.order_id,
                        "question": event.payload.get("question")
                        or event.payload.get("message")
                        or "",
                    },
                ),
                "step_provenance": {
                    "decider": "controller",
                    "model_id": None,
                    "fallback_reason": None,
                },
            }

        if legal == ["send_message"]:
            # Once a tool has prepared the exact customer-facing wording, there is no judgement
            # left to make and no arguments for the model to invent.  A live policy answer added
            # an ``answer_from_policy`` field to this call, failed validation, then retried.  Send
            # the prepared reply directly and keep the trace free of a meaningless failed step.
            return {
                "pending_decision": ActionDecision(
                    action="send_message",
                    reason_summary="Sending the prepared reply.",
                    arguments={"order_id": ctx.order.id} if ctx.order else {},
                ),
                "step_provenance": {
                    "decider": "controller",
                    "model_id": None,
                    "fallback_reason": None,
                },
            }

        try:
            decision = decider.decide(state, legal)
        except Exception as exc:  # noqa: BLE001
            if fallback is None:
                return {
                    "error": f"could not decide what to do next: "
                             f"{tools.redact_secrets(str(exc))}",
                    "completed": True,
                }
            # Recorded, not hidden: the log should say the model was unavailable rather than
            # implying it made these calls. Redacted because a provider exception quotes the
            # request it failed on, and this string is persisted and then rendered.
            decision = fallback.decide(state, legal)
            decision.reason_summary = f"[model unavailable, using standard procedure] {decision.reason_summary}"
            reason = tools.redact_secrets(f"{type(exc).__name__}: {exc}")
            return {
                "pending_decision": decision,
                "decider_error": reason,
                # Per STEP, because the fallback happens per decision -- a run can be part-model,
                # part-standard-procedure, and one run-level flag would misdescribe half of it.
                "step_provenance": {
                    "decider": type(fallback).__name__,
                    "model_id": None,
                    "fallback_reason": reason,
                },
            }
        # The gate has to bind the decision, not merely inform it. `dispatch()` enforces the
        # INTENT scope, which is wider, and `finish` never reaches dispatch at all -- it
        # short-circuits the graph. So a model that answered "finish" could end a turn the gate
        # had already ruled out, which is how a run asked the customer a question, logged that it
        # asked, and stopped without sending it.
        if decision.action not in legal:
            forced = None
            if len(legal) == 1:
                forced = legal[0]
            elif "send_message" in legal and (
                ctx.scratch.get("offer_message") or ctx.scratch.get("customer_message")
            ):
                # The outcome already exists. Whatever it reached for, the useful move now is to
                # tell the customer -- and letting the illegal call through just adds a refused
                # step they sit and wait through.
                forced = "send_message"
            elif decision.action == "finish":
                # Ending is the one illegal choice with no other guard behind it.
                forced = "send_message" if "send_message" in legal else legal[0]

            if forced is not None:
                forced_arguments = {"order_id": ctx.order.id} if ctx.order else {}
                if forced == "answer_from_policy":
                    # A one-action gate normally still asks the model for the wording.  If it
                    # nevertheless returns a different action, fall back to a truthful answer
                    # built only from the retrieved rules instead of calling the answer tool with
                    # an empty string and looping again.
                    forced_arguments["answer"] = " ".join(
                        _policy_sentences(state.get("policy_hits") or [])
                    )
                return {
                    "pending_decision": ActionDecision(
                        action=forced,
                        reason_summary=(
                            f"{forced.replace('_', ' ').capitalize()} is the only step still "
                            f"permitted here."
                        ),
                        arguments=forced_arguments,
                    ),
                    "step_provenance": {
                        "decider": "controller",
                        "model_id": None,
                        "fallback_reason": None,
                    },
                }

        return {
            "pending_decision": decision,
            "step_provenance": {"decider": primary, "model_id": model_id, "fallback_reason": None},
        }

    return node


def _act_node(ctx: tools.ToolContext):
    def node(state: SchedulingState) -> SchedulingState:
        decision: ActionDecision = state["pending_decision"]
        # Reported before the call, so the screen names the tool that is running rather than
        # the one that just finished.
        label, why = progress.TOOL_STAGES.get(decision.action, (decision.action, ""))
        if ctx.order is not None and decision.action != "finish":
            progress.stage(ctx.order.id, decision.action, label, why, tool=decision.action)
        step = state.get("step_count", 0) + 1

        result = tools.dispatch(decision.action, decision.arguments, ctx)
        if ctx.order is not None and decision.action != "finish":
            # The tool's own summary is the detail -- it is already written for a coordinator and
            # already carries the real figures, so the panel quotes it rather than paraphrasing.
            progress.finish_stage(
                ctx.order.id, decision.action, detail=result.summary, ok=result.ok
            )
        entry = AgentActionLog(
            step=step,
            tool=decision.action,
            ok=result.ok,
            # Sanitised here, on the way into the log, so nothing personal or cross-customer is
            # ever persisted -- not merely hidden at render time.
            arguments=tools.capped_for_log(decision.arguments or {}),
            summary=result.summary,
            reason_summary=decision.reason_summary,
            error=result.error,
            data=tools.capped_for_log(result.data),
            **(state.get("step_provenance") or {}),
        )
        update: SchedulingState = {
            "actions": state.get("actions", []) + [entry],
            "step_count": step,
            "last_tool_result": result.model_dump(),
        }
        # Two tools write something to say: `create_offer` produces the list of times, and the
        # conversational tools (ask_clarification, explain_choice, an out-of-horizon
        # record_availability) produce a sentence. The offer wins where both exist -- a customer
        # being given times does not also need the question that preceded them.
        reply = ctx.scratch.get("offer_message") or ctx.scratch.get("customer_message")
        if reply:
            update["customer_message"] = reply
        if ctx.scratch.get("policy_hits"):
            update["policy_hits"] = ctx.scratch["policy_hits"]
        if decision.action == "finish":
            update["completed"] = True
        return update

    return node


def _after_decide(state: SchedulingState) -> str:
    decision = state.get("pending_decision")
    if state.get("error") or decision is None:
        return "finish"
    if state.get("step_count", 0) >= MAX_TOOL_STEPS:
        return "finish"
    return "act"


def _after_act(state: SchedulingState) -> str:
    if state.get("completed") or state.get("step_count", 0) >= MAX_TOOL_STEPS:
        return "finish"
    return "decide"


def build_graph(ctx: tools.ToolContext, decider: DecisionAgent, fallback: DecisionAgent | None):
    graph = StateGraph(SchedulingState)
    graph.add_node("observe", _observe_node(ctx))
    graph.add_node("decide", _decide_node(ctx, decider, fallback))
    graph.add_node("act", _act_node(ctx))
    graph.set_entry_point("observe")
    graph.add_edge("observe", "decide")
    graph.add_conditional_edges("decide", _after_decide, {"act": "act", "finish": END})
    graph.add_conditional_edges("act", _after_act, {"decide": "decide", "finish": END})
    return graph.compile()


def handle_planning_event(
    event: PlanningEvent,
    repo: JobsRepository | None = None,
    decider: DecisionAgent | None = None,
    routing_client: RoutingClient | None = None,
    use_fallback: bool = True,
    ctx: tools.ToolContext | None = None,
) -> AgentRunLog:
    """Run the agent for one event and return its persisted log.

    Every trigger enters here: a new order, a customer's reply, a readiness change. A run is
    recorded before it starts and updated after each step, so the activity panel can be polled
    while it is still going rather than only seeing a finished result.

    Pass `ctx` when the caller needs what the run *computed*, not only what it logged. The tools
    fill in `ctx.evaluations` and `ctx.offer_id`, and those are the only reliable way to return
    the offer this run actually made -- guessing at the newest row in the table is how a caller
    ends up showing the customer slots from a different offer than the one in the log.

    A replayed event short-circuits below without running the graph, so an injected context stays
    empty. Callers must read that as "already handled", not as "no candidates".
    """
    repo = repo or (ctx.repo if ctx is not None else JobsRepository())

    if not repo.save_planning_event(event):
        # This event id has been handled already -- replaying it must not redo the work.
        existing = repo.agent_run_for_event(event.id)
        if existing is not None:
            return existing

    run = AgentRunLog(
        event_id=event.id, event_type=event.event_type, order_id=event.order_id,
        status=AgentRunStatus.RUNNING,
    )
    repo.save_agent_run(run)

    ctx = ctx if ctx is not None else tools.ToolContext(repo=repo, routing_client=routing_client)
    # The run row is created just above, so the context can carry its id from the first tool call
    # -- which is what lets offers and messages record their own provenance as they are written.
    ctx.run_id = run.id
    # The windows the customer stated, already resolved. Put on the context rather than passed as
    # tool arguments so no decider -- model or rule -- can alter them between reading the message
    # and recording it.
    if event.payload.get("stated_windows"):
        ctx.scratch["stated_windows"] = event.payload["stated_windows"]
    if event.payload.get("is_fixed"):
        ctx.scratch["timing_is_fixed"] = True
    # The one path by which an appointment may be locked. Set only for an acceptance event, so a
    # decider cannot confirm a booking the customer has not answered -- a live model did exactly
    # that, successfully, while its own question was still on the table.
    if event.event_type is PlanningEventType.CUSTOMER_ACCEPTED_OFFER:
        offer_id, slot_id = event.payload.get("offer_id"), event.payload.get("slot_id")
        if offer_id and slot_id:
            ctx.accepted = (offer_id, slot_id)
    # Scope the run to what answering THIS message may do. Operational events -- a readiness delay,
    # the morning run -- carry no intent and keep the whole registry, because they are not a reply
    # to anybody.
    intent = event.payload.get("intent")
    if intent in tools.INTENT_TOOLS:
        ctx.allowed_tools = tools.INTENT_TOOLS[intent]
    if intent:
        # The tools need it too: which routes a search may look at is decided by the intent, not
        # by judgement, and a model that picks the scope differently on two runs of the same
        # conversation makes the demo unrepeatable.
        ctx.scratch["intent"] = intent

    if decider is None:
        try:
            decider = LLMDecisionAgent()
        except Exception:  # noqa: BLE001 -- a missing provider must degrade, not 500
            decider = RuleDecisionAgent()
    run.decider = type(decider).__name__
    run.model_id = _active_model_id() if isinstance(decider, LLMDecisionAgent) else None
    fallback = RuleDecisionAgent() if use_fallback else None
    graph = build_graph(ctx, decider, fallback)

    try:
        final = graph.invoke(
            {"event": event},
            # A second, independent bound. Should never be what stops the loop.
            config={"recursion_limit": 2 * MAX_TOOL_STEPS + 6},
        )
    except Exception as exc:  # noqa: BLE001
        run.status = AgentRunStatus.FAILED
        run.final_summary = f"The scheduling agent could not complete: {exc}"
        # Before the save, on EVERY exit. A crash is the case where the customer is most likely
        # to be left with silence, and silence is indistinguishable from a broken server.
        _guarantee_a_reply(repo, event, ctx, run)
        run.completed_at = datetime.now(timezone.utc)
        repo.save_agent_run(run)
        return run

    # A run can be part-model, part-standard-procedure: the fallback happens per decision, not per
    # run. Recording the error is what makes that visible rather than a guess from a prefix.
    if final.get("decider_error"):
        run.decider_error = final["decider_error"]
    run.actions = final.get("actions", [])
    run.completed_at = datetime.now(timezone.utc)

    if final.get("error"):
        run.status = AgentRunStatus.FAILED
        run.final_summary = final["error"]
    elif final.get("step_count", 0) >= MAX_TOOL_STEPS and not final.get("completed"):
        run.status = AgentRunStatus.STEP_LIMIT_REACHED
        run.final_summary = (
            f"Stopped after {MAX_TOOL_STEPS} steps without reaching a conclusion; "
            f"handing over to a coordinator."
        )
        plan_service.raise_coordinator_exception(
            repo, run.final_summary, kind="step_limit", order_id=event.order_id,
            delivery_date=event.affected_date,
        )
    else:
        run.status = AgentRunStatus.COMPLETED
        run.final_summary = " ".join(
            a.summary for a in run.actions if a.ok and a.tool != "finish"
        ) or "No action was needed."

    # Every path through this function passes here or through the except above, so there is no
    # way to leave without the customer having been answered.
    _guarantee_a_reply(repo, event, ctx, run)

    repo.save_agent_run(run)
    return run


def _guarantee_a_reply(repo, event, ctx: tools.ToolContext, run: AgentRunLog) -> None:
    """Send something if the run did not, so no customer turn ends in silence.

    Deliberately last and deliberately dumb: it does not decide anything, it only notices that a
    message was owed and never sent. Anything smarter here would be a second decision-maker
    competing with the loop.
    """
    if ctx.order is None:
        return
    # Ask whether the customer was actually answered, not which tool ran. `send_message` is not
    # the only thing that speaks: accepting a slot writes its own confirmation, and keying off the
    # tool name meant a successful booking was followed by "sorry, a coordinator will call you".
    if any(
        message.run_id == run.id and message.direction == MessageDirection.OUTBOUND
        for message in repo.messages(ctx.order.id)
    ):
        return
    if event.event_type not in (
        PlanningEventType.NEW_ORDER,
        PlanningEventType.CUSTOMER_REJECTED_OFFER,
        PlanningEventType.CUSTOMER_ACCEPTED_OFFER,
        PlanningEventType.MANUAL_RETRY,
    ):
        return  # operational events have no customer waiting on them

    prepared = ctx.scratch.get("offer_message") or ctx.scratch.get("customer_message")
    body = prepared or (
        "Sorry -- I could not sort that out automatically just now. One of our coordinators "
        "will call you shortly to arrange your delivery."
    )

    from dispatch_agent.planning import offer_service

    offer_service.record_message(repo, ctx.order.id, body, run_id=run.id)
    run.actions.append(
        AgentActionLog(
            step=len(run.actions) + 1,
            tool="send_message",
            ok=True,
            arguments={"order_id": ctx.order.id},
            summary=f"Reply sent by the completion guarantee ({len(body)} chars).",
            reason_summary="The run ended without answering the customer.",
            decider="controller",
        )
    )
    if not prepared:
        plan_service.raise_coordinator_exception(
            repo,
            "A customer message ended without the agent producing a reply; they were told a "
            "coordinator would call.",
            kind="unanswered_message",
            order_id=ctx.order.id,
        )
