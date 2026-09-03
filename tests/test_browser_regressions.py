"""The failures a browser walkthrough found, each pinned so it cannot come back.

Every one of these passed the existing suite and failed in a real session. They shared a single
root cause: nothing constrained which tools a run could call, or how many times. The rule decider
consulted what it had already done; the model never did, and `dispatch()` executed anything on the
global allow-list. So "why this timing?" could reject the offer it was explaining, and one message
could send three identical offer bubbles.

Written at the API level on purpose. A unit test on the guard would pass with the guard wired to
nothing; these go through the same endpoint the browser does.
"""
from datetime import date, time, timedelta

import pytest

from dispatch_agent import config
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.models import (
    Address,
    JobRecord,
    JobType,
    PlanningStatus,
    TimeWindow,
)
from dispatch_agent.planning.clock import PlanningClock

BASE = date(2026, 9, 3)
SATURDAY = date(2026, 9, 5)
TUESDAY = date(2026, 9, 8)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


@pytest.fixture()
def client(temp_db, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())
    from dispatch_agent.webapp.main import app

    return TestClient(app)


def _order(client, name="Mrs Lee", postal_code="460216"):
    return client.post(
        "/api/orders",
        json={
            "customer_name": name, "address_raw": "Blk 1", "postal_code": postal_code,
            "job_type": "sofa", "availability": [],
        },
    ).json()["id"]


def _confirmed(repo, name, postal_code, day, window=(9, 0, 18, 0), duration=45):
    job = JobRecord(
        customer_name=name,
        address=Address(
            raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)
        ),
        job_type=JobType.SOFA,
        availability=[TimeWindow(start=time(window[0], window[1]), end=time(window[2], window[3]))],
        locked_window=TimeWindow(start=time(window[0], window[1]), end=time(window[2], window[3])),
        delivery_date=day,
        planning_status=PlanningStatus.CONFIRMED,
        status="approved",
        raw_message="seed",
        duration_minutes=duration,
    )
    repo.save_job(job)
    return job


def _say(client, order_id, text):
    r = client.post(f"/api/orders/{order_id}/messages", json={"body": text})
    assert r.status_code == 200, r.text
    return r.json()


def _outbound(turn):
    return [m for m in turn["messages"] if m["direction"] == "outbound"]


def _tools(turn):
    return [a["tool"] for a in (turn["run"]["actions"] if turn.get("run") else [])]


def _ok_tools(turn):
    return [a["tool"] for a in (turn["run"]["actions"] if turn.get("run") else []) if a["ok"]]


# -- Scenario A: the direct happy path ------------------------------------------


def test_one_message_produces_one_offer_one_send_and_one_bubble(client):
    """The duplicate-offer failure. "Tuesday after 1 works for me." sent the identical offer
    message three times: one create_offer, three send_message, seven steps."""
    order_id = _order(client)

    turn = _say(client, order_id, "Tuesday after 1 works for me.")

    assert _ok_tools(turn).count("send_message") == 1, _tools(turn)
    assert _ok_tools(turn).count("create_offer") == 1, _tools(turn)
    assert len(turn["offers"]) == 1
    assert len(_outbound(turn)) == 1, [m["body"][:40] for m in _outbound(turn)]


def test_no_tool_runs_twice_in_one_run(client):
    """Nothing that changes state should be reachable twice from one customer message."""
    order_id = _order(client)

    turn = _say(client, order_id, "Tuesday after 1 works for me.")

    succeeded = _ok_tools(turn)
    assert len(succeeded) == len(set(succeeded)), succeeded


def test_accepting_locks_exactly_once(client):
    """lock_appointment ran twice after "Confirm 1pm-3pm". The second was idempotent, so nothing
    broke -- but it should never have been attempted, and a trace that shows an action twice
    invites the question of what else is being retried blindly."""
    order_id = _order(client)
    _say(client, order_id, "Tuesday after 1 works for me.")

    turn = _say(client, order_id, "Okay, take the first one.")

    assert _ok_tools(turn).count("lock_appointment") == 1, _tools(turn)
    assert turn["confirmed"] is True
    assert turn["planning_status"] == "confirmed"


