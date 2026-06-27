"""Classify a listing's area into one of three tiers: 'avoid' | 'caution' | 'ok'.

The user wants prime residential SF (Richmond, Sunset, Noe, Pac Heights, …) to be
a LEVEL field ranked purely by MATCH (fit) — no favorite/preferred weighting among
them. Two area distinctions sit on top of that level field:

  - 'avoid'   — UNSAFE areas (Tenderloin + Chinatown + Lower Nob Hill/Tendernob +
                the SoMa-6th/Mid-Market/Union-Square core + Bayview/Hunters Point +
                Sunnydale). Sunk to the bottom, badged, excluded from Featured +
                Telegram, and DELETED by purge_db.
  - 'caution' — formerly-avoid border areas that research shows are OK-but-not-prime
                (upper Polk, Financial District/Jackson Square, the eastern SoMa
                waterfront — Rincon Hill/South Beach/East Cut — and Visitacion
                Valley's residential remainder). SURFACED and kept, but MATCH is
                discounted and they rank as a group BELOW every prime ('ok') area —
                "okay, but not as good as the Richmond."
  - 'ok'      — everything else: the prime, level-field residential SF.

A listing's area is determined from its ACTUAL location. `fetch_detail.py` geocodes
the street address (when the post gives one) to PRECISE coords + a neighbourhood
name; otherwise we fall back to the post's area text. A listing is 'avoid' if its
name matches the unsafe list OR its coords fall in an unsafe zone; else 'caution'
if it matches the caution names/zones; else 'ok'. AVOID always wins over CAUTION
(e.g. lower Polk is inside the Tenderloin zone -> avoid, even though "Polk Gulch"
is a caution name). The model lives in config.yaml under `unsafe:` and `caution:`.
"""
from __future__ import annotations

import math

import common

_cfg_cache: dict = {}


def _tier_cfg(key: str) -> dict:
    """Cached {names, zones} block from config.yaml for 'unsafe' or 'caution'."""
    if key not in _cfg_cache:
        _cfg_cache[key] = common.load_config().get(key, {}) or {}
    return _cfg_cache[key]


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


# ---- BART proximity (commute metric) ------------------------------------------
# The user commutes by BART, so distance to the nearest station is a real signal —
# both in the East Bay (Berkeley) and in the SF stations they'd rely on from the
# Outer Mission / south-SF side. nearest_bart() works for ANY coord (SF or East Bay).
# Official gtfs_latitude/gtfs_longitude from BART's own API (api.bart.gov, cmd=stns)
# — an authoritative reference, not eyeballed.
BART_STATIONS = [
    # Berkeley
    ("North Berkeley BART", 37.873967, -122.283440),
    ("Downtown Berkeley BART", 37.870104, -122.268133),
    ("Ashby BART", 37.852803, -122.270062),
    # SF commute corridor (Outer Mission / south SF + downtown)
    ("Daly City BART", 37.706121, -122.469081),
    ("Balboa Park BART", 37.721585, -122.447506),
    ("Glen Park BART", 37.733064, -122.433817),
    ("24th St Mission BART", 37.752470, -122.418143),
    ("16th St Mission BART", 37.765062, -122.419694),
    ("Civic Center BART", 37.779732, -122.414123),
    ("Powell St BART", 37.784471, -122.407974),
    ("Montgomery St BART", 37.789405, -122.401066),
    ("Embarcadero BART", 37.792874, -122.397020),
]


def nearest_bart(lat, lng) -> dict | None:
    """Nearest BART station to (lat,lng): {station, km}. None if no coords."""
    if lat is None or lng is None:
        return None
    name, d = min(((n, haversine_km(lat, lng, sla, sln))
                   for n, sla, sln in BART_STATIONS), key=lambda x: x[1])
    return {"station": name, "km": round(d, 2)}


# ---- East Bay / Berkeley (BART-commute option) --------------------------------
# Berkeley city only (no Oakland, per the user). FIRST-LEVEL is broad: any Berkeley
# coord near a Berkeley BART stop is `ok`; SAFETY is handled here like the SF unsafe
# zones — Oakland (south of the city line) and the South-Berkeley / Ashby flatlands
# (the genuinely less-safe pocket) are `avoid`, the way the Tenderloin is in SF.
_BERKELEY_BART = [(37.873967, -122.283440),   # North Berkeley BART
                  (37.870104, -122.268133),   # Downtown Berkeley BART
                  (37.852803, -122.270062)]   # Ashby BART (official gtfs coords)
_BERKELEY_NEAR_KM = 1.6        # broad walk/short-roll to a Berkeley station
_OAKLAND_LINE_LAT = 37.846     # Alcatraz Ave — south of this is Oakland (excluded)
_EASTBAY_LNG = -122.34         # coords east of this are across the bay (not SF)
# South Berkeley / Ashby flatlands — the less-safe pocket, treated `avoid` like the
# Tenderloin (still gets a BART-distance, but excluded from picks). Centered on Ashby.
_SOUTH_BERK_UNSAFE = (37.852803, -122.270062, 0.85)  # (lat, lng, radius_km)


