"""Destroy the local database and rebuild it with fresh demo data.

Deliberately separate from the migration. `init_db()` runs on every app start and must never
lose anything; this exists for the opposite case -- when you want the demo scenario back from a
known-good starting point and do not care about what is there now.

Requires --yes, prints exactly what it will delete, and is never invoked by application startup.

    python scripts/reset_demo.py --yes
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from dispatch_agent.config import settings
from dispatch_agent.db import init_db


def _describe(path: Path) -> str:
    if not path.exists():
        return "no existing database -- nothing to delete"
    try:
        conn = sqlite3.connect(path)
        try:
            counts = []
            for table in ("jobs", "day_sequences", "route_plan_versions", "appointment_offers"):
                try:
                    n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    if n:
                        counts.append(f"{n} {table}")
                except sqlite3.Error:
                    continue
        finally:
            conn.close()
    except sqlite3.Error:
        return f"{path} (unreadable)"
    return f"{path} containing {', '.join(counts) if counts else 'no rows'}"


CACHE_TABLES = ("geocode_cache", "drive_time_cache")


def _read_caches(path: Path) -> dict[str, tuple[list[str], list[tuple]]]:
    """Every row of the provider caches, with their column names."""
    if not path.exists():
        return {}
    saved: dict[str, tuple[list[str], list[tuple]]] = {}
    conn = sqlite3.connect(path)
    try:
        for table in CACHE_TABLES:
            try:
                cursor = conn.execute(f"SELECT * FROM {table}")
            except sqlite3.Error:
                continue  # a database old enough not to have this table yet
            columns = [c[0] for c in cursor.description]
            saved[table] = (columns, cursor.fetchall())
    finally:
        conn.close()
    return saved


def _write_caches(path: Path, saved: dict[str, tuple[list[str], list[tuple]]]) -> str:
    """Put them back into the fresh schema, skipping anything the new schema no longer has."""
    if not saved:
        return ""
    restored = []
    conn = sqlite3.connect(path)
    try:
        # The cache tables are created lazily by the modules that own them, so a freshly
        # initialised database does not have them yet. Reuse each module's own DDL rather than
        # restating the columns here, where a schema change would not be noticed.
        from dispatch_agent.geo import geocoder, matrix_cache

        conn.executescript(geocoder.SCHEMA)
        conn.executescript(matrix_cache.SCHEMA)

        for table, (columns, rows) in saved.items():
            if not rows:
                continue
            placeholders = ",".join("?" * len(columns))
            try:
                conn.executemany(
                    f"INSERT OR IGNORE INTO {table} ({','.join(columns)}) VALUES ({placeholders})",
                    rows,
                )
            except sqlite3.Error as exc:
                print(f"  ! could not restore {table}: {exc}")
                continue
            restored.append(f"{len(rows)} {table} rows")
        conn.commit()
    finally:
        conn.close()
    return ", ".join(restored)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="confirm the deletion")
    parser.add_argument("--no-seed", action="store_true", help="recreate the schema but leave it empty")
    args = parser.parse_args()

    path = Path(settings.db_path)
    print(f"This will permanently DELETE: {_describe(path)}")

    if not args.yes:
        print("\nRefusing to proceed without --yes. Nothing has been changed.")
        return 1

    # Lift the caches out before the file goes. They are not demo data -- they are the record of
    # every geocode and drive time we have already paid a provider for, and throwing them away
    # makes the next run slow, billable, and dependent on the network being up at the worst
    # possible moment.
    preserved = _read_caches(path)

    if path.exists():
        try:
            path.unlink()
            print(f"Deleted {path}")
        except PermissionError:
            # Windows will not unlink a file another process has open, and during a demo that
            # other process is the running server. Reseeding between takes has to work without
            # stopping it, so fall back to emptying the tables in place -- which is what the seed
            # does anyway, and reaches the same state by a different route.
            print(f"{path} is open in another process (the server?) -- clearing it in place.")
    init_db(path)
    print(f"Schema ready at {path}")

    restored = _write_caches(path, preserved)
    if restored:
        print(f"Preserved {restored}")

    if args.no_seed:
        print("Skipping seed data (--no-seed).")
        return 0

    # Phase 9 replaces the seed script with a clock-relative one exposing seed(); until then
    # fall back to its main(), so this command is usable throughout.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import seed_test_clients

    seeder = getattr(seed_test_clients, "seed", None) or seed_test_clients.main
    result = seeder()
    if result:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
