"""Confirm a listing's address is a REAL residential parcel, via the FREE DataSF
assessor property roll (Socrata; no key for low volume).

SF's open assessor data has no owner *name* (redacted), but it DOES carry parcel
characteristics + a point geometry, so we query by the listing's COORDINATES
(reliable; the roll's text address is fixed-width/padded) and match the parcel by
street number. We FETCH facts only — use code, # units, beds, year built — for the
vetting subagent to cross-check against the post (e.g. a "whole 3BR house" claim
vs a 1-unit condo, or an address that isn't a real parcel at all). Best-effort:
returns {found: False} on any miss; SF-only is fine.

    py scripts/owner_lookup.py --lat 37.787 --lng -122.456 --address "3870 Sacramento St"
"""
from __future__ import annotations

import argparse
import json
import re

import requests

DATASF = "https://data.sfgov.org/resource/wv5m-vpq2.json"
FIELDS = ("parcel_number,property_location,property_class_code_definition,"
          "number_of_units,number_of_bedrooms,number_of_bathrooms,"
          "year_property_built,analysis_neighborhood,closed_roll_year")


_SUFFIXES = {"st", "street", "ave", "avenue", "blvd", "boulevard", "dr", "drive",
             "ct", "court", "pl", "place", "ln", "lane", "way", "ter", "terrace",
             "rd", "road", "hwy", "cir", "circle", "row", "alley", "plaza"}


def _street_number(address: str | None) -> int | None:
    m = re.match(r"\s*(\d{1,6})\b", address or "")
    return int(m.group(1)) if m else None


def _street_name(address: str | None) -> str | None:
    """The street-name token from an address, e.g. '3870 Sacramento St' -> SACRAMENTO,
    '1419 30th Avenue' -> 30TH. Used to disambiguate same-block cross-street parcels."""
    if not address:
        return None
    toks = re.sub(r"#.*$", "", address).strip().split()
    words = [t for t in toks if not re.fullmatch(r"\d{1,6}", t)
             and t.lower().strip(".,") not in _SUFFIXES]
    return words[0].upper().strip(".,") if words else None


def _match_quality(property_location: str, target: int) -> int:
    """How well a parcel's number matches the target: 2 = exact street number,
    1 = within a street-number range, 0 = no. The roll packs 1-2 numbers before
    the street name, e.g. '0000 3876 SACRAMENTO' (single) or '3899 3801 SACRAMENTO'
    (a wide range — a big/vacant parcel that can bracket a real address)."""
    nums = [int(n) for n in re.findall(r"\d+", property_location or "")
            if n != "0000" and 10 <= int(n) <= 99999]
    if not nums:
        return 0
    if target in nums:
        return 2
    return 1 if min(nums) <= target <= max(nums) else 0


def owner_of_record(lat=None, lng=None, address=None, *, radius_m=60,
                    timeout=20) -> dict:
    if lat is None or lng is None:
        return {"found": False, "error": "need coordinates"}
    try:
        r = requests.get(DATASF, params={
            "$select": FIELDS,
            "$where": f"within_circle(the_geom, {lat}, {lng}, {radius_m})",
            "$order": "closed_roll_year DESC", "$limit": 50}, timeout=timeout)
        r.raise_for_status()
        rows = r.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        return {"found": False, "error": f"datasf failed: {e}"}
    if not rows:
        return {"found": False, "note": "no parcel near these coordinates"}

    # latest roll year per parcel
    by_parcel: dict[str, dict] = {}
    for row in rows:
        p = row.get("parcel_number")
        if p and (p not in by_parcel or
                  (row.get("closed_roll_year", "") > by_parcel[p].get("closed_roll_year", ""))):
            by_parcel[p] = row
    candidates = list(by_parcel.values())

    target = _street_number(address)
    sname = _street_name(address)
    match = None
    if target is not None:
        scored = []
        for c in candidates:
            loc = c.get("property_location", "") or ""
            if sname and sname not in loc.upper():
                continue
            q = _match_quality(loc, target)
            if q:
                scored.append((q, c))
        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)  # exact (2) before range (1)
            match = scored[0][1]

    def shape(c: dict | None) -> dict | None:
        if not c:
            return None
        return {
            "parcel": c.get("parcel_number"),
            "property_location": re.sub(r"\s+", " ", c.get("property_location", "")).strip(),
            "use": c.get("property_class_code_definition"),
            "units": _num(c.get("number_of_units")),
            "bedrooms": _num(c.get("number_of_bedrooms")),
            "bathrooms": _num(c.get("number_of_bathrooms")),
            "year_built": c.get("year_property_built"),
            "neighborhood": c.get("analysis_neighborhood"),
            "roll_year": c.get("closed_roll_year"),
        }

    return {
        "found": True,
        "owner_name": None,  # not in SF free open data (recorder only)
        "owner_name_note": "owner name not available in free SF assessor data",
        "match": shape(match),                       # parcel matched to the address number
        "nearby": [shape(c) for c in candidates[:6]],  # context if no clean match
        "source": "DataSF assessor roll (wv5m-vpq2)",
    }


def _num(v):
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lng", type=float, required=True)
    ap.add_argument("--address")
    args = ap.parse_args()
    print(json.dumps(owner_of_record(args.lat, args.lng, args.address), indent=2))


if __name__ == "__main__":
    main()
