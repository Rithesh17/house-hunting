"""SQLite storage for the SF house-hunting assistant.

Schema + small CLI used by the other scripts and by Claude Code.

Usage:
    py scripts/db.py init                 # create the database
    py scripts/db.py list [--status new]  # list listings (compact)
    py scripts/db.py show <id>            # full row as JSON
    py scripts/db.py set-status <id> <s>  # new|vetted|interested|contacted|rejected
    py scripts/db.py reset-seen           # (debug) wipe everything
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "listings.db")
# Transient Stage-2 research bundles (one JSON per listing), like data/images/.
RESEARCH_DIR = os.path.join(DATA_DIR, "research")

STATUSES = ("new", "vetted", "interested", "contacted", "rejected", "removed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id            TEXT PRIMARY KEY,        -- craigslist post id
    source        TEXT NOT NULL DEFAULT 'craigslist',
    url           TEXT NOT NULL,
    title         TEXT,
    price         INTEGER,
    bedrooms      REAL,
    bathrooms     REAL,
    sqft          INTEGER,
    housing_type  TEXT,
    room_type     TEXT,                    -- 'studio' | '1br' | '2br_plus' | 'unknown'
    area          TEXT,                    -- area label (preferred name or SF hood)
    neighborhood  TEXT,                    -- as parsed from the post
    address       TEXT,                    -- street address if the post included one
    lat           REAL,
    lng           REAL,
    posted_at     TEXT,
    description   TEXT,
    image_dir     TEXT,
    image_urls    TEXT,                    -- JSON list of remote craigslist image URLs
    image_count   INTEGER DEFAULT 0,
    contact       TEXT,
    phone         TEXT,
    reply_email   TEXT,                    -- CL relay email revealed via the reply flow (chromerpc)
    contact_fetched_at TEXT,              -- when reply contact (email/phone) was fetched
    dre_number    TEXT,                    -- CA DRE license # parsed from the body (agents only)
    status        TEXT NOT NULL DEFAULT 'new',
    reject_reason TEXT,
    dup_group     TEXT,                    -- primary listing id of its duplicate cluster
    -- Claude's verdict --
    legit_score   INTEGER,
    legit_label   TEXT,                    -- 'likely-legit' | 'unverified-amateur' | 'likely-scam'
    red_flags     TEXT,                    -- JSON array
    low_polish    INTEGER DEFAULT 0,
    fit_score     INTEGER,
    is_1br1ba     INTEGER DEFAULT 0,
    verdict_summary TEXT,
    recommendation  TEXT,
    verification    TEXT,                  -- JSON: Stage-2 cross-check {dre,owner,price,duplicates}
    source_extra    TEXT,                  -- JSON: source-specific signals (Zillow: room flags, rentZestimate, parcel, listedBy, priceHistory)
    -- bookkeeping --
    first_seen_at TEXT,
    detail_fetched_at TEXT,
    vetted_at     TEXT,
    notified      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_fit ON listings(fit_score);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Cached external market-rent ranges per (area_group, room_type). Populated by
-- the orchestrator (one web lookup per bucket) since our own listings are capped
-- at the budget and can't establish true market rate. Read by scripts/research.py.
CREATE TABLE IF NOT EXISTS market_comps (
    area_group  TEXT,
    room_type   TEXT,
    low         INTEGER,
    median      INTEGER,
    high        INTEGER,
    source      TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (area_group, room_type)
);

-- Ids that were purged (scam/low-trust or unsafe-area) and must NOT be
-- re-discovered/re-vetted on later pulls. insert_stub + the fetchers skip these.
CREATE TABLE IF NOT EXISTS blocklist (
    id         TEXT PRIMARY KEY,
    source     TEXT,
    reason     TEXT,
    blocked_at TEXT
);
"""

# Columns added after the original schema shipped; ALTER them onto older DBs.
_ADDED_COLUMNS = {"dre_number": "TEXT", "verification": "TEXT", "source_extra": "TEXT",
                  "reply_email": "TEXT", "contact_fetched_at": "TEXT"}


def migrate(conn: sqlite3.Connection) -> None:
    """Idempotently bring an existing DB up to the current schema."""
    conn.executescript(SCHEMA)  # creates any missing tables (market_comps, etc.)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
    for name, decl in _ADDED_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE listings ADD COLUMN {name} {decl}")
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    fresh = not os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if not fresh:
        migrate(conn)  # bring older DBs up to the current schema
    return conn


def init() -> None:
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"Initialized database at {DB_PATH}")


