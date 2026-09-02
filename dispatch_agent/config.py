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
    # Reject a provider's drive time when it exceeds this multiple of the straight-line estimate.
    # This is the Johor-detour filter for providers that return no route geometry (Google's
    # Distance Matrix gives only duration and distance, so there is nothing to inspect for a
    # border crossing). Generous on purpose: congestion must never trip it, a trip through
    # another country always should.
    max_drive_time_ratio: float = float(os.getenv("MAX_DRIVE_TIME_RATIO", "4.0"))
    db_path: str = os.getenv("DB_PATH", "./data/dispatch.db")
    # Pins "today" so a recorded demo and the test suite behave identically every run. Blank
    # means use the real date. ISO format, e.g. 2026-09-02.
    demo_base_date: str = os.getenv("DEMO_BASE_DATE", "")
    # The bookable window, as days from today. A customer may only choose dates in this range.
    horizon_lead_days_min: int = int(os.getenv("HORIZON_LEAD_DAYS_MIN", "2"))
    horizon_lead_days_max: int = int(os.getenv("HORIZON_LEAD_DAYS_MAX", "5"))
    # Scoring weights. A day with no jobs on it costs a fixed penalty to open; a lower-ranked
    # customer preference costs a little, so preference breaks ties without overriding routing.
    day_opening_penalty_minutes: int = int(os.getenv("DAY_OPENING_PENALTY_MINUTES", "60"))
    preference_penalty_per_rank: int = int(os.getenv("PREFERENCE_PENALTY_PER_RANK", "10"))
    default_job_duration_minutes: int = int(os.getenv("DEFAULT_JOB_DURATION_MINUTES", "60"))
    work_day_start: Time = _time("WORK_DAY_START", "09:00")
    # Hard end of the working day: the solver will not schedule past it, so a route that would
    # run late is infeasible rather than expensive.
    work_day_end: Time = _time("WORK_DAY_END", "18:00")
    # Soft end, used only for scoring: minutes worked past this (including the drive home) are
    # penalised. Because work_day_end is a hard constraint, a purely hard model can never
    # produce overtime, which would make the scoring term dead -- this is what gives it meaning.
    soft_day_end: Time = _time("SOFT_DAY_END", "17:00")
    # Whether a job's SERVICE must finish inside the customer's window, not merely start in it.
    # With this off, a 60-minute job may start at 11:55 in a 09:00-12:00 window and run to
    # 12:55 -- i.e. straight through a gap the customer said they were unavailable.
    require_service_within_window: bool = os.getenv("REQUIRE_SERVICE_WITHIN_WINDOW", "1") != "0"
    # OR-Tools uses guided local search, which burns its whole time budget regardless of when it
    # converges. Publishing a plan happens once and can afford to look harder; candidate
    # evaluation runs many solves per booking, so it gets a much shorter leash.
    solver_time_limit_seconds: int = int(os.getenv("SOLVER_TIME_LIMIT_SECONDS", "5"))
    candidate_solver_time_limit_seconds: int = int(
        os.getenv("CANDIDATE_SOLVER_TIME_LIMIT_SECONDS", "1")
    )


settings = Settings()
