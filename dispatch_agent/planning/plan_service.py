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
from dispatch_agent.db import JobsRepository, current_connection
from dispatch_agent.geo.routing_client import RoutingClient
from dispatch_agent.geo.zones import company_depot
from dispatch_agent.models import (
    Coordinates,
    CoordinatorException,
    DaySequence,
    JobRecord,
    PlanStatus,
    PlanningStatus,
    RoutePlanVersion,
    StopAssignment,
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


def promise_kept(job: JobRecord, stop: StopAssignment) -> bool:
    """Whether this stop still honours what the customer was promised.

    The one definition of that question. It was written out three times -- here, in the
    coordinator metric, and in the tests -- and three copies of a predicate is three chances for
    the published plan, the headline number and the test suite to disagree about whether anybody
    was moved.

    Measured on the ARRIVAL. A locked window says the van turns up between 5 and 9; it does not
    say the delivery is finished by 9, and solver._normalised_windows deliberately stops reserving
    the service duration inside a locked window for exactly that reason.
    """
    lock = job.locked_window
    return bool(lock) and lock.start <= stop.arrival_window.start <= lock.end


def assert_locks_respected(sequence: DaySequence, jobs_by_id: dict[str, JobRecord]) -> None:
    """Fail loudly if a published plan would move someone who was already promised a time.

    Checked on the ARRIVAL, because that is what a locked window promises: we said the van would
    turn up between 5 and 9, not that it would be finished by 9. Requiring the whole visit inside
    the lock re-imposes the service reservation that solver._normalised_windows deliberately drops
    for locked jobs -- which does not merely reject a few plans, it rejects them at publish time,
    after the customer has already accepted, surfacing as "that slot was taken while we were
    confirming" for a slot nobody took.
    """
    for stop in sequence.stops:
        job = jobs_by_id.get(stop.job_id)
        if job is None or not job.is_locked:
            continue
        lock = job.locked_window
        if not promise_kept(job, stop):
            raise LockedPlanInfeasibleError(
                f"{job.customer_name} was promised arrival between {lock.start:%H:%M} and "
                f"{lock.end:%H:%M} but the plan has the van reaching them at "
                f"{stop.arrival_window.start:%H:%M}",
                delivery_date=sequence.delivery_date,
                locked_job_ids=[job.id],
                blocking_job_id=job.id,
            )


def annotate_distances(
    sequence: DaySequence,
    jobs_by_id: dict[str, JobRecord],
    routing_client: RoutingClient,
    depot: Coordinates | None = None,
) -> DaySequence:
    """Attach per-leg distance to a solved sequence, including the drive home.

    Done here rather than in the solver because candidate evaluation deliberately hands the solver
    a precomputed drive-time matrix and has no routing client to ask -- so `_extract` structurally
    cannot make this call. `solve_day` is the single funnel every *published* sequence passes
    through, and the solve has just warmed the cache for exactly these points, so this costs no
    provider requests.

    The trailing depot is deliberate: /api/route-plan's own distance total omitted the return leg,
    which made a distant final stop look free.
    """
    depot = depot or company_depot()
    if not sequence.stops:
        return sequence.model_copy(update={"distance_recorded": True})

    points = (
        [depot]
        + [jobs_by_id[s.job_id].address.coordinates for s in sequence.stops]
        + [depot]
    )
    legs = routing_client.leg_distances(points)
    return sequence.model_copy(
        update={
            "stops": [
                stop.model_copy(update={"distance_km_from_prev": legs[i]["km"]})
                for i, stop in enumerate(sequence.stops)
            ],
            "return_distance_km": legs[-1]["km"],
            "distance_recorded": True,
        }
    )


def solve_day(
    repo: JobsRepository,
    delivery_date: Date,
    routing_client: RoutingClient | None = None,
    depot: Coordinates | None = None,
) -> DaySequence:
    """Solve a date from what is currently committed to it, verifying every promise survives."""
    jobs = routable_jobs(repo, delivery_date)
    depot = depot or company_depot()
    client = routing_client or RoutingClient()
    sequence = sequence_day(
        jobs,
        delivery_date,
        depot=depot,
        routing_client=client,
        time_limit_seconds=settings.solver_time_limit_seconds,
    )
    jobs_by_id = {job.id: job for job in jobs}
    sequence = annotate_distances(sequence, jobs_by_id, client, depot)
    assert_locks_respected(sequence, jobs_by_id)
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

    conn = current_connection()
    owns_transaction = conn is None
    if owns_transaction:
        conn = _tx()
    try:
        if owns_transaction:
            conn.execute("BEGIN IMMEDIATE")
        try:
            active = conn.execute(
                "SELECT id, data FROM route_plan_versions WHERE delivery_date = ? AND status = 'active'",
                (day,),
            ).fetchone()

            if active is not None:
                existing = RoutePlanVersion.model_validate_json(active[1])
                if existing.content_hash == digest:
                    if owns_transaction:
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
            if owns_transaction:
                conn.execute("COMMIT")
            return plan
        except Exception:
            if owns_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        if owns_transaction:
            conn.close()


def replan_day(
    repo: JobsRepository,
    delivery_date: Date,
    reason: str,
    routing_client: RoutingClient | None = None,
    depot: Coordinates | None = None,
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
