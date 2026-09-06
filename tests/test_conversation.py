"""A customer typing, in their own words, and what the system does about it.

Every test here is offline. The deterministic reader in `planning/language.py` does the
understanding, which is the same reader the live demo falls back to -- so these assertions describe
behaviour that survives a provider outage rather than behaviour that depends on a model being in a
good mood. The one test that exercises the model path injects a fake client.

The properties worth stating up front, because most of the file is defending them:

- A suggestion we make is never recorded as the customer's availability until they accept it.
- One customer message produces exactly one agent run and at most one offer round.
- An acceptance we cannot pin to a specific slot asks rather than books.
"""
from datetime import date, time, timedelta

import pytest

from dispatch_agent import config
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.models import (
    Address,
    AvailabilityOption,
    JobRecord,
    JobType,
    MessageDirection,
    OfferStatus,
    PlanningStatus,
    TimeWindow,
)
from dispatch_agent.planning import conversation, language, negotiation, offer_service
from dispatch_agent.planning.candidate_service import CandidateService
from dispatch_agent.planning.clock import PlanningClock

# A Wednesday, so the coordination cycle is that same week's Friday and Saturday -- both clear
# the two-day notice, which is what the demo script relies on.
BASE = date(2026, 9, 2)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())


def _w(h1, m1, h2, m2):
    return TimeWindow(start=time(h1, m1), end=time(h2, m2))


def _order(repo, name="Mrs Lee", postal_code="469123", options=(), duration=45):
    job = JobRecord(
        customer_name=name,
        phone="91230000",
        address=Address(
            raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)
        ),
        job_type=JobType.SOFA,
        availability_options=list(options),
        planning_status=PlanningStatus.PENDING_AVAILABILITY,
        raw_message="hi, i need a delivery",
        duration_minutes=duration,
    )
    repo.save_job(job)
    return job


def _confirmed(repo, name, postal_code, day, window=(9, 0, 18, 0), duration=45):
    job = JobRecord(
        customer_name=name,
        address=Address(
            raw_text=name, postal_code=postal_code, coordinates=postal_code_to_coords(postal_code)
        ),
        job_type=JobType.SOFA,
        availability=[_w(*window)],
        locked_window=_w(*window),
        delivery_date=day,
        planning_status=PlanningStatus.CONFIRMED,
        status="approved",
        raw_message="seed",
        duration_minutes=duration,
    )
    repo.save_job(job)
    return job


# -- reading the message -------------------------------------------------------

SATURDAY = date(2026, 9, 5)
FRIDAY = date(2026, 9, 4)
TUESDAY = date(2026, 9, 8)  # not a delivery day; only for the date-parser tests


def test_saturday_morning():
    said = language.interpret("I'm free Saturday morning.")

    assert said.intent == "provide_availability"
    # The published morning window, not a generic one. DAY_PARTS reads these off slots.py so a
    # customer who says "morning" asks for the window we actually run.
    assert [(w.date, w.window.start, w.window.end) for w in said.windows] == [
        (SATURDAY, time(10, 0), time(14, 0))
    ]


def test_tuesday_after_one():
    """"after 1" is 1pm. Nobody arranging a furniture delivery means one in the morning, and
    reading it that way would produce a window outside the working day and an odd refusal."""
    said = language.interpret("Any time after 1 on Friday")

    # Open-ended "after 1" runs to the arrival cutoff, which is the last time we will promise
    # anyone -- not to the depot's own deadline.
    assert [(w.date, w.window.start, w.window.end) for w in said.windows] == [
        (FRIDAY, time(13, 0), config.settings.arrival_cutoff)
    ]


def test_two_alternatives_with_a_stated_preference():
    """Order of mention is the default ranking, and an explicit preference overrides it. The
    customer said Friday is better, so Friday is rank 1 even though Saturday was said first."""
    said = language.interpret(
        "I can do Saturday afternoon or Friday morning, but Friday is better."
    )

    ranked = sorted(said.windows, key=lambda w: w.preference_rank)
    assert [w.date for w in ranked] == [FRIDAY, SATURDAY]
    assert ranked[0].window.start == time(10, 0)


