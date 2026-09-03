"""Reading a customer's message with a model, and falling back honestly when it is unavailable.

The model's job here is narrow on purpose: decide *what the customer wants* and quote the phrases
that say when they are free. It does not compute dates, times, drive times or windows. Every phrase
it returns is resolved by `planning.language` against the Singapore planning clock, so a model that
confidently answers "next Tuesday is the 15th" cannot put that date into the system -- the phrase is
re-parsed and the arithmetic is ours.

That split is what makes the whole thing testable offline. `interpret()` below is the only place a
provider is touched, and it always returns the same typed `Interpretation` the deterministic parser
produces, with `decider` saying which one wrote it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date

from dispatch_agent.config import settings
from dispatch_agent.llm import LLMClient, build_llm_client
from dispatch_agent.planning import language
from dispatch_agent.planning.clock import PlanningClock

UNDERSTANDING_SYSTEM_PROMPT = """You are reading one WhatsApp message from a customer of a \
Singapore furniture delivery company, and classifying what they want by calling the \
`read_message` tool.

You do NOT schedule anything, calculate anything, or decide what we can deliver. You only say what \
the customer is asking for. Times and dates are resolved by the system from the phrases you quote.

Choose exactly one `intent`:
- `provide_availability` -- they are telling us when they are free, or changing what they said \
before.
- `accept` -- they are agreeing to a time we already proposed.
- `reject` -- they are turning down a time we proposed, with or without suggesting another.
- `explain` -- they are asking why a time was chosen, or why another is not possible.
- `general_support` -- they are asking about something that is not the timing at all: changing the \
delivery ADDRESS, cancelling, what it costs, or wanting to speak to a person. These messages often \
contain scheduling-looking words ("change", "can I", a question mark) and are still not about when \
we deliver.
- `unclear` -- anything else, including messages you are not confident about. Choosing this is \
always better than guessing: a wrong date costs the customer a delivery day.

For `availability_phrases`, quote the customer's own words for each separate time they say they \
COULD do -- "Saturday morning", "after 1 on Tuesday". Rules:
- Only times they are offering. A time they are refusing ("not Saturday morning") is not \
availability.
- Do not invent, extend or tidy a phrase. Quote what they wrote.
- Order them as the customer ranked them, best first, if they expressed a preference.
- A time WE proposed is never availability, even if they are discussing it.

Set `is_fixed` true ONLY when the customer explicitly says no other time is possible -- "that's \
the only time I can do", "I can only do Saturday", "no other time works". Simply telling us when \
they are free is NOT fixed: "I'm free Saturday morning" is availability, not an ultimatum. If you \
are unsure, answer false.

For `accept`, set `refers_to` to the words identifying which option they mean -- "the first one", \
"Tuesday", "1pm" -- or leave it null if they simply agreed without saying which.

