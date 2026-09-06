"""Environment-driven configuration, loaded once at import time.

Not frozen: tests monkeypatch individual fields (e.g. db_path) to point at a temp SQLite file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
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


def _flag(env_var: str, default: bool) -> bool:
    """A boolean from the environment, accepting the words people actually write in a .env."""
    raw = os.getenv(env_var)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _time(env_var: str, default: str) -> Time:
    return Time.fromisoformat(os.getenv(env_var, default))


@dataclass
class Settings:
    # LLM_PROVIDER picks which client build_llm_client() (dispatch_agent/llm.py) returns --
    # "bedrock" (default), "openai", or "none". `none` is a real off switch used by the
    # offline suite and deterministic demo fallback.
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
    routing_provider: str = os.getenv("ROUTING_PROVIDER", "onemap").lower()
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
    # Whether to resolve postal codes against OneMap/Google. Off means every lookup falls back to
    # the postal district's centre, marked as such. Tests turn this off so they never ATTEMPT a
    # call -- catching the failure instead would let a genuinely accidental network call pass
    # unnoticed, which is the thing the offline fixture exists to prevent.
    geocoding_enabled: bool = os.getenv("GEOCODING_ENABLED", "1") != "0"
    db_path: str = os.getenv("DB_PATH", "./data/dispatch.db")
    # Pins "today" so a recorded demo and the test suite behave identically every run. Blank
    # means use the real date. ISO format, e.g. 2026-09-02.
    demo_base_date: str = os.getenv("DEMO_BASE_DATE", "")
    # Notice the operation needs: a customer may only be offered a date this many days out.
    horizon_lead_days_min: int = int(os.getenv("HORIZON_LEAD_DAYS_MIN", "2"))
    # A customer may only be inserted next to an existing stop within this many kilometres.
    # Straight-line, and used only to decide which positions are worth testing -- the ranking
    # is by added detour, which is a different and larger number.
    anchor_radius_km: float = float(os.getenv("ANCHOR_RADIUS_KM", "10"))
    # How many weeks ahead to look for a Friday/Saturday pair that is fully published, before
    # giving up and escalating. A bound, not a target -- in practice the answer is this week or
    # next, and this only stops an empty database scanning forever.
    cycle_search_weeks: int = int(os.getenv("CYCLE_SEARCH_WEEKS", "8"))
    # Scoring weights. A day with no jobs on it costs a fixed penalty to open; a lower-ranked
    # customer preference costs a little, so preference breaks ties without overriding routing.
    day_opening_penalty_minutes: int = int(os.getenv("DAY_OPENING_PENALTY_MINUTES", "60"))
    preference_penalty_per_rank: int = int(os.getenv("PREFERENCE_PENALTY_PER_RANK", "10"))
    # A customer's availability is a boundary, not the promise. Having solved the route, we offer a
    # window this wide around the arrival the solver actually chose. Two hours is a promise someone
    # can plan a morning around; nine hours is not an appointment.
    promise_window_minutes: int = int(os.getenv("PROMISE_WINDOW_MINUTES", "120"))
    # ...but never so tight that the arrival is pinned. solver._normalised_windows reduces a lock
    # [S,E] for a D-minute job to an arrival domain of [S, E-D]; at E-S == D that is a single point,
    # and one leg re-estimating by a minute makes the day infeasible for everyone on it. This is the
    # floor on how much room a later re-solve keeps.
    promise_min_slack_minutes: int = int(os.getenv("PROMISE_MIN_SLACK_MINUTES", "30"))
    default_job_duration_minutes: int = int(os.getenv("DEFAULT_JOB_DURATION_MINUTES", "60"))
    # How much driving another day must save before we ask the customer to move. The policy this
    # encodes: a time the customer asked for and we can serve is served. Counteroffering to save a
    # minute is haggling, and a coordinator who does it is one nobody wants to deal with. Fifteen
    # minutes is about a stop's worth of slack on this operation. Infeasibility, overtime and
    # opening an otherwise-empty day justify a counteroffer regardless of this number --
    # see planning/negotiation.should_counteroffer.
    counteroffer_saving_minutes: int = int(os.getenv("COUNTEROFFER_SAVING_MINUTES", "15"))
    # What an hour of the crew sitting idle is worth, against an hour of driving. A slot that adds
    # one driving minute but strands the van for three hours is not a cheap slot, and scoring that
    # counted only driving said it was. Weighted below driving because waiting is genuinely less
    # costly than moving -- no fuel, no risk -- but nothing like free.
    idle_penalty_per_hour: int = int(os.getenv("IDLE_PENALTY_PER_HOUR", "20"))
    # An idle gap big enough to be worth raising with the customer.
    material_idle_minutes: int = int(os.getenv("MATERIAL_IDLE_MINUTES", "60"))
    # Where every route starts and ends. SUTD is the demo default; in production this is the
    # company's warehouse, and the only thing that changes is these three values.
    # default_factory, not a plain default: a dataclass evaluates field defaults once, when the
    # class body runs at import. With `= os.getenv(...)` a later environment change -- a test, or
    # anything that loads .env after this module -- would be read correctly by nothing.
    depot_lat: float = field(default_factory=lambda: float(os.getenv("DEPOT_LAT", "1.34085")))
    depot_lng: float = field(default_factory=lambda: float(os.getenv("DEPOT_LNG", "103.9624851")))
    depot_address: str = field(
        default_factory=lambda: os.getenv("DEPOT_ADDRESS", "8 Somapah Rd, Singapore 487372 (SUTD)")
    )
    # Closed routes only. See validate() below -- a false here fails at startup rather than being
    # quietly ignored, because the solver has no open-route model and a flag that silently does
    # nothing is worse than no flag.
    return_to_depot: bool = field(default_factory=lambda: _flag("RETURN_TO_DEPOT", True))
    work_day_start: Time = _time("WORK_DAY_START", "09:00")
    # Three different times the day can be said to end, and they are genuinely different. One
    # setting used to serve all three, which worked only while the last promise was 18:00 and
    # everybody was home long before the day ended. The evening window breaks that: the arrival
    # is legal at 20:59, the service runs past 21:00, and the drive home lands later still.
    #
    # No customer is promised an arrival after this. Clamps every stated and offered window.
    arrival_cutoff: Time = _time("ARRIVAL_CUTOFF", "21:00")
    # Scoring only: work past this -- including the service and the drive home -- is overtime and
    # is penalised. It must sit past a NORMAL completion, or the scorer learns to avoid the
    # evening window: DaySequence.completion_minutes is the last arrival plus service plus the
    # return leg, so a 20:45 arrival routinely completes around 21:25.
    soft_day_end: Time = _time("SOFT_DAY_END", "22:00")
    # The depot's own window. A route that cannot get home by this is infeasible, not expensive.
    # Strictly later than the soft end, or overtime becomes unreachable and the term goes dead.
    hard_route_end: Time = _time("HARD_ROUTE_END", "22:30")
    # Whether a job's SERVICE must finish inside the customer's STATED availability, not merely
    # start in it. On: "I'm home 9 to 9:30" means they are there until 9:30, and a 30-minute job
    # arriving at 9:22 runs straight through a gap they told us about. This is what stops a
    # customer with disjoint windows being visited across the gap between them.
    #
    # It deliberately does NOT apply to a locked window, because that is a different kind of
    # object: a promised delivery slot is an ARRIVAL window -- the van turns up between 5 and 9 --
    # and reserving the service duration inside it would quietly shorten every slot we promise.
    # See solver._normalised_windows.
    require_service_within_window: bool = os.getenv("REQUIRE_SERVICE_WITHIN_WINDOW", "1") != "0"
    # OR-Tools uses guided local search, which burns its whole time budget regardless of when it
    # converges. Publishing a plan happens once and can afford to look harder; candidate
    # evaluation runs many solves per booking, so it gets a much shorter leash.
    solver_time_limit_seconds: int = int(os.getenv("SOLVER_TIME_LIMIT_SECONDS", "5"))
    candidate_solver_time_limit_seconds: int = int(
        os.getenv("CANDIDATE_SOLVER_TIME_LIMIT_SECONDS", "1")
    )


settings = Settings()


class ConfigurationError(RuntimeError):
    """A setting that cannot be honoured. Raised at startup, never swallowed."""


def validate(s: "Settings" = None) -> None:
    """Fail loudly on configuration the code cannot actually deliver.

    Called from the application entry points (webapp, scripts), not at import time, so tests can
    construct a deliberately invalid Settings and assert on the message.
    """
    s = s or settings

    if s.llm_provider not in {"bedrock", "openai", "none"}:
        raise ConfigurationError(
            f"LLM_PROVIDER={s.llm_provider!r} is not supported. Use bedrock, openai, or none."
        )
    if s.routing_provider not in {"google", "onemap", "haversine"}:
        raise ConfigurationError(
            f"ROUTING_PROVIDER={s.routing_provider!r} is not supported. "
            "Use google, onemap, or haversine."
        )

    if not s.return_to_depot:
        raise ConfigurationError(
            "RETURN_TO_DEPOT=false is not supported. solver._solve builds a closed route: the "
            "depot is node 0 and is both the start and the end of the single vehicle, and every "
            "distance matrix, the return leg in DaySequence.round_trip_drive_minutes and the "
            "completion time all assume the van comes home. An open route needs a different "
            "OR-Tools model -- a dummy end node with zero cost to every stop -- which is a "
            "deliberate change, not a flag. Set RETURN_TO_DEPOT=true, or make that change first."
        )

    # The three ends of the day, in order. Each must sit strictly past the one before it.
    if not (s.work_day_start < s.arrival_cutoff):
        raise ConfigurationError(
            f"WORK_DAY_START ({s.work_day_start:%H:%M}) must be before the arrival cutoff "
            f"({s.arrival_cutoff:%H:%M}); there would be no bookable time at all."
        )
    if not (s.arrival_cutoff < s.soft_day_end):
        raise ConfigurationError(
            f"SOFT_DAY_END ({s.soft_day_end:%H:%M}) must be later than the arrival cutoff "
            f"({s.arrival_cutoff:%H:%M}). Overtime is measured on completion -- the last arrival "
            f"plus its service plus the drive home -- so a soft end at the cutoff bills every "
            f"ordinary evening delivery as overtime, and the scorer learns to avoid the evening "
            f"window instead of using it."
        )
    if not (s.soft_day_end < s.hard_route_end):
        raise ConfigurationError(
            f"HARD_ROUTE_END ({s.hard_route_end:%H:%M}) must be later than SOFT_DAY_END "
            f"({s.soft_day_end:%H:%M}). The hard end makes a late route infeasible rather than "
            f"expensive, so with the two equal no feasible route can ever finish past the soft "
            f"end and the overtime term is dead."
        )

    # Singapore's actual bounding box, near enough: 1.15-1.48 N, 103.6-104.1 E. Deliberately tight
    # at the top -- Sembawang is 1.46 and Johor Bahru is 1.49, and a depot across the causeway is
    # exactly the mistake that would otherwise produce plausible-looking, entirely wrong routes.
    if not (1.15 <= s.depot_lat <= 1.48 and 103.6 <= s.depot_lng <= 104.1):
        raise ConfigurationError(
            f"DEPOT_LAT/DEPOT_LNG ({s.depot_lat}, {s.depot_lng}) is outside Singapore. Every "
            f"drive time is measured from here, so a depot in the wrong country silently makes "
            f"every route and every promise wrong rather than failing."
        )
