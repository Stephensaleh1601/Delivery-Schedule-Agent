"""Sending the finished route to the driver -- a coordinator's action, never the agent's."""
import re
import sys
from datetime import date as Date
from pathlib import Path

import pytest

from dispatch_agent import config
from dispatch_agent.planning import plan_service, tools

BASE = Date(2026, 9, 2)
FRIDAY = Date(2026, 9, 4)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


@pytest.fixture()
def client(temp_db, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())
    import seed_test_clients

    seed_test_clients.seed()
    from dispatch_agent.webapp.main import app

    return TestClient(app)


def test_the_customer_facing_agent_cannot_dispatch_a_route():
    """The guarantee, and the reason it is structural rather than a rule.

    `send_driver_route` is not in the registry at all, so there is no intent list to keep correct
    and no prompt to talk the model out of. A customer cannot reach it because it does not exist
    as something the agent can call.
    """
    assert "send_driver_route" not in tools.TOOL_REGISTRY
    for intent, scope in tools.INTENT_TOOLS.items():
        assert not {a for a in scope if "driver" in a}, intent


def test_the_message_carries_the_run_in_delivery_order(client):
    body = client.post(f"/api/plans/{FRIDAY.isoformat()}/dispatch").json()

    assert body["driver"]["name"]
    assert body["stop_count"] == 8
    assert body["plan_version"] >= 1
    # Numbered in the order the route actually runs, so the driver reads it top to bottom.
    numbered = re.findall(r"^(\d+)\. ", body["message"], re.M)
    assert [int(n) for n in numbered] == list(range(1, 9))
    assert body["driver"]["name"] in body["message"]
    assert f"v{body['plan_version']}" in body["message"]


def test_the_maps_link_covers_every_stop_from_the_depot(client):
    body = client.post(f"/api/plans/{FRIDAY.isoformat()}/dispatch").json()

    url = body["maps_url"]
    assert url.startswith("https://www.google.com/maps/dir/?api=1&origin=")
    # Depot as origin, last stop as destination, everyone else a waypoint: 8 stops means the
    # depot plus 8 points, so 7 waypoints between origin and destination.
    assert url.count("|") == 6


def test_dispatch_uses_the_latest_active_version(client, temp_db):
    """A route sent before a late booking is a route that is already wrong. It must be read at
    send time, not cached from whenever the page was opened."""
    first = client.post(f"/api/plans/{FRIDAY.isoformat()}/dispatch").json()

    order = next(j for j in temp_db.all_jobs() if j.customer_name == "Mr Rajan")
    order.delivery_date = FRIDAY
    order.availability = [temp_db.active_plan(FRIDAY).sequence.stops[0].arrival_window]
    order.locked_window = order.availability[0]
    order.set_planning_status(__import__(
        "dispatch_agent.models", fromlist=["PlanningStatus"]).PlanningStatus.CONFIRMED)
    temp_db.save_job(order)
    plan_service.replan_day(temp_db, FRIDAY, reason="a late booking")

    second = client.post(f"/api/plans/{FRIDAY.isoformat()}/dispatch").json()

    assert second["plan_version"] > first["plan_version"]
    assert second["stop_count"] == first["stop_count"] + 1
    assert "Mr Rajan" in second["message"]


def test_an_unpublished_day_cannot_be_dispatched(client):
    """Nothing to send is a 404, not an empty message a driver would act on."""
    response = client.post("/api/plans/2026-09-07/dispatch")

    assert response.status_code == 404