def test_this_is_my_only_available_time():
    said = language.interpret("Saturday morning, that's the only time I can do")

    assert said.is_fixed
    assert said.windows[0].date == SATURDAY


def test_a_preference_is_not_a_fixed_timing():
    """"I'd prefer Saturday" leaves the conversation open; "Saturday is the only time" closes it.
    Confusing the two either nags a customer who has decided, or gives up on one who has not."""
    assert not language.interpret("I'd prefer Saturday morning if possible").is_fixed


def test_take_the_first_one():
    said = language.interpret("Take the first one", has_open_offer=True)

    assert said.intent == "accept"
    assert said.accepted_ordinal == 1


def test_that_doesnt_work_can_you_do_after_one():
    """The counter-proposal is availability; the thing being refused is not. Reading "not Saturday
    morning" as an offer would have us propose back the very time they just declined."""
    said = language.interpret(
        "Not Saturday morning, can you do after 1?", has_open_offer=True, context_date=SATURDAY
    )

    assert said.intent == "reject"
    assert not said.rejects_whole_day
    assert [(w.date, w.window.start) for w in said.windows] == [(SATURDAY, time(13, 0))]


def test_rejecting_one_window_versus_the_whole_day():
    one = language.interpret("11am doesn't work.", has_open_offer=True, context_date=SATURDAY)
    all_of_them = language.interpret("None of these work.", has_open_offer=True)

    assert one.intent == all_of_them.intent == "reject"
    assert not one.rejects_whole_day
    assert all_of_them.rejects_whole_day


def test_why_this_timing():
    assert language.interpret("Why this timing?", has_open_offer=True).intent == "explain"


def test_cant_you_come_on_saturday_is_a_question_not_a_refusal():
    """It contains "can't", so a naive reader files it as a rejection and re-solves the day. It is
    a customer asking us to explain ourselves, and the answer is an explanation."""
    said = language.interpret("Can't you come on Saturday?", has_open_offer=True)

    assert said.intent == "explain"


def test_an_unrelated_message_is_unclear_rather_than_guessed():
    """Noise gets a question, never a date."""
    for noise in ("lol", "ok thanks bye", "hello?"):
        said = language.interpret(noise, has_open_offer=False)
        assert said.intent == "unclear", noise
        assert not said.windows


@pytest.mark.parametrize(
    "message,topic",
    [
        ("Can I change my delivery address?", "address"),
        ("my address is wrong, it's the wrong block", "address"),
        ("Actually I want to cancel", "cancel"),
        ("how much does it cost?", "price"),
        ("can I speak to someone?", "contact"),
    ],
)
def test_a_question_that_is_not_about_timing_is_support_not_scheduling(message, topic):
    """"Can I change my delivery address?" was answered with "which of those times would you like?"

    These messages are full of words that look like scheduling -- "change", a question mark, a
    refusal -- so a reader tuned for times mistakes them for one. They are perfectly clear; we
    simply cannot answer them by moving a van, and pretending otherwise is what makes someone give
    up on an automated agent for good.
    """
    said = language.interpret(message, has_open_offer=True)

    assert said.intent == "general_support", message
    assert said.support_topic == topic
    assert not said.windows, "a support question must never state availability"


def test_a_support_question_is_not_read_as_an_answer_to_the_offer():
    """The specific misreading: it must be neither an acceptance nor a rejection."""
    for message in ("Can I change my delivery address?", "how much will this cost?"):
        said = language.interpret(message, has_open_offer=True)
        assert said.intent not in ("accept", "reject"), message


# -- relative dates in Singapore ------------------------------------------------


def test_relative_dates_resolve_against_the_planning_clock():
    """Every one of these is measured from BASE (Wednesday 2 September), not from the machine's
    clock. A server in another timezone must not shift what "tomorrow" means."""
    assert language.parse_date("tomorrow") == BASE + timedelta(days=1)
    assert language.parse_date("day after tomorrow") == BASE + timedelta(days=2)
    assert language.parse_date("this Saturday") == SATURDAY
    assert language.parse_date("next Tuesday") == TUESDAY + timedelta(days=7)
    assert language.parse_date("Tuesday") == TUESDAY


