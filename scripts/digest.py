"""Compose + send the combined 3-stage Telegram digest.

This is the richer house-hunt digest (vs notify.py's simple new-picks list). It has
three sections:

  Stage 0 - Visits & calendar: every agreed in-person viewing (from data/visits.json),
    each with a one-tap "Add to Google Calendar" link (a Google render-template URL, so
    NO OAuth / API setup is needed - tapping it opens Google Calendar prefilled).
  Stage 1 - This fetch: how many listings were pulled and kept, split SF vs Berkeley
    (numbers from data/run_stats.json; kept totals computed live from the DB).
  Stage 2 - Outreach: how many we emailed (status='contacted') with their original CL
    links, PLUS genuinely good listings we CAN'T email directly yet (Zumper/other
    sources with no reply relay) so they can be chased manually.

It reuses notify.send_text for the actual Telegram call (same bot token / chat id).

    py scripts/digest.py            # print the composed message (dry run)
    py scripts/digest.py --send     # actually send it to Telegram

Data files (both optional - sections self-skip if absent/empty):
    data/visits.json     [{id,title,when_text,start,end,location,note}, ...]
    data/run_stats.json  {pulled_total, pulled:{SF,Berkeley}, kept_new:{SF,Berkeley}}
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
from urllib.parse import urlencode

import common
import db
import geo
import notify

ROOT = common.ROOT
DASHBOARD_URL = notify.DASHBOARD_URL
VISITS_FILE = os.path.join(db.DATA_DIR, "visits.json")
STATS_FILE = os.path.join(db.DATA_DIR, "run_stats.json")

# Good-but-unreachable bar (Zumper/other with no reply relay): worth a manual chase.
NONCONTACT_MIN_FIT = 78
NONCONTACT_MIN_TRUST = 72
NONCONTACT_CAP = 12


def _h(s) -> str:
    return notify._h(s)


def _region(row) -> str:
    """Two broad regions, matching the dashboard: Berkeley (East Bay) vs SF."""
    txt = " ".join(str(row[k] or "") for k in ("area", "neighborhood", "address")).lower()
    if "berkeley" in txt:
        return "Berkeley"
    if row["lat"] and row["lat"] >= 37.84:
        return "Berkeley"
    return "SF"


def _gcal_link(title: str, start: str, end: str, location: str, details: str) -> str:
    """Google Calendar 'render template' URL - opens a prefilled event, no API/OAuth.
    start/end are ISO strings with an offset (e.g. ...-07:00); converted to UTC Z."""
    def utc(s: str) -> str:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    q = urlencode({
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{utc(start)}/{utc(end)}",
        "location": location or "",
        "details": details or "",
    })
    return f"https://calendar.google.com/calendar/render?{q}"


def _load(path):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError):
            return None
    return None


def stage0_visits(conn) -> list[str]:
    visits = _load(VISITS_FILE) or []
    if not visits:
        return []
    out = ["\U0001F4C5 <b>STAGE 0 - Upcoming viewings</b>"]
    for v in visits:
        row = db.get(conn, v["id"]) if v.get("id") else None
        url = (row["url"] if row else None) or v.get("url") or ""
        cal = _gcal_link(v.get("title", "Apartment viewing"), v["start"], v["end"],
                         v.get("location", ""), v.get("note", "") + (f"\n{url}" if url else ""))
        block = [f"• <b>{_h(v.get('title','Viewing'))}</b>",
                 f"  \U0001F552 {_h(v.get('when_text',''))}"]
        if v.get("location"):
            block.append(f"  \U0001F4CD {_h(v['location'])}")
        block.append(f"  ➕ <a href=\"{_h(cal)}\">Add to Google Calendar</a>")
        if url:
            block.append(f"  \U0001F517 <a href=\"{_h(url)}\">original listing</a>")
        out.append("\n".join(block))
    return out


def stage1_stats(conn) -> list[str]:
    stats = _load(STATS_FILE) or {}
    rows = conn.execute(
        "SELECT * FROM listings WHERE status NOT IN ('rejected','removed')").fetchall()
    kept = {"SF": 0, "Berkeley": 0}
    for r in rows:
        kept[_region(r)] += 1
    pulled = stats.get("pulled") or {}
    kn = stats.get("kept_new") or {}
    lines = ["\U0001F4CA <b>STAGE 1 - This fetch</b>"]
    if stats.get("pulled_total") is not None:
        if pulled.get("SF") is not None and pulled.get("Berkeley") is not None:
            lines.append(f"Pulled: {stats['pulled_total']} new "
                         f"(SF {pulled['SF']} · Berkeley {pulled['Berkeley']})")
        else:
            lines.append(f"Pulled: {stats['pulled_total']} new (SF + Berkeley)")
    if kn:
        lines.append(f"Kept (new this fetch): SF {kn.get('SF',0)} · "
                     f"Berkeley {kn.get('Berkeley',0)}")
    lines.append(f"On dashboard now: {kept['SF']+kept['Berkeley']} quality "
                 f"(SF {kept['SF']} · Berkeley {kept['Berkeley']})")
    return ["\n".join(lines)]


def stage2_outreach(conn, picked: list) -> list[str]:
    rows = conn.execute(
        "SELECT * FROM listings WHERE status NOT IN ('rejected','removed')").fetchall()
    contacted = [r for r in rows if r["status"] == "contacted"]
    out = []
    head = [f"\U0001F4E8 <b>STAGE 2 - Outreach</b>",
            f"Emailed (awaiting reply): {len(contacted)}"]
    for r in contacted:
        head.append(f"  • {_h((r['title'] or 'Listing')[:42])} "
                    f"— <a href=\"{_h(r['url'])}\">CL post</a>")
    out.append("\n".join(head))

    # NEW good 1BR/1BA picks from the OTHER sources (Zillow / Zumper / Apartments) —
    # we don't auto-email those (no CL relay), so surface them for manual contact.
    # ONLY un-notified ones (notified=0) so each digest shows just the LATEST picks,
    # not the whole accumulated dashboard; they're marked notified after a send.
    cand = []
    for r in rows:
        if r["status"] != "vetted" or (r["source"] or "") == "craigslist":
            continue
        if r["notified"]:                     # already sent in a prior digest
            continue
        if (r["room_type"] or "") != "1br":   # contact ONLY 1 bed / 1 bath — no studios / 2+ bed
            continue
        if (r["legit_label"] or "") == "likely-scam":
            continue
        if geo.classify(r["lat"], r["lng"], r["area"])["area_tier"] == "avoid":
            continue
        if (r["fit_score"] or 0) < NONCONTACT_MIN_FIT or (r["legit_score"] or 0) < NONCONTACT_MIN_TRUST:
            continue
        cand.append(r)
    cand.sort(key=lambda r: (-(r["fit_score"] or 0), -(r["legit_score"] or 0)))
    picked.extend(r["id"] for r in cand)      # mark ALL of these notified after send
    if cand:
        blk = [f"\U0001F3E2 <b>New 1BR/1BA to contact yourself (Zillow/Zumper/Apartments) - {len(cand)}</b>"]
        for r in cand[:NONCONTACT_CAP]:
            blk.append(f"  • {_h((r['title'] or 'Listing')[:38])} — ${r['price']} "
                       f"1br ({_region(r)}) — <a href=\"{_h(r['url'])}\">link</a>")
        if len(cand) > NONCONTACT_CAP:
            blk.append(f"  … +{len(cand)-NONCONTACT_CAP} more on the dashboard")
        out.append("\n".join(blk))
    return out


def compose():
    """Returns (messages, picked_ids). picked_ids = the NEW non-CL 1BR picks listed
    in Stage 2; main() marks them notified after a successful send so they don't
    repeat in future digests."""
    conn = db.connect()
    picked: list = []
    header = "\U0001F3E0 <b>SF / Berkeley House-Hunt - digest</b>"
    sections = stage0_visits(conn) + stage1_stats(conn) + stage2_outreach(conn, picked)
    footer = f"\U0001F5FA Full ledger: {_h(DASHBOARD_URL)}"
    conn.close()
    # pack into <=3900-char messages (Telegram cap 4096)
    msgs, cur, n = [], [header], len(header)
    for s in sections + [footer]:
        if n + len(s) + 2 > 3900 and len(cur) > 1:
            msgs.append("\n\n".join(cur))
            cur, n = [s], len(s)
        else:
            cur.append(s)
            n += len(s) + 2
    msgs.append("\n\n".join(cur))
    return msgs, picked


def main() -> None:
    ap = argparse.ArgumentParser(description="Send the combined 3-stage house-hunt digest.")
    ap.add_argument("--send", action="store_true", help="actually send (default: print only)")
    args = ap.parse_args()
    messages, picked = compose()
    if not args.send:
        print("\n\n===== MESSAGE BREAK =====\n\n".join(messages))
        print(f"\n[dry-run] {len(messages)} message(s); {len(picked)} new pick(s) "
              "would be marked notified on send.")
        return
    ok = all(notify.send_text(m) for m in messages)
    if ok and picked:
        conn = db.connect()
        for pid in picked:
            db.mark_notified(conn, pid)
        conn.commit()
        conn.close()
    print(f"Sent {len(messages)} message(s); marked {len(picked) if ok else 0} pick(s) notified."
          if ok else "Send failed.")


if __name__ == "__main__":
    main()
