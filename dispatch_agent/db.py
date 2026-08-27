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
from dispatch_agent.models import DaySequence, JobRecord, OverrideLogEntry

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
