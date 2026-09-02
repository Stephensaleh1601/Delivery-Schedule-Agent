"""What a candidate delivery slot costs the operation.

The score answers one question: if we said yes to this window, how much worse does the day get?
Everything in it is minutes, so the terms are commensurable and the weights are readable as
"this is worth about an hour of driving to avoid".

Deliberately NOT in here: any notion of an impossible slot being merely expensive. Infeasibility
is a separate flag on CandidateSlotEvaluation, and the model refuses to carry a score alongside
it, so a slot that cannot be served can never be out-ranked into being chosen.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as Time

from dispatch_agent.config import settings
from dispatch_agent.models import Coordinates, DaySequence, JobRecord


@dataclass(frozen=True)
class ScoringConfig:
    day_opening_penalty_minutes: int = 60
    preference_penalty_per_rank: int = 10
    soft_day_end: Time = Time(17, 0)

    @classmethod
    def from_settings(cls) -> "ScoringConfig":
        return cls(
            day_opening_penalty_minutes=settings.day_opening_penalty_minutes,
            preference_penalty_per_rank=settings.preference_penalty_per_rank,
            soft_day_end=settings.soft_day_end,
        )


def _minutes_since_midnight(t: Time) -> int:
    return t.hour * 60 + t.minute


def overtime_minutes(
    sequence: DaySequence,
    jobs_by_id: dict[str, JobRecord],
    depot: Coordinates,
    config: ScoringConfig,
) -> int:
    """Minutes the crew works past the soft end of day, including the drive home.

    This term needs explaining, because the brief's formula includes it and a literal reading
    makes it unreachable. The solver's day end is a HARD bound: a route that would run late is
    infeasible, not expensive, so a purely hard model can never produce overtime and the term
    would always be zero.

    Measuring it against a *soft* end instead, after the solve, gives it meaning: 17:00 is when
    the day should finish, 18:00 is when it cannot go past. A slot that pushes the crew into
    that last hour is feasible but genuinely worse, and the score now says so.
    """
    if not sequence.stops:
        return 0
    # DaySequence.completion_minutes is the same arithmetic, kept in one place so the score and
    # anything the UI displays can never disagree about when the day ends.
    return max(0, sequence.completion_minutes - _minutes_since_midnight(config.soft_day_end))


def score_candidate(
    *,
    baseline_drive_minutes: int,
    proposed_drive_minutes: int,
    is_empty_day: bool,
    preference_rank: int,
    overtime: int,
    config: ScoringConfig,
) -> tuple[int, dict[str, int]]:
    """Returns (total, breakdown). The breakdown is what the UI shows a coordinator -- the total
    alone is not explainable, and an unexplainable number is one nobody trusts."""
    incremental = proposed_drive_minutes - baseline_drive_minutes
    opening = config.day_opening_penalty_minutes if is_empty_day else 0
    preference = config.preference_penalty_per_rank * max(0, preference_rank - 1)

    breakdown = {
        "incremental_drive_minutes": incremental,
        "day_opening_penalty_minutes": opening,
        "preference_penalty_minutes": preference,
        "overtime_penalty_minutes": overtime,
    }
    return incremental + opening + preference + overtime, breakdown