def test_acceptance_sends_exactly_one_confirmation(client):
    order_id = _order(client)
    before = _say(client, order_id, "Tuesday after 1 works for me.")

    after = _say(client, order_id, "Okay, take the first one.")

    new = len(_outbound(after)) - len(_outbound(before))
    assert new == 1, [m["body"][:50] for m in _outbound(after)]
    assert "confirmed" in _outbound(after)[-1]["body"].lower()


def test_acceptance_publishes_exactly_one_route_version(client, temp_db):
    order_id = _order(client)
    _say(client, order_id, "Tuesday after 1 works for me.")
    turn = _say(client, order_id, "Okay, take the first one.")

    versions = client.get(f"/api/plans/{turn['delivery_date']}/versions").json()

    assert len(versions) == 1, [v["version"] for v in versions]
    assert sum(1 for v in versions if v["status"] == "active") == 1


# -- Scenario B: explanation must not touch the booking -------------------------


def test_asking_why_runs_only_read_only_tools(client):
    """The worst of the failures. "Why this timing?" ran eight tools: it rejected the offer it was
    explaining, went looking for replacement customers, and raised a coordinator exception."""
    order_id = _order(client)
    _say(client, order_id, "Tuesday after 1 works for me.")

    turn = _say(client, order_id, "Why this timing?")

    assert turn["intent"] == "explain"
    allowed = {"evaluate_slots", "explain_choice", "send_message", "finish"}
    assert set(_ok_tools(turn)) <= allowed, _tools(turn)


@pytest.mark.parametrize(
    "forbidden",
    ["create_offer", "record_rejection", "find_ready_replacements", "create_exception",
     "lock_appointment", "record_availability", "replan_day"],
)
def test_an_explanation_cannot_reach_any_state_changing_tool(client, forbidden):
    order_id = _order(client)
    _say(client, order_id, "Tuesday after 1 works for me.")

    turn = _say(client, order_id, "Why this timing?")

    assert forbidden not in _ok_tools(turn), _tools(turn)


def test_an_explanation_leaves_the_offer_exactly_as_it_was(client):
    order_id = _order(client)
    opened = _say(client, order_id, "Tuesday after 1 works for me.")
    before = opened["offers"][opened["open_offer_id"]]

    turn = _say(client, order_id, "Why this timing?")

    assert turn["open_offer_id"] == opened["open_offer_id"]
    after = turn["offers"][turn["open_offer_id"]]
    assert after["options"] == before["options"]
    assert len(turn["offers"]) == 1, "an explanation created another offer"


def test_an_explanation_raises_no_coordinator_exception(client):
    order_id = _order(client)
    _say(client, order_id, "Tuesday after 1 works for me.")

    _say(client, order_id, "Why this timing?")

    assert client.get("/api/exceptions").json() == []


def test_an_explanation_publishes_no_route(client):
    order_id = _order(client)
    opened = _say(client, order_id, "Tuesday after 1 works for me.")
    day = opened["offers"][opened["open_offer_id"]]["options"][0]["date"]

    _say(client, order_id, "Why this timing?")

    assert client.get(f"/api/plans/{day}/versions").json() == []


def test_an_explanation_is_answered_with_one_message(client):
    order_id = _order(client)
    before = _say(client, order_id, "Tuesday after 1 works for me.")

    turn = _say(client, order_id, "Why this timing?")

    assert len(_outbound(turn)) - len(_outbound(before)) == 1


# -- Scenario B, continued: confirmation still works after an explanation -------


def test_the_customer_can_still_confirm_after_asking_why(client):
    """The end of the first browser session: the explanation corrupted the state, the customer
    clicked confirm, nothing came back, and the order sat on "Offer sent"."""
    order_id = _order(client)
    opened = _say(client, order_id, "Tuesday after 1 works for me.")
    slot = opened["offers"][opened["open_offer_id"]]["options"][0]

    _say(client, order_id, "Why this timing?")
    turn = _say(client, order_id, "Okay, take the first one.")

    assert turn["confirmed"] is True, _outbound(turn)[-1]["body"]
    assert turn["planning_status"] == "confirmed"
    assert turn["delivery_date"] == slot["date"]

    versions = client.get(f"/api/plans/{slot['date']}/versions").json()
    assert len(versions) == 1, "the route should be published exactly once"


