"""The HTTP surface, driven through FastAPI's TestClient.

The service layer is already tested directly; these check the contract a frontend will actually
consume -- shapes, status codes, and the behaviours that only show up over HTTP, like replaying
a request.
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from dispatch_agent import config
from dispatch_agent.models import PlanningStatus
from dispatch_agent.planning.clock import PlanningClock

BASE = date(2026, 9, 2)


@pytest.fixture()
def client(temp_db, monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())
    from dispatch_agent.webapp.main import app

    return TestClient(app)


def _order_payload(name="Mrs Tan", postal="018956", options=None):
    days = PlanningClock.horizon_dates()
    return {
        "customer_name": name,
        "phone": "91230000",
        "address_raw": f"{name}'s place",
        "postal_code": postal,
        "job_type": "sofa",
        "availability": options
        or [
            {"date": days[0].isoformat(), "window_start": "09:00", "window_end": "13:00", "preference_rank": 1},
            {"date": days[1].isoformat(), "window_start": "13:00", "window_end": "18:00", "preference_rank": 2},
        ],
    }


def test_horizon_tells_the_browser_what_dates_are_bookable(client):
    body = client.get("/api/horizon").json()
    assert body["today"] == BASE.isoformat()
    # BASE is a Wednesday, so the cycle is that same week's Friday and Saturday.
    assert body["first"] == (BASE + timedelta(days=2)).isoformat()
    assert body["last"] == (BASE + timedelta(days=3)).isoformat()
    assert len(body["dates"]) == 2


def test_booking_an_order_leaves_it_undated(client):
    response = client.post("/api/orders", json=_order_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["planning_status"] == PlanningStatus.PENDING_PLANNING.value

    order = next(o for o in client.get("/api/orders").json() if o["id"] == body["id"])
    assert order["delivery_date"] is None, "nothing has been promised yet"
    assert order["locked_window"] is None
    assert len(order["availability_options"]) == 2


def test_a_date_outside_the_horizon_is_rejected_with_an_explanation(client):
    payload = _order_payload(options=[
        {"date": (BASE + timedelta(days=1)).isoformat(), "window_start": "09:00", "window_end": "12:00"}
    ])
    response = client.post("/api/orders", json=payload)
    assert response.status_code == 400
    assert "only take bookings between" in response.json()["detail"]


def test_an_address_outside_singapore_is_refused(client, monkeypatch):
    from dispatch_agent.geo.geocoder import GeocodeResult
    from dispatch_agent.models import Coordinates
    import dispatch_agent.webapp.jobs_service as service

    monkeypatch.setattr(
        service,
        "geocode_postal_code",
        lambda c: GeocodeResult(Coordinates(lat=3.139, lng=101.687), "onemap", "Kuala Lumpur"),
    )
    response = client.post("/api/orders", json=_order_payload())
    assert response.status_code == 400
    assert "delivery area" in response.json()["detail"]


def test_plan_options_returns_an_offer_with_its_reasoning(client):
    order_id = client.post("/api/orders", json=_order_payload()).json()["id"]

    body = client.post(f"/api/orders/{order_id}/plan-options").json()

    assert body["error"] is None
    assert 1 <= len(body["offer"]["options"]) <= 2
    assert body["offer"]["options"][0]["label"], "each slot needs a human-readable label"
    # The breakdown is what lets a coordinator argue with the ranking rather than just accept it.
    feasible = [e for e in body["evaluations"] if e["feasible"]]
    assert feasible and set(feasible[0]["breakdown"]) == {
        "incremental_drive_minutes",
        "day_opening_penalty_minutes",
        "preference_penalty_minutes",
        "overtime_penalty_minutes",
    }


def test_accepting_an_offer_confirms_and_publishes_a_plan(client):
    order_id = client.post("/api/orders", json=_order_payload()).json()["id"]
    offer = client.post(f"/api/orders/{order_id}/plan-options").json()["offer"]
    slot = offer["options"][0]

    body = client.post(
        f"/api/offers/{offer['id']}/respond", json={"accepted": True, "slot_id": slot["id"]}
    ).json()

    assert body["confirmed"] is True
    assert body["delivery_date"] == slot["date"]
    assert body["plan_version"] == 1

    order = next(o for o in client.get("/api/orders").json() if o["id"] == order_id)
    assert order["planning_status"] == PlanningStatus.CONFIRMED.value
    assert order["locked_window"] is not None

    plan = client.get(f"/api/plans/{slot['date']}").json()
    assert order_id in {s["job_id"] for s in plan["stops"]}


def test_replaying_an_acceptance_is_harmless(client):
    """A retried request or a double-tapped button must not produce a second plan version."""
    order_id = client.post("/api/orders", json=_order_payload()).json()["id"]
    offer = client.post(f"/api/orders/{order_id}/plan-options").json()["offer"]
    slot = offer["options"][0]
    body = {"accepted": True, "slot_id": slot["id"]}

    client.post(f"/api/offers/{offer['id']}/respond", json=body)
    second = client.post(f"/api/offers/{offer['id']}/respond", json=body).json()

    assert second["idempotent"] is True
    versions = client.get(f"/api/plans/{slot['date']}/versions").json()
    assert len(versions) == 1


def test_declining_a_slot_comes_back_with_a_different_time(client):
    """A rejection is a step in the negotiation, not the end of one.

    This used to assert the order was left sitting in the planning pool, which was the whole of the
    behaviour: something else had to notice and act. Declining now runs the agent in the same call
    -- the time is excluded, the customer's dates are re-solved around it, and a different window
    comes back -- so the order is legitimately OFFERED again by the time this returns.
    """
    order_id = client.post("/api/orders", json=_order_payload()).json()["id"]
    offer = client.post(f"/api/orders/{order_id}/plan-options").json()["offer"]
    declined = offer["options"][0]

    body = client.post(
        f"/api/offers/{offer['id']}/respond",
        json={"accepted": False, "slot_id": declined["id"]},
    ).json()

    assert body["confirmed"] is False
    assert body["next_offer"] is not None, "declining one time should not end the conversation"
    offered_again = [
        (o["date"], o["window"]["start"], o["window"]["end"]) for o in body["next_offer"]["options"]
    ]
    assert (declined["date"], declined["window"]["start"], declined["window"]["end"]) not in offered_again

    # The run is returned with it, so the trace beside the conversation shows the exclusion.
    assert [a["tool"] for a in body["run"]["actions"]][:2] == ["record_rejection", "evaluate_slots"]

    order = next(o for o in client.get("/api/orders").json() if o["id"] == order_id)
    assert order["planning_status"] == PlanningStatus.OFFERED.value


def test_plan_versions_expose_the_history_for_comparison(client):
    """The v1-vs-v2 panel needs enough per version to be compared without extra requests."""
    first = client.post("/api/orders", json=_order_payload("Alice", "018956")).json()["id"]
    offer = client.post(f"/api/orders/{first}/plan-options").json()["offer"]
    slot = offer["options"][0]
    client.post(f"/api/offers/{offer['id']}/respond", json={"accepted": True, "slot_id": slot["id"]})

    second = client.post("/api/orders", json=_order_payload("Bob", "486123", options=[
        {"date": slot["date"], "window_start": "09:00", "window_end": "18:00"}
    ])).json()["id"]
    offer2 = client.post(f"/api/orders/{second}/plan-options").json()["offer"]
    client.post(
        f"/api/offers/{offer2['id']}/respond",
        json={"accepted": True, "slot_id": offer2["options"][0]["id"]},
    )

    versions = client.get(f"/api/plans/{slot['date']}/versions").json()
    assert [v["version"] for v in versions] == [1, 2]
    assert [v["status"] for v in versions] == ["superseded", "active"]
    assert versions[0]["stop_count"] == 1 and versions[1]["stop_count"] == 2
    assert versions[1]["reason_created"]
    assert all("round_trip_drive_minutes" in v for v in versions)


def test_regenerating_a_route_does_not_reset_confirmed_orders(client):
    """The old endpoint set every job to SEQUENCED unconditionally, which under the planning
    lifecycle would erase the fact that a customer had been promised a slot."""
    order_id = client.post("/api/orders", json=_order_payload()).json()["id"]
    offer = client.post(f"/api/orders/{order_id}/plan-options").json()["offer"]
    slot = offer["options"][0]
    client.post(f"/api/offers/{offer['id']}/respond", json={"accepted": True, "slot_id": slot["id"]})

    client.post("/api/route-plan", json={"date": slot["date"]})

    order = next(o for o in client.get("/api/orders").json() if o["id"] == order_id)
    assert order["planning_status"] == PlanningStatus.SEQUENCED.value
    assert order["locked_window"] is not None, "the promise must survive a route regeneration"


def test_no_feasible_slot_raises_a_coordinator_exception(client):
    """The system must escalate rather than quietly inventing a time nobody offered."""
    days = PlanningClock.horizon_dates()
    order_id = client.post("/api/orders", json={
        **_order_payload(),
        "duration_minutes": 300,
        "availability": [
            {"date": days[0].isoformat(), "window_start": "09:00", "window_end": "09:30"}
        ],
    }).json()["id"]

    body = client.post(f"/api/orders/{order_id}/plan-options").json()

    assert body["offer"] is None and body["error"]
    exceptions = client.get("/api/exceptions").json()
    assert any(e["order_id"] == order_id for e in exceptions)


# -- disruption, morning run, metrics ------------------------------------------


def _confirm_an_order(client, name="Mrs Tan", postal="018956"):
    order_id = client.post("/api/orders", json=_order_payload(name, postal)).json()["id"]
    offer = client.post(f"/api/orders/{order_id}/plan-options").json()["offer"]
    slot = offer["options"][0]
    client.post(f"/api/offers/{offer['id']}/respond", json={"accepted": True, "slot_id": slot["id"]})
    return order_id, slot["date"]


def test_marking_an_order_delayed_takes_it_off_the_route(client):
    order_id, day = _confirm_an_order(client)

    body = client.post(f"/api/orders/{order_id}/readiness", json={"readiness_status": "delayed"}).json()

    assert body["readiness_status"] == "delayed"
    assert body["freed_date"] == day
    plan = client.get(f"/api/plans/{day}").json()
    assert order_id not in {s["job_id"] for s in plan["stops"]}


def test_an_unknown_readiness_value_is_rejected(client):
    order_id, _ = _confirm_an_order(client)
    response = client.post(f"/api/orders/{order_id}/readiness", json={"readiness_status": "exploded"})
    assert response.status_code == 400


def test_morning_run_dispatches_the_day_and_drafts_reminders(client):
    order_id, day = _confirm_an_order(client)

    body = client.post("/api/events/morning-run", json={"date": day}).json()

    assert body["error"] is None
    assert body["dispatched"] >= 1 and body["reminders"] == body["dispatched"]
    order = next(o for o in client.get("/api/orders").json() if o["id"] == order_id)
    assert order["planning_status"] == "dispatched"
    assert order["locked_window"] is not None, "dispatching must not discard the promise"


def test_metrics_come_from_stored_data_and_count_moved_appointments(client):
    """The headline number. It is computed by comparing every published stop against the window
    that customer was actually promised -- not asserted, and not assumed to be zero."""
    _confirm_an_order(client, "Alice", "018956")
    _confirm_an_order(client, "Bob", "486123")

    body = client.get("/api/metrics").json()

    assert body["confirmed_appointments_moved"] == 0
    assert body["scheduled_stops"] >= 2
    assert body["customers_contacted"] >= 2
    assert body["plan_versions"] >= 2


def test_agent_runs_expose_the_real_tool_sequence(client):
    order_id = client.post("/api/orders", json=_order_payload()).json()["id"]
    client.post(f"/api/orders/{order_id}/plan-agentic")

    runs = client.get("/api/agent-runs").json()
    assert runs, "the agent run was not recorded"
    tools_called = [a["tool"] for a in runs[0]["actions"]]
    assert "evaluate_slots" in tools_called and "create_offer" in tools_called
    assert all(a["reason"] for a in runs[0]["actions"]), "every step needs a coordinator-facing reason"
