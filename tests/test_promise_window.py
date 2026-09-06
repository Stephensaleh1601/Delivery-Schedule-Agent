"""The arithmetic between "when could you" and "would 10 till 12 work".

A customer's availability is a boundary. The promise is a narrow window derived from the arrival
OR-Tools actually chose, and it has to satisfy several things at once that pull against each other --
containment, staying inside the availability, and leaving the solver enough room to re-optimise the
day afterwards.

The last one is the subtle one, and it fails silently: `plan_service.assert_locks_respected` runs
before any plan is published, so an over-tight promise never shows up as a moved appointment. It
shows up as a day that will not solve, and a customer told "sorry, that slot was taken while we were
confirming". So the slack floor is tested directly here AND end-to-end against the real solver below.
"""
from datetime import date, time

import pytest

from dispatch_agent import config
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.geo.zones import SINGAPORE_CENTROID
from dispatch_agent.models import Address, JobRecord, JobType, PlanningStatus, TimeWindow
from dispatch_agent.planning.promise_window import minutes_of, promise_window, subtract, width_of
from dispatch_agent.solver import sequence_day

DAY = date(2026, 9, 4)


def W(h1, m1, h2, m2) -> TimeWindow:
    return TimeWindow(start=time(h1, m1), end=time(h2, m2))


# -- the shape of a promise ----------------------------------------------------


def test_a_broad_availability_becomes_a_two_hour_promise():
    """The headline behaviour. Nine hours of availability, a van arriving at 10:35, and what the
    customer is actually asked is 'would 10 till 12 work'."""
    offered = promise_window(service=W(10, 35, 11, 20), availability=W(9, 0, 18, 0))

    assert offered == W(10, 0, 12, 0)


def test_the_promise_follows_the_solved_arrival_not_the_availability():
    """The whole point. Same availability, different arrival, different promise -- if this ever
    stopped holding we would be back to offering whatever the customer asked for."""
    availability = W(9, 0, 18, 0)

    morning = promise_window(service=W(9, 40, 10, 25), availability=availability)
    afternoon = promise_window(service=W(15, 10, 15, 55), availability=availability)

    assert morning != afternoon
    assert minutes_of(morning.start) < minutes_of(afternoon.start)


