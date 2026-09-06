"""Offering slots to a customer, and turning an acceptance into a locked appointment.

The lifecycle this implements is the product: nothing is promised until the customer picks
something, and once they have, that promise is protected.

Guardrails live here rather than in a prompt. The two-round cap is enforced by counting the
offers already made, not by asking a model to remember; and acceptance is made idempotent by a
conditional UPDATE whose row count decides whether any work happens, not by a read-then-write
that two clicks can both pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone, datetime

from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.models import (
    InsertionEvidence,
    AppointmentOffer,
    AvailabilityOption,
    CandidateSlotEvaluation,
    CustomerMessage,
    JobRecord,
    MessageDirection,
    OfferedSlot,
    OfferPurpose,
    OfferStatus,
    PlanningStatus,
    RoutePlanVersion,
)
from dispatch_agent.planning import plan_service
from dispatch_agent.solver import LockedPlanInfeasibleError, UnsolvableDayError

MAX_OFFER_ROUNDS = 2
MAX_SLOTS_PER_OFFER = 2
# A customer is asked about a freed slot at most once. How many people get asked per freed date is
# bounded separately, by recovery_service.MAX_REPLACEMENT_OFFERS.
MAX_RECOVERY_OFFERS = 1


class OfferError(Exception):
    """A request that cannot be honoured -- the caller turns this into a customer-facing reply.

    `kind` distinguishes three failures that used to share one message. Only `no_feasible_slot`
    means the customer's windows genuinely cannot be served; the other two are our own bookkeeping
    and must never be reported to a coordinator as "none of the windows can be fitted".
    """

    def __init__(self, message: str, kind: str = "no_feasible_slot"):
        super().__init__(message)
        self.kind = kind


@dataclass
class AcceptanceOutcome:
    offer: AppointmentOffer
    job: JobRecord
    plan: RoutePlanVersion | None
    idempotent: bool = False
    message: str = ""
    # Set when accepting moved the customer off a day they already held -- that day is republished
    # too, since it now has one fewer stop.
    vacated_date: object | None = None
    vacated_plan: RoutePlanVersion | None = None


def create_offer(
    repo: JobsRepository,
    order: JobRecord,
    evaluations: list[CandidateSlotEvaluation],
    purpose: OfferPurpose = OfferPurpose.BOOKING,
    run_id: str | None = None,
    customer_initiated: bool = False,
) -> AppointmentOffer:
    """Put the best feasible slots to a customer.

    Offers the best two when there are two, one when that is all there is. Raises when there are
    none, or when the round cap is reached -- both cases are a human's problem, and inventing a
    slot outside what the customer offered is never the answer.
    """
    # Counted per purpose. Asking a confirmed customer whether they would come forward is not a
    # round of negotiating their original booking, and must not consume one.
    previous = [o for o in repo.offers_for_order(order.id) if o.purpose is purpose]
    cap = MAX_OFFER_ROUNDS if purpose is OfferPurpose.BOOKING else MAX_RECOVERY_OFFERS

    # Keyed on the window, not the option. Declining 10-12 on Friday does not decline Friday, so
    # the same availability option may legitimately be offered again with a different window
    # carved out of it -- what must never repeat is the exact time they already turned down.
    already_offered = {
        (slot.availability_option_id, slot.window.start, slot.window.end)
        for offer in previous
        for slot in offer.options
    }
    servable = [e for e in evaluations if e.feasible]
    if not servable:
        raise OfferError(
            "none of the windows you gave us can be fitted into the schedule",
            kind="no_feasible_slot",
        )

    feasible = [
        e
        for e in servable
        if (e.availability_option_id, e.promise_window.start, e.promise_window.end)
        not in already_offered
    ]

    # The cap prevents the AGENT from pestering someone with endless alternatives. It must not
    # prevent the CUSTOMER from proposing a concrete new time after those rounds. Only a genuinely
    # new window tied to their recorded availability earns another offer; rerunning the old solve
    # or generating another agent suggestion still stops at the cap.
    current_option_ids = {option.id for option in order.availability_options}
    fresh_customer_choice = customer_initiated and any(
        e.availability_option_id in current_option_ids
        for e in feasible
    )
    if len(previous) >= cap and not fresh_customer_choice:
        raise OfferError(
            f"already made {len(previous)} offer rounds for this order -- escalating rather than "
            f"asking the customer again",
            kind="round_cap_reached",
        )
    if not feasible:
        # The windows are fine; we have simply already put all of them to this customer. Saying
        # they "cannot be fitted" here would be false, and it used to raise a coordinator
        # exception for a problem that does not exist.
        raise OfferError(
            "every window this customer offered has already been put to them",
            kind="all_options_already_offered",
        )


    offer = AppointmentOffer(
        order_id=order.id,
        # The authoritative link from what the customer was shown to the calls that chose it.
        run_id=run_id,
        purpose=purpose,
        round_number=len(previous) + 1,
        status=OfferStatus.SENT,
        options=[
            OfferedSlot(
                availability_option_id=e.availability_option_id,
                date=e.date,
                # The narrow window derived from where the solver actually put the van -- not the
                # customer's broad availability, which is what `e.window` still means. No fallback
                # to `e.window` here on purpose: silently promising nine hours is the behaviour this
                # replaces, and it would be invisible.
                window=e.promise_window,
                reason=e.customer_reason,
                score=e.total_score,
            )
            for e in feasible[:MAX_SLOTS_PER_OFFER]
        ],
    )
    repo.save_offer(offer)

    if purpose is OfferPurpose.BOOKING:
        order.set_planning_status(PlanningStatus.OFFERED)
        repo.save_job(order)
    # A recovery target stays CONFIRMED on the day they already hold. Being asked is not being
    # moved -- and OFFERED cannot legally carry the locked window they still have, so setting it
    # here would raise, and without the model validator it would silently drop them off their
    # current route just for being asked.
    return offer


def format_date(value) -> str:
    """"Friday, 5 September". Built by hand rather than with strftime because the no-padding
    directives (%-d on POSIX, %#d on Windows) are platform-specific and crash on the other."""
    return f"{value:%A}, {value.day} {value:%B}"


def format_time(value) -> str:
    """"9:00am" / "2:30pm", same portability reason as format_date."""
    hour = value.hour % 12 or 12
    meridiem = "am" if value.hour < 12 else "pm"
    return f"{hour}:{value.minute:02d}{meridiem}"


def format_window(window) -> str:
    return f"{format_time(window.start)}-{format_time(window.end)}"


def offer_message(offer: AppointmentOffer, some_requests_unavailable: bool = False) -> str:
    """Customer-facing wording. Deliberately says nothing about scores, penalties, or how
    convenient their preference was for us -- that is our problem, not theirs.

    `some_requests_unavailable` is the difference between two sentences that read very differently
    to the person receiving them. "That's the only one of your preferred times we can fit" is an
    apology, and it is a lie when the customer only gave us one time in the first place -- it
    implies we turned something down that they never offered. Told plainly, one workable time gets
    a plain answer.
    """
    if len(offer.options) == 1:
        slot = offer.options[0]
        reason = f" {slot.reason}" if slot.reason else ""
        closing = (
            "That's the only one of your preferred times we can fit -- does it work?"
            if some_requests_unavailable
            else "Does that work?"
        )
        return (
            f"We can deliver on {format_date(slot.date)}, between "
            f"{format_time(slot.window.start)} and {format_time(slot.window.end)}."
            f"{reason} {closing}"
        )

    lines = ["We can deliver on:"]
    for i, slot in enumerate(offer.options, start=1):
        lines.append(f"{i}. {format_date(slot.date)}, {format_window(slot.window)}")
    # Only the recommended slot carries its reason -- two explanations in one message reads as a
    # sales pitch. Say which option it belongs to: unlabelled under a list of three, it reads as
    # if it describes all of them when it describes only the first.
    if offer.options[0].reason:
        lines.append(f"The first is our closest fit: {offer.options[0].reason.lstrip()}")
    lines.append("Please choose whichever suits you best.")
    return "\n".join(lines)


def record_message(
    repo: JobsRepository,
    order_id: str,
    body: str,
    direction=MessageDirection.OUTBOUND,
    run_id: str | None = None,
    offer_id: str | None = None,
) -> CustomerMessage:
    """Persist a message with its provenance attached.

    `run_id` and `offer_id` are written here rather than reconstructed later. The alternative --
    joining a message to "the newest run for this order" -- puts the wrong trace under a message
    as soon as there are two runs, which after a page refresh is every time.
    """
    message = CustomerMessage(
        order_id=order_id, direction=direction, body=body, run_id=run_id, offer_id=offer_id
    )
    repo.save_message(message)
    return message


def accept_offer(
    repo: JobsRepository,
    offer_id: str,
    slot_id: str,
    routing_client: RoutingClient | None = None,
    run_id: str | None = None,
) -> AcceptanceOutcome:
    """Lock in a slot the customer chose, then republish that day's plan.

    Replaying the same acceptance is harmless: the conditional claim below fails the second
    time, and the recorded outcome is returned rather than a second identical plan version.
    """
    offer = repo.get_offer(offer_id)
    if offer is None:
        raise OfferError("that offer no longer exists")

    slot = next((s for s in offer.options if s.id == slot_id), None)
    if slot is None:
        raise OfferError("that slot was not part of this offer")

    # An insertion was tested against a specific published route. If that day has been republished
    # since -- somebody else booked into it, or a delay rebuilt it -- the position we measured no
    # longer means what it meant, and the arrival we promised was computed from stops that have
    # moved. Checked BEFORE the response is claimed, so a refusal leaves the offer open for the
    # customer to choose again rather than burning it.
    if slot.evidence is not None:
        current = repo.active_plan(slot.date)
        if current is None or current.version != slot.evidence.source_plan_version:
            raise OfferError(
                "that day's route changed while you were deciding, so we need you to pick again "
                "from a fresh set of times",
                kind="route_moved_on",
            )

    if not repo.claim_offer_response(offer_id, OfferStatus.ACCEPTED.value):
        # Someone already responded. Return what happened then, without re-solving.
        settled = repo.get_offer(offer_id)
        job = repo.get_job(settled.order_id)
        plan = repo.active_plan(job.delivery_date) if job and job.delivery_date else None
        return AcceptanceOutcome(
            offer=settled, job=job, plan=plan, idempotent=True,
            message="That booking is already confirmed.",
        )

    job = repo.get_job(offer.order_id)
    if job is None:
        raise OfferError("that order no longer exists")

    # A slot we suggested was never the customer's availability -- it was a question. Accepting it
    # is the moment it becomes one, and this is the only place that conversion happens. Recording it
    # here keeps the order's history honest: afterwards it shows a window the customer agreed to,
    # not one we quietly added on their behalf while they were still deciding.
    if not any(o.id == slot.availability_option_id for o in job.availability_options):
        job.availability_options = [
            *job.availability_options,
            AvailabilityOption(
                id=slot.availability_option_id,
                date=slot.date,
                window=slot.window,
                preference_rank=len(job.availability_options) + 1,
            ),
        ]

    previous_date = job.delivery_date
    job.delivery_date = slot.date
    # `availability` stays as the customer stated it. It used to be overwritten with the accepted
    # slot, which was harmless while the two were identical and destroys information now that the
    # promise is narrower -- a rejection or a later reschedule needs to know what they can still do.
    job.locked_window = slot.window
    job.set_planning_status(PlanningStatus.CONFIRMED)
    repo.save_job(job)

    try:
        plan = plan_service.replan_day(
            repo, slot.date, reason=f"{job.customer_name} confirmed {slot.date}", routing_client=routing_client
        )
    except (LockedPlanInfeasibleError, UnsolvableDayError) as exc:
        # The day was quoted as feasible moments ago, so this means something else changed in
        # between. Roll the order back rather than leaving it confirmed against a plan that does
        # not exist, and let a human sort it out.
        job.delivery_date = previous_date
        job.locked_window = None
        job.set_planning_status(PlanningStatus.EXCEPTION)
        repo.save_job(job)
        offer.status = OfferStatus.CLOSED
        repo.save_offer(offer)
        plan_service.raise_coordinator_exception(
            repo,
            f"{job.customer_name} accepted {slot.date} but the day could no longer be routed: {exc}",
            kind="acceptance_failed",
            order_id=job.id,
            delivery_date=slot.date,
        )
        raise OfferError(
            "sorry -- that slot was taken while we were confirming. Our team will call you."
        ) from exc

    # The customer has left the day they were on, so it has one fewer stop and must be
    # republished. Done AFTER the target day succeeds, so a target-day failure still rolls back
    # through the path above without having already torn up a second day.
    vacated_plan = None
    if previous_date is not None and previous_date != slot.date:
        try:
            vacated_plan = plan_service.replan_day(
                repo,
                previous_date,
                reason=f"{job.customer_name} moved to {slot.date}",
                routing_client=routing_client,
            )
        except (LockedPlanInfeasibleError, UnsolvableDayError) as exc:
            # Removing a stop should only make a day easier, so this means something else is
            # wrong with it. The move stands; a human is told about the day left behind.
            plan_service.raise_coordinator_exception(
                repo,
                f"{job.customer_name} moved off {previous_date} but that day could not be "
                f"republished: {exc}",
                kind="vacated_day_replan_failed",
                order_id=job.id,
                delivery_date=previous_date,
            )

    offer.status = OfferStatus.ACCEPTED
    offer.accepted_slot_id = slot.id
    offer.resulting_plan_id = plan.id
    offer.responded_at = datetime.now(timezone.utc)
    repo.save_offer(offer)

    # Any other outstanding offer for this order is now moot.
    for other in repo.offers_for_order(job.id):
        if other.id != offer.id and other.status in (OfferStatus.PENDING, OfferStatus.SENT):
            other.status = OfferStatus.CLOSED
            repo.save_offer(other)

    confirmation = (
        f"You're confirmed for {format_date(slot.date)}, between "
        f"{format_time(slot.window.start)} and {format_time(slot.window.end)}. See you then!"
    )
    record_message(repo, job.id, confirmation, run_id=run_id, offer_id=offer.id)

    return AcceptanceOutcome(
        offer=offer,
        job=job,
        plan=plan,
        message=confirmation,
        vacated_date=previous_date if previous_date and previous_date != slot.date else None,
        vacated_plan=vacated_plan,
    )


def reject_offer(
    repo: JobsRepository, offer_id: str, slot_id: str | None = None
) -> AppointmentOffer:
    """Record that a customer turned an offer down, freeing the order to be offered again.

    The declined windows are carved out of the availability options they came from, so the next
    round proposes a *different* time rather than the same one. This is the difference between
    "not 10 till 12" and "not Friday": the day survives with a hole in it, and the solver already
    knows how to route around one.

    `slot_id` names a single slot the customer declined. Without it the whole offer is declined and
    every window in it is excluded -- which is what the "none of these work" path sends.
    """
    offer = repo.get_offer(offer_id)
    if offer is None:
        raise OfferError("that offer no longer exists")

    if not repo.claim_offer_response(offer_id, OfferStatus.REJECTED.value):
        return repo.get_offer(offer_id)

    offer.status = OfferStatus.REJECTED
    offer.responded_at = datetime.now(timezone.utc)
    repo.save_offer(offer)

    job = repo.get_job(offer.order_id)
    if job is not None:
        declined = [s for s in offer.options if slot_id is None or s.id == slot_id]
        options_by_id = {o.id: o for o in job.availability_options}
        for slot in declined:
            option = options_by_id.get(slot.availability_option_id)
            # Matched by id, never by window equality: the offered window is now the narrow promise
            # derived from the route, so it no longer equals the option it came from.
            if option is not None and slot.window not in option.excluded_windows:
                option.excluded_windows = [*option.excluded_windows, slot.window]
        job.set_planning_status(PlanningStatus.PENDING_PLANNING)
        repo.save_job(job)
    return offer


# -- offers built from the insertion search ------------------------------------
#
# Kept apart from create_offer above, which works from solved-day evaluations. The two carry
# genuinely different policies -- one proven option versus exactly three, escalate below that --
# and the instruction that mattered here was not to let them blend: an offer that had to satisfy
# both would satisfy neither.

# Slots per offer, by purpose. Deliberately NOT one global constant: raising the shared cap to
# three would force the normal offer to carry three choices too, which is the opposite of what it
# is for.
SLOTS_BY_PURPOSE: dict[OfferPurpose, int] = {
    OfferPurpose.BOOKING: 1,      # the normal offer: one option, proven
    OfferPurpose.ALTERNATIVE: 3,  # the fallback: exactly three, or a human takes over
    OfferPurpose.RECOVERY: 1,
}

# The fallback is all-or-nothing. Two safe choices is not "nearly three" -- policy says a customer
# who cannot be given three is a customer a coordinator should call.
REQUIRED_ALTERNATIVES = 3


def offer_insertions(
    repo: JobsRepository,
    order: JobRecord,
    options,
    purpose: OfferPurpose = OfferPurpose.BOOKING,
    run_id: str | None = None,
) -> AppointmentOffer:
    """Put insertion options to a customer, in the order the search returned them.

    `options` are `insertion.InsertionOption`s. They arrive ranked and are never re-sorted here:
    the tool decided the order, and this function's job is to write it down, not to improve on it.

    Raises OfferError rather than trimming or padding. A fallback offer with two choices in it
    would look like a smaller version of the right answer, and it is not -- it is the case the
    policy says to escalate.
    """
    wanted = SLOTS_BY_PURPOSE[purpose]
    if not options:
        raise OfferError(
            "no safe position was found on either published route", kind="no_safe_position"
        )
    if purpose is OfferPurpose.ALTERNATIVE and len(options) < REQUIRED_ALTERNATIVES:
        raise OfferError(
            f"only {len(options)} safe route options were found; policy requires "
            f"{REQUIRED_ALTERNATIVES}",
            kind="too_few_alternatives",
        )

    previous = [o for o in repo.offers_for_order(order.id) if o.purpose is purpose]
    if len(previous) >= _rounds_for(purpose):
        raise OfferError(
            "this customer has already had every automatic round for that kind of offer",
            kind="round_cap_reached",
        )

    ranked = list(options)
    if purpose is OfferPurpose.BOOKING:
        # Preference decides what the NORMAL offer tries first. A customer who says "Saturday
        # morning" and is handed a Friday has not been listened to, however much cheaper Friday
        # is -- route efficiency is our problem, not theirs. It is a stable partition, so within
        # the preferred group the tool's own ranking still decides.
        ranked.sort(key=lambda o: not getattr(o, "matches_preference", False))
    chosen = ranked[:wanted]
    offer = AppointmentOffer(
        order_id=order.id,
        run_id=run_id,
        purpose=purpose,
        round_number=len(previous) + 1,
        status=OfferStatus.SENT,
        options=[_slot_from_insertion(order, option) for option in chosen],
    )
    repo.save_offer(offer)

    order.set_planning_status(PlanningStatus.OFFERED)
    repo.save_job(order)
    return offer


def _rounds_for(purpose: OfferPurpose) -> int:
    if purpose is OfferPurpose.ALTERNATIVE:
        return 1
    if purpose is OfferPurpose.RECOVERY:
        return MAX_RECOVERY_OFFERS
    return MAX_OFFER_ROUNDS


def _slot_from_insertion(order: JobRecord, option) -> OfferedSlot:
    """One offered slot, carrying the evidence that produced it.

    The availability option is created here rather than looked up: an insertion is a window WE
    found, not one the customer stated, so there is nothing existing to point at. It becomes part
    of their availability only if they accept -- which is the same boundary route-aware
    suggestions already respect.
    """
    availability = AvailabilityOption(date=option.date, window=option.window)
    order.availability_options = [
        *[o for o in order.availability_options if o.date != option.date or o.window != option.window],
        availability,
    ]
    return OfferedSlot(
        availability_option_id=availability.id,
        date=option.date,
        window=option.window,
        reason=(
            f"We are already delivering near you that {option.slot.label.lower()} -- "
            f"{option.anchor_distance_km}km away, just {option.placement} stop "
            f"{option.anchor_stop_number}."
        ),
        score=int(round(option.added_distance_km * 10)),
        evidence=InsertionEvidence(
            source_plan_id=option.source_plan_id,
            source_plan_version=option.source_plan_version,
            anchor_stop_number=option.anchor_stop_number,
            anchor_distance_km=option.anchor_distance_km,
            placement=option.placement,
            insert_position=option.insert_position,
            added_distance_km=option.added_distance_km,
            added_minutes=option.added_minutes,
            expected_arrival=option.expected_arrival,
            finish_before=option.finish_before,
            finish_after=option.finish_after,
        ),
    )
