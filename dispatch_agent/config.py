"""Environment-driven configuration, loaded once at import time.

Not frozen: tests monkeypatch individual fields (e.g. db_path) to point at a temp SQLite file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time as Time

from dotenv import load_dotenv

load_dotenv()


def _time(env_var: str, default: str) -> Time:
    return Time.fromisoformat(os.getenv(env_var, default))


@dataclass
class Settings:
    aws_region: str = os.getenv("AWS_REGION", "ap-southeast-1")
    bedrock_model_id: str = os.getenv(
        "BEDROCK_MODEL_ID",
        # Verify this against `aws bedrock list-foundation-models` in your account/region --
        # Bedrock model ids (and the cross-region "apac."/"us." prefix) change across regions
        # and model launches, so treat this default as a placeholder, not a guarantee.
        "apac.anthropic.claude-haiku-4-5-20250929-v1:0",
    )
    routing_provider: str = os.getenv("ROUTING_PROVIDER", "onemap")
    # Either a pre-issued static token (ONEMAP_TOKEN, takes priority) or an email/password pair
    # the routing client exchanges for a token itself. Leave all three blank to use the
    # haversine fallback.
    onemap_token: str = os.getenv("ONEMAP_TOKEN", "")
    onemap_email: str = os.getenv("ONEMAP_EMAIL", "")
    onemap_password: str = os.getenv("ONEMAP_PASSWORD", "")
    db_path: str = os.getenv("DB_PATH", "./data/dispatch.db")
    default_job_duration_minutes: int = int(os.getenv("DEFAULT_JOB_DURATION_MINUTES", "60"))
    work_day_start: Time = _time("WORK_DAY_START", "09:00")
    work_day_end: Time = _time("WORK_DAY_END", "18:00")


settings = Settings()
