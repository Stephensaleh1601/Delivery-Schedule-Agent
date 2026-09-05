"""The one place "today" is decided, and the Friday/Saturday pair we coordinate.

Business logic must never call date.today() directly. Two reasons: a recorded demo needs the
same horizon every time it runs, and tests need to assert on horizon boundaries without their
results changing overnight. DEMO_BASE_DATE pins the clock; unset, it is the real date.

**A cycle is a pair, not two dates.** Deliveries run on Friday and Saturday, and the two regional
clusters between them cover the whole island -- so the pair is the unit of coordination. The
temptation is to resolve each day independently ("the next published Friday", "the next published
Saturday"), and that is wrong: with an unpublished Friday it yields this week's Saturday beside
next week's Friday. The fallback search would then compare two routes the driver never runs in
the same week, and "we'll already be nearby on the other day" stops being true. So both dates
advance together, or neither does.

"No complete pair" is a real answer rather than a degenerate one. The caller escalates to a
coordinator; it does not proceed on half a cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date, timedelta
from typing import Iterator, TYPE_CHECKING

from dispatch_agent.config import settings

if TYPE_CHECKING:  # pragma: no cover -- import cycle: db imports models, models import nothing here
    from dispatch_agent.db import JobsRepository

FRIDAY = 4  # date.weekday(): Monday is 0
SATURDAY = 5


@dataclass(frozen=True)
class CoordinationCycle:
    """One week's delivery pair. Both dates are published and both clear the notice period."""

    friday: Date
    saturday: Date

    @property
    def dates(self) -> list[Date]:
        return [self.friday, self.saturday]

    def cluster_day(self, weekday: int) -> Date | None:
        return {FRIDAY: self.friday, SATURDAY: self.saturday}.get(weekday)


class PlanningClock:
    """Today, and the pair of dates a customer may be offered."""

    @staticmethod
    def today() -> Date:
        if settings.demo_base_date:
            return Date.fromisoformat(settings.demo_base_date)
        return Date.today()

    @staticmethod
    def candidate_pairs(today: Date | None = None) -> Iterator[tuple[Date, Date]]:
        """Every same-week (Friday, Saturday) pair far enough out to be bookable, soonest first.

        Pure date arithmetic -- no repository, so it stays cheap and deterministic. A pair is
        yielded only if BOTH dates clear the notice period, which is what stops a Thursday
        enquiry from keeping a Saturday whose own Friday is already too close to book.
        """
        base = today or PlanningClock.today()
        earliest = base + timedelta(days=settings.horizon_lead_days_min)

        # The Friday of base's own week; the loop below skips it if it is already too close.
        friday = base - timedelta(days=base.weekday()) + timedelta(days=FRIDAY)
        for _ in range(settings.cycle_search_weeks):
            saturday = friday + timedelta(days=1)
            if friday >= earliest and saturday >= earliest:
                yield (friday, saturday)
            friday += timedelta(days=7)

    @staticmethod
    def current_pair(today: Date | None = None) -> tuple[Date, Date]:
        """The soonest bookable pair, by date arithmetic alone -- ignoring whether it is published.

        This is what "which dates are we coordinating" means, and it is deliberately NOT the same
        question as `coordination_cycle`. Publication cannot be a precondition of bookability:
        a route is solved from the jobs assigned to its date, and a job is assigned by accepting
        an offer for that date, so requiring a published route before a date can be offered makes
        the first booking of any cycle impossible. Publication gates *insertion*, which is a
        later step with real routes to insert into.
        """
        for pair in PlanningClock.candidate_pairs(today):
            return pair
        base = today or PlanningClock.today()  # pragma: no cover -- only with a zero search bound
        return (base, base)

    @staticmethod
    def coordination_cycle(
        repo: "JobsRepository | None" = None, today: Date | None = None
    ) -> CoordinationCycle | None:
        """The soonest bookable pair where BOTH dates already have an active published route.

        Returns None when no such pair exists inside the search bound. That is
        `no_coordination_cycle`: the caller tells the customer a coordinator will be in touch
        rather than proceeding on half a cycle.
        """
        from dispatch_agent.db import JobsRepository

        repo = repo or JobsRepository()
        for friday, saturday in PlanningClock.candidate_pairs(today):
            if repo.active_plan(friday) and repo.active_plan(saturday):
                return CoordinationCycle(friday=friday, saturday=saturday)
        return None

    @staticmethod
    def horizon(today: Date | None = None) -> tuple[Date, Date]:
        """(first, last) of the pair being coordinated -- the Friday and the Saturday."""
        return PlanningClock.current_pair(today)

    @staticmethod
    def horizon_dates(today: Date | None = None) -> list[Date]:
        """The dates a customer may be offered: exactly this cycle's Friday and Saturday."""
        friday, saturday = PlanningClock.current_pair(today)
        return [friday, saturday]

    @staticmethod
    def is_within_horizon(candidate: Date, today: Date | None = None) -> bool:
        return candidate in PlanningClock.horizon_dates(today)
