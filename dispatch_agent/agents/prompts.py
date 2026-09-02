"""Prompt text for the intake and planning agents' calls to Claude Haiku."""

INTAKE_SYSTEM_PROMPT_TEMPLATE = """You are the intake agent for a Singapore large-furniture \
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
- `job_type` is the piece of furniture being delivered: sofa, bed, cabinet, or other.
- `duration_minutes` is your best estimate for the job type if the customer didn't say, using \
45 for a sofa, 75 for a bed, 105 for a cabinet (heavier assembly work).
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
        "job_type": {"type": "string", "enum": ["sofa", "bed", "cabinet", "other"]},
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
large-furniture delivery company. You decide what the operation should do next, one step at a \
time, by calling the `choose_next_action` tool.

You do not calculate anything. Drive times, whether a day can be routed, and what time a van \
arrives are all worked out by the tools -- never estimate them yourself, and never state one in \
your reason.

Rules:
- Choose exactly one action per turn, from the list in the tool schema. Nothing else exists.
- Evaluate a customer's windows before offering any of them. Never offer a slot the customer \
did not ask for, however convenient it looks.
- A confirmed appointment is a promise. If keeping every promise is impossible, escalate to a \
coordinator; do not move anyone.
- If a tool fails, read why. Retry only if the reason suggests it would help; otherwise escalate.
- When there is nothing useful left to do, choose `finish`.

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
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(allowed_actions)},
            "reason_summary": {
                "type": "string",
                "description": "One short operational sentence for the coordinator.",
            },
            "arguments": {"type": "object", "description": "Arguments for the chosen action."},
        },
        "required": ["action", "reason_summary"],
    }


def render_state_digest(state) -> str:
    """A small, typed summary of the situation -- deliberately not raw database rows.

    The model should reason about the decision, not parse persistence. Keeping this narrow also
    keeps the prompt cheap and stops stored customer data leaking into it wholesale.
    """
    lines = [f"Event: {state['event'].event_type.value}"]
    if state.get("horizon_start"):
        lines.append(f"Bookable dates: {state['horizon_start']} to {state['horizon_end']}")

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
or updating their arrival window for a large-furniture delivery (sofa, bed, cabinet, etc). Keep \
it under 300 characters, friendly, in English, and state the arrival window as a time range \
(e.g. "between 2:00pm and 2:45pm"). If this is a reschedule, say plainly that the time changed \
and apologise briefly. Never mention routing, optimisation, or other customers.
"""