def _eastbay_tier(lat, lng) -> dict | None:
    """Classify an East Bay coordinate. None when not east of the bay (SF falls
    through to SF logic). Else: Oakland (south of the city line) and the South-
    Berkeley unsafe pocket are `avoid`; a Berkeley coord near any Berkeley BART stop
    is `ok`; anything far from a station is `avoid` (not a BART-commute fit)."""
    if lat is None or lng is None or lng <= _EASTBAY_LNG:
        return None
    if lat < _OAKLAND_LINE_LAT:                       # Oakland — excluded
        return {"area_tier": "avoid", "area_name": "Oakland (excluded)", "unsafe": True}
    sla, sln, srad = _SOUTH_BERK_UNSAFE
    if haversine_km(lat, lng, sla, sln) <= srad:      # South Berkeley / Ashby flats
        return {"area_tier": "avoid",
                "area_name": "South Berkeley / Ashby (less safe)", "unsafe": True}
    near = min((haversine_km(lat, lng, bla, bln) for bla, bln in _BERKELEY_BART),
               default=99.0)
    if near <= _BERKELEY_NEAR_KM:
        return {"area_tier": "ok", "area_name": "Berkeley (near BART)", "unsafe": False}
    return {"area_tier": "avoid",
            "area_name": "East Bay (not near a Berkeley BART stop)", "unsafe": True}


def _name_hit(text: str | None, cfg: dict) -> str | None:
    """The matched area name if any of cfg's name substrings is in `text`."""
    if not text:
        return None
    low = text.lower()
    for name in cfg.get("names", []):
        if name and name in low:
            return name
    return None


def _zone_hit(lat, lng, cfg: dict) -> dict | None:
    """The first of cfg's zones whose radius contains (lat, lng), if any."""
    if lat is None or lng is None:
        return None
    for z in cfg.get("zones", []):
        if haversine_km(lat, lng, z["lat"], z["lng"]) <= z.get("radius_km", 0.6):
            return z
    return None


def _match(area_text, place_name, lat, lng, cfg) -> tuple:
    """(name_hit, zone) for a tier config against name text + coords."""
    return (_name_hit(place_name, cfg) or _name_hit(area_text, cfg),
            _zone_hit(lat, lng, cfg))


def classify(lat, lng, area_text: str | None = None,
             place_name: str | None = None) -> dict:
    """Return {area_tier, area_name, unsafe}; area_tier is 'avoid' | 'caution' | 'ok'.

    AVOID (unsafe) wins first: name match against the unsafe list OR coords in an
    unsafe zone. Else CAUTION: name/zone match against the caution config. Else OK.
    The coords should be the precise geocoded-address coords when the post supplied
    an address (see fetch_detail.py); otherwise the name match is the primary signal.
    """
    # East Bay (across the bay) is a separate world: only safe near-BART Berkeley is
    # `ok`, the rest is `avoid`. SF coords (lng <= -122.34) fall through to SF logic.
    eb = _eastbay_tier(lat, lng)
    if eb is not None:
        return eb
    nh, zh = _match(area_text, place_name, lat, lng, _tier_cfg("unsafe"))
    if nh or zh:
        label = (zh["name"] if zh else None) or place_name or area_text or nh
        return {"area_tier": "avoid", "area_name": label, "unsafe": True}
    nh, zh = _match(area_text, place_name, lat, lng, _tier_cfg("caution"))
    if nh or zh:
        label = (zh["name"] if zh else None) or place_name or area_text or nh
        return {"area_tier": "caution", "area_name": label, "unsafe": False}
    return {"area_tier": "ok",
            "area_name": place_name or area_text or "San Francisco",
            "unsafe": False}


# Area-aware MATCH multipliers: 'avoid' reads as a low match (≤30); 'caution' is
# clearly discounted (okay, not prime); 'ok' is full.
MATCH_FACTOR = {"avoid": 0.3, "caution": 0.7, "ok": 1.0}
AVOID_MATCH_FACTOR = MATCH_FACTOR["avoid"]  # back-compat alias


def display_match(fit_score, area_tier) -> int:
    """Area-aware MATCH for display/ranking. The subagent scores fit on the unit
    alone (size/condition/value); this folds the area in so an UNSAFE area reads as
    a low match and a CAUTION area reads as a discounted one — a fine unit in a
    lesser-for-you area is a lesser fit. Trust (legit_score) is untouched: it
    measures scam-risk, not desirability."""
    return round((fit_score or 0) * MATCH_FACTOR.get(area_tier, 1.0))


# Ranking groups: prime 'ok' first, then 'caution', then 'avoid' at the bottom.
_TIER_RANK = {"ok": 0, "caution": 1, "avoid": 2}


def sort_key(row) -> tuple:
    """Ranking key: prime ('ok') areas first as a level field by MATCH (fit) desc
    then trust desc; 'caution' areas as a group below them; 'avoid' at the bottom.
    `row` needs area_tier + fit_score + legit_score (dict or sqlite Row)."""
    def g(k):
        try:
            return row[k]
        except Exception:
            return None
    rank = _TIER_RANK.get(g("area_tier"), 0)
    return (rank, -((g("fit_score") or 0)), -((g("legit_score") or 0)))
