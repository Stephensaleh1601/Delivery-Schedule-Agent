"""Zone centroids for coarse grouping and map defaults.

Placeholder for the Stow team's zone centroid file (see PRD: "Reusing three files from Stow").
Reuses the same district centroids as `postal_codes.py` -- swap in the real Stow zone
definitions if the two systems draw zone boundaries differently.
"""
from __future__ import annotations

from dispatch_agent.geo.postal_codes import DISTRICT_CENTROIDS
from dispatch_agent.models import Coordinates

# Generic island centroid -- used only as a map-default / fallback, not as the depot.
SINGAPORE_CENTROID = Coordinates(lat=1.3521, lng=103.8198)

# Company office / route start-end point: 8 Somapah Rd, Singapore 487372 (SUTD). Rooftop-precision
# coordinates from the Google Geocoding API (2026-08-27), not the coarse district placeholder --
# this is a single fixed real-world point, worth resolving exactly rather than to ~1-2km.
COMPANY_DEPOT_ADDRESS = "8 Somapah Rd, Singapore 487372 (SUTD)"
COMPANY_DEPOT = Coordinates(lat=1.34085, lng=103.9624851)


def zone_for_postal_code(postal_code: str) -> int:
    return int(postal_code[:2])


def zone_centroid(zone_id: int) -> Coordinates:
    return DISTRICT_CENTROIDS.get(zone_id, SINGAPORE_CENTROID)
