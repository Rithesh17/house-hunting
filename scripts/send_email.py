"""Stage 2 — send ONE plain-text outreach email to a listing's relay address.

This script is intentionally THIN: the human-style body is hand-authored by the
orchestrator (Claude) from sensitive/email_body.json (read it, pick/adapt a
template, inject the post URL, vary the wording). This script only performs the
actual Gmail SMTP send so the message does NOT look automated:
  - sends through Gmail's authenticated SMTP (smtp.gmail.com:587, STARTTLS) using
    GMAIL_USER + GMAIL_APP_PASSWORD from .env (an App Password, NOT the account
    password), so SPF/DKIM/DMARC are Google's real signatures;
  - builds a minimal text/plain message with a real "Rithesh <user>" From — NO
    X-Mailer / auto-generated headers that would out it as a bot.

State lives ONLY in the DB (no side-files): on a successful send the listing's
status is flipped vetted -> 'contacted', which then syncs to Supabase and survives
the hydrate round-trip. So the cloud read-model is the single source of truth for
"already reached out" across every run/machine.

The resend guard is UNIT-LEVEL, not row-level — a relisted post is a NEW id, so we
refuse a send if the SAME UNIT was already contacted, detected by the dedup cluster
(dup_group), the same normalized street address, OR the same captured phone. This
stops a repost (new id, different title, even a changed price) from being emailed a
second time. --force overrides.

Autonomous (cron) sends pass --auto, which proceeds ONLY if OUTREACH_AUTOSEND=1 in
.env — so the unattended cron can't email anyone unless the switch is set. Manual
sends omit --auto and always proceed (you are the human in the loop).

Usage:
    # dry-run (print the exact envelope + body, send nothing):
    python3 scripts/send_email.py --listing 7941223993 \
        --subject "Interested in this: 540 Arguello Blvd" \
        --body-file /tmp/body.txt --dry-run

    # real send (resolves the relay address from the DB by listing id):
    python3 scripts/send_email.py --listing 7941223993 \
        --subject "..." --body-file /tmp/body.txt

    # override recipient explicitly instead of DB lookup:
    python3 scripts/send_email.py --to someone@hous.craigslist.org \
        --subject "..." --body "inline body text"
"""
from __future__ import annotations

import argparse
import os
import smtplib
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from dotenv import load_dotenv

import common
import db

ROOT = common.ROOT
load_dotenv(os.path.join(ROOT, ".env"))

