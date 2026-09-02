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
from dispatch_agent.models import (
    AgentActionLog,
    AgentRunLog,
    AgentRunStatus,
    PlanningEvent,
    PlanningEventType,
)
from dispatch_agent.planning import plan_service, tools
from dispatch_agent.planning.clock import PlanningClock

MAX_TOOL_STEPS = 6


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


class DecisionAgent(Protocol):
    def decide(self, state: "SchedulingState", allowed: list[str]) -> ActionDecision: ...


class LLMDecisionAgent:
    def __init__(self, llm: LLMClient | None = None):
        self._llm = llm or build_llm_client()

    def decide(self, state: "SchedulingState", allowed: list[str]) -> ActionDecision:
        raw = self._llm.extract_structured(
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


class RuleDecisionAgent:
    """A deterministic policy over the same state the model sees.

    Exists so a demo survives the LLM being unavailable: the flow still runs, and the run log
    says plainly that it fell back rather than pretending a model made the calls.
    """

    def decide(self, state, allowed) -> ActionDecision:
        event = state["event"]
        done = {a.tool for a in state.get("actions", [])}

        if event.event_type is PlanningEventType.NEW_ORDER:
            if "evaluate_slots" not in done:
                return ActionDecision(action="evaluate_slots",
                                      reason_summary="Checking which of the requested windows we can serve.",
                                      arguments={"order_id": event.order_id})
            if "create_offer" not in done:
                return ActionDecision(action="create_offer",
                                      reason_summary="Offering the workable slots to the customer.",
                                      arguments={"order_id": event.order_id})
            if "send_message" not in done:
                return ActionDecision(action="send_message",
                                      reason_summary="Sending the options to the customer.",
                                      arguments={"order_id": event.order_id,
                                                 "body": state.get("customer_message") or "We have some options for you."})
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
                return ActionDecision(action="record_rejection",
                                      reason_summary="Recording that the customer declined.",
                                      arguments={"offer_id": event.payload.get("offer_id")})
            if "evaluate_slots" not in done:
                return ActionDecision(action="evaluate_slots",
                                      reason_summary="Looking for another workable window.",
                                      arguments={"order_id": event.order_id})
            if "create_offer" not in done:
                return ActionDecision(action="create_offer",
                                      reason_summary="Offering the next best slot.",
                                      arguments={"order_id": event.order_id})
            if _last_failed(state, "create_offer") and "create_exception" not in done:
                # Nothing left we can offer -- every window the customer gave us has now been
                # tried. Ending here would silently abandon the order, so hand it to a human.
                return ActionDecision(
                    action="create_exception",
                    reason_summary="No remaining window works; asking a coordinator to call the customer.",
                    arguments={
                        "order_id": event.order_id,
                        "kind": "no_remaining_slot",
                        "message": "Customer declined every slot we could offer; needs a call to agree a new time.",
                    },
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
    step_count: int
    completed: bool
    error: Optional[str]


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


def _decide_node(decider: DecisionAgent, fallback: DecisionAgent | None):
    def node(state: SchedulingState) -> SchedulingState:
        try:
            decision = decider.decide(state, tools.allowed_actions())
        except Exception as exc:  # noqa: BLE001
            if fallback is None:
                return {"error": f"could not decide what to do next: {exc}", "completed": True}
            # Recorded, not hidden: the log should say the model was unavailable rather than
            # implying it made these calls.
            decision = fallback.decide(state, tools.allowed_actions())
            decision.reason_summary = f"[model unavailable, using standard procedure] {decision.reason_summary}"
        return {"pending_decision": decision}

    return node


def _act_node(ctx: tools.ToolContext):
    def node(state: SchedulingState) -> SchedulingState:
        decision: ActionDecision = state["pending_decision"]
        step = state.get("step_count", 0) + 1

        result = tools.dispatch(decision.action, decision.arguments, ctx)
        entry = AgentActionLog(
            step=step,
            tool=decision.action,
            ok=result.ok,
            summary=result.summary,
            reason_summary=decision.reason_summary,
            error=result.error,
            data=result.data,
        )
        update: SchedulingState = {
            "actions": state.get("actions", []) + [entry],
            "step_count": step,
            "last_tool_result": result.model_dump(),
        }
        if ctx.scratch.get("offer_message"):
            update["customer_message"] = ctx.scratch["offer_message"]
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
    graph.add_node("decide", _decide_node(decider, fallback))
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
) -> AgentRunLog:
    """Run the agent for one event and return its persisted log.

    Every trigger enters here: a new order, a customer's reply, a readiness change. A run is
    recorded before it starts and updated after each step, so the activity panel can be polled
    while it is still going rather than only seeing a finished result.
    """
    repo = repo or JobsRepository()

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

    ctx = tools.ToolContext(repo=repo, routing_client=routing_client)
    decider = decider or LLMDecisionAgent()
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
        run.completed_at = datetime.now(timezone.utc)
        repo.save_agent_run(run)
        return run

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

    repo.save_agent_run(run)
    return run
