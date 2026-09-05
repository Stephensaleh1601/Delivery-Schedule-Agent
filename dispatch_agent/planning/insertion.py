"""Where a new customer could be slotted into a route that already exists.

This is the deterministic core of the demo, and the division of labour is the whole point: this
module decides what is true -- which stops are near, what a detour costs, whether the day still
works -- and the language model only picks between the results and explains them. It never
computes a number and never reorders one.

**Insertion, not re-optimisation.** The existing stop order is never changed. A customer is tried
immediately before and immediately after each nearby stop, and everything else stays exactly where
the published plan put it. That is what a dispatcher does when someone asks for a different day,
and it is also what keeps the answer explainable: "we put you after Chen Li Hua" is a sentence, and
"the solver reordered your day" is not.

**Nobody is moved, but everybody after you can be made late.** That distinction is the reason step
5 exists. Inserting a stop shifts every later arrival, and a detour that looks cheap in kilometres
can push the last three customers outside the window they were promised. Only a forward simulation
catches that, so no candidate is returned without one.

**Two distances, deliberately.** Straight-line proximity to an existing stop decides which
positions are worth testing at all (`ANCHOR_RADIUS_KM`); the added detour decides the ranking. They
answer different questions, and using either one for both gives the wrong answer: proximity alone
ignores that a stop 8km off a route adds 16km of driving, and detour alone means testing every
position on every route.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date, time as Time

from dispatch_agent.config import settings
from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.geo.zones import company_depot
from dispatch_agent.models import Coordinates, JobRecord, RoutePlanVersion, TimeWindow
from dispatch_agent.planning.clock import PlanningClock
from dispatch_agent.planning.slots import SLOTS, DeliverySlot, slot_containing


def _minutes(t: Time) -> int:
    return t.hour * 60 + t.minute


def _clock(minutes: int) -> Time:
    minutes = max(0, min(minutes, 24 * 60 - 1))
    return Time(hour=minutes // 60, minute=minutes % 60)


@dataclass(frozen=True)
class InsertionOption:
    """One place this customer could go, with the evidence for offering it.

    Every field is something a coordinator could check against the route, which is the standard
    for anything that reaches the decision panel.
    """

    date: Date
    slot: DeliverySlot
    # Names of the surrounding customers. Kept on the option because a coordinator view and the
    # tests both want them -- but deliberately absent from `to_data()` and from the evidence
    # stored on an offer, because those travel to the customer's own page.
    anchor_name: str
    anchor_stop_number: int
    anchor_distance_km: float
    placement: str  # "before" or "after" the anchor
    insert_position: int  # 1-based position the new stop takes in the route
    previous_stop: str  # customer name, or "Depot"
    next_stop: str
    added_distance_km: float
    added_minutes: int
    expected_arrival: Time
    finish_before: Time
    finish_after: Time
    source_plan_id: str
    source_plan_version: int
    # Whether this lands on a (date, window) the customer actually asked for. Preference
    # decides what the NORMAL offer tries first; it never reorders the fallback ranking.
    matches_preference: bool = False

    @property
    def window(self) -> TimeWindow:
        return self.slot.window

    @property
    def key(self) -> tuple[Date, str]:
        return (self.date, self.slot.name)

    @property
    def rank_key(self) -> tuple:
        """Total order over candidates, so the same data always produces the same three.

        Added distance first because that is what the business is spending. Everything after it
        exists only to break ties -- without them, two positions costing the same kilometre would
        swap places between runs and the demo would not be reproducible.
        """
        return (
            round(self.added_distance_km, 3),
            self.added_minutes,
            self.date,
            [s.name for s in SLOTS].index(self.slot.name),
            self.insert_position,
        )


@dataclass
class InsertionSearch:
    """What the search looked at, not just what it found.

    The counts are the decision panel's evidence rows. They come from here rather than from the
    model precisely so that "compared 18 stops" is a fact rather than a claim.
    """

    routes_checked: int = 0
    stops_checked: int = 0
    anchors_within_radius: int = 0
    positions_tested: int = 0
    rejected_would_delay: int = 0
    rejected_outside_windows: int = 0
    excluded_by_customer: int = 0
    options: list[InsertionOption] = field(default_factory=list)

    def top(self, n: int) -> list[InsertionOption]:
        return self.options[:n]

    def to_data(self) -> dict:
        """The tool-result payload. Flat and countable, so the panel cannot embellish it."""
        return {
            "routes_checked": self.routes_checked,
            "stops_checked": self.stops_checked,
            "anchors_within_radius": self.anchors_within_radius,
            "anchor_radius_km": settings.anchor_radius_km,
            "positions_tested": self.positions_tested,
            "rejected_would_delay": self.rejected_would_delay,
            "rejected_outside_windows": self.rejected_outside_windows,
            "excluded_by_customer": self.excluded_by_customer,
            "valid_count": len(self.options),
            "options": [
                {
                    "date": o.date.isoformat(),
                    "slot": o.slot.name,
                    "window": {"start": f"{o.window.start:%H:%M}", "end": f"{o.window.end:%H:%M}"},
                    # Deliberately NOT anchor_name / previous_stop / next_stop. This payload is
                    # persisted on the run and served to the CUSTOMER's own page, and naming the
                    # people either side of them is telling one customer who the others are. Stop
                    # numbers say everything a judge needs and identify nobody.
                    "anchor_stop_number": o.anchor_stop_number,
                    "anchor_distance_km": o.anchor_distance_km,
                    "placement": o.placement,
                    "insert_position": o.insert_position,
                    "added_distance_km": o.added_distance_km,
                    "added_minutes": o.added_minutes,
                    "expected_arrival": f"{o.expected_arrival:%H:%M}",
                    "finish_before": f"{o.finish_before:%H:%M}",
                    "finish_after": f"{o.finish_after:%H:%M}",
                    "promises_moved": 0,
                    "source_plan_id": o.source_plan_id,
                    "source_plan_version": o.source_plan_version,
                    "matches_preference": o.matches_preference,
                }
                for o in self.options
            ],
        }


@dataclass
class _Leg:
    """One stop as the simulation sees it: where, how long, and what it was promised."""

    name: str
    coords: Coordinates
    duration: int
    window: TimeWindow | None  # None only for the candidate, whose window is being decided


def _plan_legs(repo: JobsRepository, plan: RoutePlanVersion) -> list[_Leg] | None:
    legs = []
    for stop in plan.sequence.stops:
        job = repo.get_job(stop.job_id)
        if job is None or job.address.coordinates is None:
            return None
        legs.append(
            _Leg(
                name=job.customer_name,
                coords=job.address.coordinates,
                duration=job.duration_minutes,
                window=job.locked_window,
            )
        )
    return legs


def _simulate(
    legs: list[_Leg], depot: Coordinates, client: RoutingClient
) -> tuple[list[int], int] | None:
    """Drive the day in order and report each arrival, or None if a promise breaks.

    Waiting is allowed -- a van that arrives early sits until the window opens -- but arriving
    after a promised window has closed is not, and neither is getting home after the hard route
    end. Returning None rather than a score is deliberate: an infeasible day is not an expensive
    one, and nothing downstream should be able to rank it anyway.

    Reads legs through `client.leg`, which is the cache and never the provider. The caller warms
    every pair with one batched request first; going back to `drive_minutes` here would make the
    day's whole route a fresh round trip per candidate position, which is how this search came to
    take thirty-four seconds.
    """
    at = _minutes(settings.work_day_start)
    here = depot
    arrivals: list[int] = []

    for leg in legs:
        at += client.leg(here, leg.coords)["minutes"]
        if leg.window is not None:
            opens, closes = _minutes(leg.window.start), _minutes(leg.window.end)
            at = max(at, opens)
            if at > closes:
                return None
        arrivals.append(at)
        at += leg.duration
        here = leg.coords

    at += client.leg(here, depot)["minutes"]
    if at > _minutes(settings.hard_route_end):
        return None
    return arrivals, at


def _finish(legs: list[_Leg], depot: Coordinates, client: RoutingClient) -> Time:
    simulated = _simulate(legs, depot, client)
    return _clock(simulated[1]) if simulated else _clock(_minutes(settings.hard_route_end))


def search(
    repo: JobsRepository,
    order: JobRecord,
    dates: list[Date] | None = None,
    routing_client: RoutingClient | None = None,
    depot: Coordinates | None = None,
    exclude: set[tuple[Date, str]] | None = None,
    radius_km: float | None = None,
    prefer: set[tuple[Date, str]] | None = None,
    restrict_to: set[tuple[Date, str]] | None = None,
    on_phase=None,
) -> InsertionSearch:
    """Every safe place `order` could be inserted into the published routes, best first.

    `exclude` is (date, slot name) pairs the customer has already turned down or ruled out. They
    are removed, never re-ranked: a choice someone declined is not a cheaper choice.

    `prefer` is (date, slot name) pairs the customer asked for. Options are flagged, not reordered
    -- the ranking stays the tool's, and it is `create_normal_offer` that tries a preferred option
    first. Blending the two would mean a customer's wish quietly outranking a route fact in a list
    that is supposed to be ordered by cost.

    `restrict_to` is the harder version, for a customer who said their time is the ONLY one that
    works. Then everything else is excluded rather than ranked below: "only Saturday" means Friday
    is ruled out, and offering it anyway is not a helpful alternative, it is not having listened.
    An empty result is the right answer there -- it escalates.
    """
    client = routing_client or RoutingClient()
    depot = depot or company_depot()
    dates = dates if dates is not None else PlanningClock.horizon_dates()
    exclude = exclude or set()
    radius = radius_km if radius_km is not None else settings.anchor_radius_km

    # One tool, four things worth watching separately. The callback is optional so the search
    # stays usable from a test or a script with nothing to report to.
    def phase(key: str, label: str, reason: str = "", detail: str = "") -> None:
        if on_phase is not None:
            on_phase(key, label, reason, detail)

    found = InsertionSearch()
    if order.address.coordinates is None:
        return found
    customer = order.address.coordinates
    best: dict[tuple[Date, str], InsertionOption] = {}

    for day in dates:
        plan = repo.active_plan(day)
        if plan is None or not plan.sequence.stops:
            continue
        legs = _plan_legs(repo, plan)
        if legs is None:
            continue
        found.routes_checked += 1

        # One batched provider request covering the depot, every stop and the customer -- so every
        # lookup below is a dictionary read. Without it each pair is its own round trip, and the
        # search made 236 of them.
        client.matrix([depot, customer] + [leg.coords for leg in legs])

        phase("nearby", "Finding nearby stops",
              f"Measuring you against every stop on {day:%A}'s route")

        # Every stop is measured, not just the ones we expect to be near. "Compared 18 stops" has
        # to be true, and a cheap early exit would quietly make it a guess.
        distances = [client.leg(customer, leg.coords)["km"] for leg in legs]
        found.stops_checked += len(legs)

        anchors = [i for i, km in enumerate(distances) if km <= radius]
        found.anchors_within_radius += len(anchors)
        if not anchors:
            continue

        phase("positions", "Testing insertion positions",
              "Trying before and after each stop within range")
        baseline_finish = _finish(legs, depot, client)

        # Before and after each anchor. Two anchors side by side produce the same gap twice, so
        # the positions are deduplicated -- otherwise `positions_tested` overstates the work and
        # the same option competes with itself.
        # Which anchor justified each position. Deriving it back from the index was wrong:
        # "after anchor i" is position i+1, and reading legs[i+1] there reports the NEXT stop as
        # the anchor -- which is how a 13.2km stop appeared as the reason for an option that
        # qualified on a 4km one. Where two anchors justify the same gap, the nearer one is the
        # honest answer.
        position_anchor: dict[int, int] = {}
        for i in anchors:
            for p in (i, i + 1):
                current = position_anchor.get(p)
                if current is None or distances[i] < distances[current]:
                    position_anchor[p] = i

        for position in sorted(position_anchor):
            found.positions_tested += 1
            previous = legs[position - 1] if position > 0 else None
            following = legs[position] if position < len(legs) else None
            before_coords = previous.coords if previous else depot
            after_coords = following.coords if following else depot

            added_km = round(
                client.leg(before_coords, customer)["km"]
                + client.leg(customer, after_coords)["km"]
                - client.leg(before_coords, after_coords)["km"],
                2,
            )
            added_minutes = (
                client.leg(before_coords, customer)["minutes"]
                + client.leg(customer, after_coords)["minutes"]
                - client.leg(before_coords, after_coords)["minutes"]
            )

            candidate = _Leg(order.customer_name, customer, order.duration_minutes, None)
            trial = legs[:position] + [candidate] + legs[position:]
            simulated = _simulate(trial, depot, client)
            if simulated is None:
                found.rejected_would_delay += 1
                continue

            arrivals, home = simulated
            arrival = _clock(arrivals[position])
            slot = slot_containing(arrival)
            if slot is None:
                # Before the first window opens or after the last one closes. A real arrival, but
                # not one we can describe to a customer as morning, afternoon or evening.
                found.rejected_outside_windows += 1
                continue

            if (day, slot.name) in exclude:
                found.excluded_by_customer += 1
                continue
            if restrict_to is not None and (day, slot.name) not in restrict_to:
                # The customer told us this is their only possible time. Anything else is not
                # an alternative worth ranking, it is a time they have already ruled out.
                found.excluded_by_customer += 1
                continue

            anchor_index = position_anchor[position]
            option = InsertionOption(
                date=day,
                slot=slot,
                anchor_name=legs[anchor_index].name,
                anchor_stop_number=anchor_index + 1,
                anchor_distance_km=round(distances[anchor_index], 1),
                placement="before" if position == anchor_index else "after",
                insert_position=position + 1,
                previous_stop=previous.name if previous else "Depot",
                next_stop=following.name if following else "Depot",
                added_distance_km=added_km,
                added_minutes=added_minutes,
                expected_arrival=arrival,
                finish_before=baseline_finish,
                finish_after=_clock(home),
                source_plan_id=plan.id,
                source_plan_version=plan.version,
                matches_preference=(day, slot.name) in (prefer or set()),
            )

            # One best option per (date, window). Two positions in the same afternoon are one
            # choice to a customer, and offering both as if they were different is padding.
            existing = best.get(option.key)
            if existing is None or option.rank_key < existing.rank_key:
                best[option.key] = option

    phase("timing", "Checking existing deliveries stay on time",
          "Re-driving the day with you in it",
          f"{found.rejected_would_delay} position(s) would have made someone late")
    found.options = sorted(best.values(), key=lambda o: o.rank_key)
    phase("ranking", "Ranking valid choices", "Cheapest detour first",
          f"{len(found.options)} valid from {found.positions_tested} positions tested")
    return found
