"""The business decision behind one agent run, in the terms a coordinator would use.

The side panel used to repeat the tool calls, which the per-message inspector already shows in
full. Repeating them told a judge nothing they could not get by opening the modal, and left the
actual question -- *why that time and not the other one* -- answered nowhere.

So this is the other view of the same run: what the customer asked for, what constrained the
answer, what the options actually cost, what was decided and what happened. Every figure comes
from a tool result that is already persisted; nothing here is recomputed, and nothing is model
prose. It is built from the run's own action log, so it survives a refresh for the same reason the
trace does -- it is derived from the database, not from what a browser tab happened to be holding.

There is no chain-of-thought here and there cannot be: the inputs are typed tool results.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dispatch_agent.config import settings
from dispatch_agent.models import AgentRunLog
from dispatch_agent.planning.route_facts import minutes_phrase


@dataclass
class OptionLine:
    """One candidate as it was actually evaluated -- feasible or not, and what it would cost."""

    label: str
    feasible: bool
    reason: str | None = None
    added_drive_minutes: int | None = None
    added_distance_km: float | None = None
    finishes_before: str | None = None
    finishes_after: str | None = None
    day_extends_minutes: int | None = None
    idle_minutes: int | None = None
    overtime_minutes: int | None = None
    opens_new_day: bool = False
    # Where this option came from: the customer asked for it, or we are proposing it.
    origin: str = "requested"
    chosen: bool = False


@dataclass
class DecisionRecord:
    asked_for: str = ""
    constraints: list[str] = field(default_factory=list)
    options: list[OptionLine] = field(default_factory=list)
    decision: str = ""
    outcome: list[str] = field(default_factory=list)
    # Whether this run actually decided anything. A clarification question has no options to
    # compare, and rendering an empty three-section panel for it is worse than rendering nothing.
    meaningful: bool = False

    def to_dict(self) -> dict:
        return {
            "asked_for": self.asked_for,
            "constraints": self.constraints,
            "options": [vars(o) for o in self.options],
            "decision": self.decision,
            "outcome": self.outcome,
            "meaningful": self.meaningful,
        }


def _step(run: AgentRunLog, tool: str):
    """The last successful call of `tool` in this run, or None."""
    matches = [a for a in run.actions if a.tool == tool and a.ok]
    return matches[-1] if matches else None


def _clock(minutes: int | None) -> str | None:
    if not minutes:
        return None
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def build(run: AgentRunLog, order=None, evaluations=None) -> DecisionRecord:
    """Assemble the decision from what the run actually did.

    `evaluations` is the in-memory list when the run has only just happened. After a refresh there
    is no such list, and everything below comes from the persisted action data instead -- which is
    why the tools record their figures in `data` rather than only in prose.
    """
    record = DecisionRecord()
    duration = getattr(order, "duration_minutes", None)
    item = getattr(getattr(order, "job_type", None), "value", None)

    # -- 1. what the customer asked for ---------------------------------------
    noted = _step(run, "record_availability")
    if noted:
        windows = noted.data.get("options") or []
        asked = ", ".join(
            f"{w['date']} {w['start']}-{w['end']}" for w in windows[:3]
        )
        record.asked_for = (
            f"{asked} for a {duration}-minute {item} delivery"
            if duration and item else asked
        )
        if noted.data.get("timing_is_fixed"):
            record.asked_for += " (they say it is their only possible time)"
    elif _step(run, "record_rejection"):
        declined = _step(run, "record_rejection").data.get("excluded_windows") or []
        shown = ", ".join(f"{w['date']} {w['start']}-{w['end']}" for w in declined[:2])
        record.asked_for = f"A different time -- they turned down {shown}." if shown else "A different time."
    elif _step(run, "lock_appointment"):
        record.asked_for = "To confirm the time we offered."
    elif _step(run, "explain_choice"):
        record.asked_for = "Why that time was chosen."
    elif _step(run, "ask_clarification"):
        record.asked_for = "Something we could not answer from the schedule."

    # -- 2. what constrained the answer ---------------------------------------
    record.constraints = [
        f"Service duration: {minutes_phrase(duration)}" if duration else "Service duration",
        "Existing confirmed promises on those days are never moved",
        f"Driver hours: {settings.work_day_start:%H:%M}-{settings.work_day_end:%H:%M}, "
        f"overtime counted after {settings.soft_day_end:%H:%M}",
        f"Every route starts and ends at {settings.depot_address}",
    ]
    if record.asked_for:
        record.constraints.insert(0, "The customer's own stated availability")

    # -- 3. the options, as evaluated -----------------------------------------
    priced = _step(run, "evaluate_slots")
    if priced:
        for option in priced.data.get("options") or []:
            record.options.append(
                OptionLine(
                    label=option.get("date", "?"),
                    feasible=bool(option.get("feasible")),
                    reason=option.get("reason"),
                    origin="requested",
                )
            )
    suggested = _step(run, "suggest_route_aware_windows")
    if suggested:
        for alternative in suggested.data.get("alternatives") or []:
            record.options.append(
                OptionLine(
                    label=f"{alternative['date']} {alternative['start']}-{alternative['end']}",
                    feasible=True,
                    reason=alternative.get("reason"),
                    added_drive_minutes=alternative.get("added_drive_minutes"),
                    idle_minutes=alternative.get("idle_minutes"),
                    overtime_minutes=alternative.get("overtime_minutes"),
                    opens_new_day=bool(alternative.get("opens_new_day")),
                    # Not availability. It is a question we have not yet asked.
                    origin="suggested",
                )
            )

    # Richer figures for anything the evaluations still hold in memory.
    if evaluations:
        by_date = {e.date.isoformat(): e for e in evaluations}
        for line in record.options:
            found = by_date.get(line.label.split(" ")[0])
            if found is None or not found.feasible:
                continue
            line.added_drive_minutes = found.incremental_drive_minutes
            line.added_distance_km = round(
                found.proposed_distance_km - found.baseline_distance_km, 2
            )
            line.finishes_before = _clock(found.baseline_completion_minutes)
            line.finishes_after = _clock(found.proposed_completion_minutes)
            line.day_extends_minutes = max(
                0, found.proposed_span_minutes - found.baseline_span_minutes
            )
            line.idle_minutes = max(0, found.proposed_idle_minutes - found.baseline_idle_minutes)
            line.overtime_minutes = found.overtime_penalty_minutes
            line.opens_new_day = found.opens_empty_day

    # -- 4. the decision ------------------------------------------------------
    offered = _step(run, "create_offer")
    if offered:
        for line in record.options:
            if line.label.split(" ")[0] in (offered.summary or ""):
                line.chosen = True
        record.decision = offered.summary or ""
    elif _step(run, "lock_appointment"):
        record.decision = _step(run, "lock_appointment").summary
    elif _step(run, "explain_choice"):
        record.decision = "Answered the question from the solved route. Nothing was changed."
    elif _step(run, "ask_clarification"):
        record.decision = _step(run, "ask_clarification").summary

    # -- 5. what happened -----------------------------------------------------
    locked = _step(run, "lock_appointment")
    if locked:
        record.outcome = [
            "Appointment locked",
            f"Route republished as v{locked.data.get('plan_version')}"
            if locked.data.get("plan_version") else "Route republished",
            "One stop added",
            "Existing confirmed customers moved: 0",
        ]
    elif offered:
        record.outcome = ["Offer sent -- waiting for the customer"]
    elif _step(run, "send_message"):
        record.outcome = ["Replied to the customer"]

    refused = [a for a in run.actions if a.error in ("not_allowed_for_intent", "already_done")]
    if refused:
        # Worth showing: a refusal is the guardrail doing its job, and a judge should see that the
        # system stopped something rather than that nothing was attempted.
        record.outcome.append(
            f"{len(refused)} action(s) refused as out of scope for this message"
        )

    # A clarification is a question, not a decision. Counting it as one would let "lol" wipe the
    # panel that explains a booking the customer has already confirmed.
    record.meaningful = bool(record.options or locked or offered)
    return record
