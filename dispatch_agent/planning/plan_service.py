"""Publishing route plans, and keeping the promises inside them.

Plans are versioned rather than overwritten, because "what did we tell this customer, and when
did that change?" has to stay answerable after the fact. `day_sequences` remains as a cache of
whichever version is currently active, so existing readers keep working -- but it is written
ONLY from here. Two writers would let the cache and the version history disagree, and the
v1-vs-v2 comparison would quietly start lying.

The load-bearing function in this module is `assert_locks_respected`. Raising an exception when
the solver *reports* infeasibility is necessary but not sufficient: a subtle regression could
just as easily produce a plausible-looking plan that quietly moves a confirmed appointment.
Checking the output before anything is persisted is what turns "we never move a confirmed
appointment" from a claim into something the system enforces.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date as Date
from pathlib import Path

from dispatch_agent.config import settings
from dispatch_agent.db import JobsRepository
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.geo.zones import COMPANY_DEPOT
from dispatch_agent.models import (
    Coordinates,
    CoordinatorException,
    DaySequence,
    JobRecord,
    PlanStatus,
    PlanningStatus,
    RoutePlanVersion,
)
from dispatch_agent.solver import LockedPlanInfeasibleError, sequence_day

# Jobs that belong on a published route. An order still being negotiated, cancelled, or whose
# goods have not arrived must not appear on a driver's day.
ROUTABLE_STATUSES = frozenset(
    {
        PlanningStatus.CONFIRMED,
        PlanningStatus.SEQUENCED,
        PlanningStatus.DISPATCHED,
    }
)


def routable_jobs(repo: JobsRepository, delivery_date: Date) -> list[JobRecord]:
    return [
        job
        for job in repo.jobs_for_date(delivery_date)
        if job.planning_status in ROUTABLE_STATUSES and job.readiness_status.value == "ready"
    ]


def content_hash(sequence: DaySequence) -> str:
    """Fingerprint of what a plan actually promises: who, in what order, at what time.

    Drive times and generation timestamps are excluded on purpose -- a re-solve that produces an
    identical customer-facing plan should not create a new version. Without this, replanning a
    day twice would show "Plan v7" on screen and undermine the very history it exists to record.
    """
    payload = [
        [s.job_id, s.sequence_index, s.arrival_window.start.isoformat(), s.arrival_window.end.isoformat()]
        for s in sequence.stops
    ]
    blob = json.dumps([sequence.delivery_date.isoformat(), payload], separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def assert_locks_respected(sequence: DaySequence, jobs_by_id: dict[str, JobRecord]) -> None:
    """Fail loudly if a published plan would move someone who was already promised a time."""
    for stop in sequence.stops:
        job = jobs_by_id.get(stop.job_id)
        if job is None or not job.is_locked:
            continue
        lock = job.locked_window
        if not (lock.start <= stop.arrival_window.start and stop.arrival_window.end <= lock.end):
            raise LockedPlanInfeasibleError(
                f"{job.customer_name} was promised {lock.start:%H:%M}-{lock.end:%H:%M} but the "
                f"plan places them at {stop.arrival_window.start:%H:%M}-{stop.arrival_window.end:%H:%M}",
                delivery_date=sequence.delivery_date,
                locked_job_ids=[job.id],
                blocking_job_id=job.id,
            )


def solve_day(
    repo: JobsRepository,
    delivery_date: Date,
    routing_client: RoutingClient | None = None,
    depot: Coordinates = COMPANY_DEPOT,
) -> DaySequence:
    """Solve a date from what is currently committed to it, verifying every promise survives."""
    jobs = routable_jobs(repo, delivery_date)
    sequence = sequence_day(
        jobs,
        delivery_date,
        depot=depot,
        routing_client=routing_client or RoutingClient(),
        time_limit_seconds=settings.solver_time_limit_seconds,
    )
    assert_locks_respected(sequence, {job.id: job for job in jobs})
    return sequence


def _tx(db_path: str | Path | None = None):
    """An autocommit connection, for the one place that needs explicit transaction control.
    db._connect() commits on exit and would fight an explicit BEGIN."""
    return sqlite3.connect(Path(db_path or settings.db_path), isolation_level=None)


def publish_plan_version(
    sequence: DaySequence,
    reason: str,
    parent_plan_id: str | None = None,
) -> RoutePlanVersion:
    """Make `sequence` the active plan for its date, superseding whatever was active.

    Returns the existing active version unchanged when the new sequence promises exactly the
    same thing, so a no-op replan does not inflate the version counter.

    The version number is computed inside the transaction. Doing it outside would let two
    concurrent publishes pick the same number and collide on UNIQUE(delivery_date, version).
    """
    digest = content_hash(sequence)
    day = sequence.delivery_date.isoformat()

    conn = _tx()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            active = conn.execute(
                "SELECT id, data FROM route_plan_versions WHERE delivery_date = ? AND status = 'active'",
                (day,),
            ).fetchone()

            if active is not None:
                existing = RoutePlanVersion.model_validate_json(active[1])
                if existing.content_hash == digest:
                    conn.execute("COMMIT")
                    return existing

            next_version = (
                conn.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM route_plan_versions WHERE delivery_date = ?",
                    (day,),
                ).fetchone()[0]
                + 1
            )

            if active is not None:
                superseded = RoutePlanVersion.model_validate_json(active[1]).model_copy(
                    update={"status": PlanStatus.SUPERSEDED}
                )
                conn.execute(
                    "UPDATE route_plan_versions SET status = ?, data = ? WHERE id = ?",
                    (superseded.status.value, superseded.model_dump_json(), active[0]),
                )

            plan = RoutePlanVersion(
                delivery_date=sequence.delivery_date,
                version=next_version,
                status=PlanStatus.ACTIVE,
                sequence=sequence,
                reason_created=reason,
                parent_plan_id=parent_plan_id or (active[0] if active else None),
                content_hash=digest,
            )
            conn.execute(
                "INSERT INTO route_plan_versions "
                "(id, delivery_date, version, status, content_hash, data) VALUES (?, ?, ?, ?, ?, ?)",
                (plan.id, day, plan.version, plan.status.value, digest, plan.model_dump_json()),
            )
            # The compatibility cache. Written only here -- see the module docstring.
            conn.execute(
                "INSERT INTO day_sequences (delivery_date, data) VALUES (?, ?) "
                "ON CONFLICT(delivery_date) DO UPDATE SET data = excluded.data",
                (day, sequence.model_dump_json()),
            )
            conn.execute("COMMIT")
            return plan
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def replan_day(
    repo: JobsRepository,
    delivery_date: Date,
    reason: str,
    routing_client: RoutingClient | None = None,
    depot: Coordinates = COMPANY_DEPOT,
) -> RoutePlanVersion:
    """Re-solve a date and publish the result. Raises rather than publishing a plan that would
    break a promise -- the caller turns that into a coordinator exception."""
    sequence = solve_day(repo, delivery_date, routing_client=routing_client, depot=depot)
    return publish_plan_version(sequence, reason=reason)


def raise_coordinator_exception(
    repo: JobsRepository,
    message: str,
    kind: str = "unresolved",
    order_id: str | None = None,
    delivery_date: Date | None = None,
) -> CoordinatorException:
    exception = CoordinatorException(
        order_id=order_id, delivery_date=delivery_date, kind=kind, message=message
    )
    repo.save_exception(exception)
    return exception
