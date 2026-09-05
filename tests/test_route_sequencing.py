"""The daily route optimisation, kept separate from the appointment negotiation.

These are two different problems and it is worth saying so in tests as well as in prose. Negotiation
decides WHICH day and window a customer is promised; sequencing decides the ORDER of the stops once
a day's work is fixed. This file is only about the second.

Nothing here asserts an expected region order. "East in the morning, West after lunch" is a pattern
that emerges from real travel times on a particular day's addresses, not a rule -- hardcoding it
would turn a test of the solver into a test of my guess about Singapore traffic, and would pass even
if the solver stopped working.
"""
from datetime import date, time

import pytest

from dispatch_agent.geo.routing_client import haversine_drive_minutes
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.geo.zones import SINGAPORE_CENTROID
from dispatch_agent.models import Address, JobRecord, JobType, TimeWindow
from dispatch_agent.solver import sequence_day

DAY = date(2026, 9, 4)
DEPOT = postal_code_to_coords("487372")  # Changi Business Park, the demo depot's district

# Real postal codes, spread across the island so the ordering has something to work with.
ADDRESSES = [
    ("Mrs Lee", "469123"),    # Bedok
    ("Mr Ong", "529536"),     # Tampines
    ("Ms Chua", "460115"),    # Bedok North
    ("Mr Tan", "608600"),     # Jurong East
    ("Mrs Goh", "640500"),    # Jurong West
    ("Mr Sim", "310100"),     # Toa Payoh
]


def _w(h1, m1, h2, m2):
    return TimeWindow(start=time(h1, m1), end=time(h2, m2))


def _job(name, postal_code, window=(9, 0, 18, 0), duration=45, locked=None):
    return JobRecord(
        customer_name=name,
        address=Address(
            raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)
        ),
        job_type=JobType.SOFA,
        availability=[_w(*window)],
        locked_window=_w(*locked) if locked else None,
        planning_status="confirmed" if locked else "pending_planning",
        status="approved" if locked else "new",
        delivery_date=DAY,
        raw_message="test",
        duration_minutes=duration,
    )


def _solve(jobs):
    return sequence_day(jobs, DAY, depot=DEPOT, time_limit_seconds=2)


def _round_trip(order, depot=DEPOT):
    """Drive minutes for a given order of jobs, depot out and back -- the naive baseline."""
    points = [depot] + [j.address.coordinates for j in order] + [depot]
    return sum(
        haversine_drive_minutes(points[i], points[i + 1]) for i in range(len(points) - 1)
    )


# -- the sequencing itself ----------------------------------------------------


def test_every_stop_is_visited_exactly_once():
    jobs = [_job(n, pc) for n, pc in ADDRESSES]

    sequence = _solve(jobs)

    assert sorted(s.job_id for s in sequence.stops) == sorted(j.id for j in jobs)


def test_the_optimised_route_is_no_worse_than_the_order_they_were_entered_in():
    """The claim the product makes. Not "optimal" -- OR-Tools runs guided local search on a time
    budget and makes no such promise -- but a route that is worse than the order a coordinator
    typed them in would make the whole exercise pointless."""
    jobs = [_job(n, pc) for n, pc in ADDRESSES]

    sequence = _solve(jobs)
    ordered = {j.id: j for j in jobs}
    solved = [ordered[s.job_id] for s in sequence.stops]

    assert _round_trip(solved) <= _round_trip(jobs) + 1  # a minute of rounding slack


def test_the_route_returns_to_the_depot():
    """RETURN_TO_DEPOT is on for the demo, and the last leg is real driving that has to be counted
    -- a route measured without it understates every day by the trip home."""
    jobs = [_job(n, pc) for n, pc in ADDRESSES[:3]]

    sequence = _solve(jobs)

    assert sequence.return_drive_minutes > 0
    assert sequence.round_trip_drive_minutes > sequence.total_drive_minutes


def test_each_stop_lands_inside_the_window_it_was_given():
    """Half the day free in the morning, half in the afternoon. Whatever order the solver picks,
    nobody is visited outside the hours they gave."""
    windows = [(9, 0, 13, 0), (13, 0, 18, 0), (9, 0, 13, 0), (13, 0, 18, 0)]
    jobs = [_job(n, pc, window=w) for (n, pc), w in zip(ADDRESSES, windows)]

    sequence = _solve(jobs)
    by_id = {j.id: j for j in jobs}

    for stop in sequence.stops:
        window = by_id[stop.job_id].availability[0]
        assert window.start <= stop.arrival_window.start
        assert stop.arrival_window.end <= window.end, "a job ran past the window it was given"


def test_a_locked_promise_is_honoured_while_the_rest_is_reordered():
    """The two optimisations meeting: a confirmed appointment is a fixed point the sequencer must
    route around, not a preference it may trade away."""
    locked = _job("Mrs Lee", "469123", locked=(10, 0, 12, 0))
    others = [_job(n, pc) for n, pc in ADDRESSES[1:5]]

    sequence = _solve([locked, *others])

    stop = next(s for s in sequence.stops if s.job_id == locked.id)
    assert locked.locked_window.start <= stop.arrival_window.start
    assert stop.arrival_window.start <= locked.locked_window.end


def test_the_solver_is_deterministic_enough_to_demo():
    """Guided local search is stochastic in principle. On a fixed day with a fixed budget it must
    still give the same answer twice, or a rehearsal proves nothing about the recording."""
    jobs = [_job(n, pc) for n, pc in ADDRESSES]

    first = [s.job_id for s in _solve(jobs).stops]
    second = [s.job_id for s in _solve(jobs).stops]

    assert first == second
