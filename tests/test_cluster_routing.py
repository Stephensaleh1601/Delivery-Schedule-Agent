"""Which routes each workflow is allowed to look at.

Insertion proves a slot works in every flow. What differs is the SCOPE, and getting that wrong is
the difference between a coordinator's answer and a machine's: a normal booking that quietly
searches both days is offering someone a day they never asked about, to save a kilometre they
never mentioned.
"""
import sys
from datetime import date as Date
from pathlib import Path

import pytest

from dispatch_agent import config
from dispatch_agent.planning import clusters, tools
from dispatch_agent.planning.clock import PlanningClock

BASE = Date(2026, 9, 2)
FRIDAY = Date(2026, 9, 4)
SATURDAY = Date(2026, 9, 5)

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


def _order(repo, name):
    return next(j for j in repo.all_jobs() if j.customer_name == name)


def _search(repo, name, scope):
    order = _order(repo, name)
    ctx = tools.ToolContext(repo=repo)
    result = tools.dispatch(
        "find_insertion_options", {"order_id": order.id, "scope": scope}, ctx
    )
    return result, ctx.scratch.get("insertion_scope", {})


# -- the rule itself ------------------------------------------------------------


def test_every_region_has_exactly_one_delivery_day(seeded):
    """Seven regions, two days. City sits with Central and North-East with the North side --
    without that, some postal districts would match no day and escalate for a reason nobody
    could explain."""
    from dispatch_agent.geo.postal_codes import DISTRICT_TO_REGION

    for region in sorted(set(DISTRICT_TO_REGION.values())):
        days = [d for d, regions in clusters.REGIONS_BY_WEEKDAY.items() if region in regions]
        assert len(days) == 1, f"{region} maps to {days}"


def test_an_east_customer_belongs_to_friday(seeded):
    placement = clusters.placement_of(_order(seeded, "Mrs Chua"))
    assert placement.region == "East"
    assert placement.day_name == "Friday"


def test_a_central_customer_belongs_to_saturday(seeded):
    placement = clusters.placement_of(_order(seeded, "Mr Rajan"))
    assert placement.region == "Central"
    assert placement.day_name == "Saturday"


# -- 1 & 2: the normal booking looks at one day ---------------------------------


def test_an_east_customer_with_no_day_stated_searches_friday_only(seeded):
    result, scope = _search(seeded, "Mrs Chua", "cluster")

    assert result.ok, result.summary
    assert scope["dates"] == [FRIDAY.isoformat()]
    assert {o["date"] for o in result.data["options"]} == {FRIDAY.isoformat()}


def test_a_central_customer_searches_saturday_only(seeded):
    result, scope = _search(seeded, "Mr Rajan", "cluster")

    assert result.ok, result.summary
    assert scope["dates"] == [SATURDAY.isoformat()]
    assert {o["date"] for o in result.data["options"]} == {SATURDAY.isoformat()}


# -- 3: an off-cluster request is tested, not ignored ---------------------------


def test_a_customer_asking_for_the_other_day_gets_that_day_tested(seeded):
    """A Central customer asking for Friday is not a mistake to correct. It is a request to test
    on Friday's real route, and answered honestly either way."""
    order = _order(seeded, "Mr Rajan")
    assert clusters.is_off_cluster(order, FRIDAY)

    from dispatch_agent.models import AvailabilityOption, TimeWindow
    from datetime import time

    order.availability_options = [
        AvailabilityOption(date=FRIDAY, window=TimeWindow(start=time(10, 0), end=time(14, 0)))
    ]
    seeded.save_job(order)

    result, scope = _search(seeded, "Mr Rajan", "requested")

    assert scope["dates"] == [FRIDAY.isoformat()]
    if result.ok:
        assert {o["date"] for o in result.data["options"]} == {FRIDAY.isoformat()}


# -- 4: only the fallback may look at both --------------------------------------


def test_the_fallback_searches_both_routes(seeded):
    result, scope = _search(seeded, "Mr Rajan", "both")

    assert result.ok, result.summary
    assert set(scope["dates"]) == {FRIDAY.isoformat(), SATURDAY.isoformat()}
    assert result.data["routes_checked"] == 2


def test_a_normal_booking_never_quietly_searches_the_other_day(seeded):
    """The one this exists to prevent: offering a day they never asked about, to save a kilometre
    they never mentioned."""
    for name in ("Mrs Chua", "Mr Rajan"):
        result, scope = _search(seeded, name, "cluster")
        assert len(scope["dates"]) == 1
        assert result.data["routes_checked"] == 1


# -- 5: a hard restriction is a restriction -------------------------------------


def test_only_friday_never_produces_a_saturday_option(seeded):
    """"Friday only" is not a preference to rank below a cheaper Saturday."""
    from datetime import time

    from dispatch_agent.models import AvailabilityOption, TimeWindow
    from dispatch_agent.planning import conversation

    order = _order(seeded, "Mr Rajan")
    order.availability_options = [
        AvailabilityOption(date=FRIDAY, window=TimeWindow(start=time(10, 0), end=time(14, 0)))
    ]
    conversation.mark_timing_fixed(order)
    seeded.save_job(order)

    result, _ = _search(seeded, "Mr Rajan", "both")

    offered = {o["date"] for o in (result.data or {}).get("options", [])}
    assert SATURDAY.isoformat() not in offered
