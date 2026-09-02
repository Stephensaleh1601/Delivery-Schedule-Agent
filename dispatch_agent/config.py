"""Environment-driven configuration, loaded once at import time.

Not frozen: tests monkeypatch individual fields (e.g. db_path) to point at a temp SQLite file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time as Time

from dotenv import load_dotenv

# `.env.local` holds real credentials and is gitignored; `.env` is the shared/checked-in-shaped
# fallback. Loaded local-first because python-dotenv never overwrites an already-set variable,
# so whatever `.env.local` defines wins.
load_dotenv(".env.local")
load_dotenv()

# On some networks (corporate proxy/VPN doing TLS interception, some antivirus/endpoint
# security software) the OS trusts the intercepting certificate but certifi's bundled CA list
# doesn't, so `requests` calls to Google/OneMap fail with CERTIFICATE_VERIFY_FAILED. Those
# failures are caught by the routing client's broad except-and-fall-back-to-haversine handling,
# so this doesn't crash anything -- it just silently degrades every drive-time/distance call to
# the haversine estimate and, for the batched matrix call, still pays for every failed
# connection attempt before falling back. Using the OS trust store instead (this is an
# application entry point, not a library, so this is the documented safe place for it) fixes
# both. Import errors here would mean `truststore` isn't installed -- non-fatal, same as before.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass


def _time(env_var: str, default: str) -> Time:
    return Time.fromisoformat(os.getenv(env_var, default))


@dataclass
class Settings:
    # LLM_PROVIDER picks which client build_llm_client() (dispatch_agent/llm.py) returns --
    # "bedrock" (default) or "openai". Only one needs valid credentials at a time.
    llm_provider: str = os.getenv("LLM_PROVIDER", "bedrock").lower()
    aws_region: str = os.getenv("AWS_REGION", "ap-southeast-1")
    bedrock_model_id: str = os.getenv(
        "BEDROCK_MODEL_ID",
        # Verify this against `aws bedrock list-foundation-models` in your account/region --
        # Bedrock model ids (and the cross-region "apac."/"us." prefix) change across regions
        # and model launches, so treat this default as a placeholder, not a guarantee.
        "apac.anthropic.claude-haiku-4-5-20250929-v1:0",
    )
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    # ROUTING_PROVIDER: "google" (Distance Matrix API), "onemap", or "haversine". Leave
    # credentials blank for whichever provider you're not using -- an unset/failing provider
    # falls back to the haversine estimate automatically.
    routing_provider: str = os.getenv("ROUTING_PROVIDER", "onemap")
    google_maps_api_key: str = os.getenv("GOOGLE_MAPS_API_KEY", "")
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
