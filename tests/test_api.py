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
    assert body["first"] == (BASE + timedelta(days=2)).isoformat()
    assert body["last"] == (BASE + timedelta(days=5)).isoformat()
    assert len(body["dates"]) == 4


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
    from dispatch_agent.models import Coordinates
    import dispatch_agent.webapp.jobs_service as service

    monkeypatch.setattr(service, "postal_code_to_coords", lambda c: Coordinates(lat=3.139, lng=101.687))
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


def test_rejecting_an_offer_returns_the_order_to_the_planning_pool(client):
    order_id = client.post("/api/orders", json=_order_payload()).json()["id"]
    offer = client.post(f"/api/orders/{order_id}/plan-options").json()["offer"]

    body = client.post(f"/api/offers/{offer['id']}/respond", json={"accepted": False}).json()

    assert body["confirmed"] is False
    order = next(o for o in client.get("/api/orders").json() if o["id"] == order_id)
    assert order["planning_status"] == PlanningStatus.PENDING_PLANNING.value


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
