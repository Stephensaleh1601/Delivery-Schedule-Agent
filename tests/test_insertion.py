"""The insertion search: what it measures, what it refuses, and why the order never varies.

These run against the real seeded demo, not a toy fixture, because the number the demo depends on
-- exactly three alternatives for the difficult customer -- is a property of where the seeded
stops are, and a synthetic route would prove nothing about it.
"""
import sys
from datetime import date as Date, time as Time
from pathlib import Path

import pytest

from dispatch_agent import config
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.models import PlanningStatus, TimeWindow
from dispatch_agent.planning import insertion
from dispatch_agent.planning.clock import PlanningClock

BASE = Date(2026, 9, 2)  # a Wednesday, so the cycle is that week's Friday and Saturday

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


@pytest.fixture()
def seeded(temp_db, monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())
    import seed_test_clients

    seed_test_clients.seed()
    return temp_db


def _customer(repo, name):
    return next(j for j in repo.all_jobs() if j.customer_name == name)


def _difficult(repo):
    return _customer(repo, "Mr Rajan")


def _best_case(repo):
    return _customer(repo, "Mrs Chua")


# -- what it looks at -----------------------------------------------------------


def test_every_stop_on_both_routes_is_compared(seeded):
    """"Compared 18 stops" is shown to a judge, so it has to be a count, not a claim.

    No early exit once enough anchors are found: the panel would then be reporting how hard the
    search happened to work rather than how big the problem was.
    """
    found = insertion.search(seeded, _difficult(seeded))

    total = sum(len(seeded.active_plan(d).sequence.stops) for d in PlanningClock.horizon_dates())
    assert found.routes_checked == 2
    assert found.stops_checked == total == 16


def test_anchors_beyond_the_radius_are_dropped(seeded):
    """The 10km filter, measured rather than assumed."""
    order = _difficult(seeded)
    client = RoutingClient()

    found = insertion.search(seeded, order)

    near = 0
    for day in PlanningClock.horizon_dates():
        for stop in seeded.active_plan(day).sequence.stops:
            job = seeded.get_job(stop.job_id)
            if client.distance_km(order.address.coordinates, job.address.coordinates) <= 10.0:
                near += 1
    assert found.anchors_within_radius == near
    assert near < found.stops_checked, "the filter must actually exclude something"


def test_a_smaller_radius_finds_fewer_anchors(seeded):
    wide = insertion.search(seeded, _difficult(seeded), radius_km=10.0)
    narrow = insertion.search(seeded, _difficult(seeded), radius_km=4.0)

    assert narrow.anchors_within_radius < wide.anchors_within_radius
    assert len(narrow.options) <= len(wide.options)


def test_both_sides_of_an_anchor_are_tested(seeded):
    """Before and after. Testing one side only means the cheaper half of every gap is invisible."""
    found = insertion.search(seeded, _difficult(seeded))

    # n anchors give at most 2n positions, and fewer only where anchors are adjacent -- which is
    # deduplication, not a missed test.
    assert found.positions_tested > found.anchors_within_radius / 2
    assert found.positions_tested <= 2 * found.anchors_within_radius


# -- what it works out ----------------------------------------------------------


def test_added_distance_is_the_detour_not_the_leg(seeded):
    """d(prev,c) + d(c,next) - d(prev,next). A stop that sits on the way adds almost nothing;
    charging it the full leg would make every insertion look equally bad."""
    order = _difficult(seeded)
    client = RoutingClient()
    found = insertion.search(seeded, order)
    assert found.options

    for option in found.options:
        plan = seeded.active_plan(option.date)
        names = ["Depot"] + [seeded.get_job(s.job_id).customer_name for s in plan.sequence.stops]
        names.append("Depot")
        assert option.previous_stop == names[option.insert_position - 1]
        assert option.next_stop == names[option.insert_position]
        # A detour is never negative, and never more than going out and back.
        assert option.added_distance_km >= 0
        assert option.added_distance_km <= 2 * option.anchor_distance_km + 25


def test_the_depot_legs_are_priced_at_the_ends_of_a_route(seeded):
    """A first or last stop is inserted between the depot and a customer. Treating the depot leg
    as free makes the ends of every route look like the cheapest place to put anyone."""
    order = _best_case(seeded)
    found = insertion.search(seeded, order)

    ends = [o for o in found.options if o.previous_stop == "Depot" or o.next_stop == "Depot"]
    for option in ends:
        assert option.added_distance_km > 0, "a depot leg still costs something"


def test_an_insertion_that_would_delay_someone_is_refused(seeded):
    """Nobody is moved by an insertion -- but everybody after it can be made late, and only the
    forward simulation sees that.

    Forced by shrinking the day: with the hard route end pulled back to the early afternoon, the
    evening stops can no longer be reached at all, so every candidate ahead of them fails.
    """
    loose = insertion.search(seeded, _difficult(seeded))
    assert loose.options, "the scenario must be solvable before we constrain it"

    with pytest.MonkeyPatch.context() as m:
        m.setattr(config.settings, "hard_route_end", Time(13, 0))
        tight = insertion.search(seeded, _difficult(seeded))

    assert tight.rejected_would_delay > 0
    assert len(tight.options) < len(loose.options)


