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
    AppointmentOffer,
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
    if len(previous) >= cap:
        raise OfferError(
            f"already made {len(previous)} offer rounds for this order -- escalating rather than "
            f"asking the customer again",
            kind="round_cap_reached",
        )

    already_offered = {slot.availability_option_id for offer in previous for slot in offer.options}
    servable = [e for e in evaluations if e.feasible]
    if not servable:
        raise OfferError(
            "none of the windows you gave us can be fitted into the schedule",
            kind="no_feasible_slot",
        )

    feasible = [e for e in servable if e.availability_option_id not in already_offered]
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
        purpose=purpose,
        round_number=len(previous) + 1,
        status=OfferStatus.SENT,
        options=[
            OfferedSlot(
                availability_option_id=e.availability_option_id,
                date=e.date,
                window=e.window,
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


def offer_message(offer: AppointmentOffer) -> str:
    """Customer-facing wording. Deliberately says nothing about scores, penalties, or how
    convenient their preference was for us -- that is our problem, not theirs."""
    if len(offer.options) == 1:
        slot = offer.options[0]
        return (
            f"We can deliver on {format_date(slot.date)}, between "
            f"{format_time(slot.window.start)} and {format_time(slot.window.end)}. "
            f"That's the only one of your preferred times we can fit -- does it work?"
        )

    lines = ["We can deliver on:"]
    for i, slot in enumerate(offer.options, start=1):
        lines.append(f"{i}. {format_date(slot.date)}, {format_window(slot.window)}")
    lines.append("Please choose whichever suits you best.")
    return "\n".join(lines)


def record_message(repo: JobsRepository, order_id: str, body: str, direction=MessageDirection.OUTBOUND) -> None:
    repo.save_message(CustomerMessage(order_id=order_id, direction=direction, body=body))


def accept_offer(
    repo: JobsRepository,
    offer_id: str,
    slot_id: str,
    routing_client: RoutingClient | None = None,
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

    previous_date = job.delivery_date
    job.delivery_date = slot.date
    job.availability = [slot.window]
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
        job.availability = []
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
    record_message(repo, job.id, confirmation)

    return AcceptanceOutcome(
        offer=offer,
        job=job,
        plan=plan,
        message=confirmation,
        vacated_date=previous_date if previous_date and previous_date != slot.date else None,
        vacated_plan=vacated_plan,
    )


def reject_offer(repo: JobsRepository, offer_id: str) -> AppointmentOffer:
    """Record that a customer turned an offer down, freeing the order to be offered again."""
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
        job.set_planning_status(PlanningStatus.PENDING_PLANNING)
        repo.save_job(job)
    return offer
