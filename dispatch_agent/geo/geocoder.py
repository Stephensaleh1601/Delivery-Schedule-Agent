"""Turning a Singapore postal code into a real coordinate, once, and keeping it.

Why this exists: `postal_codes.postal_code_to_coords` resolves a postal *sector* to one of 28
district centroids. That is fine for a rough sequencing estimate and useless for anything a person
looks at -- every customer in Bedok lands on the identical pixel, and the drive time between two of
them comes out as the one-minute floor. A map drawn from it looks broken, and a newly booked stop is
invisible because it sits exactly on top of an existing one.

So a coordinate is resolved properly and stored with the order. The same coordinate is then used by
the map, the distance matrix and the solver -- one location per customer, not one per consumer.

Three sources, in order:

1. **OneMap** (`onemap.gov.sg`) -- Singapore's government address database. Free, no key, and it
   resolves a bare postal code to the actual building: 469123 is 22 Bedok Walk, not "district 16".
   Unauthenticated callers are rate-limited, hence the backoff and the cache.
2. **Google Geocoding** -- reliable, but returns `APPROXIMATE` for a bare postal code, so it is the
   second choice rather than the first.
3. **District centroid** -- the old behaviour, kept as a last resort and recorded as such so the UI
   can say "approximate" instead of quietly implying a precision it does not have.

Results persist in `geocode_cache`, so a code is fetched once per database and a demo re-run costs
nothing. Tests block the network entirely, which means they exercise layer 3 -- deliberately, since
that is the path that must stay correct when the internet is not there.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from dispatch_agent.config import settings
from dispatch_agent.geo.postal_codes import postal_code_to_coords
from dispatch_agent.geo.sanity import is_in_singapore
from dispatch_agent.models import Coordinates

ONEMAP_SEARCH = "https://www.onemap.gov.sg/api/common/elastic/search"
GOOGLE_GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"

# Sources, most precise first. Stored with the result so a coordinate always says where it came from.
SOURCE_ONEMAP = "onemap"
SOURCE_GOOGLE = "google"
SOURCE_DISTRICT = "district_centroid"

PRECISE_SOURCES = frozenset({SOURCE_ONEMAP, SOURCE_GOOGLE})

SCHEMA = """
CREATE TABLE IF NOT EXISTS geocode_cache (
    postal_code TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    source TEXT NOT NULL,
    formatted_address TEXT,
    fetched_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class GeocodeResult:
    coordinates: Coordinates
    source: str
    formatted_address: str | None = None

    @property
    def precise(self) -> bool:
        """False when we fell back to a district centre, which is accurate to a kilometre or two."""
        return self.source in PRECISE_SOURCES


# -- cache --------------------------------------------------------------------


def _db_path() -> Path:
    return Path(settings.db_path)


def _cached(postal_code: str) -> GeocodeResult | None:
    path = _db_path()
    if not path.exists():
        return None
    try:
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)
            row = conn.execute(
                "SELECT lat, lng, source, formatted_address FROM geocode_cache WHERE postal_code = ?",
                (postal_code,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return GeocodeResult(Coordinates(lat=row[0], lng=row[1]), source=row[2], formatted_address=row[3])


def _remember(postal_code: str, result: GeocodeResult) -> None:
    """Only real lookups are cached. Caching a fallback would make it permanent -- the code would
    never be retried once the network came back."""
    if not result.precise:
        return
    try:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO geocode_cache "
                "(postal_code, lat, lng, source, formatted_address, fetched_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(postal_code) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, "
                "source=excluded.source, formatted_address=excluded.formatted_address, "
                "fetched_at=excluded.fetched_at",
                (
                    postal_code,
                    result.coordinates.lat,
                    result.coordinates.lng,
                    result.source,
                    result.formatted_address,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    except sqlite3.Error:
        pass  # a cache that cannot persist is still a working geocoder


# -- providers ----------------------------------------------------------------


def _from_onemap(postal_code: str, attempts: int = 3) -> GeocodeResult | None:
    """Singapore's own address database. Rate-limited without a token, so retry with backoff --
    a throttled response comes back as non-JSON rather than an HTTP error."""
    for attempt in range(attempts):
        try:
            body = requests.get(
                ONEMAP_SEARCH,
                params={"searchVal": postal_code, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": 1},
                timeout=15,
            ).json()
            results = body.get("results") or []
            if not results:
                return None
            top = results[0]
            point = Coordinates(lat=float(top["LATITUDE"]), lng=float(top["LONGITUDE"]))
            return GeocodeResult(point, SOURCE_ONEMAP, top.get("ADDRESS"))
        except (requests.RequestException, ValueError, KeyError, TypeError):
            if attempt + 1 < attempts:
                time.sleep(0.6 * (attempt + 1))
    return None


def _from_google(postal_code: str) -> GeocodeResult | None:
    if not settings.google_maps_api_key:
        return None
    try:
        body = requests.get(
            GOOGLE_GEOCODE,
            params={
                "address": f"Singapore {postal_code}",
                "components": "country:SG",
                "key": settings.google_maps_api_key,
            },
            timeout=15,
        ).json()
        if body.get("status") != "OK" or not body.get("results"):
            return None
        top = body["results"][0]
        location = top["geometry"]["location"]
        point = Coordinates(lat=float(location["lat"]), lng=float(location["lng"]))
        return GeocodeResult(point, SOURCE_GOOGLE, top.get("formatted_address"))
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


# -- entry point --------------------------------------------------------------


def geocode_postal_code(postal_code: str) -> GeocodeResult:
    """Resolve a 6-digit Singapore postal code to a coordinate.

    Never raises for an unresolvable code: it falls back to the postal district's centre and says
    so. An unknown *sector* is a different matter -- that is a malformed code, and
    `postal_code_to_coords` raises, which is what rejects it at booking.
    """
    cached = _cached(postal_code)
    if cached is not None:
        return cached

    if settings.geocoding_enabled:
        for provider in (_from_onemap, _from_google):
            result = provider(postal_code)
            # Checked here rather than inside each provider, so a source added later cannot skip
            # it. A geocoder that confidently returns Kuala Lumpur is worse than one that admits
            # it does not know.
            if result is not None and is_in_singapore(result.coordinates):
                _remember(postal_code, result)
                return result

    # Last resort. Raises for a sector we do not recognise, which is the validation we want.
    return GeocodeResult(postal_code_to_coords(postal_code), SOURCE_DISTRICT)


def seed_cache(entries: dict[str, tuple[float, float, str]]) -> None:
    """Prime the cache with coordinates resolved earlier and committed to the repo.

    Lets the demo seed produce real addresses without a network round trip, and keeps a seeded run
    byte-identical every time.
    """
    for postal_code, (lat, lng, address) in entries.items():
        _remember(
            postal_code,
            GeocodeResult(Coordinates(lat=lat, lng=lng), SOURCE_ONEMAP, address),
        )
