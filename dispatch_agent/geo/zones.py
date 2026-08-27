"""Zone centroids for coarse grouping and map defaults.

Placeholder for the Stow team's zone centroid file (see PRD: "Reusing three files from Stow").
Reuses the same district centroids as `postal_codes.py` -- swap in the real Stow zone
definitions if the two systems draw zone boundaries differently.
"""
from __future__ import annotations

from dispatch_agent.geo.postal_codes import DISTRICT_CENTROIDS
from dispatch_agent.models import Coordinates

# Used as the depot / route start-end point and the dashboard's default map center.
SINGAPORE_CENTROID = Coordinates(lat=1.3521, lng=103.8198)


def zone_for_postal_code(postal_code: str) -> int:
    return int(postal_code[:2])


def zone_centroid(zone_id: int) -> Coordinates:
    return DISTRICT_CENTROIDS.get(zone_id, SINGAPORE_CENTROID)
