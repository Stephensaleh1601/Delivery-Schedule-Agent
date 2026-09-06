"""The conversation over HTTP: one message, one run, and a thread that survives a refresh.

These are the guarantees a judge can break by accident -- pressing send twice, reloading the page,
scrolling back to an older message and opening its trace. Each of them was a real defect class in
this codebase, so each has a test that fails if the fix is removed rather than only a comment saying
it was fixed.
"""
from datetime import date, timedelta

import pytest

from dispatch_agent import config
from dispatch_agent.planning.clock import PlanningClock

BASE = date(2026, 9, 2)
SATURDAY = date(2026, 9, 5)
FRIDAY = date(2026, 9, 4)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


@pytest.fixture()
def client(temp_db, monkeypatch):
    """The app against this test's own database. `temp_db` first, so the app never touches the
    developer's real one."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())

    # Both cluster days must carry a published route, or there is no coordination cycle and every
    # conversation escalates. That is the correct behaviour -- a customer may only be inserted
    # into a route that exists -- so the fixture has to supply the world the flow assumes.
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "scripts"))
    import seed_test_clients

    seed_test_clients.seed()

    from dispatch_agent.webapp.main import app

    return TestClient(app)


def _new_order(client, name="Mrs Lee", postal_code="149544"):
    """An order with no availability yet -- the state a customer is in before they say anything.

    Two things here are load-bearing, and both were learned the hard way.

    The postal code is Commonwealth Lane, which is CENTRAL -- so this customer's cluster day is
    Saturday, which is what every test below then asks for. With an eastern address the Saturday
    route passes nowhere near them, and "I'm free Saturday" correctly produces an escalation
    rather than an offer, which is right behaviour and the wrong scenario for these tests.

    The seeded day is SATURDAY rather than `horizon_dates()[0]` for the same reason: if the
    placeholder lands on the other cluster day the order carries two stated availabilities and the
    assertions stop meaning what they say.
    """
    day = SATURDAY
    body = client.post(
        "/api/orders",
        json={
            "customer_name": name,
            "address_raw": "Blk 1",
            "postal_code": postal_code,
            "job_type": "sofa",
            "availability": [
                {"date": day.isoformat(), "window_start": "09:00", "window_end": "18:00"}
            ],
        },
    ).json()
    return body["id"]


def _say(client, order_id, text):
    response = client.post(f"/api/orders/{order_id}/messages", json={"body": text})
    assert response.status_code == 200, response.text
    return response.json()


def _outbound(turn):
    return [m for m in turn["messages"] if m["direction"] == "outbound"]


def _inbound(turn):
    return [m for m in turn["messages"] if m["direction"] == "inbound"]


# -- one message, one run -------------------------------------------------------


def test_a_typed_timing_produces_an_offer(client):
    order_id = _new_order(client)

    turn = _say(client, order_id, "I'm free Saturday morning.")

    assert turn["intent"] == "provide_availability"
    assert turn["open_offer_id"], "a stated timing should put something on the table"
    offer = turn["offers"][turn["open_offer_id"]]
    assert offer["options"], "an offer with no options is not an offer"


def test_one_message_creates_exactly_one_run_and_one_offer_round(client):
    """The duplicate-offer regression, restated for the natural-language path. Two runs per message
    means two negotiations with different slots, and the customer sees whichever arrives last."""
    order_id = _new_order(client)

    turn = _say(client, order_id, "I'm free Saturday morning.")

    assert len(turn["runs"]) == 1, f"expected one run, got {list(turn['runs'])}"
    assert len(turn["offers"]) == 1
    assert list(turn["offers"].values())[0]["round_number"] == 1


def test_sending_the_same_message_twice_does_not_start_a_second_negotiation(client):
    """A double-tap, or a client retry. Neither may open a second round or duplicate their words."""
    order_id = _new_order(client)

    first = _say(client, order_id, "I'm free Saturday morning.")
    second = _say(client, order_id, "I'm free Saturday morning.")

    assert second["duplicate"] is True
    assert len(_inbound(second)) == len(_inbound(first)) == 1
    assert len(second["offers"]) == 1
    assert len(second["runs"]) == len(first["runs"])


def test_a_second_different_message_is_answered_not_deduplicated(client):
    order_id = _new_order(client)
    _say(client, order_id, "I'm free Saturday morning.")

    turn = _say(client, order_id, "Actually, can you do the afternoon?")

    assert turn["duplicate"] is False


# -- no forced three options ----------------------------------------------------


def test_a_single_timing_is_never_rejected_for_being_only_one(client):
    """The product direction. The old form demanded two or three windows; a customer with one
    workable time must be bookable."""
    order_id = _new_order(client)

    turn = _say(client, order_id, "Saturday morning is the only time I can do.")

    assert turn["open_offer_id"], "one timing must be enough"
    assert not any(
        "two" in m["body"].lower() or "three" in m["body"].lower() for m in _outbound(turn)
    ), "nothing should ask the customer for more options"


def test_a_fixed_timing_is_not_argued_with(client):
    """They said it is their only time. The reply may confirm or refuse, but must not propose a
    different day -- that is nagging a customer who has already told us."""
    order_id = _new_order(client)

    turn = _say(client, order_id, "Saturday morning, that's the only time I can do.")

    offer = turn["offers"].get(turn["open_offer_id"] or "")
    if offer:
        assert {o["date"] for o in offer["options"]} == {SATURDAY.isoformat()}


def test_a_date_outside_the_horizon_is_explained(client):
    order_id = _new_order(client)
    tomorrow = BASE + timedelta(days=1)

    turn = _say(client, order_id, f"Can you come {tomorrow:%d %B}?")

    replies = " ".join(m["body"] for m in _outbound(turn))
    first, last = PlanningClock.horizon()
    assert f"{first.day}" in replies and f"{last.day}" in replies, (
        "the customer must be told what range they can actually book"
    )


# -- accepting ------------------------------------------------------------------


def test_typing_an_acceptance_confirms_the_appointment(client):
    order_id = _new_order(client)
    opened = _say(client, order_id, "I'm free Saturday morning.")
    offer = opened["offers"][opened["open_offer_id"]]

    turn = _say(client, order_id, "Okay, take that one.")

    assert turn["confirmed"] is True, turn["messages"][-1]["body"]
    assert turn["planning_status"] == "confirmed"
    assert turn["delivery_date"] == offer["options"][0]["date"]


def test_an_ambiguous_acceptance_asks_instead_of_booking(client):
    """Two slots and a bare "okay". Booking the first one is a van at the wrong door."""
    order_id = _new_order(client)
    opened = _say(client, order_id, "Saturday morning or Friday morning both work.")
    offer = opened["offers"].get(opened["open_offer_id"] or "")
    if not offer or len(offer["options"]) < 2:
        pytest.skip("this scenario needs two slots on the table")

    turn = _say(client, order_id, "okay")

    assert turn["confirmed"] is False
    assert turn["planning_status"] != "confirmed"


# -- rejecting ------------------------------------------------------------------


def test_declining_a_time_comes_back_with_a_different_one(client):
    """A decline must produce an answer, and never the time just declined.

    "An answer" is now one of two things, because the fallback is all-or-nothing: three
    alternatives, or a coordinator. Two safe choices is not a smaller version of the right answer,
    so the test accepts either -- what it will not accept is silence, or the same slot again.
    """
    order_id = _new_order(client)
    opened = _say(client, order_id, "I'm free Saturday, any time.")
    first = opened["offers"][opened["open_offer_id"]]["options"][0]

    turn = _say(client, order_id, "That time doesn't work. Can you do later?")

    assert _outbound(turn), "declining one time must not end the conversation in silence"
    if turn["open_offer_id"]:
        now_offered = turn["offers"][turn["open_offer_id"]]["options"]
        assert len(now_offered) == 3, "the fallback offer is exactly three or none"
        assert first["id"] not in [o["id"] for o in now_offered]
        assert (first["window"]["start"], first["window"]["end"]) not in [
            (o["window"]["start"], o["window"]["end"]) for o in now_offered
        ]
    else:
        # Escalated. The customer must be told, not left waiting.
        assert "coordinator" in _outbound(turn)[-1]["body"].lower()


def test_declining_a_time_keeps_the_day(client):
    """"Not that time" is not "not that day". The re-solve should stay on Saturday."""
    order_id = _new_order(client)
    _say(client, order_id, "I'm free Saturday, any time.")

    turn = _say(client, order_id, "That doesn't work, anything later that day?")

    offer = turn["offers"].get(turn["open_offer_id"] or "")
    if offer:
        assert SATURDAY.isoformat() in {o["date"] for o in offer["options"]}


def test_the_customer_is_not_asked_a_third_time(client):
    """The two-round cap, unchanged, and now reachable by typing."""
    order_id = _new_order(client)
    _say(client, order_id, "I'm free Saturday, any time.")
    _say(client, order_id, "That doesn't work, something else?")

    turn = _say(client, order_id, "No, that's no good either.")

    rounds = [o["round_number"] for o in turn["offers"].values()]
    assert max(rounds) <= 2, f"offered a third round: {rounds}"


def test_customer_can_propose_a_concrete_time_after_automatic_round_cap(client):
    """The cap limits agent-generated alternatives, not a customer's ability to say exactly when
    they can receive a large delivery."""
    order_id = _new_order(client)
    # Saturday, not Sunday: Sunday sat inside the old four-day horizon but is not a delivery
    # day, so it would draw a "we only deliver Friday and Saturday" reply and never consume
    # an offer round -- leaving the cap this test is about one round out of reach.
    _say(client, order_id, "I'm free Saturday after 2pm.")
    _say(client, order_id, "That doesn't work, any other time?")
    _say(client, order_id, "No, that doesn't work either.")

    # The automatic rounds are spent. A concrete new time from the CUSTOMER is not another
    # agent-generated alternative, so it still gets looked at.
    turn = _say(client, order_id, "How about 5th Sept 10am?")

    assert turn["intent"] == "provide_availability"
    called = [a["tool"] for a in turn["run"]["actions"]]
    assert "find_insertion_options" in called, (
        "a concrete time from the customer must still be checked against the routes"
    )


# -- explaining -----------------------------------------------------------------


def test_asking_why_gets_an_answer_built_from_the_route(client):
    order_id = _new_order(client)
    _say(client, order_id, "I'm free Saturday, any time.")

    turn = _say(client, order_id, "Why this timing?")

    assert turn["intent"] == "explain"
    reply = _outbound(turn)[-1]["body"]
    assert reply.strip(), "an explanation request must be answered"


def test_an_explanation_never_names_another_customer(client):
    """The route facts may say where the van will be. They may not say who else is on it."""
    other = _new_order(client, name="Mr Ong", postal_code="529536")
    _say(client, other, "Saturday morning works.")
    opened = _say(client, other, "okay")
    assert opened  # Mr Ong is now on the route

    order_id = _new_order(client, name="Mrs Lee", postal_code="469123")
    _say(client, order_id, "I'm free Saturday, any time.")
    turn = _say(client, order_id, "Why that time?")

    for message in _outbound(turn):
        assert "Mr Ong" not in message["body"]


# -- unclear --------------------------------------------------------------------


def test_an_unclear_message_asks_one_question_and_books_nothing(client):
    order_id = _new_order(client)

    turn = _say(client, order_id, "lol")

    assert turn["confirmed"] is False
    replies = _outbound(turn)
    assert len(replies) == 1, "one question, not an interrogation"
    assert replies[0]["body"].endswith("?")


# -- persistence: the refresh ---------------------------------------------------


def test_the_conversation_survives_a_reload(client):
    order_id = _new_order(client)
    live = _say(client, order_id, "I'm free Saturday morning.")

    reloaded = client.get(f"/api/orders/{order_id}/messages").json()

    assert [m["id"] for m in reloaded["messages"]] == [m["id"] for m in live["messages"]]
    assert [m["body"] for m in reloaded["messages"]] == [m["body"] for m in live["messages"]]
    assert reloaded["open_offer_id"] == live["open_offer_id"]


def test_every_agent_message_carries_the_run_that_produced_it(client):
    order_id = _new_order(client)
    _say(client, order_id, "I'm free Saturday morning.")

    reloaded = client.get(f"/api/orders/{order_id}/messages").json()

    for message in _outbound(reloaded):
        assert message["run_id"], f"no trace linked to: {message['body'][:40]}"
        assert message["run_id"] in reloaded["runs"], "the run it names must be returned with it"


def test_an_older_message_keeps_its_own_trace_after_a_newer_one(client):
    """The defect this replaces: pinning "the latest run" to every bubble. After a second message
    the first one must still open the calls that actually produced it."""
    order_id = _new_order(client)
    _say(client, order_id, "I'm free Saturday, any time.")
    _say(client, order_id, "That doesn't work, anything later?")

    reloaded = client.get(f"/api/orders/{order_id}/messages").json()
    replies = _outbound(reloaded)
    assert len(replies) >= 2, "need two agent messages to tell them apart"

    run_ids = [m["run_id"] for m in replies]
    assert len(set(run_ids)) > 1, "two messages from two runs must not share one run id"
    assert run_ids[0] != run_ids[-1]


def test_a_traces_calls_belong_only_to_that_message(client):
    order_id = _new_order(client)
    _say(client, order_id, "I'm free Saturday, any time.")
    _say(client, order_id, "That doesn't work, anything later?")

    reloaded = client.get(f"/api/orders/{order_id}/messages").json()
    first, last = _outbound(reloaded)[0], _outbound(reloaded)[-1]

    early = client.get(f"/api/agent-runs/{first['run_id']}").json()
    late = client.get(f"/api/agent-runs/{last['run_id']}").json()

    assert early["id"] != late["id"]
    # The first run opened a negotiation; the second recorded a rejection. Distinguishable by the
    # tools they actually called, which is the point of linking them properly.
    assert "record_rejection" not in [a["tool"] for a in early["actions"]]
    assert "record_rejection" in [a["tool"] for a in late["actions"]]


def test_the_customers_own_words_carry_no_trace(client):
    """An inbound message has no run. Null is a first-class case, not a gap to fill with the
    nearest run -- a customer's message did not come from a tool call."""
    order_id = _new_order(client)
    _say(client, order_id, "I'm free Saturday morning.")

    reloaded = client.get(f"/api/orders/{order_id}/messages").json()

    for message in _inbound(reloaded):
        assert message["run_id"] is None


