"""The migration from the single-day schema to the multi-day one.

This is the highest-risk change in the whole feature: it rewrites a table that holds real
bookings. The fixture builds the OLD database with raw SQL and hand-written blobs rather than
via the current JobRecord -- otherwise it would silently start testing today's model against
itself and stop being a migration test at all.
"""
import json
import sqlite3

import pytest

from dispatch_agent import config, db
from dispatch_agent.models import JobRecord, PlanningStatus, ReadinessStatus

OLD_SCHEMA = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    delivery_date TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE day_sequences (delivery_date TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE override_log (
    id TEXT PRIMARY KEY, delivery_date TEXT NOT NULL, job_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE notifications (id TEXT PRIMARY KEY, read INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
"""


def _legacy_blob(job_id, name, status, postal="018956", window=("09:00:00", "18:00:00")):
    """A job exactly as the pre-migration code wrote it. Deliberately a literal dict."""
    return {
        "id": job_id,
        "customer_name": name,
        "phone": "91234567",
        "address": {
            "raw_text": f"{name}'s place",
            "postal_code": postal,
            "coordinates": {"lat": 1.2837, "lng": 103.8517},
        },
        "job_type": "sofa",
        "availability": [{"start": window[0], "end": window[1]}],
        "duration_minutes": 45,
        "delivery_date": "2026-08-28",
        "status": status,
        "raw_message": "[seeded]",
        "notes": None,
        "created_at": "2026-08-20T10:00:00+00:00",
        "updated_at": "2026-08-20T10:00:00+00:00",
    }


LEGACY_JOBS = [
    _legacy_blob("job-new", "Amina", "new"),
    _legacy_blob("job-approved", "Ben", "approved"),
    _legacy_blob("job-sequenced", "Chandra", "sequenced"),
    _legacy_blob("job-rejected", "Dana", "rejected"),
]


@pytest.fixture()
def old_db(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.executemany(
        "INSERT INTO jobs (id, delivery_date, status, data) VALUES (?, ?, ?, ?)",
        [(j["id"], j["delivery_date"], j["status"], json.dumps(j)) for j in LEGACY_JOBS],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(config.settings, "db_path", str(path))
    return path


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT id, delivery_date, planning_status, data FROM jobs").fetchall()
    finally:
        conn.close()


def test_migration_preserves_every_row(old_db):
    db.init_db(old_db)
    assert len(_rows(old_db)) == len(LEGACY_JOBS)


def test_delivery_date_becomes_nullable(old_db):
    db.init_db(old_db)
    conn = sqlite3.connect(old_db)
    try:
        notnull = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(jobs)")}
    finally:
        conn.close()
    assert notnull["delivery_date"] == 0, "delivery_date is still NOT NULL -- unscheduled orders cannot exist"


def test_every_migrated_row_still_loads_as_a_job_record(old_db):
    """The migration writes blobs that the *new* model must accept. If this fails the database
    is unreadable by the application, which is worse than not migrating at all."""
    db.init_db(old_db)
    for _, _, _, blob in _rows(old_db):
        JobRecord.model_validate_json(blob)


def test_legacy_status_maps_forward_and_dated_jobs_are_locked(old_db):
    """A job with a date was agreed with a customer, so it must come out of the migration as a
    promise the solver will protect -- not as an unplanned order it may freely move."""
    db.init_db(old_db)
    by_id = {r[0]: JobRecord.model_validate_json(r[3]) for r in _rows(old_db)}

    assert by_id["job-approved"].planning_status is PlanningStatus.CONFIRMED
    assert by_id["job-approved"].locked_window is not None
    assert by_id["job-approved"].is_locked

    assert by_id["job-sequenced"].planning_status is PlanningStatus.SEQUENCED
    assert by_id["job-rejected"].planning_status is PlanningStatus.CANCELLED
    # A legacy "new" job was never promised anything, so it must NOT come out locked.
    assert by_id["job-new"].planning_status is PlanningStatus.PENDING_PLANNING
    assert by_id["job-new"].locked_window is None


def test_migration_does_not_invent_readiness_or_early_delivery_consent(old_db):
    db.init_db(old_db)
    job = JobRecord.model_validate_json(_rows(old_db)[0][3])
    assert job.readiness_status is ReadinessStatus.READY
    assert job.can_deliver_early is False, "consent to an earlier slot must never be assumed"


def test_migration_is_idempotent(old_db):
    db.init_db(old_db)
    first = _rows(old_db)
    db.init_db(old_db)
    assert _rows(old_db) == first, "running the migration twice changed the data"


def test_lock_existing_can_be_disabled(old_db, monkeypatch):
    """Locking every legacy job is right for the demo but can make a tight legacy day
    infeasible, so it has to be a decision an operator can reverse."""
    monkeypatch.setenv("MIGRATE_LOCK_EXISTING", "0")
    db.init_db(old_db)
    by_id = {r[0]: JobRecord.model_validate_json(r[3]) for r in _rows(old_db)}
    # Without a lock the job cannot legally sit at CONFIRMED, so it steps back to pending.
    assert by_id["job-approved"].locked_window is None
    assert by_id["job-approved"].planning_status is PlanningStatus.PENDING_PLANNING


def test_a_fresh_database_needs_no_migration(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(config.settings, "db_path", str(path))
    db.init_db(path)
    conn = sqlite3.connect(path)
    try:
        notnull = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(jobs)")}
        version = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    finally:
        conn.close()
    assert notnull["delivery_date"] == 0
    assert version is not None and version[0] == db.SCHEMA_VERSION


def test_only_one_plan_version_per_date_can_be_active(tmp_path, monkeypatch):
    """Enforced by a partial unique index rather than by convention -- UNIQUE(date, version)
    alone happily permits two active plans, which would make the v1-vs-v2 comparison lie."""
    path = tmp_path / "plans.db"
    monkeypatch.setattr(config.settings, "db_path", str(path))
    db.init_db(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO route_plan_versions (id, delivery_date, version, status, content_hash, data)"
            " VALUES ('p1', '2026-09-04', 1, 'active', 'h1', '{}')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO route_plan_versions (id, delivery_date, version, status, content_hash, data)"
                " VALUES ('p2', '2026-09-04', 2, 'active', 'h2', '{}')"
            )
    finally:
        conn.close()
