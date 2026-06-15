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
# SF bounding box
SF_BOX = {"maxLat": 37.835, "minLat": 37.700, "maxLng": -122.355, "minLng": -122.520}
PAGE = "https://www.zumper.com/apartments-for-rent/san-francisco-ca"


def zsession() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Content-Type": "application/json", "Accept": "application/json",
        "Origin": "https://www.zumper.com", "Referer": PAGE,
        "X-Requested-With": "XMLHttpRequest",
    })
    s.get(PAGE, timeout=30)  # bootstrap cookies
    return s


def fetch_box(s, box, limit=100):
    body = {"limit": limit, "box": box, "propertyTypes": {"exclude": [16, 17]},
            "external": True, "url": "san-francisco-ca"}
    r = s.post(API, data=json.dumps(body), timeout=30)
    r.raise_for_status()
    j = r.json()
    return j.get("pins", []), j.get("matching", 0)


def collect(s, box, limit, depth, out, seen):
    """Recursively pull all pins in box, subdividing when capped."""
    try:
        pins, matching = fetch_box(s, box, limit)
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
            collect(s, q, limit, depth + 1, out, seen)
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


class DescriptionFetcher:
    """Lazily-started headless browser that reads one listing description at a
    time, reusing a single page (keeps the challenge cookie warm). Degrades to
    a no-op if Playwright/Chromium isn't available — the pipeline still runs,
    listings just keep description=None."""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._page = None
        self.enabled = True

    def _ensure(self) -> bool:
        if self._page is not None:
            return True
        if not self.enabled:
            return False
        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_page()
            return True
        except Exception as e:  # missing browser, launch failure, etc.
            print(f"  ! Zumper descriptions disabled (headless browser "
                  f"unavailable: {e})", file=sys.stderr)
            self.enabled = False
            return False

    def fetch(self, url: str) -> str | None:
        if not url or not self._ensure():
            return None
        try:
            self._page.goto(url, timeout=60000, wait_until="domcontentloaded")
            for _ in range(8):  # poll while the JS challenge resolves
                self._page.wait_for_timeout(1500)
                desc = extract_description(self._page.content())
                if desc:
                    return desc
            # Some listings keep the body only in the rendered "About" section
            # (not ld+json), lazy-loaded on scroll — fall back to that.
            return self._about_text()
        except Exception as e:
            print(f"  ! description fetch failed for {url}: {e}",
                  file=sys.stderr)
        return None

    def _about_text(self) -> str | None:
        """Read the body paragraph(s) from the rendered #about section. The
        section lazy-loads ("One sec, gathering the property details") once
        scrolled into view, so we scroll then poll its <p> elements (which hold
        the real body, excluding the heading/price chrome)."""
        try:
            self._page.locator("#about").scroll_into_view_if_needed(timeout=8000)
        except Exception:
            pass
        for _ in range(8):
            self._page.wait_for_timeout(1500)
            try:
                ps = self._page.locator("#about p")
                parts = [ps.nth(i).inner_text().strip()
                         for i in range(ps.count())]
            except Exception:
                continue
            body = "\n\n".join(p for p in parts if p)
            if len(body) >= 60:
                return body
        return None

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


def main() -> None:
    cfg = common.load_config()
    max_price = cfg["max_price"]
    conn = db.connect()
    s = zsession()

    print("[zumper] pulling SF pins (recursive box subdivision)...")
    pins: list = []
    collect(s, SF_BOX, 100, 0, pins, set())
    print(f"  {len(pins)} unique pins in SF")

    sess = common.session(cfg)  # for image downloads
    descf = DescriptionFetcher()  # headless browser for listing bodies
    new = 0
    for p in pins:
        price = p.get("min_price")
        if not price or price > max_price:
            continue
        pid = f"z{p['listing_id']}"
        url = "https://www.zumper.com" + (p.get("url") or "")
        title = p.get("building_name") or p.get("address") or "Zumper listing"
        hood = p.get("neighborhood_name") or ""
        # map by neighborhood only (NOT address — that pollutes the area label)
        area = fetch_listings.assign_area(hood, cfg) \
            or cfg.get("unspecified_area_name", "(unspecified SF)")
        beds = p.get("min_bedrooms")
        room_type = ("studio" if beds == 0 else "1br" if beds == 1
                     else "2br_plus" if beds and beds >= 2 else "unknown")
        image_ids = p.get("image_ids") or []
        # zumpercdn requires the WxH path + query params; bare sizes 404.
        image_urls = [f"https://img.zumpercdn.com/{i}/1280x960?dpr=1&fit=crop&h=542&q=76&w=991"
                      for i in image_ids]

        if db.listing_exists(conn, pid):
            continue
        db.insert_stub(conn, post_id=pid, url=url, title=title, price=price,
                       room_type=room_type, area=area, neighborhood=hood,
                       posted_at=None)
        # download images transiently for vetting
        dl_urls = [f"https://img.zumpercdn.com/{i}/1280x960" for i in image_ids]
        image_dir, image_count = fetch_detail.download_images(sess, pid, dl_urls)
        # fetch the listing body (headless) so subagents can catch room-shares
        description = descf.fetch(url)
        db.update_detail(conn, pid, {
            "source": "zumper", "bedrooms": float(beds) if beds is not None else None,
            "bathrooms": float(p["min_bathrooms"]) if p.get("min_bathrooms") is not None else None,
            "sqft": p.get("min_square_feet"), "lat": p.get("lat"), "lng": p.get("lng"),
            "address": p.get("address"), "neighborhood": hood,
            "description": description,
            "image_urls": json.dumps(image_urls), "image_count": image_count,
        })
        conn.commit()
        new += 1

    descf.close()
    db.set_meta(conn, "last_pull_zumper", db.now())
    conn.commit()
    conn.close()
    print(f"\n{new} new Zumper listings <= ${max_price} added (status='new', ready to vet).")


if __name__ == "__main__":
    main()
