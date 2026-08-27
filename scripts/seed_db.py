"""Create the SQLite schema (idempotent). Run once before anything else:

    python scripts/seed_db.py
"""
from dispatch_agent.db import init_db

if __name__ == "__main__":
    init_db()
    print("Database ready.")
