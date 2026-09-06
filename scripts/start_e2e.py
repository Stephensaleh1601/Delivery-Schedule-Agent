"""Start a deterministic seeded API for the browser regression suite."""
from __future__ import annotations

import os
import sys
from pathlib import Path


root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
database = root / "data" / "playwright.db"
os.environ["DB_PATH"] = str(database)
os.environ["LLM_PROVIDER"] = "none"
os.environ["ROUTING_PROVIDER"] = "haversine"
os.environ["GEOCODING_ENABLED"] = "0"
os.environ.setdefault("DEMO_BASE_DATE", "2026-09-05")

from scripts.seed_test_clients import seed  # noqa: E402


def main() -> None:
    import uvicorn

    seed()
    uvicorn.run("dispatch_agent.webapp.main:app", host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
