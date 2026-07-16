"""Discover San Francisco rentals from Zumper's internal map API and insert them
into the same DB as Craigslist (source='zumper'), ready for the same subagent
vetting + dedup + dashboard.

Zumper serves listings via POST /api/svc/inventory/v1/listables/maplist/pins with
a lat/lng bounding box. We recursively subdivide SF until every box is under the
result cap, filter to <= max_price, and store each pin. Images are embedded from
img.zumpercdn.com (and downloaded locally only transiently for vetting).

    py scripts/fetch_zumper.py
"""
from __future__ import annotations

import json
import re
import sys

import requests

import common
import db
import fetch_detail  # for download_images
import fetch_listings  # for assign_area

API = "https://www.zumper.com/api/svc/inventory/v1/listables/maplist/pins"

# Search regions. Same downstream as Craigslist's sfc + eby passes: SF citywide,
# plus Berkeley city (East Bay BART-commute option). The Berkeley box is bounded
# south at the Oakland city line (~37.846, geo._OAKLAND_LINE_LAT) so we don't pull
# Oakland; geo.classify() still owns the final near-BART/avoid decision per coord.
REGIONS = {
    "sf": {
        "box": {"maxLat": 37.835, "minLat": 37.700, "maxLng": -122.355, "minLng": -122.520},
        "url": "san-francisco-ca",
        "page": "https://www.zumper.com/apartments-for-rent/san-francisco-ca",
        "meta_key": "last_pull_zumper",
        "label": "San Francisco",
    },
    "berkeley": {
        "box": {"maxLat": 37.906, "minLat": 37.846, "maxLng": -122.234, "minLng": -122.325},
        "url": "berkeley-ca",
        "page": "https://www.zumper.com/apartments-for-rent/berkeley-ca",
        "meta_key": "last_pull_zumper_berkeley",
        "label": "Berkeley (East Bay)",
    },
}
# Back-compat alias used elsewhere.
SF_BOX = REGIONS["sf"]["box"]


def zsession(page: str = REGIONS["sf"]["page"]) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Content-Type": "application/json", "Accept": "application/json",
        "Origin": "https://www.zumper.com", "Referer": page,
        "X-Requested-With": "XMLHttpRequest",
    })
    s.get(page, timeout=30)  # bootstrap cookies
    return s


def fetch_box(s, box, limit=100, url="san-francisco-ca"):
    body = {"limit": limit, "box": box, "propertyTypes": {"exclude": [16, 17]},
            "external": True, "url": url}
    r = s.post(API, data=json.dumps(body), timeout=30)
    r.raise_for_status()
    j = r.json()
    return j.get("pins", []), j.get("matching", 0)


def collect(s, box, limit, depth, out, seen, url="san-francisco-ca"):
    """Recursively pull all pins in box, subdividing when capped."""
    try:
        pins, matching = fetch_box(s, box, limit, url)
    except requests.exceptions.RequestException as e:
        print(f"  ! box failed: {e}", file=sys.stderr)
        return
    if matching > len(pins) and depth < 5:
        midlat = (box["maxLat"] + box["minLat"]) / 2
        midlng = (box["maxLng"] + box["minLng"]) / 2
        quads = [
            {"maxLat": box["maxLat"], "minLat": midlat, "maxLng": midlng, "minLng": box["minLng"]},
            {"maxLat": box["maxLat"], "minLat": midlat, "maxLng": box["maxLng"], "minLng": midlng},
            {"maxLat": midlat, "minLat": box["minLat"], "maxLng": midlng, "minLng": box["minLng"]},
            {"maxLat": midlat, "minLat": box["minLat"], "maxLng": box["maxLng"], "minLng": midlng},
        ]
        for q in quads:
            collect(s, q, limit, depth + 1, out, seen, url)
    else:
        for p in pins:
            if p["listing_id"] not in seen:
                seen.add(p["listing_id"])
                out.append(p)


# --- listing description -------------------------------------------------
# The map-pins API carries no description, and the listing detail pages are
# behind a JS bot-challenge (plain GET returns a "Client Challenge" stub). A
# real headless browser clears the challenge; the listing body then lives in the
# page's application/ld+json. We MUST capture this — Zumper tags room-shares as
# "1 bedroom", so without the description a private-room-in-a-shared-unit looks
# identical to a real 1BR (see the SHARED-ROOM GATE in the vetting rubric).

_LDJSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def extract_description(html: str) -> str | None:
    """Pull the listing body from a rendered page's ld+json blocks.

    Collects every `description` string in any ld+json object, drops the SEO
    boilerplate ("View <address> rent availability...") and too-short junk
    ("Review and Rating"), and returns the longest real one. Buildings with no
    real body yield None (fine — they're vetted on photos/amenities)."""
    cands: list[str] = []
    for m in _LDJSON_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                d = o.get("description")
                if isinstance(d, str):
                    cands.append(d.strip())
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)
    cands = [c for c in cands
             if len(c) >= 60 and not c.lower().startswith("view ")]
    return max(cands, key=len) if cands else None


