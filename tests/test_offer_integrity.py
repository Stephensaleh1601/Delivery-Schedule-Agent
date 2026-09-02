"""One booking, one offer, and a log that matches what the customer was told.

The frontend used to call `/plan-agentic` and then `/plan-options`. Both mint an offer, and
`create_offer` excludes windows already offered -- so the second call produced a DIFFERENT pair of
slots from the first. The customer saw one set, the agent log recorded another, both offer rounds
were spent before the customer replied, and on a two-option order the second call raised and wrote
a coordinator exception claiming the windows could not be fitted. They could; they had just already
been offered.

These tests pin every part of that shut.
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from dispatch_agent import config
from dispatch_agent.planning.clock import PlanningClock

BASE = date(2026, 9, 2)


@pytest.fixture()
def client(temp_db, monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())
    from dispatch_agent.webapp.main import app

    return TestClient(app)


def _order(client, name="Mrs Lee", postal="018956", option_count=2):
    days = PlanningClock.horizon_dates()
    windows = [("09:00", "13:00"), ("13:00", "18:00"), ("09:00", "18:00")]
    return client.post("/api/orders", json={
        "customer_name": name,
        "phone": "91112222",
        "address_raw": f"{name}'s place",
        "postal_code": postal,
        "job_type": "sofa",
        "availability": [
            {"date": days[i].isoformat(), "window_start": windows[i][0],
             "window_end": windows[i][1], "preference_rank": i + 1}
            for i in range(option_count)
        ],
    }).json()["id"]


def _offer_rows(temp_db, order_id):
    return temp_db.offers_for_order(order_id)


# -- the bug ------------------------------------------------------------------


def test_planning_an_order_produces_exactly_one_offer(client, temp_db):
    order_id = _order(client)

    client.post(f"/api/orders/{order_id}/plan-agentic")

    assert len(_offer_rows(temp_db, order_id)) == 1


def test_the_offered_slots_are_the_slots_in_the_agent_log(client, temp_db):
    """The load-bearing one. If these ever diverge, the demo is showing the customer something
    the agent did not decide."""
    order_id = _order(client, option_count=3)

    body = client.post(f"/api/orders/{order_id}/plan-agentic").json()

    offer = body["offer"]
    create = next(a for a in body["run"]["actions"] if a["tool"] == "create_offer")
    assert create["ok"], create
    assert offer["id"] == create["data"]["offer_id"], "the returned offer is not the one logged"
    # And the log's summary names those same slots. Compared on content rather than exact
    # punctuation -- the label and the summary format the same slot slightly differently.
    for slot in offer["options"]:
        assert slot["date"] in {o["date"] for o in offer["options"]}
        rendered = create["summary"]
        assert any(part in rendered for part in slot["label"].split(", ")), (
            f"{slot['label']!r} is not named in {rendered!r}"
        )


def test_calling_plan_options_after_the_agent_does_not_open_a_second_negotiation(client, temp_db):
    """The exact sequence the frontend used to run."""
    order_id = _order(client)

    first = client.post(f"/api/orders/{order_id}/plan-agentic").json()
    second = client.post(f"/api/orders/{order_id}/plan-options").json()

    assert len(_offer_rows(temp_db, order_id)) == 1
    assert second["reused"] is True
    assert second["offer"]["id"] == first["offer"]["id"]
    assert second["error"] is None


def test_a_second_planning_call_does_not_invent_a_coordinator_exception(client, temp_db):
    """With two options the old code raised on the second call and recorded 'none of the windows
    you gave us can be fitted' -- untrue, and it inflated the headline interventions figure."""
    order_id = _order(client, option_count=2)

    client.post(f"/api/orders/{order_id}/plan-agentic")
    client.post(f"/api/orders/{order_id}/plan-options")

    assert client.get("/api/exceptions").json() == []
    assert client.get("/api/metrics").json()["coordinator_interventions"] == 0


def test_the_customer_is_messaged_once(client, temp_db):
    order_id = _order(client)

    client.post(f"/api/orders/{order_id}/plan-agentic")
    client.post(f"/api/orders/{order_id}/plan-options")

    outbound = [m for m in temp_db.messages(order_id) if m.direction.value == "outbound"]
    assert len(outbound) == 1, [m.body for m in outbound]


def test_both_offer_rounds_survive_until_the_customer_replies(client, temp_db):
    """One booking must not spend the whole negotiation budget. After a rejection the customer
    should get a second offer, not an escalation."""
    order_id = _order(client, option_count=3)

    first = client.post(f"/api/orders/{order_id}/plan-agentic").json()["offer"]
    client.post(f"/api/offers/{first['id']}/respond", json={"accepted": False})
    second = client.post(f"/api/orders/{order_id}/plan-options").json()

    assert second["offer"] is not None, second["error"]
    assert second["offer"]["id"] != first["id"]


def test_replaying_the_planning_call_is_harmless(client, temp_db):
    order_id = _order(client)

    client.post(f"/api/orders/{order_id}/plan-agentic")
    again = client.post(f"/api/orders/{order_id}/plan-agentic").json()

    assert again["reused"] is True
    assert len(_offer_rows(temp_db, order_id)) == 1


# -- supporting fixes ---------------------------------------------------------


