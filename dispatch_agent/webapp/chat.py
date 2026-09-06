"""Rule-based conversation engine behind the WhatsApp-style front face (static/client.html).

Deliberately not LLM-driven: booking and reschedule both collect structured fields, and an
interactive form/button flow is more reliable and cheaper than parsing free text for that --
the existing LLM-backed intake agent (dispatch_agent/agents/intake_agent.py) stays the path for
the original free-text WhatsApp scenario (scripts/run_demo.py), untouched by this module.

Stateless by design: the frontend round-trips a small `state` dict with every request instead
of the server holding conversation sessions, so a page refresh or server restart never strands a
conversation mid-flow -- the worst case is just starting over.
"""
from __future__ import annotations

from datetime import date as Date, time as Time

from pydantic import ValidationError

from dispatch_agent.db import JobsRepository
from dispatch_agent.models import DaySequence, JobStatus, Notification, RescheduleRequest, TimeWindow
from dispatch_agent.reschedule import apply_reschedule
from dispatch_agent.webapp.jobs_service import JobSubmission, JobSubmissionError, create_job_from_submission

COMPANY_NAME = "Majestic Fighters Fresh Pet Food"
GREETING = (
    f"Hi! This is {COMPANY_NAME}. I can help you book a fresh pet-food "
    "delivery or reschedule an existing one."
)
MAIN_MENU = [
    {"label": "Book a delivery", "value": "book"},
    {"label": "Reschedule my delivery", "value": "reschedule"},
]
MAX_RESCHEDULE_ATTEMPTS = 3


def _reply(messages: list[str], state: dict, buttons: list[dict] | None = None, form: str | None = None) -> dict:
    return {
        "messages": [{"from": "bot", "text": m} for m in messages],
        "buttons": buttons,
        "form": form,
        "state": state,
    }


def _menu_reply(messages: list[str]) -> dict:
    return _reply(messages, {"flow": None, "step": None}, buttons=MAIN_MENU)


def handle_event(event: dict) -> dict:
    etype = event.get("type")
    state = event.get("state") or {}
    step = state.get("step")

    if etype == "start":
        return _menu_reply([GREETING])

    if etype == "button" and event.get("value") == "menu":
        return _menu_reply(["What would you like to do?"])

    if etype == "button" and event.get("value") == "book":
        return _reply(
            ["Sure! Please fill in your delivery details below:"],
            {"flow": "booking", "step": "awaiting_form"},
            form="booking",
        )

    if etype == "button" and event.get("value") == "reschedule":
        return _reply(
            ["No problem -- what's the name or phone number on the booking?"],
            {"flow": "reschedule", "step": "awaiting_identity"},
        )

    if etype == "form_submit" and event.get("form") == "booking":
        return _handle_booking_submit(event.get("data") or {})

    if etype == "text" and step == "awaiting_identity":
        return _handle_identity_lookup(event.get("value") or "")

    if etype == "button" and step == "choosing_job":
        return _handle_job_chosen(event.get("value"))

    if etype == "form_submit" and event.get("form") == "reschedule_slot":
        return _handle_reschedule_submit(event.get("data") or {}, state)

    # Anything unrecognised while a form/choice is pending -- nudge back to it instead of
    # silently resetting the conversation the customer is partway through.
    if step == "awaiting_form":
        return _reply(["Please fill in the form above to continue."], state, form="booking")
    if step == "awaiting_new_slot":
        return _reply(["Please use the form above to suggest a new slot."], state, form="reschedule_slot")
    if step == "choosing_job":
        return _reply(["Please tap one of the bookings above."], state)

    return _menu_reply(["Sorry, I didn't quite get that. Here's what I can help with:"])


def _handle_booking_submit(data: dict) -> dict:
    try:
        payload = JobSubmission(**data)
        job = create_job_from_submission(payload, raw_message="[submitted via WhatsApp-style chat]")
    except ValidationError:
        return _reply(
            ["Hmm, please check that every field is filled in correctly and try again."],
            {"flow": "booking", "step": "awaiting_form"},
            form="booking",
        )
    except JobSubmissionError as exc:
        return _reply(
            [f"{exc} Please try again."],
            {"flow": "booking", "step": "awaiting_form"},
            form="booking",
        )

    return _menu_reply(
        [
            f"Thanks, {job.customer_name}! Your {job.job_type.value} delivery request for "
            f"{job.delivery_date.isoformat()} has been received. We'll confirm your exact "
            "arrival window closer to the date.",
            "Anything else I can help with?",
        ]
    )


