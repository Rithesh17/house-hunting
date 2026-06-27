"""Stage 0 — read inbound replies to our outreach emails (Gmail IMAP).

Thin FETCH-only tool, mirroring research.py. It pulls the unread inbox messages,
extracts sender/subject/date/plain-body, and keeps the ones that LOOSELY look
rental-related (a reply, a Craigslist relay sender, a street-address-ish string, or
rental keywords). It does NOT try to hard-match a reply to a specific listing — that
matching is BRITTLE, and we already have a Claude instance running Stage 0 to vet
each reply, so it does the matching too. The script just emits, as one JSON object:
  - `contacted_listings`: a compact reference of every listing we've emailed
    (id, title, address, price, room_type, relay) for Claude to match against, and
  - `replies`: the loosely-relevant inbound messages.
Claude then matches each reply to a listing AND judges it (money-before-viewing =
bad; gives info / agrees to an in-person tour = good; off-platform / wire = bad).

It reads ALL inbound mail SINCE THE LAST RUN (regardless of read/unread), so a reply
you already opened in Gmail is still covered. The cutoff is a DB-meta marker
(`last_reply_read`); the run advances it to its start time afterwards. Messages are
fetched with BODY.PEEK so reading them here never changes their read state in Gmail.
(First run with no marker looks back 7 days.) Re-vetting an already-seen reply is
harmless — Claude judges it again.

Credentials: GMAIL_USER + GMAIL_APP_PASSWORD from .env (the same App Password used
for sending; it works for IMAP too).

Usage:
    python3 scripts/read_replies.py                 # all mail since last run -> JSON
    python3 scripts/read_replies.py --since 3       # last 3 days (does NOT move marker)
"""
from __future__ import annotations

import argparse
import datetime
import email
import imaplib
import json
import os
import re
import sys
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from dotenv import load_dotenv

import common
import db

ROOT = common.ROOT
load_dotenv(os.path.join(ROOT, ".env"))

META_KEY = "last_reply_read"      # marker so each run reads only mail since the last
DEFAULT_LOOKBACK_DAYS = 7         # first run (no marker yet)

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

MAX_BODY = 4000

# Loose relevance: street-address-ish ("333 9th Ave", "1419 30th St") + rental words.
_ADDR_RE = re.compile(r"\b\d{1,5}\s+\w+(\s+\w+)?\s+"
                      r"(st|street|ave|avenue|blvd|boulevard|rd|road|dr|drive|way|"
                      r"ct|court|ln|lane|pl|place|ter|terrace|hwy)\b", re.I)
_KEYWORDS = ("apartment", "apt", "rent", "available", "viewing", "showing", "tour",
             "lease", "studio", "bedroom", "bdr", "1br", "unit", "move-in", "move in",
             "deposit", "craigslist", "see the place", "see it", "still available")


def _decode(s) -> str:
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return str(s)


def _plain_body(msg) -> str:
    """Best plain-text body; fall back to a crude HTML strip."""
    if msg.is_multipart():
        # Prefer the first text/plain part.
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
                    "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return part.get_content().strip()
                except Exception:
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8",
                                          "replace").strip()
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                import re
                try:
                    html = part.get_content()
                except Exception:
                    html = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
                return re.sub(r"<[^>]+>", " ", html).strip()
        return ""
    try:
        return msg.get_content().strip()
    except Exception:
        return (msg.get_payload(decode=True) or b"").decode(
            msg.get_content_charset() or "utf-8", "replace").strip()


def _contacted_listings() -> list:
    """Reference set Claude matches replies against (NOT used for hard matching here).
    Includes everyone we've emailed plus anyone with a captured relay, newest first."""
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, title, address, price, room_type, reply_email, status "
        "FROM listings WHERE status='contacted' OR (reply_email IS NOT NULL "
        "AND reply_email != '') ORDER BY contact_fetched_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _looks_relevant(from_addr: str, subject: str, body: str) -> bool:
    """Very loose filter: keep anything plausibly about a listing; drop obvious
    newsletters/promos. Claude makes the real call, so err toward keeping."""
    hay = f"{subject}\n{body}".lower()
    if "craigslist.org" in (from_addr or "").lower():
        return True
    if re.match(r"\s*(re|fwd)\s*:", subject or "", re.I):
        return True
    if _ADDR_RE.search(hay):
        return True
    return any(k in hay for k in _KEYWORDS)


def _msg_dt(s):
    """Parse a Date header to an aware UTC datetime (None if unparseable)."""
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt.replace(tzinfo=datetime.timezone.utc) if dt.tzinfo is None else dt


def fetch(since_days) -> list:
    """Read inbound mail newer than the cutoff (since last run, or --since N days),
    regardless of read/unread. BODY.PEEK so read state is never changed."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("GMAIL_USER / GMAIL_APP_PASSWORD not set in .env.", file=sys.stderr)
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    if since_days:
        cutoff = now - datetime.timedelta(days=since_days)
    else:
        conn = db.connect()
        last = db.get_meta(conn, META_KEY)
        conn.close()
        cutoff = None
        if last:
            try:
                cutoff = datetime.datetime.fromisoformat(last)
            except ValueError:
                cutoff = None
        if cutoff is None:
            cutoff = now - datetime.timedelta(days=DEFAULT_LOOKBACK_DAYS)
        elif cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=datetime.timezone.utc)

    M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    M.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    M.select("INBOX")
    # IMAP SINCE is day-granular; widen by a day, then filter precisely by Date.
    imap_date = (cutoff - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f"(SINCE {imap_date})")
    ids = data[0].split() if data and data[0] else []
    out = []
    for num in ids:
        typ, msg_data = M.fetch(num, "(BODY.PEEK[])")   # PEEK: never mark as read
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        dt = _msg_dt(msg.get("Date", ""))
        if dt is not None and dt <= cutoff:
            continue   # already covered by an earlier run
        from_name, from_addr = parseaddr(msg.get("From", ""))
        subject = _decode(msg.get("Subject", ""))
        body = _plain_body(msg)[:MAX_BODY]
        if not _looks_relevant(from_addr, subject, body):
            continue  # obvious non-rental noise
        out.append({
            "from_name": _decode(from_name),
            "from_addr": from_addr,
            "subject": subject,
            "date": msg.get("Date", ""),
            "body": body,
        })

    M.logout()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Read inbound outreach replies (Gmail IMAP).")
    ap.add_argument("--since", type=int,
                    help="scan the last N days instead of since-last-run (does NOT move the marker)")
    args = ap.parse_args()

    scan_start = db.now()   # capture BEFORE reading; mail arriving during the run is caught next time
    replies = fetch(args.since)
    if not args.since:
        conn = db.connect()
        db.set_meta(conn, META_KEY, scan_start)
        conn.commit()
        conn.close()
    print(f"{len(replies)} relevant repl(y/ies) since last run — Claude matches + vets them.",
          file=sys.stderr)
    print(json.dumps({
        "contacted_listings": _contacted_listings(),
        "replies": replies,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
