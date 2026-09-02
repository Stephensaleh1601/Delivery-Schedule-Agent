"""Service-area and route-plausibility checks, shared by every routing provider.

The PRD calls out one specific failure: a Singapore-focused router occasionally returns a route
that dips across the Straits into Johor for what should be a short local hop, usually near the
Woodlands/Tuas checkpoints. Geometrically valid, operationally nonsense, and it "will happen
during a demo otherwise".

The check used to live only on the OneMap path, which reads the route's own turn-by-turn
coordinates. Google's Distance Matrix API returns **no geometry at all** -- only a duration and a
distance -- so the same check is impossible there, and the default provider was running with no
protection whatsoever.

What is possible on both is a plausibility test: a drive time wildly out of proportion to the
straight-line distance is not a real local trip. That catches the Johor detour, and also catches
a mis-parsed response or a wrong-continent coordinate. It is a heuristic, deliberately loose --
it exists to reject the absurd, not to second-guess normal traffic.
"""
from __future__ import annotations

from dispatch_agent.config import settings
from dispatch_agent.models import Coordinates

# Singapore mainland bounding box, generous margin. A point outside this is in Johor, Batam, or
# an offshore island -- none of which is a same-day install stop.
SG_LAT_MIN, SG_LAT_MAX = 1.13, 1.47
SG_LNG_MIN, SG_LNG_MAX = 103.60, 104.05


class OutsideServiceAreaError(ValueError):
    """A delivery location is not in Singapore. Raised at booking, on every front door, so the
    customer finds out immediately rather than the router silently returning a cross-border
    drive time for a job nobody can actually service."""


def is_in_singapore(point: Coordinates) -> bool:
    return SG_LAT_MIN <= point.lat <= SG_LAT_MAX and SG_LNG_MIN <= point.lng <= SG_LNG_MAX


def validate_delivery_location(point: Coordinates, label: str = "That address") -> None:
    if not is_in_singapore(point):
        raise OutsideServiceAreaError(
            f"{label} is outside our Singapore delivery area."
        )


def is_plausible_leg(origin: Coordinates, destination: Coordinates, minutes: int) -> bool:
    """Whether a provider's drive time is believable for this pair.

    Compared against the straight-line estimate rather than an absolute threshold, so a genuinely
    long cross-island trip is judged on the same terms as a short one. The multiplier is
    deliberately generous (default 4x): normal congestion should never trip this, a detour
    through another country always will.
    """
    from dispatch_agent.geo.routing_client import haversine_drive_minutes  # circular at import

    return minutes <= max(1, haversine_drive_minutes(origin, destination)) * settings.max_drive_time_ratio


def route_geometry_stays_in_singapore(points) -> bool:
    """True if every sampled point of a route's geometry is inside Singapore.

    Used where the provider actually returns geometry (OneMap). Missing or unparseable geometry
    is treated as a pass -- this rejects routes we can prove leave the country, it does not
    reject routes we simply cannot see.
    """
    for lat, lng in points:
        if not (SG_LAT_MIN <= lat <= SG_LAT_MAX and SG_LNG_MIN <= lng <= SG_LNG_MAX):
            return False
    return True


def onemap_instruction_points(onemap_response: dict):
    """Yield (lat, lng) from OneMap's turn-by-turn `route_instructions`, whose entries carry an
    unencoded "lat,lng" string at index 3. Sampling those catches a Johor detour without needing
    a full polyline decode of `route_geometry`."""
    for leg in onemap_response.get("route_instructions") or []:
        try:
            lat_str, lng_str = str(leg[3]).split(",")
            yield float(lat_str), float(lng_str)
        except (IndexError, TypeError, ValueError):
            continue
