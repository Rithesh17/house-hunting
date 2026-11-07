"""Reverse-geocode listings to their REAL town / neighbourhood (non-legacy regions).

For a region profile with `reverse_geocode: true` (regions/<slug>.yaml), a post's
self-described area text is not trusted alone — city lines are often blocks apart
(e.g. Menlo Park | East Palo Alto | North Fair Oaks). This looks up each listing's
coords on OpenStreetMap Nominatim (free, 1 req/s, polite UA) and writes:
  - `area`          = the profile `areas` label whose match hits the town/suburb
                      text (else the raw town name), so geo.classify's name match +
                      market_comps grouping see the real place;
  - `neighborhood`  = "<suburb/neighbourhood>, <town>" when the row has none.
Rows already located are skipped (source_extra.located = true). No-op for the
legacy `sf` profile.

    python3 tools/locate.py --all          # every unlocated, non-rejected row
    python3 tools/locate.py <id> <id> ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import common  # noqa: E402
import db  # noqa: E402

UA = {"User-Agent": "house-hunt/1.0 (personal project)"}


def reverse(lat, lng) -> dict | None:
    try:
        r = requests.get("https://nominatim.openstreetmap.org/reverse",
                         params={"lat": lat, "lon": lng, "format": "json",
                                 "zoom": 16, "addressdetails": 1},
                         headers=UA, timeout=20)
        return (r.json() or {}).get("address")
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"  ! reverse geocode failed: {e}", file=sys.stderr)
        return None


def label_for(addr: dict, region: dict) -> tuple[str | None, str | None]:
    """(area_label, neighbourhood_text) from a Nominatim address block."""
    town = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("hamlet") or addr.get("municipality") or "")
    sub = (addr.get("neighbourhood") or addr.get("suburb") or addr.get("quarter") or "")
    text = f"{sub} {town}".lower()
    # longest keyword wins: "east palo alto" must not read as "palo alto"
    hit = common.best_area(text, region.get("areas", []))
    area = hit["name"] if hit else None
    hood = ", ".join(x for x in (sub, town) if x) or None
    return area or (town or None), hood


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    cfg = common.load_config()
    region = cfg["region"]
    if region.get("legacy") or not region.get("reverse_geocode"):
        print("locate: active region has no reverse_geocode — nothing to do.")
        return
    conn = db.connect()
    if args.all:
        rows = conn.execute("SELECT * FROM listings WHERE lat IS NOT NULL AND "
                            "status NOT IN ('rejected','removed')").fetchall()
    else:
        rows = [db.get(conn, i) for i in args.ids]
    done = 0
    for row in rows:
        if not row or row["lat"] is None:
            continue
        extra = json.loads(row["source_extra"] or "{}") if row["source_extra"] else {}
        if extra.get("located"):
            continue
        addr = reverse(row["lat"], row["lng"])
        time.sleep(1.1)                       # Nominatim usage policy: <= 1 req/s
        if not addr:
            continue
        area, hood = label_for(addr, region)
        extra["located"] = True
        extra["osm_address"] = {k: addr.get(k) for k in
                                ("neighbourhood", "suburb", "city", "town", "village",
                                 "hamlet", "county", "postcode") if addr.get(k)}
        fields = {"source_extra": json.dumps(extra)}
        if area:
            fields["area"] = area
        if hood and not row["neighborhood"]:
            fields["neighborhood"] = hood
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE listings SET {sets} WHERE id=?", (*fields.values(), row["id"]))
        conn.commit()
        done += 1
        print(f"  {row['id']}: {area} ({hood})")
    print(f"locate: {done} listing(s) reverse-geocoded.")


if __name__ == "__main__":
    main()
