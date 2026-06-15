"""Classify a listing by COORDINATES into an area tier + proximity to work.

Drives ranking everywhere: order by proximity to the workplace (Transamerica
Pyramid), then by match score, with `avoid` areas (Tenderloin + everything
adjacent/rough) heavily punished — they get a sentinel-large proximity so they
sort last, and consumers exclude them from Featured + Telegram alerts.

The geo model lives in config.yaml under `geo:` (work, avoid_zones, preferred).
We classify by lat/lng because the listing's area text is often missing/wrong;
when coords are absent we fall back to matching the area label.
"""
from __future__ import annotations

import math

import common

TIERS = ("preferred", "acceptable", "avoid")
AVOID_PROXIMITY = 999.0  # sentinel so avoid-tier always sorts last

_cfg_geo = None


def _geo() -> dict:
    global _cfg_geo
    if _cfg_geo is None:
        _cfg_geo = common.load_config().get("geo", {})
    return _cfg_geo


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def classify(lat, lng, area_label: str | None = None) -> dict:
    """Return {area_tier, area_name, proximity_km, dist_km}.

    proximity_km is the EFFECTIVE distance used for ordering (raw straight-line
    km to work minus any transit bonus); avoid-tier returns the sentinel."""
    g = _geo()
    work = g.get("work", {})

    if lat is None or lng is None:
        return _classify_by_label(area_label)

    # avoid zones first — any hit wins, regardless of how close to work
    for z in g.get("avoid_zones", []):
        if haversine_km(lat, lng, z["lat"], z["lng"]) <= z.get("radius_km", 0.6):
            return {"area_tier": "avoid", "area_name": z["name"],
                    "proximity_km": AVOID_PROXIMITY,
                    "dist_km": round(haversine_km(lat, lng, work["lat"], work["lng"]), 2)}

    dist = haversine_km(lat, lng, work["lat"], work["lng"])

    # nearest preferred centroid within match radius -> preferred (+transit bonus)
    best = None
    radius = g.get("preferred_match_radius_km", 1.4)
    for p in g.get("preferred", []):
        d = haversine_km(lat, lng, p["lat"], p["lng"])
        if d <= radius and (best is None or d < best[1]):
            best = (p, d)
    if best:
        p = best[0]
        eff = max(0.0, dist - float(p.get("transit_bonus_km", 0)))
        return {"area_tier": "preferred", "area_name": p["name"],
                "proximity_km": round(eff, 2), "dist_km": round(dist, 2)}

    return {"area_tier": "acceptable", "area_name": area_label,
            "proximity_km": round(dist, 2), "dist_km": round(dist, 2)}


def _classify_by_label(area_label: str | None) -> dict:
    """No coords: best-effort from the area text."""
    g = _geo()
    lab = (area_label or "").lower()
    for z in g.get("avoid_zones", []):
        key = z["name"].split(" /")[0].lower()
        if key and key in lab:
            return {"area_tier": "avoid", "area_name": z["name"],
                    "proximity_km": AVOID_PROXIMITY, "dist_km": None}
    work = g.get("work", {})
    for p in g.get("preferred", []):
        key = p["name"].split(" /")[0].lower()
        if key and key in lab:
            d = haversine_km(p["lat"], p["lng"], work["lat"], work["lng"])
            eff = max(0.0, d - float(p.get("transit_bonus_km", 0)))
            return {"area_tier": "preferred", "area_name": p["name"],
                    "proximity_km": round(eff, 2), "dist_km": round(d, 2)}
    # unknown SF location: treat as acceptable but far (mid/high proximity)
    return {"area_tier": "acceptable", "area_name": area_label,
            "proximity_km": 8.0, "dist_km": None}


def sort_key(row) -> tuple:
    """Ranking key: proximity to work asc, then match (fit) desc, then trust.
    avoid-tier sinks via its sentinel proximity. `row` needs proximity_km +
    fit_score + legit_score (dict or sqlite Row)."""
    def g(k):
        try:
            return row[k]
        except Exception:
            return None
    return (g("proximity_km") if g("proximity_km") is not None else AVOID_PROXIMITY,
            -((g("fit_score") or 0)), -((g("legit_score") or 0)))
