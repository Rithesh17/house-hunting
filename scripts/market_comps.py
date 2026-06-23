"""Cache of external market-rent ranges per (area_group, room_type).

Our own listings are capped at the budget, so they can't tell us the true market
rate for an area. This is a thin CACHE: the orchestrator does ONE web lookup per
bucket (e.g. "Inner Richmond 1BR average rent") and stores the range here with
`set`; scripts/research.py reads it to attach a price-plausibility context to each
listing. No web calls happen inside this script.

Areas are coarsely grouped so we do few lookups.

    py scripts/market_comps.py group "Inner Richmond"          # show the bucket
    py scripts/market_comps.py set richmond 1br 2600 3000 3600 web:2026-06
    py scripts/market_comps.py get richmond 1br
    py scripts/market_comps.py list
    py scripts/market_comps.py stale '[["richmond","1br"],["sunset","studio"]]'
"""
from __future__ import annotations

import argparse
import json
import sys

import db

MAX_AGE_DAYS = 60  # refresh a bucket if older than this

# Coarse area groups (keyword -> group). First match wins; default 'other'.
_GROUPS = [
    ("richmond", ["richmond", "seacliff", "laurel", "presidio"]),
    ("sunset", ["sunset", "parkside", "west portal", "forest hill"]),
    ("north", ["marina", "cow hollow", "pacific heights", "pac heights",
               "russian hill", "north beach", "telegraph", "nob hill", "tendernob"]),
    ("central", ["noe", "castro", "cole valley", "ashbury", "haight", "hayes",
                 "mission", "bernal", "glen park", "duboce", "western addition",
                 "nopa", "alamo"]),
    ("east", ["soma", "south of market", "potrero", "dogpatch", "mission bay"]),
    ("downtown", ["tenderloin", "civic center", "downtown", "union square",
                  "financial", "fidi", "chinatown", "mid-market", "mid market"]),
    ("south", ["bayview", "hunters point", "excelsior", "ingleside", "outer mission",
               "visitacion", "oceanview", "crocker", "portola", "sfsu", "ccsf",
               "lakeshore", "merced"]),
]


def area_group(area: str | None) -> str:
    a = (area or "").lower()
    for group, kws in _GROUPS:
        if any(k in a for k in kws):
            return group
    return "other"


def get(conn, group: str, room_type: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM market_comps WHERE area_group=? AND room_type=?",
        (group, room_type)).fetchone()
    return {k: r[k] for k in r.keys()} if r else None


def set_range(conn, group: str, room_type: str, low, median, high,
              source: str = "web") -> None:
    conn.execute(
        """INSERT INTO market_comps(area_group, room_type, low, median, high, source, fetched_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(area_group, room_type) DO UPDATE SET
             low=excluded.low, median=excluded.median, high=excluded.high,
             source=excluded.source, fetched_at=excluded.fetched_at""",
        (group, room_type, low, median, high, source, db.now()))
    conn.commit()


def _too_old(fetched_at: str | None) -> bool:
    if not fetched_at:
        return True
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(fetched_at)
        age = (datetime.now(timezone.utc) - ts).days
        return age > MAX_AGE_DAYS
    except ValueError:
        return True


def stale_buckets(conn, buckets: list[tuple[str, str]]) -> list[list[str]]:
    """Of the requested (group, room_type) buckets, which are missing or stale."""
    out = []
    for group, rt in buckets:
        row = get(conn, group, rt)
        if not row or _too_old(row.get("fetched_at")):
            out.append([group, rt])
    return out


def range_for(conn, area: str | None, room_type: str | None) -> dict | None:
    """The cached market range for a listing's area + room_type (None if missing)."""
    if not room_type:
        return None
    return get(conn, area_group(area), room_type)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("group").add_argument("area")
    g = sub.add_parser("get"); g.add_argument("group"); g.add_argument("room_type")
    s = sub.add_parser("set")
    for a in ("group", "room_type", "low", "median", "high"):
        s.add_argument(a)
    s.add_argument("source", nargs="?", default="web")
    sub.add_parser("list")
    sub.add_parser("stale").add_argument("buckets_json")
    args = ap.parse_args()

    conn = db.connect()
    if args.cmd == "group":
        print(area_group(args.area))
    elif args.cmd == "get":
        print(json.dumps(get(conn, args.group, args.room_type), indent=2))
    elif args.cmd == "set":
        set_range(conn, args.group, args.room_type, int(args.low), int(args.median),
                  int(args.high), args.source)
        print(f"set {args.group}/{args.room_type} = {args.low}-{args.median}-{args.high}")
    elif args.cmd == "list":
        for r in conn.execute("SELECT * FROM market_comps ORDER BY area_group, room_type"):
            print(dict(r))
    elif args.cmd == "stale":
        print(json.dumps(stale_buckets(conn, json.loads(args.buckets_json))))
    conn.close()


if __name__ == "__main__":
    main()