def test_the_same_weekday_means_next_week_not_today():
    """"Saturday" said on a Saturday is the coming one. Resolving it to today would offer a
    delivery inside the notice period and then refuse it, for a reason nobody can see."""
    assert language.resolve_weekday("saturday", SATURDAY) == SATURDAY + timedelta(days=7)


def test_singapore_is_the_timezone_even_when_the_server_is_not(monkeypatch):
    """With DEMO_BASE_DATE unset the real clock is used -- and it must be Singapore's."""
    monkeypatch.setattr(config.settings, "demo_base_date", "")
    from datetime import datetime

    assert language.today() == datetime.now(language.SINGAPORE).date()


def test_a_date_outside_the_horizon_is_explained_not_just_refused():
    too_soon = BASE + timedelta(days=1)

    complaint = conversation.horizon_complaint([too_soon])

    assert complaint is not None
    first, last = PlanningClock.horizon()
    assert offer_service.format_date(first) in complaint
    assert offer_service.format_date(last) in complaint
    assert "notice" in complaint, "a refusal with no reason is what makes an agent infuriating"


def test_a_date_inside_the_horizon_produces_no_complaint():
    assert conversation.horizon_complaint([SATURDAY, FRIDAY]) is None


# -- one timing is enough -------------------------------------------------------


def test_a_single_stated_timing_is_enough_to_get_an_offer(temp_db):
    """The product direction: no form, no "give me two or three times". One timing books."""
    order = _order(temp_db)
    said = language.interpret("I'm free Saturday morning.")

    order.availability_options = conversation.merge_availability(order, said.windows)
    temp_db.save_job(order)

    evaluations = CandidateService(repo=temp_db).evaluate_all(order)
    offer = offer_service.create_offer(temp_db, order, evaluations)

    assert len(order.availability_options) == 1
    assert offer.options, "one stated timing must be bookable on its own"
    assert offer.options[0].date == SATURDAY


def test_restating_a_day_corrects_it_rather_than_adding_a_second_window(temp_db):
    """"Actually, make it Saturday afternoon" is a correction. Accumulating both would have us
    offer a morning slot the customer has just moved away from."""
    order = _order(temp_db)
    order.availability_options = conversation.merge_availability(
        order, language.interpret("Saturday morning").windows
    )
    order.availability_options = conversation.merge_availability(
        order, language.interpret("actually Saturday afternoon").windows
    )

    saturdays = [o for o in order.availability_options if o.date == SATURDAY]
    assert len(saturdays) == 1
    assert saturdays[0].window.start == time(14, 0)


# -- suggestions are not availability -------------------------------------------


def test_a_suggestion_is_never_recorded_as_the_customers_availability(temp_db):
    """The property the whole negotiation rests on. We may ASK about Friday; until they say yes,
    the order must not claim they are free on Friday."""
    order = _order(temp_db, options=[AvailabilityOption(date=SATURDAY, window=_w(9, 0, 13, 0))])
    _confirmed(temp_db, "Anchor", "469123", FRIDAY)

    suggestions = negotiation.route_aware_windows(order, CandidateService(repo=temp_db))

    assert suggestions, "there should be somewhere else to propose"
    stored = temp_db.get_job(order.id)
    assert [o.date for o in stored.availability_options] == [SATURDAY], (
        "suggesting a day must not write it into the customer's availability"
    )


def test_a_suggestion_becomes_availability_only_when_accepted(temp_db):
    """The other half. Acceptance is the moment a question becomes a commitment, and the order's
    record should then show a window the customer agreed to."""
    order = _order(temp_db, options=[AvailabilityOption(date=SATURDAY, window=_w(9, 0, 13, 0))])
    _confirmed(temp_db, "Anchor", "469123", FRIDAY)

    suggestions = negotiation.route_aware_windows(order, CandidateService(repo=temp_db))
    chosen = suggestions[0]
    offer = offer_service.create_offer(temp_db, order, [chosen.evaluation])
    offer_service.accept_offer(temp_db, offer.id, offer.options[0].id)

    stored = temp_db.get_job(order.id)
    assert chosen.date in [o.date for o in stored.availability_options]
    assert stored.locked_window == offer.options[0].window


