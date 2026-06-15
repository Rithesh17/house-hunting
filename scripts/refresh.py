"""One-shot incremental refresh of the deterministic pipeline:

    py scripts/refresh.py

Runs, in order:
  1. Craigslist incremental pull   (scripts/fetch_listings.py)
  2. Zumper incremental pull        (scripts/fetch_zumper.py)
  3. Detail + photos + hard gates   (scripts/fetch_detail.py --all-new)
  4. Prune taken-down listings      (scripts/check_links.py)
  5. Dedupe across sources          (tools/dedup.py)
  6. Publish to Supabase            (scripts/sync_supabase.py)

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
    ("Craigslist pull", ["scripts/fetch_listings.py"]),
    ("Zumper pull", ["scripts/fetch_zumper.py"]),
    ("Detail + photos + gates", ["scripts/fetch_detail.py", "--all-new"]),
    ("Prune dead links", ["scripts/check_links.py"]),
    ("Dedupe across sources", ["tools/dedup.py"]),
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
        print("Next (Claude): vet them via subagents -> py tools/apply_verdicts.py "
              "-> py tools/dedup.py -> py scripts/sync_supabase.py "
              "-> py scripts/notify.py --all-qualifying -> py tools/purge_images.py --all")
    else:
        print("Nothing new to vet. Cloud + dashboard are up to date.")


if __name__ == "__main__":
    main()
