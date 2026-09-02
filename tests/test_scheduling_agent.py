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
from dispatch_agent.planning.clock import PlanningClock

BASE = date(2026, 9, 2)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


def _w(start, end):
    return TimeWindow(start=time(*start), end=time(*end))


def _order(repo, name="Mrs Tan", postal_code="018956", option_count=2):
    days = PlanningClock.horizon_dates()
    windows = [((9, 0), (13, 0)), ((13, 0), (18, 0)), ((9, 0), (18, 0))]
    job = JobRecord(
        customer_name=name,
        address=Address(raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)),
        job_type=JobType.SOFA,
        availability_options=[
            AvailabilityOption(date=days[i], window=_w(*windows[i]), preference_rank=i + 1)
            for i in range(option_count)
        ],
        raw_message="booked via chat",
        duration_minutes=45,
    )
    repo.save_job(job)
    return job


def _event(order, event_type=PlanningEventType.NEW_ORDER, **payload):
    return PlanningEvent(event_type=event_type, order_id=order.id, payload=payload)


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
    assert len(run.actions) == MAX_TOOL_STEPS, f"ran {len(run.actions)} steps against a cap of {MAX_TOOL_STEPS}"
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


# -- the flows ----------------------------------------------------------------


def test_new_order_evaluates_then_offers_then_messages(temp_db):
    order = _order(temp_db)

    run = handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)

    assert run.status is AgentRunStatus.COMPLETED
    assert [a.tool for a in run.actions] == ["evaluate_slots", "create_offer", "send_message", "finish"]
    assert all(a.ok for a in run.actions)
    assert temp_db.get_job(order.id).planning_status is PlanningStatus.OFFERED
    assert temp_db.messages(order.id), "the customer was never actually told"


def test_acceptance_locks_the_appointment_and_publishes_a_plan(temp_db):
    order = _order(temp_db)
    handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)
    offer = temp_db.offers_for_order(order.id)[0]
    slot = offer.options[0]

    run = handle_planning_event(
        _event(order, PlanningEventType.CUSTOMER_ACCEPTED_OFFER, offer_id=offer.id, slot_id=slot.id),
        repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False,
    )

    assert run.status is AgentRunStatus.COMPLETED
    job = temp_db.get_job(order.id)
    assert job.planning_status is PlanningStatus.CONFIRMED
    assert job.is_locked and job.delivery_date == slot.date
    assert temp_db.active_plan(slot.date) is not None


def test_rejection_leads_to_another_offer(temp_db):
    """Three windows, two offered first time round -- so there is genuinely a third to fall back
    on. (With only two options this correctly escalates instead; see the test below.)"""
    order = _order(temp_db, option_count=3)
    handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)
    first = temp_db.offers_for_order(order.id)[0]

    handle_planning_event(
        _event(order, PlanningEventType.CUSTOMER_REJECTED_OFFER, offer_id=first.id),
        repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False,
    )

    offers = temp_db.offers_for_order(order.id)
    assert len(offers) == 2, "a rejection should produce a second offer, not a dead end"
    already = {s.availability_option_id for s in offers[0].options}
    assert not any(s.availability_option_id in already for s in offers[1].options), (
        "the second offer repeated a slot the customer already turned down"
    )


def test_rejecting_the_last_available_slot_escalates_rather_than_giving_up(temp_db):
    """When every window the customer gave has been tried, the order must reach a human --
    quietly ending the run would abandon it."""
    order = _order(temp_db, option_count=2)
    handle_planning_event(_event(order), repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False)
    first = temp_db.offers_for_order(order.id)[0]

    run = handle_planning_event(
        _event(order, PlanningEventType.CUSTOMER_REJECTED_OFFER, offer_id=first.id),
        repo=temp_db, decider=RuleDecisionAgent(), use_fallback=False,
    )

    assert any(a.tool == "create_exception" and a.ok for a in run.actions)
    assert any(e.kind == "no_remaining_slot" for e in temp_db.open_exceptions())


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
    assert all("model unavailable" in a.reason_summary for a in run.actions)


def test_reason_summaries_are_truncated_not_trusted(temp_db):
    decision = ActionDecision(action="finish", reason_summary="x " * 400)
    assert len(decision.reason_summary) <= 240
