"""Send ONE short, text-only Telegram digest of top listings (no images).

Each block is name + price/type + area + trust/match scores + a one-line summary
+ a deep-link into the dashboard (#id=<id>); a footer links the full ledger.
Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID and the dashboard URL from .env.
Scams are always blocked; thresholds apply unless --force.

Usage:
    py scripts/notify.py --new               # the normal refresh send: qualifying
                                             # NEW (un-notified) picks, best first;
                                             # if none qualify, a short "nothing
                                             # worthy today" note instead.
    py scripts/notify.py --top 10            # best N overall as one digest
    py scripts/notify.py <id> [<id> ...] [--force]   # specific ids, one digest
    py scripts/notify.py --all-qualifying    # every qualifying un-notified (can be a lot)
"""
from __future__ import annotations

import argparse
import os
import sys

import requests
from dotenv import load_dotenv

import common
import db
import geo

ROOT = common.ROOT
load_dotenv(os.path.join(ROOT, ".env"))

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
# Prefer the public Vercel dashboard for the "view it here" link; fall back local.
DASHBOARD_URL = (os.getenv("VERCEL_DASHBOARD_URL")
                 or os.getenv("DASHBOARD_URL", "http://localhost:8000")).rstrip("/")

LABEL_EMOJI = {"likely-legit": "🟢", "unverified-amateur": "🟡", "likely-scam": "🔴"}


def _tier(row) -> str:
    return geo.classify(row["lat"], row["lng"], row["area"])["area_tier"]


def qualifies(row, cfg: dict) -> bool:
    n = cfg.get("notify", {})
    if (row["legit_label"] or "") == "likely-scam":
        return False
    if _tier(row) == "avoid":  # never alert on Tenderloin-adjacent / avoid areas
        return False
    if (row["legit_score"] or 0) < n.get("min_legit_score", 70):
        return False
    if (row["fit_score"] or 0) < n.get("min_fit_score", 60):
        return False
    return True


def _rank_key(row) -> tuple:
    """Flat model: order by MATCH (fit) desc, then trust (legit) desc.
    qualifies() already drops unsafe/avoid areas, so no area weighting here."""
    return (-(row["fit_score"] or 0), -(row["legit_score"] or 0))


def _item_block(row) -> str:
    """One compact, text-only block for a listing: name, price/type, area,
    scores, a one-line summary, and a deep-link into the dashboard. No image."""
    d = db.row_to_dict(row)
    emoji = LABEL_EMOJI.get(d.get("legit_label"), "⚪")
    type_bits = []
    if d.get("bedrooms") is not None:
        type_bits.append("Studio" if d["bedrooms"] == 0 else f"{d['bedrooms']:g}BR")
    if d.get("bathrooms") is not None:
        type_bits.append(f"{d['bathrooms']:g}BA")
    if d.get("sqft"):
        type_bits.append(f"~{d['sqft']}sqft")
    type_str = " / ".join(type_bits)

    area = geo.classify(d.get("lat"), d.get("lng"), d.get("area"))
    head = f"🏠 <b>{_h(d.get('title') or 'Listing')}</b>"
    money = f"💵 ${d.get('price', '?')}" + (f" · {_h(type_str)}" if type_str else "")
    place = (f"📍 {_h(area['area_name'] or d.get('area') or 'San Francisco')}"
             f"  ·  {emoji} {d.get('legit_score','?')} · ⭐ {d.get('fit_score','?')}")
    lines = [head, money, place]
    if d.get("verdict_summary"):
        s = d["verdict_summary"]
        lines.append(f"📝 {_h(s if len(s) <= 160 else s[:157] + '...')}")
    lines.append(f"🔗 {_h(DASHBOARD_URL)}/#id={_h(d['id'])}")
    return "\n".join(lines)