# -- Scenario D: general support ------------------------------------------------


def test_an_address_question_is_not_read_as_an_answer_to_the_offer(client):
    """It was answered with "Which of those times would you like?"."""
    order_id = _order(client)
    opened = _say(client, order_id, "Tuesday after 1 works for me.")

    turn = _say(client, order_id, "Can I change my delivery address?")

    assert turn["intent"] == "general_support"
    assert turn["confirmed"] is False
    assert turn["open_offer_id"] == opened["open_offer_id"], "the offer was disturbed"
    reply = _outbound(turn)[-1]["body"].lower()
    assert "which of those times" not in reply
    assert "postal" in reply or "colleague" in reply


def test_a_support_question_cannot_reach_the_booking_tools(client):
    order_id = _order(client)
    _say(client, order_id, "Tuesday after 1 works for me.")

    turn = _say(client, order_id, "Can I change my delivery address?")

    allowed = {"ask_clarification", "create_exception", "send_message", "finish"}
    assert set(_ok_tools(turn)) <= allowed, _tools(turn)
    assert len(turn["offers"]) == 1


def test_a_support_question_escalates_once_not_repeatedly(client):
    order_id = _order(client)
    _say(client, order_id, "Tuesday after 1 works for me.")

    _say(client, order_id, "Can I change my delivery address?")

    assert len(client.get("/api/exceptions").json()) == 1


# -- Scenario E: rejection ------------------------------------------------------


def test_rejecting_one_window_re_solves_the_same_day_once(client):
    order_id = _order(client)
    opened = _say(client, order_id, "I'm free Tuesday, any time.")
    first = opened["offers"][opened["open_offer_id"]]["options"][0]

    turn = _say(client, order_id, "That doesn't work, anything later that day?")

    assert _ok_tools(turn).count("record_rejection") == 1
    assert _ok_tools(turn).count("create_offer") <= 1
    assert _ok_tools(turn).count("send_message") == 1
    offered = turn["offers"][turn["open_offer_id"]]["options"]
    assert (first["window"]["start"], first["window"]["end"]) not in [
        (o["window"]["start"], o["window"]["end"]) for o in offered
    ]


def test_a_rejection_sends_one_message_not_several(client):
    order_id = _order(client)
    before = _say(client, order_id, "I'm free Tuesday, any time.")

    turn = _say(client, order_id, "That doesn't work, anything later that day?")

    assert len(_outbound(turn)) - len(_outbound(before)) == 1


# -- Scenario F: persistence ----------------------------------------------------


def test_the_decision_panel_survives_a_refresh_and_keeps_the_confirmation(client):
    """The panel reverted to "Waiting for the customer" after a confirmed booking, because it only
    ever showed the run from the current request -- and a page load has no current request."""
    order_id = _order(client)
    _say(client, order_id, "Tuesday after 1 works for me.")
    _say(client, order_id, "Okay, take the first one.")

    reloaded = client.get(f"/api/orders/{order_id}/messages").json()

    assert reloaded["decision"] is not None
    assert reloaded["decision"]["meaningful"] is True
    assert any("locked" in line.lower() for line in reloaded["decision"]["outcome"]), (
        reloaded["decision"]["outcome"]
    )
    assert reloaded["confirmed"] is True


def test_a_clarification_does_not_replace_the_last_real_decision(client):
    """Asking something unrelated must not blank the panel that explains the booking."""
    order_id = _order(client)
    _say(client, order_id, "Tuesday after 1 works for me.")
    booked = client.get(f"/api/orders/{order_id}/messages").json()["decision_run_id"]

    _say(client, order_id, "lol")
    after = client.get(f"/api/orders/{order_id}/messages").json()

    assert after["decision_run_id"] == booked
    assert after["decision"]["meaningful"] is True


