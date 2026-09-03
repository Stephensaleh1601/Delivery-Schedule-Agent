"""One customer message, handled end to end.

This is the seam between "a person typing on WhatsApp" and the deterministic machinery. It owns
three things the rest of the system deliberately does not:

**What the customer meant.** The model reads the message; `language.interpret` is the fallback and
the grounding. Either way the result is a typed `Interpretation`, and the model never gets to
compute a date, a drive time or a window -- it says *what the customer wants*, and the tools say
what is true.

**Which offer they are talking about.** "Take the first one" and "confirm 1pm" only mean anything
against a specific open offer. Resolution happens here, against the offer actually on the table, and
an ambiguous reference produces a question rather than a booking. Accepting the wrong slot silently
is the worst failure this system has, so it is the one guarded hardest.

**That one message produces one run.** The earlier duplicate-offer bug came from two API calls each
starting their own negotiation with different slots. The guarantee now is structural: this module is
the single entry point, it creates exactly one run, and it refuses to start a second while one is
in flight for the same order.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as Date

from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.models import (
    AgentRunLog,
    AppointmentOffer,
    AvailabilityOption,
    CandidateSlotEvaluation,
    CustomerMessage,
    JobRecord,
    MessageDirection,
    OfferStatus,
    PlanningEvent,
    PlanningEventType,
    PlanningStatus,
)
from dispatch_agent.planning import language, offer_service
from dispatch_agent.planning.clock import PlanningClock


@dataclass
class Reply:
    """What the conversation produced for one inbound message."""

    messages: list[CustomerMessage] = field(default_factory=list)
    offer: AppointmentOffer | None = None
    run: AgentRunLog | None = None
    evaluations: list[CandidateSlotEvaluation] = field(default_factory=list)
    confirmed: bool = False
    # The intent we acted on, for the trace and for tests.
    intent: str = "unclear"

    @property
    def body(self) -> str:
        return "\n\n".join(m.body for m in self.messages)


# -- resolving what they accepted ----------------------------------------------


class AmbiguousAcceptance(Exception):
    """The customer said yes to something we cannot identify with certainty.

    Raised rather than guessed. A wrong booking is a van at the wrong door, so the only safe
    response to "okay" against two open offers is to ask which one.
    """

    def __init__(self, message: str, options: list[str] | None = None):
        super().__init__(message)
        self.options = options or []


def resolve_accepted_slot(offer: AppointmentOffer, said: language.Interpretation):
    """Which slot of `offer` the customer meant, or an exception asking them to be specific.

    Deliberately narrow. It resolves an ordinal ("the first one"), a day name, or a time that
    appears in exactly one slot. Anything matching two slots, or none, is ambiguous -- including a
    bare "okay" when two were offered, which is the case people most expect us to guess at.
    """
    if not offer.options:
        raise AmbiguousAcceptance("there is nothing on the table to accept")

    if len(offer.options) == 1:
        # Only one thing was proposed, so "okay" is unambiguous whatever words they used.
        return offer.options[0]

    if said.accepted_ordinal is not None:
        index = said.accepted_ordinal - 1
        if 0 <= index < len(offer.options):
            return offer.options[index]
        raise AmbiguousAcceptance(
            f"they referred to option {said.accepted_ordinal} but only {len(offer.options)} "
            f"were offered"
        )

    phrase = (said.accepted_phrase or said.note or "").lower()
    if phrase:
        matches = [slot for slot in offer.options if _slot_matches(slot, phrase)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousAcceptance(
                "what they said matches more than one of the times we offered",
                [offer_service.format_date(s.date) for s in matches],
            )

    raise AmbiguousAcceptance(
        "they agreed but did not say which time",
        [
            f"{offer_service.format_date(s.date)}, {offer_service.format_window(s.window)}"
            for s in offer.options
        ],
    )


def _slot_matches(slot, phrase: str) -> bool:
    """Whether a customer's words pick out this slot.

    Matches on the weekday, the date, or a clock time falling inside the window -- "1pm" identifies
    a 13:00-15:00 slot. Not fuzzy: a time outside the window is not a match, so "confirm 4pm"
    against a 1-3 slot correctly fails and asks rather than booking the nearest thing.
    """
    if slot.date.strftime("%A").lower() in phrase:
        return True
    # Built by hand rather than with %-d: the no-padding directive is POSIX-only and raises on
    # Windows, which is where this demo runs.
    if f"{slot.date.day} {slot.date.strftime('%B').lower()}" in phrase:
        return True
    if slot.date.isoformat() in phrase:
        return True

    named = language.parse_time(phrase, assume_afternoon=True)
    if named and slot.window.start <= named < slot.window.end:
        return True

    # An explicit "9am-11am" quoted back.
    window = language.parse_window(phrase)
    if window and window.start == slot.window.start:
        return True

    return False


# -- horizon -------------------------------------------------------------------


def horizon_complaint(dates: list[Date]) -> str | None:
    """A sentence explaining the bookable window, when a customer asks for a date outside it.

    Returns None when every date is fine. The wording says *why* rather than only refusing, because
    "we cannot do that" with no reason is the thing that makes an automated agent infuriating.
    """
    first, last = PlanningClock.horizon()
    outside = [d for d in dates if not PlanningClock.is_within_horizon(d)]
    if not outside:
        return None

    too_soon = [d for d in outside if d < first]
    when = (
        "we need a couple of days' notice to fit a delivery into a route"
        if too_soon
        else "we only plan a few days ahead"
    )
    return (
        f"Sorry -- {when}, so right now we can book between "
        f"{offer_service.format_date(first)} and {offer_service.format_date(last)}. "
        f"Would any day in that range work?"
    )


# -- state ---------------------------------------------------------------------


def open_offer(repo: JobsRepository, order_id: str) -> AppointmentOffer | None:
    """The offer currently awaiting a reply, if any."""
    live = [
        o
        for o in repo.offers_for_order(order_id)
        if o.status in (OfferStatus.PENDING, OfferStatus.SENT)
    ]
    if not live:
        return None
    # Newest by round, since an earlier round is closed when a later one is made.
    return max(live, key=lambda o: o.round_number)


def context_date(offer: AppointmentOffer | None) -> Date | None:
    """The date under discussion, so "after 2 instead" attaches to the right day."""
    return offer.options[0].date if offer and offer.options else None


def record_inbound(
    repo: JobsRepository, order_id: str, body: str
) -> CustomerMessage:
    """The customer's own words, persisted before anything is done with them.

    Written first and unconditionally: if the agent then fails, the thread still shows what they
    said, and a coordinator can see the message that caused the problem.
    """
    return offer_service.record_message(
        repo, order_id, body, direction=MessageDirection.INBOUND
    )


def merge_availability(
    order: JobRecord, stated: list[language.StatedWindow]
) -> list[AvailabilityOption]:
    """Fold newly stated windows into the order's availability, without inventing any.

    Only windows the customer actually said reach this function. A window we merely suggested is a
    `negotiation.Suggestion` and never arrives here -- which is the structural half of "never
    silently claim the customer is available at a time they did not provide".

    A restatement of the same date replaces the earlier window rather than accumulating: "actually,
    make it Saturday afternoon" is a correction, not a second option.
    """
    existing = {o.date: o for o in order.availability_options}
    for said in stated:
        previous = existing.get(said.date)
        existing[said.date] = AvailabilityOption(
            # Keep the id where the date is unchanged, so any offer already referring to it, and
            # any exclusions recorded against it, still point at something real.
            id=previous.id if previous else AvailabilityOption(date=said.date, window=said.window).id,
            date=said.date,
            window=said.window,
            excluded_windows=previous.excluded_windows if previous else [],
            preference_rank=said.preference_rank,
        )

    ordered = sorted(existing.values(), key=lambda o: (o.preference_rank, o.date))
    for rank, option in enumerate(ordered, start=1):
        option.preference_rank = rank
    return ordered


def is_only_option(order: JobRecord) -> bool:
    """Whether the customer has told us their timing is fixed.

    Stored on the order as a note rather than inferred each turn, because it has to survive the
    next message: "that's my only time" said once must stop us counteroffering for the rest of the
    conversation, not just for one reply.
    """
    return bool(order.notes and "[timing-fixed]" in order.notes)


def mark_timing_fixed(order: JobRecord) -> None:
    if not is_only_option(order):
        order.notes = f"{order.notes or ''} [timing-fixed]".strip()


# -- one run per message -------------------------------------------------------


def planning_event_for(intent: str, order_id: str, payload: dict) -> PlanningEvent:
    """The event type this intent corresponds to, so the existing agent loop drives it."""
    mapping = {
        "provide_availability": PlanningEventType.NEW_ORDER,
        "accept": PlanningEventType.CUSTOMER_ACCEPTED_OFFER,
        "reject": PlanningEventType.CUSTOMER_REJECTED_OFFER,
    }
    return PlanningEvent(
        event_type=mapping.get(intent, PlanningEventType.MANUAL_RETRY),
        order_id=order_id,
        payload=payload,
    )


_WHITESPACE = re.compile(r"\s+")


def normalise(body: str) -> str:
    """A message reduced to a comparison key, for duplicate detection."""
    return _WHITESPACE.sub(" ", (body or "").strip().lower())


def is_duplicate_send(repo: JobsRepository, order_id: str, body: str) -> bool:
    """Whether this is the same message we just processed.

    Guards the double-tap: a customer pressing send twice, or a client retrying, must not open two
    negotiations. Compared against the last inbound message only -- someone genuinely repeating
    themselves later in the conversation is saying something, and should be answered.
    """
    inbound = [m for m in repo.messages(order_id) if m.direction is MessageDirection.INBOUND]
    return bool(inbound) and normalise(inbound[-1].body) == normalise(body)


def order_is_settled(order: JobRecord) -> bool:
    """Whether this order's booking conversation is finished."""
    return order.planning_status in (
        PlanningStatus.CONFIRMED,
        PlanningStatus.SEQUENCED,
        PlanningStatus.DISPATCHED,
        PlanningStatus.COMPLETED,
        PlanningStatus.CANCELLED,
    )
