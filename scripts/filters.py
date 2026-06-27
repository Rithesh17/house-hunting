"""Deterministic OBJECTIVE gates applied after a listing's full detail is fetched.

Philosophy: pull broad, then judge by VISION + research, not brittle keywords.
Only two no-false-positive gates run mechanically here — no photos, and outside
San Francisco by map coordinates. Everything else (rooms, scams, fit, legitimacy)
is decided by the subagent vetting pass (Stage 1 + Stage 2), never by keyword
matching (which historically caused false drops).

`objective_reject_reason(...)` returns a short reason to reject, or None to keep.
`OUT_OF_SF_CITIES` is also consumed by scripts/fetch_listings.py at discovery time.
"""
from __future__ import annotations

# Rough San Francisco bounding box (lat, lng).
SF_LAT = (37.695, 37.840)
SF_LNG = (-122.530, -122.340)

# Berkeley search box (East Bay BART-commute option). Coords inside this are KEPT
# through the objective gate; the precise safe-near-BART vs avoid call is made by
# geo.classify (Oakland / South Berkeley get classified `avoid` and purged later).
BERKELEY_LAT = (37.845, 37.905)
BERKELEY_LNG = (-122.300, -122.230)

# Other Bay Area / NorCal cities that disqualify a post at discovery time. NOTE:
# deliberately NOT including bare "richmond" (Inner/Outer Richmond are SF).
OUT_OF_SF_CITIES = [
    "suisun", "daly city", "oakland", "berkeley", "san jose", "vallejo",
    "fairfield", "hayward", "fremont", "san mateo", "south san francisco",
    "pacifica", "concord", "antioch", "tracy", "discovery bay", "mountain house",
    "san leandro", "alameda", "novato", "petaluma", "santa rosa", "stockton",
    "sacramento", "brentwood", "milpitas", "redwood city", "palo alto",
    "mountain view", "sunnyvale", "santa clara", "san rafael", "walnut creek",
    "emeryville", "el cerrito", "san bruno", "burlingame", "brisbane",
    "san pablo", "martinez", "union city", "newark", "dublin", "pleasanton",
    "livermore", "napa", "benicia", "castro valley", "millbrae", "foster city",
    "menlo park", "vacaville",
]


def objective_reject_reason(*, image_count, lat, lng) -> str | None:
    """The two objective, no-false-positive gates. Everything else (rooms, scams,
    fit) is left to the subagent vetting pass."""
    if not image_count:
        return "no photos"
    if lat is not None and lng is not None:
        in_sf = (SF_LAT[0] <= lat <= SF_LAT[1] and SF_LNG[0] <= lng <= SF_LNG[1])
        in_berk = (BERKELEY_LAT[0] <= lat <= BERKELEY_LAT[1]
                   and BERKELEY_LNG[0] <= lng <= BERKELEY_LNG[1])
        if not (in_sf or in_berk):
            return f"outside target area (coords {lat:.3f},{lng:.3f})"
    return None
