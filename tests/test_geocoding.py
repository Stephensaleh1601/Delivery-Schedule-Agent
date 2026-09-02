"""Where a customer's coordinate comes from, and what happens when nothing can resolve it.

The point of the geocoder is that one location is stored per customer and then used by the map, the
distance matrix and the solver alike. These tests pin the parts that must hold when the network is
not there -- which is every test run, and possibly the demo.
"""
import sqlite3
from datetime import date

import pytest

from dispatch_agent import config
from dispatch_agent.geo import geocoder
from dispatch_agent.geo.geocoder import (
    SOURCE_DISTRICT,
    SOURCE_ONEMAP,
    GeocodeResult,
    geocode_postal_code,
    seed_cache,
)
from dispatch_agent.models import Coordinates
from dispatch_agent.planning.clock import PlanningClock

BASE = date(2026, 9, 2)


def test_without_a_lookup_it_falls_back_to_the_district_and_says_so(temp_db):
    """Tests run with geocoding disabled, so this is the path they all take. It must produce a
    usable coordinate AND admit that it is approximate."""
    result = geocode_postal_code("018956")

    assert result.source == SOURCE_DISTRICT
    assert result.precise is False
    assert result.coordinates.lat and result.coordinates.lng


def test_a_malformed_postal_code_is_still_rejected(temp_db):
    """The fallback must not swallow a genuinely invalid code -- that is the check that stops a
    bad address being booked at all."""
    with pytest.raises(ValueError):
        geocode_postal_code("999999")


def test_a_cached_coordinate_is_used_without_any_lookup(temp_db):
    """The demo depends on this: seeded real coordinates come back even with the network blocked
    and geocoding switched off."""
    seed_cache({"469123": (1.33096, 103.94704, "22 Bedok Walk")})

    result = geocode_postal_code("469123")

    assert result.source == SOURCE_ONEMAP
    assert result.precise is True
    assert result.coordinates.lat == pytest.approx(1.33096)
    assert result.formatted_address == "22 Bedok Walk"


def test_a_fallback_is_never_cached(temp_db):
    """Caching a district centroid would make it permanent -- the code would never be retried
    once the network came back."""
    geocode_postal_code("018956")

    with sqlite3.connect(config.settings.db_path) as conn:
        conn.executescript(geocoder.SCHEMA)
        rows = conn.execute("SELECT postal_code FROM geocode_cache").fetchall()

    assert rows == []


def test_a_booked_order_stores_where_its_coordinate_came_from(temp_db, monkeypatch):
    """One location per customer, recorded with its provenance, so the UI can mark an approximate
    pin instead of implying precision it does not have."""
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())
    seed_cache({"469123": (1.33096, 103.94704, "22 Bedok Walk")})
    from dispatch_agent.webapp.jobs_service import AvailabilityChoice, OrderSubmission, create_order

    day = PlanningClock.horizon_dates()[0]
    job = create_order(
        OrderSubmission(
            customer_name="Mrs Lee",
            address_raw="Blk 22",
            postal_code="469123",
            availability=[AvailabilityChoice(date=day, window_start="09:00", window_end="13:00")],
        ),
        raw_message="test",
    )

    stored = temp_db.get_job(job.id)
    assert stored.address.geocode_source == SOURCE_ONEMAP
    assert stored.address.precisely_located is True
    assert stored.address.coordinates.lat == pytest.approx(1.33096)
    assert stored.address.formatted_address == "22 Bedok Walk"


def test_an_out_of_area_result_is_refused_even_from_a_real_provider(temp_db, monkeypatch):
    monkeypatch.setattr(
        geocoder, "_from_onemap",
        lambda code, attempts=3: GeocodeResult(Coordinates(lat=3.139, lng=101.687), SOURCE_ONEMAP),
    )
    monkeypatch.setattr(config.settings, "geocoding_enabled", True)
    monkeypatch.setattr(geocoder, "_from_google", lambda code: None)

    # The provider layer itself rejects a point outside Singapore, so this falls through.
    result = geocode_postal_code("018956")
    assert result.source == SOURCE_DISTRICT


# -- the seed -----------------------------------------------------------------


def test_no_two_seeded_customers_share_a_coordinate(temp_db, monkeypatch):
    """The reason this file exists. Four customers used to sit on one identical pixel, one minute
    apart, which makes a map unreadable and hides a newly booked stop completely."""
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import seed_test_clients

    seed_test_clients.seed()

    jobs = temp_db.all_jobs()
    seen: dict[tuple[float, float], list[str]] = {}
    for job in jobs:
        key = (round(job.address.coordinates.lat, 5), round(job.address.coordinates.lng, 5))
        seen.setdefault(key, []).append(job.customer_name)

    collisions = {k: v for k, v in seen.items() if len(v) > 1}
    assert not collisions, f"seeded customers share a coordinate: {collisions}"
    assert len(seen) == len(jobs)


def test_every_seeded_customer_is_at_a_real_address(temp_db, monkeypatch):
    monkeypatch.setattr(config.settings, "demo_base_date", BASE.isoformat())
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import seed_test_clients

    seed_test_clients.seed()

    for job in temp_db.all_jobs():
        assert job.address.precisely_located, f"{job.customer_name} fell back to a district centre"
        assert job.address.formatted_address
