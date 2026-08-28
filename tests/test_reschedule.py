from datetime import date, time

from conftest import FakeLLM

from dispatch_agent.agents.planning_agent import run_planning
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.models import Address, JobRecord, JobType, RescheduleRequest, TimeWindow
from dispatch_agent.reschedule import apply_reschedule


def _job(name, postal_code, day, start, end):
    return JobRecord(
        customer_name=name,
        address=Address(raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)),
        job_type=JobType.SOFA,
        availability=[TimeWindow(start=time(*start), end=time(*end))],
        delivery_date=day,
        raw_message="test",
        duration_minutes=30,
    )


def test_reschedule_reports_affected_customers(temp_db):
    day = date(2026, 8, 28)
    jobs = [
        _job("A", "018956", day, (9, 0), (18, 0)),
        _job("B", "119613", day, (9, 0), (18, 0)),
        _job("C", "238874", day, (9, 0), (18, 0)),
    ]
    for job in jobs:
        temp_db.save_job(job)

    llm = FakeLLM()
    result = run_planning(jobs, day, llm=llm)
    sequence = result["sequence"]
    assert sequence is not None
    temp_db.save_sequence(sequence)

    moved = jobs[0]
    new_day = date(2026, 9, 3)
    plan = apply_reschedule(
        RescheduleRequest(job_id=moved.id, new_date=new_day, raw_message="move me"),
        repo=temp_db,
        llm=llm,
    )

    assert moved.id in plan.affected_job_ids
    assert plan.delivery_date == new_day
    assert len(plan.proposed_sequence.stops) == 1