def test_suggestions_never_name_another_customer(temp_db):
    """A suggestion's reason may say where the van will be. It may not say who else is on it."""
    order = _order(temp_db, options=[AvailabilityOption(date=SATURDAY, window=_w(9, 0, 13, 0))])
    _confirmed(temp_db, "Mrs Devi", "469123", FRIDAY)
    _confirmed(temp_db, "Mr Ong", "529536", FRIDAY)

    for suggestion in negotiation.route_aware_windows(order, CandidateService(repo=temp_db)):
        reason = suggestion.reason or ""
        assert "Mrs Devi" not in reason and "Mr Ong" not in reason


def test_suggestions_stay_inside_the_horizon_and_working_hours(temp_db):
    order = _order(temp_db, options=[AvailabilityOption(date=SATURDAY, window=_w(9, 0, 13, 0))])
    _confirmed(temp_db, "Anchor", "469123", FRIDAY)

    for suggestion in negotiation.route_aware_windows(order, CandidateService(repo=temp_db)):
        assert PlanningClock.is_within_horizon(suggestion.date)
        assert suggestion.window.start >= config.settings.work_day_start
        assert suggestion.window.end <= config.settings.arrival_cutoff


def test_at_most_two_alternatives_are_offered(temp_db):
    """Four horizon dates, all feasible -- and a customer must still be given a choice, not a
    timetable."""
    order = _order(temp_db, options=[AvailabilityOption(date=SATURDAY, window=_w(9, 0, 13, 0))])

    assert len(negotiation.route_aware_windows(order, CandidateService(repo=temp_db))) <= 2


# -- when to argue, and when not to ---------------------------------------------


def _evaluation(temp_db, order, day, window=(9, 0, 18, 0)):
    option = AvailabilityOption(date=day, window=_w(*window))
    return CandidateService(repo=temp_db).evaluate(order, option)


def test_a_workable_request_is_honoured_rather_than_haggled_over(temp_db):
    """The default. A coordinator who pushes back on every booking to save a minute is one nobody
    wants to deal with."""
    order = _order(temp_db)
    _confirmed(temp_db, "Anchor", "469123", SATURDAY)
    requested = _evaluation(temp_db, order, SATURDAY)

    decision = negotiation.should_counteroffer(requested, alternatives=[])

    assert not decision.should_ask
    assert decision.kind == "honour_request"


def test_a_fixed_timing_stops_us_asking_again(temp_db):
    """Even when a genuinely better day exists. They have told us; asking again is nagging."""
    order = _order(temp_db)
    _confirmed(temp_db, "Anchor", "469123", SATURDAY)
    requested = _evaluation(temp_db, order, SATURDAY)
    alternatives = negotiation.route_aware_windows(order, CandidateService(repo=temp_db))

    decision = negotiation.should_counteroffer(requested, alternatives, customer_says_fixed=True)

    assert not decision.should_ask
    assert decision.kind == "customer_fixed"


def test_an_infeasible_request_always_produces_a_counteroffer(temp_db):
    order = _order(temp_db)

    decision = negotiation.should_counteroffer(None, alternatives=[])

    assert decision.should_ask and decision.kind == "infeasible"


def test_a_small_saving_is_not_worth_asking_about(temp_db, monkeypatch):
    """Explicit and configurable, per the policy. Below the threshold the customer's own choice is
    worth more than the minutes."""
    monkeypatch.setattr(config.settings, "counteroffer_saving_minutes", 15)
    order = _order(temp_db)
    _confirmed(temp_db, "Anchor", "469123", SATURDAY)
    requested = _evaluation(temp_db, order, SATURDAY)

    cheaper = _evaluation(temp_db, order, SATURDAY).model_copy(
        update={"incremental_drive_minutes": requested.incremental_drive_minutes - 5}
    )
    alternative = negotiation.Suggestion(
        date=FRIDAY, window=_w(9, 0, 11, 0), evaluation=cheaper,
        option=AvailabilityOption(date=FRIDAY, window=_w(9, 0, 11, 0)),
    )

    assert not negotiation.should_counteroffer(requested, [alternative]).should_ask