# --- chromerpc full-page detail (replaces the dead Playwright path) -----------
# Zumper renders the body + contact client-side and lazy-loads sections on scroll
# ("One sec, gathering ..."). A real browser (chromerpc) scroll-loads the whole
# page, then we read the ABOUT body (so the SHARED-ROOM GATE works — a private-
# room-in-a-shared-house is invisible from photos alone) + the posted age + a
# best-effort contact. Contact placement varies, so we scan the WHOLE rendered
# text rather than rely on a fixed selector.
_AGE_RE = re.compile(r"(\d+)\+?\s*(hour|day|week|month)s?\s*ago", re.I)
_PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?:\s*(?:ext\.?|x)\s*\d+)?", re.I)


def _age_to_iso(text: str) -> str | None:
    if re.search(r"\b(today|just posted)\b", text, re.I):
        return db.now()
    m = _AGE_RE.search(text)
    if not m:
        return None
    hrs = {"hour": 1, "day": 24, "week": 168, "month": 720}[m.group(2).lower()] * int(m.group(1))
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hrs)).isoformat()


def _about_from_text(text: str) -> str | None:
    """The real body sits under 'About <addr>' (after a 'Sweet deal!' note) and
    before 'Building details'/'Neighborhood'. This is where room-share language
    ('private master bedroom', 'sharing this house', 'roommates') shows up."""
    k = text.find("About ")
    if k < 0:
        return None
    ends = [i for i in (text.find("Building details", k), text.find("Neighborhood", k),
                        text.find("Similar ", k)) if i > k] + [k + 1600]
    seg = text[k:min(ends)]
    seg = re.sub(r"^About\b.*?\bUSA\b\s*", "", seg, flags=re.S)         # strip 'About <addr>, USA'
    if seg.lstrip().startswith("About"):                                # no 'USA' -> try the CA zip
        seg = re.sub(r"^About\b.*?CA \d{5},?\s*", "", seg, flags=re.S)
    seg = re.sub(r"^Home\S.*?\n", "", seg).strip()
    seg = re.sub(r"Sweet deal!.*?median[^.]*\.", "", seg, flags=re.S).strip()
    seg = re.sub(r"\s+", " ", seg).strip(" ,")
    return seg if len(seg) >= 60 else None


_NONNAME = {"contact", "avail", "ask", "report", "request", "view", "tour", "check",
            "property", "question", "available", "rent", "home", "house", "apartment"}


def _contact_from_text(text: str) -> dict:
    """Best-effort, no fixed selector: the contact (name + routed number/extension)
    sits inconsistently, so find the phone and grab the window around it (the name
    usually precedes it). Stored for the vetting step to read. Zumper numbers are
    routed (no direct landlord line)."""
    out = {"contact_name": None, "phone": None, "contact_details": None}
    ph = _PHONE_RE.search(text)
    if not ph:
        return out
    out["phone"] = re.sub(r"\s+", " ", ph.group(0)).strip()
    # contact_details only when there's a real special step (extension/code beyond the
    # bare number); a plain number lives in `phone`, and we never store surrounding prose.
    if re.search(r"\b(?:ext\.?|x)\s*\d{1,5}\b|\btext\s+\d{1,5}\s+to\b", out["phone"], re.I):
        out["contact_details"] = ("Call/Text " + out["phone"])[:160]
    pre = re.sub(r"\s+", " ", text[max(0, ph.start() - 70):ph.start()]).strip()
    names = [w for w in re.findall(r"\b([A-Z][a-z]{1,})\b", pre) if w.lower() not in _NONNAME]
    if names:
        out["contact_name"] = names[-1]
    return out


