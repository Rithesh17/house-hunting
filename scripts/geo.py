"""Classify a listing's area into a binary tier: 'avoid' (unsafe) or 'ok'.

The user wants a LEVEL playing field — every SF area ranks equally by MATCH
(fit); only UNSAFE areas (Tenderloin + everything adjacent/rough) are
deprioritized: they sink to the bottom, are badged, and are excluded from
Featured + Telegram alerts. There is no favorite/preferred weighting and no
proximity-to-work ordering anymore.

A listing's area is determined from its ACTUAL location. `fetch_detail.py`
geocodes the street address (when the post gives one) to PRECISE coords + a
neighbourhood name; when there is no address we fall back to the post's area
text. A listing is UNSAFE if its neighbourhood NAME matches the unsafe list OR
its coords fall inside an unsafe zone (a coordinate backstop). The model lives in
config.yaml under `unsafe:` (names + zones).
"""
from __future__ import annotations

import math

import common

_cfg_unsafe = None


def _unsafe() -> dict:
    global _cfg_unsafe
    if _cfg_unsafe is None:
        _cfg_unsafe = common.load_config().get("unsafe", {})
    return _cfg_unsafe


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _name_hit(text: str | None) -> str | None:
    """The matched unsafe-area name if any unsafe substring is in `text`."""
    if not text:
        return None
    low = text.lower()
    for name in _unsafe().get("names", []):
        if name and name in low:
            return name
    return None


def _zone_hit(lat, lng) -> dict | None:
    """The first unsafe zone whose radius contains (lat, lng), if any."""
    if lat is None or lng is None:
        return None
    for z in _unsafe().get("zones", []):
        if haversine_km(lat, lng, z["lat"], z["lng"]) <= z.get("radius_km", 0.6):
            return z
    return None


def classify(lat, lng, area_text: str | None = None,
             place_name: str | None = None) -> dict:
    """Return {area_tier, area_name, unsafe}; area_tier is 'avoid' | 'ok'.

    UNSAFE (=> 'avoid') if the neighbourhood NAME (the geocoded `place_name` or
    the post's `area_text`) matches the unsafe list, OR the coords fall in an
    unsafe zone. The coords should be the precise geocoded-address coords when the
    post supplied an address (see fetch_detail.py); otherwise the name match is
    the primary signal. Everything else is 'ok' — a level field ranked by match.
    """
    name_hit = _name_hit(place_name) or _name_hit(area_text)
    zone = _zone_hit(lat, lng)
    if name_hit or zone:
        label = (zone["name"] if zone else None) or place_name or area_text or name_hit
        return {"area_tier": "avoid", "area_name": label, "unsafe": True}
    return {"area_tier": "ok",
            "area_name": place_name or area_text or "San Francisco",
            "unsafe": False}


AVOID_MATCH_FACTOR = 0.3  # unsafe areas read as LOW match (≤30), not 0 — order kept


def display_match(fit_score, area_tier) -> int:
    """Area-aware MATCH for display/ranking. The subagent scores fit on the unit
    alone (size/condition/value); this folds the area in so an UNSAFE ('avoid')
    area reads as a low match — a fine unit in a bad-for-you area is still a poor
    fit. Trust (legit_score) is untouched: it measures scam-risk, not desirability."""
    f = fit_score or 0
    return round(f * AVOID_MATCH_FACTOR) if area_tier == "avoid" else f


def sort_key(row) -> tuple:
    """Ranking key: unsafe ('avoid') areas sink to the bottom; everything else is
    a level field ranked by MATCH (fit) desc, then trust (legit) desc. `row`
    needs area_tier + fit_score + legit_score (dict or sqlite Row)."""
    def g(k):
        try:
            return row[k]
        except Exception:
            return None
    avoid = 1 if g("area_tier") == "avoid" else 0
    return (avoid, -((g("fit_score") or 0)), -((g("legit_score") or 0)))
