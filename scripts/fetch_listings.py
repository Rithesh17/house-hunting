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
import sys

import requests
from bs4 import BeautifulSoup

import common
import db
import filters


def build_url(cfg: dict, offset: int) -> str:
    """One broad pass: price-capped, office/parking excluded server-side. No
    bedroom filter (it wrongly excludes sublets with unset attributes)."""
    params = {
        "max_price": cfg["max_price"],
        "s": offset,
        "availabilityMode": 0,
        "sale_date": "all+dates",
    }
    if cfg.get("excats"):
        params["excats"] = cfg["excats"]
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{common.CL_SEARCH}?{query}"


GENERIC_SF = {"", "san francisco", "city of san francisco", "sf"}


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


def run_pass(conn, cfg: dict, area_filter: str | None = None) -> list[dict]:
    """One broad citywide pass (all bed types). Keeps all of SF; only drops
    non-rental categories and clearly out-of-SF cities."""
    sess = common.session(cfg)
    page_size = cfg["scrape"].get("page_size", 120)
    max_pages = cfg["scrape"].get("max_pages_per_pass", 10)
    blocked = set(cfg.get("blocked_categories", common.DEFAULT_BLOCKED_CATEGORIES))
    new_rows: list[dict] = []
    seen_urls: set[str] = set()
    scanned = dropped = dropped_cat = 0

    for page in range(max_pages):
        url = build_url(cfg, page * page_size)
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
            area = assign_area(r["hood"], cfg)
            if area is None or (area_filter and area != area_filter):
                dropped += 1
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
          f"{dropped} out-of-SF, kept {len(new_rows)} new")
    return new_rows


def main() -> None:
    cfg = common.load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--area", help="limit to one configured area by name")
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

    prior = db.get_meta(conn, "last_pull")
    if prior:
        print(f"(last pull: {prior} — incremental: only new posts are added)")
    print("[broad pass] all of SF, price-capped, office/parking excluded:")
    total_new = run_pass(conn, cfg, area_filter)
    db.set_meta(conn, "last_pull", db.now())
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
