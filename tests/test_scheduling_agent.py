"""The scheduling agent's guardrails.

These are the tests that matter for an autonomous loop: not "does it do the right thing when
scripted correctly", but "what happens when the model asks for something it shouldn't". A model
that invents an action, invents an argument, or never stops must not be able to do damage.
"""
from datetime import date, time

import pytest

from dispatch_agent import config
from dispatch_agent.agents.scheduling_agent import (
    MAX_TOOL_STEPS,
    ActionDecision,
    RuleDecisionAgent,
    ScriptedDecisionAgent,
    handle_planning_event,
)
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.models import (
    Address,
    AgentRunStatus,
    AvailabilityOption,
    JobRecord,
    JobType,
    PlanningEvent,
    PlanningEventType,
    PlanningStatus,
    TimeWindow,
)
from dispatch_agent.planning import policy_kb, tools
from dispatch_agent.planning.clock import PlanningClock

BASE = date(2026, 9, 2)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


def _w(start, end):
    return TimeWindow(start=time(*start), end=time(*end))


def _order(repo, name="Mrs Tan", postal_code="018956", option_count=2):
    days = PlanningClock.horizon_dates()
    # The cycle is two days, so a third option is a second window on the FIRST day rather than a
    # third date. The windows are chosen not to overlap within a day: two options covering the
    # same hours would be one choice wearing two hats, and a test asserting "another offer came
    # back" would pass on a duplicate.
    windows = [((9, 0), (13, 0)), ((9, 0), (13, 0)), ((13, 0), (18, 0))]
    job = JobRecord(
        customer_name=name,
        address=Address(raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)),
        job_type=JobType.SOFA,
        availability_options=[
            AvailabilityOption(
                date=days[i % len(days)], window=_w(*windows[i]), preference_rank=i + 1
            )
            for i in range(option_count)
        ],
        raw_message="booked via chat",
        duration_minutes=45,
    )
    repo.save_job(job)
    return job


def _event(order, event_type=PlanningEventType.NEW_ORDER, **payload):
    return PlanningEvent(event_type=event_type, order_id=order.id, payload=payload)


@pytest.mark.parametrize(
    ("search_scope", "expected", "forbidden"),
    [
        ("cluster", "find_normal_slot", "find_requested_day_slot"),
        ("requested", "find_requested_day_slot", "find_normal_slot"),
    ],
)
def test_a_live_decider_is_shown_only_the_customer_requested_route_scope(
    temp_db, search_scope, expected, forbidden
):
    order = _order(temp_db)
    stated = order.availability_options[0]

    class CaptureScope:
        def __init__(self):
            self.seen = []

        def decide(self, state, allowed):
            self.seen.append(set(allowed))
            if "record_availability" in allowed:
                return ActionDecision(
                    action="record_availability",
                    reason_summary="Recording the customer's exact words.",
                    arguments={"order_id": order.id},
                )
            return ActionDecision(action="finish", reason_summary="scope captured")

    decider = CaptureScope()
    handle_planning_event(
        _event(
            order,
            intent="provide_availability",
            search_scope=search_scope,
            stated_windows=[
                {
                    "date": stated.date.isoformat(),
                    "start": stated.window.start.strftime("%H:%M"),
                    "end": stated.window.end.strftime("%H:%M"),
                }
            ],
        ),
        repo=temp_db,
        decider=decider,
        use_fallback=False,
    )

    assert any(expected in legal for legal in decider.seen)
    assert all(forbidden not in legal for legal in decider.seen)


# -- guardrails ---------------------------------------------------------------


def test_an_action_that_is_not_on_the_list_is_refused_and_changes_nothing(temp_db):
    """The central guardrail. A model asking for something outside the allow-list must be
    refused visibly, not crash and not improvise."""
    order = _order(temp_db)
    before = temp_db.get_job(order.id).model_dump_json()

    run = handle_planning_event(
        _event(order),
        repo=temp_db,
        decider=ScriptedDecisionAgent([
            {"action": "delete_everything", "reason_summary": "should never run",
             "arguments": {"table": "jobs"}},
            {"action": "finish", "reason_summary": "stopping"},
        ]),
        use_fallback=False,
    )

    refusal = run.actions[0]
    assert refusal.ok is False
    assert refusal.error == "action_not_allowed"
    assert temp_db.get_job(order.id).model_dump_json() == before, "a refused action still changed data"