@pytest.mark.parametrize("duration", [30, 45, 60, 75, 105, 180])
@pytest.mark.parametrize(
    "available",
    [(540, 1080), (540, 780), (780, 1080), (600, 900)],
    ids=["all day", "morning", "afternoon", "midday"],
)
def test_every_arrival_produces_a_sound_promise(duration, available):
    """A sweep rather than a handful of examples, because the failures live at the boundaries --
    an arrival in the first half hour, or in the last."""
    available_start, available_end = available
    if available_end - available_start < duration:
        pytest.skip("the job cannot fit this availability at all")

    for arrival in range(available_start, available_end - duration + 1, 5):
        service = W(arrival // 60, arrival % 60, (arrival + duration) // 60, (arrival + duration) % 60)
        availability = W(
            available_start // 60, available_start % 60, available_end // 60, available_end % 60
        )

        offered = promise_window(service=service, availability=availability)
        start, end = minutes_of(offered.start), minutes_of(offered.end)

        assert start <= arrival, f"promise starts after the van arrives ({arrival})"
        assert arrival + duration <= end, f"promise ends before the job does ({arrival + duration})"
        assert start >= available_start, "promise starts before the customer is free"
        assert end <= available_end, "promise ends after the customer is free"


def test_a_promise_never_pins_the_van_to_a_single_minute():
    """If the promise is exactly as long as the job, the solver's arrival domain is a single point
    and one leg re-estimating by a minute makes the day infeasible for everyone on it."""
    offered = promise_window(service=W(10, 35, 11, 20), availability=W(9, 0, 18, 0))

    slack = width_of(offered) - 45
    assert slack >= config.settings.promise_min_slack_minutes


def test_a_long_job_widens_the_promise_rather_than_squeezing_it():
    """A 105-minute job cannot honestly be promised in a two-hour window with any room to move.
    The window widens; it does not pin the van."""
    offered = promise_window(service=W(10, 35, 12, 20), availability=W(9, 0, 18, 0))

    assert width_of(offered) == 105 + config.settings.promise_min_slack_minutes
    assert offered.start <= time(10, 35) and time(12, 20) <= offered.end


def test_an_availability_narrower_than_the_promise_is_returned_untouched():
    """We cannot promise more room than the customer gave us, and narrowing their own tight window
    further would be inventing a constraint they never agreed to."""
    availability = W(9, 0, 10, 30)

    assert promise_window(service=W(9, 0, 9, 45), availability=availability) == availability


def test_an_availability_exactly_as_long_as_the_job_is_returned_untouched():
    availability = W(10, 0, 11, 45)

    assert promise_window(service=W(10, 0, 11, 45), availability=availability) == availability


def test_promises_land_on_the_half_hour_where_they_can():
    for arrival, duration in [(9 * 60 + 40, 45), (14 * 60 + 20, 45), (11 * 60 + 10, 60)]:
        service = W(arrival // 60, arrival % 60, (arrival + duration) // 60, (arrival + duration) % 60)
        offered = promise_window(service=service, availability=W(9, 0, 18, 0))
        assert offered.start.minute % 30 == 0, f"{offered.start} is not on the half hour"


def test_the_promise_never_escapes_the_working_day():
    """An availability wider than the working day is clamped the way the solver clamps it, rather
    than promising an hour the van will never be out."""
    offered = promise_window(service=W(17, 15, 18, 0), availability=W(6, 0, 23, 0))

    assert offered.start >= config.settings.work_day_start
    assert offered.end <= config.settings.arrival_cutoff


def test_applying_the_promise_twice_changes_nothing():
    availability = W(9, 0, 18, 0)
    service = W(10, 35, 11, 20)

    once = promise_window(service=service, availability=availability)
    twice = promise_window(service=service, availability=once)

    assert twice == once


# -- the solver actually accepts it -------------------------------------------


@pytest.mark.parametrize("duration", [45, 105])
def test_a_day_locked_to_its_promise_still_solves(duration):
    """The invariant that matters, checked against the real solver rather than a restatement of its
    arithmetic. A promise the solver cannot honour is not a promise, it is an outage."""
    availability = W(9, 0, 18, 0)
    job = JobRecord(
        customer_name="Mrs Lee",
        address=Address(
            raw_text="x", postal_code="469123", coordinates=postal_code_to_coords("469123")
        ),
        job_type=JobType.SOFA,
        availability=[availability],
        delivery_date=DAY,
        raw_message="test",
        duration_minutes=duration,
    )

    first = sequence_day([job], DAY, depot=SINGAPORE_CENTROID, time_limit_seconds=1)
    offered = promise_window(service=first.stops[0].arrival_window, availability=availability)

    # Lock it to the promise, exactly as accepting an offer would, and re-solve.
    locked = job.model_copy(
        update={
            "locked_window": offered,
            "availability": [availability],
            "planning_status": PlanningStatus.CONFIRMED,
            "status": "approved",
        }
    )
    again = sequence_day([locked], DAY, depot=SINGAPORE_CENTROID, time_limit_seconds=1)

    stop = again.stops[0]
    assert offered.start <= stop.arrival_window.start
    assert stop.arrival_window.end <= offered.end


# -- carving out a rejected window --------------------------------------------


def test_rejecting_a_window_leaves_the_rest_of_the_day():
    """A rejection applies to the time offered, not to the whole day. Nine hours minus a rejected
    two leaves two bookable pieces -- which the solver already knows how to handle."""
    remaining = subtract(W(9, 0, 18, 0), [W(10, 0, 12, 0)], min_width=60)

    assert remaining == [W(9, 0, 10, 0), W(12, 0, 18, 0)]


def test_a_piece_too_short_for_the_job_is_not_offered_back():
    """Turning down 10-12 out of a nine-hour day leaves an hour before it -- but an hour cannot
    hold a 45-minute job plus the slack the solver needs, so only the afternoon is really on
    offer. Returning the morning anyway would produce an infeasible solve and look like a bug."""
    remaining = subtract(W(9, 0, 18, 0), [W(10, 0, 12, 0)], min_width=45 + 30)

    assert remaining == [W(12, 0, 18, 0)]


def test_slivers_too_small_to_book_are_dropped():
    """A twelve-minute gap is not a slot anyone can be offered; passing it to the solver would only
    produce an infeasible day."""
    remaining = subtract(W(9, 0, 18, 0), [W(9, 12, 17, 50)], min_width=75)

    assert remaining == [], f"kept an unbookable sliver: {remaining}"


def test_rejecting_everything_leaves_nothing():
    assert subtract(W(9, 0, 12, 0), [W(9, 0, 12, 0)], min_width=30) == []


def test_a_rejection_outside_the_window_changes_nothing():
    window = W(13, 0, 18, 0)

    assert subtract(window, [W(9, 0, 11, 0)], min_width=30) == [window]


def test_several_rejections_accumulate():
    remaining = subtract(W(9, 0, 18, 0), [W(10, 0, 12, 0), W(14, 0, 15, 0)], min_width=60)

    assert remaining == [W(9, 0, 10, 0), W(12, 0, 14, 0), W(15, 0, 18, 0)]
