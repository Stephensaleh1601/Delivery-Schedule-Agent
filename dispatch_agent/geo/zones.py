"""Zone centroids for coarse grouping and map defaults.

Placeholder for the Stow team's zone centroid file (see PRD: "Reusing three files from Stow").
Reuses the same district centroids as `postal_codes.py` -- swap in the real Stow zone
definitions if the two systems draw zone boundaries differently.
"""
from __future__ import annotations

from dispatch_agent.config import settings
from dispatch_agent.geo.postal_codes import DISTRICT_CENTROIDS
from dispatch_agent.models import Coordinates

# Generic island centroid -- used only as a map-default / fallback, not as the depot.
SINGAPORE_CENTROID = Coordinates(lat=1.3521, lng=103.8198)

def company_depot() -> Coordinates:
    """Where every route starts and ends, read from settings on each call.

    A function rather than a module constant because a Coordinates bound at import time would be
    captured by every `depot: Coordinates = COMPANY_DEPOT` default argument in the codebase, and
    those defaults are evaluated once -- so a DEPOT_LAT override, or a test monkeypatching
    settings, would change the constant and change nothing that uses it. Reading here is the only
    version that actually honours the environment.
    """
    return Coordinates(lat=settings.depot_lat, lng=settings.depot_lng)


def company_depot_address() -> str:
    return settings.depot_address


# Kept for the handful of read-only call sites that only ever want the demo default (map centring,
# the /api/config payload). New code should call company_depot(): this one is frozen at import.
COMPANY_DEPOT_ADDRESS = settings.depot_address
COMPANY_DEPOT = Coordinates(lat=settings.depot_lat, lng=settings.depot_lng)


def zone_for_postal_code(postal_code: str) -> int:
    return int(postal_code[:2])


def zone_centroid(zone_id: int) -> Coordinates:
    return DISTRICT_CENTROIDS.get(zone_id, SINGAPORE_CENTROID)