def test_invalid_arguments_are_rejected_before_the_tool_runs(temp_db):
    """extra="forbid" on every args model: a hallucinated field must not ride along into a
    service call."""
    order = _order(temp_db)

    run = handle_planning_event(
        _event(order),
        repo=temp_db,
        decider=ScriptedDecisionAgent([
            {"action": "evaluate_slots", "reason_summary": "checking",
             "arguments": {"order_id": order.id, "force_confirm": True}},
            {"action": "finish", "reason_summary": "stopping"},
        ]),
        use_fallback=False,
    )

    assert run.actions[0].ok is False
    assert run.actions[0].error == "invalid_arguments"


def test_the_step_limit_stops_the_loop_and_escalates(temp_db):
    """A model that never chooses `finish` must be stopped by us, and a human told."""
    order = _order(temp_db)

    run = handle_planning_event(
        _event(order),
        repo=temp_db,
        decider=ScriptedDecisionAgent(
            [{"action": "evaluate_slots", "reason_summary": "again", "arguments": {"order_id": order.id}}]
            * (MAX_TOOL_STEPS + 5)
        ),
        use_fallback=False,
    )

    assert run.status is AgentRunStatus.STEP_LIMIT_REACHED
    model_actions = [action for action in run.actions if action.decider != "controller"]
    assert len(model_actions) == MAX_TOOL_STEPS
    assert run.actions[-1].tool == "send_message", "the controller must still answer the customer"
    assert any(e.kind == "step_limit" for e in temp_db.open_exceptions())


def test_the_agent_never_confirms_a_slot_the_customer_did_not_choose(temp_db):
    """Locking requires an offer the customer responded to. A model cannot shortcut it."""
    order = _order(temp_db)

    run = handle_planning_event(
        _event(order),
        repo=temp_db,
        decider=ScriptedDecisionAgent([
            {"action": "lock_appointment", "reason_summary": "just confirm it",
             "arguments": {"offer_id": "made-up", "slot_id": "also-made-up"}},
            {"action": "finish", "reason_summary": "stopping"},
        ]),
        use_fallback=False,
    )

    assert run.actions[0].ok is False
    assert temp_db.get_job(order.id).planning_status is PlanningStatus.PENDING_PLANNING
    assert temp_db.get_job(order.id).locked_window is None


def test_a_replayed_event_does_not_run_twice(temp_db):
    """Events are keyed by id, so a retried webhook or double submission is a no-op."""
    order = _order(temp_db)
    event = _event(order)

    first = handle_planning_event(event, repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)
    second = handle_planning_event(event, repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)

    assert second.id == first.id
    assert len(temp_db.offers_for_order(order.id)) == 1, "replaying the event made a second offer"


def test_graph_exception_is_redacted_before_the_run_is_persisted(temp_db, monkeypatch):
    from dispatch_agent.agents import scheduling_agent

    order = _order(temp_db)
    secret = "AKIAIOSFODNN7EXAMPLE"

    class BrokenGraph:
        def invoke(self, *args, **kwargs):
            raise RuntimeError(f"AWS failed for {secret} token=supersecrettoken")

    monkeypatch.setattr(scheduling_agent, "build_graph", lambda *args, **kwargs: BrokenGraph())

    run = handle_planning_event(
        _event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False
    )

    persisted = temp_db.get_agent_run(run.id)
    assert secret not in persisted.final_summary
    assert "supersecrettoken" not in persisted.final_summary
    assert "[redacted]" in persisted.final_summary


# -- the flows ----------------------------------------------------------------


def test_new_order_evaluates_then_offers_then_messages(temp_db):
    order = _order(temp_db)

    run = handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)

    assert run.status is AgentRunStatus.COMPLETED
    assert [a.tool for a in run.actions] == ["evaluate_slots", "create_offer", "send_message", "finish"]
    assert all(a.ok for a in run.actions)
    assert temp_db.get_job(order.id).planning_status is PlanningStatus.OFFERED
    assert temp_db.messages(order.id), "the customer was never actually told"


