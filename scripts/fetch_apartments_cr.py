"""Discover SF + Berkeley rentals from Apartments.com via the LOCAL headful
chromerpc browser — HUMAN interaction only, NO page JavaScript (per THUMB_RULES).

Apartments.com (CoStar/Akamai) blocks headless instantly; a HEADFUL Chrome with a
homepage warm-up (sets the _abck cookie) gets in. The search result page is fully
SERVER-RENDERED: every listing is a `.placard` element with data-url /
data-streetaddress / data-listingid attributes + visible price/beds/type/phone. So
we read it JS-free: navigate -> read placards via the CDP DOM (QuerySelectorAll +
GetAttributes + GetOuterHTML) and parse in Python. DROP "Room for Rent" placards.
For each NEW non-room listing, open the detail page and read the #descriptionSection
body + photos + phone + embedded lat/lng (else geocode) from the serialized HTML.

NO Runtime.Evaluate anywhere. NO AUTO-MAP — store coords/price/url/photos/desc +
raw card fields in source_extra.raw; the vetting subagent authors the display
fields. Images are best-effort (the dashboard links to the original listing anyway).

    py scripts/fetch_apartments_cr.py
    py scripts/fetch_apartments_cr.py --region sf --max-detail 30
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time

import common
import db
import fetch_detail            # download_images, geocode
import fetch_cl_contacts as cr  # CDP DOM + human-input helpers (navigate, _qsa, _outer, _attrs, ...)

REGIONS = {
    "sf": {"slug": "san-francisco-ca", "city": "San Francisco, CA",
           "meta_key": "last_pull_apartments", "label": "San Francisco"},
    "berkeley": {"slug": "berkeley-ca", "city": "Berkeley, CA",
                 "meta_key": "last_pull_apartments_berkeley", "label": "Berkeley (East Bay)"},
}

_TYPE_RE = re.compile(r"(Room for Rent|House for Rent|Condo for Rent|Townhome for Rent|Apartment for Rent|For Rent by Owner)", re.I)
_PHONE = re.compile(r"\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}")
_PRICE = re.compile(r"\$[\d,]+")
_BEDS = re.compile(r"(Studio|\d+\s*Bed)", re.I)
_IMG = re.compile(r"https://images1\.apartments\.com/i2/[^\s\"'\\)]+\.(?:jpg|jpeg|png|webp)[^\s\"'\\)]*", re.I)
_LATLNG = re.compile(r'"latitude":\s*"?(-?\d+\.\d+)"?[^}]{0,80}?"longitude":\s*"?(-?\d+\.\d+)"?')


def _chromerpc_ready() -> bool:
    return "_err" not in cr._call("cdp.dom.DOMService/GetDocument", {"depth": 0})


def _card_fields(txt: str) -> dict:
    return {
        "price": (_PRICE.search(txt).group(0) if _PRICE.search(txt) else None),
        "beds": (_BEDS.search(txt).group(0) if _BEDS.search(txt) else None),
        "type": (_TYPE_RE.search(txt).group(0) if _TYPE_RE.search(txt) else None),
        "phone": (_PHONE.search(txt).group(0) if _PHONE.search(txt) else None),
    }


def _price_int(s):
    m = re.search(r"\$([\d,]+)", str(s or ""))
    return int(m.group(1).replace(",", "")) if m else None


def collect_cards(slug: str, cap: int, max_pages: int = 6) -> list[dict]:
    base = f"https://www.apartments.com/{slug}/under-{cap}/"
    seen, out = {}, []
    for pg in range(1, max_pages + 1):
        cr.navigate(base if pg == 1 else f"{base}{pg}/")
        time.sleep(5)
        ttl = cr._qs("title")
        title = cr._node_text(ttl) if ttl else ""
        if "denied" in title.lower():
            print(f"  ! page {pg}: Access Denied (need headful chromerpc + warm-up)", file=sys.stderr)
            break
        placards = cr._qsa(".placard")
        new = 0
        for nid in placards:
            a = cr._attrs(nid)
            url = a.get("data-url") or ""
            if not url or url in seen:
                continue
            seen[url] = 1
            txt = cr._node_text(nid)
            out.append({"url": url, "id": a.get("data-listingid") or "",
                        "addr": a.get("data-streetaddress") or "", **_card_fields(txt)})
            new += 1
        if new == 0 and pg > 1:
            break
    return out


def _detail(url: str) -> dict:
    cr.navigate(url)
    time.sleep(4)
    html = cr._outer(cr._doc_root())
    dnode = cr._qs("#descriptionSection") or cr._qs(".descriptionText") or cr._qs("[data-tab-content=description]")
    desc = cr._node_text(dnode) if dnode else ""
    imgs = list(dict.fromkeys(_IMG.findall(html)))[:12]
    m = _LATLNG.search(html)
    lat = float(m.group(1)) if m else None
    lng = float(m.group(2)) if m else None
    text = re.sub(r"<[^>]+>", " ", html)
    pm = _PHONE.search(text)
    return {"desc": desc, "imgs": imgs, "lat": lat, "lng": lng,
            "phone": (pm.group(0) if pm else "")}


def pull_region(conn, cfg, sess, region_key: str, max_detail: int) -> int:
    reg = REGIONS[region_key]
    cap = cfg["max_price"]
    cr.navigate("https://www.apartments.com/"); time.sleep(5)   # Akamai warm-up (_abck cookie)
    print(f"[apartments-cr] {reg['label']}: searching under ${cap} ...")
    cards = collect_cards(reg["slug"], cap)
    rooms = [c for c in cards if "room for rent" in (c.get("type") or "").lower()]
    cands = [c for c in cards if c not in rooms]
    print(f"  {len(cards)} card(s); dropped {len(rooms)} 'Room for Rent'; {len(cands)} candidate(s)")

    new = 0
    for c in cands:
        pid = "apt" + (c.get("id") or __import__("hashlib").md5(c["url"].encode()).hexdigest()[:12])
        if db.listing_exists(conn, pid) or db.is_blocked(conn, pid):
            continue
        if new >= max_detail:
            print(f"  reached --max-detail ({max_detail}); remaining deferred to next run")
            break
        price = _price_int(c.get("price"))
        if price and price > cap:
            continue
        d = _detail(c["url"])
        lat, lng, place = d.get("lat"), d.get("lng"), None
        if (lat is None or lng is None) and c.get("addr"):
            g = fetch_detail.geocode(sess, f"{c['addr']}, {reg['city']}")
            if g:
                lat, lng, place = g
            time.sleep(1)   # be kind to Nominatim
        if not db.insert_stub(conn, post_id=pid, url=c["url"],
                              title=f"Apartments.com {pid} — vet for details", price=price,
                              room_type="unknown",
                              area=cfg.get("unspecified_area_name", "(unspecified SF)"),
                              neighborhood=place, posted_at=None):
            continue
        imgs = d.get("imgs") or []
        image_dir, image_count = fetch_detail.download_images(sess, pid, imgs)
        raw = {k: v for k, v in {
            "card_price": c.get("price"), "beds": c.get("beds"), "type": c.get("type"),
            "address": c.get("addr"), "card_phone": c.get("phone"),
        }.items() if v not in (None, "")}
        fields = {
            "source": "apartments", "lat": lat, "lng": lng, "address": c.get("addr"),
            "description": d.get("desc") or None,
            "image_urls": json.dumps(imgs), "image_count": image_count,
            "source_extra": json.dumps({"raw": raw}) if raw else None,
        }
        if d.get("phone") or c.get("phone"):
            fields["phone"] = d.get("phone") or c.get("phone")
        db.update_detail(conn, pid, fields)
        conn.commit()
        new += 1

    db.set_meta(conn, reg["meta_key"], db.now())
    conn.commit()
    print(f"  {new} new Apartments.com listing(s) <= ${cap} in {reg['label']} "
          f"(status='new', ready to vet).")
    return new


def main() -> None:
    ap = argparse.ArgumentParser(description="Pull Apartments.com rentals via headful chromerpc (JS-free).")
    ap.add_argument("--region", default="both", choices=["sf", "berkeley", "both"])
    ap.add_argument("--max-detail", type=int, default=40,
                    help="max NEW detail pages to fetch per region per run (default 40)")
    args = ap.parse_args()
    regions = ["sf", "berkeley"] if args.region == "both" else [args.region]

    if not _chromerpc_ready():
        print("[apartments-cr] chromerpc not reachable on :50051 — skipping Apartments "
              "(start it headful: chromerpc -addr :50051 -headless=false).", file=sys.stderr)
        return

    cfg = common.load_config()
    conn = db.connect()
    sess = common.session(cfg)
    total = 0
    for rk in regions:
        total += pull_region(conn, cfg, sess, rk, args.max_detail)
    conn.close()
    print(f"\n{total} new Apartments.com listing(s) added across {', '.join(regions)}.")


if __name__ == "__main__":
    main()
