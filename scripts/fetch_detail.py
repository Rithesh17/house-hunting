"""Fetch + parse one Craigslist post, download its photos locally.

Stores description, attributes (beds/baths/sqft), price, map coordinates,
neighborhood, and contact into the DB, and downloads images to
data/images/<id>/ so Claude Code can open them with the Read tool for vetting.

Usage:
    py scripts/fetch_detail.py <post_id> [<post_id> ...]
    py scripts/fetch_detail.py --all-new        # every status='new' listing
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import requests
from bs4 import BeautifulSoup

import common
import db
import filters

IMG_RE = re.compile(r"https://images\.craigslist\.org/([\w]+)_\d+x\d+\.jpg")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def parse_post(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    data: dict = {}

    # Title + neighborhood
    title_el = soup.select_one("#titletextonly")
    if title_el:
        data["title"] = title_el.get_text(strip=True)
    hood_el = soup.select_one(".postingtitletext small")
    if hood_el:
        data["neighborhood"] = hood_el.get_text(strip=True).strip("() ")

    # Price (take the dollar amount before any cents, ignore commas)
    price_el = soup.select_one(".price")
    if price_el:
        m = re.search(r"\$\s*([\d,]+)", price_el.get_text())
        if m:
            data["price"] = int(m.group(1).replace(",", ""))

    # Description body (strip the QR/print preamble)
    body = soup.select_one("#postingbody")
    if body:
        for junk in body.select(".print-information, .print-qrcode-container"):
            junk.decompose()
        data["description"] = body.get_text("\n", strip=True)

    # Attributes: beds / baths / sqft / housing type
    attr_text = " ".join(s.get_text(" ", strip=True)
                         for s in soup.select(".attrgroup")).lower()
    bed = re.search(r"(\d+(?:\.\d+)?)\s*br", attr_text)
    bath = re.search(r"(\d+(?:\.\d+)?)\s*ba", attr_text)
    sqft = re.search(r"(\d{2,5})\s*ft2", attr_text)
    if bed:
        data["bedrooms"] = float(bed.group(1))
    elif "studio" in attr_text:
        data["bedrooms"] = 0.0
    if bath:
        data["bathrooms"] = float(bath.group(1))
    if sqft:
        data["sqft"] = int(sqft.group(1))
    for ht in ("apartment", "house", "condo", "cottage/cabin", "in-law",
               "duplex", "flat", "loft", "townhouse"):
        if ht in attr_text:
            data["housing_type"] = ht
            break

    # Map coordinates (from the post's map widget)
    map_el = soup.select_one("#map")
    if map_el:
        if map_el.get("data-latitude"):
            data["lat"] = float(map_el["data-latitude"])
        if map_el.get("data-longitude"):
            data["lng"] = float(map_el["data-longitude"])

    # Street address, when the poster included one (e.g. ".mapaddress")
    addr_el = soup.select_one(".mapaddress")
    if addr_el:
        addr = re.sub(r"\(.*?google map.*?\)", "", addr_el.get_text(" ", strip=True),
                      flags=re.I).strip(" ()")
        if addr:
            data["address"] = addr

    # Contact from the post body: phone and/or email (posters who include one).
    # The Craigslist relay email lives behind a JS token; the headless reply
    # fetcher (fetch_contacts) captures that separately for the shortlist.
    blob = " ".join([data.get("title", ""), data.get("description", "")])
    phone = PHONE_RE.search(blob)
    email = EMAIL_RE.search(blob)
    if phone:
        data["phone"] = phone.group(0)
    bits = []
    if phone:
        bits.append("☎ " + phone.group(0))
    if email:
        bits.append("✉ " + email.group(0))
    if bits:
        data["contact"] = "  ".join(bits)

    # Image URLs (dedup by image id, request large size for good vision)
    ids: list[str] = []
    for m in IMG_RE.finditer(html):
        if m.group(1) not in ids:
            ids.append(m.group(1))
    # 1200x900 for local vetting download; 600x450 remote URLs stored for the
    # dashboard to embed directly (so we don't keep image copies on disk).
    data["_image_urls"] = [f"https://images.craigslist.org/{i}_1200x900.jpg" for i in ids]
    data["image_urls"] = json.dumps(
        [f"https://images.craigslist.org/{i}_600x450.jpg" for i in ids])
    return data


def download_images(sess: requests.Session, post_id: str,
                    urls: list[str]) -> tuple[str, int]:
    out_dir = os.path.join(common.IMAGES_DIR, post_id)
    os.makedirs(out_dir, exist_ok=True)
    count = 0
    for idx, url in enumerate(urls, 1):
        dest = os.path.join(out_dir, f"{idx:02d}.jpg")
        if os.path.exists(dest):
            count += 1
            continue
        try:
            r = sess.get(url, timeout=30)
            if r.status_code == 200 and r.content:
                with open(dest, "wb") as f:
                    f.write(r.content)
                count += 1
        except requests.exceptions.RequestException as e:
            print(f"  ! image download failed {url}: {e}", file=sys.stderr)
    return out_dir, count


# Rough San Francisco bounding box — used to reject a bad geocode (e.g. a
# city-centroid fallback) before it overwrites a listing's coords.
_SF_BOUNDS = (37.70, 37.84, -122.52, -122.35)  # (lat_min, lat_max, lng_min, lng_max)


def _in_sf(lat, lng) -> bool:
    return (_SF_BOUNDS[0] <= lat <= _SF_BOUNDS[1]
            and _SF_BOUNDS[2] <= lng <= _SF_BOUNDS[3])


def geocode(sess: requests.Session, address: str):
    """Geocode a street address via OpenStreetMap Nominatim. Returns
    (lat, lng, place_name) where place_name is the best neighbourhood/suburb
    label Nominatim has for that address (feeds the area model), or None."""
    try:
        r = sess.get("https://nominatim.openstreetmap.org/search",
                     params={"q": address, "format": "json", "limit": 1,
                             "countrycodes": "us", "addressdetails": 1},
                     headers={"User-Agent": "sf-house-hunt/1.0 (personal project)"},
                     timeout=20)
        j = r.json()
        if j:
            a = j[0].get("address", {}) or {}
            place = (a.get("neighbourhood") or a.get("suburb")
                     or a.get("quarter") or a.get("city_district"))
            return float(j[0]["lat"]), float(j[0]["lon"]), place
    except (requests.exceptions.RequestException, ValueError, KeyError) as e:
        print(f"  ! geocode failed: {e}", file=sys.stderr)
    return None


def fetch_one(conn, cfg: dict, post_id: str) -> None:
    row = db.get(conn, post_id)
    if not row:
        print(f"  ! {post_id} not in DB (run fetch_listings first)", file=sys.stderr)
        return

    # Objective category gate: Craigslist's own "rooms / shared" category (roo)
    # is the poster's explicit declaration that this is a room, not a unit. Skip
    # before downloading anything. (apa/sub still go to full subagent vetting,
    # which catches rooms masquerading as in-law "apartments".)
    if common.category_from_url(row["url"]) == "roo":
        db.auto_reject(conn, post_id, "Craigslist rooms/shared category")
        conn.commit()
        print(f"  ⨯ {post_id} skipped: Craigslist rooms/shared category")
        return

    sess = common.session(cfg)
    try:
        resp = sess.get(row["url"], timeout=30)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"  ! fetch failed {post_id}: {e}", file=sys.stderr)
        return

    data = parse_post(resp.text)
    image_urls = data.pop("_image_urls", [])
    image_dir, image_count = download_images(sess, post_id, image_urls)

    # Derive room type from the parsed bedroom count.
    b = data.get("bedrooms")
    if b is not None:
        data["room_type"] = "studio" if b == 0 else "1br" if b == 1 else "2br_plus"

    # Determine the area from the ACTUAL location: when the post gives a street
    # address, geocode it for a PRECISE location + neighbourhood name (more
    # reliable than Craigslist's loose map pin, which drives area-model
    # mistakes). Capture the neighbourhood (when the post didn't supply one) for
    # the unsafe-name match, and prefer the geocoded coords as long as they land
    # inside SF (guards against a bad city-centroid fallback overwriting a pin).
    geocoded = False
    if data.get("address"):
        g = geocode(sess, data["address"])
        if g:
            glat, glng, place = g
            if place and not data.get("neighborhood"):
                data["neighborhood"] = place
            if _in_sf(glat, glng):
                data["lat"], data["lng"] = glat, glng
                geocoded = True
            common.polite_sleep(cfg)

    fields = {k: v for k, v in data.items() if v is not None}
    fields["image_dir"] = image_dir
    fields["image_count"] = image_count
    db.update_detail(conn, post_id, fields)
    conn.commit()

    # Only the two OBJECTIVE gates here (no-photos, outside-SF coords). Rooms,
    # scams, fit, etc. are judged by the subagent vetting pass, not by scripts.
    reason = filters.objective_reject_reason(
        image_count=image_count, lat=data.get("lat"), lng=data.get("lng"))
    if reason:
        db.auto_reject(conn, post_id, reason)
        conn.commit()
        print(f"  ⨯ {post_id} AUTO-REJECTED: {reason}")
        return

    # Print a summary Claude can read directly for vetting.
    print(f"\n=== {post_id} =====================================================")
    print(f"Title:        {data.get('title', row['title'])}")
    print(f"Price:        ${data.get('price', row['price'])}")
    print(f"Type:         {data.get('housing_type', '?')} | "
          f"{data.get('bedrooms', '?')}BR / {data.get('bathrooms', '?')}BA | "
          f"{data.get('sqft', '?')} sqft")
    print(f"Neighborhood: {data.get('neighborhood', row['neighborhood'])} "
          f"(area: {row['area']})")
    print(f"Address:      {data.get('address', '(not given)')}")
    print(f"Coords:       {data.get('lat', '?')}, {data.get('lng', '?')}"
          f"{' (geocoded)' if geocoded else ''}")
    print(f"Contact:      {data.get('contact', 'via Craigslist reply (relay email)')}")
    print(f"URL:          {row['url']}")
    print(f"Images:       {image_count} downloaded -> {image_dir}")
    print(f"\nDescription:\n{data.get('description', '(none)')[:2000]}")
    print("\n>> Claude: Read each image in the folder above, judge legitimacy + "
          "fit, then run save_verdict.py.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", help="post id(s)")
    ap.add_argument("--all-new", action="store_true",
                    help="fetch details for every status='new' listing")
    args = ap.parse_args()

    cfg = common.load_config()
    conn = db.connect()

    ids = list(args.ids)
    if args.all_new:
        rows = conn.execute(
            "SELECT id FROM listings WHERE status='new' AND detail_fetched_at IS NULL"
        ).fetchall()
        ids.extend(r["id"] for r in rows)
    if not ids:
        print("No ids given. Use post ids or --all-new.", file=sys.stderr)
        sys.exit(1)

    for i, pid in enumerate(ids):
        fetch_one(conn, cfg, pid)
        if i < len(ids) - 1:
            common.polite_sleep(cfg)
    conn.close()


if __name__ == "__main__":
    main()
