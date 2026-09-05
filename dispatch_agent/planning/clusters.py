"""Which day a customer normally belongs to, and which routes a search may look at.

The operation runs two delivery days a week and each one covers a group of regions, so every
address has exactly one normal day. That is the whole rule:

    Friday    North, North-East, South, East
    Saturday  Central, City, West

`DISTRICT_TO_REGION` has seven regions, not five. City sits with Central and North-East with the
North side, so every postal district has a home -- without that, some customers would match no
delivery day at all and escalate for a reason nobody could explain.

**Scope is the thing this module exists for.** Insertion is used in every flow to prove a slot
works; what differs is which routes get searched, and getting that wrong is the difference between
a coordinator's answer and a machine's:

    CLUSTER    the customer's own day. The normal booking -- do not go looking at the other day
               to save a kilometre, they did not ask.
    REQUESTED  a day they named that is not theirs. An exception, tested honestly rather than
               silently ignored.
    BOTH       after they turn the first offer down and will consider anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date

from dispatch_agent.geo.postal_codes import region_of
from dispatch_agent.models import JobRecord

FRIDAY = 4  # date.weekday(): Monday is 0
SATURDAY = 5

REGIONS_BY_WEEKDAY: dict[int, frozenset[str]] = {
    FRIDAY: frozenset({"North", "North-East", "South", "East"}),
    SATURDAY: frozenset({"Central", "City", "West"}),
}

WEEKDAY_NAME = {FRIDAY: "Friday", SATURDAY: "Saturday"}


@dataclass(frozen=True)
class Placement:
    """Where a customer normally belongs, in the words the panel and the reply both use."""

    region: str | None
    weekday: int | None

    @property
    def day_name(self) -> str:
        return WEEKDAY_NAME.get(self.weekday, "either day")


def placement_of(order: JobRecord) -> Placement:
    """The customer's region and the delivery day that region belongs to."""
    address = order.address
    region = region_of(address.postal_code, address.coordinates) if address else None
    for weekday, regions in REGIONS_BY_WEEKDAY.items():
        if region in regions:
            return Placement(region=region, weekday=weekday)
    return Placement(region=region, weekday=None)


def cluster_dates(order: JobRecord, cycle_dates: list[Date]) -> list[Date]:
    """The customer's own delivery day, as a list so callers can treat every scope alike.

    Empty when their region maps to no day -- which should be impossible, and is returned rather
    than guessed so it surfaces as "we could not place you" instead of a silent search of both.
    """
    weekday = placement_of(order).weekday
    return [d for d in cycle_dates if d.weekday() == weekday] if weekday is not None else []


def requested_dates(order: JobRecord, cycle_dates: list[Date]) -> list[Date]:
    """The days the customer actually named, in this cycle."""
    named = {option.date for option in order.availability_options}
    return [d for d in cycle_dates if d in named]


def is_off_cluster(order: JobRecord, day: Date) -> bool:
    """Whether `day` is outside the customer's own delivery day.

    A West customer asking for Friday is not a mistake to correct, it is a request to test.
    """
    weekday = placement_of(order).weekday
    return weekday is not None and day.weekday() != weekday


def resolve_scope(order: JobRecord, scope: str, cycle_dates: list[Date]) -> list[Date]:
    """Turn the workflow the agent chose into the dates the search may look at.

    The agent picks the WORKFLOW; this picks the dates. That split is the point -- a model
    choosing "their normal day" is choosing a path, and a model choosing "2026-09-12" is doing
    arithmetic it has no business doing.
    """
    if scope == "requested":
        # No silent fallback. Searching their cluster day here is exactly the failure the rule
        # forbids -- answering about a day they did not ask about while appearing to answer about
        # the one they did. An empty list is the caller's cue to say so.
        return requested_dates(order, cycle_dates)
    if scope == "both":
        return list(cycle_dates)
    return cluster_dates(order, cycle_dates) or list(cycle_dates)
