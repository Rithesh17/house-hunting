"""One-shot incremental refresh of the deterministic pipeline:

    py scripts/refresh.py                 # Craigslist + Zumper always; Zillow once/day
    py scripts/refresh.py --no-zillow     # never run Zillow this run
    py scripts/refresh.py --force-zillow  # run Zillow even if already pulled today

Runs, in order:
  0. Hydrate local DB from Supabase (scripts/hydrate_from_supabase.py)
  1. Craigslist incremental pull   (scripts/fetch_listings.py)
  2. Zumper incremental pull        (scripts/fetch_zumper.py)
  2b. Zillow pull via Apify         (scripts/fetch_zillow.py; needs APIFY_TOKEN —
      skipped gracefully if unset) — ONCE PER CALENDAR DAY ONLY. Zillow is a
      pay-per-event API, so we cap it to the first refresh of each local day (the
      9am cron does the full all-sources run; ad-hoc refreshes later in the day do
      Craigslist + Zumper only and skip Zillow). See THUMB_RULES.md.
  3. Detail + photos + hard gates   (scripts/fetch_detail.py --all-new)
  4. Prune taken-down listings      (scripts/check_links.py)
  5. Dedupe across sources          (tools/dedup.py)
  6. Stage-2 research bundles       (scripts/research.py --all-new)
  7. Publish to Supabase            (scripts/sync_supabase.py)

Step 6 fetches the free external facts (DRE / ownership / duplicate siblings /
cached market range) into data/research/<id>.json for the vetting subagents to
cross-check. Market buckets with no cached range are printed so the orchestrator
can fill them (one web lookup per area-group via market_comps.py set).

Step 0 rebuilds local state from the cloud read-model so a fresh checkout (or a
machine that lost the gitignored `data/listings.db`) resumes WITHOUT re-vetting
everything or letting step 7 delete cloud rows that are merely missing locally.
It is INSERT OR IGNORE, so on a healthy local DB it is a quick no-op.

Step 7 mirrors the current local DB up to the cloud so the public dashboard
reflects prunes/dedup right away; re-run it after vetting so new scores publish.
It then prints how many NEW listings need vetting. Vetting itself needs Claude's
vision (subagents), so it is NOT done here — see the "Refresh" section of
CLAUDE.md for the full cycle (vet -> apply_verdicts -> dedup -> notify -> purge).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable or "py"

PRE_STEPS = [
    ("Hydrate from Supabase", ["scripts/hydrate_from_supabase.py"]),
    ("Craigslist pull (SF)", ["scripts/fetch_listings.py"]),
    ("Craigslist pull (Berkeley / East Bay)",
     ["scripts/fetch_listings.py", "--region", "eby"]),
    ("Zumper pull", ["scripts/fetch_zumper.py"]),
]
ZILLOW_STEP = ("Zillow pull (Apify)", ["scripts/fetch_zillow.py"])
POST_STEPS = [
    ("Detail + photos + gates", ["scripts/fetch_detail.py", "--all-new"]),
    ("Prune dead links", ["scripts/check_links.py"]),
    ("Dedupe across sources", ["tools/dedup.py"]),
    ("Research bundles (DRE/owner/dups/market)", ["scripts/research.py", "--all-new"]),
    ("Publish to Supabase", ["scripts/sync_supabase.py"]),
]


def run(label, args):
    print(f"\n{'='*70}\n>> {label}\n{'='*70}")
    r = subprocess.run([PY, *args], cwd=ROOT)
    if r.returncode != 0:
        print(f"  ! step '{label}' exited {r.returncode}", file=sys.stderr)


def _zillow_pulled_today(db, conn) -> bool:
    """True if last_pull_zillow falls on today's LOCAL calendar date."""
    last = db.get_meta(conn, "last_pull_zillow")
    if not last:
        return False
    try:
        return datetime.fromisoformat(last).astimezone().date() == datetime.now().astimezone().date()
    except (ValueError, TypeError):
        return False


def main():
    ap = argparse.ArgumentParser(description="Incremental refresh of the pipeline.")
    ap.add_argument("--no-zillow", action="store_true",
                    help="skip Zillow entirely this run")
    ap.add_argument("--force-zillow", action="store_true",
                    help="run Zillow even if it was already pulled today")
    args = ap.parse_args()

    for label, a in PRE_STEPS:
        run(label, a)

    # Zillow: pay-per-event API -> at most once per local calendar day.
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import db  # noqa: E402
    conn = db.connect()
    pulled_today = _zillow_pulled_today(db, conn)
    conn.close()
    if args.no_zillow:
        print(f"\n{'='*70}\n>> Zillow pull (Apify) — SKIPPED (--no-zillow)\n{'='*70}")
    elif pulled_today and not args.force_zillow:
        print(f"\n{'='*70}\n>> Zillow pull (Apify) — SKIPPED (already pulled today; "
              f"once/day cap to respect Apify pricing — use --force-zillow to override)\n{'='*70}")
    else:
        run(*ZILLOW_STEP)

    for label, a in POST_STEPS:
        run(label, a)

    conn = db.connect()
    to_vet = conn.execute(
        "SELECT count(*) c FROM listings WHERE status='new' AND detail_fetched_at IS NOT NULL"
    ).fetchone()["c"]
    last = db.get_meta(conn, "last_pull")
    conn.close()

    print(f"\n{'='*70}\nREFRESH COMPLETE — last pull {last}")
    print(f"{to_vet} new listing(s) awaiting vetting.")
    if to_vet:
        print("Next (Claude): (1) fill any market buckets research.py flagged "
              "(market_comps.py set …); (2) re-run research.py --all-new so ranges "
              "attach; (3) vet via subagents reading post + photos + "
              "data/research/<id>.json (Stage 1 + Stage 2) -> py tools/apply_verdicts.py "
              "-> py tools/dedup.py -> py tools/purge_db.py --execute "
              "-> py scripts/sync_supabase.py "
              "-> py scripts/notify.py --new -> py tools/purge_images.py --all")
    else:
        print("Nothing new to vet. Cloud + dashboard are up to date.")


if __name__ == "__main__":
    main()
