"""Recovering a delivery slot when an order cannot go out.

The business case for the whole product: a manufacturing delay does not just lose one delivery,
it wastes the slot too, and today nobody notices in time to refill it. When goods are marked
delayed, the day is rebuilt without that stop and the freed capacity is offered to a customer
who already said they would take an earlier delivery.

Two rules this must not break:

- Consent is never assumed. Only customers who explicitly opted in to an earlier delivery are
  approached, and being approached is an offer, not a move.
- Everyone else's promise still holds. Removing the delayed order is a replan like any other,
  so it goes through plan_service and the locked-window check applies unchanged.

Deliberately reuses CandidateService rather than growing its own scoring. A replacement is
chosen on exactly the same terms as an ordinary booking.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date

from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.models import (
    AvailabilityOption,
    OfferPurpose,
    TimeWindow,
    CandidateSlotEvaluation,
    JobRecord,
    Notification,
    PlanningStatus,
    ReadinessStatus,
    RoutePlanVersion,
)
from dispatch_agent.planning import offer_service, plan_service
from dispatch_agent.planning.candidate_service import CandidateService
from dispatch_agent.solver import LockedPlanInfeasibleError, UnsolvableDayError

MAX_REPLACEMENT_OFFERS = 2


def offer_freed_slot(
    repo: JobsRepository,
    order_id: str,
    freed_date: Date,
    window: TimeWindow | None = None,
    routing_client: RoutingClient | None = None,
):
    """Put a slot that has just opened up to a customer who agreed to come forward.

    This mints an offer for a date the customer never listed, which is only legitimate because
    `can_deliver_early` is an explicit opt-in and because it is an OFFER: nothing about their
    existing appointment changes unless they accept it themselves.

    The synthetic availability option is persisted rather than invented on the fly, so the offer's
    availability_option_id points at something real -- and so the freed date becomes evaluable for
    that customer afterwards.
    """
    job = repo.get_job(order_id)
    if job is None:
        raise offer_service.OfferError("that order no longer exists", kind="unknown_order")
    if not job.can_deliver_early:
        raise offer_service.OfferError(
            f"{job.customer_name} has not agreed to an earlier delivery", kind="no_consent"
        )
    if job.readiness_status is not ReadinessStatus.READY:
        raise offer_service.OfferError(
            f"{job.customer_name}'s goods are not ready", kind="not_ready"
        )
    if job.delivery_date is None or job.delivery_date <= freed_date:
        raise offer_service.OfferError(
            "recovery moves a customer forward, never back", kind="not_earlier"
        )

    quoted = window or job.locked_window or (job.availability[0] if job.availability else None)
    if quoted is None:
        raise offer_service.OfferError("no window to offer", kind="no_window")

    option = next(
        (o for o in job.availability_options if o.date == freed_date and o.window == quoted), None
    )
    if option is None:
        option = AvailabilityOption(date=freed_date, window=quoted, preference_rank=1)
        job.availability_options = [*job.availability_options, option]
        repo.save_job(job)

    evaluation = CandidateService(repo=repo, routing_client=routing_client).evaluate(job, option)
    if not evaluation.feasible:
        raise offer_service.OfferError(
            f"that slot cannot be served: {evaluation.infeasible_reason}", kind="no_feasible_slot"
        )

    offer = offer_service.create_offer(
        repo, job, [evaluation], purpose=OfferPurpose.RECOVERY
    )
    # The window in the message must be the one on the offer, not `quoted`: create_offer narrows
    # it to the solved arrival, and telling the customer a different time from the one they can tap
    # is how a demo turns into a support call.
    promised = offer.options[0].window
    message = (
        f"Hi {job.customer_name} -- a slot has opened up on "
        f"{offer_service.format_date(freed_date)}, {offer_service.format_window(promised)}. "
        f"You mentioned an earlier delivery would suit you. Your existing booking on "
        f"{offer_service.format_date(job.delivery_date)} stands unless you take this one."
    )
    offer_service.record_message(repo, job.id, message)
    return offer, message, evaluation


@dataclass
class ReplacementCandidate:
    job: JobRecord
    evaluation: CandidateSlotEvaluation

    @property
    def score(self) -> int:
        return self.evaluation.total_score


@dataclass
class DisruptionOutcome:
    job: JobRecord
    freed_date: Date | None
    plan: RoutePlanVersion | None
    replacements: list[ReplacementCandidate]
    error: str | None = None


def mark_readiness(
    repo: JobsRepository,
    order_id: str,
    readiness: ReadinessStatus,
    routing_client: RoutingClient | None = None,
) -> DisruptionOutcome:
    """Record a readiness change from the (mock) ERP and rebuild the affected day.

    A delayed order keeps its history -- it is not deleted, and it is not silently cancelled.
    It simply stops being routable, which is what frees the capacity.
    """
    job = repo.get_job(order_id)
    if job is None:
        raise ValueError(f"unknown order {order_id!r}")

    previous = job.readiness_status
    freed_date = job.delivery_date
    job.readiness_status = readiness
    repo.save_job(job)

    if previous is readiness or freed_date is None:
        return DisruptionOutcome(job=job, freed_date=freed_date, plan=None, replacements=[])

    plan = None
    error = None
    try:
        plan = plan_service.replan_day(
            repo,
            freed_date,
            reason=f"{job.customer_name}'s order became {readiness.value}",
            routing_client=routing_client,
        )
    except LockedPlanInfeasibleError as exc:
        # Removing work should never make a day harder, so this means something else is wrong.
        error = str(exc)
        plan_service.raise_coordinator_exception(
            repo, f"Could not rebuild {freed_date} after a readiness change: {exc}",
            kind="replan_failed", order_id=order_id, delivery_date=freed_date,
        )
    except UnsolvableDayError as exc:
        error = str(exc)

    replacements = []
    if readiness is ReadinessStatus.DELAYED:
        replacements = find_replacements(repo, freed_date, routing_client=routing_client)
        repo.add_notification(
            Notification(
                message=(
                    f"{job.customer_name}'s delivery on {freed_date} is delayed and has been "
                    f"taken off the route. "
                    + (
                        f"{len(replacements)} customer(s) could take the freed slot."
                        if replacements
                        else "No customer has agreed to an earlier delivery, so the slot is idle."
                    )
                ),
                dates=[freed_date],
            )
        )

    return DisruptionOutcome(
        job=job, freed_date=freed_date, plan=plan, replacements=replacements, error=error
    )


def find_replacements(
    repo: JobsRepository,
    target_date: Date,
    routing_client: RoutingClient | None = None,
    limit: int = MAX_REPLACEMENT_OFFERS,
) -> list[ReplacementCandidate]:
    """Ranked customers who could move onto `target_date`.

    Restricted to orders that are ready, currently scheduled LATER than the target, and whose
    customer opted in to an earlier delivery. The horizon rule deliberately does not apply here:
    it governs what a customer may request at booking, not whether we may ask an existing
    customer about a date that has just opened up -- applying it would reject every replacement
    the recovery flow is for.
    """
    candidates = [
        job
        for job in repo.jobs_by_planning_status(PlanningStatus.CONFIRMED, PlanningStatus.SEQUENCED)
        if job.can_deliver_early
        and job.readiness_status is ReadinessStatus.READY
        and job.delivery_date is not None
        and job.delivery_date > target_date
    ]
    if not candidates:
        return []

    service = CandidateService(repo=repo, routing_client=routing_client)
    ranked: list[ReplacementCandidate] = []
    for job in candidates:
        option = next(
            (o for o in job.availability_options if o.date == target_date),
            AvailabilityOption(
                date=target_date,
                # Quote the window they already agreed to, on the newly free day. Still an
                # offer -- nothing moves unless they say yes.
                window=job.locked_window or (job.availability[0] if job.availability else None),
                preference_rank=1,
            ),
        )
        if option.window is None:
            continue
        evaluation = service.evaluate(job, option)
        if evaluation.feasible:
            ranked.append(ReplacementCandidate(job=job, evaluation=evaluation))

    ranked.sort(key=lambda c: c.score)
    return ranked[:limit]
