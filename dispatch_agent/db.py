"""SQLite persistence: jobs, generated day sequences, and the coordinator override log.

Nested Pydantic fields are stored as JSON blobs rather than normalized columns -- at hackathon
scale (tens of jobs a day) that trade-off buys simplicity, and the Pydantic models stay the
single source of truth for shape and validation.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date as Date
from pathlib import Path

from dispatch_agent.config import settings
from dispatch_agent.models import (
    LEGACY_TO_PLANNING_STATUS,
    LOCKED_PLANNING_STATUSES,
    PLANNING_TO_LEGACY_STATUS,
    AgentRunLog,
    AppointmentOffer,
    CoordinatorException,
    CustomerMessage,
    DaySequence,
    JobRecord,
    JobStatus,
    Notification,
    OfferStatus,
    OverrideLogEntry,
    PlanningEvent,
    PlanningStatus,
    ReadinessStatus,
    RoutePlanVersion,
)

SCHEMA = """
-- delivery_date is nullable: an order exists from the moment it is booked, but has no date
-- until the customer accepts an offered slot. Databases created before that change are rebuilt
-- by migrate() below; IF NOT EXISTS deliberately does not touch them here.
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    delivery_date TEXT,
    status TEXT NOT NULL,
    planning_status TEXT NOT NULL DEFAULT 'pending_planning',
    readiness_status TEXT NOT NULL DEFAULT 'ready',
    data TEXT NOT NULL
);
-- The jobs indexes are NOT created here. This script runs before migrate(), and on a database
-- predating the multi-day schema the jobs table has no planning_status column yet -- indexing it
-- would abort the whole script. They are created in _ensure_job_indexes() once the shape is right.

