"""Turning broad availability into a promise you could actually say out loud.

A customer who says "Friday, any time" is describing a boundary, not asking to be told a nine-hour
arrival window. An experienced coordinator solves the route first and then asks for something
specific: *could you do Friday morning? we'll already be in the East.* This module is that step --
the arithmetic between "when could you" and "would 10 till 12 work".

The window is derived from the arrival OR-Tools actually chose, and the availability only bounds it.
Deriving it from the availability alone would reproduce today's behaviour with extra steps.

Two constraints pull against each other, and the width is where they meet:

- **Narrow enough to be a promise.** Nine hours is not an appointment.
- **Wide enough that the day can still be re-optimised.** solver._normalised_windows reduces a lock
  [S,E] for a job of duration D to an arrival domain of [S, E-D]. The width of THAT is the room a
  later re-solve has to move this stop. At E-S == D it is a single point: the arrival is pinned, and
  one leg re-estimating by a minute makes the whole day infeasible for everyone on it.

So the promise is never the service interval. It is max(120, D + 30) minutes wide, which leaves at
least half an hour of arrival freedom whatever the job.

Note what the failure looks like if this is got wrong. `plan_service.assert_locks_respected` runs
before any plan is published, so a stop can never be published outside its promise -- the day simply
becomes unroutable instead. That means an over-tight promise does NOT show up in
`confirmed_appointments_moved`; it shows up as a customer being told "sorry, that slot was taken
while we were confirming". The metric will not warn you, which is exactly why the slack floor is a
hard rule here rather than a tuning preference.
"""
from __future__ import annotations

from datetime import time as Time

from dispatch_agent.config import settings
from dispatch_agent.models import TimeWindow

SNAP_MINUTES = 30


def minutes_of(value: Time) -> int:
    return value.hour * 60 + value.minute


def time_of(minutes: int) -> Time:
    minutes %= 24 * 60
    return Time(hour=minutes // 60, minute=minutes % 60)


def width_of(window: TimeWindow) -> int:
    return minutes_of(window.end) - minutes_of(window.start)


def promise_window(
    service: TimeWindow,
    availability: TimeWindow,
    promise_minutes: int | None = None,
    min_slack: int | None = None,
    snap: int = SNAP_MINUTES,
) -> TimeWindow:
    """The window to offer a customer, given where the solver actually put the van.

    `service` is the candidate's own StopAssignment.arrival_window -- [arrival, arrival + duration].
    `availability` is what the customer said they could do.

    Guarantees, all asserted below because a violation is a bug rather than a runtime condition:

    - contains the whole service period
    - never extends beyond the customer's availability, nor outside working hours
    - is at least `duration + min_slack` wide, so the solver keeps room to re-optimise --
      UNLESS the customer's own window is already narrower, in which case it is returned
      unchanged, because we cannot promise more room than they gave us
    - starts on a `snap`-minute boundary wherever the availability allows
    """
    promise_minutes = promise_minutes if promise_minutes is not None else settings.promise_window_minutes
    min_slack = min_slack if min_slack is not None else settings.promise_min_slack_minutes

    arrival = minutes_of(service.start)
    departure = minutes_of(service.end)
    duration = departure - arrival

    # Clamp to the working day exactly as the solver does (solver._normalised_windows), so we never
    # offer a window whose edges the solver would silently trim.
    day_start = minutes_of(settings.work_day_start)
    day_end = minutes_of(settings.arrival_cutoff)
    available_start = max(minutes_of(availability.start), day_start)
    available_end = min(minutes_of(availability.end), day_end)

    width = max(promise_minutes, duration + min_slack)

    # The customer's own window is already at least as tight as anything we would propose. Return it
    # verbatim -- narrowing further would promise less room than they offered, and widening would
    # promise time they never gave us.
    if available_end - available_start <= width:
        return TimeWindow(start=time_of(available_start), end=time_of(available_end))

    # The bounds on where the window may start, derived so that ANY start inside them satisfies
    # every invariant. Deriving them first is what makes the clamp below safe in either order.
    #
    #   earliest: not before the customer is free, and late enough that start + width still covers
    #             the end of the job -- snapping the start earlier also pulls the end earlier, which
    #             is how a 105-minute job in a 135-minute window can lose its own last five minutes
    #   latest:   not after the van arrives, and early enough to stay inside the availability
    earliest_start = max(available_start, departure - width)
    latest_start = min(arrival, available_end - width)

    # Centre the window on the service so the van is not due at the very edge of what we promised,
    # then snap to the NEAREST half-hour. Nearest rather than down: a 10:35 arrival should read
    # "10:00 to 12:00", not "09:30 to 11:30". The range above is non-empty because width is at least
    # duration + slack and the solver placed the whole service inside the availability -- both
    # re-asserted below.
    centred = arrival - (width - duration) // 2
    start = min(max(((centred + snap // 2) // snap) * snap, earliest_start), latest_start)

    # If the clamp pushed us off the grid, take the next boundary up when one still fits. A tidy
    # number is worth having, but never at the cost of an invariant.
    if start % snap:
        aligned = -(-start // snap) * snap
        if aligned <= latest_start:
            start = aligned

    end = start + width

    window = TimeWindow(start=time_of(start), end=time_of(end))
    _assert_promise_is_sound(window, service, available_start, available_end, duration, min_slack)
    return window


def _assert_promise_is_sound(
    window: TimeWindow,
    service: TimeWindow,
    available_start: int,
    available_end: int,
    duration: int,
    min_slack: int,
) -> None:
    start, end = minutes_of(window.start), minutes_of(window.end)
    arrival, departure = minutes_of(service.start), minutes_of(service.end)

    assert start <= arrival, f"promise starts at {start} but the van arrives at {arrival}"
    assert departure <= end, f"promise ends at {end} but the job runs to {departure}"
    assert start >= available_start, "promise starts before the customer is available"
    assert end <= available_end, "promise ends after the customer is available"
    assert end - start >= duration + min_slack, (
        f"promise is {end - start} minutes for a {duration}-minute job -- too tight for the solver "
        f"to re-optimise around"
    )


def subtract(window: TimeWindow, excluded: list[TimeWindow], min_width: int) -> list[TimeWindow]:
    """`window` with `excluded` intervals carved out, dropping anything too narrow to book.

    Used when a customer turns down a proposed time: the rejection applies to that window, not to
    their whole day, so the interval is removed and the day is re-solved. The solver already handles
    the disjoint result -- it punches the gaps out with CumulVar.RemoveInterval.

    Pieces narrower than `min_width` are dropped rather than returned: a twelve-minute sliver is not
    a slot anyone can be offered, and passing it on would only produce an infeasible solve.
    """
    pieces = [(minutes_of(window.start), minutes_of(window.end))]

    for gap in excluded:
        gap_start, gap_end = minutes_of(gap.start), minutes_of(gap.end)
        remaining: list[tuple[int, int]] = []
        for piece_start, piece_end in pieces:
            if gap_end <= piece_start or gap_start >= piece_end:
                remaining.append((piece_start, piece_end))  # no overlap
                continue
            if gap_start > piece_start:
                remaining.append((piece_start, gap_start))
            if gap_end < piece_end:
                remaining.append((gap_end, piece_end))
        pieces = remaining

    return [
        TimeWindow(start=time_of(s), end=time_of(e))
        for s, e in pieces
        if e - s >= min_width
    ]
