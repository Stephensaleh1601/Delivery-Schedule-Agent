"""Scripted walkthrough of the PRD's demo beat: three WhatsApp messages in, a proposed day,
a simulated coordinator approval, then a reschedule that ripples to the other stops.

    python scripts/run_demo.py

Uses the sample messages in data/sample_messages.json. Requires AWS credentials with Bedrock
access (see .env.example) -- this calls the real intake and planning agents, not stubs.
"""
from __future__ import annotations

import json
from datetime import timedelta
from datetime import date as Date
from pathlib import Path

from dispatch_agent.agents.intake_agent import run_intake
from dispatch_agent.agents.planning_agent import run_planning
from dispatch_agent.db import JobsRepository, init_db
from dispatch_agent.models import JobStatus, RescheduleRequest
from dispatch_agent.reschedule import apply_reschedule

SAMPLE_MESSAGES_PATH = Path(__file__).resolve().parent.parent / "data" / "sample_messages.json"

# "Thursday" per the PRD's demo script -- the next Thursday after the jobs' delivery date.
THURSDAY = 3


def _next_weekday(d: Date, weekday: int) -> Date:
    days_ahead = (weekday - d.weekday()) % 7
    days_ahead = days_ahead or 7
    return d + timedelta(days=days_ahead)


def main() -> None:
    init_db()
    repo = JobsRepository()
    messages = json.loads(SAMPLE_MESSAGES_PATH.read_text())

    print(f"-- Intake: {len(messages)} WhatsApp messages --")
    job_ids = []
    for raw in messages:
        result = run_intake(raw)
        if result.get("errors"):
            print(f"  [needs coordinator] {raw[:40]}... -> {result['errors']}")
            continue
        job = result["job"]
        job_ids.append(job.id)
        print(f"  OK: {job.customer_name} ({job.job_type.value}, {job.delivery_date})")

    if not job_ids:
        print("No jobs extracted cleanly -- nothing to sequence. Check Bedrock credentials.")
        return

    delivery_date = repo.get_job(job_ids[0]).delivery_date
    jobs = repo.jobs_for_date(delivery_date)

    print(f"\n-- Planning: sequencing {len(jobs)} jobs for {delivery_date} --")
    result = run_planning(jobs, delivery_date)
    sequence = result.get("sequence")
    if sequence is None:
        print(f"  Could not sequence the day: {result.get('error')}")
        return
    repo.save_sequence(sequence)
    for stop in sequence.stops:
        job = repo.get_job(stop.job_id)
        print(f"  {stop.sequence_index + 1}. {job.customer_name}: {stop.arrival_window.start}-{stop.arrival_window.end}")
    print(f"  Total drive time: {sequence.total_drive_minutes} min")

    print("\n-- Coordinator approves the proposed day --")
    for stop in sequence.stops:
        job = repo.get_job(stop.job_id)
        job.status = JobStatus.APPROVED
        repo.save_job(job)
    print("  Approved.")

    print("\n-- Reschedule: customer two asks to move to Thursday --")
    if len(sequence.stops) < 2:
        print("  Only one job sequenced -- skipping the reschedule beat.")
        return
    moved_job_id = sequence.stops[1].job_id
    new_date = _next_weekday(delivery_date, THURSDAY)
    plan = apply_reschedule(
        RescheduleRequest(job_id=moved_job_id, raw_message="Can we do Thursday instead?", new_date=new_date)
    )
    repo.save_sequence(plan.proposed_sequence)
    print(f"  Moved to {new_date}. {len(plan.affected_job_ids)} customers affected: {plan.affected_job_ids}")
    print("  New slots proposed -- back to coordinator approval.")


if __name__ == "__main__":
    main()