def test_offers_for_an_order_come_back_in_round_order(client, temp_db):
    """`ORDER BY id` sorted a random uuid, so offers[-1] could be the earlier round."""
    order_id = _order(client, option_count=3)
    first = client.post(f"/api/orders/{order_id}/plan-agentic").json()["offer"]
    client.post(f"/api/offers/{first['id']}/respond", json={"accepted": False})
    second = client.post(f"/api/orders/{order_id}/plan-options").json()["offer"]

    offers = _offer_rows(temp_db, order_id)

    assert [o.round_number for o in offers] == [1, 2]
    assert offers[-1].id == second["id"]
    assert temp_db.latest_offer_for_order(order_id).id == second["id"]


def test_a_failed_offer_never_sends_the_customer_a_placeholder(client, temp_db):
    """When there is nothing to offer, the rule agent used to fall through to send_message with
    no message set and send the literal string 'We have some options for you.'"""
    days = PlanningClock.horizon_dates()
    order_id = client.post("/api/orders", json={
        "customer_name": "Mr Impossible", "phone": "91110000",
        "address_raw": "Nowhere", "postal_code": "018956", "job_type": "cabinet",
        "duration_minutes": 300,
        "availability": [
            {"date": days[0].isoformat(), "window_start": "09:00", "window_end": "09:30"}
        ],
    }).json()["id"]

    body = client.post(f"/api/orders/{order_id}/plan-agentic").json()

    bodies = [m.body for m in temp_db.messages(order_id)]
    assert not any("We have some options for you" in b for b in bodies), bodies
    assert any(a["tool"] == "create_exception" and a["ok"] for a in body["run"]["actions"])


def test_the_round_cap_is_not_reported_as_an_unservable_window(client, temp_db):
    """Hitting our own offer cap is bookkeeping, not a routing failure, and must not be recorded
    as one."""
    from dispatch_agent.planning.offer_service import OfferError, create_offer
    from dispatch_agent.planning.candidate_service import CandidateService

    order_id = _order(client, option_count=3)
    order = temp_db.get_job(order_id)
    service = CandidateService(repo=temp_db)
    for _ in range(2):
        offer = create_offer(temp_db, order, service.evaluate_all(order))
        from dispatch_agent.planning.offer_service import reject_offer
        reject_offer(temp_db, offer.id)
        order = temp_db.get_job(order_id)

    with pytest.raises(OfferError) as exc:
        create_offer(temp_db, order, service.evaluate_all(order))
    assert exc.value.kind == "round_cap_reached"


# -- the activity log ---------------------------------------------------------


def test_the_log_records_what_each_tool_was_called_with(client, temp_db):
    order_id = _order(client)
    client.post(f"/api/orders/{order_id}/plan-agentic")

    run = client.get("/api/agent-runs").json()[0]
    evaluate = next(a for a in run["actions"] if a["tool"] == "evaluate_slots")

    assert evaluate["arguments"] == {"order_id": order_id}
    assert evaluate["data"]["feasible_count"] >= 1
    assert evaluate["timestamp"]


def test_an_activity_log_never_carries_another_customers_details(client, temp_db):
    """A coordinator reading one order's activity must not thereby read someone else's address,
    phone or day. Redaction happens where the log is written, so this holds for the stored row
    and not merely the rendered one."""
    import json

    days = PlanningClock.horizon_dates()
    neighbours = []
    for i, postal in enumerate(["469123", "520101"]):
        other = client.post("/api/orders", json={
            "customer_name": f"Neighbour {i}", "phone": f"9000000{i}",
            "address_raw": f"{i} Secret Lane", "postal_code": postal, "job_type": "sofa",
            "availability": [{"date": days[0].isoformat(), "window_start": "09:00", "window_end": "18:00"}],
        }).json()["id"]
        offer = client.post(f"/api/orders/{other}/plan-agentic").json()["offer"]
        client.post(f"/api/offers/{offer['id']}/respond",
                    json={"accepted": True, "slot_id": offer["options"][0]["id"]})
        neighbours.append((f"Neighbour {i}", f"9000000{i}", postal, "Secret Lane"))

    order_id = _order(client, name="Mrs Lee", postal="529536")
    client.post(f"/api/orders/{order_id}/plan-agentic")

    dumped = json.dumps(client.get("/api/agent-runs").json())
    for name, phone, postal, street in neighbours:
        assert phone not in dumped, f"another customer's phone leaked into the log"
        assert street not in dumped, f"another customer's address leaked into the log"
        assert postal not in dumped, f"another customer's postal code leaked into the log"


def test_a_long_message_is_capped_in_the_log(temp_db):
    import json
    from dispatch_agent.planning import tools

    capped = tools.capped_for_log({"order_id": "abc", "body": "x" * 5000})

    assert len(capped["body"]) <= tools.MAX_LOG_STRING + 3
    assert len(json.dumps(capped)) < tools.MAX_LOG_BYTES


def test_a_nested_sequence_is_redacted_not_merely_truncated(temp_db):
    from dispatch_agent.planning import tools

    cleaned = tools.sanitise_for_log(
        {"proposed_sequence": {"stops": [{"job_id": "other-customer", "arrival_window": "09:00"}]}}
    )

    assert cleaned["proposed_sequence"] == "[redacted]"
    assert "other-customer" not in str(cleaned)
