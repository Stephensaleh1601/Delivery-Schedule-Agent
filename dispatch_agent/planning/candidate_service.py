"""Evaluating a customer's proposed windows against the real operation.

This is the deterministic core of the product. For each date+window a customer said they could
accept, it solves that day with and without the order and reports what saying yes would cost.
The language model chooses between the results; it never produces them.

Two design points worth knowing:

**The baseline is always re-solved, never read from a stored plan.** The brief suggests falling
back to solving only when no stored plan exists, but comparing a stored plan against a freshly
solved one makes the incremental cost meaningless -- possibly negative -- because the stored
plan may have been produced with a different job set, different settings, or a different routing
provider. Stored plans are for display; comparisons are made against a baseline computed in the
same run, with the same parameters.

**One service instance evaluates one order.** Each date's baseline is solved once and memoised,
so three options spread over two dates cost two baselines rather than three. Combined with the
drive-time cache underneath RoutingClient, evaluating a full booking is a handful of solves and
almost no provider traffic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date

from dispatch_agent.config import settings
from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.geo.zones import company_depot
from dispatch_agent.models import (
    AvailabilityOption,
    CandidateSlotEvaluation,
    Coordinates,
    DaySequence,
    JobRecord,
    PlanningStatus,
)
from dispatch_agent.planning import plan_service
from dispatch_agent.planning.clock import PlanningClock
from dispatch_agent.planning.promise_window import promise_window
from dispatch_agent.planning.route_facts import coordinator_reason, customer_reason, route_facts
from dispatch_agent.planning.scoring import ScoringConfig, overtime_minutes, score_candidate
from dispatch_agent.solver import UnsolvableDayError, sequence_day

# Planning statuses whose jobs are actually on the road that day. An order that was cancelled,
# or is still being negotiated, must not shape the route another customer is quoted against.
SCHEDULED_STATUSES = frozenset(
    {
        PlanningStatus.CONFIRMED,
        PlanningStatus.SEQUENCED,
        PlanningStatus.DISPATCHED,
        PlanningStatus.COMPLETED,
    }
)


@dataclass
class DayContext:
    """A date's existing workload and what it currently costs to serve."""

    delivery_date: Date
    jobs: list[JobRecord]
    baseline: DaySequence | None
    baseline_drive_minutes: int
    baseline_error: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.jobs


