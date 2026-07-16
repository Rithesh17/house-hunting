"""Discover Craigslist listings, filtered by price + room type + area.

Craigslist honors price + bedroom filters server-side but its nh= neighborhood
codes are unreliable, so we run one citywide pass per room type and bucket each
result into a configured target area by matching its location text (areas and
their match keywords live in config.yaml). New posts are inserted as
status='new'; out-of-area / named non-target hoods are dropped. Prints the new
ids+urls for Claude to fetch details on.

Usage:
    py scripts/fetch_listings.py            # all room types, all target areas
    py scripts/fetch_listings.py --room 1br
    py scripts/fetch_listings.py --area "Inner Richmond"   # keep only this area
"""
from __future__ import annotations

import argparse
import re
import sys

import requests
from bs4 import BeautifulSoup

import common
import db
import filters

# Craigslist's new /view/d/ search URLs dropped the category code, so the old
# objective `roo` (rooms/shared) gate can't fire from the URL anymore — rooms now
# flood discovery. This pre-filter drops the unambiguous ones by their TITLE using
# word-boundary phrases (so "1 bedroom", "sunroom", "bonus room" never match). The
# subagent shared-room gate remains the backstop for anything subtler.
_ROOM_TITLE_RE = re.compile(
    r"\b(room for rent|rooms for rent|private room|private bedroom|"
    r"room in (?:a|an|the|my|our|shared|beautiful|quiet)|shared room|"
    r"furnished room|room available|rooms available|single room|room rental|sro)\b",
    re.I)


def looks_like_room(title: str | None) -> bool:
    return bool(title and _ROOM_TITLE_RE.search(title))


def build_url(cfg: dict, offset: int, region: str = "sfc") -> str:
    """One broad pass: price-capped, office/parking excluded server-side. No
    bedroom filter (it wrongly excludes sublets with unset attributes). `region` is
    'sfc' (San Francisco, default) or 'eby' (East Bay — the Berkeley BART search)."""
    params = {
        "max_price": cfg["max_price"],
        "s": offset,
        "availabilityMode": 0,
        "sale_date": "all+dates",
    }
    if cfg.get("excats"):
        params["excats"] = cfg["excats"]
    query = "&".join(f"{k}={v}" for k, v in params.items())
    base = common.CL_SEARCH_EBY if region == "eby" else common.CL_SEARCH
    return f"{base}?{query}"


GENERIC_SF = {"", "san francisco", "city of san francisco", "sf"}

# East Bay / Berkeley discovery filter — deliberately BROAD (first-level only). KEEP
# anything that reads as Berkeley (incl. South Berkeley / Ashby — the area model
# marks the genuinely-unsafe pocket `avoid` later, like the Tenderloin in SF). DROP
# only clearly-OTHER East Bay cities. The geocoded coords are the real authority:
# geo.classify sorts safe-near-BART Berkeley (`ok`) from Oakland/unsafe (`avoid`).
_BERK_DROP = ("oakland", "rockridge", "temescal", "emeryville", "albany",
              "el cerrito", "kensington", "richmond", "san pablo", "alameda",
              "piedmont", "el sobrante", "hercules", "pinole", "san leandro",
              "hayward", "fremont", "castro valley", "walnut creek", "concord")
_BERK_KEEP = ("berkeley", "gourmet ghetto", "westbrae", "northbrae",
              "thousand oaks", "north shattuck", "elmwood", "claremont", "northside")


def assign_area_berkeley(location: str) -> str | None:
    """For the East Bay pass (BROAD first-level): keep a post if its location reads as
    Berkeley and is NOT a clearly-other East Bay city. Returns 'Berkeley' or None;
    the area model refines safe-near-BART vs avoid from the geocoded coords."""
    loc = (location or "").strip().lower()
    if any(d in loc for d in _BERK_DROP):
        return None
    if any(k in loc for k in _BERK_KEEP):
        return "Berkeley"
    return None  # not clearly Berkeley -> drop (coords would reject most anyway)


def assign_area(location: str, cfg: dict) -> str | None:
    """Map a Craigslist location string to an area label, keeping ALL of SF.

    Preferred target areas get their config name; any other SF neighborhood is
    kept under its own hood label; vague SF -> the unspecified bucket. Only
    clearly out-of-SF cities are dropped (None). Coordinates are the final
    authority later, in the objective gate.
    """
    loc = (location or "").strip().lower()
    for area in cfg["areas"]:
        for kw in area.get("match", []):
            if kw in loc:
                return area["name"]
    if "south san francisco" in loc:
        return None
    if any(c in loc for c in filters.OUT_OF_SF_CITIES):
        return None
    if not loc or loc in GENERIC_SF or "san francisco" in loc:
        return cfg.get("unspecified_area_name", "(unspecified SF)")
    # a named SF neighborhood not in the preferred list — keep it (all of SF)
    return location.strip().title()


