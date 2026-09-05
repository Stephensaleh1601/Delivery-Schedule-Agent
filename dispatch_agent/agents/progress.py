"""What the agent is doing right now, while the customer waits.

A conversational turn takes several seconds: the model reads the message, the search measures two
routes, the model picks. A spinner over that is a lie by omission -- it says "waiting" when the
honest answer is "comparing seventeen stops". This module is what lets the screen say the honest
thing.

Three rules it exists to keep:

**Real events only.** Every stage here is written by the code that actually did the work, at the
moment it did it. Nothing is on a timer, and nothing is predicted -- a stage that never runs never
appears, and a stage that takes nine seconds shows nine seconds.

**Observable actions, not reasoning.** A stage says which tool is running and why in one plain
sentence a coordinator would use. It never carries the model's deliberation, which is not ours to
publish and is not evidence of anything.

**In memory, and deliberately.** Progress is interesting for the seconds it takes and worthless
afterwards -- the finished run is already persisted with timestamps, and that is what a reload
reads. Storing live progress durably would mean writing rows nobody will ever query again.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# How long a finished run stays readable, so a client polling just after the reply lands still
# sees the completed trace instead of an empty one.
RETAIN_SECONDS = 120


@dataclass
class Stage:
    """One observable step: what ran, why, and how long it took."""

    key: str
    label: str
    reason: str = ""
    tool: str | None = None
    state: str = "running"  # running | done | failed
    detail: str = ""
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    @property
    def seconds(self) -> float:
        return round((self.ended_at or time.time()) - self.started_at, 2)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "reason": self.reason,
            "tool": self.tool,
            "state": self.state,
            "detail": self.detail,
            "seconds": self.seconds,
        }


@dataclass
class Progress:
    order_id: str
    run_id: str | None = None
    stages: list[Stage] = field(default_factory=list)
    summary: str = ""
    state: str = "running"  # running | done | failed
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "run_id": self.run_id,
            "state": self.state,
            "summary": self.summary,
            "seconds": round((self.ended_at or time.time()) - self.started_at, 2),
            "stages": [s.to_dict() for s in self.stages],
        }


_LOCK = threading.Lock()
_LIVE: dict[str, Progress] = {}


def begin(order_id: str) -> None:
    """Start a fresh trace. Replaces any previous one -- a new message is a new answer."""
    with _LOCK:
        _prune()
        _LIVE[order_id] = Progress(order_id=order_id)


def stage(order_id: str, key: str, label: str, reason: str = "", tool: str | None = None) -> None:
    """Open a stage, closing whichever one was running.

    Idempotent on `key`: a tool that reports the same phase twice updates it rather than adding a
    second row, so a retry does not read as extra work.
    """
    with _LOCK:
        live = _LIVE.get(order_id)
        if live is None:
            return
        for existing in live.stages:
            if existing.state == "running":
                existing.state = "done"
                existing.ended_at = time.time()
        for existing in live.stages:
            if existing.key == key:
                existing.state = "running"
                existing.ended_at = None
                existing.reason = reason or existing.reason
                existing.tool = tool or existing.tool
                return
        live.stages.append(Stage(key=key, label=label, reason=reason, tool=tool))


def finish_stage(order_id: str, key: str, detail: str = "", ok: bool = True) -> None:
    with _LOCK:
        live = _LIVE.get(order_id)
        if live is None:
            return
        for existing in live.stages:
            if existing.key == key:
                existing.state = "done" if ok else "failed"
                existing.ended_at = time.time()
                if detail:
                    existing.detail = detail
                return


def complete(order_id: str, summary: str = "", run_id: str | None = None, ok: bool = True) -> None:
    with _LOCK:
        live = _LIVE.get(order_id)
        if live is None:
            return
        for existing in live.stages:
            if existing.state == "running":
                existing.state = "done" if ok else "failed"
                existing.ended_at = time.time()
        live.state = "done" if ok else "failed"
        live.summary = summary
        live.run_id = run_id or live.run_id
        live.ended_at = time.time()


def read(order_id: str) -> dict | None:
    with _LOCK:
        live = _LIVE.get(order_id)
        return live.to_dict() if live else None


def _prune() -> None:
    """Drop traces nobody is watching any more. Called under the lock."""
    cutoff = time.time() - RETAIN_SECONDS
    for key in [k for k, v in _LIVE.items() if (v.ended_at or v.started_at) < cutoff]:
        _LIVE.pop(key, None)


# What each tool is doing, in words a coordinator would use. Keyed by tool name so a stage label
# cannot drift from the action that produced it.
TOOL_STAGES: dict[str, tuple[str, str]] = {
    "record_availability": ("Noting what you told us", "Writing down the times you gave"),
    "record_rejection": ("Noting what you turned down", "Excluding that time, not the whole day"),
    "retrieve_policy": ("Reading delivery policy", "Checking the rules before touching a route"),
    "get_existing_routes": (
        "Loading Friday and Saturday routes",
        "Only routes already published can take a new stop",
    ),
    "find_insertion_options": (
        "Searching the routes",
        "Measuring where you could be fitted in",
    ),
    "create_normal_offer": ("Choosing what to offer", "Putting the proven option to you"),
    "create_alternative_offer": ("Choosing what to offer", "Putting the calculated top three to you"),
    "explain_choice": ("Explaining the choice", "Answering from the measured insertion"),
    "ask_clarification": ("Asking a question", "One specific question rather than a guess"),
    "create_exception": ("Handing over to a coordinator", "A person can do what the rules cannot"),
    "send_message": ("Preparing the reply", "Sending the wording the previous step produced"),
    "lock_appointment": ("Confirming your booking", "Locking the window and republishing the day"),
    "finish": ("Done", ""),
}
