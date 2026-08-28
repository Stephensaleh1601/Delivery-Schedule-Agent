"""Drive-time/distance client, with a filter for routes that stray into Johor and a
network-free fallback. Three providers:

- "google": Google Maps Platform's Distance Matrix API -- needs GOOGLE_MAPS_API_KEY, a plain
  Maps API key (not a GCP service account). Returns both drive time and distance in one call.
- "onemap": Singapore's free government routing API (https://www.onemap.gov.sg), which needs
  no billing account -- a good fit for a hackathon demo restricted to Singapore.
- "haversine": a zero-dependency straight-line fallback (great-circle distance over an assumed
  average speed), used automatically when the selected provider has no credentials configured
  or its API call fails, so the rest of the system -- and the whole test suite -- keeps working
  offline.

The Johor filter (OneMap only): a Singapore-focused router will occasionally return a route
that dips across the Straits into Johor, Malaysia for what should be a short local hop, usually
near the Woodlands/Tuas checkpoints. That route is geometrically "valid" but wrong for a
same-day domestic run. We reject any OneMap route that leaves Singapore's bounding box and fall
back to the haversine estimate for that pair instead of surfacing a cross-border drive time.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import requests

from dispatch_agent.config import settings
from dispatch_agent.models import Coordinates

# Singapore mainland bounding box, generous margin. A route leaving this box is heading to
# Johor or an offshore island, neither of which is a same-day install stop.
SG_LAT_MIN, SG_LAT_MAX = 1.13, 1.47
SG_LNG_MIN, SG_LNG_MAX = 103.60, 104.05

AVERAGE_URBAN_SPEED_KMH = 30.0


def _in_singapore(lat: float, lng: float) -> bool:
    return SG_LAT_MIN <= lat <= SG_LAT_MAX and SG_LNG_MIN <= lng <= SG_LNG_MAX


def haversine_km(a: Coordinates, b: Coordinates) -> float:
    r = 6371.0
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = math.radians(b.lat - a.lat)
    dlng = math.radians(b.lng - a.lng)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def haversine_drive_minutes(a: Coordinates, b: Coordinates) -> int:
    km = haversine_km(a, b) * 1.3  # road-distance fudge factor over straight-line distance
    return max(1, round(km / AVERAGE_URBAN_SPEED_KMH * 60))


class RoutingClient:
    """Drive time between two points, with the Johor-route filter and a haversine fallback."""

    def __init__(self, provider: str | None = None):
        self.provider = provider or settings.routing_provider
        # A pre-issued static token (settings.onemap_token) skips the email/password exchange
        # below entirely. OneMap tokens are long-lived (issued for ~3 days), so treat it as
        # already "fetched" and never refresh it ourselves -- if it expires, calls fail closed
        # and we fall back to haversine rather than trying to re-authenticate with it.
        self._onemap_token: str | None = settings.onemap_token or None
        self._onemap_token_expiry: datetime | None = (
            datetime.max if settings.onemap_token else None
        )

    def drive_minutes(self, origin: Coordinates, destination: Coordinates) -> int:
        route = self._route(origin, destination)
        return route["minutes"] if route else haversine_drive_minutes(origin, destination)

    def distance_km(self, origin: Coordinates, destination: Coordinates) -> float:
        """Drive distance in km, same provider/fallback logic as drive_minutes -- used for
        display (e.g. the route planner's per-leg breakdown), never by the solver itself."""
        route = self._route(origin, destination)
        return route["km"] if route else round(haversine_km(origin, destination) * 1.3, 2)

    def matrix(self, points: list[Coordinates]) -> list[list[int]]:
        """Full drive-time matrix in minutes, points[i] -> points[j]."""
        if self.provider == "google" and settings.google_maps_api_key and len(points) > 1:
            batched = self._google_matrix(points)
            if batched is not None:
                return batched
        return [
            [0 if i == j else self.drive_minutes(a, b) for j, b in enumerate(points)]
            for i, a in enumerate(points)
        ]

    def leg_distances(self, ordered_points: list[Coordinates]) -> list[dict]:
        """{"minutes": int, "km": float} for each consecutive leg in ordered_points (e.g.
        depot -> stop1 -> stop2 -> ...) -- batched requests instead of one call per leg, which
        is what made a 10-stop route-plan display take the better part of a minute and a half.
        Used for the route-plan/admin-map display, never by the solver (which needs the full
        points-by-points matrix from matrix() instead)."""
        if len(ordered_points) < 2:
            return []
        if self.provider == "google" and settings.google_maps_api_key:
            grid = self._google_distance_matrix(ordered_points[:-1], ordered_points[1:])
            if grid is not None:
                return [grid[i][i] for i in range(len(grid))]
        legs = []
        for i in range(len(ordered_points) - 1):
            origin, destination = ordered_points[i], ordered_points[i + 1]
            route = self._route(origin, destination)
            legs.append(
                route
                or {
                    "minutes": haversine_drive_minutes(origin, destination),
                    "km": round(haversine_km(origin, destination) * 1.3, 2),
                }
            )
        return legs

    def _route(self, origin: Coordinates, destination: Coordinates) -> dict | None:
        """{"minutes": int, "km": float} for one leg via the configured provider, or None if
        it's unconfigured/unreachable -- callers fall back to the haversine estimate."""
        if self.provider == "google" and settings.google_maps_api_key:
            info = self._google_route(origin, destination)
            if info is not None:
                return {
                    "minutes": max(1, round(info["duration_seconds"] / 60)),
                    "km": round(info["distance_meters"] / 1000, 2),
                }
            return None

        has_onemap_auth = settings.onemap_token or (settings.onemap_email and settings.onemap_password)
        if self.provider == "onemap" and has_onemap_auth:
            summary = self._onemap_route(origin, destination)
            if summary is not None:
                try:
                    return {
                        "minutes": max(1, round(int(summary["total_time"]) / 60)),
                        "km": round(float(summary["total_distance"]) / 1000, 2),
                    }
                except (KeyError, ValueError, TypeError):
                    pass
        return None

    # -- Google Maps Distance Matrix ---------------------------------------------

    def _google_route(self, origin: Coordinates, destination: Coordinates) -> dict | None:
        """{"duration_seconds": int, "distance_meters": int} for one leg, or None on any
        failure (bad key, no route, quota, network) -- caller falls back to haversine."""
        try:
            resp = requests.get(
                "https://maps.googleapis.com/maps/api/distancematrix/json",
                params={
                    "origins": f"{origin.lat},{origin.lng}",
                    "destinations": f"{destination.lat},{destination.lng}",
                    "mode": "driving",
                    "key": settings.google_maps_api_key,
                },
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("status") != "OK":
                return None
            element = body["rows"][0]["elements"][0]
            if element.get("status") != "OK":
                return None
            return {
                "duration_seconds": int(element["duration"]["value"]),
                "distance_meters": int(element["distance"]["value"]),
            }
        except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
            return None

    # Google's Distance Matrix API caps elements (origins x destinations) per request --
    # documented as up to 25 origins/destinations, but the actual enforced cap on the element
    # *product* is much lower (observed: an 11x11 = 121-element request is rejected outright,
    # silently falling back to ~110 sequential pairwise calls -- which is what made a 10-stop
    # day take 80+ seconds before this). Chunking origins keeps each request's element count
    # under this cap regardless of how many stops are in a day.
    _MAX_ELEMENTS_PER_REQUEST = 100

    def _google_matrix(self, points: list[Coordinates]) -> list[list[int]] | None:
        """Full drive-time matrix (minutes) for the solver, built from _google_distance_matrix."""
        grid = self._google_distance_matrix(points, points)
        if grid is None:
            return None
        return [[0 if i == j else grid[i][j]["minutes"] for j in range(len(points))] for i in range(len(points))]

    def _google_distance_matrix(
        self, origins: list[Coordinates], destinations: list[Coordinates]
    ) -> list[list[dict]] | None:
        """Full origins x destinations grid of {"minutes": int, "km": float}, chunked over
        origins into multiple requests so no single request exceeds the element cap. Returns
        None (triggering the caller's pairwise/haversine fallback) only if a request fails
        outright -- an individual element's own failure status still falls back to haversine
        for just that pair, same as the single-pair path."""
        n_destinations = len(destinations)
        if not origins or not n_destinations:
            return []

        batch_size = max(1, self._MAX_ELEMENTS_PER_REQUEST // n_destinations)
        destinations_str = "|".join(f"{p.lat},{p.lng}" for p in destinations)
        grid: list[list[dict] | None] = [None] * len(origins)

        for start in range(0, len(origins), batch_size):
            batch = origins[start : start + batch_size]
            origins_str = "|".join(f"{p.lat},{p.lng}" for p in batch)
            try:
                resp = requests.get(
                    "https://maps.googleapis.com/maps/api/distancematrix/json",
                    params={
                        "origins": origins_str,
                        "destinations": destinations_str,
                        "mode": "driving",
                        "key": settings.google_maps_api_key,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                body = resp.json()
                if body.get("status") != "OK":
                    return None
                rows = body["rows"]
                if len(rows) != len(batch):
                    return None

                for bi, row_data in enumerate(rows):
                    i = start + bi
                    elements = row_data["elements"]
                    if len(elements) != n_destinations:
                        return None
                    row: list[dict] = []
                    for j, element in enumerate(elements):
                        if element.get("status") == "OK":
                            row.append(
                                {
                                    "minutes": max(1, round(int(element["duration"]["value"]) / 60)),
                                    "km": round(int(element["distance"]["value"]) / 1000, 2),
                                }
                            )
                        else:
                            row.append(
                                {
                                    "minutes": haversine_drive_minutes(origins[i], destinations[j]),
                                    "km": round(haversine_km(origins[i], destinations[j]) * 1.3, 2),
                                }
                            )
                    grid[i] = row
            except (requests.RequestException, KeyError, ValueError, TypeError, IndexError):
                return None

        return grid

    # -- OneMap -----------------------------------------------------------------

    def _onemap_auth_token(self) -> str | None:
        if self._onemap_token and self._onemap_token_expiry and datetime.utcnow() < self._onemap_token_expiry:
            return self._onemap_token
        try:
            resp = requests.post(
                "https://www.onemap.gov.sg/api/auth/post/getToken",
                json={"email": settings.onemap_email, "password": settings.onemap_password},
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
            self._onemap_token = body["access_token"]
            self._onemap_token_expiry = datetime.utcnow() + timedelta(hours=2)
            return self._onemap_token
        except (requests.RequestException, KeyError, ValueError):
            return None

    def _onemap_route(self, origin: Coordinates, destination: Coordinates) -> dict | None:
        """route_summary dict (total_time in seconds, total_distance in metres) for one leg,
        or None on any failure/Johor-detour -- callers fall back to the haversine estimate."""
        token = self._onemap_auth_token()
        if not token:
            return None
        try:
            resp = requests.get(
                "https://www.onemap.gov.sg/api/public/routingsvc/route",
                params={
                    "start": f"{origin.lat},{origin.lng}",
                    "end": f"{destination.lat},{destination.lng}",
                    "routeType": "drive",
                },
                headers={"Authorization": token},
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
            if not self._route_stays_in_singapore(body):
                return None  # the Johor detour -- fall back to haversine for this pair
            return body["route_summary"]
        except (requests.RequestException, KeyError, ValueError, TypeError):
            return None

    @staticmethod
    def _route_stays_in_singapore(onemap_response: dict) -> bool:
        # OneMap's turn-by-turn `route_instructions` entries carry unencoded lat/lng at
        # index 3 -- sampling those is enough to catch a Johor detour without needing a full
        # polyline decode of `route_geometry`.
        instructions = onemap_response.get("route_instructions")
        if not instructions:
            return True  # nothing to check against, don't reject on missing data
        for leg in instructions:
            try:
                lat_str, lng_str = str(leg[3]).split(",")
                lat, lng = float(lat_str), float(lng_str)
            except (IndexError, TypeError, ValueError):
                continue
            if not _in_singapore(lat, lng):
                return False
        return True