def test_the_decision_leads_with_the_conclusion_not_the_constraints(client):
    """A judge should understand this in five seconds. Heading, what changed, at most two
    candidates, one recommendation -- and the planning rules collapsed out of the way."""
    order_id = _order(client)

    turn = _say(client, order_id, "Saturday morning works.")

    decision = turn["decision"]
    assert decision["heading"] == "Why these times?"
    assert decision["asked"], decision
    assert decision["decision"], "the panel must state a conclusion"
    assert len(decision["candidates"]) <= 2, decision["candidates"]
    assert decision["planning_rules"], "the rules still exist, just collapsed"


def test_the_broad_availability_is_not_shown_as_a_candidate(client):
    """"Saturday 9am-6pm" used to sit beside "Saturday 11am-1pm" as though they were alternatives.
    One is a boundary the customer gave us; the other is a time we would actually turn up."""
    order_id = _order(client)

    turn = _say(client, order_id, "I'm free Saturday, any time.")

    for candidate in turn["decision"]["candidates"]:
        assert candidate["window"], candidate
        span = candidate["window"]
        assert "9am–6pm" not in span and "9am–18" not in span, (
            f"the customer's whole day is being shown as a candidate: {span}"
        )


def test_two_candidates_are_never_the_same_answer_twice(client, temp_db):
    """Two windows on one day with the same route impact are one option shown twice."""
    _confirmed(temp_db, "Anchor", "469123", SATURDAY, window=(9, 0, 12, 0))
    order_id = _order(client, postal_code="828761")

    turn = _say(client, order_id, "I'm free Saturday, any time.")

    candidates = turn["decision"]["candidates"]
    labels = [c["label"] for c in candidates]
    assert len(labels) == len(set(labels)), labels
    if len(candidates) == 2:
        a, b = candidates
        identical = (
            a["date"] == b["date"]
            and abs((a["added_drive_minutes"] or 0) - (b["added_drive_minutes"] or 0)) < 3
        )
        assert not identical, f"two candidates on one day with the same impact: {candidates}"


def test_a_candidate_is_labelled_customer_friendly_or_lowest_route_impact(client, temp_db):
    _confirmed(temp_db, "Anchor", "469123", TUESDAY)
    order_id = _order(client, postal_code="828761")

    turn = _say(client, order_id, "Saturday morning works.")

    for candidate in turn["decision"]["candidates"]:
        assert candidate["badge"] in ("Customer-friendly", "Lowest route impact"), candidate
        assert candidate["explanation"], candidate
        assert candidate["kind"] in ("customer", "route")


def test_the_recommendation_matches_the_cheaper_candidate(client, temp_db):
    """The decision sentence must name the option the numbers actually favour."""
    _confirmed(temp_db, "Anchor", "469123", TUESDAY)
    order_id = _order(client, postal_code="828761")

    turn = _say(client, order_id, "Saturday morning works.")
    decision = turn["decision"]
    candidates = decision["candidates"]
    if len(candidates) < 2:
        pytest.skip("this assertion is about a two-way comparison")

    cheapest = min(candidates, key=lambda c: c["added_drive_minutes"] or 0)
    assert "Recommend" in decision["decision"]
    assert cheapest["label"] in decision["decision"], (
        f"recommended something other than the cheapest: {decision['decision']}"
    )


def test_a_rejection_shows_the_sequence_that_produced_the_new_time(client):
    """The whole point of the redesign. A judge saw 9-11 become 11-1 and no reason why."""
    order_id = _order(client)
    opened = _say(client, order_id, "I'm free Saturday, any time.")
    rejected = opened["offers"][opened["open_offer_id"]]["options"][0]

    turn = _say(client, order_id, "That doesn't work, anything later that day?")

    decision = turn["decision"]
    assert decision["heading"] == "Why the offer changed"
    # The exact window that was removed, named.
    assert _clock(rejected["window"]["start"]) in decision["what_changed"], decision["what_changed"]
    assert "rejected" in decision["what_changed"] and "removed" in decision["what_changed"]
    tones = [s["tone"] for s in decision["steps"]]
    assert "removed" in tones and "solved" in tones, decision["steps"]


def _clock(hhmm):
    hour, minute = (int(p) for p in hhmm.split(":"))
    suffix = "am" if hour < 12 else "pm"
    shown = hour % 12 or 12
    return f"{shown}:{minute:02d}{suffix}" if minute else f"{shown}{suffix}"


