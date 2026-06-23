"""One-shot incremental refresh of the deterministic pipeline:

    py scripts/refresh.py

Runs, in order:
  0. Hydrate local DB from Supabase (scripts/hydrate_from_supabase.py)
  1. Craigslist incremental pull   (scripts/fetch_listings.py)
  2. Zumper incremental pull        (scripts/fetch_zumper.py)
  2b. Zillow pull via Apify         (scripts/fetch_zillow.py; needs APIFY_TOKEN —
      skipped gracefully if unset; bills ~40 Apify results/run, ~2,500/mo free)
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
everything or letting step 6 delete cloud rows that are merely missing locally.
It is INSERT OR IGNORE, so on a healthy local DB it is a quick no-op.

Step 6 mirrors the current local DB up to the cloud so the public dashboard
reflects prunes/dedup right away; re-run it after vetting so new scores publish.
It then prints how many NEW listings need vetting. Vetting itself needs Claude's
vision (subagents), so it is NOT done here — see the "Refresh" section of
CLAUDE.md for the full cycle (vet -> apply_verdicts -> dedup -> notify -> purge).
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable or "py"

STEPS = [
    ("Hydrate from Supabase", ["scripts/hydrate_from_supabase.py"]),
    ("Craigslist pull", ["scripts/fetch_listings.py"]),
    ("Zumper pull", ["scripts/fetch_zumper.py"]),
    ("Zillow pull (Apify)", ["scripts/fetch_zillow.py"]),
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


def main():
    for label, args in STEPS:
        run(label, args)

    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import db  # noqa: E402
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
              "-> py tools/dedup.py -> py scripts/sync_supabase.py "
              "-> py scripts/notify.py --new -> py tools/purge_images.py --all")
    else:
        print("Nothing new to vet. Cloud + dashboard are up to date.")


if __name__ == "__main__":
    main()
