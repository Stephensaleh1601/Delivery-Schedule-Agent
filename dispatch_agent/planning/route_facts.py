"""Why this window, in words -- read off the solved route rather than imagined.

An experienced coordinator does not say "our optimiser scored Friday at 93". They say *we'll
already be in the East that morning, between Bedok and Simei*. That sentence is worth having, and
it is only worth having if every word of it is true.

So everything here is derived from the `DaySequence` OR-Tools actually returned: the position the
stop was inserted at, the names either side of it, the region from the postal district table, and
how much of that day's work is already in the same region. Nothing is inferred from the score, and
no geography is invented -- there is no model call on this path, because a model asked to describe
a route will cheerfully produce a plausible neighbourhood that the van is not going to.

Two audiences, two functions. `coordinator_reason` may name the stops either side, because a
dispatcher is looking at the whole day anyway. `customer_reason` may not: it says the van is
already working the area, never who else is on it.
"""
from __future__ import annotations

from dataclasses import dataclass

from dispatch_agent.config import settings
from dispatch_agent.geo.postal_codes import region_of
from dispatch_agent.models import DaySequence, JobRecord

# How close in TIME another stop in the same region has to be before "we'll already be there"
# describes the same part of the day. Two hours: near enough that a crew genuinely is in the area
# around then, far enough that ordinary route spacing still qualifies.
NEARBY_MINUTES = 120

MATERIAL_IDLE_MINUTES = settings.material_idle_minutes


@dataclass(frozen=True)
class RouteFacts:
    """Facts about one candidate's place in a solved day. Every field is observed, not estimated."""

    region: str | None
    # Where in the day's order this stop landed, 1-based, and how many stops there are in total.
    position: int
    stop_count: int
    previous_customer: str | None
    next_customer: str | None
    # Other stops that day in the same region -- excluding this one. This is what licenses
    # "we'll already be in the area"; at zero, the phrase would be false and is not used.
    neighbours_in_region: int
    opens_empty_day: bool
    # Minutes between this stop and the nearest neighbouring stop in the same region. "We'll
    # already be in the East" is a claim about WHEN as well as where, and this is the when.
    minutes_to_nearest_neighbour: int | None = None
    # Waiting this insertion creates across the day, and overtime it causes. Both are consequences
    # the customer deserves to hear about rather than a cheerful sentence about the neighbourhood.
    added_idle_minutes: int = 0
    overtime_minutes: int = 0

    @property
    def already_in_the_area(self) -> bool:
        """Whether we can honestly say the van will already be nearby.

        Three conditions, all necessary. The product said "we'll already be delivering in the East
        on Tuesday afternoon" about a route whose only eastern stop was at 9am, after which it went
        west -- the sentence was true about the day and false about the hour, and the customer would
        have waited three and a half hours to find out.
        """
        if self.region is None or self.neighbours_in_region == 0:
            return False
        if self.minutes_to_nearest_neighbour is None:
            return False
        if self.minutes_to_nearest_neighbour > NEARBY_MINUTES:
            return False
        return self.added_idle_minutes < MATERIAL_IDLE_MINUTES


def _minutes_of(value) -> int:
    return value.hour * 60 + value.minute


def route_facts(
    sequence: DaySequence,
    candidate: JobRecord,
    jobs_by_id: dict[str, JobRecord],
    added_idle_minutes: int = 0,
    overtime_minutes: int = 0,
) -> RouteFacts:
    """Read the candidate's position and neighbourhood out of a solved day."""
    ids = [stop.job_id for stop in sequence.stops]
    try:
        index = ids.index(candidate.id)
    except ValueError as exc:  # the candidate is not in its own solved day -- a caller bug
        raise ValueError(f"{candidate.id} is not a stop in this sequence") from exc

    region = region_of(candidate.address.postal_code, candidate.address.coordinates)

    own_arrival = _minutes_of(sequence.stops[index].arrival_window.start)
    neighbours = 0
    nearest: int | None = None
    for stop in sequence.stops:
        if stop.job_id == candidate.id:
            continue
        other = jobs_by_id.get(stop.job_id)
        if other is None:
            continue
        if region is None or region_of(other.address.postal_code, other.address.coordinates) != region:
            continue
        neighbours += 1
        # How far apart in the DAY, not just on the map. A stop in the same region eight hours
        # earlier does not make us "already there" at 1pm.
        gap = abs(_minutes_of(stop.arrival_window.start) - own_arrival)
        nearest = gap if nearest is None else min(nearest, gap)

    def name_at(i: int) -> str | None:
        if not (0 <= i < len(ids)):
            return None
        job = jobs_by_id.get(ids[i])
        return job.customer_name if job else None

    return RouteFacts(
        region=region,
        position=index + 1,
        stop_count=len(ids),
        previous_customer=name_at(index - 1),
        next_customer=name_at(index + 1),
        neighbours_in_region=neighbours,
        opens_empty_day=len(ids) == 1,
        minutes_to_nearest_neighbour=nearest,
        added_idle_minutes=added_idle_minutes,
        overtime_minutes=overtime_minutes,
    )