def parse_results(html: str) -> list[dict]:
    """Parse one search-results page. Returns list of {url,title,price,hood}."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []

    # Modern craigslist server-rendered fallback nodes.
    nodes = soup.select("li.cl-static-search-result")
    if nodes:
        for li in nodes:
            a = li.find("a", href=True)
            if not a:
                continue
            title_el = li.select_one(".title")
            price_el = li.select_one(".price")
            loc_el = li.select_one(".location")
            out.append({
                "url": a["href"],
                "title": (title_el.get_text(strip=True) if title_el
                          else li.get("title", "")),
                "price": _parse_price(price_el.get_text() if price_el else ""),
                "hood": loc_el.get_text(strip=True) if loc_el else "",
            })
        return out

    # Older layout fallback.
    for row in soup.select("li.result-row, .cl-search-result"):
        a = row.select_one("a.result-title, a.posting-title, a[href]")
        if not a or not a.get("href"):
            continue
        price_el = row.select_one(".result-price, .price")
        hood_el = row.select_one(".result-hood, .location")
        out.append({
            "url": a["href"],
            "title": a.get_text(strip=True),
            "price": _parse_price(price_el.get_text() if price_el else ""),
            "hood": (hood_el.get_text(strip=True).strip("() ") if hood_el else ""),
        })
    return out


def _parse_price(text: str):
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def run_pass(conn, cfg: dict, area_filter: str | None = None,
             region: str = "sfc") -> list[dict]:
    """One broad pass (all bed types). region='sfc' keeps all of SF; region='eby'
    runs the East Bay search and keeps only safe near-BART Berkeley (Oakland etc.
    dropped). Only non-rental categories and out-of-area posts are dropped here."""
    sess = common.session(cfg)
    page_size = cfg["scrape"].get("page_size", 120)
    max_pages = cfg["scrape"].get("max_pages_per_pass", 10)
    blocked = set(cfg.get("blocked_categories", common.DEFAULT_BLOCKED_CATEGORIES))
    assign = assign_area_berkeley if region == "eby" else (
        lambda loc, _cfg=cfg: assign_area(loc, _cfg))
    new_rows: list[dict] = []
    seen_urls: set[str] = set()
    scanned = dropped = dropped_cat = dropped_room = 0

    for page in range(max_pages):
        url = build_url(cfg, page * page_size, region)
        try:
            resp = sess.get(url, timeout=30)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"  ! request failed (p{page}): {e}", file=sys.stderr)
            break

        results = parse_results(resp.text)
        fresh = [r for r in results if r["url"] not in seen_urls]
        if not fresh:
            break
        page_new_start = len(new_rows)
        for r in fresh:
            seen_urls.add(r["url"])
            scanned += 1
            cat = common.category_from_url(r["url"])
            if cat in blocked:
                dropped_cat += 1
                continue
            area = assign(r["hood"])
            if area is None or (area_filter and area != area_filter):
                dropped += 1
                continue
            if looks_like_room(r["title"]):
                dropped_room += 1
                continue
            pid = common.post_id_from_url(r["url"])
            if not pid:
                continue
            inserted = db.insert_stub(
                conn, post_id=pid, url=r["url"], title=r["title"],
                price=r["price"], room_type="unknown", area=area,
                neighborhood=r["hood"], posted_at=None,
            )
            if inserted:
                new_rows.append({"id": pid, "url": r["url"], "title": r["title"],
                                 "price": r["price"], "area": area})
        conn.commit()
        # Incremental: Craigslist sorts newest-first, so once a whole page is
        # already in the DB (0 new inserts), older pages are too — stop.
        if page > 0 and len(new_rows) == page_new_start:
            break
        if len(results) < page_size:
            break
        common.polite_sleep(cfg)

    print(f"  scanned {scanned}, dropped {dropped_cat} non-rental + "
          f"{dropped} out-of-area + {dropped_room} rooms (title), kept {len(new_rows)} new")
    return new_rows


def main() -> None:
    cfg = common.load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", help="limit to one configured area by name")
    ap.add_argument("--region", default="sfc", choices=["sfc", "eby"],
                    help="'sfc' = San Francisco (default); 'eby' = East Bay / safe "
                         "near-BART Berkeley (Oakland excluded)")
    args = ap.parse_args()
    conn = db.connect()

    area_filter = None
    if args.area:
        names = [a["name"] for a in cfg["areas"]]
        match = [n for n in names if n.lower() == args.area.lower()]
        if not match:
            print(f"No area named {args.area!r} in config. Options: {names}",
                  file=sys.stderr)
            sys.exit(1)
        area_filter = match[0]

    meta_key = "last_pull_eby" if args.region == "eby" else "last_pull"
    prior = db.get_meta(conn, meta_key)
    if prior:
        print(f"(last {args.region} pull: {prior} — incremental: only new posts added)")
    label = ("safe near-BART Berkeley (East Bay)" if args.region == "eby"
             else "all of SF")
    print(f"[broad pass] {label}, price-capped, office/parking excluded:")
    # Watermark = the instant BEFORE the pass begins (run start), NOT db.now()
    # after it finishes. The fetch runs at the very START of a refresh, so
    # stamping the start guarantees the NEXT run reads back to THIS run's
    # start-of-fetch → now with zero gap — a post made DURING this run's later
    # stages is still caught next time. Stamping after the pass would silently
    # skip that window.
    pull_started = db.now()
    total_new = run_pass(conn, cfg, area_filter, args.region)
    db.set_meta(conn, meta_key, pull_started)
    conn.commit()
    conn.close()

    print("\n" + "=" * 70)
    print(f"{len(total_new)} NEW listing(s) to fetch + vet:")
    total_new.sort(key=lambda r: (r["price"] or 99999))
    for r in total_new:
        price = f"${r['price']}" if r["price"] else "$?"
        print(f"  {r['id']}  {price:<7} {r['area']:<26} {r['url']}")
    if total_new:
        print("\nNext: run  py scripts/fetch_detail.py --all-new  then vet via subagents.")


if __name__ == "__main__":
    main()
