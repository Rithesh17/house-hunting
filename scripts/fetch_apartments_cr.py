"""Discover SF + Berkeley rentals from Apartments.com via the LOCAL headful
chromerpc browser. Inserts source='apartments' rows into the same DB, ready for
the same subagent vetting + dedup + dashboard.

WHY chromerpc, headful: Apartments.com (CoStar) is behind Akamai. A HEADLESS
browser sends a `HeadlessChrome` UA and gets an instant "Access Denied"; a HEADFUL
Chrome with a normal UA passes (after a homepage warm-up that sets the _abck
cookie). So this needs chromerpc started with `-headless=false` (refresh.py does).
No-ops if chromerpc isn't reachable on :50051.

HOW: warm up on the homepage, then navigate `/<region>/under-<cap>/` (+ paginated
/N/). Read each placard's address/price/beds/type/phone straight off the card.
DROP "Room for Rent" cards (Apartments.com labels shared rooms explicitly). For
each remaining NEW listing, open the detail page and read the DESCRIPTION
(#descriptionSection) + photos + phone + embedded lat/lng (else geocode the
address). NO AUTO-MAP — store coords/price/url/photos/description + raw card
fields in source_extra.raw; the vetting subagent authors the display fields.

NOTE: Apartments.com lists managed BUILDINGS (not dated posts) and has no real
"posted within 24h" — recency is approximate. Photo URLs (images1.apartments.com)
sometimes resist direct download; we still store the remote URLs (the dashboard
embeds those), and local copies are best-effort for the vetting step.

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
import fetch_detail  # download_images, geocode

REGIONS = {
    "sf": {"slug": "san-francisco-ca", "meta_key": "last_pull_apartments", "label": "San Francisco"},
    "berkeley": {"slug": "berkeley-ca", "meta_key": "last_pull_apartments_berkeley",
                 "label": "Berkeley (East Bay)"},
}

JS_CARDS = r"""
(function(){
 return JSON.stringify([...document.querySelectorAll('.placard')].map(function(c){
   return {id:c.getAttribute('data-listingid')||'', url:c.getAttribute('data-url')||'',
           addr:c.getAttribute('data-streetaddress')||'',
           txt:(c.innerText||'').replace(/\s+/g,' ').trim().slice(0,200)};
 }).filter(x=>x.url));
})()
"""

JS_DETAIL = r"""
(function(){
 var d=(document.querySelector('#descriptionSection, .descriptionText, [data-tab-content=description]')||{}).innerText||'';
 var imgs=[...new Set([...document.querySelectorAll('img')].map(i=>i.src).filter(s=>/images1\.apartments\.com\/i2\//.test(s)))];
 var lat=null,lng=null;
 var m=document.body.innerHTML.match(/"latitude":\s*"?(-?\d+\.\d+)"?[^}]{0,60}?"longitude":\s*"?(-?\d+\.\d+)"?/);
 if(m){lat=parseFloat(m[1]);lng=parseFloat(m[2]);}
 var body=document.body.innerText||'';
 var phone=(body.match(/\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}/)||[''])[0];
 return JSON.stringify({desc:d.slice(0,4000), imgs:imgs.slice(0,12), lat:lat, lng:lng, phone:phone});
})()
"""

_TYPE_RE = re.compile(r"(Room for Rent|House for Rent|Condo for Rent|Townhome for Rent|Apartment for Rent|For Rent by Owner)", re.I)


def _chromerpc_ready() -> bool:
    try:
        import fetch_cl_contacts as cr
        return "_err" not in cr._call("cdp.runtime.RuntimeService/Evaluate",
                                      {"expression": "1", "return_by_value": True})
    except Exception:
        return False


def _card_fields(txt: str) -> dict:
    return {
        "price": (re.search(r"\$[\d,]+", txt) or [None])[0] if re.search(r"\$[\d,]+", txt) else None,
        "beds": (re.search(r"(Studio|\d+\s*Bed)", txt, re.I) or [None])[0] if re.search(r"(Studio|\d+\s*Bed)", txt, re.I) else None,
        "type": (_TYPE_RE.search(txt).group(0) if _TYPE_RE.search(txt) else None),
        "phone": (re.search(r"\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}", txt) or [None])[0] if re.search(r"\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}", txt) else None,
    }


def _price_int(s):
    m = re.search(r"\$([\d,]+)", str(s or ""))
    return int(m.group(1).replace(",", "")) if m else None


def collect_cards(cr, slug: str, cap: int, max_pages: int = 6) -> list[dict]:
    base = f"https://www.apartments.com/{slug}/under-{cap}/"
    seen, out = {}, []
    for pg in range(1, max_pages + 1):
        cr.navigate(base if pg == 1 else f"{base}{pg}/")
        time.sleep(5)
        title = cr.ev("document.title") or ""
        if "Access Denied" in title or "denied" in title.lower():
            print(f"  ! page {pg}: Access Denied (need headful chromerpc + warm-up)", file=sys.stderr)
            break
        items = cr.ev(JS_CARDS)
        if not isinstance(items, list):
            break
        new = 0
        for x in items:
            if x["url"] not in seen:
                seen[x["url"]] = 1
                x.update(_card_fields(x.get("txt", "")))
                out.append(x); new += 1
        if new == 0 and pg > 1:
            break
    return out


def pull_region(conn, cfg, sess, cr, region_key: str, max_detail: int) -> int:
    reg = REGIONS[region_key]
    cap = cfg["max_price"]
    # Akamai warm-up: hit the homepage so the bot-manager cookie is set first.
    cr.navigate("https://www.apartments.com/"); time.sleep(5)
    print(f"[apartments-cr] {reg['label']}: searching under ${cap} ...")
    cards = collect_cards(cr, reg["slug"], cap)
    rooms = [c for c in cards if "room for rent" in (c.get("type") or "").lower()]
    cands = [c for c in cards if c not in rooms]
    print(f"  {len(cards)} card(s); dropped {len(rooms)} 'Room for Rent'; {len(cands)} candidate(s)")

    new = 0
    for c in cands:
        url = c["url"]
        pid = "apt" + (c.get("id") or __import__("hashlib").md5(url.encode()).hexdigest()[:12])
        if db.listing_exists(conn, pid) or db.is_blocked(conn, pid):
            continue
        if new >= max_detail:
            print(f"  reached --max-detail ({max_detail}); remaining deferred to next run")
            break
        price = _price_int(c.get("price"))
        if price and price > cap:
            continue
        cr.navigate(url); time.sleep(4)
        d = cr.ev(JS_DETAIL)
        if not isinstance(d, dict):
            d = {}
        lat, lng = d.get("lat"), d.get("lng")
        place = None
        if (lat is None or lng is None) and c.get("addr"):
            geo = fetch_detail.geocode(sess, c["addr"] + ", San Francisco, CA"
                                       if region_key == "sf" else c["addr"] + ", Berkeley, CA")
            if geo:
                lat, lng, place = geo
        if not db.insert_stub(conn, post_id=pid, url=url,
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
    ap = argparse.ArgumentParser(description="Pull Apartments.com rentals via headful chromerpc.")
    ap.add_argument("--region", default="both", choices=["sf", "berkeley", "both"])
    ap.add_argument("--max-detail", type=int, default=40,
                    help="max NEW detail pages to fetch per region per run (default 40)")
    args = ap.parse_args()
    regions = ["sf", "berkeley"] if args.region == "both" else [args.region]

    if not _chromerpc_ready():
        print("[apartments-cr] chromerpc not reachable on :50051 — skipping Apartments "
              "(start it headful: chromerpc -addr :50051 -headless=false).", file=sys.stderr)
        return
    import fetch_cl_contacts as cr
    cr._call("cdp.emulation.EmulationService/SetDeviceMetricsOverride",
             {"width": 1366, "height": 900, "deviceScaleFactor": 1, "mobile": False})

    cfg = common.load_config()
    conn = db.connect()
    sess = common.session(cfg)
    total = 0
    for rk in regions:
        total += pull_region(conn, cfg, sess, cr, rk, args.max_detail)
    conn.close()
    print(f"\n{total} new Apartments.com listing(s) added across {', '.join(regions)}.")


if __name__ == "__main__":
    main()
