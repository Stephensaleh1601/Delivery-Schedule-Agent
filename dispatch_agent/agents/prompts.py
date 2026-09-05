"""Prompt text for the intake and planning agents' calls to Claude Haiku."""

INTAKE_SYSTEM_PROMPT_TEMPLATE = """You are the intake agent for a Singapore fresh pet-food \
delivery company's dispatch system. You read a single WhatsApp message from a customer and \
extract a structured job record by calling the `record_job` tool.

Today's date is {today} (Singapore time). Resolve relative dates ("tomorrow", "this Friday", \
"next Tuesday") against that.

Rules:
- `postal_code` must be exactly 6 digits. If the message gives an address but no explicit \
postal code, infer it only if you are certain; otherwise leave it null.
- `availability` is the customer's stated free time windows, in 24-hour HH:MM local time. If \
the customer gives a whole day ("any time Tuesday"), use a single window covering a normal \
work day, 09:00-18:00.
- `job_type` is what is being delivered: `pet_food_box` for a recurring subscription box, `one_off_pet_order` for a single order, or `other`.
- `duration_minutes` is your best estimate if the customer didn't say, using \
10 for a subscription box, 15 otherwise. Fresh food is a doorstep handover, not an installation.
- Never invent a name, address, or date the message doesn't support -- leave the field null \
and it will be flagged for the coordinator instead of silently guessed.
"""

RECORD_JOB_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "customer_name": {"type": ["string", "null"]},
        "phone": {"type": ["string", "null"]},
        "address_raw_text": {"type": "string"},
        "postal_code": {"type": ["string", "null"]},
        "job_type": {"type": "string", "enum": ["pet_food_box", "one_off_pet_order", "other"]},
        "duration_minutes": {"type": "integer"},
        "delivery_date": {"type": "string", "description": "ISO date, YYYY-MM-DD"},
        "availability": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "HH:MM"},
                    "end": {"type": "string", "description": "HH:MM"},
                },
                "required": ["start", "end"],
            },
        },
        "notes": {"type": ["string", "null"]},
    },
    "required": ["address_raw_text", "job_type", "delivery_date", "availability"],
}


SCHEDULING_DECISION_SYSTEM_PROMPT = """You are the scheduling coordinator for a Singapore \
fresh pet-food delivery company. You decide what the operation should do next, one step at a \
time, by calling the `choose_next_action` tool.

You do not calculate anything. Drive times, whether a day can be routed, and what time a van \
arrives are all worked out by the tools -- never estimate them yourself, and never state one in \
your reason.

Rules:
- Choose exactly one action per turn, from the list in the tool schema. Nothing else exists.
- Evaluate a customer's windows before offering any of them.
- A confirmed appointment is a promise. If keeping every promise is impossible, escalate to a \
coordinator; do not move anyone.
- If a tool fails, read why. Retry only if the reason suggests it would help; otherwise escalate.
- When there is nothing useful left to do, choose `finish`.

The usual order for a customer who has just told you when they are free:

1. `record_availability` -- FIRST, and only when the digest below lists "Times the customer just \
gave". Pass those entries through unchanged as `windows`; do not edit, re-date or add to them. \
Skipping this step means the next step prices an order with nothing on it.
2. `evaluate_slots` -- solve their dates against the real routes.
3. `suggest_route_aware_windows` -- find days that suit the route. Skip it when they have said \
their timing is fixed.
4. `create_offer` -- put times to them. It decides for itself whether an alternative is worth \
raising; a time they asked for that we can serve is simply honoured.
5. `send_message` -- send the wording the previous step produced, verbatim.

When they have ACCEPTED a time: `lock_appointment`, using the `offer_id` and `slot_id` given in \
the digest, then `finish`. Nothing else -- the appointment is booked and the day republished by \
that one call, and the customer is sent a confirmation automatically.

When they have declined something: `record_rejection` (with the `slot_id` if one was given), then \
evaluate, then suggest, then offer, then send. A rejection applies to the TIME proposed, not to \
the whole day.

`send_message` sends the wording the previous step already produced. You do not write it: it \
carries the specific window and the reason from the solved route, and rewriting it drops both. \
Just call the action.

When they are asking why a time was chosen: `evaluate_slots`, then `explain_choice`.

When the message is unclear: `ask_clarification` with ONE specific question in the `question` \
argument, then `send_message`. Never guess a date.

`reason_summary` is one short sentence shown to the coordinator, describing what you are doing \
and why in operational terms ("Checking which of the three requested windows we can serve"). It \
is not private reasoning, and it must not mention scores, penalties or internal weightings.
"""


