"""Reading a customer's own words: dates, times, and what they are trying to do.

This module is deterministic. It exists for three reasons, in order of importance:

1. **Tests must not call a model.** Every natural-language behaviour the product claims is pinned
   here, offline, so a change in provider or prompt cannot quietly break "Saturday morning".
2. **The demo must survive a provider outage.** When Bedrock is unreachable the conversation still
   has to work, and it has to say plainly that it fell back rather than pretending otherwise.
3. **It is the model's grounding.** The LLM decides *intent* -- what the customer wants -- and hands
   back the phrases; the arithmetic of turning "next Tuesday" into a date happens here, once,
   against the planning clock. A model asked to compute a date will confidently produce one that is
   a week out, and nothing downstream would catch it.

Everything relative resolves against `PlanningClock.today()` in Asia/Singapore, never against the
server's own clock. That is not pedantry: the machine recording the demo is in another timezone, and
"tomorrow" changing meaning depending on where the process runs is exactly the class of bug that
only shows up on the day.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as Date, datetime, time as Time, timedelta
from zoneinfo import ZoneInfo

from dispatch_agent.config import settings
from dispatch_agent.models import TimeWindow
from dispatch_agent.planning import slots
from dispatch_agent.planning.clock import PlanningClock

SINGAPORE = ZoneInfo("Asia/Singapore")

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# Named parts of the day, taken from the delivery windows we actually publish rather than from a
# generic working day. When "morning" meant 09:00-13:00 here and 10:00-14:00 on the route, the
# decision panel told the customer they had asked for a window nobody offers.
DAY_PARTS: dict[str, tuple[Time, Time]] = {
    "morning": (slots.MORNING.start, slots.MORNING.end),
    "afternoon": (slots.AFTERNOON.start, slots.AFTERNOON.end),
    "evening": (slots.EVENING.start, slots.EVENING.end),
    # Not a published window -- deliberately spans the morning/afternoon boundary, so it matches
    # either rather than silently becoming one of them.
    "midday": (Time(12, 0), Time(15, 0)),
    "lunchtime": (Time(12, 0), Time(15, 0)),
    "noon": (Time(12, 0), Time(15, 0)),
}


def today() -> Date:
    """The application's today, in Singapore.

    PlanningClock is the authority (DEMO_BASE_DATE pins it for a recording). Only when it is
    unpinned does the real clock matter, and then it must be Singapore's, not the server's.
    """
    if settings.demo_base_date:
        return PlanningClock.today()
    return datetime.now(SINGAPORE).date()


# -- times ---------------------------------------------------------------------

_TIME = re.compile(
    r"(?P<hour>\d{1,2})"
    r"(?:[:.](?P<minute>\d{2}))?"
    r"\s*(?P<meridiem>am|pm|a\.m\.|p\.m\.)?",
    re.IGNORECASE,
)


def parse_time(text: str, assume_afternoon: bool = False) -> Time | None:
    """A clock time from a fragment like "1pm", "10:30", "after 1".

    `assume_afternoon` handles the Singaporean "after 1" -- nobody means 1am. Applied only to bare
    hours 1-7 with no meridiem, because "9" plainly means 9am in a delivery conversation and "12"
    is ambiguous in the other direction.
    """
    match = _TIME.search(text.strip())
    if not match:
        return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = (match.group("meridiem") or "").replace(".", "").lower()

    if hour > 23 or minute > 59:
        return None

    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and assume_afternoon and 1 <= hour <= 7:
        hour += 12

    return Time(hour=hour, minute=minute)


def clamp_to_working_day(window: TimeWindow) -> TimeWindow | None:
    """A stated window intersected with the hours we actually operate.

    Returns None when the intersection is empty -- "8pm" is a real answer that we cannot serve, and
    saying so is better than silently delivering at 5.
    """
    start = max(window.start, settings.work_day_start)
    end = min(window.end, settings.arrival_cutoff)
    if start >= end:
        return None
    return TimeWindow(start=start, end=end)


# -- dates ---------------------------------------------------------------------


def resolve_weekday(name: str, base: Date, qualifier: str = "") -> Date:
    """The next date falling on `name`, from `base` (exclusive).

    "this Saturday" and "Saturday" both mean the coming one. "next Tuesday" means the one after
    that when today is already past Tuesday's week -- the reading most people intend, and the one
    that at least never resolves into the past.
    """
    target = WEEKDAYS[name.lower()]
    ahead = (target - base.weekday()) % 7
    if ahead == 0:
        ahead = 7  # "Saturday" said on a Saturday means the next one, not today
    resolved = base + timedelta(days=ahead)
    if "next" in qualifier.lower():
        resolved += timedelta(days=7)
    return resolved


_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
    re.IGNORECASE,
)
_MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def parse_date(text: str, base: Date | None = None) -> Date | None:
    """A date from the way people actually write one, resolved in Singapore."""
    base = base or today()
    lowered = text.lower()

    iso = _ISO_DATE.search(lowered)
    if iso:
        return Date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    if re.search(r"\bday after tomorrow\b", lowered):
        return base + timedelta(days=2)
    if re.search(r"\btomorrow\b", lowered):
        return base + timedelta(days=1)
    if re.search(r"\btoday\b", lowered):
        return base

    day_month = _DAY_MONTH.search(lowered)
    month_day = _MONTH_DAY.search(lowered)
    if day_month or month_day:
        if day_month:
            day = int(day_month.group(1))
            month_name = day_month.group(2)
        else:
            day = int(month_day.group(2))
            month_name = month_day.group(1)
        month = _MONTHS.index(month_name.lower()[:3]) + 1
        year = base.year
        # A day/month already behind us means next year -- "3 January" said in December.
        if (month, day) < (base.month, base.day):
            year += 1
        try:
            return Date(year, month, day)
        except ValueError:
            return None

    weekday = re.search(
        r"\b(this|next|coming)?\s*(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b",
        lowered,
    )
    if weekday:
        return resolve_weekday(weekday.group(2), base, weekday.group(1) or "")

    return None


# -- windows -------------------------------------------------------------------

_AFTER = re.compile(r"\b(?:after|from|any\s*time\s*after|later\s*than)\s+(?P<t>[\d:.]+\s*(?:am|pm)?)", re.I)
_BEFORE = re.compile(r"\b(?:before|until|till|by|earlier\s*than)\s+(?P<t>[\d:.]+\s*(?:am|pm)?)", re.I)
_BETWEEN = re.compile(
    r"\b(?:between\s+)?(?P<a>\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?)\s*(?:-|–|to|till|until|and)\s*"
    r"(?P<b>\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?)",
    re.I,
)
_AT = re.compile(
    r"\b(?:at|around|about)\s+"
    r"(?P<t>\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?)\b",
    re.I,
)
_BARE_MERIDIEM_TIME = re.compile(
    r"\b(?P<t>\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.))\b", re.I
)


def parse_window(text: str) -> TimeWindow | None:
    """A time window from a phrase, clamped to the working day.

    Order matters: "between 1 and 3" must be read as a range before "1" is read as a bare time, and
    an explicit range must beat a day-part word in the same sentence.
    """
    lowered = text.lower()

    span = _BETWEEN.search(lowered)
    if span:
        start = parse_time(span.group("a"), assume_afternoon=True)
        end = parse_time(span.group("b"), assume_afternoon=True)
        if start and end:
            # "9-11" with no meridiem: if the end reads earlier than the start, the range crossed
            # midday, so the end is a pm time. "1-3" is already both-afternoon by the assumption.
            if end <= start:
                shifted = Time((end.hour + 12) % 24, end.minute)
                if shifted > start:
                    end = shifted
            if end > start:
                return clamp_to_working_day(TimeWindow(start=start, end=end))

    after = _AFTER.search(lowered)
    if after:
        start = parse_time(after.group("t"), assume_afternoon=True)
        if start and start < settings.arrival_cutoff:
            return clamp_to_working_day(TimeWindow(start=start, end=settings.arrival_cutoff))

    before = _BEFORE.search(lowered)
    if before:
        end = parse_time(before.group("t"), assume_afternoon=True)
        if end and end > settings.work_day_start:
            return clamp_to_working_day(TimeWindow(start=settings.work_day_start, end=end))

    for word, (start, end) in DAY_PARTS.items():
        if re.search(rf"\b{word}\b", lowered):
            return TimeWindow(start=start, end=end)

    if re.search(r"\b(any\s*time|anytime|whole\s*day|all\s*day|any)\b", lowered):
        return TimeWindow(start=settings.work_day_start, end=settings.arrival_cutoff)

    at = _AT.search(lowered)
    if at:
        moment = parse_time(at.group("t"), assume_afternoon=True)
        if moment:
            # A point in time is not a window. Read it as the two hours around it, which is the
            # width we would promise anyway, then let the solver narrow it.
            start = max(settings.work_day_start, moment)
            end = min(settings.arrival_cutoff, Time(min(23, start.hour + 2), start.minute))
            if start < end:
                return TimeWindow(start=start, end=end)

    # A date followed directly by a clock time is ordinary chat: "5th Sept 10am". Require the
    # am/pm marker here so the date's "5th" is never mistaken for 5am.
    bare_time = _BARE_MERIDIEM_TIME.search(lowered)
    if bare_time:
        moment = parse_time(bare_time.group("t"), assume_afternoon=True)
        if moment:
            start = max(settings.work_day_start, moment)
            end = min(settings.arrival_cutoff, Time(min(23, start.hour + 2), start.minute))
            if start < end:
                return TimeWindow(start=start, end=end)

    return None


# -- what the customer is doing -------------------------------------------------


@dataclass
class StatedWindow:
    """One date+window the customer actually said, with the words they used.

    `phrase` is kept so a reply can quote them back ("Saturday morning") rather than reading out an
    ISO date, and so a coordinator reviewing the thread can see what was interpreted from what.
    """

    date: Date
    window: TimeWindow
    phrase: str
    preference_rank: int = 1


@dataclass
class Interpretation:
    """What one customer message means, as far as anything deterministic can tell.

    Deliberately not "the answer": `intent` may be `unclear`, and that is a real outcome the
    conversation handles by asking one question rather than guessing a date.
    """

    intent: str = "unclear"
    windows: list[StatedWindow] = field(default_factory=list)
    # The customer says this is their only possibility. Turns off counteroffers -- see
    # negotiation.should_counteroffer.
    is_fixed: bool = False
    # For an acceptance: which offered slot they meant, as an ordinal (1-based) or a time phrase.
    accepted_ordinal: int | None = None
    accepted_phrase: str | None = None
    # For a rejection: whether they turned down the whole day or just the time proposed.
    rejects_whole_day: bool = False
    # "can you do later?" / "anything earlier?" -- a direction without a time. Enough to re-solve
    # the same day usefully, and not enough to pretend they named a window.
    direction: str | None = None
    # For general_support: what they are actually asking about, so the reply can be specific
    # rather than a generic apology. "address", "cancel", "price", "contact" or None.
    support_topic: str | None = None
    # Free-text the caller may quote back when asking for clarification.
    note: str = ""


INTENTS = frozenset(
    {
        "provide_availability", "accept", "reject", "explain", "policy_question",
        "general_support", "unclear",
    }
)

# Questions about how delivery works, which the written policy can answer. Separate from `explain`
# (which is about one offer we made) and from `general_support` (which we cannot answer at all):
# "what timings do you have?" has a published answer, and sending it to a human is as wrong as
# guessing at it.
_POLICY_QUESTION = re.compile(
    r"\b(?:what|which|when|where|how|do|does|can|could|are|is|why)\b[^?]*\b(?:"
    r"deliver|delivery|deliveries|delivering|timing|timings|slot|slots|window|windows|"
    r"hours|day|days|friday|saturday|region|area|zone|cluster|west|east|north|south|"
    r"central|leave|unattended|doorstep|outside|home|person|driver|route|policy|rules?"
    r")\b",
    re.I,
)

# Things customers ask about that are not the timing. Kept separate from `unclear` because they
# are perfectly clear -- we simply cannot answer them by moving a van. "Can I change my delivery
# address?" was being answered with "which of those times would you like?", which is the kind of
# reply that makes someone give up on an automated agent for good.
_SUPPORT_TOPICS: list[tuple[str, "re.Pattern[str]"]] = [
    ("address", re.compile(
        r"\b(?:change|update|correct|wrong|different|new|move)\b[^.?!]*\b"
        r"(?:address|postal\s*code|postcode|location|unit|block|flat)\b"
        r"|\b(?:address|postal\s*code|postcode)\b[^.?!]*\b(?:change|updated?|wrong|different)\b",
        re.I)),
    ("cancel", re.compile(r"\b(?:cancel|call it off|don'?t want|no longer need)\b", re.I)),
    ("price", re.compile(r"\b(?:how much|price|cost|fee|charge|payment|pay)\b", re.I)),
    ("contact", re.compile(r"\b(?:speak|talk|call me|phone|human|someone|manager|agent)\b", re.I)),
]


def support_topic(text: str) -> str | None:
    for name, pattern in _SUPPORT_TOPICS:
        if pattern.search(text):
            return name
    return None

_ACCEPT = re.compile(
    r"\b(ok(ay)?|sure|yes|yep|yeah|confirm(ed|s)?|book|take|works?|fine|good|great|"
    r"sounds good|that works|deal|perfect|lets do|let's do|go ahead)\b",
    re.I,
)
_REJECT = re.compile(
    r"\b(no|nope|not|can'?t|cannot|won'?t|doesn'?t work|does not work|unable|"
    r"none of|neither|too early|too late|another time|something else)\b",
    re.I,
)
_EXPLAIN = re.compile(
    r"\b(why|how come|what'?s the reason|reason|explain|can'?t you|cannot you|"
    r"is there|what about)\b.*\?|^\s*why\b",
    re.I,
)
_FIXED = re.compile(
    r"\b(only|just) (available |possible |free |workable )?(time|timing|slot|window|day|option)\b"
    r"|\bthat'?s the only\b|\bonly one that works\b|\bno other time\b|\bnothing else works\b"
    r"|\bcan only do\b|\bonly free\b",
    re.I,
)
# Words that genuinely identify WHICH option. Deliberately excludes bare "one" and "two":
# "take that one" is a demonstrative, not an ordinal, and reading it as "the first" books a slot
# the customer never picked -- silently, and against two options they were still choosing between.
# With nothing else to go on that message is ambiguous, and ambiguous means ask.
_ORDINALS = {
    "first": 1, "1st": 1, "former": 1, "earlier one": 1,
    "second": 2, "2nd": 2, "latter": 2, "later one": 2, "last": 2,
}
_PREFERS = re.compile(
    r"\b(?P<what>[a-z0-9: ]+?)\s+(?:is|would be|works)\s+(?:better|best|preferred|ideal)\b"
    r"|\bprefer(?:ably)?\s+(?P<what2>[a-z0-9: ]+)",
    re.I,
)


def _split_alternatives(text: str) -> list[str]:
    """"Saturday afternoon or Tuesday morning" -> two fragments.

    Split on "or" and "," but not on "and", which usually joins one range ("between 1 and 3")
    rather than offering a second option.
    """
    parts = re.split(r"\bor\b|;|,(?!\s*\d{2}\b)", text, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def interpret(
    text: str, has_open_offer: bool = False, context_date: Date | None = None
) -> Interpretation:
    """A best deterministic reading of one customer message.

    `has_open_offer` changes what a bare "okay" means: with something on the table it is an
    acceptance, without it is small talk we should not act on. That ambiguity is the reason this
    takes the conversation's state rather than only the words.

    `context_date` is the date currently under discussion. "after 2 instead" names a time and no
    day, and the day it means is obviously the one just proposed -- without this the window is
    dropped and the customer is asked a question they already answered.

    This is the fallback and the test harness. In the live demo the model does this job and this
    result is the safety net underneath it.
    """
    raw = (text or "").strip()
    if not raw:
        return Interpretation(intent="unclear", note="empty message")

    lowered = raw.lower()
    result = Interpretation(
        is_fixed=bool(_FIXED.search(lowered)), direction=_direction(lowered)
    )

    windows = _extract_windows(raw, context_date=context_date)

    # Something other than the timing. Tested FIRST, and whether or not an offer is open, because
    # these messages are full of words that look like scheduling: "Can I change my delivery
    # address?" contains "change" and a question mark, "I don't want the old one" contains a
    # refusal. It was being answered with "which of those times would you like?".
    topic = support_topic(raw)

    # A question about how delivery works, which the written policy can answer -- "what timings do
    # you have?", "can you leave it outside?". Sitting between the two support tiers on purpose:
    #
    #   address / cancel / price   a person owns these outright, so they win.
    #   POLICY QUESTION            we have a published answer, so answer it.
    #   contact                    "someone", "agent" and "speak" are weak words. Without this
    #                              order, "why do you need someone at home?" -- a question the
    #                              policy answers in one rule -- was handed to a human because it
    #                              contains "someone".
    bare_days = _dates_without_times(raw) if not windows else []
    asks_for_times_on_day = bool(
        bare_days and re.search(r"\b(what time|which time|times?|avail(?:able|ability)?)\b", raw, re.I)
    )
    policy_like = bool(
        raw.count("?") and not windows and not asks_for_times_on_day and _POLICY_QUESTION.search(raw)
    )
    if topic and not windows and not (policy_like and topic == "contact"):
        result.intent = "general_support"
        result.support_topic = topic
        result.note = raw
        return result

    # An explanation request is a question about a proposal, so it only exists while one is open,
    # and it must be tested before rejection: "can't you come on Saturday?" contains "can't".
    if has_open_offer and _EXPLAIN.search(raw) and not windows:
        result.intent = "explain"
        result.note = raw
        return result

    if has_open_offer and _REJECT.search(lowered):
        result.intent = "reject"
        # "Not Saturday morning, can you do after 1?" states two windows, and only the second is
        # something the customer is offering. Treating the negated half as availability would have
        # us propose back the very time they just turned down.
        result.windows = [w for w in windows if not _is_negated(w.phrase)]
        for i, w in enumerate(result.windows):
            w.preference_rank = i + 1
        result.rejects_whole_day = bool(
            re.search(r"\bnone of (these|them|those)\b|\bnot (that|any) day\b", lowered)
        )
        result.note = raw
        return result

    # An acceptance word, or an ordinal on its own. "The second one please" contains no yes, but
    # picking one of two options out loud is not something anyone does by accident.
    if has_open_offer and not windows and (
        _ACCEPT.search(lowered) or _accepted_ordinal(lowered) is not None
    ):
        result.intent = "accept"
        result.accepted_ordinal = _accepted_ordinal(lowered)
        result.accepted_phrase = _accepted_phrase(raw)
        result.note = raw
        return result

    # A question about how delivery works, with no time in it. Deliberately below accept, reject
    # and explain: those all mention days too, and with an offer open they are the right reading.
    # "Can't you come on Saturday?" asks about the slot we proposed; the same words with nothing
    # on the table ask which days we run.
    if policy_like:
        result.intent = "policy_question"
        result.note = raw
        return result

    if windows:
        result.intent = "provide_availability"
        result.windows = windows
        return result

    # A day named with no time at all -- "can you come Thursday?", "Saturday please". A whole
    # working day is what they mean, and asking them to be more specific about something they said
    # perfectly clearly is the kind of thing that makes people give up on an automated agent.
    #
    # Only reached once accept, reject and explain have been ruled out, because those all mention
    # days too: "Saturday works" is an acceptance, not an offer of the whole of Saturday.
    if bare_days:
        result.intent = "provide_availability"
        result.windows = bare_days
        return result

    # An acceptance-shaped message that also names a time ("Tuesday works") reaches here only when
    # the date could not be resolved -- so it is genuinely ambiguous, not an acceptance.
    if has_open_offer and _ACCEPT.search(lowered):
        result.intent = "accept"
        result.accepted_ordinal = _accepted_ordinal(lowered)
        result.accepted_phrase = _accepted_phrase(raw)
        result.note = raw
        return result

    # "can you do later?" -- no time, no acceptance word, but unmistakably a counter-proposal
    # about the slot on the table.
    if has_open_offer and result.direction:
        result.intent = "reject"
        result.note = raw
        return result

    result.intent = "unclear"
    result.note = raw
    return result


_DIRECTION = re.compile(
    r"\b(?P<later>later|after that|further (?:in|into) the day|towards? the (?:end|afternoon))\b"
    r"|\b(?P<earlier>earlier|sooner|before that|first thing|start of the day)\b",
    re.I,
)


def _direction(lowered: str) -> str | None:
    match = _DIRECTION.search(lowered)
    if not match:
        return None
    return "later" if match.group("later") else "earlier"


def _extract_windows(text: str, context_date: Date | None = None) -> list[StatedWindow]:
    """Every date+window the message states, in the order given.

    A fragment with a time but no date inherits the last date seen, so "Saturday morning or
    afternoon" produces two windows on Saturday rather than one and a dangling phrase.
    """
    base = today()
    found: list[StatedWindow] = []
    # The day under discussion, so a fragment naming only a time attaches to it rather than being
    # discarded.
    last_date: Date | None = context_date

    for fragment in _split_alternatives(text):
        date = parse_date(fragment, base)
        window = parse_window(fragment)
        if date:
            last_date = date
        if window is None:
            continue
        anchor = date or last_date
        if anchor is None:
            continue
        found.append(StatedWindow(date=anchor, window=window, phrase=fragment))

    return _apply_preference(text, found)


def _dates_without_times(text: str) -> list[StatedWindow]:
    """Days the customer named with no time attached, as whole working days."""
    base = today()
    whole_day = TimeWindow(start=settings.work_day_start, end=settings.arrival_cutoff)

    found: list[StatedWindow] = []
    seen: set[Date] = set()
    for fragment in _split_alternatives(text):
        if _is_negated(fragment):
            continue
        date = parse_date(fragment, base)
        if date is None or date in seen:
            continue
        seen.add(date)
        found.append(
            StatedWindow(
                date=date, window=whole_day, phrase=fragment, preference_rank=len(found) + 1
            )
        )
    return found


def _apply_preference(text: str, windows: list[StatedWindow]) -> list[StatedWindow]:
    """Rank the stated windows, honouring an explicit "but Tuesday is better".

    Order of mention is the default ranking. A preference sentence promotes whichever window it
    names -- and only that; nothing else is reordered, because the customer said one thing.
    """
    for i, w in enumerate(windows):
        w.preference_rank = i + 1

    preferred = _PREFERS.search(text)
    if not preferred or len(windows) < 2:
        return windows

    phrase = (preferred.group("what") or preferred.group("what2") or "").strip().lower()
    if not phrase:
        return windows

    for w in windows:
        if any(token in w.phrase.lower() for token in phrase.split() if len(token) > 2):
            others = [x for x in windows if x is not w]
            w.preference_rank = 1
            for i, other in enumerate(others):
                other.preference_rank = i + 2
            return sorted(windows, key=lambda x: x.preference_rank)

    return windows


_NEGATED_FRAGMENT = re.compile(
    r"^\s*(?:no|not|nope|neither|none|can'?t|cannot|won'?t|don'?t|do not|"
    r"unable to do|nothing on)\b",
    re.I,
)


def _is_negated(fragment: str) -> bool:
    """Whether this fragment is the customer ruling a time OUT rather than offering it."""
    return bool(_NEGATED_FRAGMENT.search(fragment.strip()))


def _accepted_ordinal(lowered: str) -> int | None:
    for word, n in _ORDINALS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return n
    return None


def _accepted_phrase(text: str) -> str | None:
    """A time or day named in an acceptance -- "confirm 1pm", "Tuesday works".

    Returned as text, not resolved: the caller matches it against the slots actually on offer,
    which is the only context in which "1pm" identifies anything.
    """
    lowered = text.lower()
    time_named = _TIME.search(lowered)
    day_named = re.search("|".join(sorted(WEEKDAYS, key=len, reverse=True)), lowered)
    if time_named or day_named:
        return text.strip()
    return None