def test_an_explanation_panel_says_nothing_changed(client):
    order_id = _order(client)
    _say(client, order_id, "I'm free Saturday, any time.")

    turn = _say(client, order_id, "Why this timing?")

    decision = turn["decision"]
    assert decision["heading"] == "Why this time?"
    assert "Nothing changed" in decision["what_changed"]
    assert decision["outcome"] == ["Offer unchanged", "Nothing rejected", "No route published"]


def test_the_confirmation_panel_reports_the_version_change(client, temp_db):
    """v1 -> v2, which only means anything if the day started at v1 -- which is what the seeded
    baseline routes are for. Without one the booking publishes v1 and there is no before."""
    from dispatch_agent.planning import plan_service

    _confirmed(temp_db, "Anchor", "469123", SATURDAY, window=(9, 0, 12, 0))
    plan_service.publish_plan_version(
        plan_service.solve_day(temp_db, SATURDAY), reason="Initial route for the day"
    )
    assert temp_db.active_plan(SATURDAY).version == 1

    order_id = _order(client, postal_code="828761")
    opened = _say(client, order_id, "I'm free Saturday, any time.")
    if opened["offers"][opened["open_offer_id"]]["options"][0]["date"] != SATURDAY.isoformat():
        pytest.skip("this assertion needs the Saturday slot to be the one offered first")

    turn = _say(client, order_id, "Okay, take the first one.")

    decision = turn["decision"]
    assert decision["heading"] == "What the agent changed"
    joined = " ".join(decision["outcome"])
    assert "Appointment locked" in joined
    assert "moved: 0" in joined
    assert "v1 → v2" in joined, joined


# -- the step limit -------------------------------------------------------------


def test_no_run_exceeds_the_server_side_step_limit(client):
    """The UI claimed six steps maximum while runs contained seven and eight."""
    from dispatch_agent.agents.scheduling_agent import MAX_TOOL_STEPS

    order_id = _order(client)
    for message in ["I'm free Tuesday, any time.", "Why this timing?",
                    "That doesn't work, anything later?", "Okay, take the first one."]:
        turn = _say(client, order_id, message)
        if turn.get("run"):
            assert len(turn["run"]["actions"]) <= MAX_TOOL_STEPS, (message, _tools(turn))


# -- Scenario C: a slot that is cheap to drive to but expensive to serve --------


def _early_route(repo):
    """A Tuesday whose confirmed work is all in the morning, and an unbooked customer nearby.

    Staggered locked windows, because three jobs all locked to the same two hours cannot be routed
    at all -- and an infeasible fixture would skip this test rather than fail it, which is how a
    scenario quietly stops covering the thing it was written for.
    """
    _confirmed(repo, "Early A", "469123", TUESDAY, window=(9, 0, 10, 30))
    _confirmed(repo, "Early B", "460115", TUESDAY, window=(10, 30, 12, 0))
    order = JobRecord(
        customer_name="Mrs Lee",
        address=Address(raw_text="x", postal_code="460216",
                        coordinates=postal_code_to_coords("460216")),
        job_type=JobType.SOFA, raw_message="t", duration_minutes=45,
    )
    repo.save_job(order)
    return order


def _evaluate(repo, order, start_h, end_h):
    from dispatch_agent.models import AvailabilityOption
    from dispatch_agent.planning.candidate_service import CandidateService

    return CandidateService(repo=repo).evaluate(order, AvailabilityOption(
        date=TUESDAY, window=TimeWindow(start=time(start_h, 0), end=time(end_h, 0))))


