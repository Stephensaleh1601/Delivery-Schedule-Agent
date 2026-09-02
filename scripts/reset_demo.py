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

    if path.exists():
        path.unlink()
        print(f"Deleted {path}")
    init_db(path)
    print(f"Recreated schema at {path}")

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
