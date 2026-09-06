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

The Johor filter: a Singapore-focused router will occasionally return a route that dips across
the Straits into Johor, Malaysia for what should be a short local hop, usually near the
Woodlands/Tuas checkpoints. Geometrically "valid", wrong for a same-day domestic run. Both
providers are now protected, by different means (see geo/sanity.py):

- OneMap returns turn-by-turn coordinates, so its route is rejected if any of them leave
  Singapore's bounding box.
- Google's Distance Matrix returns no geometry at all -- only a duration and a distance -- so
  there is nothing to inspect for a border crossing. Instead each leg is checked against the
  straight-line estimate and rejected if wildly out of proportion. A heuristic, but until this
  the default provider had no protection at all.

Either way a rejected leg falls back to the haversine estimate for that pair only.

Every drive time goes through the module-level cache in geo/matrix_cache.py, so a pair is
fetched at most once per process (and once per database, if it persists). This is what makes
evaluating a booking across four candidate dates affordable -- see that module for the details.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import requests

from dispatch_agent.config import settings
from dispatch_agent.geo.matrix_cache import MATRIX_CACHE, cover_misses
from dispatch_agent.geo.sanity import (
    is_plausible_leg,
    onemap_instruction_points,
    route_geometry_stays_in_singapore,
)
from dispatch_agent.models import Coordinates

AVERAGE_URBAN_SPEED_KMH = 30.0


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
        """Full drive-time matrix in minutes, points[i] -> points[j].

        Everything already in the cache is free; only the genuinely unknown pairs are fetched,
        grouped into as few provider requests as possible. A day that has been solved once costs
        nothing to solve again, which is what makes candidate evaluation across four dates
        affordable.
        """
        pairs = [(a, b) for i, a in enumerate(points) for j, b in enumerate(points) if i != j]
        if pairs:
            self._fill_cache(pairs)
        return [
            [0 if i == j else self._cached_leg(a, b)["minutes"] for j, b in enumerate(points)]
            for i, a in enumerate(points)
        ]

    def leg(self, origin: Coordinates, destination: Coordinates) -> dict:
        """One leg as {"minutes", "km"}, from the cache -- no provider call, ever.

        The public face of `_cached_leg`, for callers that ask about the same points hundreds of
        times. `drive_minutes` and `distance_km` each make their own provider request, which is
        right for a one-off display figure and ruinous for a search: the insertion search asked
        236 times per run and spent 34 seconds doing it.

        Warm the cache first with `matrix(points)` -- one batched request covering every pair --
        and then every lookup here is free. Uncached pairs fall back to the haversine estimate
        rather than reaching out, so this can never surprise a caller with latency.
        """
        return self._cached_leg(origin, destination)

    def _cached_leg(self, origin: Coordinates, destination: Coordinates) -> dict:
        """The cached value for one leg, falling back to haversine if the provider never
        supplied one (unconfigured, unreachable, or the pair was rejected as implausible)."""
        cached = MATRIX_CACHE.peek(self.provider, origin, destination)
        if cached is not None:
            return cached
        return {
            "minutes": haversine_drive_minutes(origin, destination),
            "km": round(haversine_km(origin, destination) * 1.3, 2),
        }

    def _fill_cache(self, pairs: list[tuple[Coordinates, Coordinates]]) -> None:
        """Fetch whatever `pairs` the cache does not already hold, in as few requests as we can."""
        _hits, misses = MATRIX_CACHE.split(self.provider, pairs)
        if not misses:
            return

        if self.provider == "google" and settings.google_maps_api_key:
            for origins, destinations in cover_misses(misses):
                grid = self._google_distance_matrix(origins, destinations)
                if grid is None:
                    continue  # whole rectangle failed -- those pairs stay haversine
                # Clamp on the way into the cache, so nothing implausible can be stored no
                # matter which code path produced it.
                MATRIX_CACHE.put_many(
                    self.provider,
                    [
                        (
                            origins[i],
                            destinations[j],
                            self._sane_leg(
                                origins[i],
                                destinations[j],
                                minutes=grid[i][j]["minutes"],
                                km=grid[i][j]["km"],
                            ),
                        )
                        for i in range(len(origins))
                        for j in range(len(destinations))
                    ],
                )
                MATRIX_CACHE.record_fetch(len(origins) * len(destinations))
            return

        # OneMap and haversine have no batch endpoint: one call per missing pair, but at least
        # only for the pairs we actually lack.
        for origin, destination in misses:
            route = self._route(origin, destination)
            if route is not None:
                MATRIX_CACHE.put(self.provider, origin, destination, route)
                MATRIX_CACHE.record_fetch(1)

    def leg_distances(self, ordered_points: list[Coordinates]) -> list[dict]:
        """{"minutes": int, "km": float} for each consecutive leg in ordered_points (e.g.
        depot -> stop1 -> stop2 -> ...) -- batched requests instead of one call per leg, which
        is what made a 10-stop route-plan display take the better part of a minute and a half.
        Used for the route-plan/admin-map display, never by the solver (which needs the full
        points-by-points matrix from matrix() instead)."""
        if len(ordered_points) < 2:
            return []
        # Previously this asked the provider for an (n-1)x(n-1) grid and kept only the diagonal --
        # (n-1)^2 billable elements to obtain n-1 legs. Going through the cache asks for exactly
        # the n-1 pairs needed, and after a solve over the same points it asks for nothing at all.
        legs = [(ordered_points[i], ordered_points[i + 1]) for i in range(len(ordered_points) - 1)]
        self._fill_cache(legs)
        return [self._cached_leg(origin, destination) for origin, destination in legs]

    def _sane_leg(self, origin: Coordinates, destination: Coordinates, minutes: int, km: float) -> dict:
        """Accept a provider's numbers, or substitute the straight-line estimate when they are
        not believable for this pair.

        This is the Johor filter for providers that return no geometry. Google's Distance Matrix
        gives only a duration and a distance, so there is nothing to inspect for a border
        crossing -- but a local hop that comes back four times longer than the straight-line
        estimate is not a local hop. Heuristic by construction; it rejects the absurd rather than
        second-guessing traffic.
        """
        if is_plausible_leg(origin, destination, minutes):
            return {"minutes": minutes, "km": km}
        return {
            "minutes": haversine_drive_minutes(origin, destination),
            "km": round(haversine_km(origin, destination) * 1.3, 2),
        }

    def _route(self, origin: Coordinates, destination: Coordinates) -> dict | None:
        """{"minutes": int, "km": float} for one leg via the configured provider, or None if
        it's unconfigured/unreachable -- callers fall back to the haversine estimate."""
        if self.provider == "google" and settings.google_maps_api_key:
            info = self._google_route(origin, destination)
            if info is not None:
                return self._sane_leg(
                    origin,
                    destination,
                    minutes=max(1, round(info["duration_seconds"] / 60)),
                    km=round(info["distance_meters"] / 1000, 2),
                )
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
                            # Same plausibility clamp as the single-leg path -- a cross-border
                            # detour is just as likely to appear inside a batched grid.
                            row.append(
                                self._sane_leg(
                                    origins[i],
                                    destinations[j],
                                    minutes=max(1, round(int(element["duration"]["value"]) / 60)),
                                    km=round(int(element["distance"]["value"]) / 1000, 2),
                                )
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
        """Geometry-based Johor check. Kept as a thin wrapper so the bounding box and the
        traversal live in geo/sanity.py alongside the checks the Google path uses."""
        return route_geometry_stays_in_singapore(onemap_instruction_points(onemap_response))