def test_a_late_slot_on_an_early_route_is_recognised_as_costly(temp_db):
    """The business failure underneath the browser session.

    A confirmed morning route, and the customer asks for the afternoon. Driving does not move at
    all -- the van visits the same places either way -- but the crew now sits idle for hours and
    finishes far later. Scoring that counted only driving called these equivalent, and the product
    then told the customer the late slot was "the time we can promise most reliably".
    """
    order = _early_route(temp_db)

    morning = _evaluate(temp_db, order, 9, 12)
    afternoon = _evaluate(temp_db, order, 15, 18)
    assert morning.feasible and afternoon.feasible, "both windows must be servable"

    # The trap: driving alone cannot tell these apart.
    assert morning.incremental_drive_minutes == afternoon.incremental_drive_minutes

    added_idle = afternoon.proposed_idle_minutes - afternoon.baseline_idle_minutes
    assert added_idle > 120, f"an afternoon slot on a morning route must show waiting: {added_idle}m"
    assert afternoon.proposed_span_minutes > morning.proposed_span_minutes + 120
    assert afternoon.total_score > morning.total_score, (
        f"idle time is not reaching the score: morning={morning.total_score} "
        f"afternoon={afternoon.total_score} on identical driving"
    )


def test_the_customer_is_told_about_the_wait_not_the_neighbourhood(temp_db):
    order = _early_route(temp_db)

    afternoon = _evaluate(temp_db, order, 15, 18)

    assert "waiting" in afternoon.customer_reason
    assert "promise most reliably" not in afternoon.customer_reason


def test_a_big_idle_gap_is_worth_asking_the_customer_about(temp_db):
    from dispatch_agent.planning import negotiation

    order = _early_route(temp_db)
    requested = _evaluate(temp_db, order, 15, 18)
    assert requested.feasible

    decision = negotiation.should_counteroffer(requested, alternatives=[])

    assert decision.should_ask and decision.kind == "idle_gap", decision
    assert "waiting" in decision.reason


def test_a_fixed_timing_is_honoured_even_when_it_strands_the_crew(temp_db):
    """"After 1pm is my only possible time" is a hard constraint. We may not like the day it
    produces, but we do not argue with it."""
    from dispatch_agent.planning import negotiation

    order = _early_route(temp_db)
    requested = _evaluate(temp_db, order, 15, 18)

    decision = negotiation.should_counteroffer(
        requested, alternatives=[], customer_says_fixed=True
    )

    assert not decision.should_ask
    assert decision.kind == "customer_fixed"


def test_already_nearby_is_only_claimed_when_it_is_true_at_that_hour(temp_db):
    """"We'll already be delivering in the East on Tuesday afternoon" was said about a route whose
    only eastern stop was at 9am, after which it went west. True about the day, false about the
    hour -- and the customer would have waited three and a half hours to find out."""
    from dispatch_agent.planning.route_facts import NEARBY_MINUTES, RouteFacts

    close = RouteFacts(region="East", position=2, stop_count=3, previous_customer="A",
                       next_customer="B", neighbours_in_region=2, opens_empty_day=False,
                       minutes_to_nearest_neighbour=45)
    hours_apart = RouteFacts(region="East", position=3, stop_count=3, previous_customer="A",
                             next_customer=None, neighbours_in_region=2, opens_empty_day=False,
                             minutes_to_nearest_neighbour=NEARBY_MINUTES + 120)
    stranded = RouteFacts(region="East", position=2, stop_count=3, previous_customer="A",
                          next_customer="B", neighbours_in_region=2, opens_empty_day=False,
                          minutes_to_nearest_neighbour=30, added_idle_minutes=226)

    assert close.already_in_the_area
    assert not hours_apart.already_in_the_area, "same region, wrong part of the day"
    assert not stranded.already_in_the_area, "nearby, but only after three hours of waiting"


def test_the_reason_states_the_consequence_rather_than_the_neighbourhood(temp_db):
    """A slot with overtime is not "the time we can promise most reliably", however little driving
    it adds. The customer is told what it actually costs."""
    from dispatch_agent.planning.route_facts import RouteFacts, customer_reason

    costly = RouteFacts(region="East", position=3, stop_count=3, previous_customer="A",
                        next_customer=None, neighbours_in_region=2, opens_empty_day=False,
                        minutes_to_nearest_neighbour=30, added_idle_minutes=226,
                        overtime_minutes=34)

    reason = customer_reason(costly, TUESDAY, TimeWindow(start=time(13, 0), end=time(15, 0)))

    assert "already" not in reason.lower()
    assert "34 minutes" in reason
    assert "promise most reliably" not in reason


