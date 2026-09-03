"""The words the agent puts to a customer, checked against the route it actually solved.

The failure this guards against is not a crash. It is a fluent, plausible sentence -- "we'll
already be in the East" -- said about a van that is going west. Every assertion here compares the
text to the solved sequence rather than to another string.
"""
from datetime import date, time

import pytest

from dispatch_agent.geo.postal_codes import DISTRICT_TO_REGION, region_of
from dispatch_agent.geo.zones import SINGAPORE_CENTROID
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.models import Address, JobRecord, JobType, TimeWindow
from dispatch_agent.planning.route_facts import (
    coordinator_reason,
    customer_reason,
    route_facts,
)
from dispatch_agent.solver import sequence_day

DAY = date(2026, 9, 4)

# Real postal codes, one per region we name. Chosen so a reader can check them.
EAST = ["469123", "529536", "460115"]      # Bedok, Tampines, Bedok North
WEST = ["608600", "640500"]                 # Jurong East, Jurong West


def _job(name, postal_code, duration=45):
    return JobRecord(
        customer_name=name,
        address=Address(
            raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)
        ),
        job_type=JobType.SOFA,
        availability=[TimeWindow(start=time(9, 0), end=time(18, 0))],
        delivery_date=DAY,
        raw_message="test",
        duration_minutes=duration,
    )


def _solve(jobs):
    return sequence_day(jobs, DAY, depot=SINGAPORE_CENTROID, time_limit_seconds=1)


# -- the region table ---------------------------------------------------------


def test_every_postal_district_has_a_region():
    """A district with no region silently produces a reason with a hole in it."""
    assert set(DISTRICT_TO_REGION) == set(range(1, 29))


def test_a_district_names_the_same_region_however_it_was_geocoded():
    """The region must not depend on whether a geocoder was reachable, or the reason a customer is
    given would change with the weather."""
    assert region_of("469123") == region_of("469123", postal_code_to_coords("469123")) == "East"


def test_an_address_with_no_postal_code_still_gets_a_region():
    assert region_of(None, postal_code_to_coords("469123")) is not None


# -- facts read off the solved route ------------------------------------------


def test_position_and_neighbours_come_from_the_sequence_not_the_input_order():
    """`position` is where the solver put the stop. If this were the input index, the reason would
    describe a route nobody drives."""
    jobs = [_job(f"C{i}", pc) for i, pc in enumerate(EAST)]
    sequence = _solve(jobs)
    jobs_by_id = {j.id: j for j in jobs}

    for job in jobs:
        facts = route_facts(sequence, job, jobs_by_id)
        expected = [s.job_id for s in sequence.stops].index(job.id) + 1
        assert facts.position == expected
        assert facts.stop_count == len(jobs)


def test_the_neighbours_either_side_are_the_ones_the_solver_put_there():
    jobs = [_job(f"C{i}", pc) for i, pc in enumerate(EAST)]
    sequence = _solve(jobs)
    jobs_by_id = {j.id: j for j in jobs}
    names = [jobs_by_id[s.job_id].customer_name for s in sequence.stops]

    facts = route_facts(sequence, jobs_by_id[sequence.stops[1].job_id], jobs_by_id)

    assert facts.previous_customer == names[0]
    assert facts.next_customer == names[2]


def test_the_first_stop_has_nobody_before_it():
    jobs = [_job(f"C{i}", pc) for i, pc in enumerate(EAST)]
    sequence = _solve(jobs)
    jobs_by_id = {j.id: j for j in jobs}

    facts = route_facts(sequence, jobs_by_id[sequence.stops[0].job_id], jobs_by_id)

    assert facts.previous_customer is None
    assert facts.next_customer is not None


def test_a_job_not_in_the_sequence_is_a_caller_bug_not_a_blank_reason():
    jobs = [_job("A", EAST[0])]
    sequence = _solve(jobs)

    with pytest.raises(ValueError):
        route_facts(sequence, _job("Stranger", WEST[0]), {j.id: j for j in jobs})


# -- what is claimed in the sentence ------------------------------------------


