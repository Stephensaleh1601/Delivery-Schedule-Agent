"""Two kinds of offer, two policies, and the reason they must not blend.

The normal offer is one proven option on the customer's own cluster day. The fallback, after that
is declined, is exactly three or a coordinator. Sharing a slot cap between them would force the
normal offer to carry three choices, which is the opposite of what it is for.
"""
import sys
from datetime import date as Date
from pathlib import Path

import pytest

from dispatch_agent import config
from dispatch_agent.models import OfferPurpose, PlanningStatus
from dispatch_agent.planning import insertion, offer_service, plan_service, tools
from dispatch_agent.planning.clock import PlanningClock

BASE = Date(2026, 9, 2)

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


def _searched(repo, name, exclude=None):
    """A context carrying a real search result, as the agent would have after calling the tool."""
    order = _order(repo, name)
    ctx = tools.ToolContext(repo=repo)
    ctx.scratch["insertion"] = insertion.search(repo, order, exclude=exclude or set())
    return order, ctx


# -- the normal offer -----------------------------------------------------------


def test_the_normal_offer_carries_exactly_one_proven_option(seeded):
    order, ctx = _searched(seeded, "Mrs Chua")

    result = tools.dispatch("create_normal_offer", {"order_id": order.id}, ctx)

    assert result.ok, result.summary
    assert len(result.data["slots"]) == 1
    slot = result.data["slots"][0]
    assert slot["added_distance_km"] is not None and slot["anchor_stop_number"]
    assert seeded.get_job(order.id).planning_status is PlanningStatus.OFFERED


def test_an_offer_never_names_another_customer(seeded):
    """An offer and its evidence are served to the customer's own page. A stop number says
    everything the panel needs; a name tells one customer who the others are."""
    import json

    order, ctx = _searched(seeded, "Mrs Chua")
    result = tools.dispatch("create_normal_offer", {"order_id": order.id}, ctx)

    others = {j.customer_name for j in seeded.all_jobs() if j.id != order.id}
    dumped = json.dumps(result.data)
    for name in others:
        assert name not in dumped, name

    stored = json.dumps(
        [s.model_dump(mode="json") for s in seeded.offers_for_order(order.id)[0].options]
    )
    for name in others:
        assert name not in stored, name


def test_the_normal_offer_did_not_raise_the_shared_slot_cap(seeded):
    """The older evaluation-based path still offers two. Raising one global constant to three
    would have changed it, which is why the caps are per purpose."""
    assert offer_service.MAX_SLOTS_PER_OFFER == 2
    assert offer_service.SLOTS_BY_PURPOSE[OfferPurpose.BOOKING] == 1
    assert offer_service.SLOTS_BY_PURPOSE[OfferPurpose.ALTERNATIVE] == 3


# -- the fallback ---------------------------------------------------------------


def test_the_fallback_offers_exactly_the_three_the_tool_returned(seeded):
    """In the tool's order. Nothing between the search and the customer re-sorts them."""
    friday, saturday = PlanningClock.horizon_dates()
    order, ctx = _searched(seeded, "Mr Rajan", exclude={(saturday, "morning")})
    expected = [(o.date, o.slot.name) for o in ctx.scratch["insertion"].options]
    assert len(expected) == 3

    result = tools.dispatch("create_alternative_offer", {"order_id": order.id}, ctx)

    assert result.ok, result.summary
    offered = [(Date.fromisoformat(s["date"]), s["window"]["start"]) for s in result.data["slots"]]
    assert len(offered) == 3
    assert [d for d, _ in offered] == [d for d, _ in expected]
    assert result.data["purpose"] == "alternative"


def test_fewer_than_three_escalates_rather_than_offering_two(seeded):
    """Two safe choices is not a smaller version of the right answer. Policy says a customer who
    cannot be given three is a customer a coordinator should call."""
    order, ctx = _searched(seeded, "Mr Rajan")
    ctx.scratch["insertion"].options = ctx.scratch["insertion"].options[:2]

    result = tools.dispatch("create_alternative_offer", {"order_id": order.id}, ctx)

    assert not result.ok
    assert result.error == "too_few_alternatives"
    assert seeded.offers_for_order(order.id) == []


def test_offering_without_searching_first_is_refused(seeded):
    """The options come from what the search returned, never from the model's arguments -- so an
    offer with nothing verified behind it has no list to draw on at all."""
    order = _order(seeded, "Mr Rajan")

    result = tools.dispatch("create_normal_offer", {"order_id": order.id}, tools.ToolContext(repo=seeded))

    assert not result.ok
    assert result.error == "nothing_searched"


