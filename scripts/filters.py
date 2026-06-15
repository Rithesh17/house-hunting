"""Deterministic hard filters applied after a listing's full detail is fetched.

These remove obvious non-candidates BEFORE the (expensive) vision vetting:
  - non-housing PSA / scam-warning notices
  - listings with no photos
  - listings outside San Francisco (by map coords, city name, or ZIP)
  - shared / private rooms (the user wants a self-contained unit)

`auto_reject_reason(...)` returns a short reason string to reject, or None to keep.
"""
from __future__ import annotations

import re

# Rough San Francisco bounding box (lat, lng).
SF_LAT = (37.695, 37.840)
SF_LNG = (-122.530, -122.340)

# Other Bay Area / NorCal cities that disqualify a post. NOTE: deliberately NOT
# including bare "richmond" (Inner/Outer Richmond are SF neighborhoods).
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

# Phrases that mark a shared / private room rather than a self-contained unit.
SHARED_ROOM_PATTERNS = [
    "room for rent", "rooms for rent", "roommate", "housemate", "room in home",
    "room in a home", "room in house", "private room", "shared room",
    "shared bath", "shared kitchen", "single room", " sro ", "sro ",
    "shared housing", "furnished room", "room in shared", "room available in",
    "private bedroom in a", "bedrooms available", "bedroom available",
    "rooms available", "room available", "bedroom in a shared",
    "master bedroom", "a bed in a", "shared apartment", "shared flat",
    "share the apartment", "share my apartment",
]

# A self-contained unit must have its own kitchen; weekly/short-term pricing is
# out of scope (it implies a furnished short-stay, not a real lease).
NO_KITCHEN_PATTERNS = ["no kitchen", "without a kitchen", "without kitchen"]
# Match a PRICE followed by per-week (e.g. "$1200/week", "$1,200 per wk") — NOT
# incidental phrases like "laundry once per week".
WEEKLY_PRICE_RE = re.compile(
    r"(?:\$\s?)?\d{2,}[\d,]*\s*(?:/|per\s+)(?:wk|week)\b", re.IGNORECASE)

# Phrases that mark a non-listing public-service-announcement / scam warning.
PSA_PATTERNS = [
    "too good to be true", "be careful out there", "24 hours notice",
    "if you see an ad", "report scam", "avoid scam", "many scam",
    "scam listings", "this is not a real listing", "beware of scam",
]


def _first_match(text: str, patterns) -> str | None:
    return next((p for p in patterns if p in text), None)


def objective_reject_reason(*, image_count, lat, lng) -> str | None:
    """Only the two objective, no-false-positive gates. Everything else
    (rooms, scams, PSAs, fit) is left to the subagent vetting pass."""
    if not image_count:
        return "no photos"
    if lat is not None and lng is not None:
        if not (SF_LAT[0] <= lat <= SF_LAT[1] and SF_LNG[0] <= lng <= SF_LNG[1]):
            return f"outside San Francisco (coords {lat:.3f},{lng:.3f})"
    return None


def auto_reject_reason(*, title, description, neighborhood, image_count,
                       lat, lng, price) -> str | None:
    t = (title or "").lower()
    d = (description or "").lower()
    n = (neighborhood or "").lower()
    blob = " ".join((t, d, n))

    # 1. non-housing PSA / scam-warning notice
    psa = _first_match(blob, PSA_PATTERNS)
    title_warns = any(w in t for w in ("scam", "beware", "warning"))
    if psa and (not price or title_warns):
        return f"not a housing listing (PSA/warning: '{psa}')"
    if title_warns and ("scam" in t):
        return "not a housing listing (scam-warning notice)"

    # 2. no photos (hard filter)
    if not image_count:
        return "no photos"

    # 3. location: map coordinates are authoritative. Only fall back to
    #    city-name / ZIP text matching when the post has NO coordinates
    #    (text mentions like "Stockton St" or "views of Berkeley" are NOT
    #    reliable location signals and caused false rejects).
    has_coords = lat is not None and lng is not None
    if has_coords:
        if not (SF_LAT[0] <= lat <= SF_LAT[1] and SF_LNG[0] <= lng <= SF_LNG[1]):
            return f"outside San Francisco (coords {lat:.3f},{lng:.3f})"

    # 4. shared / private room (always)
    room = _first_match(blob, SHARED_ROOM_PATTERNS)
    if room:
        return f"shared/private room ('{room.strip()}')"

    # 4b. no kitchen -> not a self-contained unit
    nk = _first_match(blob, NO_KITCHEN_PATTERNS)
    if nk:
        return f"no kitchen ('{nk}')"

    # 4c. weekly / short-term pricing -> out of scope
    wk = WEEKLY_PRICE_RE.search(blob)
    if wk:
        return f"short-term / weekly pricing ('{wk.group(0).strip()}')"

    # 5. coordless fallback: city name / ZIP text checks
    if not has_coords:
        city = _first_match(blob, OUT_OF_SF_CITIES)
        if city:
            return f"outside San Francisco (mentions '{city}')"
        zips = re.findall(r"\b(9\d{4})\b", blob)
        if zips and not any(z.startswith("941") for z in zips):
            return f"outside San Francisco (ZIP {zips[0]})"

    return None