# Reuse the dedup address normalizer for the unit-level contacted guard.
_TOOLS = os.path.join(ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
import dedup  # noqa: E402  (provides norm_addr)

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
FROM_NAME = os.getenv("GMAIL_FROM_NAME", "Rithesh")


def _get_row(listing_id: str):
    conn = db.connect()
    row = conn.execute(
        "SELECT id, reply_email, url, status, room_type FROM listings WHERE id=?",
        (listing_id,)
    ).fetchone()
    conn.close()
    return row


def _mark_contacted(listing_id: str) -> None:
    """Flip the listing to 'contacted'. Persists to Supabase on the next sync and
    survives hydrate."""
    conn = db.connect()
    db.set_status(conn, listing_id, "contacted")
    conn.commit()
    conn.close()


def _digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _unit_already_contacted(listing_id: str):
    """Return (other_id, reason) if the SAME UNIT was already contacted — by dedup
    cluster, same normalized address, or same phone — even if dedup didn't cluster
    them. The unit-level guard a relisted post must not slip past."""
    conn = db.connect()
    me = conn.execute(
        "SELECT id, dup_group, address, phone FROM listings WHERE id=?", (listing_id,)
    ).fetchone()
    if not me:
        conn.close()
        return None
    contacted = conn.execute(
        "SELECT id, dup_group, address, phone FROM listings "
        "WHERE status='contacted' AND id!=?", (listing_id,)
    ).fetchall()
    conn.close()
    grp = me["dup_group"] or listing_id
    my_addr = dedup.norm_addr(me["address"])
    my_phone = _digits(me["phone"])
    for c in contacted:
        if (c["dup_group"] or c["id"]) == grp:
            return c["id"], "same dedup cluster"
        if my_addr and dedup.norm_addr(c["address"]) == my_addr:
            return c["id"], f"same address ({me['address']})"
        if my_phone and len(my_phone) >= 10 and _digits(c["phone"]) == my_phone:
            return c["id"], "same contact phone"
    return None


def send(to_addr: str, subject: str, body: str, listing_id: str | None,
         dry_run: bool = False) -> bool:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        print("GMAIL_USER / GMAIL_APP_PASSWORD not set in .env — cannot send.",
              file=sys.stderr)
        return False

    msg = EmailMessage()
    msg["From"] = formataddr((FROM_NAME, GMAIL_USER))
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=GMAIL_USER.split("@")[-1])
    msg.set_content(body)

    print("=" * 64)
    print(f"From:    {msg['From']}")
    print(f"To:      {to_addr}")
    print(f"Subject: {subject}")
    print("-" * 64)
    print(body)
    print("=" * 64)

    if dry_run:
        print("[dry-run] nothing sent.")
        return True

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.ehlo()
        s.starttls()
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)
    print(f"[sent] -> {to_addr}")

    if listing_id:
        _mark_contacted(listing_id)
        print(f"[status] {listing_id} -> contacted (run sync_supabase.py to publish)")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Send one outreach email (Gmail SMTP).")
    ap.add_argument("--listing", help="listing id (resolves relay email + logs the send)")
    ap.add_argument("--to", help="explicit recipient (overrides DB lookup)")
    ap.add_argument("--subject", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--body", help="inline body text")
    g.add_argument("--body-file", help="path to a file holding the body text")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="send even if this unit was already contacted")
    ap.add_argument("--auto", action="store_true",
                    help="autonomous (cron) send: proceeds ONLY if OUTREACH_AUTOSEND=1 "
                         "in .env. Manual sends omit this and always proceed.")
    args = ap.parse_args()

    if args.auto and os.getenv("OUTREACH_AUTOSEND") != "1":
        print("Autonomous send blocked: OUTREACH_AUTOSEND != 1 in .env "
              "(set it to enable cron auto-send).", file=sys.stderr)
        return 1

    body = args.body
    if args.body_file:
        with open(args.body_file) as f:
            body = f.read().strip("\n")

    to_addr = args.to
    if args.listing:
        row = _get_row(args.listing)
        if not row:
            print(f"Listing {args.listing} not in DB.", file=sys.stderr)
            return 1
        # AUTO-send is restricted to 1-bed / 1-bath units. Studios and 2+ bed are
        # surfaced on the dashboard (scores untouched) but never auto-emailed —
        # contact those manually (a manual send omits --auto and is allowed).
        if args.auto and (row["room_type"] or "") != "1br":
            print(f"Auto-send is 1BR/1BA-only — {args.listing} is "
                  f"'{row['room_type']}'; surface it on the dashboard and contact "
                  "manually if wanted.", file=sys.stderr)
            return 1
        if not args.force and not args.dry_run:
            if row["status"] == "contacted":
                print(f"Listing {args.listing} is already 'contacted' — not re-sending. "
                      "Use --force to override.", file=sys.stderr)
                return 1
            hit = _unit_already_contacted(args.listing)
            if hit:
                other, why = hit
                print(f"Listing {args.listing} is the SAME UNIT as already-contacted "
                      f"{other} ({why}) — not re-sending. Use --force to override.",
                      file=sys.stderr)
                return 1
        if not to_addr:
            to_addr = row["reply_email"]
            if not to_addr:
                print(f"No relay email captured for listing {args.listing} "
                      "(reveal the CL reply contact BY HAND first — see CLAUDE.md "
                      "'BROWSER = MANUAL, ALWAYS' / Refresh step 4c).", file=sys.stderr)
                return 1
    if not to_addr:
        print("Need --to or --listing with a captured relay email.", file=sys.stderr)
        return 1

    ok = send(to_addr, args.subject, body, args.listing, dry_run=args.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