class CandidateService:
    def __init__(
        self,
        repo: JobsRepository | None = None,
        routing_client: RoutingClient | None = None,
        depot: Coordinates | None = None,
        scoring: ScoringConfig | None = None,
    ):
        self._repo = repo or JobsRepository()
        self._routing = routing_client or RoutingClient()
        self._depot = depot or company_depot()
        self._scoring = scoring or ScoringConfig.from_settings()
        self._contexts: dict[Date, DayContext] = {}

    # -- day baselines --------------------------------------------------------

    def day_context(self, delivery_date: Date) -> DayContext:
        """The date's committed jobs and the cost of serving them, solved at most once."""
        if delivery_date in self._contexts:
            return self._contexts[delivery_date]

        jobs = [
            job
            for job in self._repo.jobs_for_date(delivery_date)
            if job.planning_status in SCHEDULED_STATUSES and job.readiness_status.value == "ready"
        ]
        baseline: DaySequence | None = None
        error: str | None = None
        if jobs:
            try:
                baseline = self._solve(jobs, delivery_date)
            except UnsolvableDayError as exc:
                # An already-broken day is not this customer's fault, but nothing can be quoted
                # against it either -- every candidate for this date will report the reason.
                error = str(exc)
        else:
            baseline = DaySequence(
                delivery_date=delivery_date, stops=[], total_drive_minutes=0, return_drive_minutes=0
            )
        if baseline is not None:
            baseline = self._with_distances(baseline, {j.id: j for j in jobs})

        context = DayContext(
            delivery_date=delivery_date,
            jobs=jobs,
            baseline=baseline,
            baseline_drive_minutes=baseline.round_trip_drive_minutes if baseline else 0,
            baseline_error=error,
        )
        self._contexts[delivery_date] = context
        return context

    def _with_distances(self, sequence: DaySequence, jobs_by_id: dict[str, JobRecord]) -> DaySequence:
        """Kilometres for a sequence the solver has just produced.

        The solver is handed a precomputed matrix and has no routing client, so distance is not
        part of what it returns -- which is why a candidate evaluation reported 0.0 km either side
        and would have rendered a fabricated zero next to real driving minutes. The solve has just
        warmed the drive-time cache for exactly these points, so asking for them here costs no
        provider requests.
        """
        return plan_service.annotate_distances(sequence, jobs_by_id, self._routing, self._depot)

    def _solve(self, jobs: list[JobRecord], delivery_date: Date) -> DaySequence:
        return sequence_day(
            jobs,
            delivery_date,
            depot=self._depot,
            routing_client=self._routing,
            time_limit_seconds=settings.candidate_solver_time_limit_seconds,
        )

    # -- evaluation -----------------------------------------------------------

    def evaluate(self, order: JobRecord, option: AvailabilityOption) -> CandidateSlotEvaluation:
        """Solve `option`'s day with this order inserted, and price the difference."""
        context = self.day_context(option.date)

        if context.baseline_error is not None:
            return self._infeasible(option, f"that day cannot currently be routed: {context.baseline_error}")

        # What is left of the option after anything the customer has already declined. A rejection
        # applies to the window we proposed, not to their whole day, so the day is re-solved with a
        # hole in it -- the solver punches these out with CumulVar.RemoveInterval and handles the
        # disjoint result natively.
        bookable = option.bookable_windows(
            min_width=order.duration_minutes + settings.promise_min_slack_minutes
        )
        if not bookable:
            return self._infeasible(
                option,
                "the customer has ruled out every part of that day that could hold the delivery",
            )

        # An in-memory copy pinned to just this window. Never persisted -- the order keeps no
        # date and no lock until a customer actually accepts something.
        candidate = order.model_copy(
            update={
                "delivery_date": option.date,
                "availability": bookable,
                "availability_options": [],
                "locked_window": None,
                "planning_status": PlanningStatus.PENDING_PLANNING,
                "status": "new",
            }
        )

        try:
            proposed = self._solve(context.jobs + [candidate], option.date)
        except UnsolvableDayError as exc:
            return self._infeasible(option, str(exc))

        # The stop the solver actually made for this order. `model_copy` preserves the id, so this
        # is a lookup rather than a guess -- and it is what makes the offered window a consequence of
        # the route instead of a restatement of the customer's availability.
        service = next(s.arrival_window for s in proposed.stops if s.job_id == candidate.id)
        # Bound the promise by the PIECE the van actually landed in, not by the option's outer
        # window: with 10-12 declined out of 9-6, a promise bounded by 9-6 could stretch back over
        # the interval the customer just turned down.
        piece = next(
            (w for w in bookable if w.start <= service.start and service.end <= w.end),
            option.window,
        )
        promise = promise_window(service=service, availability=piece)

        jobs_by_id = {job.id: job for job in context.jobs + [candidate]}
        proposed = self._with_distances(proposed, jobs_by_id)
        proposed_minutes = proposed.round_trip_drive_minutes
        added_idle = proposed.idle_minutes - (context.baseline.idle_minutes if context.baseline else 0)
        overtime = overtime_minutes(proposed, jobs_by_id, self._depot, self._scoring)
        # The consequences go into the facts, so the sentence the customer reads is about the day
        # this creates rather than only about where the van happens to be.
        facts = route_facts(
            proposed, candidate, jobs_by_id,
            added_idle_minutes=max(0, added_idle), overtime_minutes=overtime,
        )
        total, breakdown = score_candidate(
            baseline_drive_minutes=context.baseline_drive_minutes,
            proposed_drive_minutes=proposed_minutes,
            is_empty_day=context.is_empty,
            preference_rank=option.preference_rank,
            overtime=overtime,
            config=self._scoring,
            # Extra waiting this insertion creates. A promise for later in the day than the route
            # naturally reaches strands the crew, and no driving figure shows it.
            incremental_idle_minutes=added_idle,
        )

        return CandidateSlotEvaluation(
            availability_option_id=option.id,
            date=option.date,
            window=option.window,
            service_window=service,
            promise_window=promise,
            feasible=True,
            baseline_drive_minutes=context.baseline_drive_minutes,
            proposed_drive_minutes=proposed_minutes,
            baseline_distance_km=context.baseline.round_trip_distance_km if context.baseline else 0.0,
            proposed_distance_km=proposed.round_trip_distance_km,
            baseline_stop_count=len(context.baseline.stops) if context.baseline else 0,
            proposed_stop_count=len(proposed.stops),
            baseline_completion_minutes=context.baseline.completion_minutes if context.baseline else 0,
            proposed_completion_minutes=proposed.completion_minutes,
            baseline_span_minutes=context.baseline.working_span_minutes if context.baseline else 0,
            proposed_span_minutes=proposed.working_span_minutes,
            baseline_idle_minutes=context.baseline.idle_minutes if context.baseline else 0,
            proposed_idle_minutes=proposed.idle_minutes,
            opens_empty_day=context.is_empty,
            preference_rank=option.preference_rank,
            region=facts.region,
            route_position=facts.position,
            route_stop_count=facts.stop_count,
            customer_reason=customer_reason(facts, option.date, promise),
            coordinator_reason=coordinator_reason(
                facts, proposed_minutes - context.baseline_drive_minutes
            ),
            total_score=total,
            proposed_sequence=proposed,
            **breakdown,
        )

    def evaluate_all(self, order: JobRecord) -> list[CandidateSlotEvaluation]:
        """Every option the customer offered, ranked best first.

        Options outside the bookable horizon are rejected here rather than silently scored, so a
        customer is never quoted a date the operation cannot commit to.
        """
        evaluations = []
        for option in order.availability_options:
            if not PlanningClock.is_within_horizon(option.date):
                first, last = PlanningClock.horizon()
                evaluations.append(
                    self._infeasible(
                        option, f"we only take bookings between {first} and {last}"
                    )
                )
                continue
            evaluations.append(self.evaluate(order, option))
        return self.rank(evaluations)

    @staticmethod
    def rank(evaluations: list[CandidateSlotEvaluation]) -> list[CandidateSlotEvaluation]:
        """Feasibility, then what the route actually costs, then the customer's preference, then date.

        Sorting on `not feasible` keeps infeasibility a separate key rather than a large score --
        an impossible slot sorts last no matter how cheap its arithmetic would have been.

        Preference is a key in its own right rather than a term inside the score. Folded into the
        total it was worth ten minutes of driving per rank, which is a number nobody chose on
        purpose: it silently outranks a genuinely better route by a small margin. As a tiebreak it
        does what it is for -- deciding between days the operation is indifferent about.
        """
        return sorted(
            evaluations,
            key=lambda e: (
                not e.feasible,
                e.total_score - e.preference_penalty_minutes,
                e.preference_rank,
                e.date,
            ),
        )

    @staticmethod
    def _infeasible(option: AvailabilityOption, reason: str) -> CandidateSlotEvaluation:
        return CandidateSlotEvaluation(
            availability_option_id=option.id,
            date=option.date,
            window=option.window,
            feasible=False,
            infeasible_reason=reason,
        )
