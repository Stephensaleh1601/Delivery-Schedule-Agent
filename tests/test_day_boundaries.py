"""Three different times the day can be said to end, and why one setting could not be all of them.

`work_day_end` used to do three jobs at once: clamp every customer's arrival window, bound the
depot's return, and -- via `soft_day_end` -- decide what counts as overtime. That worked while the
last promise was 18:00 and everybody was home well before the day ended. It stops working the
moment a customer is promised "evening, 5 to 9": the arrival is legal at 20:59, the service runs
past 21:00, and the drive home lands later still.

    arrival cutoff  21:00   no customer is promised an arrival after this
    soft day end    22:00   work past this is overtime, and penalised
    hard route end  22:30   a route returning later is infeasible

Each must sit strictly past the one before. Collapsing the soft end onto the arrival cutoff bills
an ordinary evening delivery as overtime; collapsing the hard end onto the soft end makes the
overtime term unreachable, which is the trap config.py already warns about for the old pair.
"""
from datetime import date as Date, time as Time

import pytest

from dispatch_agent import config
from dispatch_agent.config import ConfigurationError
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.geo.zones import company_depot
from dispatch_agent.models import (
    Address,
    Coordinates,
    DaySequence,
    JobRecord,
    JobType,
    PlanningStatus,
    StopAssignment,
    TimeWindow,
)
from dispatch_agent.planning import scoring
from dispatch_agent.solver import UnsolvableDayError, sequence_day

FRIDAY = Date(2026, 9, 4)


def _w(sh, sm, eh, em):
    return TimeWindow(start=Time(sh, sm), end=Time(eh, em))


def _job(name, postal_code, window, duration=15):
    return JobRecord(
        customer_name=name,
        address=Address(
            raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)
        ),
        job_type=JobType.SOFA,
        delivery_date=FRIDAY,
        availability=[window],
        duration_minutes=duration,
        raw_message="seed",
    )


# -- overtime -------------------------------------------------------------------


def test_a_normal_evening_delivery_is_not_overtime():
    """The failure this pins: measuring overtime against the arrival cutoff.

    `completion_minutes` is the last arrival PLUS its service PLUS the drive home, so an arrival
    at 20:45 finishes at 21:00 and lands back at the depot at 21:25. Judged against a 21:00 soft
    end that is 25 minutes of overtime on an entirely ordinary delivery -- and the scorer would
    learn to avoid the evening window we just built.
    """
    sequence = DaySequence(
        delivery_date=FRIDAY,
        stops=[
            StopAssignment(
                job_id="j1", sequence_index=1, arrival_window=_w(20, 45, 21, 0), drive_minutes_from_prev=20
            )
        ],
        total_drive_minutes=20,
        return_drive_minutes=25,
    )
    assert sequence.completion_minutes == 21 * 60 + 25

    overtime = scoring.overtime_minutes(
        sequence, {}, company_depot(), scoring.ScoringConfig.from_settings()
    )
    assert overtime == 0, "an arrival inside the evening window is not overtime"


def test_work_genuinely_past_the_soft_end_is_still_overtime():
    """The term must stay reachable -- moving the soft end out is not switching it off."""
    sequence = DaySequence(
        delivery_date=FRIDAY,
        stops=[
            StopAssignment(
                job_id="j1", sequence_index=1, arrival_window=_w(20, 45, 21, 0), drive_minutes_from_prev=20
            )
        ],
        total_drive_minutes=20,
        return_drive_minutes=75,  # a long haul back from the far west
    )

    overtime = scoring.overtime_minutes(
        sequence, {}, company_depot(), scoring.ScoringConfig.from_settings()
    )
    assert overtime == 15, "22:15 is a quarter hour past the soft end"


# -- the arrival cutoff ---------------------------------------------------------


def test_an_evening_arrival_is_schedulable():
    """A 17:00-21:00 promise has to be solvable, which it is not while the day ends at 18:00."""
    sequence = sequence_day([_job("Mr Tan", "469123", _w(17, 0, 21, 0))], FRIDAY, company_depot())

    arrival = sequence.stops[0].arrival_window.start
    assert Time(17, 0) <= arrival <= Time(21, 0)


def test_service_may_run_past_a_promised_slot():
    """A locked window is a promise about ARRIVAL: we said we would turn up between 5 and 9.

    Reserving the service duration inside it would shorten every slot we promise by the job
    length, so an evening slot would silently stop accepting arrivals after 20:45.
    """
    job = _job("Mr Tan", "469123", _w(17, 0, 21, 0), duration=30)
    job.locked_window = _w(20, 45, 21, 0)
    job.availability = [job.locked_window]
    job.set_planning_status(PlanningStatus.CONFIRMED)

    sequence = sequence_day([job], FRIDAY, company_depot())

    stop = sequence.stops[0]
    assert stop.arrival_window.start >= Time(20, 45)
    assert stop.arrival_window.end > Time(21, 0), "service should be allowed past the cutoff"


def test_stated_availability_still_reserves_the_service():
    """The other half of the same rule, and the reason it is not a blanket setting.

    "I'm home 9 to 9:30" says when the customer is THERE. A 30-minute job cannot be squeezed in
    at 9:22 -- that runs through a gap they told us about, which is precisely the failure
    `require_service_within_window` exists to prevent.
    """
    with pytest.raises(UnsolvableDayError, match="cannot fit"):
        sequence_day(
            [_job("Ms Ong", "469123", _w(9, 0, 9, 30), duration=45)], FRIDAY, company_depot()
        )


# -- the hard route end ---------------------------------------------------------


def test_a_route_that_cannot_get_home_in_time_is_infeasible():
    """The depot's own window is the hard end. A late arrival far from base must be refused."""
    far_from_depot = "640690"  # Jurong West, right across the island

    with pytest.raises(UnsolvableDayError):
        sequence_day(
            [_job("Ms Lim", far_from_depot, _w(22, 15, 22, 30), duration=60)],
            FRIDAY,
            company_depot(),
        )


# -- the ordering itself --------------------------------------------------------


def test_the_three_boundaries_must_increase(monkeypatch):
    """Config that cannot be honoured fails at startup rather than scoring strangely for a week."""
    monkeypatch.setattr(config.settings, "soft_day_end", Time(21, 0))
    monkeypatch.setattr(config.settings, "arrival_cutoff", Time(21, 0))

    with pytest.raises(ConfigurationError, match="arrival cutoff"):
        config.validate(config.settings)


def test_the_hard_end_must_be_past_the_soft_end(monkeypatch):
    monkeypatch.setattr(config.settings, "hard_route_end", Time(22, 0))
    monkeypatch.setattr(config.settings, "soft_day_end", Time(22, 0))

    with pytest.raises(ConfigurationError, match="overtime"):
        config.validate(config.settings)


def test_the_shipped_defaults_are_valid():
    config.validate(config.settings)
