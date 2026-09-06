"""What the agent may do right now, and why the model is shown that rather than the registry.

`dispatch()` has always refused an out-of-scope action. The gap this closes is what the model was
*offered*: the whole tool list, with `lock_appointment` sitting in it before anyone had accepted
anything. Being shown an action you are not allowed to take is an invitation to take it, and a
live model accepted that invitation more than once -- which is why the browser regressions exist.

The gate narrows, never widens. Intent scoping still applies on top and dispatch re-checks
independently, so a bug here can make the agent do less than it should; it cannot make it do
something unsafe.
"""
import pytest

from dispatch_agent.planning import tools


class _Fake:
    """Stands in for an insertion search result -- only `.options` is read."""

    def __init__(self, options):
        self.options = options


def _ctx(intent=None, **kwargs):
    ctx = tools.ToolContext(repo=None)
    if intent:
        ctx.allowed_tools = tools.INTENT_TOOLS[intent]
    for key, value in kwargs.items():
        setattr(ctx, key, value)
    return ctx


def test_finishing_is_always_available():
    """An agent with no legal move must still be able to stop."""
    assert "finish" in tools.legal_actions({}, _ctx("unclear"))


def test_an_offer_is_not_offered_before_anything_has_been_searched():
    """Offering before searching is offering something nobody checked."""
    ctx = _ctx("reject")

    assert "create_alternative_offer" not in tools.legal_actions({}, ctx)

    ctx.scratch["insertion"] = _Fake(["one option"])
    assert "create_alternative_offer" in tools.legal_actions({}, ctx)


def test_a_search_that_found_nothing_does_not_unlock_an_offer():
    """An empty result is a search that happened, not a list to offer from."""
    ctx = _ctx("reject", scratch={"insertion": _Fake([])})

    assert "create_alternative_offer" not in tools.legal_actions({}, ctx)


def test_nothing_can_be_locked_until_the_customer_accepted_a_slot():
    """The tool refuses this too. Hiding it as well means the model is never shown a booking it
    could make by mistake."""
    ctx = _ctx("accept")
    assert "lock_appointment" not in tools.legal_actions({}, ctx)

    ctx.accepted = ("offer-1", "slot-1")
    assert "lock_appointment" in tools.legal_actions({}, ctx)


def test_a_message_needs_wording_a_tool_prepared():
    """Otherwise a run ends with an empty bubble in the customer's thread."""
    ctx = _ctx("reject")
    assert "send_message" not in tools.legal_actions({}, ctx)

    ctx.scratch["offer_message"] = "We can deliver Friday morning."
    assert "send_message" in tools.legal_actions({}, ctx)


def test_a_policy_search_must_be_answered_before_anything_can_be_sent():
    """Finding the rule is not the same as answering the customer.

    The live app found WINDOW-1/2/3, then repeatedly tried ``send_message`` with no prepared
    wording until the step limit.  Once a policy search succeeds, preparing the answer is the
    only legal next action.
    """
    ctx = _ctx("policy_question")
    ctx.succeeded.add("search_delivery_policy")
    ctx.scratch["policy_hits"] = [{"id": "WINDOW-1", "text": "Morning is 10am-2pm."}]

    assert tools.legal_actions({}, ctx) == ["answer_from_policy"]


def test_after_the_customer_has_been_written_to_the_run_is_over():
    """Anything further in the same run is an action they will never see a message about."""
    ctx = _ctx("reject", scratch={"offer_message": "..."})
    ctx.succeeded.add("send_message")

    assert tools.legal_actions({}, ctx) == ["finish"]


def test_a_once_per_run_action_disappears_after_it_succeeds():
    ctx = _ctx("reject", scratch={"insertion": _Fake(["x"])})
    assert "create_alternative_offer" in tools.legal_actions({}, ctx)

    ctx.succeeded.add("create_alternative_offer")
    assert "create_alternative_offer" not in tools.legal_actions({}, ctx)


def test_the_gate_never_widens_the_intent_scope():
    """The property that makes this safe to get wrong: every legal action is one the intent
    already allowed, so the gate can only ever subtract."""
    for intent, scope in tools.INTENT_TOOLS.items():
        ctx = _ctx(intent, scratch={"insertion": _Fake(["x"]), "offer_message": "..."})
        ctx.accepted = ("o", "s")

        legal = set(tools.legal_actions({}, ctx))

        assert legal <= set(scope) | {"finish"}, intent


@pytest.mark.parametrize("intent", ["explain", "general_support", "unclear"])
def test_a_read_only_intent_is_never_given_a_way_to_book(intent):
    """"Why are you suggesting this?" must not be able to end in a booking, whatever the model
    decides it wants to do."""
    ctx = _ctx(intent, scratch={"insertion": _Fake(["x"]), "offer_message": "..."})
    ctx.accepted = ("o", "s")

    legal = set(tools.legal_actions({}, ctx))

    assert not legal & {
        "lock_appointment", "create_offer", "create_normal_offer",
        "create_alternative_offer", "record_rejection", "record_availability",
    }


def test_a_run_may_not_end_with_an_unsent_reply():
    """The worst failure a conversation can have: the agent writes something for the customer,
    logs that it wrote it, and stops.

    It happened. "why this timing?" before any offer existed was read as unclear, the model asked
    a clarification, saw wording was ready, asked again, and finished -- leaving a person watching
    a thread that had simply stopped answering.
    """
    ctx = _ctx("unclear", scratch={"customer_message": "Which day suits you?"})

    legal = tools.legal_actions({}, ctx)

    assert "send_message" in legal
    assert "finish" not in legal, "ending is not allowed while a reply is written but unsent"


def test_finishing_is_allowed_again_once_the_reply_is_sent():
    ctx = _ctx("unclear", scratch={"customer_message": "Which day suits you?"})
    ctx.succeeded.add("send_message")

    assert tools.legal_actions({}, ctx) == ["finish"]


def test_ending_is_not_withheld_where_there_is_no_way_to_send():
    """The guard must never trap the loop. An intent with no `send_message` in scope can still
    finish, whatever is sitting in the scratch space."""
    ctx = tools.ToolContext(repo=None)
    ctx.allowed_tools = frozenset({"finish"})
    ctx.scratch["customer_message"] = "Which day suits you?"

    assert tools.legal_actions({}, ctx) == ["finish"]


def test_asking_the_same_question_twice_in_one_run_is_refused():
    """Same duplicate-action failure as sending the same offer three times."""
    assert "ask_clarification" in tools.ONCE_PER_RUN

    ctx = _ctx("unclear")
    assert "ask_clarification" in tools.legal_actions({}, ctx)

    ctx.succeeded.add("ask_clarification")
    assert "ask_clarification" not in tools.legal_actions({}, ctx)
