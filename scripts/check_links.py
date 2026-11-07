"""Check whether existing listing pages are still live; mark taken-down ones
'removed' so the dashboard can drop them.

    py scripts/check_links.py            # check active (non-rejected/removed) listings
    py scripts/check_links.py --all      # also re-check removed ones
    py scripts/check_links.py --max-age-days 45   # override the manual-source age-out

TWO CLASSES OF SOURCE, because only one of them can be checked by a plain GET:

* **Craigslist** answers honestly. A post is DEAD when the page 404s or says it
  was deleted / expired / flagged for removal. These are fetched and judged.

* **Zillow / Apartments.com / Zumper are bot-walled** and answer a scripted GET
  with 403 (or a challenge page). The old version of this script counted that as
  "alive", so a manually-gathered row could sit on the dashboard for months after
  its listing ended — exactly what happened to 118A Holloway Ave, which was still
  being presented as a live $1,980 rental five weeks after the listing came down
  (the Zillow page had long since reverted to an "Off market" home-value page).
  A 403 means "we could not tell", never "still live", so we must not fetch-and-
  guess. Instead these rows are **aged out**: anything first seen more than
  MANUAL_MAX_AGE_DAYS ago is marked 'removed' as unconfirmed. Re-confirming one is
  a by-hand chromerpc job (see MANUAL_SOURCES.md), and doing so refreshes
  last_seen_at / first_seen_at so it survives the next sweep.

Rows already `contacted` or `interested` are NEVER touched here — they are the
outreach history that the unit-level resend guard depends on (OUTREACH.md), so
losing them could mean emailing the same landlord twice.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

import requests

import common
import db

DEAD_MARKERS = (
    "this posting has been deleted",
    "this posting has expired",
    "this posting has been flagged for removal",
    "page not found",
)

# Sources whose pages answer a plain scripted GET truthfully.
SCRIPTABLE_SOURCES = {"craigslist"}

# Manually-gathered rows are dropped once they are this old and still unconfirmed.
MANUAL_MAX_AGE_DAYS = 30

# Statuses that carry outreach history — never auto-removed.
PROTECTED_STATUS = {"contacted", "interested"}

# Anti-bot / rate-limit responses. These mean "could not tell", NOT "alive".
UNVERIFIABLE_CODES = {401, 403, 405, 429}


def is_dead(resp) -> bool:
    if resp.status_code == 404:
        return True
    low = resp.text.lower()
    return any(m in low for m in DEAD_MARKERS)


def _age_days(row) -> float | None:
    """Days since we last had eyes on this listing."""
    stamp = row["first_seen_at"]
    for key in ("last_seen_at", "detail_fetched_at"):
        try:
            if row[key]:
                stamp = max(stamp or "", row[key])
        except (IndexError, KeyError):
            pass
    if not stamp:
        return None
    try:
        ts = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="also re-check removed listings")
    ap.add_argument("--max-age-days", type=int, default=MANUAL_MAX_AGE_DAYS,
                    help=f"age out unconfirmed manual-source rows older than this "
                         f"(default {MANUAL_MAX_AGE_DAYS})")
    args = ap.parse_args()

    cfg = common.load_config()
    conn = db.connect()
    statuses = "('rejected')" if args.all else "('rejected', 'removed')"
    rows = conn.execute(
        f"SELECT * FROM listings WHERE status NOT IN {statuses}"
    ).fetchall()

    fetchable = [r for r in rows
                 if (r["source"] or "") in SCRIPTABLE_SOURCES
                 and r["status"] not in PROTECTED_STATUS]
    manual = [r for r in rows
              if (r["source"] or "") not in SCRIPTABLE_SOURCES
              and r["status"] not in PROTECTED_STATUS]
    protected = len(rows) - len(fetchable) - len(manual)

    print(f"{len(rows)} active listing(s): {len(fetchable)} fetchable "
          f"({'/'.join(sorted(SCRIPTABLE_SOURCES))}), {len(manual)} manual-source "
          f"(bot-walled, aged out at {args.max_age_days}d), {protected} protected "
          f"(contacted/interested)")

    dead = alive = unverifiable = 0
    for i, r in enumerate(fetchable):
        try:
            resp = sess_get(cfg, r["url"])
        except requests.exceptions.RequestException as e:
            print(f"  ? {r['id']} error ({e}); leaving as-is", file=sys.stderr)
            unverifiable += 1
            continue
        if resp.status_code in UNVERIFIABLE_CODES:
            # Blocked or throttled — we genuinely do not know. Say so; do NOT
            # count it as alive, and do NOT remove it on a guess.
            print(f"  ? {r['id']} HTTP {resp.status_code} (blocked/throttled); "
                  f"leaving as-is", file=sys.stderr)
            unverifiable += 1
        elif is_dead(resp):
            db.set_status(conn, r["id"], "removed")
            conn.commit()
            dead += 1
            print(f"  ✗ {r['id']} taken down -> removed")
        else:
            alive += 1
        if i < len(fetchable) - 1:
            common.polite_sleep(cfg)

    # Manual sources: never fetched (bot-walled, and hammering them is impolite).
    # Age out anything we have not laid eyes on recently.
    aged = 0
    for r in manual:
        age = _age_days(r)
        if age is not None and age > args.max_age_days:
            db.set_status(conn, r["id"], "removed")
            conn.commit()
            aged += 1
            print(f"  ⏳ {r['id']} ({r['source']}) unconfirmed for {age:.0f}d "
                  f"-> removed")

    db.set_meta(conn, "last_link_check", db.now())
    conn.commit()
    conn.close()
    print(f"done: {alive} live, {dead} taken down, {aged} aged out, "
          f"{unverifiable} unverifiable (left as-is), "
          f"{len(manual) - aged} manual-source rows still within the age window")


_SESSION = None


def sess_get(cfg, url):
    global _SESSION
    if _SESSION is None:
        _SESSION = common.session(cfg)
    return _SESSION.get(url, timeout=30, allow_redirects=True)


if __name__ == "__main__":
    main()