def test_minute_is_singular_when_there_is_one_of_them():
    """The product wrote "1 minutes"."""
    from dispatch_agent.planning.route_facts import minutes_phrase

    assert minutes_phrase(1) == "1 minute"
    assert minutes_phrase(2) == "2 minutes"
    assert minutes_phrase(60) == "1 hour"
    assert minutes_phrase(226) == "3h 46m"


def test_a_support_topic_survives_the_model_calling_it_unclear(temp_db):
    """A live run had gpt-4o-mini file "Can I change my delivery address?" as `unclear`, and the
    clarification branch then answered it with "which of those times would you like?" -- the
    original misreading, arriving by a different route.

    Whether a message is about an address is a narrow lexical question. The regex answers it
    reliably, so it overrides the model, exactly as `is_fixed` does.
    """
    from dispatch_agent.agents.understanding import MessageReader
    from tests.conftest import FakeLLM

    confused = FakeLLM(structured_response={"intent": "unclear"})

    understood = MessageReader(llm=confused).read(
        "Can I change my delivery address?", has_open_offer=True
    )

    assert understood.interpretation.intent == "general_support"
    assert understood.interpretation.support_topic == "address"


def test_the_explanation_compares_the_options_with_real_numbers(client, temp_db):
    """"Explained the timing from the solved route" is not an answer to "why Tuesday?".

    The answer is the comparison: this one adds a minute, that one adds sixteen. Both figures come
    from solves this run performed, and the "already nearby" clause is only attached when the
    driving figure supports it.
    """
    _confirmed(temp_db, "Anchor", "469123", TUESDAY)
    order_id = _order(client, postal_code="828761")
    _say(client, order_id, "Saturday morning works.")

    turn = _say(client, order_id, "Why Tuesday?")

    decision = turn["decision"]["decision"]
    assert "driving" in decision, decision
    assert any(ch.isdigit() for ch in decision), f"no figures in the answer: {decision}"
    assert "Explained the timing" not in decision


def test_no_nearby_claim_without_the_driving_to_support_it(client, temp_db):
    """"Already nearby" is a claim about the route, and it is only made when the added driving
    says so. An expensive option must not borrow the phrase."""
    _confirmed(temp_db, "Anchor", "469123", TUESDAY)
    order_id = _order(client, postal_code="828761")
    _say(client, order_id, "Saturday morning works.")

    turn = _say(client, order_id, "Why Tuesday?")
    decision = turn["decision"]

    if "already nearby" in decision["decision"]:
        cheapest = min(
            decision["candidates"], key=lambda c: c["added_drive_minutes"] or 0
        )
        assert (cheapest["added_drive_minutes"] or 0) <= 5, cheapest
        assert not cheapest["opens_new_day"]


def test_the_route_strip_has_the_position_it_needs_to_draw(client, temp_db):
    """The strip shows where the stop lands in the sequence. Without a position it cannot, and a
    route claim with no visible evidence is the thing this was added to fix."""
    _confirmed(temp_db, "Anchor", "469123", TUESDAY)
    order_id = _order(client, postal_code="828761")

    turn = _say(client, order_id, "Saturday morning works.")

    for candidate in turn["decision"]["candidates"]:
        assert candidate["position"], candidate
        assert candidate["stops_before"] is not None, candidate
        assert candidate["insertion"], "the insertion must be described in words too"


def test_a_confirmation_names_the_window_not_a_database_row(client, temp_db):
    """"Customer selected 2026-09-08" is a row. The panel's one quotable line must be readable."""
    from dispatch_agent.planning import plan_service

    _confirmed(temp_db, "Anchor", "469123", SATURDAY, window=(9, 0, 12, 0))
    plan_service.publish_plan_version(
        plan_service.solve_day(temp_db, SATURDAY), reason="Initial route for the day"
    )
    order_id = _order(client, postal_code="828761")
    _say(client, order_id, "I'm free Saturday, any time.")

    turn = _say(client, order_id, "Okay, take the first one.")

    changed = turn["decision"]["what_changed"]
    assert "Customer selected" in changed
    assert "2026-" not in changed, f"raw ISO date in the panel: {changed}"
    assert "am" in changed or "pm" in changed, changed
