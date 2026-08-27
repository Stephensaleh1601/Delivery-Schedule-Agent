from datetime import date

from conftest import FakeLLM

from dispatch_agent.agents.intake_agent import build_intake_graph


def test_intake_produces_job_record(temp_db):
    structured = {
        "customer_name": "Mrs Tan",
        "phone": "91234567",
        "address_raw_text": "1 Marina Blvd",
        "postal_code": "018956",
        "job_type": "delivery",
        "duration_minutes": 60,
        "delivery_date": "2026-08-28",
        "availability": [{"start": "09:00", "end": "12:00"}],
        "notes": None,
    }
    graph = build_intake_graph(llm=FakeLLM(structured_response=structured), repo=temp_db)
    result = graph.invoke({"raw_message": "hi, need delivery to 1 Marina Blvd S018956, free Fri morning"})

    assert result["errors"] == []
    job = result["job"]
    assert job.customer_name == "Mrs Tan"
    assert job.delivery_date == date(2026, 8, 28)
    assert temp_db.get_job(job.id) is not None


def test_intake_flags_missing_postal_code(temp_db):
    structured = {
        "customer_name": "Mr Lee",
        "address_raw_text": "somewhere in Tampines",
        "postal_code": None,
        "job_type": "installation",
        "delivery_date": "2026-08-28",
        "availability": [{"start": "09:00", "end": "12:00"}],
    }
    graph = build_intake_graph(llm=FakeLLM(structured_response=structured), repo=temp_db)
    result = graph.invoke({"raw_message": "install my aircon somewhere in Tampines"})

    assert result["job"] is None
    assert any("postal code" in e for e in result["errors"])
