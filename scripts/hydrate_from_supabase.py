"""Rebuild the local SQLite DB from the Supabase cloud read-model.

The pipeline runs locally and treats `data/listings.db` as the source of truth,
but that file is gitignored and is NOT always present (fresh machine, new
checkout, lost disk). The cloud (Supabase) holds a durable read-model: one row
per deduped unit (the cluster primary) with Claude's verdict + display
essentials. This script pulls those rows back down so an incremental refresh can
resume WITHOUT (a) re-vetting/re-notifying everything, or (b) letting the final
`sync_supabase.py` delete cloud rows that are simply missing locally.

What it restores:
  * Every cloud row -> a full local `listings` row (verdict, scores, photos,
    coords, status). These are marked detail-fetched + vetted + already-notified
    so the refresh leaves them alone, and dedup re-clusters them from their own
    coords/images/address.
  * Each cloud row's embedded `sources` (the OTHER posts folded into that unit)
    -> a minimal `status='removed'` stub keyed by post id. That id is what makes
    the incremental fetch SKIP the post instead of re-discovering and re-vetting
    it; 'removed' keeps it off the dashboard and out of the next cloud sync.

SAFE to run on a populated DB: every write is INSERT OR IGNORE, so existing
local rows are never clobbered — only missing ids are filled in. This is why
`refresh.py` can call it unconditionally as its first step.

    py scripts/hydrate_from_supabase.py
"""
from __future__ import annotations

import json
import os
import re
import sys

import requests
from dotenv import load_dotenv

import common
import db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
# Reading is allowed with either key (anon has SELECT via RLS); prefer service.
KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")

# Local listings columns we populate, in order (others keep their defaults).
COLS = [
    "id", "source", "url", "title", "price", "bedrooms", "bathrooms", "sqft",
    "room_type", "area", "neighborhood", "address", "lat", "lng",
    "description", "image_urls", "image_count", "phone", "status",
    "reject_reason", "dup_group", "legit_score", "legit_label", "red_flags",
    "low_polish", "fit_score", "is_1br1ba", "verdict_summary", "recommendation",
    "first_seen_at", "detail_fetched_at", "vetted_at", "notified",
]

_ZUMPER_ID_RE = re.compile(r"/(?:listings|apartment-buildings)/p?(\d+)")


def _json_str(val) -> str:
    """Normalize a cloud JSON column (already a list/dict, or a string) to text."""
    if val is None:
        return "[]"
    if isinstance(val, str):
        return val
    return json.dumps(val)


def _derive_id(entry: dict) -> str | None:
    """Post id for a folded `sources` entry. Craigslist ids live in the URL;
    Zumper's real id is `z<listing_id>`, which the map API gives us but the URL
    only approximates — so this is best-effort for Zumper (a miss just means that
    one duplicate may be re-vetted once on a later scan, then re-folded)."""
    url = entry.get("url") or ""
    if entry.get("source") == "zumper" or "zumper.com" in url:
        m = _ZUMPER_ID_RE.search(url)
        return f"z{m.group(1)}" if m else None
    return common.post_id_from_url(url)


def _fetch_cloud_rows() -> list[dict]:
    if not SUPABASE_URL or not KEY:
        raise SystemExit("SUPABASE_URL / SUPABASE key not set in .env")
    headers = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
    out: list[dict] = []
    # Page through PostgREST (default cap 1000/req) to be safe as data grows.
    step = 1000
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/listings?select=*&order=id",
            headers={**headers, "Range-Unit": "items",
                     "Range": f"{offset}-{offset + step - 1}"},
            timeout=60)
        r.raise_for_status()
        batch = r.json()
        out.extend(batch)
        if len(batch) < step:
            break
        offset += step
    return out


