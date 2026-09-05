"""The natural-language conversation endpoint: one typed message in, one agent run out.

Registered onto the main app as a router. Kept out of `main.py` because this is the only endpoint
with real conversational state, and it is easier to reason about the "exactly one run per message"
guarantee when the whole path is on one screen.

The guarantee, and why it is here rather than in a comment:

- **One run.** This function creates exactly one `PlanningEvent` and calls the agent once. The
  earlier duplicate-offer bug came from a client making two calls that each opened a negotiation
  with different slots; there is now one entry point and it is this one.
- **One offer round.** The round cap lives in `offer_service.create_offer`, counted from persisted
  rows. A second message cannot produce a third round even if the client retries.
- **No double-send.** An identical message arriving twice in a row is answered from what already
  happened rather than re-run.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from dispatch_agent.agents.scheduling_agent import LLMDecisionAgent, handle_planning_event
from dispatch_agent.agents.understanding import MessageReader
from dispatch_agent.db import JobsRepository
from dispatch_agent.models import (
    AgentRunStatus,
    MessageDirection,
    PlanningEvent,
    PlanningEventType,
)
from dispatch_agent.planning import conversation, offer_service, tools

router = APIRouter()


class InboundMessage(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    # Optional client-supplied key. A retry with the same key returns what happened the first time
    # rather than starting a second negotiation.
    client_message_id: str | None = None


def _slot_labels(offer) -> list[str]:
    return [
        f"{offer_service.format_date(s.date)}, {offer_service.format_window(s.window)}"
        for s in (offer.options if offer else [])
    ]


def _stated_labels(order) -> list[str]:
    return [
        f"{offer_service.format_date(o.date)} "
        f"{o.window.start:%H:%M}-{o.window.end:%H:%M}"
        for o in order.availability_options
    ]


@router.post("/api/orders/{order_id}/messages")
def receive_message(order_id: str, payload: InboundMessage) -> dict:
    """Handle one customer message, start to finish.

    Returns the whole turn -- the messages, the offer now on the table, the run that produced it and
    the evaluations behind it -- so the client renders exactly what was persisted and never has to
    ask a second question to find the trace.
    """
    repo = JobsRepository()
    order = repo.get_job(order_id)
    if order is None:
        raise HTTPException(404, "Order not found")

    body = payload.body.strip()
    if not body:
        raise HTTPException(400, "Empty message")

    # The double-tap guard. Checked before anything is written, so a retry cannot append a second
    # copy of the customer's words or open a second negotiation.
    if conversation.is_duplicate_send(repo, order_id, body):
        return _turn(repo, order_id, duplicate=True)

    inbound = conversation.record_inbound(repo, order_id, body)

    live_offer = conversation.open_offer(repo, order_id)
    reader = MessageReader()
    understanding = reader.read(
        body,
        has_open_offer=live_offer is not None,
        context_date=conversation.context_date(live_offer, order),
        offered=_slot_labels(live_offer),
        already_stated=_stated_labels(order),
    )
    said = understanding.interpretation

    event = _event_for(order_id, said, live_offer, order)
    if event is None:
        # An acceptance we could not pin to a slot. Asking is the only safe answer -- booking the
        # nearest guess puts a van at the wrong door, and the customer cannot see it coming.
        return _turn(repo, order_id, extra_run=None)

    ctx = tools.ToolContext(repo=repo)
    # ONE model call per customer message: the reading above. Execution is then deterministic.
    #
    # It used to be one model call per step, which is where 30-40 seconds a message came from --
    # six or seven round trips to decide things that were already implied ("you have just created
    # an offer; now send it"). Worse, each of those was a fresh chance to choose something wrong:
    # the three duplicate offer messages and the eight-tool "why this timing?" were both a model
    # being asked to decide again when there was nothing left to decide.
    #
    # The model still makes the decision that matters -- what the customer wants. What follows from
    # that is a procedure, and procedures do not need a language model.
    # The model chooses the actions, from the set the state gate says are legal right now.
    # `use_fallback=True` so a provider outage degrades to the standard procedure mid-run
    # instead of ending the conversation -- and says so, per step, in the activity log.
    run = handle_planning_event(
        event, repo=repo, ctx=ctx, decider=LLMDecisionAgent(), use_fallback=True
    )

    # Two providers, two fields. `decider`/`model_id` are set by the loop and say who chose the
    # actions; these say who read the sentence. They used to be the same field, which was
    # honest while the steps were rule-driven and would now claim the tools were picked by
    # whatever parsed the message.
    run.reader = understanding.decider
    run.reader_model_id = understanding.model_id
    run.reader_error = understanding.fallback_reason
    run.final_summary = run.final_summary or f"Read as: {said.intent}."
    repo.save_agent_run(run)

    return _turn(
        repo, order_id, extra_run=run, intent=said.intent,
        inbound_id=inbound.id, evaluations=ctx.evaluations,
        # The route-friendly alternatives this run found. Passed through so the panel can price
        # them properly rather than repeating the one-line summary the tool logged.
        suggestions=ctx.scratch.get("suggestions"),
    )


def _event_for(order_id: str, said, live_offer, order):
    """The planning event this message corresponds to, or None when we must ask instead."""
    if said.intent == "accept" and live_offer is not None:
        try:
            slot = conversation.resolve_accepted_slot(live_offer, said)
        except conversation.AmbiguousAcceptance as unclear:
            # They agreed to something we cannot identify. Two honest readings, and silence is
            # neither: if they named a usable time, treat it as a new suggestion of theirs and
            # re-solve; otherwise ask which of the offered times they meant.
            counter = _counter_proposal(said, live_offer)  # only from their own words
            if counter:
                return PlanningEvent(
                    event_type=PlanningEventType.NEW_ORDER,
                    order_id=order_id,
                    payload={
                        "intent": "provide_availability",
                        "stated_windows": counter,
                        "is_fixed": conversation.is_only_option(order),
                    },
                )
            return PlanningEvent(
                event_type=PlanningEventType.MANUAL_RETRY,
                order_id=order_id,
                payload={
                    "intent": "unclear",
                    "question": (
                        "Just to be sure — did you mean "
                        + " or ".join(unclear.options or _slot_labels(live_offer))
                        + "?"
                    ),
                },
            )
        return PlanningEvent(
            event_type=PlanningEventType.CUSTOMER_ACCEPTED_OFFER,
            order_id=order_id,
            payload={"offer_id": live_offer.id, "slot_id": slot.id, "intent": "accept"},
        )

    if said.intent == "reject" and live_offer is not None:
        # A rejection names the slot it is about. Without one -- "none of these work" -- the whole
        # offer is declined, which is a different and larger thing.
        slot_id = None if said.rejects_whole_day else (
            live_offer.options[0].id if live_offer.options else None
        )
        payload = {"offer_id": live_offer.id, "slot_id": slot_id, "intent": "reject"}
        # A counter-proposal in the same breath ("not 11, after 1?") is availability, and recording
        # it is what makes the re-solve land where they asked.
        if said.windows:
            payload["stated_windows"] = _windows_payload(said)
        return PlanningEvent(
            event_type=PlanningEventType.CUSTOMER_REJECTED_OFFER,
            order_id=order_id,
            payload=payload,
        )

    if said.intent == "provide_availability" and said.windows:
        return PlanningEvent(
            event_type=PlanningEventType.NEW_ORDER,
            order_id=order_id,
            payload={
                "intent": "provide_availability",
                "stated_windows": _windows_payload(said),
                "is_fixed": said.is_fixed or conversation.is_only_option(order),
            },
        )

    if said.intent == "explain":
        return PlanningEvent(
            event_type=PlanningEventType.MANUAL_RETRY,
            order_id=order_id,
            payload={"intent": "explain", "message": said.note},
        )

    if said.intent == "general_support":
        return PlanningEvent(
            event_type=PlanningEventType.MANUAL_RETRY,
            order_id=order_id,
            payload={
                "intent": "general_support",
                "topic": said.support_topic,
                "message": said.note,
                "question": _support_question(said),
            },
        )

    return PlanningEvent(
        event_type=PlanningEventType.MANUAL_RETRY,
        order_id=order_id,
        payload={"intent": "unclear", "question": _clarifying_question(said, live_offer)},
    )


def _support_question(said) -> str:
    """What to say to someone asking about something other than the timing.

    A delivery address is not a scheduling question -- it changes where the van goes, which changes
    every route it is on. "Which of those times would you like?" was the old answer, and it is the
    kind of reply that makes people stop trusting an automated agent entirely.
    """
    if said.support_topic == "address":
        return (
            "Of course -- what's the new postal code? I'll need to re-check the route for it, and "
            "a colleague will confirm the change with you."
        )
    if said.support_topic == "cancel":
        return "I'll pass that to a colleague, who will call you to sort it out."
    return "I can help with the delivery timing. For anything else a colleague will call you back."


def _counter_proposal(said, live_offer) -> list[dict] | None:
    """A time they named that is not one we offered, as availability on the day under discussion.

    "11am okay?" against a 9-11 slot is the customer proposing 11, not accepting 9. Read that way
    the day is re-solved around what they actually asked for, which is a far better answer than
    asking them to repeat themselves.
    """
    from dispatch_agent.planning import language

    if not live_offer or not live_offer.options:
        return None
    phrase = said.accepted_phrase or said.note or ""
    named = language.parse_time(phrase, assume_afternoon=True)
    if not named:
        return None

    day = live_offer.options[0].date
    window = language.clamp_to_working_day(
        language.TimeWindow(start=named, end=__import__("datetime").time(23, 59))
    )
    if window is None:
        return None
    return [{
        "date": day.isoformat(),
        "start": window.start.strftime("%H:%M"),
        "end": window.end.strftime("%H:%M"),
        "phrase": phrase,
        "preference_rank": 1,
    }]


def _windows_payload(said) -> list[dict]:
    return [
        {
            "date": w.date.isoformat(),
            "start": w.window.start.strftime("%H:%M"),
            "end": w.window.end.strftime("%H:%M"),
            "phrase": w.phrase,
            "preference_rank": w.preference_rank,
        }
        for w in said.windows
    ]


def _clarifying_question(said, live_offer) -> str:
    """One question, chosen for the situation rather than a generic apology."""
    if live_offer and live_offer.options:
        return (
            "Sorry -- just to be sure, which of those would you like: "
            + " or ".join(_slot_labels(live_offer))
            + "?"
        )
    if said.direction == "later":
        return "Sure -- how much later would suit you, and on which day?"
    if said.direction == "earlier":
        return "Of course -- roughly what time would you like, and on which day?"
    return "Sorry, I didn't catch that -- which day and roughly what time would suit you?"


def _decision_for(repo: JobsRepository, order_id: str, extra_run, evaluations, suggestions=None) -> dict:
    """The most recent run that decided something, as a rendered decision record.

    Walks backwards through this order's runs. The side panel went blank -- "Waiting for the
    customer" -- after a confirmed booking because it only ever showed the run from the current
    request, and a page load has no current request.
    """
    from dispatch_agent.planning import decision_record

    order = repo.get_job(order_id)
    # What the customer can accept right now. A run that made no offer cannot know this, and
    # guessing labelled a compared-only alternative as though it had been put to them.
    live = conversation.open_offer(repo, order_id)
    on_the_table = {
        (slot.date.isoformat(), slot.window.start.strftime("%H:%M"))
        for slot in (live.options if live else [])
    }
    candidates = []
    if extra_run is not None:
        candidates.append(extra_run)
    seen = {r.id for r in candidates}
    messages = repo.messages(order_id)
    for run in repo.agent_runs_by_id([m.run_id for m in messages if m.run_id]).values():
        if run.id not in seen:
            candidates.append(run)
    candidates.sort(key=lambda r: r.started_at, reverse=True)

    for run in candidates:
        record = decision_record.build(
            run,
            order=order,
            evaluations=evaluations if run is extra_run else None,
            suggestions=suggestions if run is extra_run else None,
            on_the_table=on_the_table,
            # The live offer, so the cards can be built from the evidence stored on its slots
            # rather than recomputed -- what the panel shows and what the customer was sent
            # then cannot drift apart.
            offer=live,
        )
        if record.meaningful:
            return {"decision": record.to_dict(), "decision_run_id": run.id}
    return {"decision": None, "decision_run_id": None}


def _turn(
    repo: JobsRepository,
    order_id: str,
    extra_run=None,
    intent: str = "unclear",
    duplicate: bool = False,
    inbound_id: str | None = None,
    evaluations=None,
    suggestions=None,
) -> dict:
    """The conversation as it now stands, read back from the database.

    Read back rather than assembled from what just happened, so what the client renders is exactly
    what a refresh would show. If those two could differ, the refresh test would be the only place
    anyone found out.
    """
    from dispatch_agent.webapp.main import _offer_to_dict, _run_to_dict  # noqa

    order = repo.get_job(order_id)
    messages = repo.messages(order_id)
    runs = repo.agent_runs_by_id([m.run_id for m in messages if m.run_id])
    if extra_run is not None:
        runs[extra_run.id] = extra_run
    offers = {o.id: o for o in repo.offers_for_order(order_id)}
    live = conversation.open_offer(repo, order_id)

    return {
        "order_id": order_id,
        "intent": intent,
        "duplicate": duplicate,
        "planning_status": order.planning_status.value if order else None,
        "confirmed": bool(order and order.is_locked),
        "delivery_date": order.delivery_date.isoformat() if order and order.delivery_date else None,
        "inbound_message_id": inbound_id,
        "messages": [
            {
                "id": m.id,
                "direction": m.direction.value,
                "body": m.body,
                "created_at": m.created_at.isoformat(),
                "run_id": m.run_id,
                "offer_id": m.offer_id,
            }
            for m in messages
        ],
        "runs": {rid: _run_to_dict(r) for rid, r in runs.items()},
        "offers": {oid: _offer_to_dict(o) for oid, o in offers.items()},
        "open_offer_id": live.id if live else None,
        "run": _run_to_dict(extra_run) if extra_run is not None else None,
        # The business decision, rebuilt from the persisted run rather than from whatever a
        # browser tab was holding. `decision_run_id` is the last run that actually decided
        # something -- a clarification question is not a decision, and after a confirmation the
        # panel must keep showing the confirmation rather than reverting to "waiting".
        **_decision_for(repo, order_id, extra_run, evaluations, suggestions),
        "error": (
            extra_run.final_summary
            if extra_run is not None and extra_run.status is not AgentRunStatus.COMPLETED
            else None
        ),
    }


@router.get("/api/orders/{order_id}/messages")
def list_messages(order_id: str) -> dict:
    """The persisted thread, for a client that has just reloaded."""
    if JobsRepository().get_job(order_id) is None:
        raise HTTPException(404, "Order not found")
    return _turn(JobsRepository(), order_id)