def test_a_material_saving_is_worth_asking_about(temp_db, monkeypatch):
    monkeypatch.setattr(config.settings, "counteroffer_saving_minutes", 15)
    order = _order(temp_db)
    _confirmed(temp_db, "Anchor", "469123", SATURDAY)
    requested = _evaluation(temp_db, order, SATURDAY)

    cheaper = requested.model_copy(
        update={"incremental_drive_minutes": requested.incremental_drive_minutes - 40}
    )
    alternative = negotiation.Suggestion(
        date=FRIDAY, window=_w(9, 0, 11, 0), evaluation=cheaper,
        option=AvailabilityOption(date=FRIDAY, window=_w(9, 0, 11, 0)),
    )

    decision = negotiation.should_counteroffer(requested, [alternative])
    assert decision.should_ask and decision.kind == "material_saving"


def test_the_threshold_is_configurable(temp_db, monkeypatch):
    order = _order(temp_db)
    _confirmed(temp_db, "Anchor", "469123", SATURDAY)
    requested = _evaluation(temp_db, order, SATURDAY)
    cheaper = requested.model_copy(
        update={"incremental_drive_minutes": requested.incremental_drive_minutes - 10}
    )
    alternative = negotiation.Suggestion(
        date=FRIDAY, window=_w(9, 0, 11, 0), evaluation=cheaper,
        option=AvailabilityOption(date=FRIDAY, window=_w(9, 0, 11, 0)),
    )

    monkeypatch.setattr(config.settings, "counteroffer_saving_minutes", 20)
    assert not negotiation.should_counteroffer(requested, [alternative]).should_ask

    monkeypatch.setattr(config.settings, "counteroffer_saving_minutes", 5)
    assert negotiation.should_counteroffer(requested, [alternative]).should_ask


def test_overtime_justifies_a_counteroffer(temp_db):
    order = _order(temp_db)
    _confirmed(temp_db, "Anchor", "469123", SATURDAY)
    requested = _evaluation(temp_db, order, SATURDAY).model_copy(
        update={"overtime_penalty_minutes": 25}
    )

    decision = negotiation.should_counteroffer(requested, [])
    assert decision.should_ask and decision.kind == "overtime"


def test_opening_an_empty_day_justifies_one_only_if_there_is_somewhere_else(temp_db):
    """Complaining about opening a day when every alternative also opens one is noise."""
    order = _order(temp_db)
    requested = _evaluation(temp_db, order, SATURDAY)
    assert requested.opens_empty_day, "no work is seeded, so this day is empty"

    assert not negotiation.should_counteroffer(requested, []).should_ask

    _confirmed(temp_db, "Anchor", "469123", FRIDAY)
    elsewhere = negotiation.route_aware_windows(order, CandidateService(repo=temp_db))
    running = [s for s in elsewhere if not s.evaluation.opens_empty_day]
    assert running, "Friday now has work on it"
    assert negotiation.should_counteroffer(requested, running).kind == "opens_new_day"


# -- resolving what they accepted -----------------------------------------------


def _two_slot_offer(temp_db):
    order = _order(
        temp_db,
        options=[
            AvailabilityOption(date=SATURDAY, window=_w(9, 0, 18, 0), preference_rank=1),
            AvailabilityOption(date=FRIDAY, window=_w(9, 0, 18, 0), preference_rank=2),
        ],
    )
    evaluations = CandidateService(repo=temp_db).evaluate_all(order)
    return order, offer_service.create_offer(temp_db, order, evaluations)


def test_an_ordinal_picks_the_slot_in_the_order_it_was_offered(temp_db):
    _, offer = _two_slot_offer(temp_db)

    chosen = conversation.resolve_accepted_slot(
        offer, language.interpret("take the first one", has_open_offer=True)
    )

    assert chosen.id == offer.options[0].id


def test_a_day_name_picks_the_slot_on_that_day(temp_db):
    _, offer = _two_slot_offer(temp_db)
    wanted = offer.options[1]

    chosen = conversation.resolve_accepted_slot(
        offer,
        language.interpret(f"{wanted.date:%A} works", has_open_offer=True),
    )

    assert chosen.id == wanted.id


