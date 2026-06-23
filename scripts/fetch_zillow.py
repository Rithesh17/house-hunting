"""Discover San Francisco rentals from Zillow via the Apify `maxcopell/zillow-scraper`
actor (Zillow itself hard-blocks scraping; Apify runs it behind proxies for us).

We hit Zillow's own search (SF, For-Rent, <= max_price, sorted newest) through the
actor's run-sync endpoint, then map each listing to our schema and insert
`source='zillow'` rows — same incremental fetch -> research -> two-stage vet ->
dashboard flow as Zumper. Already-seen zpids are skipped (id-dedup).

COST: Apify bills per result item it RETURNS (~$0.002 each; ~2,500/mo free on the
$5 plan), and client-side filters run AFTER the return — so the only way to cut the
bill is to make Zillow return fewer rows in the first place. Two query-side levers:
  - --max-items caps the hard ceiling (default 40);
  - the "days on Zillow" (doz) recency filter limits the response to listings added
    in the last N days, so we don't re-pull (and re-pay for) data already in our DB.
We DERIVE doz from how long ago we last pulled (`last_pull_zillow`): a run a day
after the last pull asks Zillow for only ~1 day of listings (a handful of billed
items); a run a week later asks for ~7. doz only honors Zillow's buckets
(1/7/14/30/90 days), so we snap UP to the smallest covering bucket; dedup absorbs
the overlap. --days overrides the auto value; --days 0 drops the filter (full pull).

    py scripts/fetch_zillow.py [--max-items 40] [--days N]   # default --days = auto

Needs APIFY_TOKEN in .env.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

import common
import db
import fetch_detail
import fetch_listings
import filters

ROOT = common.ROOT
load_dotenv(os.path.join(ROOT, ".env"))
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
ACTOR = "maxcopell~zillow-scraper"
# SF region + bounding box (matches scripts/filters SF box).
_SF_BOUNDS = {"west": -122.5160, "east": -122.3540, "south": 37.7034, "north": 37.8120}
_PHOTO_RE = re.compile(r"https://photos\.zillowstatic\.com/[^\s\"'\\]+")
# Zillow only honors these "days on Zillow" (doz) recency buckets; an arbitrary
# value is silently ignored (= full response = full bill), so we snap UP to the
# smallest bucket that still covers the window we need.
DOZ_BUCKETS = (1, 7, 14, 30, 90)


def _snap_doz(days: int) -> str | None:
    """Smallest Zillow doz bucket that covers `days`; None if days<=0 (no filter)."""
    if not days or days <= 0:
        return None
    for b in DOZ_BUCKETS:
        if days <= b:
            return str(b)
    return str(DOZ_BUCKETS[-1])


def auto_days(conn, fallback: int = 7) -> int:
    """Days to request, derived from how long ago we last pulled Zillow. A run soon
    after the last pull needs only a tiny window (cheap); a long gap needs more. We
    ceil the gap (so listings posted just after the prior run aren't missed) and let
    _snap_doz round to a real bucket. No prior pull (fresh DB) -> `fallback`."""
    last = db.get_meta(conn, "last_pull_zillow")
    if not last:
        return fallback
    try:
        gap_days = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 86400
    except (ValueError, TypeError):
        return fallback
    return max(1, math.ceil(gap_days))


def search_url(max_price: int, days: int = 7) -> str:
    """A Zillow SF / For-Rent / <=max_price / newest-first search URL, restricted to
    listings added in the last `days` (Zillow's 'days on Zillow' filter, snapped to a
    real bucket) so each run pulls only recent listings — incremental + minimal Apify
    billing. `days<=0` drops the recency filter (full pull)."""
    fs = {
        "fore": {"value": False}, "auc": {"value": False}, "nc": {"value": False},
        "fsbo": {"value": False}, "cmsn": {"value": False}, "fsba": {"value": False},
        "fr": {"value": True},          # for rent
        "mp": {"max": max_price},        # monthly rent max
        "sort": {"value": "days"},       # newest first
    }
    doz = _snap_doz(days)
    if doz:
        fs["doz"] = {"value": doz}  # days on Zillow (recency)
    sqs = {
        "isMapVisible": False, "mapBounds": _SF_BOUNDS, "filterState": fs,
        "isListVisible": True,
        "regionSelection": [{"regionId": 20330, "regionType": 6}],  # San Francisco
        "pagination": {},
    }
    return ("https://www.zillow.com/san-francisco-ca/rentals/?searchQueryState="
            + urllib.parse.quote(json.dumps(sqs)))


def run_actor(max_price: int, max_items: int, days: int) -> list[dict]:
    body = {"searchUrls": [{"url": search_url(max_price, days)}],
            "extractionMethod": "PAGINATION"}
    r = requests.post(
        f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
        f"?token={APIFY_TOKEN}&maxItems={max_items}",
        json=body, timeout=300)
    if r.status_code >= 300:
        raise SystemExit(f"Apify run failed {r.status_code}: {r.text[:400]}")
    return r.json()


def _photos(item: dict) -> list[str]:
    """All distinct Zillow photo URLs in the item (imgSrc + carousel)."""
    urls = []
    if item.get("imgSrc"):
        urls.append(item["imgSrc"])
    for u in _PHOTO_RE.findall(json.dumps(item)):
        if u not in urls:
            urls.append(u)
    return urls[:15]


def main() -> None:
    if not APIFY_TOKEN:
        raise SystemExit("APIFY_TOKEN not set in .env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-items", type=int, default=40,
                    help="cap billable results per run (default 40)")
    ap.add_argument("--days", type=int, default=None,
                    help="only listings added in the last N days on Zillow "
                         "(default: auto from last pull; 0 = no recency filter / "
                         "full pull). Snapped to Zillow buckets 1/7/14/30/90.")
    args = ap.parse_args()
    cfg = common.load_config()
    max_price = cfg["max_price"]

    conn = db.connect()
    days = args.days if args.days is not None else auto_days(conn)
    src = "manual" if args.days is not None else "auto from last pull"
    print(f"[zillow] querying Apify actor (SF, for-rent, <=${max_price}, newest, "
          f"last {_snap_doz(days) or 'all'}d [{days}d {src}], max {args.max_items})...")
    items = run_actor(max_price, args.max_items, days)
    print(f"  {len(items)} listings returned")

    sess = common.session(cfg)
    new = skipped = 0
    for it in items:
        zpid = str(it.get("zpid") or "")
        price = it.get("unformattedPrice")
        # Skip building/multi-unit cards (no real zpid, price is a range).
        if not zpid.isdigit() or not price or price > max_price:
            skipped += 1
            continue
        pid = f"zl{zpid}"
        if db.listing_exists(conn, pid) or db.is_blocked(conn, pid):
            continue
        detail = it.get("detailUrl") or ""
        url = detail if detail.startswith("http") else "https://www.zillow.com" + detail
        beds = it.get("beds")
        room_type = ("studio" if beds == 0 else "1br" if beds == 1
                     else "2br_plus" if beds and beds >= 2 else "unknown")
        addr = None if it.get("isUndisclosedAddress") else it.get("addressStreet")
        ll = it.get("latLong") or {}
        lat, lng = ll.get("latitude"), ll.get("longitude")
        title = it.get("statusText") or (addr or "Zillow rental")
        photos = _photos(it)

        db.insert_stub(conn, post_id=pid, url=url, title=title, price=price,
                       room_type=room_type, area=cfg.get("unspecified_area_name", "(unspecified SF)"),
                       neighborhood="", posted_at=None)
        image_dir, image_count = fetch_detail.download_images(sess, pid, photos)
        db.update_detail(conn, pid, {
            "source": "zillow",
            "bedrooms": float(beds) if beds is not None else None,
            "bathrooms": float(it["baths"]) if it.get("baths") is not None else None,
            "sqft": it.get("area"), "lat": lat, "lng": lng, "address": addr,
            "image_urls": json.dumps(photos), "image_count": image_count,
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
          f"(skipped {skipped} building-cards/over-cap). Ready to research + vet.")


if __name__ == "__main__":
    main()