def test_the_area_claim_is_only_made_when_the_van_is_actually_in_the_area():
    """The sentence this product turns on. An eastern customer among eastern stops may be told the
    van will already be there; a lone western customer among them may NOT."""
    jobs = [_job(f"E{i}", pc) for i, pc in enumerate(EAST)] + [_job("W", WEST[0])]
    sequence = _solve(jobs)
    jobs_by_id = {j.id: j for j in jobs}

    eastern = route_facts(sequence, jobs[0], jobs_by_id)
    western = route_facts(sequence, jobs[-1], jobs_by_id)

    assert eastern.region == "East" and eastern.already_in_the_area
    assert western.region == "West" and not western.already_in_the_area


def test_a_lone_stop_is_never_told_we_will_already_be_there():
    job = _job("Only", EAST[0])
    sequence = _solve([job])

    facts = route_facts(sequence, job, {job.id: job})

    assert not facts.already_in_the_area
    assert facts.opens_empty_day
    reason = customer_reason(facts, DAY, TimeWindow(start=time(10, 0), end=time(12, 0)))
    assert "already" not in reason.lower()


def test_the_customer_sentence_names_no_other_customer():
    """A customer-facing reason leaking another customer's name is a privacy failure, not a wording
    preference."""
    jobs = [_job(name, pc) for name, pc in zip(["Mrs Lee", "Mr Ong", "Ms Chua"], EAST)]
    sequence = _solve(jobs)
    jobs_by_id = {j.id: j for j in jobs}

    facts = route_facts(sequence, jobs[0], jobs_by_id)
    reason = customer_reason(facts, DAY, TimeWindow(start=time(10, 0), end=time(12, 0)))

    for other in ("Mr Ong", "Ms Chua"):
        assert other not in reason


def test_the_customer_sentence_quotes_no_score_or_penalty():
    jobs = [_job(f"C{i}", pc) for i, pc in enumerate(EAST)]
    sequence = _solve(jobs)
    facts = route_facts(sequence, jobs[0], {j.id: j for j in jobs})

    reason = customer_reason(facts, DAY, TimeWindow(start=time(10, 0), end=time(12, 0)))

    for leak in ("score", "penalty", "min", "cost"):
        assert leak not in reason.lower()


def test_morning_and_afternoon_follow_the_promised_window():
    """A reason that names the time of day must name the RIGHT one.

    Uses a lone stop, because the "we'll already be nearby" branch deliberately says "around then"
    instead: that sentence is a claim about the hour the van is in the area, and pinning it to
    "Tuesday afternoon" is how it came to be said about a route whose only eastern stop was 9am.
    """
    job = _job("Only", EAST[0])
    sequence = _solve([job])
    facts = route_facts(sequence, job, {job.id: job})

    morning = customer_reason(facts, DAY, TimeWindow(start=time(9, 30), end=time(11, 30)))
    afternoon = customer_reason(facts, DAY, TimeWindow(start=time(14, 0), end=time(16, 0)))

    assert "morning" in morning and "afternoon" not in morning
    assert "afternoon" in afternoon and "morning" not in afternoon


def test_the_coordinator_sentence_states_the_real_position_and_driving():
    jobs = [_job(name, pc) for name, pc in zip(["Mrs Lee", "Mr Ong", "Ms Chua"], EAST)]
    sequence = _solve(jobs)
    jobs_by_id = {j.id: j for j in jobs}
    middle = jobs_by_id[sequence.stops[1].job_id]

    reason = coordinator_reason(route_facts(sequence, middle, jobs_by_id), added_drive_minutes=8)

    assert "Stop 2 of 3" in reason
    assert "adding 8 minutes of driving" in reason


def test_no_extra_driving_is_said_plainly_rather_than_as_zero_minutes():
    job = _job("Only", EAST[0])
    sequence = _solve([job])

    reason = coordinator_reason(route_facts(sequence, job, {job.id: job}), added_drive_minutes=0)

    assert "no extra driving" in reason
    assert "0 driving minutes" not in reason
