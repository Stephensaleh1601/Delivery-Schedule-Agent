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

from dispatch_agent.geo.postal_codes import region_of
from dispatch_agent.models import DaySequence, JobRecord


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

    @property
    def already_in_the_area(self) -> bool:
        return self.neighbours_in_region > 0 and self.region is not None


def route_facts(
    sequence: DaySequence,
    candidate: JobRecord,
    jobs_by_id: dict[str, JobRecord],
) -> RouteFacts:
    """Read the candidate's position and neighbourhood out of a solved day."""
    ids = [stop.job_id for stop in sequence.stops]
    try:
        index = ids.index(candidate.id)
    except ValueError as exc:  # the candidate is not in its own solved day -- a caller bug
        raise ValueError(f"{candidate.id} is not a stop in this sequence") from exc

    region = region_of(candidate.address.postal_code, candidate.address.coordinates)

    neighbours = 0
    for job_id in ids:
        if job_id == candidate.id:
            continue
        other = jobs_by_id.get(job_id)
        if other is None:
            continue
        if region is not None and region_of(other.address.postal_code, other.address.coordinates) == region:
            neighbours += 1

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
    )


def _weekday_part(window) -> str:
    """"Friday morning" / "Friday afternoon", by when the promise starts."""
    return "morning" if window.start.hour < 12 else "afternoon"


def customer_reason(facts: RouteFacts, date, window) -> str:
    """One sentence a customer can be sent. Names no one else and quotes no score."""
    when = f"{date:%A} {_weekday_part(window)}"
    if facts.already_in_the_area and facts.region:
        return (
            f"We'll already be delivering in the {facts.region} on {when}, "
            f"so this is the time we can promise most reliably."
        )
    if facts.opens_empty_day:
        return f"{when.capitalize()} is clear, so we can start the day with you."
    return f"{when.capitalize()} fits between the other deliveries we already have booked."


def coordinator_reason(facts: RouteFacts, added_drive_minutes: int) -> str:
    """The dispatcher's version: the same facts, plus who this stop sits between."""
    where = f"{facts.region}-side" if facts.region else "this"
    parts = [f"Stop {facts.position} of {facts.stop_count} on a {where} route"]

    between = [n for n in (facts.previous_customer, facts.next_customer) if n]
    if len(between) == 2:
        parts.append(f"between {between[0]} and {between[1]}")
    elif between and facts.position == 1:
        parts.append(f"ahead of {between[0]}")
    elif between:
        parts.append(f"after {between[0]}")

    if added_drive_minutes > 0:
        parts.append(f"adding {added_drive_minutes} driving minutes")
    elif added_drive_minutes < 0:
        parts.append(f"saving {abs(added_drive_minutes)} driving minutes")
    else:
        parts.append("with no extra driving")

    if facts.opens_empty_day:
        parts.append("and opening a delivery day that would otherwise be empty")

    return ", ".join(parts) + "."