def test_policy_search_keeps_the_customers_exact_question(temp_db):
    """A model omitting the query must not turn a clear policy question into an escalation."""
    order = _order(temp_db)
    question = "So you can only do Saturday?"

    run = handle_planning_event(
        _event(
            order,
            PlanningEventType.MANUAL_RETRY,
            intent="policy_question",
            question=question,
            message=question,
        ),
        repo=temp_db,
        # This incomplete call reproduces what the live model returned. The controller should
        # bypass it while search_delivery_policy is the only legal action.
        decider=ScriptedDecisionAgent([
            {
                "action": "search_delivery_policy",
                "reason_summary": "searching",
                "arguments": {"order_id": order.id},
            }
        ]),
        use_fallback=False,
    )

    assert run.actions[0].tool == "search_delivery_policy"
    assert run.actions[0].ok is True
    assert run.actions[0].arguments["question"] == question
    assert all(a.tool != "escalate_booking" for a in run.actions)
    assert temp_db.messages(order.id), "the policy answer was never sent"


def test_saturday_only_question_retrieves_the_normal_day_exception():
    hits = policy_kb.search("So you can only do Saturday?")

    assert hits
    assert hits[0].id == "CLUSTER-5"


def test_acceptance_locks_the_appointment_and_publishes_a_plan(temp_db):
    order = _order(temp_db)
    handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)
    offer = temp_db.offers_for_order(order.id)[0]
    slot = offer.options[0]

    run = handle_planning_event(
        _event(order, PlanningEventType.CUSTOMER_ACCEPTED_OFFER, offer_id=offer.id, slot_id=slot.id),
        repo=temp_db,
        decider=ScriptedDecisionAgent([
            {
                "action": "confirm_offer",
                "reason_summary": "bad model arguments",
                "arguments": {"confirm_offer": True},
            }
        ]),
        use_fallback=False,
    )

    assert run.status is AgentRunStatus.COMPLETED
    job = temp_db.get_job(order.id)
    assert job.planning_status is PlanningStatus.CONFIRMED
    assert job.is_locked and job.delivery_date == slot.date
    assert temp_db.active_plan(slot.date) is not None
    assert run.actions[0].tool == "confirm_offer"
    failed = [(action.tool, action.error, action.arguments) for action in run.actions if not action.ok]
    assert not failed, failed


def test_rejection_leads_to_another_offer(temp_db):
    """A rejection must produce a different TIME, which is not the same as a different day.

    This assertion used to be "a different availability option", which was right only while the
    offered window was the customer's whole day. Now that the offer is a narrow window carved out
    of that day, declining 10-12 on Friday leaves the rest of Friday genuinely available -- and
    re-offering it is the correct behaviour, not a repeat. What must never come back is the exact
    window they turned down.
    """
    order = _order(temp_db, option_count=3)
    handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)
    first = temp_db.offers_for_order(order.id)[0]

    handle_planning_event(
        _event(order, PlanningEventType.CUSTOMER_REJECTED_OFFER, offer_id=first.id),
        repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False,
    )

    offers = temp_db.offers_for_order(order.id)
    assert len(offers) == 2, "a rejection should produce a second offer, not a dead end"
    declined = {(s.date, s.window.start, s.window.end) for s in offers[0].options}
    repeated = [s for s in offers[1].options if (s.date, s.window.start, s.window.end) in declined]
    assert not repeated, f"re-offered a time the customer already turned down: {repeated}"


