"""The agent's decision, in the shape a judge can read in five seconds.

Rewritten from a panel that was correct and unreadable. It listed constraints, depot configuration,
service durations and before/after tables, and a judge watching the conversation had to work out
for themselves why 9-11 had become 11-1. It looked like the optimiser had moved the customer at
random.

What actually happened was a sequence: the customer rejected 9-11, that window was removed, Saturday
was re-solved, 11-1 came back. Four steps, none of them arbitrary. So the panel now leads with that
sequence, then shows at most two candidates -- the one that matches what the customer asked for, and
the one that is easiest on the route -- and ends with a single recommendation.

Everything is derived from persisted tool results. There is no chain-of-thought here and there
cannot be: the inputs are typed results, not model prose.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dispatch_agent.config import settings
from dispatch_agent.models import AgentRunLog
from dispatch_agent.planning.route_facts import minutes_phrase

# Two candidates. A judge comparing five is not comparing anything.
MAX_CANDIDATES = 3

# Below this, two windows are the same answer twice and only one is worth showing.
SAME_IMPACT_MINUTES = 3


@dataclass
class Step:
    """One link in "what changed" -- rejected, removed, re-solved, found."""

    text: str
    tone: str = "neutral"  # "removed" | "solved" | "found"


@dataclass
class Candidate:
    """One option, with the consequences of choosing it."""

    label: str
    # "customer" -- matches what they asked for. "route" -- easiest on the operation.
    kind: str = "customer"
    badge: str = ""
    explanation: str = ""
    added_drive_minutes: int | None = None
    added_distance_km: float | None = None
    finishes_later_minutes: int | None = None
    idle_minutes: int | None = None
    overtime_minutes: int | None = None
    promises_moved: int = 0
    opens_new_day: bool = False
    feasible: bool = True
    # Where it sits in the solved day, for the route strip.
    insertion: str | None = None
    stops_before: int | None = None
    position: int | None = None
    chosen: bool = False
    # 24-hour start, for matching against the live offer without re-parsing prose.
    start: str = ""
    # Whether this window was actually put to the customer. A candidate can be worth comparing and
    # still not be offered -- when the time they asked for works and is not materially worse, we
    # honour it rather than negotiate. Saying "offer both" when one was offered is a lie the panel
    # tells confidently.
    offered: bool = True
    date: str = ""
    window: str = ""


@dataclass
class DecisionRecord:
    """What the panel renders. `heading` changes with the event, because "Why these times?" and
    "Why the offer changed" are different questions and one panel answering both answers neither."""

    heading: str = "Agent decision"
    asked: str = ""
    what_changed: str = ""
    steps: list[Step] = field(default_factory=list)
    # What the search actually looked at, in counts. Read straight off the persisted tool result
    # rather than narrated, so "compared 16 stops" is a fact a judge can check against the routes
    # and not a sentence the model composed about its own diligence.
    evidence: list[Step] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    decision: str = ""
    outcome: list[str] = field(default_factory=list)
    # Collapsed by default: true, and worth having, and not what anyone needs first.
    planning_rules: list[str] = field(default_factory=list)
    meaningful: bool = False

    def to_dict(self) -> dict:
        return {
            "heading": self.heading,
            "asked": self.asked,
            "what_changed": self.what_changed,
            "steps": [vars(s) for s in self.steps],
            "evidence": [vars(e) for e in self.evidence],
            "candidates": [vars(c) for c in self.candidates],
            "decision": self.decision,
            "outcome": self.outcome,
            "planning_rules": self.planning_rules,
            "meaningful": self.meaningful,
        }


def _step(run: AgentRunLog, tool: str):
    matches = [a for a in run.actions if a.tool == tool and a.ok]
    return matches[-1] if matches else None


def _window_label(date: str, start: str, end: str) -> str:
    from datetime import date as Date

    try:
        day = Date.fromisoformat(date).strftime("%A")
    except ValueError:
        day = date
    return f"{day} {_clock(start)}–{_clock(end)}"


def _clock(hhmm: str) -> str:
    """"13:00" -> "1pm", "11:30" -> "11:30am"."""
    try:
        hour, minute = (int(p) for p in hhmm.split(":"))
    except (ValueError, AttributeError):
        return hhmm
    suffix = "am" if hour < 12 else "pm"
    shown = hour % 12 or 12
    return f"{shown}:{minute:02d}{suffix}" if minute else f"{shown}{suffix}"


def build(
    run: AgentRunLog, order=None, evaluations=None, suggestions=None, on_the_table=None,
    offer=None,
) -> DecisionRecord:
    """Assemble the decision from what the run actually did.

    `on_the_table` is the set of (date, start) pairs the customer can currently accept. Passed in
    rather than inferred, because a run that made no offer -- an explanation, say -- has no way to
    know what is open, and defaulting to "offered" labelled a compared-only alternative as though
    it had been put to the customer.
    """
    record = DecisionRecord()
    intent = _intent_of(run)

    record.heading = {
        "provide_availability": "Why these times?",
        "reject": "Why the offer changed",
        "explain": "Why this time?",
        "accept": "What the agent changed",
    }.get(intent, "Agent decision")

    record.planning_rules = _planning_rules(order)

    if intent == "accept":
        _confirmation(record, run, order)
        return record

    _what_the_customer_asked(record, run, intent)
    # Candidates first: the last step of "what changed" names the window that was found, and
    # reading it off the candidate is exact where scraping it out of a summary string was not.
    record.evidence = _evidence(run)
    # An offer built from the insertion search carries its own evidence -- the anchor, the
    # position, the detour -- measured against the published route. Preferred over the
    # evaluation-derived cards, which describe a whole re-solved day and cannot say where in
    # the route the customer would go.
    record.candidates = _candidates_from_offer(offer) or _candidates(
        run, evaluations, suggestions, on_the_table
    )
    _what_changed(record, run, intent)
    _decide(record, run, intent)

    record.meaningful = bool(
        record.candidates or record.outcome or record.what_changed or record.evidence
    )
    return record


def _intent_of(run: AgentRunLog) -> str:
    tools = [a.tool for a in run.actions]
    if "lock_appointment" in tools:
        return "accept"
    if "record_rejection" in tools:
        return "reject"
    if "explain_choice" in tools:
        return "explain"
    if "record_availability" in tools:
        return "provide_availability"
    return "other"


def _planning_rules(order) -> list[str]:
    """True, and collapsed. Nobody opens a panel to be told what a working day is."""
    duration = getattr(order, "duration_minutes", None)
    rules = [
        "Confirmed promises are never moved to fit a new booking",
        f"Driver hours {settings.work_day_start:%H:%M}–{settings.arrival_cutoff:%H:%M}, "
        f"overtime counted after {settings.soft_day_end:%H:%M}",
        f"Every route starts and ends at {settings.depot_address}",
    ]
    if duration:
        rules.insert(0, f"This delivery needs {minutes_phrase(duration)} on site")
    return rules


def _what_the_customer_asked(record: DecisionRecord, run: AgentRunLog, intent: str) -> None:
    noted = _step(run, "record_availability")
    if noted:
        windows = noted.data.get("options") or []
        if windows:
            first = windows[0]
            record.asked = _window_label(first["date"], first["start"], first["end"])
            if len(windows) > 1:
                record.asked += f" (and {len(windows) - 1} other)"
        if noted.data.get("timing_is_fixed"):
            record.asked += " — their only possible time"
    elif intent == "reject":
        record.asked = "A different time"
    elif intent == "explain":
        record.asked = "Why that time was chosen"


def _what_changed(record: DecisionRecord, run: AgentRunLog, intent: str) -> None:
    """The sequence that makes a shifted window look deliberate instead of random."""
    declined = _step(run, "record_rejection")
    if declined:
        windows = declined.data.get("excluded_windows") or []
        if windows:
            w = windows[0]
            label = _window_label(w["date"], w["start"], w["end"])
            day = label.split(" ")[0]
            record.what_changed = (
                f"{label} was rejected and removed. The rest of {day} stayed available."
            )
            record.steps = [
                Step(f"{label} rejected", "removed"),
                Step("Removed from their availability", "removed"),
                Step(f"{day} re-solved around the gap", "solved"),
            ]
            replacement = next(
                (c for c in record.candidates if c.label.startswith(day)), None
            )
            record.steps.append(
                Step(
                    f"{replacement.label} found" if replacement else "Next workable time found",
                    "found",
                )
            )
        return

    if intent == "explain":
        record.what_changed = "Nothing changed — this was a question about the offer on the table."
        record.steps = [
            Step("Offer still active", "found"),
            Step("No appointment rejected", "found"),
            Step("No route republished", "found"),
        ]
        return

    priced = _step(run, "evaluate_slots")
    if priced:
        options = priced.data.get("options") or []
        workable = sum(1 for o in options if o.get("feasible"))
        record.what_changed = (
            f"Solved the customer's requested time against the real routes: "
            f"{workable} of {len(options)} can be delivered."
        )


def _candidates(run: AgentRunLog, evaluations, suggestions, on_the_table=None) -> list[Candidate]:
    """At most two, and never the same answer twice.

    The panel used to list every evaluated window, including the customer's broad availability
    rendered as though it were a candidate in its own right -- so "Saturday 9am-6pm" sat beside
    "Saturday 11am-1pm" as if they were alternatives. Only windows we would actually offer, or
    would actually propose, belong here.
    """
    # An explanation makes no offer, and still has to compare the options it is explaining --
    # "Tuesday adds a minute, Saturday adds sixteen" is the answer to "why Tuesday?", and the
    # generic "explained from the solved route" is not.
    if not _step(run, "create_offer") and not _step(run, "explain_choice"):
        return []

    found: list[Candidate] = []
    by_key = {}
    for evaluation in evaluations or []:
        if evaluation.feasible and evaluation.promise_window:
            by_key[(evaluation.date.isoformat(),
                    evaluation.promise_window.start.strftime("%H:%M"))] = evaluation

    # The customer's own request first, then the route-friendly alternative.
    requested = [e for e in (evaluations or []) if e.feasible and e.promise_window]
    for evaluation in requested[:1]:
        found.append(_candidate_from(evaluation, kind="customer"))

    for suggestion in (suggestions or []):
        evaluation = suggestion.evaluation
        if not evaluation.feasible or not evaluation.promise_window:
            continue
        key = (evaluation.date.isoformat(), evaluation.promise_window.start.strftime("%H:%M"))
        if any(c.date == key[0] and c.window.startswith(_clock(key[1])) for c in found):
            continue
        found.append(_candidate_from(evaluation, kind="route"))
        if len(found) >= MAX_CANDIDATES:
            break

    # Two options that cost the same are one option shown twice.
    if len(found) == 2:
        a, b = found
        same_impact = (
            abs((a.added_drive_minutes or 0) - (b.added_drive_minutes or 0)) < SAME_IMPACT_MINUTES
            and abs((a.finishes_later_minutes or 0) - (b.finishes_later_minutes or 0)) < 30
        )
        if same_impact and a.date == b.date:
            found = found[:1]

    _label(found)
    offered = _step(run, "create_offer")
    counteroffered = bool(offered and offered.data.get("counteroffered"))
    for candidate in found:
        if offered and candidate.date in (offered.summary or ""):
            candidate.chosen = True
        if on_the_table is not None:
            # What the customer can actually accept right now, from the live offer.
            candidate.offered = (candidate.date, candidate.start) in on_the_table
        elif candidate.kind == "route" and offered and not counteroffered:
            # Alternatives only reach the customer when the counteroffer policy says they should.
            candidate.offered = False
    return found[:MAX_CANDIDATES]


def _candidate_from(evaluation, kind: str) -> Candidate:
    window = evaluation.promise_window
    return Candidate(
        label=_window_label(
            evaluation.date.isoformat(),
            window.start.strftime("%H:%M"),
            window.end.strftime("%H:%M"),
        ),
        kind=kind,
        date=evaluation.date.isoformat(),
        window=f"{_clock(window.start.strftime('%H:%M'))}–{_clock(window.end.strftime('%H:%M'))}",
        start=window.start.strftime("%H:%M"),
        added_drive_minutes=evaluation.incremental_drive_minutes,
        added_distance_km=round(
            evaluation.proposed_distance_km - evaluation.baseline_distance_km, 1
        ),
        finishes_later_minutes=max(
            0, evaluation.proposed_completion_minutes - evaluation.baseline_completion_minutes
        ) if evaluation.baseline_completion_minutes else None,
        idle_minutes=max(0, evaluation.proposed_idle_minutes - evaluation.baseline_idle_minutes),
        overtime_minutes=evaluation.overtime_penalty_minutes,
        opens_new_day=evaluation.opens_empty_day,
        position=evaluation.route_position or None,
        stops_before=evaluation.baseline_stop_count,
        insertion=_insertion(evaluation),
        feasible=True,
    )


def _insertion(evaluation) -> str | None:
    """Where in the solved day this stop lands, in plain words."""
    if not evaluation.route_position or not evaluation.route_stop_count:
        return None
    if evaluation.route_position == evaluation.route_stop_count:
        return f"Inserted after stop {evaluation.route_position - 1} and before the return journey."
    if evaluation.route_position == 1:
        return "Inserted as the first stop of the day."
    return (
        f"Inserted between stop {evaluation.route_position - 1} and stop "
        f"{evaluation.route_position} of {evaluation.route_stop_count}."
    )


def _label(candidates: list[Candidate]) -> None:
    """Badges and one-line explanations, from the numbers rather than from adjectives."""
    if not candidates:
        return

    def impact_key(candidate: Candidate) -> tuple[float, float]:
        drive = (
            float(candidate.added_drive_minutes)
            if candidate.added_drive_minutes is not None
            else float("inf")
        )
        distance = (
            float(candidate.added_distance_km)
            if candidate.added_distance_km is not None
            else float("inf")
        )
        return drive, distance

    lowest = min(candidates, key=impact_key)
    for candidate in candidates:
        if candidate.kind == "customer":
            candidate.badge = "Customer-friendly"
            candidate.explanation = "This is what the customer asked for."
        else:
            candidate.badge = "Route alternative"
            candidate.explanation = "This option also fits the existing routes."

    if len(candidates) == 2:
        other = next(c for c in candidates if c is not lowest)
        drive_saving = (other.added_drive_minutes or 0) - (lowest.added_drive_minutes or 0)
        distance_saving = (other.added_distance_km or 0) - (lowest.added_distance_km or 0)

        lowest.badge = "Lowest route impact"
        if drive_saving > 0:
            lowest.explanation = (
                f"Adds {minutes_phrase(drive_saving)} less driving than {other.label}."
            )
        elif distance_saving > 0:
            lowest.explanation = (
                f"Ties on driving time and adds {distance_saving:.1f} km less than {other.label}."
            )
        else:
            lowest.explanation = "Tied for the lowest driving impact."

        # A route can trade a little more driving for avoiding overtime. Name that trade-off
        # instead of falsely calling both cards "lowest" or saying the slower option adds less.
        if other.overtime_minutes == 0 and (lowest.overtime_minutes or 0) > 0:
            other.badge = "Avoids overtime"
            tradeoff = f"Avoids {minutes_phrase(lowest.overtime_minutes or 0)} of overtime"
            if drive_saving > 0:
                tradeoff += f", but adds {minutes_phrase(drive_saving)} more driving"
            if distance_saving > 0:
                tradeoff += f" and {distance_saving:.1f} km more"
            other.explanation = tradeoff + f" than {lowest.label}."
        elif other.kind == "route" and drive_saving > 0:
            other.explanation = (
                f"Adds {minutes_phrase(drive_saving)} more driving than {lowest.label}."
            )


def _decide(record: DecisionRecord, run: AgentRunLog, intent: str) -> None:
    offered = _step(run, "create_offer")
    explained = _step(run, "explain_choice")

    if explained and not offered:
        # A question, answered. Nothing to decide.
        record.decision = _explanation_sentence(record.candidates, explained)
        record.outcome = ["Offer unchanged", "Nothing rejected", "No route published"]
        record.meaningful = True
        return

    if not offered:
        asked = _step(run, "ask_clarification")
        if asked:
            record.decision = asked.summary
        return

    if len(record.candidates) == 2:
        route = next((c for c in record.candidates if c.kind == "route"), None)
        customer = next((c for c in record.candidates if c.kind == "customer"), None)
        if route and customer and route.offered:
            lowest = min(
                record.candidates,
                key=lambda c: (
                    c.added_drive_minutes if c.added_drive_minutes is not None else float("inf"),
                    c.added_distance_km if c.added_distance_km is not None else float("inf"),
                ),
            )
            other = next(c for c in record.candidates if c is not lowest)
            if (lowest.overtime_minutes or 0) > (other.overtime_minutes or 0):
                record.decision = (
                    f"Offer both. {lowest.label} adds the least driving; {other.label} avoids "
                    f"{minutes_phrase((lowest.overtime_minutes or 0) - (other.overtime_minutes or 0))} "
                    "of overtime."
                )
            else:
                record.decision = (
                    f"Offer both. Recommend {lowest.label} for the lowest route impact; "
                    f"{other.label} remains available."
                )
        elif route and customer:
            # We looked, and the alternative was not enough better to be worth asking about. The
            # customer asked for a time we can serve, so we serve it.
            reason = (offered.data or {}).get("counteroffer_reason", "")
            drive_difference = (route.added_drive_minutes or 0) - (
                customer.added_drive_minutes or 0
            )
            if drive_difference >= 0:
                record.decision = (
                    f"Offer {customer.label}. {route.label} was checked, but it adds "
                    f"{minutes_phrase(drive_difference)} more driving."
                )
            else:
                record.decision = (
                    f"Offer {customer.label}. {route.label} was checked and is not enough better to "
                    f"be worth asking them to move"
                    + (f" ({reason.replace('_', ' ')})." if reason and reason != "honour_request" else ".")
                )
    elif record.candidates:
        only = record.candidates[0]
        record.decision = f"Offer {only.label} — {only.explanation[0].lower()}{only.explanation[1:]}"

    record.outcome = ["Offer sent — waiting for the customer"]
    refused = [a for a in run.actions if a.error in ("not_allowed_for_intent", "already_done")]
    if refused:
        record.outcome.append(f"{len(refused)} action(s) refused as out of scope")


def _explanation_sentence(candidates: list[Candidate], explained) -> str:
    """The comparison, in the customer's terms. Numbers from the solve, never an adjective."""
    if len(candidates) == 2:
        cheapest = min(candidates, key=lambda c: c.added_drive_minutes or 0)
        other = next(c for c in candidates if c is not cheapest)
        because = (
            " because the van is already nearby at that time"
            if (cheapest.added_drive_minutes or 0) <= 5 and not cheapest.opens_new_day
            else ""
        )
        return (
            f"{cheapest.label} adds {minutes_phrase(cheapest.added_drive_minutes or 0)} of "
            f"driving{because}. {other.label} adds "
            f"{minutes_phrase(other.added_drive_minutes or 0)}."
        )
    if candidates:
        only = candidates[0]
        return (
            f"{only.label} adds {minutes_phrase(only.added_drive_minutes or 0)} of driving to "
            f"that day's route."
        )
    return explained.summary or "Answered from the solved route."