def test_the_two_budgets_are_counted_separately(seeded):
    """Declining the normal offer must not consume the fallback round -- that would leave the
    difficult customer with nothing, which is the whole scenario."""
    friday, saturday = PlanningClock.horizon_dates()
    order, ctx = _searched(seeded, "Mr Rajan")
    tools.dispatch("create_normal_offer", {"order_id": order.id}, ctx)

    order, ctx2 = _searched(seeded, "Mr Rajan", exclude={(saturday, "morning")})
    result = tools.dispatch("create_alternative_offer", {"order_id": order.id}, ctx2)

    assert result.ok, result.summary
    purposes = [o.purpose for o in seeded.offers_for_order(order.id)]
    assert OfferPurpose.BOOKING in purposes and OfferPurpose.ALTERNATIVE in purposes


def test_only_one_offer_per_run(seeded):
    """A second set of choices in one thread is a customer answering whichever arrived last."""
    order, ctx = _searched(seeded, "Mrs Chua")

    first = tools.dispatch("create_normal_offer", {"order_id": order.id}, ctx)
    second = tools.dispatch("create_normal_offer", {"order_id": order.id}, ctx)

    assert first.ok
    assert not second.ok and second.error == "already_done"


# -- accepting ------------------------------------------------------------------


def test_accepting_a_slot_whose_route_has_moved_asks_the_customer_to_pick_again(seeded):
    """An insertion was tested against a specific published route. If that day is republished
    while the customer is deciding, the position we measured is not the position any more."""
    order, ctx = _searched(seeded, "Mrs Chua")
    tools.dispatch("create_normal_offer", {"order_id": order.id}, ctx)
    offer = seeded.offers_for_order(order.id)[0]
    slot = offer.options[0]

    # Somebody else's booking lands on that day first. It has to genuinely change the route --
    # a replan that produces an identical plan deliberately reuses the version rather than
    # inflating the counter, so re-solving alone would not make this offer stale.
    rival = _order(seeded, "Mr Rajan")
    rival.delivery_date = slot.date
    rival.availability = [slot.window]
    rival.locked_window = slot.window
    rival.set_planning_status(PlanningStatus.CONFIRMED)
    seeded.save_job(rival)
    plan_service.replan_day(seeded, slot.date, reason="another booking landed first")
    assert seeded.active_plan(slot.date).version > slot.evidence.source_plan_version

    with pytest.raises(offer_service.OfferError) as exc:
        offer_service.accept_offer(seeded, offer.id, slot.id)

    assert exc.value.kind == "route_moved_on"
    # Refused before the response was claimed, so the offer is still open to answer.
    assert seeded.get_offer(offer.id).accepted_slot_id is None


def test_accepting_an_unmoved_route_confirms_and_republishes(seeded):
    order, ctx = _searched(seeded, "Mrs Chua")
    tools.dispatch("create_normal_offer", {"order_id": order.id}, ctx)
    offer = seeded.offers_for_order(order.id)[0]
    slot = offer.options[0]
    before = seeded.active_plan(slot.date)

    outcome = offer_service.accept_offer(seeded, offer.id, slot.id)

    after = seeded.active_plan(slot.date)
    assert outcome.job.planning_status is PlanningStatus.CONFIRMED
    assert outcome.job.locked_window == slot.window
    assert after.version == before.version + 1
    assert len(after.sequence.stops) == len(before.sequence.stops) + 1
    assert outcome.job.id in {s.job_id for s in after.sequence.stops}


def test_the_existing_stop_order_is_preserved_by_an_acceptance(seeded):
    """Insertion, not re-optimisation. Everyone already on the route keeps their place relative
    to each other -- only the new customer appears."""
    order, ctx = _searched(seeded, "Mrs Chua")
    tools.dispatch("create_normal_offer", {"order_id": order.id}, ctx)
    offer = seeded.offers_for_order(order.id)[0]
    slot = offer.options[0]
    before = [s.job_id for s in seeded.active_plan(slot.date).sequence.stops]

    offer_service.accept_offer(seeded, offer.id, slot.id)

    after = [s.job_id for s in seeded.active_plan(slot.date).sequence.stops]
    assert [j for j in after if j != order.id] == before