For `reject`, set `rejects_whole_day` true only if they ruled out the entire day rather than the \
specific time offered.
"""


def read_message_schema() -> dict:
    """The tool schema. Enums steer the model; nothing here accepts a computed date or time."""
    return {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": sorted(language.INTENTS),
            },
            "availability_phrases": {
                "type": "array",
                "description": "The customer's own words for each time they say they could do. "
                               "Quoted verbatim, never resolved to a date.",
                "items": {"type": "string"},
            },
            "is_fixed": {
                "type": "boolean",
                "description": "True only if they say this is their ONLY possible time.",
            },
            "refers_to": {
                "type": ["string", "null"],
                "description": "For an acceptance: the words identifying which offered option.",
            },
            "rejects_whole_day": {
                "type": "boolean",
                "description": "For a rejection: true only if the whole day is ruled out.",
            },
        },
        "required": ["intent"],
    }


def render_context(
    message: str,
    has_open_offer: bool,
    offered: list[str] | None = None,
    already_stated: list[str] | None = None,
) -> str:
    """What the model is told. Deliberately small: the message, and what is on the table.

    No stored customer data beyond this order's own conversation, and no other customer's anything.
    """
    today = language.today()
    first, last = PlanningClock.horizon()
    lines = [
        f"Today is {today:%A, %d %B %Y} (Singapore).",
        f"We can book deliveries between {first:%A %d %B} and {last:%A %d %B}.",
    ]
    if already_stated:
        lines.append("Times this customer has already given us: " + "; ".join(already_stated))
    if has_open_offer and offered:
        lines.append("Times we have just proposed to them: " + "; ".join(offered))
    elif not has_open_offer:
        lines.append("We have not proposed any time yet.")
    lines.append("")
    lines.append(f"Customer's message: {message}")
    return "\n".join(lines)


@dataclass
class Understanding:
    """An interpretation plus the provenance of who produced it."""

    interpretation: language.Interpretation
    decider: str
    model_id: str | None = None
    fallback_reason: str | None = None

    @property
    def used_model(self) -> bool:
        return self.fallback_reason is None and self.model_id is not None


class MessageReader:
    """Reads one customer message, with the deterministic parser underneath.

    Constructed per request. The provider client is built lazily inside `read` so a missing
    credential degrades that one message rather than failing at import and taking the app with it.
    """

    def __init__(self, llm: LLMClient | None = None, use_model: bool | None = None):
        self._llm = llm
        # An injected client is always used; otherwise the provider setting decides. LLM_PROVIDER
        # =none is how the test suite guarantees no provider is reached, and how a demo machine
        # with no credentials still runs the whole conversation.
        if use_model is None:
            use_model = llm is not None or settings.llm_provider not in ('none', '')
        self._use_model = use_model

    def read(
        self,
        message: str,
        has_open_offer: bool = False,
        context_date: Date | None = None,
        offered: list[str] | None = None,
        already_stated: list[str] | None = None,
    ) -> Understanding:
        deterministic = language.interpret(
            message, has_open_offer=has_open_offer, context_date=context_date
        )

        if not self._use_model:
            return Understanding(deterministic, decider="RuleMessageReader")

        try:
            client = self._llm or build_llm_client()
            raw = client.extract_structured(
                system=UNDERSTANDING_SYSTEM_PROMPT,
                user=render_context(message, has_open_offer, offered, already_stated),
                tool_name="read_message",
                tool_schema=read_message_schema(),
            )
        except Exception as exc:  # noqa: BLE001 -- any provider failure degrades, never 500s
            from dispatch_agent.planning.tools import redact_secrets

            return Understanding(
                deterministic,
                decider="RuleMessageReader",
                fallback_reason=redact_secrets(f"{type(exc).__name__}: {exc}"),
            )

        merged = self._merge(raw, deterministic, has_open_offer, context_date, message)
        return Understanding(
            merged,
            decider="LLMMessageReader",
            model_id=(
                settings.bedrock_model_id
                if settings.llm_provider == "bedrock"
                else settings.openai_model
            ),
        )

    @staticmethod
    def _merge(
        raw: dict,
        deterministic: language.Interpretation,
        has_open_offer: bool,
        context_date: Date | None,
        message: str = "",
    ) -> language.Interpretation:
        """The model's reading, with every date and time re-derived from its quoted phrases.

        This is the safety property, not a tidy-up. The model says "they are giving availability,
        and the words were 'Saturday morning'"; the dates come from parsing those words here. So a
        model that hallucinates a date cannot introduce one -- there is no field for it to arrive in.
        """
        intent = str(raw.get("intent") or "unclear")
        if intent not in language.INTENTS:
            intent = "unclear"

        # An intent that needs an offer on the table cannot be right when there isn't one.
        if intent in ("accept", "reject", "explain") and not has_open_offer:
            intent = deterministic.intent if deterministic.intent == "provide_availability" else "unclear"

        # A support topic the deterministic reader recognises wins, for the same reason `is_fixed`
        # does: it is a narrow lexical question. A live run had gpt-4o-mini file "Can I change my
        # delivery address?" as `unclear`, which was then answered with "which of those times would
        # you like?" -- the original misreading, arriving by a different route.
        if deterministic.intent == "general_support" and not deterministic.windows:
            intent = "general_support"

        # ...and the same rule in the other direction, which matters more. A message the parser
        # resolved to a real date IS about scheduling, whatever the model called it. "5th Sept what
        # time avail" came back as `general_support`, and the customer -- asking about delivery
        # times, which is the only thing this system does -- was told a colleague would call them
        # back. Twice. A resolved date is a fact the regex established; a model calling it "not
        # about timing" is simply wrong, and the reply is the worst one available.
        if intent in ("general_support", "unclear") and deterministic.windows:
            intent = deterministic.intent

        result = language.Interpretation(
            intent=intent,
            support_topic=deterministic.support_topic,
            # NOT the model's answer. A live smoke test had gpt-4o-mini mark "I'm free Saturday
            # morning" as fixed -- a plain statement of availability, not a declaration that
            # nothing else is possible. Getting this wrong silently switches off counteroffers, so
            # the customer is never told about a better slot and nobody can see why. It is a narrow
            # lexical question (does the message contain an exclusivity phrase) that a regex answers
            # reliably and a model over-generalises.
            is_fixed=deterministic.is_fixed,
            rejects_whole_day=bool(raw.get("rejects_whole_day")) or deterministic.rejects_whole_day,
            direction=deterministic.direction,
            note=deterministic.note,
        )

        phrases = [str(p) for p in (raw.get("availability_phrases") or []) if str(p).strip()]
        if phrases and intent in ("provide_availability", "reject"):
            windows: list[language.StatedWindow] = []
            for rank, phrase in enumerate(phrases, start=1):
                parsed = language._extract_windows(phrase, context_date=context_date)
                for w in parsed:
                    w.preference_rank = rank
                    windows.append(w)
            # Re-ranked against the original sentence. The same smoke test had the model return
            # "Saturday afternoon or Tuesday morning, but Tuesday is better" in the order spoken,
            # ignoring the stated preference -- and a preference silently dropped is the customer's
            # wish being overruled by a routing score they cannot see.
            result.windows = language._apply_preference(message, windows)

        # Nothing usable came back from the phrases, but the deterministic parser found something.
        # Prefer the concrete reading over an empty one -- an intent with no windows would send us
        # to a clarification question the customer has already answered.
        if not result.windows and deterministic.windows and intent in (
            "provide_availability", "reject"
        ):
            result.windows = deterministic.windows

        refers = raw.get("refers_to")
        if intent == "accept":
            result.accepted_phrase = str(refers) if refers else deterministic.accepted_phrase
            result.accepted_ordinal = (
                language._accepted_ordinal(str(refers).lower())
                if refers
                else deterministic.accepted_ordinal
            )

        return result
