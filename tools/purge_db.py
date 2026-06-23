"""Purge listings we don't need from the local DB (then sync drops them from the
cloud/dashboard too). Each purged id is added to the blocklist so it is NEVER
re-discovered or re-vetted on later pulls (otherwise every refresh would re-pull +
re-vet all the unsafe-area posts and re-bill Apify).

POLICY (keep the DB to only what's worth seeing):
  DELETE a listing if EITHER
    - low trust  : legit_score < 40 (includes confirmed scams), or
    - unsafe area: area_tier == 'avoid' (computed from coords/area via geo.py)
  KEEP: ok-area listings with trust >= 40 (and still-unvetted ok-area rows).

Dry-run by default (prints what WOULD go); pass --execute to actually delete.

    py tools/purge_db.py            # preview counts
    py tools/purge_db.py --execute  # delete + blocklist, then run sync_supabase.py
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import db  # noqa: E402
import geo  # noqa: E402

TRUST_FLOOR = 40


def _reason(row, tier) -> str | None:
    ls = row["legit_score"]
    if ls is not None and ls < TRUST_FLOOR:
        lab = row["legit_label"] or "low-trust"
        return f"low trust ({lab} {ls} < {TRUST_FLOOR})"
    if tier == "avoid":
        return "unsafe area (avoid)"
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="actually delete (default is dry-run)")
    args = ap.parse_args()

    conn = db.connect()
    rows = conn.execute("SELECT * FROM listings").fetchall()
    victims = []
    from collections import Counter
    by_reason = Counter(); by_source = Counter()
    for r in rows:
        tier = geo.classify(r["lat"], r["lng"], r["area"], r["neighborhood"])["area_tier"]
        reason = _reason(r, tier)
        if reason:
            victims.append((r["id"], r["source"], reason))
            by_reason["low trust (<40)" if reason.startswith("low") else "unsafe area"] += 1
            by_source[r["source"]] += 1

    total = len(rows)
    print(f"DB has {total} listings; {len(victims)} match the purge policy "
          f"(keep {total - len(victims)}).")
    print("  by reason:", dict(by_reason))
    print("  by source:", dict(by_source))

    if not args.execute:
        print("\nDRY RUN — nothing deleted. Re-run with --execute to delete + blocklist.")
        conn.close()
        return

    research_dir = db.RESEARCH_DIR
    for pid, source, reason in victims:
        db.block(conn, pid, source or "", reason)
        conn.execute("DELETE FROM listings WHERE id = ?", (pid,))
        bundle = os.path.join(research_dir, f"{pid}.json")
        if os.path.exists(bundle):
            os.remove(bundle)
    conn.commit()
    kept = conn.execute("SELECT count(*) c FROM listings").fetchone()["c"]
    blocked = conn.execute("SELECT count(*) c FROM blocklist").fetchone()["c"]
    conn.close()
    print(f"\nDeleted {len(victims)} listing(s); {kept} remain. Blocklist now {blocked}.")
    print("Next: run  py scripts/sync_supabase.py  to drop them from the cloud/dashboard.")


if __name__ == "__main__":
    main()
