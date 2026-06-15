"""Backfill remote craigslist image URLs for non-rejected listings that don't
have them yet (re-fetches each post page once). Lets the dashboard embed remote
images so local copies can be purged.

    py tools/backfill_image_urls.py
"""
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import common  # noqa: E402
import db  # noqa: E402

IMG_RE = re.compile(r"https://images\.craigslist\.org/([\w]+)_\d+x\d+\.jpg")


def main():
    conn = db.connect()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()]
    if "image_urls" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN image_urls TEXT")
        conn.commit()

    rows = conn.execute(
        "SELECT id, url FROM listings WHERE status != 'rejected' "
        "AND (image_urls IS NULL OR image_urls = '')").fetchall()
    sess = common.session()
    print(f"backfilling {len(rows)} listings...")
    done = 0
    for r in rows:
        try:
            html = sess.get(r["url"], timeout=30).text
        except requests.exceptions.RequestException as e:
            print(f"  ! {r['id']} fetch failed: {e}")
            continue
        ids = []
        for m in IMG_RE.finditer(html):
            if m.group(1) not in ids:
                ids.append(m.group(1))
        urls = [f"https://images.craigslist.org/{i}_600x450.jpg" for i in ids]
        conn.execute("UPDATE listings SET image_urls=? WHERE id=?",
                     (json.dumps(urls), r["id"]))
        conn.commit()
        done += 1
        if done % 20 == 0:
            print(f"  ...{done}")
        time.sleep(1.0)
    conn.close()
    print(f"done: {done} listings backfilled with remote image URLs")


if __name__ == "__main__":
    main()
