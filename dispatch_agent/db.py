"""SQLite persistence: jobs, generated day sequences, and the coordinator override log.

Nested Pydantic fields are stored as JSON blobs rather than normalized columns -- at hackathon
scale (tens of jobs a day) that trade-off buys simplicity, and the Pydantic models stay the
single source of truth for shape and validation.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date as Date
from pathlib import Path

from dispatch_agent.config import settings
from dispatch_agent.models import DaySequence, JobRecord, Notification, OverrideLogEntry

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    delivery_date TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL
);

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
"""


def init_db(db_path: str | Path | None = None) -> None:
    """Create the schema if it doesn't exist yet. Safe to call on every startup."""
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def _connect():
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

    def save_job(self, job: JobRecord) -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, delivery_date, status, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET delivery_date=excluded.delivery_date, "
                "status=excluded.status, data=excluded.data",
                (job.id, job.delivery_date.isoformat(), job.status.value, job.model_dump_json()),
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
