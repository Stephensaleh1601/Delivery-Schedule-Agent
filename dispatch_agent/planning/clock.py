"""The one place "today" is decided.

Business logic must never call date.today() directly. Two reasons: a recorded demo needs the
same horizon every time it runs, and tests need to assert on horizon boundaries without their
results changing overnight. DEMO_BASE_DATE pins the clock; unset, it is the real date.
"""
from __future__ import annotations

from datetime import date as Date, timedelta

from dispatch_agent.config import settings


class PlanningClock:
    """Today, and the window of dates a customer may choose from."""

    @staticmethod
    def today() -> Date:
        if settings.demo_base_date:
            return Date.fromisoformat(settings.demo_base_date)
        return Date.today()

    @staticmethod
    def horizon(today: Date | None = None) -> tuple[Date, Date]:
        """Inclusive (first, last) bookable date.

        The lead time exists because the operation needs notice: N+2 is the earliest a new order
        can realistically be fitted, N+5 the furthest out worth planning against.
        """
        base = today or PlanningClock.today()
        return (
            base + timedelta(days=settings.horizon_lead_days_min),
            base + timedelta(days=settings.horizon_lead_days_max),
        )

    @staticmethod
    def horizon_dates(today: Date | None = None) -> list[Date]:
        first, last = PlanningClock.horizon(today)
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]

    @staticmethod
    def is_within_horizon(candidate: Date, today: Date | None = None) -> bool:
        first, last = PlanningClock.horizon(today)
        return first <= candidate <= last
