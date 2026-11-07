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


REGIONS_DIR = os.path.join(ROOT, "regions")

# Keys a non-legacy region profile REPLACES in the loaded config. Everything else
# (scrape settings, excats, notify thresholds...) is shared across regions.
_REGION_OVERLAY_KEYS = ("max_price", "budget_target", "areas", "unsafe", "caution")


def active_region_slug(raw_cfg: dict | None = None) -> str:
    """The region this run targets: $HH_REGION, else config.yaml `active_region`,
    else 'sf' (the original behaviour)."""
    env = os.getenv("HH_REGION")
    if env:
        return env.strip()
    if raw_cfg is None:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw_cfg = yaml.safe_load(f) or {}
    return (raw_cfg.get("active_region") or "sf").strip()


def load_region(slug: str) -> dict:
    """Load regions/<slug>.yaml (see regions/README.md for the schema)."""
    path = os.path.join(REGIONS_DIR, f"{slug}.yaml")
    if not os.path.exists(path):
        raise SystemExit(f"No region profile {path} — write one (regions/README.md).")
    with open(path, "r", encoding="utf-8") as f:
        prof = yaml.safe_load(f) or {}
    prof.setdefault("slug", slug)
    return prof


def load_config() -> dict:
    """config.yaml + the active region profile at cfg['region']. A legacy profile
    (sf) changes nothing; any other profile overlays its budget/areas/area-model."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    region = load_region(active_region_slug(cfg))
    cfg["region"] = region
    if not region.get("legacy"):
        for k in _REGION_OVERLAY_KEYS:
            if k in region:
                cfg[k] = region[k]
        cfg["unspecified_area_name"] = region.get("default_area_name", region.get("label"))
    return cfg


_cfg_memo: dict | None = None


def cached_config() -> dict:
    """load_config() once per process (for hot per-listing helpers)."""
    global _cfg_memo
    if _cfg_memo is None:
        _cfg_memo = load_config()
    return _cfg_memo


def region_is_legacy(cfg: dict | None = None) -> bool:
    cfg = cfg or cached_config()
    return bool(cfg.get("region", {}).get("legacy", False))


def best_area(text: str | None, areas: list[dict]) -> dict | None:
    """The profile `areas` entry whose LONGEST matching keyword hits `text` (so
    "east palo alto" beats "palo alto"), or None."""
    t = (text or "").lower()
    best, best_len = None, 0
    for a in areas or []:
        for kw in a.get("match", []):
            if kw and kw in t and len(kw) > best_len:
                best, best_len = a, len(kw)
    return best


def in_region_bounds(lat, lng, cfg: dict | None = None) -> bool:
    """True when (lat, lng) is inside the active (non-legacy) region's bounds box."""
    b = (cfg or cached_config())["region"].get("bounds") or {}
    if lat is None or lng is None or not b:
        return False
    return b["lat"][0] <= lat <= b["lat"][1] and b["lng"][0] <= lng <= b["lng"][1]


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