def test_a_declined_time_is_excluded_from_the_day_it_came_from(temp_db):
    """The mechanism behind the test above, checked on the order itself: the declined window is
    carved out of the availability option, so the same date is re-solved with a hole in it rather
    than abandoned."""
    order = _order(temp_db, option_count=3)
    handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)
    first = temp_db.offers_for_order(order.id)[0]
    declined = first.options[0]

    handle_planning_event(
        _event(order, PlanningEventType.CUSTOMER_REJECTED_OFFER,
               offer_id=first.id, slot_id=declined.id),
        repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False,
    )

    stored = temp_db.get_job(order.id)
    option = next(o for o in stored.availability_options if o.id == declined.availability_option_id)
    assert declined.window in option.excluded_windows
    # ...and the day is still on the table, not written off.
    assert option.bookable_windows(min_width=stored.duration_minutes + 30)


def test_the_negotiation_reaches_a_human_rather_than_petering_out(temp_db):
    """Two rounds is the cap, and hitting it must escalate rather than end the run quietly.

    This used to assert that exhausting the customer's OWN windows escalates. That stopped being
    true, and rightly so: the agent now searches the horizon for a route-friendly alternative, so
    "every time you gave me is taken" is answered with a different day rather than a shrug. What
    ends a negotiation is the round cap -- which is the guardrail against pestering, and is counted
    from persisted offers rather than remembered.
    """
    order = _order(temp_db, option_count=2)
    handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)

    for _ in range(2):
        latest = max(temp_db.offers_for_order(order.id), key=lambda o: o.round_number)
        run = handle_planning_event(
            _event(order, PlanningEventType.CUSTOMER_REJECTED_OFFER, offer_id=latest.id),
            repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False,
        )

    rounds = [o.round_number for o in temp_db.offers_for_order(order.id)]
    assert max(rounds) <= 2, f"offered a third round: {rounds}"
    assert any(a.tool == "create_exception" and a.ok for a in run.actions), (
        "the order must reach a coordinator, not simply stop being answered"
    )
    assert temp_db.open_exceptions()


def test_a_rejection_looks_at_other_days_before_giving_up(temp_db):
    """The behaviour that replaced it. A customer whose stated windows are all narrow and now
    excluded is offered another day, not an apology."""
    order = _order(temp_db, option_count=1)
    days = PlanningClock.horizon_dates()
    order.availability_options = [
        AvailabilityOption(date=days[0], window=_w((9, 0), (11, 0)), preference_rank=1)
    ]
    temp_db.save_job(order)

    handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)
    first = temp_db.offers_for_order(order.id)[0]

    run = handle_planning_event(
        _event(order, PlanningEventType.CUSTOMER_REJECTED_OFFER, offer_id=first.id),
        repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False,
    )

    assert "suggest_route_aware_windows" in [a.tool for a in run.actions]
    offers = temp_db.offers_for_order(order.id)
    assert len(offers) == 2, "a second offer should have been made from another day"


def test_the_activity_log_records_real_outcomes_not_narration(temp_db):
    """The coordinator-facing log must reflect what the tools actually did."""
    order = _order(temp_db)

    run = handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)

    persisted = temp_db.agent_runs()[0]
    assert [a.tool for a in persisted.actions] == [a.tool for a in run.actions]
    evaluate = next(a for a in persisted.actions if a.tool == "evaluate_slots")
    assert "can be delivered" in evaluate.summary
    assert evaluate.data["feasible_count"] >= 1
    for action in persisted.actions:
        assert len(action.reason_summary) <= 240, "reason summaries must stay one-liners"


def test_the_loop_falls_back_to_standard_procedure_when_the_model_is_unavailable(temp_db):
    """A Bedrock outage mid-demo should degrade, not derail -- and must say so rather than
    implying the model made the calls."""
    order = _order(temp_db)

    class BrokenDecider:
        def decide(self, state, allowed):
            raise RuntimeError("bedrock unavailable")

    run = handle_planning_event(_event(order), repo=temp_db, decider=BrokenDecider(), use_fallback=True)

    assert run.status is AgentRunStatus.COMPLETED
    assert [a.tool for a in run.actions] == ["evaluate_slots", "create_offer", "send_message", "finish"]

    # Not one step may be attributed to the model. Every step was either taken by the standard
    # procedure -- which says so in its own words -- or by the controller, which takes the only
    # legal move when there is exactly one and does not pretend a judgement was exercised.
    for action in run.actions:
        assert action.decider in ("RuleDecisionAgent", "controller"), action.decider
        if action.decider == "RuleDecisionAgent":
            assert "model unavailable" in action.reason_summary
    assert any(a.decider == "RuleDecisionAgent" for a in run.actions)


