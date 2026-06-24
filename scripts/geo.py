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
