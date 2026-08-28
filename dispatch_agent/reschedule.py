"""The reschedule loop: a customer moves, the day re-solves, and only the customers whose
arrival window actually changed get flagged for a new message and coordinator approval.
"""
from __future__ import annotations

from datetime import date as Date

from dispatch_agent.agents.planning_agent import run_planning
from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.llm import LLMClient
from dispatch_agent.models import (
    DaySequence,
    JobStatus,
    OverrideLogEntry,
    ReschedulePlan,
    RescheduleRequest,
)


def apply_reschedule(
    request: RescheduleRequest,
    repo: JobsRepository | None = None,
    llm: LLMClient | None = None,
    routing_client: RoutingClient | None = None,
    draft_messages: bool = True,
) -> ReschedulePlan:
    repo = repo or JobsRepository()
    job = repo.get_job(request.job_id)
    if job is None:
        raise ValueError(f"unknown job_id {request.job_id!r}")

    previous_date = job.delivery_date
    previous_sequence = repo.get_sequence(previous_date)

    new_date: Date = request.new_date or job.delivery_date
    new_availability = request.new_availability or job.availability

    # Test feasibility against a candidate copy first -- don't persist the move until we know
    # the new day can actually fit it, otherwise a rejected request ("sorry, that slot doesn't
    # work") would still silently leave the job moved in the database.
    candidate = job.model_copy(update={"delivery_date": new_date, "availability": new_availability})
    jobs_on_new_day = [j for j in repo.jobs_for_date(new_date) if j.id != job.id] + [candidate]

    new_result = run_planning(jobs_on_new_day, new_date, llm, routing_client, draft_messages=draft_messages)
    proposed_new_day = new_result.get("sequence")
    if proposed_new_day is None:
        raise ValueError(new_result.get("error") or "could not find a feasible sequence for the new day")

    job.delivery_date = new_date
    job.availability = new_availability
    job.status = JobStatus.RESCHEDULE_REQUESTED
    job.raw_message = request.raw_message
    repo.save_job(job)

    proposed_old_day: DaySequence | None = None
    remaining_on_old_day = [j for j in repo.jobs_for_date(previous_date) if j.id != job.id]
    if remaining_on_old_day:
        old_result = run_planning(remaining_on_old_day, previous_date, llm, routing_client, draft_messages=draft_messages)
        proposed_old_day = old_result.get("sequence")

    affected_job_ids = _diff_affected(previous_sequence, proposed_old_day, proposed_new_day, moved_job_id=job.id)

    return ReschedulePlan(
        delivery_date=new_date,
        previous_sequence=previous_sequence
        or DaySequence(delivery_date=previous_date, stops=[], total_drive_minutes=0),
        proposed_sequence=proposed_new_day,
        affected_job_ids=affected_job_ids,
    )


def _diff_affected(
    previous_sequence: DaySequence | None,
    proposed_old_day: DaySequence | None,
    proposed_new_day: DaySequence,
    moved_job_id: str,
) -> list[str]:
    """Jobs whose arrival window actually changed -- these are the only ones that need a new
    message to the customer."""
    previous_windows = {s.job_id: s.arrival_window for s in (previous_sequence.stops if previous_sequence else [])}
    affected: set[str] = set()

    for stop in proposed_new_day.stops:
        if stop.job_id == moved_job_id:
            continue
        prior = previous_windows.get(stop.job_id)
        if prior is None or prior != stop.arrival_window:
            affected.add(stop.job_id)

    if proposed_old_day is not None:
        for stop in proposed_old_day.stops:
            prior = previous_windows.get(stop.job_id)
            if prior is None or prior != stop.arrival_window:
                affected.add(stop.job_id)

    affected.add(moved_job_id)
    return sorted(affected)


def record_override(
    delivery_date: Date,
    job_id: str,
    field_changed: str,
    agent_value: str,
    coordinator_value: str,
    reason: str | None = None,
    repo: JobsRepository | None = None,
) -> None:
    """Log any edit the coordinator makes to an agent-proposed value. This is the record the
    PRD's roadmap says a future version could learn from -- for now it's write-only, no
    learning loop reads it back."""
    repo = repo or JobsRepository()
    repo.log_override(
        OverrideLogEntry(
            delivery_date=delivery_date,
            job_id=job_id,
            field_changed=field_changed,
            agent_value=agent_value,
            coordinator_value=coordinator_value,
            reason=reason,
        )
    )