def test_reason_summaries_are_truncated_not_trusted(temp_db):
    decision = ActionDecision(action="finish", reason_summary="x " * 400)
    assert len(decision.reason_summary) <= 240


def test_the_run_records_which_provider_actually_decided(temp_db):
    """An inspector that says "the agent decided" without saying who is decorative. Nothing stored
    this before -- the only evidence of a fallback was a prefix inside a truncated string."""
    order = _order(temp_db)

    run = handle_planning_event(
        _event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False
    )

    assert run.decider == "RuleDecisionAgent"
    assert run.model_id is None
    assert run.decider_error is None
    assert temp_db.agent_runs()[0].decider == "RuleDecisionAgent"


def test_a_fallback_keeps_the_exception_that_caused_it(temp_db):
    """"No AWS credentials" and "the model returned garbage" are different problems, and a demo
    that cannot tell them apart cannot be debugged in the five minutes before it starts."""
    order = _order(temp_db)

    class BrokenDecider:
        def decide(self, state, allowed):
            raise RuntimeError("bedrock unavailable")

    run = handle_planning_event(_event(order), repo=temp_db, decider=BrokenDecider())

    assert run.decider == "BrokenDecider"
    assert "bedrock unavailable" in run.decider_error
    assert temp_db.agent_runs()[0].decider_error == run.decider_error


def test_the_digest_gives_the_model_every_identifier_it_must_produce(temp_db):
    """A live run had the model invent an `order_id` -- record_availability came back
    "unknown_order", and a send_message that reported success filed the reply against an order that
    does not exist, so the customer simply got silence.

    An identifier a model is required to produce but is never shown is a trap, not a test of the
    model. Everything a tool needs by id has to appear in the digest verbatim.
    """
    from dispatch_agent.agents.prompts import render_state_digest

    order = _order(temp_db, option_count=1)
    event = _event(order, PlanningEventType.CUSTOMER_REJECTED_OFFER,
                   offer_id="offer-123", slot_id="slot-456")

    digest = render_state_digest({"event": event, "actions": [], "step_count": 0})

    assert order.id in digest, "the order id must be quotable, not guessable"
    assert "offer-123" in digest
    assert "slot-456" in digest


def test_the_digest_lists_the_windows_the_customer_just_gave(temp_db):
    """Without these the model cannot know record_availability has anything to record, and a live
    run skipped straight to evaluating an order with nothing on it."""
    from dispatch_agent.agents.prompts import render_state_digest

    order = _order(temp_db, option_count=1)
    day = PlanningClock.horizon_dates()[0]
    event = _event(
        order,
        stated_windows=[{"date": day.isoformat(), "start": "09:00", "end": "13:00",
                         "phrase": "Saturday morning"}],
    )

    digest = render_state_digest({"event": event, "actions": [], "step_count": 0})

    assert day.isoformat() in digest
    assert "09:00-13:00" in digest
    assert "Saturday morning" in digest


def test_a_rejected_call_is_told_what_the_arguments_should_have_been(temp_db):
    """"Arguments were not valid" told the model nothing, so a live run repeated the identical bad
    call six times and burned the step budget without learning that it had written `message` where
    `body` was expected."""
    ctx = tools.ToolContext(repo=temp_db)

    result = tools.dispatch("send_message", {"message": "hello"}, ctx)

    assert not result.ok and result.error == "invalid_arguments"
    assert "body" in result.summary, "the summary must name the fields it actually takes"
    assert "order_id" in result.summary