def test_the_offer_names_the_run_that_created_it(client):
    order_id = _new_order(client)
    turn = _say(client, order_id, "I'm free Saturday morning.")

    offer = turn["offers"][turn["open_offer_id"]]

    assert offer["run_id"] in turn["runs"]


# -- the inspector's data -------------------------------------------------------


def test_the_trace_shows_the_real_calls_with_per_step_provenance(client):
    order_id = _new_order(client)
    turn = _say(client, order_id, "I'm free Saturday morning.")

    run = list(turn["runs"].values())[0]

    assert run["actions"], "a run with no actions is not a trace"
    for action in run["actions"]:
        assert action["tool"]
        assert action["reason"], "every step needs a one-line operational reason"
        assert action["decider"], "every step must say who chose it"
        assert isinstance(action["ok"], bool)


def test_the_step_count_is_whatever_actually_ran(client):
    """The inspector's count is read from this list, so it cannot be hardcoded."""
    order_id = _new_order(client)
    turn = _say(client, order_id, "I'm free Saturday morning.")

    run = list(turn["runs"].values())[0]
    assert len(run["actions"]) == len({a["step"] for a in run["actions"]})
    assert [a["step"] for a in run["actions"]] == list(range(1, len(run["actions"]) + 1))


def test_the_negotiation_calls_are_visible_to_a_judge(client):
    """The specific tools the demo is meant to show off, actually called and actually logged."""
    order_id = _new_order(client)
    turn = _say(client, order_id, "I'm free Saturday morning.")

    called = [a["tool"] for a in list(turn["runs"].values())[0]["actions"]]

    assert "record_availability" in called
    # The insertion path, not the older day-evaluation one. These are the calls the demo is about:
    # the policy that was read, the routes that were loaded, and the search that measured a real
    # position on one of them.
    assert "retrieve_policy" in called
    assert "get_existing_routes" in called
    assert "find_insertion_options" in called
    assert "create_normal_offer" in called


def test_a_rejection_shows_the_exclusion_and_the_resolve(client):
    order_id = _new_order(client)
    _say(client, order_id, "I'm free Saturday, any time.")
    turn = _say(client, order_id, "That doesn't work, anything later?")

    called = [a["tool"] for a in turn["run"]["actions"]]

    assert "record_rejection" in called
    assert "find_insertion_options" in called, (
        "the routes have to be searched again around the excluded time"
    )


def test_no_step_leaks_another_customers_details(client):
    other = _new_order(client, name="Mr Ong", postal_code="529536")
    _say(client, other, "Saturday morning works.")
    _say(client, other, "okay")

    order_id = _new_order(client, name="Mrs Lee", postal_code="469123")
    turn = _say(client, order_id, "I'm free Saturday, any time.")

    import json

    dumped = json.dumps(turn["runs"])
    assert "Mr Ong" not in dumped
    assert "529536" not in dumped
