"""Cluster duplicate reposts of the same unit and tag each with a dup_group
(the cluster's PRIMARY = best listing by fit, then trust, then most photos).

Duplicates are detected by:
  - sharing one or more IDENTICAL craigslist photos (same image id in the
    remote image URLs — works without any local files), and/or
  - same normalized street address + price + room_type, and/or
  - same normalized address (or ~same coords) + room_type with a CLOSE price
    (<=PRICE_TOL apart) — catches the same unit RE-POSTED at a changed rent
    (a different post id + different title + re-uploaded photos), and/or
  - same captured contact phone + room_type — the landlord's repost of one unit.

This matters for outreach: a relist of a unit we already CONTACTED must fold into
the same cluster so we don't email it again. (send_email.py adds a unit-level guard
on top, so even a missed cluster won't double-contact.)

Only non-rejected listings are clustered. Run after vetting:
    py tools/dedup.py
"""
import json
import os
import re
import sys
from collections import defaultdict
from itertools import combinations

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import db  # noqa: E402

IMG_ID_RE = re.compile(r"images\.craigslist\.org/([\w]+)_\d+x\d+")
PRICE_TOL = 0.15  # a relist often nudges the rent; treat a <=15% gap as the same unit
PHONE_RE = re.compile(r"\d")


def ensure_column(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()]
    if "dup_group" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN dup_group TEXT")


def img_ids(image_urls_json):
    """Set of craigslist image ids parsed from the stored remote URLs."""
    if not image_urls_json:
        return set()
    try:
        urls = json.loads(image_urls_json)
    except (ValueError, TypeError):
        return set()
    return {m.group(1) for u in urls for m in [IMG_ID_RE.search(u)] if m}


def norm_addr(address):
    """Normalize a street address so the SAME unit matches across sources / reposts
    ("2111 grove st near cole" == "2111 grove st"; "... apt 5" / "... #310" == "...")."""
    if not address:
        return None
    a = re.sub(r"\s+", " ", address.lower()).strip()
    a = re.sub(r",?\s*(san francisco|ca|usa|\d{5}).*$", "", a).strip(" ,")
    a = re.sub(r"\s+(near|at|@|by|off)\s+.+$", "", a).strip(" ,")
    a = re.sub(r"[#,]?\s*\b(apt|apartment|unit|suite|ste|rm|room)\b\.?\s*\w*$", "", a).strip(" ,#")
    a = re.sub(r"\s+#\s*\w+$", "", a).strip(" ,#")
    a = re.sub(r"\s+", " ", a).strip()
    return a or None


def addr_key(r):
    a = norm_addr(r["address"])
    return f"{a}|{r['price']}|{r['room_type']}" if a else None


def coord_key(r):
    """Same location (~100m) + price + room type — catches the same unit across
    sources (e.g. a broker listing on both Craigslist and Zumper) even when one
    has no parsed street address."""
    if r["lat"] is None or r["lng"] is None or not r["price"]:
        return None
    return f"{round(r['lat'], 3)},{round(r['lng'], 3)}|{r['price']}|{r['room_type']}"


def addr_only_key(r):
    """Address + room_type, PRICE-AGNOSTIC. Members are union'd only if their prices
    are within PRICE_TOL — so a relist at a changed rent folds in, but two genuinely
    different units that merely share a building address (and differ a lot in price)
    do not."""
    a = norm_addr(r["address"])
    return f"{a}|{r['room_type']}" if a else None


def coord_only_key(r):
    """~Same coords + room_type, PRICE-AGNOSTIC (price checked before union). Catches
    a price-changed repost when the address is undisclosed."""
    if r["lat"] is None or r["lng"] is None:
        return None
    return f"{round(r['lat'], 3)},{round(r['lng'], 3)}|{r['room_type']}"


def phone_key(r):
    """Captured contact phone (digits only) + room_type — the same landlord's repost
    of one unit. Relay emails are per-post (unique), so they can't link reposts; a
    phone can."""
    digits = "".join(PHONE_RE.findall(r["phone"] or ""))
    return f"{digits}|{r['room_type']}" if len(digits) >= 10 else None


def _price_close(a, b, tol=PRICE_TOL):
    if not a or not b:
        return False
    return abs(a - b) <= tol * max(a, b)


class UF:
    def __init__(self): self.p = {}
    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb: self.p[ra] = rb


def main():
    conn = db.connect()
    ensure_column(conn)
    rows = conn.execute("SELECT * FROM listings WHERE status != 'rejected'").fetchall()

    uf = UF()
    by_hash = defaultdict(list)
    by_addr = defaultdict(list)
    by_coord = defaultdict(list)
    by_phone = defaultdict(list)
    by_addr_only = defaultdict(list)   # price-agnostic; union'd only if price-close
    by_coord_only = defaultdict(list)
    for r in rows:
        uf.find(r["id"])
        for h in img_ids(r["image_urls"]):
            by_hash[h].append(r["id"])
        ak = addr_key(r)
        if ak:
            by_addr[ak].append(r["id"])
        ck = coord_key(r)
        if ck:
            by_coord[ck].append(r["id"])
        pk = phone_key(r)
        if pk:
            by_phone[pk].append(r["id"])
        aok = addr_only_key(r)
        if aok:
            by_addr_only[aok].append(r)
        cok = coord_only_key(r)
        if cok:
            by_coord_only[cok].append(r)
    # Exact-match keys: union the whole group.
    for group in (list(by_hash.values()) + list(by_addr.values())
                  + list(by_coord.values()) + list(by_phone.values())):
        for other in group[1:]:
            uf.union(group[0], other)
    # Price-agnostic address/coord groups: union pairs whose prices are close (a
    # relist of the SAME unit at a slightly changed rent) — folds a price-changed
    # repost into the cluster of the post we may already have contacted.
    for group in list(by_addr_only.values()) + list(by_coord_only.values()):
        for a, b in combinations(group, 2):
            if _price_close(a["price"], b["price"]):
                uf.union(a["id"], b["id"])

    clusters = defaultdict(list)
    rowmap = {r["id"]: r for r in rows}
    for r in rows:
        clusters[uf.find(r["id"])].append(r["id"])

    # primary = best by (fit, legit, image_count)
    def score(rid):
        r = rowmap[rid]
        return (r["fit_score"] or -1, r["legit_score"] or -1, r["image_count"] or 0)

    n_clusters = n_dups = 0
    for members in clusters.values():
        primary = max(members, key=score)
        if len(members) > 1:
            n_clusters += 1
            n_dups += len(members) - 1
        for rid in members:
            conn.execute("UPDATE listings SET dup_group=? WHERE id=?", (primary, rid))
    # rejected listings: group = self
    conn.execute("UPDATE listings SET dup_group=id WHERE status='rejected' AND dup_group IS NULL")
    conn.commit()
    conn.close()
    print(f"clustered {len(rows)} candidates into {len(clusters)} groups "
          f"({n_clusters} multi-listing clusters, {n_dups} duplicates folded)")


if __name__ == "__main__":
    main()