def test_the_customer_facing_wording_is_not_the_models_to_write(temp_db):
    """A live run had gpt-4o-mini rewrite an offer into "Dear Mrs. Lee ... Best regards, The
    Delivery Team" -- fluent, and missing the route reason the message existed to carry.

    The specific window and the reason both come from the solved route. A model paraphrasing them
    is a model inventing the explanation, which is the one thing the architecture is built to stop.
    """
    order = _order(temp_db, option_count=1)

    run = handle_planning_event(
        _event(order),
        repo=temp_db,
        decider=ScriptedDecisionAgent([
            {"action": "evaluate_slots", "reason_summary": "checking",
             "arguments": {"order_id": order.id}},
            {"action": "create_offer", "reason_summary": "offering",
             "arguments": {"order_id": order.id}},
            {"action": "send_message", "reason_summary": "sending",
             "arguments": {"order_id": order.id,
                           "body": "Dear Mrs Tan, best regards, The Delivery Team"}},
            {"action": "finish", "reason_summary": "done"},
        ]),
        use_fallback=False,
    )

    assert run.status is AgentRunStatus.COMPLETED
    sent = temp_db.messages(order.id)[-1].body
    assert "Best regards" not in sent and "Dear Mrs Tan" not in sent
    assert "We can deliver" in sent, f"the prepared wording was replaced: {sent!r}"


def test_a_message_with_nothing_prepared_and_nothing_written_is_refused(temp_db):
    """The other side: send_message must not save an empty bubble."""
    ctx = tools.ToolContext(repo=temp_db)
    order = _order(temp_db, option_count=1)

    result = tools.dispatch("send_message", {"order_id": order.id}, ctx)

    assert not result.ok and result.error == "nothing_to_send"
    assert not temp_db.messages(order.id)


def test_an_unanswered_offer_cannot_be_locked_by_the_agent(temp_db):
    """The most serious thing a live model did: it called lock_appointment while its own question
    was still on the table, and succeeded -- booking a van to a customer who had not replied.

    No prompt reliably prevents that, and the failure is invisible to the person it happens to.
    The guard is structural: the accepted slot is set from the event, never claimed by a decider.
    """
    order = _order(temp_db)
    handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)
    offer = temp_db.offers_for_order(order.id)[0]
    slot = offer.options[0]

    run = handle_planning_event(
        # A NEW_ORDER event -- nobody has accepted anything -- with a decider that tries anyway.
        PlanningEvent(event_type=PlanningEventType.NEW_ORDER, order_id=order.id,
                      payload={"stated_windows": []}),
        repo=temp_db,
        decider=ScriptedDecisionAgent([
            {"action": "lock_appointment", "reason_summary": "confirming",
             "arguments": {"offer_id": offer.id, "slot_id": slot.id}},
            {"action": "finish", "reason_summary": "done"},
        ]),
        use_fallback=False,
    )

    assert run.actions[0].ok is False
    assert run.actions[0].error == "not_legal_yet"
    job = temp_db.get_job(order.id)
    assert job.planning_status is PlanningStatus.OFFERED
    assert job.locked_window is None, "an unanswered offer became a booking"


def test_locking_a_different_slot_than_the_one_accepted_is_refused(temp_db):
    """They said Tuesday; the model asks to lock Saturday. That is a van at the wrong door."""
    order = _order(temp_db)
    handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)
    offer = temp_db.offers_for_order(order.id)[0]
    accepted, other = offer.options[0], offer.options[-1]
    if accepted.id == other.id:
        pytest.skip("this scenario needs two offered slots")

    ctx = tools.ToolContext(repo=temp_db)
    ctx.accepted = (offer.id, accepted.id)
    result = tools.dispatch(
        "lock_appointment",
        {"offer_id": offer.id, "slot_id": other.id},
        ctx,
    )

    assert result.ok is False and result.error == "wrong_slot"
    assert temp_db.get_job(order.id).locked_window is None


def test_the_slot_the_customer_did_accept_still_locks(temp_db):
    """The guard must not break the thing it guards."""
    order = _order(temp_db)
    handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)
    offer = temp_db.offers_for_order(order.id)[0]
    slot = offer.options[0]

    handle_planning_event(
        _event(order, PlanningEventType.CUSTOMER_ACCEPTED_OFFER, offer_id=offer.id, slot_id=slot.id),
        repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False,
    )

    job = temp_db.get_job(order.id)
    assert job.planning_status is PlanningStatus.CONFIRMED
    assert job.locked_window == slot.window
