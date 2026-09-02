"""Solver guarantees that the multi-day planning flow depends on:

- a customer with disjoint windows is never scheduled in the gap between them
- the service itself finishes inside the window, not merely starts in it
- a locked window is the job's only option, and an unsatisfiable set of locks is reported as
  such rather than resolved by moving someone
- the drive home is counted
"""
from datetime import date, time

import pytest

from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.geo.zones import SINGAPORE_CENTROID
from dispatch_agent.models import Address, JobRecord, JobType, TimeWindow
from dispatch_agent.solver import LockedPlanInfeasibleError, UnsolvableDayError, sequence_day

DAY = date(2026, 8, 28)


def _job(name, postal_code, windows, duration_minutes=30, locked=None):
    return JobRecord(
        customer_name=name,
        address=Address(raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)),
        job_type=JobType.SOFA,
        availability=[TimeWindow(start=time(*s), end=time(*e)) for s, e in windows],
        locked_window=TimeWindow(start=time(*locked[0]), end=time(*locked[1])) if locked else None,
        delivery_date=DAY,
        raw_message="test",
        duration_minutes=duration_minutes,
    )


def _minutes(t):
    return t.hour * 60 + t.minute


def _solve(jobs):
    return sequence_day(jobs, DAY, depot=SINGAPORE_CENTROID, time_limit_seconds=1)


def test_never_schedules_inside_a_gap_between_disjoint_windows():
    """The whole point of RemoveInterval.

    Constructed so the solver actively *wants* the gap. The depot is ~22 minutes from this
    postal district, so the customer's own 09:00-09:30 window is unreachable -- a 30-minute job
    would have to arrive at exactly 09:00 to finish by 09:30, and the van cannot get there
    before 09:22. The earliest arrival the route can otherwise offer is therefore 09:22, which
    is squarely inside the 09:30-14:00 gap. Only the domain hole pushes it out to the afternoon.

    Verified by mutation: commenting out the RemoveInterval call schedules this stop at 09:25,
    which runs to 09:55 -- straight into the gap -- and fails the assertion below.

    The assertion checks the property that actually matters: the whole visit lies inside ONE of
    the customer's stated windows. Checking "not between 09:30 and 14:00" would have passed on
    the 09:25 start, because the arrival is outside the gap even though the visit is not.
    """
    jobs = [_job("Gap", "018956", [((9, 0), (9, 30)), ((14, 0), (17, 0))])]
    sequence = _solve(jobs)
    stop = next(s for s in sequence.stops if s.job_id == jobs[0].id)

    fits_a_stated_window = any(
        _minutes(w.start) <= _minutes(stop.arrival_window.start)
        and _minutes(stop.arrival_window.end) <= _minutes(w.end)
        for w in jobs[0].availability
    )
    assert fits_a_stated_window, (
        f"visit {stop.arrival_window.start}-{stop.arrival_window.end} does not fit inside any "
        f"window the customer offered ({[(str(w.start), str(w.end)) for w in jobs[0].availability]})"
    )


def test_service_must_finish_inside_the_window():
    """A 90-minute job with only a 09:00-10:00 window cannot start at 09:00 and run to 10:30:
    the window means the customer is home until 10:00, not that they answer the door by then."""
    with pytest.raises(UnsolvableDayError, match="cannot fit"):
        _solve([_job("Tight", "018956", [((9, 0), (10, 0))], duration_minutes=90)])


def test_locked_window_is_the_only_option_considered():
    """The job is nominally free all day, but its lock pins it to the afternoon. The solver must
    honour the lock and ignore the wider availability it would otherwise prefer."""
    jobs = [
        _job("Locked", "018956", [((9, 0), (18, 0))], locked=((14, 0), (15, 0))),
        _job("Other", "119613", [((9, 0), (18, 0))]),
    ]
    sequence = _solve(jobs)
    locked_stop = next(s for s in sequence.stops if s.job_id == jobs[0].id)
    assert 14 * 60 <= _minutes(locked_stop.arrival_window.start)
    assert _minutes(locked_stop.arrival_window.end) <= 15 * 60


def test_conflicting_locks_raise_locked_plan_infeasible_naming_the_jobs():
    """Two customers promised overlapping hour-long windows on opposite sides of the island.
    This is a human problem -- the solver must say so, not quietly move one of them."""
    jobs = [
        _job("West", "638123", [((9, 0), (18, 0))], duration_minutes=60, locked=((9, 0), (10, 0))),
        _job("East", "486123", [((9, 0), (18, 0))], duration_minutes=60, locked=((9, 0), (10, 0))),
    ]
    with pytest.raises(LockedPlanInfeasibleError) as exc:
        _solve(jobs)
    assert set(exc.value.locked_job_ids) == {jobs[0].id, jobs[1].id}
    assert exc.value.delivery_date == DAY


def test_unfittable_unlocked_job_is_not_blamed_on_the_locks():
    """When the confirmed appointments are mutually fine and an unconfirmed job is what cannot
    be added, the error must be a plain UnsolvableDayError naming that customer -- raising
    LockedPlanInfeasibleError here would send a coordinator hunting the wrong problem."""
    jobs = [
        _job("Confirmed", "018956", [((9, 0), (18, 0))], locked=((9, 0), (10, 0))),
        _job("Impossible", "119613", [((9, 0), (9, 20))], duration_minutes=120),
    ]
    with pytest.raises(UnsolvableDayError) as exc:
        _solve(jobs)
    assert not isinstance(exc.value, LockedPlanInfeasibleError)
    assert "Impossible" in str(exc.value)


def test_total_includes_the_drive_home():
    jobs = [_job("A", "018956", [((9, 0), (18, 0))]), _job("B", "486123", [((9, 0), (18, 0))])]
    sequence = _solve(jobs)
    assert sequence.return_drive_minutes > 0
    assert sequence.round_trip_drive_minutes == sequence.total_drive_minutes + sequence.return_drive_minutes


def test_supplied_matrix_bypasses_the_routing_client():
    """Candidate evaluation solves the same day repeatedly; it must be able to hand the solver a
    precomputed matrix rather than re-fetching drive times for every option."""

    class ExplodingClient:
        def matrix(self, points):
            raise AssertionError("solver fetched drive times despite being given a matrix")

    jobs = [_job("A", "018956", [((9, 0), (18, 0))]), _job("B", "119613", [((9, 0), (18, 0))])]
    flat = [[0, 10, 20], [10, 0, 15], [20, 15, 0]]
    sequence = sequence_day(
        jobs, DAY, depot=SINGAPORE_CENTROID, routing_client=ExplodingClient(), matrix=flat, time_limit_seconds=1
    )
    assert len(sequence.stops) == 2