def _h(text: str) -> str:
    """Escape for Telegram HTML parse mode."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def send_text(text: str) -> bool:
    """Send one plain HTML text message (no image)."""
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env — skipping.",
              file=sys.stderr)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}, timeout=30)
        if r.status_code != 200:
            print(f"Telegram error {r.status_code}: {r.text}", file=sys.stderr)
            return False
    except requests.exceptions.RequestException as e:
        print(f"Telegram request failed: {e}", file=sys.stderr)
        return False
    return True


def _chunk_blocks(header: str, blocks: list[str], footer: str,
                  limit: int = 3900) -> list[str]:
    """Pack header + blocks + footer into as few <=limit-char messages as
    possible (Telegram's hard cap is 4096)."""
    msgs, cur = [], [header]
    for b in blocks:
        candidate = "\n\n".join(cur + [b])
        if len(candidate) > limit and len(cur) > 1:
            msgs.append("\n\n".join(cur))
            cur = [b]
        else:
            cur.append(b)
    cur.append(footer)
    msgs.append("\n\n".join(cur))
    return msgs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("id", nargs="*", help="one or more post ids")
    ap.add_argument("--force", action="store_true",
                    help="send even if below thresholds (still blocks scams)")
    ap.add_argument("--new", action="store_true",
                    help="digest the qualifying NEW (un-notified) picks from "
                         "this fetch, best first; if none qualify, send a short "
                         "'nothing worthy today' note. The normal refresh send.")
    ap.add_argument("--top", type=int, metavar="N",
                    help="digest the top N qualifying, deduped picks (best by "
                         "match+trust) — best overall, regardless of new-ness")
    ap.add_argument("--all-qualifying", action="store_true",
                    help="digest EVERY vetted, qualifying, un-notified listing "
                         "(can be a lot — prefer --top N)")
    ap.add_argument("--minimal", action="store_true",
                    help="(kept for back-compat; notifications are always short text)")
    ap.add_argument("--quiet-if-empty", action="store_true",
                    help="with --new, send NOTHING when no new picks qualify — for "
                         "frequent (e.g. 4-hourly) cron runs, to avoid repeated "
                         "'nothing new' pings. Only real picks ping.")
    args = ap.parse_args()

    cfg = common.load_config()
    conn = db.connect()

    if args.new:
        # qualifying NEW (un-notified) primaries from this fetch, best first
        rows = conn.execute(
            "SELECT * FROM listings WHERE status!='rejected' AND notified=0 "
            "AND legit_score IS NOT NULL"
        ).fetchall()
        prim = [r for r in rows if r["dup_group"] in (None, r["id"])]
        targets = [r for r in prim if qualifies(r, cfg)]
    elif args.top:
        rows = conn.execute(
            "SELECT * FROM listings WHERE status!='rejected' AND legit_score IS NOT NULL"
        ).fetchall()
        # only cluster primaries (dup_group null or == id), qualifying, best first
        prim = [r for r in rows if r["dup_group"] in (None, r["id"])]
        q = [r for r in prim if qualifies(r, cfg)]
        q.sort(key=_rank_key)  # match, then trust
        targets = q[:args.top]
    elif args.all_qualifying:
        rows = conn.execute(
            "SELECT * FROM listings WHERE status!='rejected' AND notified=0 "
            "AND legit_score IS NOT NULL"
        ).fetchall()
        targets = [r for r in rows if qualifies(r, cfg)]
    elif args.id:
        targets = []
        for pid in args.id:
            row = db.get(conn, pid)
            if not row:
                print(f"No listing {pid}", file=sys.stderr)
                continue
            if (row["legit_label"] or "") == "likely-scam":
                print(f"Skipping likely-scam {pid}.", file=sys.stderr)
                continue
            if not args.force and not qualifies(row, cfg):
                print(f"{pid} below thresholds (use --force); skipping.")
                continue
            targets.append(row)
    else:
        ap.error("give one or more post ids, --new, --top N, or --all-qualifying")

    if not targets:
        # For the routine --new send, still ping so the user knows the fetch ran
        # and found nothing worth a look; otherwise just stay quiet. --quiet-if-empty
        # suppresses that ping (frequent cron runs ping only on a real pick).
        if args.new and not args.quiet_if_empty:
            note = ("🏠 <b>SF House-Hunt</b>\nNo new postings worth a look this "
                    f"round.\n\n🗺 Full ledger: {_h(DASHBOARD_URL)}")
            if send_text(note):
                print("Sent 'nothing worthy this round' note.")
            else:
                print("Note send failed.", file=sys.stderr)
        else:
            print("Nothing to notify (quiet).")
        conn.close()
        return

    # rank by match, then trust; send ONE digest
    targets.sort(key=_rank_key)
    n = len(targets)
    kind = "new pick" if args.new else "top pick"
    header = f"🏠 <b>SF House-Hunt — {n} {kind}{'s' if n != 1 else ''}</b>"
    footer = f"🗺 Full ledger: {_h(DASHBOARD_URL)}"
    messages = _chunk_blocks(header, [_item_block(r) for r in targets], footer)

    sent_all = all(send_text(m) for m in messages)
    if sent_all:
        for row in targets:
            db.mark_notified(conn, row["id"])
        conn.commit()
        print(f"Sent digest of {n} listing(s) in {len(messages)} message(s).")
    else:
        print("Digest send failed; not marking notified.", file=sys.stderr)
    conn.close()


if __name__ == "__main__":
    main()
