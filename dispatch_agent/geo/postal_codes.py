"""Singapore postal-code geocoding.

Placeholder for the Stow team's postal-code-to-coordinates map (see PRD: "Reusing three files
from Stow"). That file isn't in this repo -- drop it in as a replacement for
`DISTRICT_CENTROIDS` / `postal_code_to_coords` before relying on this for anything beyond the
demo. Until then this falls back to Singapore's 28 postal districts, keyed by the first two
digits of the 6-digit code, which is precise to roughly one district (~1-2km) -- fine for
sequencing, not for a doorstep.
"""
from __future__ import annotations

from dispatch_agent.models import Coordinates

# District -> approximate centroid, keyed by the postal sector prefix (first two digits of a
# Singapore 6-digit postal code). Approximate public-reference coordinates for each of
# Singapore's 28 postal districts.
DISTRICT_CENTROIDS: dict[int, Coordinates] = {
    1: Coordinates(lat=1.2837, lng=103.8517),  # Raffles Place, Cecil, Marina, People's Park
    2: Coordinates(lat=1.2789, lng=103.8451),  # Anson, Tanjong Pagar
    3: Coordinates(lat=1.2872, lng=103.8330),  # Queenstown, Tiong Bahru
    4: Coordinates(lat=1.2650, lng=103.8200),  # Telok Blangah, Harbourfront
    5: Coordinates(lat=1.3100, lng=103.7650),  # Pasir Panjang, Clementi New Town
    6: Coordinates(lat=1.2900, lng=103.8500),  # High Street, Beach Road
    7: Coordinates(lat=1.3010, lng=103.8560),  # Middle Road, Golden Mile
    8: Coordinates(lat=1.3100, lng=103.8570),  # Little India, Farrer Park
    9: Coordinates(lat=1.3000, lng=103.8250),  # Orchard, Cairnhill, River Valley
    10: Coordinates(lat=1.3150, lng=103.8100),  # Ardmore, Bukit Timah, Holland Road
    11: Coordinates(lat=1.3350, lng=103.8350),  # Watten Estate, Novena, Thomson
    12: Coordinates(lat=1.3250, lng=103.8600),  # Balestier, Toa Payoh, Serangoon
    13: Coordinates(lat=1.3350, lng=103.8800),  # Macpherson, Braddell
    14: Coordinates(lat=1.3200, lng=103.8900),  # Geylang, Eunos
    15: Coordinates(lat=1.3050, lng=103.9050),  # Katong, Joo Chiat, Amber Road
    16: Coordinates(lat=1.3200, lng=103.9300),  # Bedok, Upper East Coast, Eastwood
    17: Coordinates(lat=1.3600, lng=103.9800),  # Loyang, Changi
    18: Coordinates(lat=1.3550, lng=103.9450),  # Tampines, Pasir Ris
    19: Coordinates(lat=1.3650, lng=103.8850),  # Serangoon Garden, Hougang, Punggol
    20: Coordinates(lat=1.3600, lng=103.8450),  # Bishan, Ang Mo Kio
    21: Coordinates(lat=1.3400, lng=103.7900),  # Upper Bukit Timah, Clementi Park
    22: Coordinates(lat=1.3350, lng=103.7050),  # Jurong
    23: Coordinates(lat=1.3800, lng=103.7600),  # Hillview, Dairy Farm, Bukit Panjang, Choa Chu Kang
    24: Coordinates(lat=1.4000, lng=103.7100),  # Lim Chu Kang, Tengah
    25: Coordinates(lat=1.4200, lng=103.7750),  # Kranji, Woodgrove
    26: Coordinates(lat=1.4000, lng=103.8300),  # Upper Thomson, Springleaf
    27: Coordinates(lat=1.4400, lng=103.7950),  # Yishun, Sembawang
    28: Coordinates(lat=1.3900, lng=103.8850),  # Seletar
}


def postal_code_to_coords(postal_code: str) -> Coordinates:
    if len(postal_code) != 6 or not postal_code.isdigit():
        raise ValueError(f"expected a 6-digit Singapore postal code, got {postal_code!r}")
    district = int(postal_code[:2])
    if district not in DISTRICT_CENTROIDS:
        raise ValueError(f"unrecognised postal sector {postal_code[:2]!r} in {postal_code!r}")
    return DISTRICT_CENTROIDS[district]
