"""Discover SF + Berkeley rentals from Zillow via the LOCAL headful chromerpc
browser (replaces the paid Apify actor — `fetch_zillow.py`). Inserts source='zillow'
rows into the same DB, ready for the same subagent vetting + dedup + dashboard.

WHY chromerpc, headful: Zillow is behind PerimeterX. A HEADLESS browser (or the
default chromerpc launch) triggers the "Press & Hold" / "Access denied" wall. A
HEADFUL Chrome with a normal UA passes cleanly. So this needs chromerpc started
with `-headless=false`; refresh.py launches it that way. If chromerpc isn't
reachable on :50051 this adapter no-ops (the pipeline still runs).

HOW: navigate the Zillow rentals search with a `searchQueryState` URL (price cap +
sort=Newest + region map bounds), parse the page's embedded `listResults` JSON
(every card: price, beds, sqft, address, detailUrl, recency, lat/lng), enforce the
<= max_price cap (Zillow shows building cards whose range can exceed the cap), skip
already-seen / blocklisted ids, then for each NEW listing open its detail page and
read the DESCRIPTION FROM THE DOM (NOT __NEXT_DATA__ — Zillow omits the body there
for /homedetails/, and the SHARED-ROOM GATE needs the real text) + photos + contact.

NO AUTO-MAP: like fetch_zumper, we store only coords (area model), price (cap +
provisional), url, photos, the page description, and the raw card fields in
source_extra.raw. The vetting subagent reads all that + photos and AUTHORS every
display field (room_type/beds/title/...). See CLAUDE.md.

    py scripts/fetch_zillow_cr.py                 # both regions
    py scripts/fetch_zillow_cr.py --region sf --max-detail 40
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
from urllib.parse import quote

import common
import db
import fetch_detail  # download_images

# Region map bounds. Berkeley bounded south at the Oakland line (geo._OAKLAND_LINE_LAT
# ~37.846) so we don't pull Oakland; geo.classify() owns the final per-coord call.
REGIONS = {
    "sf": {"bounds": {"west": -122.55, "east": -122.35, "south": 37.70, "north": 37.84},
           "term": "San Francisco, CA", "url": "san-francisco-ca",
           "meta_key": "last_pull_zillow", "label": "San Francisco"},
    "berkeley": {"bounds": {"west": -122.33, "east": -122.23, "south": 37.846, "north": 37.908},
                 "term": "Berkeley, CA", "url": "berkeley-ca",
                 "meta_key": "last_pull_zillow_berkeley", "label": "Berkeley (East Bay)"},
}

JS_RESULTS = r"""
(function(){
 function find(o,key,d){ if(d>12||!o||typeof o!=='object')return null; if(Array.isArray(o[key]))return o[key];
   for(var k in o){var r=find(o[k],key,d+1); if(r)return r;} return null; }
 var data=null,nx=document.getElementById('__NEXT_DATA__');
 if(nx){try{data=JSON.parse(nx.textContent);}catch(e){}}
 if(!data){var s=[...document.querySelectorAll('script')].map(x=>x.textContent).find(t=>t&&t.includes('listResults'));
   if(s){var i=s.indexOf('{');try{data=JSON.parse(s.slice(i));}catch(e){}}}
 if(!data)return JSON.stringify({err:(document.title||'').slice(0,60),items:[]});
 var lr=find(data,'listResults',0)||[];
 return JSON.stringify({err:'',items:lr.map(function(x){return {
   zpid:x.zpid,
   price:(x.units&&x.units.length?x.units.map(u=>u.price).join(' | '):(x.price||'')),
   beds:x.beds, baths:x.baths, sqft:x.area, addr:x.address, url:x.detailUrl,
   status:(x.variableData&&x.variableData.text)||x.statusText||'',
   lat:x.latLong&&x.latLong.latitude, lng:x.latLong&&x.latLong.longitude,
   hometype:x.hdpData&&x.hdpData.homeInfo&&x.hdpData.homeInfo.homeType };})});
})()
"""

JS_DETAIL = r"""
(function(){
 // Real listing body lives in [data-testid=description] for /homedetails/. For
 // building pages (/b/, /apartments/) fall back to the ld+json description (the
 // building overview). NEVER scrape a greedy 'longest text block' - it grabs the
 // 'Nearby apartments' carousel and would poison the shared-room gate.
 var el=document.querySelector('[data-testid=description]')
        || document.querySelector('[data-test=building-description]');
 var desc=el?el.innerText:'';
 if(!desc||desc.length<60){
   [...document.querySelectorAll('script[type="application/ld+json"]')].forEach(function(s){
     try{var stack=[JSON.parse(s.textContent)];while(stack.length){var o=stack.pop();
       if(o&&typeof o==='object'){if(typeof o.description==='string'&&o.description.length>desc.length)desc=o.description;
         for(var k in o)stack.push(o[k]);}}}catch(e){}});
 }
 desc=(desc||'').replace(/\s*Show (more|less)\s*$/i,'').trim();
 var imgs=[];[...document.querySelectorAll('img')].forEach(function(i){var s=i.src||'';if(/photos\.zillowstatic.*\.(jpg|jpeg|webp|png)/i.test(s))imgs.push(s.replace(/-cc_ft_\d+/,'-cc_ft_960'));});
 imgs=[...new Set(imgs)];
 var body=document.body.innerText||'';
 var phone=(body.match(/\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}/)||[''])[0];
 return JSON.stringify({desc:desc.slice(0,4000), imgs:imgs.slice(0,12), phone:phone});
})()
"""

_AGE_RE = re.compile(r"(\d+)\+?\s*(hour|day|week|month)s?\s*ago", re.I)
# Zillow SEO / chrome boilerplate that is NOT a real listing body (building pages
# leak the page-level ld+json blurb). Reject these so the shared-room gate never
# reads junk; a real /homedetails/ body comes from [data-testid=description].
_DESC_JUNK = re.compile(r"^(this is a list|view |browse |don't forget|nearby apartments"
                        r"|skip to|previous items|next items)", re.I)


def _clean_desc(s):
    if not s:
        return None
    s = s.strip()
    if len(s) < 60 or _DESC_JUNK.search(s) or "list of all of the rental" in s.lower():
        return None
    return s


def _chromerpc_ready() -> bool:
    try:
        import fetch_cl_contacts as cr
        return "_err" not in cr._call("cdp.runtime.RuntimeService/Evaluate",
                                      {"expression": "1", "return_by_value": True})
    except Exception:
        return False


def _age_to_iso(text: str) -> str | None:
    if not text:
        return None
    if re.search(r"\b(today|just listed|just posted|hours? ago|minutes? ago)\b", text, re.I):
        m = _AGE_RE.search(text)
        if not m:
            return db.now()
    m = _AGE_RE.search(text)
    if not m:
        return None
    hrs = {"hour": 1, "day": 24, "week": 168, "month": 720}[m.group(2).lower()] * int(m.group(1))
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hrs)).isoformat()


def _min_price(price_str: str):
    nums = [int(n.replace(",", "")) for n in re.findall(r"\$([\d,]+)", str(price_str or ""))]
    return min(nums) if nums else None


def _search_url(region: dict, cap: int, page: int) -> str:
    sqs = {
        "pagination": {"currentPage": page} if page > 1 else {},
        "usersSearchTerm": region["term"],
        "mapBounds": region["bounds"], "isMapVisible": True,
        "filterState": {
            "fr": {"value": True}, "fsba": {"value": False}, "fsbo": {"value": False},
            "nc": {"value": False}, "cmsn": {"value": False}, "auc": {"value": False},
            "fore": {"value": False}, "mp": {"max": cap}, "price": {"max": cap},
            "sort": {"value": "days"}},
        "isListVisible": True}
    return (f"https://www.zillow.com/{region['url']}/rentals/?searchQueryState="
            + quote(json.dumps(sqs)))


def collect_cards(cr, region: dict, cap: int, max_pages: int = 5) -> list[dict]:
    seen, out = set(), []
    for pg in range(1, max_pages + 1):
        cr.navigate(_search_url(region, cap, pg))
        time.sleep(6)
        res = cr.ev(JS_RESULTS)
        if not isinstance(res, dict):
            print(f"  ! page {pg}: unexpected result ({str(res)[:80]})", file=sys.stderr)
            break
        if res.get("err"):
            print(f"  ! page {pg}: '{res['err']}' (blocked? need headful chromerpc)", file=sys.stderr)
            break
        items = res.get("items") or []
        new = 0
        for x in items:
            k = x.get("url") or x.get("zpid")
            if k and k not in seen:
                seen.add(k); out.append(x); new += 1
        if new == 0 and pg > 1:
            break
    return out


def detail(cr, url: str) -> dict:
    cr.navigate(url)
    time.sleep(4)
    d = cr.ev(JS_DETAIL)
    return d if isinstance(d, dict) else {}


def pull_region(conn, cfg, sess, cr, region_key: str, max_detail: int) -> int:
    reg = REGIONS[region_key]
    cap = cfg["max_price"]
    print(f"[zillow-cr] {reg['label']}: searching <= ${cap}, sort=Newest ...")
    cards = collect_cards(cr, reg, cap)
    # enforce cap (building cards leak units above the cap); keep priced cards only
    cards = [c for c in cards if (_min_price(c.get("price")) or 10**9) <= cap]
    print(f"  {len(cards)} card(s) <= ${cap}")

    new = 0
    for c in cards:
        url = c.get("url") or ""
        if url.startswith("/"):
            url = "https://www.zillow.com" + url
        if not url:
            continue
        m = re.search(r"/(\d+)_zpid", url) or re.search(r"homedetails/[^/]+/(\d+)", url)
        pid = "zlw" + (m.group(1) if m else __import__("hashlib").md5(url.encode()).hexdigest()[:12])
        if db.listing_exists(conn, pid) or db.is_blocked(conn, pid):
            continue
        if new >= max_detail:
            print(f"  reached --max-detail ({max_detail}); remaining new cards deferred to next run")
            break
        d = detail(cr, url)
        price = _min_price(c.get("price"))
        raw = {k: v for k, v in {
            "card_price": c.get("price"), "beds": c.get("beds"), "baths": c.get("baths"),
            "sqft": c.get("sqft"), "address": c.get("addr"), "hometype": c.get("hometype"),
            "recency": c.get("status"),
        }.items() if v not in (None, "", 0)}
        if not db.insert_stub(conn, post_id=pid, url=url,
                              title=f"Zillow {pid} — vet for details", price=price,
                              room_type="unknown",
                              area=cfg.get("unspecified_area_name", "(unspecified SF)"),
                              neighborhood=None, posted_at=None):
            continue
        imgs = d.get("imgs") or []
        image_dir, image_count = fetch_detail.download_images(sess, pid, imgs)
        fields = {
            "source": "zillow", "lat": c.get("lat"), "lng": c.get("lng"),
            "address": c.get("addr"),
            "description": _clean_desc(d.get("desc")),
            "image_urls": json.dumps(imgs), "image_count": image_count,
            "source_extra": json.dumps({"raw": raw}) if raw else None,
        }
        posted = _age_to_iso(c.get("status"))
        if posted:
            fields["posted_at"] = posted
        if d.get("phone"):
            fields["phone"] = d["phone"]
        db.update_detail(conn, pid, fields)
        conn.commit()
        new += 1

    db.set_meta(conn, reg["meta_key"], db.now())
    conn.commit()
    print(f"  {new} new Zillow listing(s) <= ${cap} in {reg['label']} (status='new', ready to vet).")
    return new


def main() -> None:
    ap = argparse.ArgumentParser(description="Pull Zillow rentals via headful chromerpc.")
    ap.add_argument("--region", default="both", choices=["sf", "berkeley", "both"])
    ap.add_argument("--max-detail", type=int, default=50,
                    help="max NEW detail pages to fetch per region per run (default 50)")
    args = ap.parse_args()
    regions = ["sf", "berkeley"] if args.region == "both" else [args.region]

    if not _chromerpc_ready():
        print("[zillow-cr] chromerpc not reachable on :50051 — skipping Zillow "
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
    print(f"\n{total} new Zillow listing(s) added across {', '.join(regions)}.")


if __name__ == "__main__":
    main()
