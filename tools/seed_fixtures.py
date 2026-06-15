"""Insert deterministic fixture listings so the dashboard validation is stable.

Creates two fixtures (one 1BR likely-legit, one studio unverified-amateur) with
coordinates, verdicts, and a tiny placeholder photo each. Idempotent.

    py tools/seed_fixtures.py
    py tools/seed_fixtures.py --clean   # remove fixtures
"""
from __future__ import annotations

import argparse
import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import common  # noqa: E402
import db  # noqa: E402

# 1x1 JPEG so the gallery/thumbnail renders during validation.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP//////////////////////////////"
    "////////////////////////////////////////////////2wBDAf//////////"
    "////////////////////////////////////////////////////////////////"
    "////////wAARCAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAAAP/EAB"
    "QQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAA"
    "AAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AvwA//9k="
)

FIXTURES = [
    {
        "id": "fixture_1br",
        "url": "https://example.com/fixture_1br",
        "title": "Sunny 1BR/1BA near Golden Gate Park",
        "price": 1875, "room_type": "1br", "area": "Inner Richmond",
        "neighborhood": "inner richmond", "bedrooms": 1.0, "bathrooms": 1.0,
        "sqft": 620, "housing_type": "apartment",
        "lat": 37.7820, "lng": -122.4646,
        "description": "Bright top-floor one bedroom, hardwood floors, eat-in "
                       "kitchen, laundry on site. Cat ok. Available now.",
        "contact": "415-555-0101",
        "verdict": {
            "legit_score": 88, "legit_label": "likely-legit", "red_flags": [],
            "low_polish": False, "fit_score": 86, "is_1br1ba": True,
            "verdict_summary": "Consistent photos, normal terms, prime area.",
            "recommendation": "Strong match — email to schedule a viewing.",
        },
    },
    {
        "id": "fixture_studio",
        "url": "https://example.com/fixture_studio",
        "title": "Spacious studio, Inner Sunset",
        "price": 1700, "room_type": "studio", "area": "Inner Sunset / UCSF",
        "neighborhood": "inner sunset", "bedrooms": 0.0, "bathrooms": 1.0,
        "sqft": 470, "housing_type": "in-law",
        "lat": 37.7630, "lng": -122.4660,
        "description": "Large in-law studio, full kitchen and bath. Few photos, "
                       "owner-managed.",
        "contact": "see reply button on page",
        "verdict": {
            "legit_score": 72, "legit_label": "unverified-amateur",
            "red_flags": [], "low_polish": True, "fit_score": 64,
            "is_1br1ba": False,
            "verdict_summary": "Plausible but sparse listing; verify in person.",
            "recommendation": "Worth a look; confirm size and legitimacy on tour.",
        },
    },
]


def seed() -> None:
    db.init()
    conn = db.connect()
    for fx in FIXTURES:
        img_dir = os.path.join(common.IMAGES_DIR, fx["id"])
        os.makedirs(img_dir, exist_ok=True)
        with open(os.path.join(img_dir, "01.jpg"), "wb") as f:
            f.write(TINY_JPEG)
        if not db.listing_exists(conn, fx["id"]):
            db.insert_stub(conn, post_id=fx["id"], url=fx["url"], title=fx["title"],
                           price=fx["price"], room_type=fx["room_type"],
                           area=fx["area"], neighborhood=fx["neighborhood"],
                           posted_at=None)
        db.update_detail(conn, fx["id"], {
            "bedrooms": fx["bedrooms"], "bathrooms": fx["bathrooms"],
            "sqft": fx["sqft"], "housing_type": fx["housing_type"],
            "lat": fx["lat"], "lng": fx["lng"], "description": fx["description"],
            "contact": fx["contact"], "image_dir": img_dir, "image_count": 1,
        })
        db.save_verdict(conn, fx["id"], fx["verdict"])
    conn.commit()
    conn.close()
    print(f"Seeded {len(FIXTURES)} fixtures.")


def clean() -> None:
    conn = db.connect()
    for fx in FIXTURES:
        conn.execute("DELETE FROM listings WHERE id = ?", (fx["id"],))
    conn.commit()
    conn.close()
    print("Removed fixtures.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()
    clean() if args.clean else seed()