def _primary_row(c: dict) -> dict:
    """Map a cloud primary -> a full local listings row."""
    img = c.get("image_urls") or []
    vetted = c.get("fit_score") is not None or c.get("legit_score") is not None
    return {
        "id": c["id"],
        "source": c.get("source") or "craigslist",
        "url": c.get("url"),
        "title": c.get("title"),
        "price": c.get("price"),
        "bedrooms": c.get("bedrooms"),
        "bathrooms": c.get("bathrooms"),
        "sqft": c.get("sqft"),
        "room_type": c.get("room_type"),
        "area": c.get("area"),
        "neighborhood": c.get("neighborhood"),
        "address": c.get("address"),
        "lat": c.get("lat"),
        "lng": c.get("lng"),
        "description": None,                 # body is never stored in the cloud
        "image_urls": _json_str(img),
        "image_count": len(img) if isinstance(img, list) else 0,
        "phone": c.get("phone"),
        "status": c.get("status") or "vetted",
        "reject_reason": None,
        "dup_group": c["id"],                # dedup recomputes from coords/images
        "legit_score": c.get("legit_score"),
        "legit_label": c.get("legit_label"),
        "red_flags": _json_str(c.get("red_flags")),
        "low_polish": 0,
        "fit_score": c.get("fit_score"),
        "is_1br1ba": 1 if c.get("room_type") == "1br" else 0,
        "verdict_summary": c.get("verdict_summary"),
        "recommendation": c.get("recommendation"),
        "first_seen_at": c.get("first_seen_at"),
        "detail_fetched_at": c.get("first_seen_at") or db.now(),  # skip re-fetch
        "vetted_at": db.now() if vetted else None,                # skip re-vet
        "notified": 1,                       # historical: never re-notify
    }


def _source_stub(entry: dict, pid: str) -> dict:
    """Minimal 'removed' stub for a folded duplicate post (identity only)."""
    return {
        "id": pid,
        "source": entry.get("source") or "craigslist",
        "url": entry.get("url"),
        "title": entry.get("title"),
        "price": entry.get("price"),
        "bedrooms": None, "bathrooms": None, "sqft": None,
        "room_type": entry.get("room_type"),
        "area": entry.get("area"),
        "neighborhood": None, "address": None, "lat": None, "lng": None,
        "description": None, "image_urls": "[]", "image_count": 0, "phone": None,
        "status": "removed",                 # hidden, not synced, not re-vetted
        "reject_reason": "duplicate (folded into another post; hydrated stub)",
        "dup_group": pid,
        "legit_score": entry.get("legit_score"),
        "legit_label": entry.get("legit_label"),
        "red_flags": "[]", "low_polish": 0,
        "fit_score": entry.get("fit_score"), "is_1br1ba": 0,
        "verdict_summary": None, "recommendation": None,
        "first_seen_at": None,
        "detail_fetched_at": db.now(),       # already known; skip detail fetch
        "vetted_at": None, "notified": 1,
    }


def _insert_ignore(conn, row: dict) -> bool:
    placeholders = ",".join("?" for _ in COLS)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO listings ({','.join(COLS)}) VALUES ({placeholders})",
        [row.get(c) for c in COLS])
    return cur.rowcount > 0


def main() -> None:
    conn = db.connect()
    conn.executescript(db.SCHEMA)            # ensure tables exist (fresh DB)

    cloud = _fetch_cloud_rows()
    print(f"Fetched {len(cloud)} row(s) from Supabase.")

    n_primary = n_stub = 0
    seen_ids: set[str] = set()
    for c in cloud:
        if _insert_ignore(conn, _primary_row(c)):
            n_primary += 1
        seen_ids.add(c["id"])

    # Folded duplicate posts: restore their ids so the next fetch skips them.
    for c in cloud:
        for entry in (c.get("sources") or []):
            pid = _derive_id(entry)
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            if _insert_ignore(conn, _source_stub(entry, pid)):
                n_stub += 1

    # Informational watermarks (the next pull overwrites last_pull with now()).
    first_seen = [c.get("first_seen_at") for c in cloud if c.get("first_seen_at")]
    if first_seen and not db.get_meta(conn, "last_pull"):
        db.set_meta(conn, "last_pull", max(first_seen))
    if first_seen and not db.get_meta(conn, "last_pull_zumper"):
        db.set_meta(conn, "last_pull_zumper", max(first_seen))

    conn.commit()
    conn.close()
    print(f"Hydrated {n_primary} unit(s) + {n_stub} folded-duplicate stub(s) "
          f"into the local DB (INSERT OR IGNORE; existing rows untouched).")


if __name__ == "__main__":
    main()