def test_a_bare_okay_against_two_options_asks_rather_than_guessing(temp_db):
    """The worst failure this system could have is booking a slot the customer did not choose. A
    van at the wrong door, and they cannot see it coming."""
    _, offer = _two_slot_offer(temp_db)

    with pytest.raises(conversation.AmbiguousAcceptance) as exc:
        conversation.resolve_accepted_slot(
            offer, language.interpret("okay", has_open_offer=True)
        )

    assert exc.value.options, "the question must list what they are choosing between"


def test_a_bare_okay_against_one_option_is_unambiguous(temp_db):
    order = _order(temp_db, options=[AvailabilityOption(date=SATURDAY, window=_w(9, 0, 13, 0))])
    evaluations = CandidateService(repo=temp_db).evaluate_all(order)
    offer = offer_service.create_offer(temp_db, order, evaluations)
    assert len(offer.options) == 1

    chosen = conversation.resolve_accepted_slot(
        offer, language.interpret("okay", has_open_offer=True)
    )

    assert chosen.id == offer.options[0].id


def test_a_time_outside_every_offered_window_is_not_quietly_rounded(temp_db):
    """"Confirm 4pm" against 9-11 and 1-3 is not an acceptance of the nearest one."""
    _, offer = _two_slot_offer(temp_db)
    narrow = [s for s in offer.options if not (s.window.start <= time(16, 0) < s.window.end)]
    assert len(narrow) == len(offer.options), "fixture assumption: neither slot contains 4pm"

    with pytest.raises(conversation.AmbiguousAcceptance):
        conversation.resolve_accepted_slot(
            offer, language.interpret("confirm 4pm", has_open_offer=True)
        )


def test_an_ordinal_beyond_the_offer_is_refused(temp_db):
    _, offer = _two_slot_offer(temp_db)

    with pytest.raises(conversation.AmbiguousAcceptance):
        conversation.resolve_accepted_slot(
            offer, language.Interpretation(intent="accept", accepted_ordinal=5)
        )


# -- the model path -------------------------------------------------------------


def test_the_model_decides_intent_but_never_the_date(temp_db):
    """The safety property of the whole architecture, tested with a model that lies about dates.

    The reader hands back an intent and the customer's own phrases; the date comes from parsing
    those phrases here. There is no field a hallucinated date could arrive in.
    """
    from dispatch_agent.agents.understanding import MessageReader
    from tests.conftest import FakeLLM

    liar = FakeLLM(
        structured_response={
            "intent": "provide_availability",
            "availability_phrases": ["Saturday morning"],
            "is_fixed": False,
        }
    )

    understood = MessageReader(llm=liar).read("I'm free Saturday morning.")

    assert understood.decider == "LLMMessageReader"
    assert [w.date for w in understood.interpretation.windows] == [SATURDAY]


def test_can_do_friday_checks_the_route_even_if_model_calls_it_policy(temp_db):
    from dispatch_agent.agents.understanding import MessageReader
    from tests.conftest import FakeLLM

    misunderstood = FakeLLM(structured_response={"intent": "policy_question"})
    understood = MessageReader(llm=misunderstood).read("Can you do Friday?")

    assert understood.interpretation.intent == "provide_availability"
    assert [w.date for w in understood.interpretation.windows] == [FRIDAY]


def test_an_unavailable_model_degrades_and_says_so(temp_db):
    """The demo must survive a provider outage -- and must not claim a model made the reading."""
    from dispatch_agent.agents.understanding import MessageReader

    class Broken:
        def extract_structured(self, **kwargs):
            raise RuntimeError("bedrock unavailable")

        def complete(self, *a, **k):
            raise RuntimeError("bedrock unavailable")

    understood = MessageReader(llm=Broken()).read("I'm free Saturday morning.")

    assert understood.decider == "RuleMessageReader"
    assert "bedrock unavailable" in understood.fallback_reason
    assert not understood.used_model
    # ...and the fallback still understood the message.
    assert [w.date for w in understood.interpretation.windows] == [SATURDAY]


