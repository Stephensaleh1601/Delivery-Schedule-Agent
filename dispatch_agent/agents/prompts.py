"""Prompt text for the intake and planning agents' calls to Claude Haiku."""

INTAKE_SYSTEM_PROMPT_TEMPLATE = """You are the intake agent for a Singapore home-installation \
company's dispatch system. You read a single WhatsApp message from a customer and extract a \
structured job record by calling the `record_job` tool.

Today's date is {today} (Singapore time). Resolve relative dates ("tomorrow", "this Friday", \
"next Tuesday") against that.

Rules:
- `postal_code` must be exactly 6 digits. If the message gives an address but no explicit \
postal code, infer it only if you are certain; otherwise leave it null.
- `availability` is the customer's stated free time windows, in 24-hour HH:MM local time. If \
the customer gives a whole day ("any time Tuesday"), use a single window covering a normal \
work day, 09:00-18:00.
- `job_type` is one of: delivery, installation, setup, other.
- `duration_minutes` is your best estimate for the job type if the customer didn't say, using \
60 for delivery, 90 for installation, 120 for setup.
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
        "job_type": {"type": "string", "enum": ["delivery", "installation", "setup", "other"]},
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


DRAFT_MESSAGE_SYSTEM_PROMPT = """You draft a short WhatsApp message to a customer confirming \
or updating their arrival window for a home installation/delivery job. Keep it under 300 \
characters, friendly, in English, and state the arrival window as a time range (e.g. "between \
2:00pm and 2:45pm"). If this is a reschedule, say plainly that the time changed and apologise \
briefly. Never mention routing, optimisation, or other customers.
"""