def test_a_shift_is_fine_because_a_window_is_a_range(seeded):
    """Inserting a stop does delay everyone after it. That is allowed, and this is why.

    A promise is a broad arrival window, not a clock time, so a later stop can move within its
    window without the promise breaking -- the check is against the window, never against the
    minute the last plan happened to choose. On the seeded Friday every stop has at least two
    hours of room before its window closes, and the dearest insertion costs eighteen minutes.

    The finishing time usually does not move at all, which is the same fact from the other end:
    the later stops were already waiting for their windows to open, so the detour is spent out of
    idle time rather than added to the end of the day.
    """
    order = _difficult(seeded)
    found = insertion.search(seeded, order)
    assert found.options

    for option in found.options:
        plan = seeded.active_plan(option.date)
        slack = []
        for stop in plan.sequence.stops:
            job = seeded.get_job(stop.job_id)
            arrival = stop.arrival_window.start.hour * 60 + stop.arrival_window.start.minute
            closes = job.locked_window.end.hour * 60 + job.locked_window.end.minute
            slack.append(closes - arrival)
        assert min(slack) > option.added_minutes, (
            "the insertion must cost less than the room the narrowest promise still has"
        )
        assert option.finish_after <= option.finish_before or option.added_minutes > 0


def test_an_arrival_outside_every_window_is_not_offered(seeded):
    """An arrival is a real time; a delivery window is a promise we have words for. One that falls
    outside all three cannot be offered as morning, afternoon or evening, so it is dropped rather
    than rounded into the nearest one."""
    found = insertion.search(seeded, _difficult(seeded))

    for option in found.options:
        assert option.slot.contains(option.expected_arrival)


# -- what it returns ------------------------------------------------------------


def test_one_option_per_date_and_window(seeded):
    """Two positions in the same afternoon are one choice to a customer. Offering both as if they
    were different choices is padding the list to reach three."""
    found = insertion.search(seeded, _difficult(seeded))

    keys = [o.key for o in found.options]
    assert len(keys) == len(set(keys))


def test_the_ranking_is_deterministic(seeded):
    """Same data, same three, every run -- or the recording cannot be repeated."""
    runs = [
        [(o.date, o.slot.name, o.added_distance_km) for o in insertion.search(seeded, _difficult(seeded)).options]
        for _ in range(3)
    ]
    assert runs[0] == runs[1] == runs[2]

    options = insertion.search(seeded, _difficult(seeded)).options
    assert [o.rank_key for o in options] == sorted(o.rank_key for o in options)


def test_an_excluded_choice_is_removed_not_reranked(seeded):
    """A slot the customer turned down is gone. Not demoted -- a declined choice does not become
    attractive by being cheap."""
    order = _difficult(seeded)
    before = insertion.search(seeded, order)
    assert before.options
    declined = before.options[0].key

    after = insertion.search(seeded, order, exclude={declined})

    assert declined not in {o.key for o in after.options}
    assert after.excluded_by_customer > 0


# -- the scenario the demo depends on -------------------------------------------


def test_the_difficult_customer_has_exactly_three_alternatives(seeded):
    """The number the whole demo turns on, pinned so the seed cannot drift away from it.

    Mr Rajan is in Toa Payoh, which is Central, so his cluster day is Saturday. Declining the
    Saturday morning offer must leave exactly three -- and two of them on Friday, which is the
    point being made: his region says Saturday, but the Friday van already passes near him.
    """
    order = _difficult(seeded)
    friday, saturday = PlanningClock.horizon_dates()

    found = insertion.search(seeded, order, exclude={(saturday, "morning")})

    assert len(found.options) == 3, [(o.date, o.slot.name) for o in found.options]
    assert {(o.date, o.slot.name) for o in found.options} == {
        (friday, "morning"),
        (friday, "afternoon"),
        (saturday, "afternoon"),
    }
    assert sum(1 for o in found.options if o.date == friday) == 2


def test_the_best_case_customer_can_be_placed_on_their_cluster_day(seeded):
    """Mrs Chua is in the East, so Friday is hers, and a Friday morning option must exist for the
    happy path to be a happy path."""
    friday, _ = PlanningClock.horizon_dates()

    found = insertion.search(seeded, _best_case(seeded), dates=[friday])

    assert found.options
    assert {o.date for o in found.options} == {friday}
    assert "morning" in {o.slot.name for o in found.options}


def test_the_evidence_counts_survive_serialisation(seeded):
    """These become the judge-facing rows, so they travel as data rather than prose."""
    data = insertion.search(seeded, _difficult(seeded)).to_data()

    assert data["routes_checked"] == 2
    assert data["stops_checked"] == 16
    assert data["anchor_radius_km"] == 10
    assert data["valid_count"] == len(data["options"])
    assert all(o["promises_moved"] == 0 for o in data["options"])
    assert all(o["source_plan_version"] >= 1 for o in data["options"])