def _handle_identity_lookup(query: str) -> dict:
    query = query.strip()
    if not query:
        return _reply(
            ["Could you share the name or phone number on the booking?"],
            {"flow": "reschedule", "step": "awaiting_identity"},
        )

    matches = [j for j in JobsRepository().find_jobs_by_customer(query) if j.status != JobStatus.REJECTED]
    if not matches:
        return _reply(
            [f'I couldn\'t find a booking under "{query}". Want to try a different name/phone, or book a new delivery?'],
            {"flow": None, "step": None},
            buttons=[{"label": "Try again", "value": "reschedule"}, {"label": "Book a delivery", "value": "book"}],
        )

    buttons = [
        {"label": f"{j.job_type.value.title()} -- {j.delivery_date.isoformat()}", "value": j.id} for j in matches[:8]
    ]
    return _reply(
        ["Found it! Which booking would you like to reschedule?"],
        {"flow": "reschedule", "step": "choosing_job"},
        buttons=buttons,
    )


def _handle_job_chosen(job_id: str | None) -> dict:
    job = JobsRepository().get_job(job_id) if job_id else None
    if job is None:
        return _menu_reply(["Hmm, I lost track of that booking -- let's start over."])

    return _reply(
        [
            f"Got it -- your {job.job_type.value} delivery is currently scheduled for "
            f"{job.delivery_date.isoformat()}. What new date and time window would you like?"
        ],
        {"flow": "reschedule", "step": "awaiting_new_slot", "job_id": job_id, "attempts": 0},
        form="reschedule_slot",
    )


def _handle_reschedule_submit(data: dict, state: dict) -> dict:
    job_id = state.get("job_id")
    attempts = state.get("attempts", 0)
    if not job_id:
        return _reply(
            ["Let's start over -- what's the name or phone on the booking?"],
            {"flow": "reschedule", "step": "awaiting_identity"},
        )

    repo = JobsRepository()
    job = repo.get_job(job_id)
    if job is None:
        return _menu_reply(["Hmm, I lost track of that booking -- let's start over."])

    try:
        new_date = Date.fromisoformat(data["new_date"])
        window_start = Time.fromisoformat(data["window_start"])
        window_end = Time.fromisoformat(data["window_end"])
    except (KeyError, ValueError):
        return _reply(["That date/time didn't look right -- please try again."], state, form="reschedule_slot")
    if window_end <= window_start:
        return _reply(["The end time must be after the start time -- please try again."], state, form="reschedule_slot")

    # Snapshot before apply_reschedule mutates the job -- these tell us whether a route plan
    # already exists for either date and is about to go stale.
    previous_date = job.delivery_date
    old_sequence_existed = _has_stops(repo.get_sequence(previous_date))
    new_sequence_existed = _has_stops(repo.get_sequence(new_date))

    try:
        plan = apply_reschedule(
            RescheduleRequest(
                job_id=job_id,
                new_date=new_date,
                new_availability=[TimeWindow(start=window_start, end=window_end)],
                raw_message="[reschedule requested via WhatsApp-style chat]",
            ),
            repo=repo,
            # The chat sends its own confirmation to the customer -- no need for the planning
            # agent's drafted WhatsApp messages here, and skipping them means this doesn't
            # depend on a working LLM provider just to re-sequence a day.
            draft_messages=False,
        )
    except ValueError as exc:
        attempts += 1
        if attempts >= MAX_RESCHEDULE_ATTEMPTS:
            return _menu_reply(
                [
                    f"Sorry, {exc} We're having trouble fitting that in automatically after a few "
                    "tries -- our team will reach out directly to sort out a time."
                ]
            )
        return _reply(
            [f"Sorry, that doesn't work: {exc} Could you suggest a different date or time window?"],
            {**state, "attempts": attempts},
            form="reschedule_slot",
        )

    stale_dates = sorted({d for d, existed in ((previous_date, old_sequence_existed), (new_date, new_sequence_existed)) if existed})
    if stale_dates:
        dates_text = " and ".join(d.isoformat() for d in stale_dates)
        repo.add_notification(
            Notification(
                message=(
                    f"{job.customer_name}'s {job.job_type.value} delivery moved from "
                    f"{previous_date.isoformat()} to {new_date.isoformat()}. The route plan for "
                    f"{dates_text} was already generated and is now out of date -- regenerate it."
                ),
                dates=stale_dates,
            )
        )

    return _menu_reply(
        [
            f"Done! Your delivery is now scheduled for {plan.delivery_date.isoformat()} between "
            f"{window_start.strftime('%H:%M')} and {window_end.strftime('%H:%M')}.",
            f"This affected {len(plan.affected_job_ids)} other stop(s) on that day -- our team "
            "will confirm the exact details with everyone.",
        ]
    )


def _has_stops(sequence: DaySequence | None) -> bool:
    return sequence is not None and bool(sequence.stops)
