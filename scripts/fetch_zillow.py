"""Discover San Francisco rentals from Zillow via the Apify
`igolaizola/zillow-scraper-ppe` actor (Zillow itself hard-blocks scraping; Apify
runs it behind residential proxies for us).

We query Zillow's own rental search by location + structured filters (SF, for-rent,
entire-place, <= max_price, newest-first) and map each listing to our schema,
inserting `source='zillow'` rows — same incremental fetch -> research -> two-stage
vet -> dashboard flow as Zumper. Already-seen zpids and blocklisted ids are skipped.

Why this actor (over maxcopell/zillow-scraper): it returns MANY photos and, with
`fetchDetails`, the full listing description + an exact posted timestamp
(`listingDateTimeOnZillow`). The description is essential — it lets the vetting
subagent run the SHARED-ROOM GATE on Zillow rows (a "studio" that is really a room
in a shared community apartment is otherwise invisible from one photo).

COST (pay-per-event; bills only for what's RETURNED, no monthly fee):
  - Actor Start   : $0.0005 / run
  - Result        : $0.0009 / listing  (includes the photos)
  - Fetch Detail  : $0.002  / listing  (only when fetchDetails=true; adds the body)
So a listing with details ~= $0.0029. The lever that controls cost is how many
listings come back, so we DERIVE `timeOnZillow` (Zillow's "listed within the last
N" recency filter) from how long ago we last pulled: a run a day after the last
pull asks Zillow for only listings added in the last day. Smallest bucket is 1 day.
The actor is location/filter-based (no per-id detail fetch), so fetchDetails is
all-or-nothing per run; with once-per-refresh cadence the recency window ~= the new
listings, so we pay details essentially only for genuinely-new units. dedup +
blocklist still guarantee nothing already seen is re-stored or re-vetted.

    py scripts/fetch_zillow.py [--max-items 40] [--time-on-zillow 1d] [--no-details]

Needs APIFY_TOKEN in .env.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import common
import db
import fetch_detail
import filters

ROOT = common.ROOT
load_dotenv(os.path.join(ROOT, ".env"))
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
ACTOR = "igolaizola~zillow-scraper-ppe"
_PHOTO_RE = re.compile(r'https://photos\.zillowstatic\.com/[^\s"\\]+')
_SIZE_ORDER = "abcdefg"  # Zillow `-p_<letter>` photo size suffixes (a=small .. g=large)

# Zillow's "time on Zillow" (max-age) recency buckets. '1d' = listed within the
# last day = newest. We snap UP to the smallest bucket covering the gap since our
# last pull, so each run requests only listings new since then; dedup absorbs the
# overlap. (minTimeOnZillow is the OPPOSITE filter — at-least-N-old — don't use it.)
TOZ_BUCKETS = [(1, "1d"), (7, "1w"), (14, "2w"), (30, "1m"), (90, "3m"),
               (180, "6m"), (365, "1y"), (730, "2y"), (1095, "3y")]


def _snap_toz(days: int) -> str:
    for d, v in TOZ_BUCKETS:
        if days <= d:
            return v
    return TOZ_BUCKETS[-1][1]


def auto_time_on_zillow(conn, fallback: str = "1w") -> str:
    """Recency window to request, derived from how long ago we last pulled Zillow.
    ceil the gap (so a listing posted just after the prior run isn't missed) and
    snap to a real bucket. No prior pull (fresh DB) -> `fallback`."""
    last = db.get_meta(conn, "last_pull_zillow")
    if not last:
        return fallback
    try:
        gap = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 86400
    except (ValueError, TypeError):
        return fallback
    return _snap_toz(max(1, math.ceil(gap)))


def run_actor(max_price: int, max_items: int, time_on_zillow: str,
              fetch_details: bool) -> list[dict]:
    inp = {
        "operation": "rent",
        "location": "San Francisco, CA",
        "space": "entirePlace",           # exclude room-shares at the query level
        "maxPrice": max_price,            # monthly rent cap (verified monthly, not weekly)
        "sortBy": "newest",
        "maxItems": max_items,
        "fetchDetails": fetch_details,    # adds the full description + facts
    }
    if time_on_zillow:
        inp["timeOnZillow"] = time_on_zillow
    cap = max(0.5, round(max_items * 0.005, 2))  # PPE safety ceiling (min $0.50)
    r = requests.post(
        f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
        f"?token={APIFY_TOKEN}&maxItems={max_items}&maxTotalChargeUsd={cap}",
        json=inp, timeout=600)
    if r.status_code >= 300:
        raise SystemExit(f"Apify run failed {r.status_code}: {r.text[:400]}")
    return r.json()


def _photos(item: dict) -> list[str]:
    """Distinct Zillow photos in the item, deduped by photo hash (so we keep one
    URL per real photo, not every size variant), preferring the largest size."""
    best: dict[str, tuple[int, str]] = {}
    for u in _PHOTO_RE.findall(json.dumps(item)):
        m = re.search(r"/fp/([0-9a-f]+)-(?:p_([a-g])|cc_ft_\d+)", u)
        key = m.group(1) if m else u
        rank = _SIZE_ORDER.find(m.group(2)) if (m and m.group(2)) else 5
        if key not in best or rank > best[key][0]:
            best[key] = (rank, u)
    return [u for _, u in best.values()][:15]


def _posted_at(item: dict) -> str | None:
    ms = item.get("listingDateTimeOnZillow")
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError, OSError):
        return None


def _listed_by(det: dict) -> str | None:
    """Agent / brokerage display names from _details.listedBy (list or dict)."""
    lb = det.get("listedBy")
    items = lb if isinstance(lb, list) else [lb] if isinstance(lb, dict) else []
    names = []
    for e in items:
        if isinstance(e, dict):
            for k in ("display_name", "business_name", "name"):
                v = e.get(k)
                if v and v not in names:
                    names.append(v)
    return ", ".join(names) or None


def _price_history(det: dict, n: int = 5) -> list[dict]:
    """Compact recent priceHistory events: {date, price, event}."""
    out = []
    for e in (det.get("priceHistory") or [])[:n]:
        if isinstance(e, dict):
            out.append({"date": e.get("date"), "price": e.get("price"), "event": e.get("event")})
    return out


def _source_extra(it: dict, det: dict) -> dict:
    """Zillow signals worth carrying into vetting: room-share flags (Zillow's own),
    its market rent estimate, the parcel #, the listing agent, and price history.
    Falsy/empty values are dropped (so `is_room_for_rent` only appears when True)."""
    rental = it.get("rental") or {}
    est = it.get("estimates") or {}
    rf = det.get("resoFacts") or {}
    extra = {
        "is_room_for_rent": rental.get("isRoomForRent"),
        "is_rent_by_bed": rental.get("isRentByBed"),
        "rent_zestimate": est.get("rentZestimate") or det.get("rentZestimate"),
        "zestimate": det.get("zestimate"),
        "parcel_number": rf.get("parcelNumber") or det.get("parcelId"),
        "listed_by": _listed_by(det),
        "price_history": _price_history(det),
    }
    return {k: v for k, v in extra.items() if v not in (None, "", [], False)}


def main() -> None:
    if not APIFY_TOKEN:
        raise SystemExit("APIFY_TOKEN not set in .env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-items", type=int, default=40,
                    help="cap billable results per run (default 40)")
    ap.add_argument("--time-on-zillow", default=None,
                    help="recency window: 1d/1w/2w/1m/3m/6m/1y/2y/3y "
                         "(default: auto from last pull; '' = any age / full pull)")
    ap.add_argument("--no-details", action="store_true",
                    help="skip fetchDetails (no descriptions; cheaper, $0.0009/result)")
    args = ap.parse_args()
    cfg = common.load_config()
    max_price = cfg["max_price"]

    conn = db.connect()
    toz = args.time_on_zillow if args.time_on_zillow is not None else auto_time_on_zillow(conn)
    src = "manual" if args.time_on_zillow is not None else "auto from last pull"
    fetch_details = not args.no_details

    print(f"[zillow] igolaizola PPE (SF rent, entirePlace, <=${max_price}, newest, "
          f"timeOnZillow={toz or 'any'} [{src}], details={fetch_details}, "
          f"max {args.max_items})...")
    items = run_actor(max_price, args.max_items, toz, fetch_details)
    print(f"  {len(items)} listings returned")

    sess = common.session(cfg)
    new = skipped = 0
    for it in items:
        zpid = str(it.get("zpid") or "")
        price = (it.get("price") or {}).get("value")
        if not zpid.isdigit() or not price or price > max_price:
            skipped += 1
            continue
        pid = f"zl{zpid}"
        if db.listing_exists(conn, pid) or db.is_blocked(conn, pid):
            continue
        url = it.get("url") or ""
        if url and not url.startswith("http"):
            url = "https://www.zillow.com" + url
        beds = it.get("bedrooms")
        room_type = ("studio" if beds == 0 else "1br" if beds == 1
                     else "2br_plus" if beds and beds >= 2 else "unknown")
        addr = it.get("address") or {}
        street = addr.get("streetAddress")
        if street and "undisclosed" in street.lower():
            street = None
        loc = it.get("location") or {}
        det = it.get("_details") or {}
        lat, lng = loc.get("latitude"), loc.get("longitude")
        # Undisclosed-address listings have no top-level coords, but _details does —
        # use it so the area model can still classify them (else they default to ok).
        if lat is None or lng is None:
            lat = det.get("latitude") if det.get("latitude") is not None else lat
            lng = det.get("longitude") if det.get("longitude") is not None else lng
        description = det.get("description")
        photos = _photos(it)
        title = street or "Zillow rental"
        extra = _source_extra(it, det)

        db.insert_stub(conn, post_id=pid, url=url, title=title, price=price,
                       room_type=room_type,
                       area=cfg.get("unspecified_area_name", "(unspecified SF)"),
                       neighborhood="", posted_at=_posted_at(it))
        image_dir, image_count = fetch_detail.download_images(sess, pid, photos)
        db.update_detail(conn, pid, {
            "source": "zillow",
            "bedrooms": float(beds) if beds is not None else None,
            "bathrooms": float(it["bathrooms"]) if it.get("bathrooms") is not None else None,
            "sqft": it.get("livingArea"), "lat": lat, "lng": lng, "address": street,
            "description": description,
            "image_urls": json.dumps(photos), "image_count": image_count,
            "source_extra": json.dumps(extra) if extra else None,
        })
        # objective gate (no photos / outside SF by coords) — consistent w/ Craigslist
        reason = filters.objective_reject_reason(image_count=image_count, lat=lat, lng=lng)
        if reason:
            db.auto_reject(conn, pid, reason)
        conn.commit()
        new += 1

    db.set_meta(conn, "last_pull_zillow", db.now())
    conn.commit()
    conn.close()
    print(f"\n{new} new Zillow listings <= ${max_price} added "
          f"(skipped {skipped} over-cap/building-cards). Ready to research + vet.")


if __name__ == "__main__":
    main()
