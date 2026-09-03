"""When to argue with the customer, and what to offer instead.

Two decisions live here, and both are policy rather than arithmetic, so both are configurable and
neither is left to a language model.

**Should we counteroffer at all?** A coordinator who pushes back on every booking to save a minute
is a coordinator nobody wants to deal with. The customer asked for a time; if we can serve it and it
is not materially worse than the alternatives, we say yes. `should_counteroffer` states exactly what
"materially worse" means -- infeasible, or overtime, or opening a delivery day that would otherwise
stay shut, or a genuinely large driving saving elsewhere.

**What should we suggest instead?** `route_aware_windows` searches the horizon for days the van is
already working near this customer, and derives promise windows from the arrivals OR-Tools actually
produces. Every alternative is a real solved slot, not a guess about the map.

The distinction the rest of the system depends on:

- **stated availability** -- what the customer said. Only they can create this.
- **tentative suggestion** -- what we are asking about. Created here. NOT availability.
- **offered promise** -- a tentative window put to them formally, as an AppointmentOffer.
- **locked appointment** -- one they accepted.

A suggestion never becomes availability until the customer accepts it. That is enforced structurally:
`route_aware_windows` returns `Suggestion` objects and writes nothing, and the only path from a
suggestion into `job.availability_options` runs through an acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date

from dispatch_agent.config import settings
from dispatch_agent.models import (
    AvailabilityOption,
    CandidateSlotEvaluation,
    JobRecord,
    TimeWindow,
)
from dispatch_agent.planning.candidate_service import CandidateService
from dispatch_agent.planning.clock import PlanningClock

MAX_SUGGESTIONS = 2


def material_saving_minutes() -> int:
    """How much driving another day must save before we ask the customer to move.

    Read per call, not bound at import: a module constant would freeze whatever the environment
    happened to say when this file was first imported, and a test tuning the policy would change
    the number and change no behaviour.
    """
    return settings.counteroffer_saving_minutes


@dataclass(frozen=True)
class CounterofferDecision:
    """Whether to push back, and the reason in operational terms."""

    should_ask: bool
    reason: str
    # The machine-readable version, for the log and the inspector.
    kind: str = "honour_request"


def should_counteroffer(
    requested: CandidateSlotEvaluation | None,
    alternatives: list["Suggestion"],
    customer_says_fixed: bool = False,
) -> CounterofferDecision:
    """Whether the customer's own request is worth arguing with.

    The default answer is no. A request we can serve is served -- route efficiency is our problem,
    and a customer who gave us a workable time should not be negotiated at for a marginal gain.
    """
    if customer_says_fixed:
        # They have told us this is the only time. Asking again is not negotiation, it is nagging,
        # and the brief is explicit that we stop.
        return CounterofferDecision(
            should_ask=False,
            kind="customer_fixed",
            reason="the customer said this is their only possible time",
        )

    if requested is None or not requested.feasible:
        return CounterofferDecision(
            should_ask=True,
            kind="infeasible",
            reason="the requested time cannot be served at all",
        )

    added_idle = requested.proposed_idle_minutes - requested.baseline_idle_minutes
    if added_idle >= settings.material_idle_minutes:
        # The case the browser test found: +1 driving minute, +3h46 on the working day. Driving
        # alone called it the efficient choice. A crew sitting outside a block for three hours has
        # driven nowhere, which is precisely why no driving figure showed it.
        return CounterofferDecision(
            should_ask=True,
            kind="idle_gap",
            reason=(
                f"serving it leaves the crew waiting about {added_idle // 60}h "
                f"{added_idle % 60:02d}m that day"
            ),
        )

    if requested.overtime_penalty_minutes > 0:
        return CounterofferDecision(
            should_ask=True,
            kind="overtime",
            reason=(
                f"serving it would run the crew {requested.overtime_penalty_minutes} minutes past "
                f"the end of the working day"
            ),
        )

    if requested.opens_empty_day:
        # Only worth raising if there is somewhere else to put them -- otherwise we would be
        # complaining about a day we are going to open anyway.
        elsewhere = [s for s in alternatives if not s.evaluation.opens_empty_day]
        if elsewhere:
            return CounterofferDecision(
                should_ask=True,
                kind="opens_new_day",
                reason="it would open a delivery day with no other work on it",
            )

    if alternatives:
        best = min(alternatives, key=lambda s: s.evaluation.incremental_drive_minutes)
        saving = requested.incremental_drive_minutes - best.evaluation.incremental_drive_minutes
        if saving >= material_saving_minutes():
            return CounterofferDecision(
                should_ask=True,
                kind="material_saving",
                reason=f"another day would save about {saving} minutes of driving",
            )

    return CounterofferDecision(
        should_ask=False,
        kind="honour_request",
        reason="the time they asked for works and is not materially worse than the alternatives",
    )


@dataclass
class Suggestion:
    """A window WE are proposing, derived from a solved route.

    Not availability. The customer has not agreed to this and may never; it exists to be asked
    about. `option` is an unsaved AvailabilityOption carrying the id the evaluation refers to --
    it is persisted only if and when the customer accepts.
    """

    date: Date
    window: TimeWindow
    evaluation: CandidateSlotEvaluation
    option: AvailabilityOption

    @property
    def reason(self) -> str | None:
        return self.evaluation.customer_reason


def route_aware_windows(
    order: JobRecord,
    service: CandidateService,
    exclude_dates: set[Date] | None = None,
    limit: int = MAX_SUGGESTIONS,
) -> list[Suggestion]:
    """Days across the horizon where this delivery would fit the route well.

    Each candidate date is solved with the order inserted into a full working day; the promise
    window comes from the arrival OR-Tools chose, so a suggestion is always a slot we could actually
    keep. Days that already have compatible work rank ahead of empty ones because inserting into an
    existing route is what makes an alternative worth asking about.

    Returns at most `limit`, and never a date the customer already ruled out.
    """
    exclude = exclude_dates or set()
    working_day = TimeWindow(start=settings.work_day_start, end=settings.work_day_end)
    # Times this customer has already turned down, per date. Carried onto the search so a
    # suggestion can never be the window they just rejected -- which is not merely untidy, it is
    # the agent appearing not to have listened, immediately after being told no.
    already_declined = {
        option.date: list(option.excluded_windows)
        for option in order.availability_options
        if option.excluded_windows
    }

    found: list[Suggestion] = []
    for date in PlanningClock.horizon_dates():
        if date in exclude:
            continue
        # A whole working day as the search space: we are asking "where would the van naturally be
        # on this date", and the solver's answer is the promise. Narrowing it here would bias the
        # search towards our own guess about the day.
        option = AvailabilityOption(
            date=date,
            window=working_day,
            excluded_windows=already_declined.get(date, []),
            preference_rank=1,
        )
        evaluation = service.evaluate(order, option)
        if not evaluation.feasible or evaluation.promise_window is None:
            continue
        found.append(
            Suggestion(
                date=date,
                window=evaluation.promise_window,
                evaluation=evaluation,
                option=option,
            )
        )

    # Prefer a day already running, then the cheapest insertion, then the earliest date. Sorting on
    # `opens_empty_day` first is what "prefer days with an existing compatible route" means.
    # Prefer a day already running, then the least disruptive insertion, then the earliest date.
    # `operational_score` rather than driving alone: a slot that adds one driving minute and three
    # hours of waiting is not the best alternative to offer somebody, and sorting on driving said
    # it was.
    found.sort(
        key=lambda s: (
            s.evaluation.opens_empty_day,
            s.evaluation.total_score - s.evaluation.preference_penalty_minutes,
            s.date,
        )
    )
    return found[:limit]
