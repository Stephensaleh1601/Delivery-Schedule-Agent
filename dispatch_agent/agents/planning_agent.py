"""LangGraph graph: a day's jobs -> a sequenced route with drafted per-customer messages.

Two nodes: solve (OR-Tools, via the routing client for real drive times) -> draft_messages
(Claude, one short WhatsApp draft per stop). This is "it acts by calling routing and drafting
messages" from the PRD -- the actual send is still the coordinator's call, made from the
dashboard after approval.
"""
from __future__ import annotations

from datetime import date as Date
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

from dispatch_agent.agents.prompts import DRAFT_MESSAGE_SYSTEM_PROMPT
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.geo.zones import COMPANY_DEPOT
from dispatch_agent.llm import LLMClient, build_llm_client
from dispatch_agent.models import DaySequence, JobRecord
from dispatch_agent.solver import sequence_day


class PlanningState(TypedDict, total=False):
    delivery_date: Date
    jobs: list[JobRecord]
    sequence: Optional[DaySequence]
    messages: dict[str, str]  # job_id -> drafted WhatsApp message
    error: Optional[str]


def _solve_node(routing_client: RoutingClient):
    def node(state: PlanningState) -> PlanningState:
        try:
            sequence = sequence_day(
                state["jobs"], state["delivery_date"], depot=COMPANY_DEPOT, routing_client=routing_client
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced to the coordinator, not swallowed
            return {"error": str(exc), "sequence": None}
        return {"sequence": sequence, "error": None}

    return node


def _draft_messages_node(llm: LLMClient):
    def node(state: PlanningState) -> PlanningState:
        sequence = state.get("sequence")
        if sequence is None:
            return {"messages": {}}
        jobs_by_id = {j.id: j for j in state["jobs"]}
        messages: dict[str, str] = {}
        for stop in sequence.stops:
            job = jobs_by_id[stop.job_id]
            user_prompt = (
                f"Customer: {job.customer_name}\n"
                f"Job type: {job.job_type.value}\n"
                f"Arrival window: {stop.arrival_window.start.strftime('%I:%M%p')} - "
                f"{stop.arrival_window.end.strftime('%I:%M%p')}\n"
                f"Date: {sequence.delivery_date.isoformat()}"
            )
            messages[job.id] = llm.complete(system=DRAFT_MESSAGE_SYSTEM_PROMPT, user=user_prompt, max_tokens=200)
        return {"messages": messages}

    return node


def build_planning_graph(llm: LLMClient | None = None, routing_client: RoutingClient | None = None):
    llm = llm or build_llm_client()
    routing_client = routing_client or RoutingClient()

    graph = StateGraph(PlanningState)
    graph.add_node("solve", _solve_node(routing_client))
    graph.add_node("draft_messages", _draft_messages_node(llm))
    graph.set_entry_point("solve")
    graph.add_edge("solve", "draft_messages")
    graph.add_edge("draft_messages", END)
    return graph.compile()


def run_planning(
    jobs: list[JobRecord],
    delivery_date: Date,
    llm: LLMClient | None = None,
    routing_client: RoutingClient | None = None,
) -> PlanningState:
    graph = build_planning_graph(llm, routing_client)
    return graph.invoke({"delivery_date": delivery_date, "jobs": jobs, "messages": {}})
