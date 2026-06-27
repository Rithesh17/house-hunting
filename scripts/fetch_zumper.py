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


# --- chromerpc full-page detail (replaces the dead Playwright path) -----------
# Zumper renders the body + contact client-side and lazy-loads sections on scroll
# ("One sec, gathering ..."). A real browser (chromerpc) scroll-loads the whole
# page, then we read the ABOUT body (so the SHARED-ROOM GATE works — a private-
# room-in-a-shared-house is invisible from photos alone) + the posted age + a
# best-effort contact. Contact placement varies, so we scan the WHOLE rendered
# text rather than rely on a fixed selector.
_AGE_RE = re.compile(r"(\d+)\+?\s*(hour|day|week|month)s?\s*ago", re.I)
_PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?:\s*(?:ext\.?|x)\s*\d+)?", re.I)


def _chromerpc_ready() -> bool:
    try:
        import fetch_cl_contacts as cr
        return "_err" not in cr._call(
            "cdp.runtime.RuntimeService/Evaluate",
            {"expression": "1", "return_by_value": True})
    except Exception:
        return False


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


def chromerpc_zumper_detail(url: str) -> dict | None:
    """Render the full Zumper listing via chromerpc (scroll to load every lazy
    section) and extract {description, posted_at, contact_name, phone,
    contact_details}. Returns None if chromerpc isn't reachable on :50051."""
    import time
    import fetch_cl_contacts as cr
    if not _chromerpc_ready():
        return None
    try:
        cr._call("cdp.emulation.EmulationService/SetDeviceMetricsOverride",
                 {"width": 1440, "height": 1200, "deviceScaleFactor": 1, "mobile": False})
        cr.navigate(url)
        time.sleep(6)
        H = cr.ev("document.body.scrollHeight") or 6000
        for _ in range(3):                          # scroll-load until lazy sections settle
            for y in range(0, int(H) + 800, 500):
                cr.ev(f"window.scrollTo(0,{y})")
                time.sleep(0.45)
            if not cr.ev("(document.body.innerText.match(/One sec, gathering/g)||[]).length"):
                break
            H = cr.ev("document.body.scrollHeight") or H
        cr.ev("window.scrollTo(0,0)")
        time.sleep(0.4)
        html = cr.ev("document.documentElement.outerHTML") or ""
        text = cr.ev("document.body.innerText") or ""
    except Exception as e:
        print(f"  ! chromerpc zumper detail failed for {url}: {e}", file=sys.stderr)
        return None
    out = {"description": _about_from_text(text) or extract_description(html),
           "posted_at": _age_to_iso(text)}
    out.update(_contact_from_text(text))
    return out


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
    descf = DescriptionFetcher()  # fallback (Playwright; usually a no-op here)
    use_cr = _chromerpc_ready()
    print("  [zumper] detail via " + ("chromerpc full-page render (description + contact)"
          if use_cr else "Playwright fallback (likely description=None)"))
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
        # Full-page detail so the SHARED-ROOM GATE works: chromerpc renders the
        # body + contact (preferred); else the dead Playwright path -> None.
        detail = chromerpc_zumper_detail(url) if use_cr else None
        if detail is None:
            detail = {"description": descf.fetch(url)}
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

    descf.close()
    db.set_meta(conn, "last_pull_zumper", db.now())
    conn.commit()
    conn.close()
    print(f"\n{new} new Zumper listings <= ${max_price} added (status='new', ready to vet).")


if __name__ == "__main__":
    main()
