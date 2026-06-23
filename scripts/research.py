"""Stage 2 — gather FREE external facts for each new listing and write a research
bundle the vetting subagent reads alongside the post + photos.

Division of labor: this script only FETCHES facts (deterministic, free); it makes
NO verdict. The subagent semantically cross-checks them against the post's claims
(name match, price plausibility, legit-repost vs scam-flood, real parcel).

Per listing it assembles `data/research/<id>.json`:
  - dre:      DRE license record(s) for any license # in the body (verify_dre)
  - owner:    assessor parcel facts for the address/coords (owner_lookup)
  - market:   cached market range for (area_group, room_type) + price ratio
  - siblings: other posts (ANY status) that share an address / coords / photo /
              contact — for the legit-repost-vs-flood and stolen-photo judgment

    py scripts/research.py 7942626550            # one or more ids
    py scripts/research.py --all-new             # every new, detail-fetched listing
    py scripts/research.py --buckets             # print market buckets needing a web lookup
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))   # reuse dedup's cluster helpers

import db
import dedup            # img_ids / addr_key / coord_key (single source of truth)
import market_comps
import owner_lookup
import verify_dre


def _contacts(row) -> set[str]:
    out = set()
    for k in ("phone", "contact", "dre_number"):
        try:
            v = row[k]
        except (KeyError, IndexError):
            v = None
        if v:
            out.add(str(v).strip().lower())
    return out


def find_siblings(conn, target) -> list[dict]:
    """Other listings sharing an address / coords / photo / contact with target.
    Same-address+coords = same unit (repost?); shared photo/contact across a
    DIFFERENT address = reused/stolen (spam ring). The subagent judges which."""
    t_imgs = dedup.img_ids(target["image_urls"])
    t_addr = dedup.addr_key(target)
    t_coord = dedup.coord_key(target)
    t_contacts = _contacts(target)
    out = []
    for o in conn.execute("SELECT * FROM listings WHERE id != ?", (target["id"],)):
        shared = []
        same_addr = bool(t_addr and dedup.addr_key(o) == t_addr)
        if t_imgs and (t_imgs & dedup.img_ids(o["image_urls"])):
            shared.append("photo")
        if same_addr:
            shared.append("address")
        if t_coord and dedup.coord_key(o) == t_coord:
            shared.append("coords")
        if t_contacts and (t_contacts & _contacts(o)):
            shared.append("contact")
        if shared:
            out.append({
                "id": o["id"], "status": o["status"], "price": o["price"],
                "room_type": o["room_type"], "title": o["title"],
                "address": o["address"], "url": o["url"],
                "legit_label": o["legit_label"],
                "shared_via": shared, "same_address": same_addr,
            })
    return out


def _dre_block(row) -> dict | None:
    nums = []
    if row["dre_number"]:
        nums += verify_dre.extract_dre(row["dre_number"]) or [row["dre_number"]]
    nums += verify_dre.extract_dre(row["description"])
    nums = list(dict.fromkeys(verify_dre.normalize_id(n) for n in nums if n))
    if not nums:
        return None
    recs = []
    for n in nums[:3]:
        recs.append(verify_dre.lookup(n))
        time.sleep(0.4)
    return {"numbers_found": nums, "records": recs}


def _web_checks(row, dre: dict | None) -> list[dict]:
    """Suggested WebSearches for the vetting subagent (scripts can't web-search).
    The big one: is the SAME property listed elsewhere at a different price / for
    sale? That exposes cloned/hijacked listings (how 3870 Sacramento & 133 Caine
    Ave were caught)."""
    out = []
    addr = (row["address"] or "").strip()
    if addr:
        out.append({
            "purpose": "Is this SAME address listed elsewhere? FLAG only on a "
                       "CONTRADICTION: a much-higher real rent (big gap = cloned + "
                       "undercut bait, counts even if that listing is now "
                       "'unavailable') or a FOR-SALE listing (stolen photos). "
                       "Comparable price, or no other listing, or just aggregator "
                       "echoes of this post = fine (do not flag).",
            "query": f'"{addr}" San Francisco rent OR "for sale"'})
    if row["phone"]:
        out.append({"purpose": "phone reused across unrelated listings / scam reports",
                    "query": f'"{row["phone"]}"'})
    for rec in (dre or {}).get("records", []):
        if rec.get("found") and rec.get("name"):
            out.append({"purpose": "the licensed agent's real listings / brokerage",
                        "query": f'{rec["name"]} {rec.get("employing_broker") or ""} '
                                 f'San Francisco rental'.strip()})
            break
    return out


def _market_block(conn, row) -> dict:
    group = market_comps.area_group(row["area"])
    rng = market_comps.range_for(conn, row["area"], row["room_type"])
    block = {"area_group": group, "room_type": row["room_type"]}
    if not rng:
        block["needs_lookup"] = True
        return block
    price, median = row["price"], rng.get("median")
    block["range"] = {k: rng.get(k) for k in ("low", "median", "high", "source", "fetched_at")}
    if price and median:
        block["ratio_vs_median"] = round(price / median, 2)  # context only; LLM judges
    return block


def build_bundle(conn, post_id: str) -> dict | None:
    row = db.get(conn, post_id)
    if not row:
        print(f"  ! {post_id} not in DB", file=sys.stderr)
        return None
    dre = _dre_block(row)
    bundle = {
        "id": post_id,
        "fetched_at": db.now(),
        "post": {k: row[k] for k in ("address", "price", "room_type", "area",
                                     "neighborhood", "lat", "lng", "phone",
                                     "contact", "dre_number")},
        "dre": dre,
        "owner": owner_lookup.owner_of_record(row["lat"], row["lng"], row["address"])
                 if row["lat"] is not None else {"found": False, "note": "no coords"},
        "market": _market_block(conn, row),
        "siblings": find_siblings(conn, row),
        "web_checks": _web_checks(row, dre),
    }
    os.makedirs(db.RESEARCH_DIR, exist_ok=True)
    with open(os.path.join(db.RESEARCH_DIR, f"{post_id}.json"), "w") as f:
        json.dump(bundle, f, indent=2)
    return bundle


def _new_ids(conn) -> list[str]:
    return [r["id"] for r in conn.execute(
        "SELECT id FROM listings WHERE status='new' AND detail_fetched_at IS NOT NULL")]


def _buckets_for(conn, ids) -> list[list[str]]:
    seen = set()
    for pid in ids:
        r = db.get(conn, pid)
        if r and r["room_type"]:
            seen.add((market_comps.area_group(r["area"]), r["room_type"]))
    return market_comps.stale_buckets(conn, sorted(seen))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all-new", action="store_true")
    ap.add_argument("--buckets", action="store_true",
                    help="just print market (area_group, room_type) buckets needing a web lookup")
    args = ap.parse_args()
    conn = db.connect()

    ids = args.ids or (_new_ids(conn) if (args.all_new or args.buckets) else [])
    if not ids:
        print("No target listings.")
        return

    if args.buckets:
        print(json.dumps(_buckets_for(conn, ids)))
        conn.close()
        return

    n = 0
    for pid in ids:
        b = build_bundle(conn, pid)
        if b:
            n += 1
            sib = len(b["siblings"])
            dre = "dre✓" if b["dre"] else "dre-"
            mk = b["market"].get("ratio_vs_median", "?")
            print(f"  {pid}: siblings={sib} {dre} price/median={mk} "
                  f"-> data/research/{pid}.json")
    need = _buckets_for(conn, ids)
    print(f"\nWrote {n} research bundle(s).")
    if need:
        print(f"Market buckets needing a web lookup (orchestrator fills via "
              f"market_comps.py set): {json.dumps(need)}")
    conn.close()


if __name__ == "__main__":
    main()
