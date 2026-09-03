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

from dispatch_agent.agents.scheduling_agent import handle_planning_event
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
        context_date=conversation.context_date(live_offer),
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
    run = handle_planning_event(event, repo=repo, ctx=ctx)

    # Provenance of the READING, which is a separate decision from the tool choices and has its own
    # provider. Recorded on the run so the inspector can say the message was understood by the model
    # even when the steps then ran from the standard procedure, or vice versa.
    run.decider_error = run.decider_error or understanding.fallback_reason
    if understanding.used_model:
        run.model_id = run.model_id or understanding.model_id
    run.final_summary = run.final_summary or f"Read as: {said.intent}."
    repo.save_agent_run(run)

    return _turn(repo, order_id, extra_run=run, intent=said.intent, inbound_id=inbound.id)


def _event_for(order_id: str, said, live_offer, order):
    """The planning event this message corresponds to, or None when we must ask instead."""
    if said.intent == "accept" and live_offer is not None:
        try:
            slot = conversation.resolve_accepted_slot(live_offer, said)
        except conversation.AmbiguousAcceptance:
            return None
        return PlanningEvent(
            event_type=PlanningEventType.CUSTOMER_ACCEPTED_OFFER,
            order_id=order_id,
            payload={"offer_id": live_offer.id, "slot_id": slot.id},
        )

    if said.intent == "reject" and live_offer is not None:
        # A rejection names the slot it is about. Without one -- "none of these work" -- the whole
        # offer is declined, which is a different and larger thing.
        slot_id = None if said.rejects_whole_day else (
            live_offer.options[0].id if live_offer.options else None
        )
        payload = {"offer_id": live_offer.id, "slot_id": slot_id}
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

    return PlanningEvent(
        event_type=PlanningEventType.MANUAL_RETRY,
        order_id=order_id,
        payload={"intent": "unclear", "question": _clarifying_question(said, live_offer)},
    )


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


def _turn(
    repo: JobsRepository,
    order_id: str,
    extra_run=None,
    intent: str = "unclear",
    duplicate: bool = False,
    inbound_id: str | None = None,
) -> dict:
    """The conversation as it now stands, read back from the database.

    Read back rather than assembled from what just happened, so what the client renders is exactly
    what a refresh would show. If those two could differ, the refresh test would be the only place
    anyone found out.
    """
    from dispatch_agent.webapp.main import _offer_to_dict, _run_to_dict, _evaluation_to_dict  # noqa

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