def _weekday_part(window) -> str:
    """"Friday morning" / "Friday afternoon", by when the promise starts.

    Only used where a vague part of the day is genuinely what we mean. A window that straddles
    midday -- 11am to 1pm -- is neither, and calling it "Saturday morning" to a customer who has
    just been offered 11-1 is describing the wrong thing: `describe_window` names the hours.
    """
    return "morning" if window.start.hour < 12 else "afternoon"


def describe_window(date, window) -> str:
    """"Saturday 11am-1pm". The exact hours, for anything a customer will read back.

    The product said "Saturday morning fits between the other deliveries" directly after offering
    11am-1pm, which is not morning and does not match the times in the same message.
    """
    return f"{date:%A} {_clock(window.start)}-{_clock(window.end)}"


def _clock(value) -> str:
    hour = value.hour % 12 or 12
    suffix = "am" if value.hour < 12 else "pm"
    return f"{hour}:{value.minute:02d}{suffix}" if value.minute else f"{hour}{suffix}"


def minutes_phrase(count: int) -> str:
    """"1 minute", "34 minutes", "3h 46m". The product wrote "1 minutes"."""
    if count < 60:
        return f"{count} minute{'' if count == 1 else 's'}"
    hours, rest = divmod(count, 60)
    if rest == 0:
        return f"{hours} hour{'' if hours == 1 else 's'}"
    return f"{hours}h {rest}m"


def customer_reason(facts: RouteFacts, date, window) -> str:
    """One sentence a customer can be sent. Names no one else and quotes no score.

    The consequences come first. A slot that costs the crew hours of waiting and half an hour of
    overtime is not "the time we can promise most reliably", however little driving it adds -- and
    saying so was the product reciting a routing metric rather than describing the day.
    """
    # The exact hours, not a vague part of the day. See describe_window.
    when = describe_window(date, window)

    if facts.overtime_minutes > 0:
        return (
            f"We can make {when} work, though it does run our crew "
            f"{minutes_phrase(facts.overtime_minutes)} past the end of their day."
        )
    if facts.added_idle_minutes >= MATERIAL_IDLE_MINUTES:
        return (
            f"We can do {when}, though our crew would be waiting around "
            f"{minutes_phrase(facts.added_idle_minutes)} for it."
        )
    if facts.already_in_the_area and facts.region:
        return (
            f"We'll already be delivering in the {facts.region} around then, "
            f"so this is the time we can promise most reliably."
        )
    if facts.opens_empty_day:
        return f"{date:%A} is clear, so we can start the day with you at {_clock(window.start)}."
    return f"{when} fits between the other deliveries we already have booked."


def coordinator_reason(facts: RouteFacts, added_drive_minutes: int) -> str:
    """The dispatcher's version: the same facts, plus who this stop sits between."""
    where = f"{facts.region}-side" if facts.region else "this"
    # "a East-side route" was appearing in the coordinator panel.
    article = "an" if where[0].upper() in "AEIOU" else "a"
    parts = [f"Stop {facts.position} of {facts.stop_count} on {article} {where} route"]

    between = [n for n in (facts.previous_customer, facts.next_customer) if n]
    if len(between) == 2:
        parts.append(f"between {between[0]} and {between[1]}")
    elif between and facts.position == 1:
        parts.append(f"ahead of {between[0]}")
    elif between:
        parts.append(f"after {between[0]}")

    if added_drive_minutes > 0:
        parts.append(f"adding {minutes_phrase(added_drive_minutes)} of driving")
    elif added_drive_minutes < 0:
        parts.append(f"saving {minutes_phrase(abs(added_drive_minutes))} of driving")
    else:
        parts.append("with no extra driving")

    if facts.added_idle_minutes >= 30:
        parts.append(f"and {minutes_phrase(facts.added_idle_minutes)} of waiting")
    if facts.overtime_minutes > 0:
        parts.append(f"and {minutes_phrase(facts.overtime_minutes)} of overtime")

    if facts.opens_empty_day:
        parts.append("and opening a delivery day that would otherwise be empty")

    return ", ".join(parts) + "."
