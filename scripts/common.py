"""Shared helpers: config loading, HTTP session, paths."""
from __future__ import annotations

import os
import re
import sys
import time

import requests
import yaml

# Windows consoles default to cp1252 and crash on emoji/✓ in print(). Force UTF-8.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
DATA_DIR = os.path.join(ROOT, "data")
IMAGES_DIR = os.path.join(DATA_DIR, "images")

CL_BASE = "https://sfbay.craigslist.org"
# sfc = San Francisco city, hhh = the full HOUSING supercategory (more listings
# than apa alone: also includes sublets, rooms, office, parking, for-sale...).
# We keep only real apartment/housing rentals via the category allow-list below.
CL_SEARCH = CL_BASE + "/search/sfc/hhh"
# East Bay subarea — used for the Berkeley BART-commute search (filtered to safe
# near-BART Berkeley at discovery + by the area model). sfc = SF city, eby = East Bay.
CL_SEARCH_EBY = CL_BASE + "/search/eby/hhh"

# Craigslist post URLs encode the subcategory: .../sfc/<cat>/d/<slug>/<id>.html
# We pull broadly and let subagents filter rooms/scams. We only BLOCK non-rental
# categories by post-URL code: off (office), prk/park (parking), rea/reb (real
# estate for sale), vac (vacation), swp (housing swap), fee. We KEEP apa, sub,
# AND roo (rooms) — subagents decide on rooms, not scripts.
DEFAULT_BLOCKED_CATEGORIES = {"off", "rea", "reb", "vac", "swp", "prk", "park", "fee"}


def category_from_url(url: str) -> str | None:
    """Return the craigslist subcategory code from a posting URL, or None."""
    m = re.search(r"/([a-z]{3})/d/", url)
    if m:
        return m.group(1)
    m = re.search(r"/([a-z]{3})/\d+\.html", url)
    return m.group(1) if m else None


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_session: requests.Session | None = None


def session(cfg: dict | None = None) -> requests.Session:
    global _session
    if _session is None:
        cfg = cfg or load_config()
        ua = cfg.get("scrape", {}).get("user_agent", "Mozilla/5.0")
        _session = requests.Session()
        _session.headers.update({"User-Agent": ua,
                                 "Accept-Language": "en-US,en;q=0.9"})
    return _session


def polite_sleep(cfg: dict) -> None:
    time.sleep(float(cfg.get("scrape", {}).get("delay_seconds", 2.0)))


def post_id_from_url(url: str) -> str | None:
    """Extract the craigslist post id from a posting URL. Handles BOTH formats:
    - legacy:  https://sfbay.craigslist.org/sfc/apa/d/<slug>/<digits>.html
    - new static-search:  https://www.craigslist.org/view/d/<slug>/<alphanumeric-id>
      (CL changed search-result URLs to this in 2026 — no category code, no .html,
      and an alphanumeric id; the old parser returned None, silently dropping every
      CL listing.)"""
    tail = url.rstrip("/").split("/")[-1]
    if tail.endswith(".html"):
        tail = tail[:-5]
    if tail.isdigit():
        return tail
    if "/view/d/" in url and re.fullmatch(r"[A-Za-z0-9]{8,}", tail):
        return tail
    return None
