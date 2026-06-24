"""Push the local SQLite listings up to Supabase for the public dashboard.

Design: keep the cloud SMALL. We send ONE row per deduped unit (the cluster
primary), carrying only identity, display essentials, remote photo URLs, and
Claude's verdict (scores / summary / recommendation / red flags). The verbatim
post body is NOT uploaded — the dossier links out to the source post. The other
source posts of a unit are embedded in `sources` so the dashboard can list them.

Auth: writes use the service_role key (bypasses RLS); the dashboard reads with
the public anon key (RLS allows SELECT only). Run after vetting + dedup:

    py scripts/sync_supabase.py

It upserts current primaries and deletes cloud rows that no longer exist locally
(removed / re-clustered), so the cloud mirrors local state.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

import requests
from dotenv import load_dotenv

import db
import geo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
REST = f"{SUPABASE_URL}/rest/v1/listings" if SUPABASE_URL else None

# Listings in these states are never published to the cloud/dashboard read-model.
SKIP_STATUS = {"rejected", "removed"}


def _json_list(val):
    if not val:
        return []
    if isinstance(val, list):
        return val
    try:
        out = json.loads(val)
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def _json_obj(val):
    """Parse a stored JSON object column (verification) to a dict, else None."""
    if not val:
        return None
    if isinstance(val, dict):
        return val
    try:
        out = json.loads(val)
        return out if isinstance(out, dict) else None
    except (ValueError, TypeError):
        return None


def _source_entry(d: dict) -> dict:
    return {
        "url": d.get("url"), "source": d.get("source"), "price": d.get("price"),
        "fit_score": d.get("fit_score"), "legit_score": d.get("legit_score"),
        "legit_label": d.get("legit_label"), "area": d.get("area"),
        "room_type": d.get("room_type"), "title": d.get("title"),
    }


def build_rows(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM listings").fetchall()
    objs = {}
    for r in rows:
        d = db.row_to_dict(r)
        if d.get("status") in SKIP_STATUS:
            continue
        objs[d["id"]] = d

    # cluster by dup_group; one row per primary (best fit/legit in the cluster)
    groups = defaultdict(list)
    for d in objs.values():
        groups[d.get("dup_group") or d["id"]].append(d)

    out = []
    for gid, members in groups.items():
        members.sort(key=lambda m: (m.get("fit_score") or -1,
                                    m.get("legit_score") or -1), reverse=True)
        primary = objs.get(gid, members[0])
        # Flat area model: 'avoid' (unsafe) vs 'ok'. Classify from the listing's
        # actual location — the geocoded/post neighbourhood name + coords.
        area = geo.classify(primary.get("lat"), primary.get("lng"),
                            primary.get("area"), primary.get("neighborhood"))
        row = {
            "id": primary["id"],
            "area_tier": area["area_tier"],
            "proximity_km": None,  # no longer computed (flat model, no work-distance)
            "source": primary.get("source"),
            "url": primary.get("url"),
            "title": primary.get("title"),
            "price": primary.get("price"),
            "room_type": primary.get("room_type"),
            "bedrooms": primary.get("bedrooms"),
            "bathrooms": primary.get("bathrooms"),
            "sqft": primary.get("sqft"),
            "area": primary.get("area"),
            "neighborhood": primary.get("neighborhood"),
            "address": primary.get("address"),
            "lat": primary.get("lat"),
            "lng": primary.get("lng"),
            "image_urls": _json_list(primary.get("image_urls")),
            "phone": primary.get("phone"),
            "reply_email": primary.get("reply_email"),
            "contact_name": primary.get("contact_name"),
            "legit_score": primary.get("legit_score"),
            "legit_label": primary.get("legit_label"),
            "red_flags": _json_list(primary.get("red_flags")),
            # MATCH is area-aware: unsafe areas read low even if the unit is nice
            # (the local fit_score stays the raw unit score).
            "fit_score": geo.display_match(primary.get("fit_score"), area["area_tier"]),
            "verdict_summary": primary.get("verdict_summary"),
            "recommendation": primary.get("recommendation"),
            "verification": _json_obj(primary.get("verification")),  # null if unvetted
            "status": primary.get("status"),
            "dup_count": len(members),
            "sources": [_source_entry(m) for m in members],
            "first_seen_at": primary.get("first_seen_at"),
        }
        out.append(row)
    return out


def _headers(extra: dict | None = None) -> dict:
    h = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def upsert(rows: list[dict]) -> None:
    # chunked bulk upsert (merge on id)
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        r = requests.post(
            f"{REST}?on_conflict=id",
            headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            data=json.dumps(chunk), timeout=60)
        if r.status_code >= 300:
            raise SystemExit(f"upsert failed {r.status_code}: {r.text[:500]}")


def delete_stale(current_ids: set[str]) -> int:
    r = requests.get(f"{REST}?select=id", headers=_headers(), timeout=60)
    r.raise_for_status()
    cloud_ids = {row["id"] for row in r.json()}
    stale = sorted(cloud_ids - current_ids)
    for i in range(0, len(stale), 100):
        chunk = stale[i:i + 100]
        inlist = ",".join(f'"{x}"' for x in chunk)
        dr = requests.delete(f"{REST}?id=in.({inlist})",
                             headers=_headers({"Prefer": "return=minimal"}), timeout=60)
        if dr.status_code >= 300:
            raise SystemExit(f"delete failed {dr.status_code}: {dr.text[:300]}")
    return len(stale)


def main() -> None:
    if not SUPABASE_URL or not SERVICE_KEY:
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set in .env")
    conn = db.connect()
    rows = build_rows(conn)
    conn.close()
    ids = {row["id"] for row in rows}
    upsert(rows)
    removed = delete_stale(ids)
    print(f"Supabase sync: upserted {len(rows)} unit(s), removed {removed} stale. "
          f"Cloud now mirrors local.")


if __name__ == "__main__":
    main()
