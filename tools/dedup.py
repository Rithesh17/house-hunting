"""Cluster duplicate reposts of the same unit and tag each with a dup_group
(the cluster's PRIMARY = best listing by fit, then trust, then most photos).

Duplicates are detected by:
  - sharing one or more IDENTICAL craigslist photos (same image id in the
    remote image URLs — works without any local files), and/or
  - same normalized street address + price + room_type.

Only non-rejected listings are clustered. Run after vetting:
    py tools/dedup.py
"""
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import db  # noqa: E402

IMG_ID_RE = re.compile(r"images\.craigslist\.org/([\w]+)_\d+x\d+")


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


def addr_key(r):
    if not r["address"]:
        return None
    a = re.sub(r"\s+", " ", r["address"].lower()).strip()
    a = re.sub(r",?\s*(san francisco|ca|usa|\d{5}).*$", "", a).strip(" ,")
    # Drop cross-street / unit qualifiers so the SAME unit matches across sources
    # ("2111 grove st near cole" == "2111 grove st"; "... apt 5" / "... #310" == "...").
    a = re.sub(r"\s+(near|at|@|by|off)\s+.+$", "", a).strip(" ,")
    a = re.sub(r"[#,]?\s*\b(apt|apartment|unit|suite|ste|rm|room)\b\.?\s*\w*$", "", a).strip(" ,#")
    a = re.sub(r"\s+#\s*\w+$", "", a).strip(" ,#")
    a = re.sub(r"\s+", " ", a).strip()
    return f"{a}|{r['price']}|{r['room_type']}" if a else None


def coord_key(r):
    """Same location (~100m) + price + room type — catches the same unit across
    sources (e.g. a broker listing on both Craigslist and Zumper) even when one
    has no parsed street address."""
    if r["lat"] is None or r["lng"] is None or not r["price"]:
        return None
    return f"{round(r['lat'], 3)},{round(r['lng'], 3)}|{r['price']}|{r['room_type']}"


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
    for group in list(by_hash.values()) + list(by_addr.values()) + list(by_coord.values()):
        for other in group[1:]:
            uf.union(group[0], other)

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