def listing_exists(conn: sqlite3.Connection, post_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM listings WHERE id = ?", (post_id,))
    return cur.fetchone() is not None


def is_blocked(conn: sqlite3.Connection, post_id: str) -> bool:
    return conn.execute("SELECT 1 FROM blocklist WHERE id = ?", (post_id,)).fetchone() is not None


def block(conn: sqlite3.Connection, post_id: str, source: str, reason: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO blocklist(id, source, reason, blocked_at) VALUES(?,?,?,?)",
        (post_id, source, reason, now()))


def insert_stub(conn: sqlite3.Connection, *, post_id: str, url: str, title: str,
                price, room_type: str, area: str, neighborhood: str,
                posted_at: str | None) -> bool:
    """Insert a freshly-discovered listing. Returns True if newly inserted.
    Skips ids that were purged to the blocklist (so they aren't re-surfaced)."""
    if listing_exists(conn, post_id) or is_blocked(conn, post_id):
        return False
    conn.execute(
        """INSERT INTO listings
           (id, url, title, price, room_type, area, neighborhood, posted_at,
            status, first_seen_at)
           VALUES (?,?,?,?,?,?,?,?, 'new', ?)""",
        (post_id, url, title, price, room_type, area, neighborhood, posted_at, now()),
    )
    return True


def update_detail(conn: sqlite3.Connection, post_id: str, fields: dict) -> None:
    fields = dict(fields)
    fields["detail_fetched_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE listings SET {cols} WHERE id = ?",
                 (*fields.values(), post_id))


def save_verdict(conn: sqlite3.Connection, post_id: str, v: dict) -> None:
    ver = v.get("verification")
    conn.execute(
        """UPDATE listings SET
              legit_score = ?, legit_label = ?, red_flags = ?, low_polish = ?,
              fit_score = ?, is_1br1ba = ?, verdict_summary = ?, recommendation = ?,
              verification = ?,
              status = CASE WHEN status = 'new' THEN 'vetted' ELSE status END,
              vetted_at = ?
           WHERE id = ?""",
        (
            v.get("legit_score"),
            v.get("legit_label"),
            json.dumps(v.get("red_flags", [])),
            1 if v.get("low_polish") else 0,
            v.get("fit_score"),
            1 if v.get("is_1br1ba") else 0,
            v.get("verdict_summary"),
            v.get("recommendation"),
            json.dumps(ver) if ver is not None else None,
            now(),
            post_id,
        ),
    )


def set_status(conn: sqlite3.Connection, post_id: str, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    conn.execute("UPDATE listings SET status = ? WHERE id = ?", (status, post_id))


def auto_reject(conn: sqlite3.Connection, post_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE listings SET status = 'rejected', reject_reason = ? WHERE id = ?",
        (reason, post_id),
    )


def mark_notified(conn: sqlite3.Connection, post_id: str) -> None:
    conn.execute("UPDATE listings SET notified = 1 WHERE id = ?", (post_id,))


def get(conn: sqlite3.Connection, post_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM listings WHERE id = ?", (post_id,)).fetchone()


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("red_flags"):
        try:
            d["red_flags"] = json.loads(d["red_flags"])
        except (json.JSONDecodeError, TypeError):
            d["red_flags"] = []
    else:
        d["red_flags"] = []
    if d.get("source_extra"):
        try:
            d["source_extra"] = json.loads(d["source_extra"])
        except (json.JSONDecodeError, TypeError):
            d["source_extra"] = None
    return d


# --------------------------------------------------------------------------- CLI
def _cli() -> None:
    p = argparse.ArgumentParser(description="house-hunting database CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    lp = sub.add_parser("list")
    lp.add_argument("--status")
    sp = sub.add_parser("show")
    sp.add_argument("id")
    ss = sub.add_parser("set-status")
    ss.add_argument("id")
    ss.add_argument("status")
    sub.add_parser("reset-seen")
    args = p.parse_args()

    if args.cmd == "init":
        init()
        return

    conn = connect()
    if args.cmd == "list":
        q = "SELECT id, status, price, room_type, neighborhood, fit_score, legit_label, title FROM listings"
        params: tuple = ()
        if args.status:
            q += " WHERE status = ?"
            params = (args.status,)
        q += " ORDER BY (fit_score IS NULL), fit_score DESC, price ASC"
        rows = conn.execute(q, params).fetchall()
        for r in rows:
            print(f"{r['id']:>12} | {r['status']:<10} | ${r['price'] or '?':<6} | "
                  f"{(r['room_type'] or '?'):<6} | {(r['neighborhood'] or '?'):<24} | "
                  f"fit={r['fit_score'] if r['fit_score'] is not None else '-':<4} | "
                  f"{r['legit_label'] or '-':<18} | {(r['title'] or '')[:50]}")
        print(f"\n{len(rows)} listing(s).")
    elif args.cmd == "show":
        row = get(conn, args.id)
        if not row:
            print(f"No listing {args.id}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(row_to_dict(row), indent=2))
    elif args.cmd == "set-status":
        set_status(conn, args.id, args.status)
        conn.commit()
        print(f"{args.id} -> {args.status}")
    elif args.cmd == "reset-seen":
        conn.execute("DELETE FROM listings")
        conn.commit()
        print("All listings deleted.")
    conn.close()


if __name__ == "__main__":
    _cli()