CREATE TABLE IF NOT EXISTS day_sequences (
    delivery_date TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS override_log (
    id TEXT PRIMARY KEY,
    delivery_date TEXT NOT NULL,
    job_id TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    read INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appointment_offers (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_offers_order ON appointment_offers(order_id);

CREATE TABLE IF NOT EXISTS route_plan_versions (
    id TEXT PRIMARY KEY,
    delivery_date TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    data TEXT NOT NULL,
    UNIQUE(delivery_date, version)
);
-- UNIQUE(delivery_date, version) does NOT express "one active plan per date" -- it permits any
-- number of versions to be active at once. This partial index is what actually enforces it,
-- turning a would-be silent data bug into an IntegrityError. (SQLite has supported partial
-- indexes since 3.8.0.)
CREATE UNIQUE INDEX IF NOT EXISTS ux_plan_active_per_date
    ON route_plan_versions(delivery_date) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS planning_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    direction TEXT NOT NULL,
    channel TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_order ON messages(order_id);

CREATE TABLE IF NOT EXISTS coordinator_exceptions (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
"""

SCHEMA_VERSION = "2"


_ACTIVE_CONNECTION: ContextVar[sqlite3.Connection | None] = ContextVar(
    "dispatch_active_connection", default=None
)


def current_connection() -> sqlite3.Connection | None:
    """The request-scoped transaction connection, when one is active."""
    return _ACTIVE_CONNECTION.get()


def init_db(db_path: str | Path | None = None) -> None:
    """Create the schema if it doesn't exist yet, then migrate. Safe to call on every startup,
    and it is -- webapp/main.py calls it at import."""
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
    migrate(path)


# -- Migration ----------------------------------------------------------------


def _schema_version(conn) -> str:
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    return row[0] if row else "1"


def _jobs_delivery_date_is_not_null(conn) -> bool:
    # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
    return any(r[1] == "delivery_date" and r[3] == 1 for r in conn.execute("PRAGMA table_info(jobs)"))


def migrate(db_path: str | Path | None = None) -> None:
    """Bring an existing database up to the current schema, preserving every row.

    Idempotent and cheap when already current: the version check short-circuits before any work.
    """
    path = Path(db_path or settings.db_path)
    if not path.exists():
        return
    # Explicit transactions need an autocommit connection -- the default isolation_level opens
    # its own transaction implicitly and would fight the BEGIN below.
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.executescript(SCHEMA)
        if _schema_version(conn) == SCHEMA_VERSION:
            return
        if _jobs_delivery_date_is_not_null(conn):
            _rebuild_jobs_table(conn)
        _ensure_job_indexes(conn)
        _backfill_job_blobs(conn)
        conn.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
    finally:
        conn.close()


def _ensure_job_indexes(conn) -> None:
    """Created here rather than in SCHEMA because they depend on columns a pre-migration
    database does not have."""
    conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_delivery_date ON jobs(delivery_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_planning_status ON jobs(planning_status)")


def _rebuild_jobs_table(conn) -> None:
    """Make jobs.delivery_date nullable, so an order can exist before a date is agreed.

    SQLite has no ALTER COLUMN, so this is the documented table-rebuild. `jobs` has no foreign
    keys, triggers or views pointing at it, which keeps the procedure short.
    """
    # PRAGMA foreign_keys is silently ignored inside a transaction, so it must come first.
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """CREATE TABLE jobs_new (
                id TEXT PRIMARY KEY,
                delivery_date TEXT,
                status TEXT NOT NULL,
                planning_status TEXT NOT NULL DEFAULT 'pending_planning',
                readiness_status TEXT NOT NULL DEFAULT 'ready',
                data TEXT NOT NULL)"""
        )
        conn.execute(
            "INSERT INTO jobs_new (id, delivery_date, status, planning_status, readiness_status, data) "
            "SELECT id, delivery_date, status, 'pending_planning', 'ready', data FROM jobs"
        )
        conn.execute("DROP TABLE jobs")
        conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_delivery_date ON jobs(delivery_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_jobs_planning_status ON jobs(planning_status)")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _backfill_job_blobs(conn) -> None:
    """Give pre-existing rows the fields the new model expects.

    Defaults alone would leave every legacy job PENDING_PLANNING, which is wrong -- a job with a
    date was agreed with a customer. Its legacy status is mapped forward, and (unless
    MIGRATE_LOCK_EXISTING=0) its first availability window becomes a locked_window.

    That last part is a deliberate choice, not an accident: locking existing jobs is what makes
    "no confirmed appointment was moved" true for legacy data, but it can also render a
    previously-solvable day infeasible if the old windows were tight. Hence the escape hatch.
    """
    lock_existing = os.getenv("MIGRATE_LOCK_EXISTING", "1") != "0"
    updates = []
    for job_id, blob in conn.execute("SELECT id, data FROM jobs").fetchall():
        data = json.loads(blob)
        if "planning_status" in data:
            continue
        legacy = JobStatus(data.get("status", "new"))
        has_date = bool(data.get("delivery_date"))
        planning = LEGACY_TO_PLANNING_STATUS[legacy] if has_date else PlanningStatus.PENDING_PLANNING
        windows = data.get("availability") or []

        if planning in LOCKED_PLANNING_STATUSES:
            if lock_existing and windows and not data.get("locked_window"):
                data["locked_window"] = windows[0]
            if not data.get("locked_window"):
                # The lifecycle requires a locked window at these statuses. Without one we must
                # step the job back rather than write a record the model would refuse to load.
                planning = PlanningStatus.PENDING_PLANNING

        data["planning_status"] = planning.value
        data["status"] = PLANNING_TO_LEGACY_STATUS[planning].value
        data.setdefault("readiness_status", ReadinessStatus.READY.value)
        data.setdefault("can_deliver_early", False)
        data.setdefault("priority", 0)
        if has_date and not data.get("availability_options"):
            data["availability_options"] = [
                {
                    "id": f"{job_id}-legacy-{i}",
                    "date": data["delivery_date"],
                    "window": window,
                    "preference_rank": i + 1,
                }
                for i, window in enumerate(windows)
            ]

        job = JobRecord.model_validate(data)  # refuse to write anything the model rejects
        updates.append(
            (job.model_dump_json(), job.status.value, job.planning_status.value,
             job.readiness_status.value, job_id)
        )

    if updates:
        conn.executemany(
            "UPDATE jobs SET data = ?, status = ?, planning_status = ?, readiness_status = ? "
            "WHERE id = ?",
            updates,
        )


@contextmanager
def _connect():
    active = current_connection()
    if active is not None:
        yield active
        return

    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class JobsRepository:
    """CRUD for JobRecord, DaySequence and OverrideLogEntry."""

    @contextmanager
    def transaction(self):
        """Make every repository write in the block commit or roll back together."""
        if current_connection() is not None:
            yield self
            return

        path = Path(settings.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        token = None
        began = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            began = True
            token = _ACTIVE_CONNECTION.set(conn)
            yield self
            conn.execute("COMMIT")
            began = False
        except Exception:
            if began:
                conn.execute("ROLLBACK")
            raise
        finally:
            if token is not None:
                _ACTIVE_CONNECTION.reset(token)
            conn.close()

    def save_job(self, job: JobRecord) -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, delivery_date, status, planning_status, readiness_status, data) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET delivery_date=excluded.delivery_date, "
                "status=excluded.status, planning_status=excluded.planning_status, "
                "readiness_status=excluded.readiness_status, data=excluded.data",
                (
                    job.id,
                    job.delivery_date.isoformat() if job.delivery_date else None,
                    job.status.value,
                    job.planning_status.value,
                    job.readiness_status.value,
                    job.model_dump_json(),
                ),
            )

    def delete_job(self, job_id: str) -> None:
        with _connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def get_job(self, job_id: str) -> JobRecord | None:
        with _connect() as conn:
            row = conn.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return JobRecord.model_validate_json(row[0]) if row else None

    def jobs_for_date(self, delivery_date: Date) -> list[JobRecord]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT data FROM jobs WHERE delivery_date = ? ORDER BY id",
                (delivery_date.isoformat(),),
            ).fetchall()
        return [JobRecord.model_validate_json(r[0]) for r in rows]

    def all_jobs(self) -> list[JobRecord]:
        """Every job regardless of date -- the back office's "orders coming in" feed."""
        with _connect() as conn:
            rows = conn.execute("SELECT data FROM jobs ORDER BY delivery_date, id").fetchall()
        return [JobRecord.model_validate_json(r[0]) for r in rows]

    def find_jobs_by_customer(self, query: str) -> list[JobRecord]:
        """Case-insensitive substring match on name or phone -- how the reschedule chat flow
        looks a customer's booking up. Jobs are stored as opaque JSON blobs (see module
        docstring), so this filters in Python rather than with SQL LIKE on a real column."""
        needle = query.strip().lower()
        if not needle:
            return []
        return [
            job
            for job in self.all_jobs()
            if needle in job.customer_name.lower() or (job.phone and needle in job.phone.lower())
        ]

    def pending_dates(self) -> list[Date]:
        """Distinct delivery dates with at least one job -- populates the route-plan date picker."""
        with _connect() as conn:
            rows = conn.execute("SELECT DISTINCT delivery_date FROM jobs ORDER BY delivery_date").fetchall()
        return [Date.fromisoformat(r[0]) for r in rows]

    def save_sequence(self, sequence: DaySequence) -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO day_sequences (delivery_date, data) VALUES (?, ?) "
                "ON CONFLICT(delivery_date) DO UPDATE SET data=excluded.data",
                (sequence.delivery_date.isoformat(), sequence.model_dump_json()),
            )

    def get_sequence(self, delivery_date: Date) -> DaySequence | None:
        with _connect() as conn:
            row = conn.execute(
                "SELECT data FROM day_sequences WHERE delivery_date = ?",
                (delivery_date.isoformat(),),
            ).fetchone()
        return DaySequence.model_validate_json(row[0]) if row else None

    def log_override(self, entry: OverrideLogEntry) -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO override_log (id, delivery_date, job_id, data) VALUES (?, ?, ?, ?)",
                (entry.id, entry.delivery_date.isoformat(), entry.job_id, entry.model_dump_json()),
            )

    def overrides_for_date(self, delivery_date: Date) -> list[OverrideLogEntry]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT data FROM override_log WHERE delivery_date = ? ORDER BY id",
                (delivery_date.isoformat(),),
            ).fetchall()
        return [OverrideLogEntry.model_validate_json(r[0]) for r in rows]

    def add_notification(self, notification: Notification) -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO notifications (id, read, data) VALUES (?, ?, ?)",
                (notification.id, int(notification.read), notification.model_dump_json()),
            )

    def unread_notifications(self) -> list[Notification]:
        with _connect() as conn:
            rows = conn.execute("SELECT data FROM notifications WHERE read = 0 ORDER BY id").fetchall()
        return [Notification.model_validate_json(r[0]) for r in rows]

    # -- Multi-day planning ---------------------------------------------------

    def jobs_by_planning_status(self, *statuses: PlanningStatus) -> list[JobRecord]:
        """Filtered in SQL rather than by deserialising every blob -- the historical-orders seed
        makes all_jobs() an increasingly bad way to ask this."""
        if not statuses:
            return []
        placeholders = ",".join("?" * len(statuses))
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT data FROM jobs WHERE planning_status IN ({placeholders}) ORDER BY id",
                tuple(s.value for s in statuses),
            ).fetchall()
        return [JobRecord.model_validate_json(r[0]) for r in rows]

    def jobs_in_range(self, first: Date, last: Date) -> list[JobRecord]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT data FROM jobs WHERE delivery_date BETWEEN ? AND ? ORDER BY delivery_date, id",
                (first.isoformat(), last.isoformat()),
            ).fetchall()
        return [JobRecord.model_validate_json(r[0]) for r in rows]

    def save_offer(self, offer: AppointmentOffer) -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO appointment_offers (id, order_id, status, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
                (offer.id, offer.order_id, offer.status.value, offer.model_dump_json()),
            )

    def get_offer(self, offer_id: str) -> AppointmentOffer | None:
        with _connect() as conn:
            row = conn.execute(
                "SELECT data FROM appointment_offers WHERE id = ?", (offer_id,)
            ).fetchone()
        return AppointmentOffer.model_validate_json(row[0]) if row else None

    def offers_for_order(self, order_id: str) -> list[AppointmentOffer]:
        """An order's offers, oldest first.

        This used to be `ORDER BY id`, which sorts a random uuid hex -- so `offers[-1]` could hand
        back the EARLIER round. Callers used that to decide which slots to show a customer.
        `rowid` is insertion order (save_offer inserts once, then updates), and round_number is the
        real semantic order, so sort on both.
        """
        with _connect() as conn:
            rows = conn.execute(
                "SELECT data FROM appointment_offers WHERE order_id = ? ORDER BY rowid", (order_id,)
            ).fetchall()
        offers = [AppointmentOffer.model_validate_json(r[0]) for r in rows]
        return sorted(offers, key=lambda o: (o.round_number, o.created_at))

    def latest_offer_for_order(self, order_id: str) -> AppointmentOffer | None:
        offers = self.offers_for_order(order_id)
        return offers[-1] if offers else None

    def open_offer_for_order(self, order_id: str) -> AppointmentOffer | None:
        """The offer this customer is currently being asked to respond to, if any.

        Used to stop a second planning call opening a competing negotiation while the first is
        still outstanding."""
        for offer in reversed(self.offers_for_order(order_id)):
            if offer.status in (OfferStatus.PENDING, OfferStatus.SENT):
                return offer
        return None

    def claim_offer_response(self, offer_id: str, status: str) -> bool:
        """Atomically move an offer out of the awaiting-response state.

        Returns False if it had already been responded to, which is how a double-tapped accept
        button is made harmless: the caller returns the recorded outcome instead of re-solving
        and publishing a second, identical plan version.
        """
        with _connect() as conn:
            cursor = conn.execute(
                "UPDATE appointment_offers SET status = ? "
                "WHERE id = ? AND status IN ('pending', 'sent')",
                (status, offer_id),
            )
            return cursor.rowcount > 0

    def plan_versions(self, delivery_date: Date) -> list[RoutePlanVersion]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT data FROM route_plan_versions WHERE delivery_date = ? ORDER BY version",
                (delivery_date.isoformat(),),
            ).fetchall()
        return [RoutePlanVersion.model_validate_json(r[0]) for r in rows]

    def active_plan(self, delivery_date: Date) -> RoutePlanVersion | None:
        with _connect() as conn:
            row = conn.execute(
                "SELECT data FROM route_plan_versions WHERE delivery_date = ? AND status = 'active'",
                (delivery_date.isoformat(),),
            ).fetchone()
        return RoutePlanVersion.model_validate_json(row[0]) if row else None

    def save_planning_event(self, event: PlanningEvent) -> bool:
        """Record an event. Returns False if this id was already seen, which is what makes an
        Idempotency-Key on the customer-response endpoint work."""
        with _connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO planning_events (id, event_type, created_at, data) VALUES (?, ?, ?, ?)",
                    (
                        event.id,
                        event.event_type.value,
                        event.created_at.isoformat(),
                        event.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def reserve_planning_run(self, event: PlanningEvent, run: AgentRunLog) -> bool:
        """Atomically claim one event id and create its initial run row.

        A concurrent retry must never observe a claimed event without its run. ``BEGIN IMMEDIATE``
        serialises the two inserts, so the loser can safely return the winner's persisted run.
        """
        with self.transaction():
            if not self.save_planning_event(event):
                return False
            self.save_agent_run(run)
        return True

    def get_planning_event(self, event_id: str) -> PlanningEvent | None:
        with _connect() as conn:
            row = conn.execute("SELECT data FROM planning_events WHERE id = ?", (event_id,)).fetchone()
        return PlanningEvent.model_validate_json(row[0]) if row else None

    def save_agent_run(self, run: AgentRunLog) -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO agent_runs (id, event_id, status, started_at, data) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
                (run.id, run.event_id, run.status.value, run.started_at.isoformat(), run.model_dump_json()),
            )

    def agent_runs(self, limit: int = 50) -> list[AgentRunLog]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT data FROM agent_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [AgentRunLog.model_validate_json(r[0]) for r in rows]

    def get_agent_run(self, run_id: str) -> AgentRunLog | None:
        """One run by id. The only correct way to fetch the trace behind a message -- "the newest
        run" is what this replaces."""
        with _connect() as conn:
            row = conn.execute("SELECT data FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        return AgentRunLog.model_validate_json(row[0]) if row else None

    def agent_runs_by_id(self, run_ids: list[str]) -> dict[str, AgentRunLog]:
        """Several runs in one query, so rendering a conversation is not N+1 requests."""
        wanted = [r for r in dict.fromkeys(run_ids) if r]
        if not wanted:
            return {}
        placeholders = ",".join("?" * len(wanted))
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT data FROM agent_runs WHERE id IN ({placeholders})", wanted
            ).fetchall()
        runs = [AgentRunLog.model_validate_json(r[0]) for r in rows]
        return {run.id: run for run in runs}

    def agent_run_for_event(self, event_id: str) -> AgentRunLog | None:
        with _connect() as conn:
            row = conn.execute(
                "SELECT data FROM agent_runs WHERE event_id = ? ORDER BY started_at DESC LIMIT 1",
                (event_id,),
            ).fetchone()
        return AgentRunLog.model_validate_json(row[0]) if row else None

    def save_message(self, message: CustomerMessage) -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO messages (id, order_id, direction, channel, created_at, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.order_id,
                    message.direction.value,
                    message.channel,
                    message.created_at.isoformat(),
                    message.model_dump_json(),
                ),
            )

    def messages(self, order_id: str | None = None) -> list[CustomerMessage]:
        with _connect() as conn:
            if order_id:
                rows = conn.execute(
                    "SELECT data FROM messages WHERE order_id = ? ORDER BY created_at", (order_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT data FROM messages ORDER BY created_at").fetchall()
        return [CustomerMessage.model_validate_json(r[0]) for r in rows]

    def save_exception(self, exception: CoordinatorException) -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO coordinator_exceptions (id, status, created_at, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data",
                (
                    exception.id,
                    "resolved" if exception.resolved else "open",
                    exception.created_at.isoformat(),
                    exception.model_dump_json(),
                ),
            )

    def open_exceptions(self) -> list[CoordinatorException]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT data FROM coordinator_exceptions WHERE status = 'open' ORDER BY created_at"
            ).fetchall()
        return [CoordinatorException.model_validate_json(r[0]) for r in rows]

    def mark_notification_read(self, notification_id: str) -> None:
        with _connect() as conn:
            row = conn.execute("SELECT data FROM notifications WHERE id = ?", (notification_id,)).fetchone()
            if row is None:
                return
            notification = Notification.model_validate_json(row[0])
            notification.read = True
            conn.execute(
                "UPDATE notifications SET read = 1, data = ? WHERE id = ?",
                (notification.model_dump_json(), notification_id),
            )
