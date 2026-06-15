"""Send a Telegram card for a vetted listing.

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from .env. Sends the lead photo
with a caption (price, type, neighborhood, legit + fit scores, scam notes, and
links). Respects the notify thresholds unless --force is given.

Usage:
    py scripts/notify.py <post_id> [--force]
    py scripts/notify.py --all-qualifying    # every vetted, non-scam, un-notified
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv

import common
import db

ROOT = common.ROOT
load_dotenv(os.path.join(ROOT, ".env"))

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:8000")

LABEL_EMOJI = {"likely-legit": "🟢", "unverified-amateur": "🟡", "likely-scam": "🔴"}


def qualifies(row, cfg: dict) -> bool:
    n = cfg.get("notify", {})
    if (row["legit_label"] or "") == "likely-scam":
        return False
    if (row["legit_score"] or 0) < n.get("min_legit_score", 70):
        return False
    if (row["fit_score"] or 0) < n.get("min_fit_score", 60):
        return False
    return True


def build_minimal_caption(row) -> str:
    """Essentials + a one-line summary and the original Craigslist link."""
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

    lines = [
        f"🏠 <b>{_h(d.get('title') or 'Listing')}</b>",
        f"💵 ${d.get('price', '?')}" + (f"  ·  {_h(type_str)}" if type_str else ""),
        f"📍 {_h(d.get('neighborhood') or d.get('area') or 'San Francisco')}",
        f"{emoji} trust {d.get('legit_score','?')}%  |  ⭐ match {d.get('fit_score','?')}%",
    ]
    if d.get("verdict_summary"):
        s = d["verdict_summary"]
        lines.append(f"\n📝 {_h(s if len(s) <= 350 else s[:347] + '...')}")
    if d.get("contact"):
        lines.append(f"☎ {_h(d['contact'])}")
    src = "Zumper" if d.get("source") == "zumper" else "Craigslist"
    lines.append(f"\n🔗 View on {src}: {_h(d['url'])}")
    return "\n".join(lines)


def build_caption(row) -> str:
    d = db.row_to_dict(row)
    emoji = LABEL_EMOJI.get(d.get("legit_label"), "⚪")
    type_bits = []
    if d.get("bedrooms") is not None:
        type_bits.append(f"{d['bedrooms']:g}BR")
    if d.get("bathrooms") is not None:
        type_bits.append(f"{d['bathrooms']:g}BA")
    if d.get("sqft"):
        type_bits.append(f"{d['sqft']}sqft")
    type_str = " / ".join(type_bits) or (d.get("room_type") or "")

    lines = [
        f"🏠 <b>{_h(d.get('title') or 'Listing')}</b>",
        f"💵 ${d.get('price', '?')}  |  {_h(type_str)}",
        f"📍 {_h(d.get('neighborhood') or d.get('area') or '?')}",
        f"{emoji} legit {d.get('legit_score','?')}%  |  ⭐ fit {d.get('fit_score','?')}%",
    ]
    if d.get("verdict_summary"):
        lines.append(f"\n{_h(d['verdict_summary'])}")
    if d.get("red_flags"):
        lines.append("⚠️ " + _h("; ".join(d["red_flags"])))
    if d.get("contact"):
        lines.append(f"☎️ {_h(d['contact'])}")
    src = "Zumper" if d.get("source") == "zumper" else "Craigslist"
    lines.append(f"\n🔗 View on {src}: {_h(d['url'])}")
    lines.append(f"🗺 Dashboard: {_h(DASHBOARD_URL)}")
    return "\n".join(lines)


def _h(text: str) -> str:
    """Escape for Telegram HTML parse mode."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def lead_photo_url(row) -> str | None:
    """First remote image URL (we no longer keep local copies)."""
    if not row["image_urls"]:
        return None
    try:
        photos = json.loads(row["image_urls"])
        return photos[0] if photos else None
    except (ValueError, TypeError):
        return None


def send(row, minimal: bool = False) -> bool:
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env — skipping.",
              file=sys.stderr)
        return False
    caption = build_minimal_caption(row) if minimal else build_caption(row)
    photo = lead_photo_url(row)
    # Fetch the image bytes ourselves (Telegram's server can't fetch some hosts
    # e.g. Craigslist) and upload them — works for any source, no local storage.
    img_bytes = None
    if photo:
        try:
            ir = requests.get(photo, timeout=30, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://www.zumper.com/"})
            if ir.status_code == 200 and ir.content:
                img_bytes = ir.content
        except requests.exceptions.RequestException:
            pass
    try:
        if img_bytes:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"photo": ("photo.jpg", img_bytes)}, timeout=30)
        else:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": caption, "parse_mode": "HTML",
                      "disable_web_page_preview": True}, timeout=30)
        if r.status_code != 200:
            print(f"Telegram error {r.status_code}: {r.text}", file=sys.stderr)
            return False
    except requests.exceptions.RequestException as e:
        print(f"Telegram request failed: {e}", file=sys.stderr)
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("id", nargs="?")
    ap.add_argument("--force", action="store_true",
                    help="send even if below thresholds (still blocks scams)")
    ap.add_argument("--all-qualifying", action="store_true",
                    help="notify every vetted, qualifying, un-notified listing")
    ap.add_argument("--minimal", action="store_true",
                    help="send a minimal card (photo + title + price + trust)")
    args = ap.parse_args()

    cfg = common.load_config()
    conn = db.connect()

    if args.all_qualifying:
        rows = conn.execute(
            "SELECT * FROM listings WHERE status!='rejected' AND notified=0 "
            "AND legit_score IS NOT NULL"
        ).fetchall()
        targets = [r for r in rows if qualifies(r, cfg)]
    elif args.id:
        row = db.get(conn, args.id)
        if not row:
            print(f"No listing {args.id}", file=sys.stderr)
            sys.exit(1)
        if not args.force and not qualifies(row, cfg):
            print(f"{args.id} does not meet thresholds (use --force).")
            sys.exit(0)
        if args.force and (row["legit_label"] or "") == "likely-scam":
            print("Refusing to notify a likely-scam listing.", file=sys.stderr)
            sys.exit(1)
        targets = [row]
    else:
        ap.error("give a post id or --all-qualifying")

    sent = 0
    for row in targets:
        if send(row, minimal=args.minimal):
            db.mark_notified(conn, row["id"])
            conn.commit()
            sent += 1
            print(f"  ✓ notified {row['id']}")
    conn.close()
    print(f"Sent {sent} notification(s).")


if __name__ == "__main__":
    main()