# NOTE: the old chromerpc_zumper_detail() auto-driver and the Playwright
# DescriptionFetcher were REMOVED — no browser is auto-driven for Zumper anymore.
# Each new Zumper listing's body/posted-age/contact is gathered BY HAND via chromerpc
# (manual detail pass, like Zillow/Apartments — see MANUAL_SOURCES.md). When you read
# a hand-gathered page's text/HTML, the parsers above (_about_from_text, _age_to_iso,
# _contact_from_text, extract_description) are still here to help you turn it into
# {description, posted_at, contact_name, phone} before you write the enrich fields.
def pull_region(conn, cfg, region_key, sess) -> int:
    """Pull one region's pins (SF or Berkeley) and insert new ones. Returns count.

    Inserts map-API STUBS only (coords + price + photos + raw fields). The listing
    BODY / posted age / contact are NOT auto-driven anymore — like Zillow and
    Apartments.com, each new Zumper listing's detail page is gathered BY HAND by the
    LLM via chromerpc (see MANUAL_SOURCES.md 'BROWSER = MANUAL, ALWAYS'). So rows
    land with description=None and must have the body read by hand before the
    SHARED-ROOM GATE can be trusted."""
    reg = REGIONS[region_key]
    max_price = cfg["max_price"]
    s = zsession(reg["page"])

    # Watermark = the instant BEFORE this pass begins (run start), NOT db.now()
    # after it. Fetch runs at the very START of a refresh; stamping the start
    # guarantees the NEXT run reads back to THIS run's start-of-fetch with zero
    # gap — so a listing posted DURING this run's later stages is still caught
    # next time. Stamping after the pass would silently skip that window.
    pull_started = db.now()

    print(f"[zumper] pulling {reg['label']} pins (recursive box subdivision)...")
    pins: list = []
    collect(s, reg["box"], 100, 0, pins, set(), reg["url"])
    print(f"  {len(pins)} unique pins in {reg['label']}")

    new = 0
    for p in pins:
        price = p.get("min_price")
        if not price or price > max_price:   # cap filter + provisional price only
            continue
        pid = f"z{p['listing_id']}"
        url = "https://www.zumper.com" + (p.get("url") or "")
        image_ids = p.get("image_ids") or []
        # zumpercdn requires the WxH path + query params; bare sizes 404.
        image_urls = [f"https://img.zumpercdn.com/{i}/1280x960?dpr=1&fit=crop&h=542&q=76&w=991"
                      for i in image_ids]

        if db.listing_exists(conn, pid) or db.is_blocked(conn, pid):
            continue
        # NO AUTO-MAP. We do NOT derive title/room_type/beds/baths/sqft/address/
        # neighborhood from the map API — that logic is gone. We keep only coords
        # (area model), price (cap + provisional), url, photos, the raw page
        # description, and the raw map-API fields in source_extra.raw. The vetting
        # subagent reads all that (+ photos) and AUTHORS every display field in its
        # enrich block. See CLAUDE.md (LLM-authored, never auto-mapped).
        db.insert_stub(conn, post_id=pid, url=url,
                       title=f"Zumper {p['listing_id']} — vet for details", price=price,
                       room_type="unknown",
                       area=cfg.get("unspecified_area_name", "(unspecified SF)"),
                       neighborhood=None, posted_at=None)
        # download images transiently for vetting
        dl_urls = [f"https://img.zumpercdn.com/{i}/1280x960" for i in image_ids]
        image_dir, image_count = fetch_detail.download_images(sess, pid, dl_urls)
        # NO auto browser drive. The body/contact/posted-age are gathered BY HAND
        # later (manual chromerpc detail pass, like Zillow/Apartments) — the map API
        # has no description, so this stub lands with description=None until then.
        detail = {"description": None}
        raw = {
            "building_name": p.get("building_name"),
            "address": p.get("address"),
            "min_bedrooms": p.get("min_bedrooms"),
            "min_bathrooms": p.get("min_bathrooms"),
            "min_square_feet": p.get("min_square_feet"),
            "neighborhood_name": p.get("neighborhood_name"),
            "min_price": price,
        }
        raw = {k: v for k, v in raw.items() if v not in (None, "")}
        fields = {
            "source": "zumper", "lat": p.get("lat"), "lng": p.get("lng"),
            "description": detail.get("description"),
            "image_urls": json.dumps(image_urls), "image_count": image_count,
            "source_extra": json.dumps({"raw": raw}) if raw else None,
        }
        # carry posted_at + contact only when we actually found them (don't null-overwrite)
        for k in ("posted_at", "phone", "contact_name", "contact_details"):
            if detail.get(k):
                fields[k] = detail[k]
        db.update_detail(conn, pid, fields)
        conn.commit()
        new += 1

    db.set_meta(conn, reg["meta_key"], pull_started)
    conn.commit()
    print(f"  {new} new Zumper listings <= ${max_price} in {reg['label']} "
          f"(status='new', ready to vet).")
    return new


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Pull Zumper rentals into the DB.")
    ap.add_argument("--region", default="both", choices=["sf", "berkeley", "both"],
                    help="'sf' (default citywide SF), 'berkeley' (East Bay near-BART), "
                         "or 'both'")
    args = ap.parse_args()
    regions = ["sf", "berkeley"] if args.region == "both" else [args.region]

    cfg = common.load_config()
    conn = db.connect()
    sess = common.session(cfg)         # for image downloads
    print("[zumper] map-API stubs only — listing bodies/contacts are gathered BY HAND "
          "via chromerpc (manual detail pass), like Zillow/Apartments.")

    total = 0
    for rk in regions:
        total += pull_region(conn, cfg, rk, sess)

    conn.close()
    print(f"\n{total} new Zumper listings added across {', '.join(regions)} "
          f"(description=None — gather each body BY HAND before vetting).")


if __name__ == "__main__":
    main()
