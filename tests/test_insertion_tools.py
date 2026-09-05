"""The three read-only tools, and the guards that decide when the agent may call them."""
import sys
from datetime import date as Date
from pathlib import Path

import pytest

from dispatch_agent import config
from dispatch_agent.planning import tools

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


def _ctx(repo, intent=None):
    ctx = tools.ToolContext(repo=repo)
    if intent:
        ctx.allowed_tools = tools.INTENT_TOOLS[intent]
    return ctx


def _order(repo, name):
    return next(j for j in repo.all_jobs() if j.customer_name == name)


# -- policy ---------------------------------------------------------------------


def test_policy_comes_back_with_its_rule_ids(seeded):
    result = tools.dispatch("retrieve_policy", {"topic": "alternatives"}, _ctx(seeded))

    assert result.ok
    assert "ALT-3" in result.data["policy_ids"]
    assert "10 km" in result.data["text"]


def test_an_unknown_policy_topic_fails_visibly(seeded):
    """A failed result the model can see and correct, not an empty string it might read as
    "there is no rule about that"."""
    result = tools.dispatch("retrieve_policy", {"topic": "refunds"}, _ctx(seeded))

    assert not result.ok
    assert result.error == "unknown_topic"


# -- routes ---------------------------------------------------------------------


def test_only_published_routes_are_returned(seeded):
    result = tools.dispatch("get_existing_routes", {}, _ctx(seeded))

    assert result.ok
    assert [r["weekday"] for r in result.data["routes"]] == ["Friday", "Saturday"]
    assert result.data["total_stops"] == 16
    assert all(r["version"] >= 1 and r["stop_count"] > 0 for r in result.data["routes"])


def test_no_published_pair_is_a_refusal_not_an_empty_list(seeded):
    """An empty list reads as "there is room everywhere". The agent must be told the cycle does
    not exist, so it escalates instead of inventing a day."""
    for day in (BASE.replace(day=4), BASE.replace(day=5)):
        with __import__("sqlite3").connect(config.settings.db_path) as conn:
            conn.execute("DELETE FROM route_plan_versions WHERE delivery_date = ?", (day.isoformat(),))

    result = tools.dispatch("get_existing_routes", {}, _ctx(seeded))

    assert not result.ok
    assert result.error == "no_coordination_cycle"


# -- insertion ------------------------------------------------------------------


def test_the_tool_returns_the_ranked_options_and_the_counts(seeded):
    order = _order(seeded, "Mr Rajan")

    result = tools.dispatch("find_insertion_options", {"order_id": order.id}, _ctx(seeded))

    assert result.ok
    assert result.data["routes_checked"] == 2
    assert result.data["stops_checked"] == 16
    assert result.data["valid_count"] == len(result.data["options"])
    added = [o["added_distance_km"] for o in result.data["options"]]
    assert added == sorted(added), "the tool returns them ranked; nothing downstream reorders"


def test_the_model_cannot_smuggle_in_its_own_numbers(seeded):
    """The arguments are just an order id. Coordinates and distances are not fields the model
    could fill in, which is stronger than asking it not to."""
    order = _order(seeded, "Mr Rajan")

    result = tools.dispatch(
        "find_insertion_options",
        {"order_id": order.id, "distance_km": 2.0, "lat": 1.3, "lng": 103.8},
        _ctx(seeded),
    )

    assert not result.ok
    assert result.error == "invalid_arguments"


def test_an_unlocatable_order_fails_rather_than_guessing(seeded):
    order = _order(seeded, "Mr Rajan")
    order.address.coordinates = None
    seeded.save_job(order)

    result = tools.dispatch("find_insertion_options", {"order_id": order.id}, _ctx(seeded))

    assert not result.ok
    assert result.error == "not_geocoded"


def test_the_search_result_is_left_where_the_offer_can_read_it(seeded):
    """create_offer reads the options from the context, never from anything the model retypes --
    which is what stops an option being reworded or reordered on the way to the customer."""
    order = _order(seeded, "Mr Rajan")
    ctx = _ctx(seeded)

    tools.dispatch("find_insertion_options", {"order_id": order.id}, ctx)

    assert ctx.scratch["insertion"].options
    assert ctx.scratch["insertion"].options[0].source_plan_version >= 1


# -- intent scoping -------------------------------------------------------------


@pytest.mark.parametrize("intent", ["reject", "provide_availability", "explain"])
def test_the_search_is_reachable_on_the_intents_that_need_it(seeded, intent):
    """Without an INTENT_TOOLS entry dispatch() refuses these as not_allowed_for_intent, and a
    declined offer has nowhere to go but a coordinator."""
    order = _order(seeded, "Mr Rajan")

    result = tools.dispatch("find_insertion_options", {"order_id": order.id}, _ctx(seeded, intent))

    assert result.error != "not_allowed_for_intent"


@pytest.mark.parametrize("action", ["retrieve_policy", "get_existing_routes", "find_insertion_options"])
def test_none_of_them_are_reachable_while_accepting(seeded, action):
    """The customer said yes to a specific slot. Re-searching at that point is how an agent books
    something other than what was accepted."""
    result = tools.dispatch(action, {"order_id": "x", "topic": "alternatives"}, _ctx(seeded, "accept"))

    assert result.error == "not_allowed_for_intent"


def test_a_question_cannot_change_the_booking(seeded):
    """"Why are you suggesting this?" must be read-only. These three tools write nothing, which is
    what makes them safe to expose on an explanation."""
    order = _order(seeded, "Mr Rajan")
    before = order.model_dump()

    for action, args in (
        ("retrieve_policy", {"topic": "alternatives"}),
        ("get_existing_routes", {}),
        ("find_insertion_options", {"order_id": order.id}),
    ):
        tools.dispatch(action, args, _ctx(seeded, "explain"))

    after = seeded.get_job(order.id).model_dump()
    assert {k: v for k, v in after.items() if k != "updated_at"} == {
        k: v for k, v in before.items() if k != "updated_at"
    }
    assert seeded.offers_for_order(order.id) == []
    assert seeded.messages(order.id) == []
