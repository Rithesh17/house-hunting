"""Check whether existing listing pages are still live; mark taken-down ones
'removed' so the dashboard can drop them.

    py scripts/check_links.py            # check active (non-rejected/removed) listings
    py scripts/check_links.py --all      # also re-check removed ones

A Craigslist post is considered DEAD when the page 404s or says it was
deleted / expired / flagged for removal.
"""
from __future__ import annotations

import argparse
import sys

import requests

import common
import db

DEAD_MARKERS = (
    "this posting has been deleted",
    "this posting has expired",
    "this posting has been flagged for removal",
    "page not found",
)


def is_dead(resp) -> bool:
    if resp.status_code == 404:
        return True
    low = resp.text.lower()
    return any(m in low for m in DEAD_MARKERS)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="also re-check removed listings")
    args = ap.parse_args()

    cfg = common.load_config()
    conn = db.connect()
    statuses = "('rejected')" if args.all else "('rejected', 'removed')"
    rows = conn.execute(
        f"SELECT id, url, status FROM listings WHERE status NOT IN {statuses}"
    ).fetchall()
    sess = common.session(cfg)
    dead = alive = 0
    print(f"checking {len(rows)} listing links...")
    for i, r in enumerate(rows):
        try:
            resp = sess.get(r["url"], timeout=30, allow_redirects=True)
        except requests.exceptions.RequestException as e:
            print(f"  ? {r['id']} error ({e}); leaving as-is", file=sys.stderr)
            continue
        if is_dead(resp):
            db.set_status(conn, r["id"], "removed")
            conn.commit()
            dead += 1
            print(f"  ✗ {r['id']} taken down -> removed")
        else:
            alive += 1
        if i < len(rows) - 1:
            common.polite_sleep(cfg)
    db.set_meta(conn, "last_link_check", db.now())
    conn.commit()
    conn.close()
    print(f"done: {alive} live, {dead} removed")


if __name__ == "__main__":
    main()
