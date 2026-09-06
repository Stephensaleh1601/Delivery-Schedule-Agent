"""Cost behaviour of the drive-time cache, and the route-sanity checks around it.

These tests assert *how much a provider is asked for*, not just what comes back. That is the
whole point of the cache: candidate evaluation solves the same days repeatedly, and without
these guarantees a single booking would fan out into dozens of billed requests.

A counting fake stands in for Google so the numbers are exact and nothing touches the network.
"""
from datetime import date, time
from time import monotonic

import pytest

from dispatch_agent import config
from dispatch_agent.geo.matrix_cache import MATRIX_CACHE, cover_misses
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.geo.routing_client import RoutingClient, haversine_drive_minutes
from dispatch_agent.geo.sanity import OutsideServiceAreaError, is_plausible_leg, validate_delivery_location
from dispatch_agent.models import Address, Coordinates, JobType, TimeWindow
from dispatch_agent.webapp.jobs_service import JobSubmission, JobSubmissionError, _validated_fields

POINTS = [
    Coordinates(lat=1.2837, lng=103.8517),
    Coordinates(lat=1.3350, lng=103.7050),
    Coordinates(lat=1.3600, lng=103.9800),
    Coordinates(lat=1.3150, lng=103.8100),
]


class CountingGoogle(RoutingClient):
    """A google-provider client whose batched grid call is recorded rather than sent."""

    def __init__(self):
        super().__init__(provider="google")
        self.requests: list[tuple[int, int]] = []

    @property
    def elements(self) -> int:
        return sum(o * d for o, d in self.requests)

    def _google_distance_matrix(self, origins, destinations):
        self.requests.append((len(origins), len(destinations)))
        return [
            [{"minutes": 10 + i + j, "km": 5.0 + i + j} for j in range(len(destinations))]
            for i in range(len(origins))
        ]


@pytest.fixture()
def google(monkeypatch):
    monkeypatch.setattr(config.settings, "routing_provider", "google")
    monkeypatch.setattr(config.settings, "google_maps_api_key", "test-key")
    MATRIX_CACHE.clear()
    return CountingGoogle()


def test_cold_matrix_is_one_batched_request(google):
    google.matrix(POINTS)
    assert len(google.requests) == 1, f"expected one rectangle, got {google.requests}"
    assert google.elements == len(POINTS) * len(POINTS)


def test_cache_persistence_reuses_the_active_write_transaction(temp_db):
    """A confirmation must not wait on a second SQLite writer behind its own transaction."""
    MATRIX_CACHE.clear()
    started = monotonic()
    with temp_db.transaction():
        MATRIX_CACHE.put("google", POINTS[0], POINTS[1], {"minutes": 12, "km": 4.2})
    elapsed = monotonic() - started

    assert elapsed < 1.0, f"cache write deadlocked behind the active transaction for {elapsed:.2f}s"
    assert MATRIX_CACHE.peek("google", POINTS[0], POINTS[1]) == {"minutes": 12, "km": 4.2}


def test_resolving_the_same_day_again_costs_nothing(google):
    google.matrix(POINTS)
    before = len(google.requests)
    google.matrix(POINTS)
    assert len(google.requests) == before, "a second solve of the same day re-fetched drive times"


def test_adding_one_candidate_to_a_known_day_costs_2n_not_n_squared(google):
    """The shape candidate evaluation actually takes: a day already solved, plus one prospective
    stop. Only the new point's legs are unknown, so the cost is linear in the day, not quadratic."""
    google.matrix(POINTS)
    google.requests.clear()

    candidate = Coordinates(lat=1.4300, lng=103.7900)
    google.matrix(POINTS + [candidate])

    # Two rectangles: candidate -> everyone, and everyone -> candidate.
    assert len(google.requests) == 2, f"expected 2 rectangles, got {google.requests}"
    assert google.elements == 2 * len(POINTS)


def test_leg_distances_after_a_solve_is_free(google):
    """Display used to cost (n-1)^2 elements to obtain n-1 legs. After a solve over the same
    points it should now cost nothing at all."""
    google.matrix(POINTS)
    google.requests.clear()

    legs = google.leg_distances(POINTS)

    assert len(legs) == len(POINTS) - 1
    assert google.requests == [], "route display re-fetched drive times the solver already had"


def test_leg_distances_alone_fetches_only_the_legs_it_needs(google):
    google.leg_distances(POINTS)
    assert google.elements <= 2 * (len(POINTS) - 1), (
        f"asked for {google.elements} elements to obtain {len(POINTS) - 1} legs"
    )


def test_cover_misses_groups_shared_destinations():
    a, b, c = POINTS[0], POINTS[1], POINTS[2]
    rectangles = cover_misses([(a, c), (b, c)])
    assert len(rectangles) == 1
    origins, destinations = rectangles[0]
    assert len(origins) == 2 and len(destinations) == 1


# -- route sanity -------------------------------------------------------------


def test_implausible_drive_time_falls_back_to_the_straight_line_estimate(google, monkeypatch):
    """A Johor detour has no geometry to inspect on the Google path, so it shows up as a drive
    time wildly out of proportion to the distance. It must not reach the solver."""
    near_woodlands = Coordinates(lat=1.4400, lng=103.7900)
    also_north = Coordinates(lat=1.4400, lng=103.8200)
    straight = haversine_drive_minutes(near_woodlands, also_north)

    monkeypatch.setattr(
        google, "_google_distance_matrix",
        lambda o, d: [[{"minutes": straight * 20, "km": 400.0} for _ in d] for _ in o],
    )
    matrix = google.matrix([near_woodlands, also_north])

    assert matrix[0][1] == straight, "an absurd cross-border drive time was accepted"


def test_plausible_drive_time_is_kept(google):
    """The clamp must not second-guess ordinary traffic -- only reject the absurd."""
    a, b = POINTS[0], POINTS[1]
    congested = haversine_drive_minutes(a, b) * 2
    assert is_plausible_leg(a, b, congested)


def test_out_of_singapore_location_is_rejected_at_booking():
    """Every front door funnels through _validated_fields, so one check covers REST, chat and
    the admin edit form."""
    payload = JobSubmission(
        customer_name="Overseas", address_raw="Somewhere else", postal_code="123456",
        job_type=JobType.SOFA, delivery_date=date(2026, 9, 10),
        window_start=time(9, 0), window_end=time(12, 0),
    )
    kuala_lumpur = Coordinates(lat=3.1390, lng=101.6869)

    import dispatch_agent.webapp.jobs_service as service
    from dispatch_agent.geo.geocoder import GeocodeResult

    original = service.geocode_postal_code
    service.geocode_postal_code = lambda code: GeocodeResult(kuala_lumpur, "onemap", "Somewhere else")
    try:
        with pytest.raises(JobSubmissionError, match="outside our Singapore delivery area"):
            _validated_fields(payload)
    finally:
        service.geocode_postal_code = original


def test_singapore_location_passes_validation():
    validate_delivery_location(postal_code_to_coords("018956"))