def _confirmation(record: DecisionRecord, run: AgentRunLog, order=None) -> None:
    locked = _step(run, "lock_appointment")
    if not locked:
        return
    version = locked.data.get("plan_version")
    when = locked.data.get("delivery_date", "")
    # The window they chose, in words. "Customer selected 2026-09-08" is a database row, not an
    # answer -- and it is the one line of this panel a judge reads out loud.
    slot = when
    if order is not None and getattr(order, "locked_window", None) and when:
        slot = _window_label(
            when,
            order.locked_window.start.strftime("%H:%M"),
            order.locked_window.end.strftime("%H:%M"),
        )
    record.asked = "To confirm the time we offered"
    record.what_changed = f"Customer selected {slot}." if slot else "Customer confirmed."
    record.decision = locked.summary
    record.outcome = [
        "Appointment locked",
        f"Route v{version - 1} → v{version}" if version and version > 1 else "Route published",
        "One stop inserted",
        "Existing promises moved: 0",
    ]
    record.meaningful = True


def _evidence(run: AgentRunLog) -> list[Step]:
    """The judge-facing rows: what was read, what was compared, what was thrown away and why.

    Every number comes from `AgentActionLog.data` -- the payload the tool persisted -- so this
    cannot report work that did not happen. Where a count is zero the row is left out rather than
    printed as "rejected 0", which reads as an absence of rigour rather than an absence of
    problems.
    """
    rows: list[Step] = []

    policy = next((a for a in run.actions if a.tool == "retrieve_policy" and a.ok), None)
    if policy:
        ids = ", ".join(policy.data.get("policy_ids", [])[:3])
        rows.append(Step(f"Read the delivery policy{f' ({ids})' if ids else ''}", "neutral"))

    routes = next((a for a in run.actions if a.tool == "get_existing_routes" and a.ok), None)
    if routes:
        count = len(routes.data.get("routes", []))
        rows.append(Step(f"Loaded {count} published route{'s' if count != 1 else ''}", "neutral"))

    search = next((a for a in run.actions if a.tool == "find_insertion_options"), None)
    if search is None:
        return rows

    data = search.data or {}
    radius = data.get("anchor_radius_km")
    if data.get("stops_checked"):
        rows.append(Step(f"Compared the customer with {data['stops_checked']} stops", "neutral"))
    if data.get("anchors_within_radius"):
        rows.append(
            Step(f"Found {data['anchors_within_radius']} within {radius}km", "found")
        )
    if data.get("positions_tested"):
        rows.append(
            Step(f"Tested {data['positions_tested']} positions, before and after each", "neutral")
        )
    if data.get("rejected_would_delay"):
        rows.append(
            Step(
                f"Rejected {data['rejected_would_delay']} that would have made someone late",
                "removed",
            )
        )
    if data.get("rejected_outside_windows"):
        rows.append(
            Step(
                f"Rejected {data['rejected_outside_windows']} arriving outside every window",
                "removed",
            )
        )
    if data.get("excluded_by_customer"):
        rows.append(
            Step(f"Dropped {data['excluded_by_customer']} the customer had ruled out", "removed")
        )
    if data.get("valid_count"):
        rows.append(Step(f"Produced {data['valid_count']} valid choices", "solved"))

    offered = next(
        (a for a in run.actions
         if a.tool in ("create_normal_offer", "create_alternative_offer") and a.ok),
        None,
    )
    if offered:
        count = len(offered.data.get("slots", []))
        rows.append(Step(f"Offered the calculated top {count}", "solved"))

    return rows


