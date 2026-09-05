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


SCHEDULING_DECISION_SYSTEM_PROMPT = """You are the scheduling coordinator for a Singapore fresh pet-food delivery company. The food is made fresh and cannot be left at the door, so somebody has to be home -- which is why a delivery time is agreed with the customer rather than announced at them.

Each turn you make ONE judgement: which situation is this? Then you call the action for it. What happens inside that action is already decided -- you are not assembling a procedure out of small steps.

THE DELIVERY DAYS

Friday covers North, North-East, South and East. Saturday covers Central, City and West. Every address has one normal delivery day, and the digest below tells you which one this customer has.

WHICH SITUATION IS THIS?

- They told you when they are free, or said they are flexible, and did not ask for a particular day -> `find_normal_slot`. It searches their own day and offers the best proven slot on it.
- They explicitly asked for a specific day -> `record_availability` first, then `find_requested_day_slot`. It searches that day and nothing else, and says plainly if it will not work.
- They turned down what you offered -> `record_rejection`, then `find_fallback_options`. It searches both routes and offers the calculated top three.
- They accepted one of the times you offered -> `confirm_offer`.
- They asked why -> `explain_offer`. A question changes nothing about the booking.
- You cannot tell what they mean -> `ask_clarification`, ONE specific question. Never guess a date.
- Nothing fits, or the search came back with too few options -> `escalate_booking`.

You do not choose which routes get searched. That is fixed by the action you pick, so pick the one that matches what the customer actually said.

BOUNDARIES -- these are not preferences

- You never calculate a distance, a drive time, an arrival, or whether a day still works. The tools do that. Never put such a number in your reason.
- You never reorder, re-select or replace what a tool returned. Three options in an order IS the answer.
- You never promise a time without tool evidence behind it.
- You never offer a day or time the customer has ruled out. If they said a day is their only option, that is a restriction: search it, and escalate if it cannot take them.

FINISHING

Every customer message gets exactly one reply. Once an action has produced an outcome -- an offer, an explanation, a question, an escalation -- call `send_message`, then `finish`. Never end a turn silently: a person watching a thread that stopped answering cannot tell you from a broken server.

Only the actions in the tool schema exist, and that list changes as the booking moves forward. If something you expected is missing, it is not allowed yet -- pick from what is there.

`reason_summary` is one short operational sentence for a coordinator ("Checking their normal Friday route"). Not private reasoning, and never scores or internal weightings.
"""


def action_decision_schema(allowed_actions: list[str]) -> dict:
    """The tool schema handed to the model.

    The action enum lives HERE rather than on the Python model. Constraining the Python type
    would make an out-of-list action unrepresentable, and the "never execute an unknown action"
    guardrail would become untestable -- the failure would surface as a parse error instead of a
    refusal we can log. In the schema it steers generation without preventing us from observing
    a model that ignores it.
    """
    from dispatch_agent.planning.tools import guide_for, render_argument_help

    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(allowed_actions),
                # Described here rather than in the system prompt because the permitted set
                # changes every turn: describing all sixteen up front would spend the prompt on
                # actions that are not available and leave the available ones undescribed.
                "description": "Choose one:\n" + guide_for(list(allowed_actions)),
            },
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
    placement = state.get("placement")
    if placement:
        lines.append(
            f"Customer region: {placement['region']} -- their normal delivery day is "
            f"{placement['normal_day']}."
        )
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
