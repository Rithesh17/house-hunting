"""One-shot incremental refresh of the deterministic pipeline:

    py scripts/refresh.py                 # all sources (CL + Zumper + Zillow + Apartments)
    py scripts/refresh.py --no-zillow     # skip the Zillow pull this run
    py scripts/refresh.py --no-apartments # skip the Apartments.com pull
    py scripts/refresh.py --no-browser    # don't auto-launch chromerpc

Runs, in order:
  0. Launch HEADFUL chromerpc on :50051 if not already up (backs Zumper + Zillow +
     Apartments, which are bot-walled against headless, and the CL contact fetch).
     Set CHROMERPC_BIN / CHROME_BIN in .env to point at the binaries.
  1. Hydrate local DB from Supabase (scripts/hydrate_from_supabase.py)
  2. Craigslist incremental pull    (scripts/fetch_listings.py, SF + East Bay)
  3. Zumper incremental pull         (scripts/fetch_zumper.py)
  4. Zillow pull via headful chromerpc (scripts/fetch_zillow_cr.py) — FREE, every
     run. Replaced the paid Apify actor (fetch_zillow.py kept as a manual fallback).
  5. Apartments.com pull via headful chromerpc (scripts/fetch_apartments_cr.py)
  6. Detail + photos + hard gates    (scripts/fetch_detail.py --all-new)
  7. Prune taken-down listings       (scripts/check_links.py)
  8. Dedupe across sources           (tools/dedup.py)
  9. Stage-2 research bundles        (scripts/research.py --all-new)
 10. Publish to Supabase             (scripts/sync_supabase.py)

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
import shutil
import socket
import subprocess
import sys
import time

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **k):  # noqa: D401 - optional dependency
        return False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable or "py"
load_dotenv(os.path.join(ROOT, ".env"))

PRE_STEPS = [
    ("Hydrate from Supabase", ["scripts/hydrate_from_supabase.py"]),
    ("Craigslist pull (SF)", ["scripts/fetch_listings.py"]),
    ("Craigslist pull (Berkeley / East Bay)",
     ["scripts/fetch_listings.py", "--region", "eby"]),
    ("Zumper pull", ["scripts/fetch_zumper.py"]),
]
# Browser-backed (headful chromerpc) source pulls — free, run every refresh. These
# replaced the paid Apify Zillow actor; fetch_zillow.py (Apify) is kept only as a
# manual fallback and is no longer in the default flow.
ZILLOW_STEP = ("Zillow pull (headful chromerpc)", ["scripts/fetch_zillow_cr.py"])
APARTMENTS_STEP = ("Apartments.com pull (headful chromerpc)", ["scripts/fetch_apartments_cr.py"])
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


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_chromerpc_bin() -> str | None:
    cand = [os.getenv("CHROMERPC_BIN"),
            os.path.join(ROOT, "chromerpc", "bin", "chromerpc.exe"),
            os.path.join(ROOT, "chromerpc", "bin", "chromerpc"),
            os.path.join(os.getenv("LOCALAPPDATA", ""), "Temp", "househunt-chromerpc",
                         "bin", "chromerpc.exe")]
    return next((c for c in cand if c and os.path.exists(c)), None)


def _find_chrome_bin() -> str | None:
    cand = [os.getenv("CHROME_BIN"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            shutil.which("google-chrome"), shutil.which("chromium")]
    return next((c for c in cand if c and os.path.exists(c)), None)


def ensure_chromerpc(port: int = 50051) -> bool:
    """Make sure a HEADFUL chromerpc is listening on :port (Zumper/Zillow/Apartments
    + CL-contact all need it; Zillow & Apartments are bot-walled against headless).
    Launches it if a binary is found (env CHROMERPC_BIN / CHROME_BIN override the
    auto-detected paths). Returns True if reachable."""
    if _port_open("127.0.0.1", port):
        print(f"[chromerpc] already up on :{port}")
        return True
    binp = _find_chromerpc_bin()
    if not binp:
        print("[chromerpc] not running and binary not found — set CHROMERPC_BIN in "
              ".env to auto-launch (Zillow/Apartments will be skipped).", file=sys.stderr)
        return False
    chrome = _find_chrome_bin()
    cmd = [binp, "-addr", f":{port}", "-headless=false"]
    if chrome:
        cmd += ["-chrome", chrome]
    print(f"[chromerpc] launching headful: {' '.join(cmd)}")
    try:
        create_flags = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
        subprocess.Popen(cmd, cwd=os.path.dirname(os.path.dirname(binp)) or ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=create_flags) if os.name == "nt" else \
            subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        print(f"[chromerpc] launch failed: {e}", file=sys.stderr)
        return False
    for _ in range(20):
        time.sleep(1)
        if _port_open("127.0.0.1", port):
            print(f"[chromerpc] up on :{port}")
            time.sleep(2)  # let Chrome attach
            return True
    print("[chromerpc] did not come up in time.", file=sys.stderr)
    return False


def main():
    ap = argparse.ArgumentParser(description="Incremental refresh of the pipeline.")
    ap.add_argument("--no-zillow", action="store_true", help="skip the Zillow pull")
    ap.add_argument("--no-apartments", action="store_true", help="skip the Apartments.com pull")
    ap.add_argument("--no-browser", action="store_true",
                    help="don't auto-launch chromerpc (assume it's already running, or skip "
                         "browser-backed sources)")
    ap.add_argument("--force-zillow", action="store_true",
                    help="(deprecated no-op: Zillow is now free via chromerpc, runs every refresh)")
    args = ap.parse_args()

    # Headful chromerpc backs Zumper + Zillow + Apartments (all bot-walled against
    # headless) and the CL contact fetch. Bring it up before the source pulls.
    if not args.no_browser:
        ensure_chromerpc()

    for label, a in PRE_STEPS:
        run(label, a)

    if args.no_zillow:
        print(f"\n{'='*70}\n>> Zillow pull — SKIPPED (--no-zillow)\n{'='*70}")
    else:
        run(*ZILLOW_STEP)

    if args.no_apartments:
        print(f"\n{'='*70}\n>> Apartments.com pull — SKIPPED (--no-apartments)\n{'='*70}")
    else:
        run(*APARTMENTS_STEP)

    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import db  # noqa: E402
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