def action_decision_schema(allowed_actions: list[str]) -> dict:
    """The tool schema handed to the model.

    The action enum lives HERE rather than on the Python model. Constraining the Python type
    would make an out-of-list action unrepresentable, and the "never execute an unknown action"
    guardrail would become untestable -- the failure would surface as a parse error instead of a
    refusal we can log. In the schema it steers generation without preventing us from observing
    a model that ignores it.
    """
    from dispatch_agent.planning.tools import render_argument_help

    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(allowed_actions)},
            "reason_summary": {
                "type": "string",
                "description": "One short operational sentence for the coordinator.",
            },
            "arguments": {
                "type": "object",
                # The argument names, listed. This used to be a bare object with no properties, so
                # the model had to guess the field names -- and a live run guessed `message` for
                # `body` and left out `order_id` six times running, because extra="forbid" rejected
                # each attempt without ever saying what the right names were. Generated from the
                # Pydantic models, so it cannot drift from what is actually accepted.
                "description": (
                    "Arguments for the chosen action. Each action takes exactly these fields, and "
                    "no others -- anything else is rejected:\n" + render_argument_help()
                ),
            },
        },
        "required": ["action", "reason_summary", "arguments"],
    }


def render_state_digest(state) -> str:
    """A small, typed summary of the situation -- deliberately not raw database rows.

    The model should reason about the decision, not parse persistence. Keeping this narrow also
    keeps the prompt cheap and stops stored customer data leaking into it wholesale.
    """
    event = state["event"]
    lines = [f"Event: {event.event_type.value}"]
    # The id every tool needs, given verbatim. Without it the model has to invent an `order_id`,
    # and a live run did exactly that: `record_availability` came back "unknown_order", and a
    # `send_message` that "succeeded" filed the reply against an order that does not exist, so the
    # customer got silence. An identifier the model must produce but is never shown is a trap.
    if event.order_id:
        lines.append(f"order_id: {event.order_id}   (use this exact value for order_id)")
    if state.get("horizon_start"):
        lines.append(f"Bookable dates: {state['horizon_start']} to {state['horizon_end']}")

    # What this customer message actually said. Without it the model cannot know that
    # `record_availability` has anything to record, and skips straight to evaluating an order with
    # no windows on it -- which is exactly what a live run did before this was added.
    stated = event.payload.get("stated_windows")
    if stated:
        lines.append("Times the customer just gave (pass these to record_availability unchanged):")
        for window in stated:
            phrase = window.get("phrase")
            lines.append(
                f"  - {window['date']} {window['start']}-{window['end']}"
                + (f'  ("{phrase}")' if phrase else "")
            )
    if event.payload.get("is_fixed"):
        lines.append("They say this is their ONLY possible time -- do not suggest alternatives.")
    if event.payload.get("offer_id"):
        lines.append(f"Offer under discussion: {event.payload['offer_id']}")
    if event.payload.get("slot_id"):
        lines.append(f"Slot they were talking about: {event.payload['slot_id']}")
    if event.payload.get("intent"):
        lines.append(f"Their message was read as: {event.payload['intent']}")
    if event.payload.get("question"):
        lines.append(f"Suggested clarification to ask: {event.payload['question']}")

    order = state.get("order_summary")
    if order:
        lines.append(
            f"Order: {order['customer_name']}, {order['job_type']}, "
            f"{order['duration_minutes']} minutes, status {order['planning_status']}"
        )
        for option in order.get("options", []):
            lines.append(
                f"  - requested {option['date']} {option['start']}-{option['end']} "
                f"(preference {option['preference_rank']})"
            )
    if state.get("affected_date"):
        lines.append(f"Affected date: {state['affected_date']}")

    for action in state.get("actions", []):
        outcome = "ok" if action.ok else f"FAILED ({action.error})"
        lines.append(f"Step {action.step}: {action.tool} -> {outcome}. {action.summary}")

    steps_used = state.get("step_count", 0)
    lines.append(f"Steps used: {steps_used}. Choose the next action.")
    return "\n".join(lines)


DRAFT_MESSAGE_SYSTEM_PROMPT = """You draft a short WhatsApp message to a customer confirming \
or updating their arrival window for a fresh pet-food delivery. Keep \
it under 300 characters, friendly, in English, and state the arrival window as a time range \
(e.g. "between 2:00pm and 2:45pm"). If this is a reschedule, say plainly that the time changed \
and apologise briefly. Never mention routing, optimisation, or other customers.
"""