def test_a_model_inventing_an_intent_is_ignored(temp_db):
    from dispatch_agent.agents.understanding import MessageReader
    from tests.conftest import FakeLLM

    nonsense = FakeLLM(structured_response={"intent": "cancel_everything"})

    understood = MessageReader(llm=nonsense).read("hello there")

    assert understood.interpretation.intent in language.INTENTS


def test_an_acceptance_with_nothing_offered_is_not_an_acceptance(temp_db):
    """A model that reads "okay" as an acceptance when we have proposed nothing must not be able
    to confirm a booking that does not exist."""
    from dispatch_agent.agents.understanding import MessageReader
    from tests.conftest import FakeLLM

    eager = FakeLLM(structured_response={"intent": "accept"})

    understood = MessageReader(llm=eager).read("okay", has_open_offer=False)

    assert understood.interpretation.intent != "accept"


def test_take_that_one_is_a_demonstrative_not_an_ordinal(temp_db):
    """"Take that one" against two options does not mean "the first one".

    It read as ordinal 1 because "one" was in the ordinal table, so a customer still choosing
    between two times got the earlier one booked without being asked. Nothing on screen would have
    shown them it was a guess.
    """
    _, offer = _two_slot_offer(temp_db)
    assert len(offer.options) == 2

    said = language.interpret("Okay, take that one.", has_open_offer=True)

    assert said.accepted_ordinal is None
    with pytest.raises(conversation.AmbiguousAcceptance):
        conversation.resolve_accepted_slot(offer, said)


def test_an_explicit_ordinal_still_resolves(temp_db):
    """The other side of the same fix -- "the first one" must keep working."""
    _, offer = _two_slot_offer(temp_db)

    for phrase, index in [("take the first one", 0), ("the second one please", 1)]:
        chosen = conversation.resolve_accepted_slot(
            offer, language.interpret(phrase, has_open_offer=True)
        )
        assert chosen.id == offer.options[index].id, phrase


def test_the_models_is_fixed_answer_is_not_trusted(temp_db):
    """A live smoke test had gpt-4o-mini mark "I'm free Saturday morning" as fixed.

    That is a plain statement of availability, not an ultimatum, and believing it silently switches
    off counteroffers -- so the customer is never told about a better slot, and nothing on any
    screen says why. Whether a message contains an exclusivity phrase is a narrow lexical question
    the regex answers reliably, so the model's answer is ignored for this field.
    """
    from dispatch_agent.agents.understanding import MessageReader
    from tests.conftest import FakeLLM

    overeager = FakeLLM(
        structured_response={
            "intent": "provide_availability",
            "availability_phrases": ["Saturday morning"],
            "is_fixed": True,
        }
    )

    understood = MessageReader(llm=overeager).read("I'm free Saturday morning.")

    assert not understood.interpretation.is_fixed


def test_a_genuine_exclusivity_phrase_is_still_fixed(temp_db):
    """The other side: the regex must catch it even when the model says nothing."""
    from dispatch_agent.agents.understanding import MessageReader
    from tests.conftest import FakeLLM

    quiet = FakeLLM(
        structured_response={
            "intent": "provide_availability",
            "availability_phrases": ["Saturday morning"],
        }
    )

    understood = MessageReader(llm=quiet).read(
        "Saturday morning, that's the only time I can do."
    )

    assert understood.interpretation.is_fixed


def test_a_stated_preference_survives_the_models_ordering(temp_db):
    """The same smoke test returned the phrases in the order they were spoken, ignoring "but
    Friday is better". A dropped preference is the customer's wish being overruled by a routing
    score they cannot see, so the ranking is re-derived from the sentence."""
    from dispatch_agent.agents.understanding import MessageReader
    from tests.conftest import FakeLLM

    spoken_order = FakeLLM(
        structured_response={
            "intent": "provide_availability",
            "availability_phrases": ["Saturday afternoon", "Friday morning"],
        }
    )

    understood = MessageReader(llm=spoken_order).read(
        "I can do Saturday afternoon or Friday morning, but Friday is better."
    )

    ranked = sorted(understood.interpretation.windows, key=lambda w: w.preference_rank)
    assert [w.date for w in ranked] == [FRIDAY, SATURDAY]