def _candidates_from_offer(offer) -> list[Candidate]:
    """One card per offered slot, from the evidence stored on it.

    Read off the offer rather than recomputed, so what the panel shows and what the customer was
    sent cannot drift apart -- and so the figures survive a page refresh without re-running a
    search whose inputs may have moved.
    """
    if offer is None:
        return []
    slots = [s for s in offer.options if s.evidence is not None]
    if not slots:
        return []

    cheapest = min(s.evidence.added_distance_km for s in slots)
    cards: list[Candidate] = []
    for slot in slots[:MAX_CANDIDATES]:
        e = slot.evidence
        later = max(
            0,
            (e.finish_after.hour * 60 + e.finish_after.minute)
            - (e.finish_before.hour * 60 + e.finish_before.minute),
        )
        cards.append(
            Candidate(
                label=_window_label(
                    slot.date.isoformat(),
                    slot.window.start.strftime("%H:%M"),
                    slot.window.end.strftime("%H:%M"),
                ),
                kind="route",
                badge="Lowest impact" if e.added_distance_km == cheapest else "",
                explanation=slot.reason or "",
                added_drive_minutes=e.added_minutes,
                added_distance_km=e.added_distance_km,
                finishes_later_minutes=later,
                # Nobody is moved by an insertion -- the search rejects any position that would
                # make an existing stop late, so a returned option always has zero here.
                promises_moved=0,
                opens_new_day=False,
                feasible=True,
                # Positions, not names: this panel sits beside the customer's own thread.
                insertion=(
                    f"{e.placement.title()} stop {e.anchor_stop_number}, "
                    f"{e.anchor_distance_km}km away — new stop {e.insert_position} on the day"
                ),
                stops_before=e.insert_position - 1,
                position=e.insert_position,
                chosen=e.added_distance_km == cheapest,
                start=slot.window.start.strftime("%H:%M"),
                offered=True,
                date=slot.date.isoformat(),
                window=(
                    f"{_clock(slot.window.start.strftime('%H:%M'))}–"
                    f"{_clock(slot.window.end.strftime('%H:%M'))}"
                ),
            )
        )
    return cards
