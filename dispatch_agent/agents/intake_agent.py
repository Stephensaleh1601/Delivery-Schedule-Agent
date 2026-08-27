"""LangGraph graph: WhatsApp message text -> a persisted, geocoded JobRecord.

Three nodes: extract (Claude, forced tool call) -> validate_and_geocode (pure Python) ->
persist (SQLite). A message that's missing a required field or an ungeocodable address comes
back with `errors` set and `job` unset -- the caller (dashboard or demo script) is expected to
route that to the coordinator instead of silently dropping it.
"""
from __future__ import annotations

from datetime import date as Date, time as Time
from typing import Optional, TypedDict

from langgraph.graph import END, StateGraph

from dispatch_agent.agents.prompts import INTAKE_SYSTEM_PROMPT_TEMPLATE, RECORD_JOB_TOOL_SCHEMA
from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.llm import BedrockClaude
from dispatch_agent.models import Address, JobRecord, JobType, TimeWindow


class IntakeState(TypedDict, total=False):
    raw_message: str
    extracted: dict
    job: Optional[JobRecord]
    errors: list[str]


def _extract_node(llm: BedrockClaude):
    def node(state: IntakeState) -> IntakeState:
        system = INTAKE_SYSTEM_PROMPT_TEMPLATE.format(today=Date.today().isoformat())
        extracted = llm.extract_structured(
            system=system,
            user=state["raw_message"],
            tool_name="record_job",
            tool_schema=RECORD_JOB_TOOL_SCHEMA,
        )
        return {"extracted": extracted}

    return node


def _validate_and_geocode_node(state: IntakeState) -> IntakeState:
    extracted = state["extracted"]
    errors: list[str] = []

    for field in ("address_raw_text", "job_type", "delivery_date", "availability"):
        if not extracted.get(field):
            errors.append(f"missing required field: {field}")

    if not extracted.get("customer_name"):
        errors.append("missing customer_name -- coordinator must fill this in")

    coordinates = None
    postal_code = extracted.get("postal_code")
    if postal_code:
        try:
            coordinates = postal_code_to_coords(postal_code)
        except ValueError as exc:
            errors.append(str(exc))
    else:
        errors.append("no postal code extracted -- coordinator must geocode manually")

    if errors:
        return {"errors": errors, "job": None}

    address = Address(raw_text=extracted["address_raw_text"], postal_code=postal_code, coordinates=coordinates)
    availability = [
        TimeWindow(start=Time.fromisoformat(w["start"]), end=Time.fromisoformat(w["end"]))
        for w in extracted["availability"]
    ]
    job = JobRecord(
        customer_name=extracted.get("customer_name") or "UNKNOWN — needs coordinator input",
        phone=extracted.get("phone"),
        address=address,
        job_type=JobType(extracted["job_type"]),
        availability=availability,
        duration_minutes=extracted.get("duration_minutes") or 60,
        delivery_date=Date.fromisoformat(extracted["delivery_date"]),
        raw_message=state["raw_message"],
        notes=extracted.get("notes"),
    )
    return {"job": job, "errors": []}


def _persist_node(repo: JobsRepository):
    def node(state: IntakeState) -> IntakeState:
        if state.get("job") is not None:
            repo.save_job(state["job"])
        return {}

    return node


def build_intake_graph(llm: BedrockClaude | None = None, repo: JobsRepository | None = None):
    llm = llm or BedrockClaude()
    repo = repo or JobsRepository()

    graph = StateGraph(IntakeState)
    graph.add_node("extract", _extract_node(llm))
    graph.add_node("validate_and_geocode", _validate_and_geocode_node)
    graph.add_node("persist", _persist_node(repo))
    graph.set_entry_point("extract")
    graph.add_edge("extract", "validate_and_geocode")
    graph.add_edge("validate_and_geocode", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


def run_intake(
    raw_message: str, llm: BedrockClaude | None = None, repo: JobsRepository | None = None
) -> IntakeState:
    graph = build_intake_graph(llm, repo)
    return graph.invoke({"raw_message": raw_message})
