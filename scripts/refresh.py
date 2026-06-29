"""One-shot incremental refresh of the deterministic pipeline:

    py scripts/refresh.py                  # SCRIPTED sources (Craigslist + Zumper)
    py scripts/refresh.py --no-browser     # don't auto-launch chromerpc
    py scripts/refresh.py --teardown-chromerpc  # stop the chromerpc we launched + rm temp

Only Craigslist + Zumper are scripted. **Zillow + Apartments.com are gathered BY
HAND** by the LLM driving headful chromerpc directly (no scrapers) — see
MANUAL_SOURCES.md. (Their old scrapers were deleted: they were bot-walled + brittle,
and it is a tiny daily list not worth a script.) chromerpc is still launched here
because Zumper detail + the CL contact fetch + the manual Zillow/Apartments gather
all need it.

Runs, in order:
  0. Launch HEADFUL chromerpc on :50051 if not already up (backs Zumper detail, the
     CL contact fetch, and the manual Zillow/Apartments gather — all bot-walled
     against headless). Self-contained: if no prebuilt binary is found (CHROMERPC_BIN
     / a repo-local bin/), it git-clones chromerpc into an OS temp dir and `go build`s
     it, then launches that. The launch is recorded (data/.chromerpc_runtime.json);
     call `--teardown-chromerpc` once ALL stages are done to stop it + delete the temp
     clone. OS-independent (tempfile + git + go; needs git + a Go toolchain only
     when building from source). CHROME_BIN overrides Chrome auto-detection.
  1. Hydrate local DB from Supabase (scripts/hydrate_from_supabase.py)
  2. Craigslist incremental pull    (scripts/fetch_listings.py, SF + East Bay)
  3. Zumper incremental pull         (scripts/fetch_zumper.py)
  4. Detail + photos + hard gates    (scripts/fetch_detail.py --all-new)
  5. Prune taken-down listings       (scripts/check_links.py)
  6. Dedupe across sources           (tools/dedup.py)
  7. Stage-2 research bundles        (scripts/research.py --all-new)
  8. Publish to Supabase             (scripts/sync_supabase.py)

After this finishes, the LLM does the MANUAL Zillow + Apartments gather (MANUAL_SOURCES.md)
before vetting, so those listings join the same vet -> dedup -> sync flow.

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
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **k):  # noqa: D401 - optional dependency
        return False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable or "py"
load_dotenv(os.path.join(ROOT, ".env"))

# chromerpc, self-contained. When no prebuilt binary is found we clone+build it
# from source into an OS temp dir; this file records what we launched (pid) and
# what to delete (the temp clone) so `--teardown-chromerpc` can stop it and clean
# up after every stage has run. OS-independent (tempfile + git + go build).
CHROMERPC_REPO = "https://github.com/accretional/chromerpc"
CHROMERPC_STATE = os.path.join(ROOT, "data", ".chromerpc_runtime.json")

PRE_STEPS = [
    ("Hydrate from Supabase", ["scripts/hydrate_from_supabase.py"]),
    ("Craigslist pull (SF)", ["scripts/fetch_listings.py"]),
    ("Craigslist pull (Berkeley / East Bay)",
     ["scripts/fetch_listings.py", "--region", "eby"]),
    ("Zumper pull", ["scripts/fetch_zumper.py"]),
]
# NOTE: Zillow + Apartments.com are NOT scripted. The LLM gathers them BY HAND through
# headful chromerpc after this run finishes (see MANUAL_SOURCES.md). Their scrapers were
# deleted on purpose — bot-walled, brittle, and a tiny daily list not worth automating.
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
            # macOS
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            # Windows
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            # Linux / PATH
            shutil.which("google-chrome"), shutil.which("google-chrome-stable"),
            shutil.which("chromium"), shutil.which("chromium-browser")]
    return next((c for c in cand if c and os.path.exists(c)), None)


def _clone_and_build_chromerpc() -> tuple[str, str] | None:
    """Clone chromerpc from GitHub into a fresh OS temp dir and `go build` it.
    Returns (binary_path, temp_dir) on success, or None. OS-independent: uses
    tempfile for the dir, git for the clone, and the Go toolchain for the build
    (GOTOOLCHAIN=auto pulls the exact go.mod version if the local one is older)."""
    git = shutil.which("git")
    go = shutil.which("go") or (os.path.exists("/usr/local/go/bin/go") and "/usr/local/go/bin/go") or None
    if not git or not go:
        print("[chromerpc] cannot build from source — "
              f"{'git' if not git else 'go'} not found on PATH.", file=sys.stderr)
        return None
    tmp = tempfile.mkdtemp(prefix="househunt-chromerpc-")
    binname = "chromerpc.exe" if os.name == "nt" else "chromerpc"
    binpath = os.path.join(tmp, "bin", binname)
    try:
        print(f"[chromerpc] cloning {CHROMERPC_REPO} -> {tmp}")
        subprocess.run([git, "clone", "--depth", "1", CHROMERPC_REPO, tmp],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                       timeout=300)
        print("[chromerpc] building (go build ./cmd/chromerpc) — first build downloads modules…")
        subprocess.run([go, "build", "-o", os.path.join("bin", binname), "./cmd/chromerpc"],
                       cwd=tmp, check=True, timeout=900,
                       env={**os.environ, "GOTOOLCHAIN": "auto", "CGO_ENABLED": "0"})
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[chromerpc] clone/build failed: {e}", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    if not os.path.exists(binpath):
        print("[chromerpc] build produced no binary.", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    return binpath, tmp


def _launch_chromerpc(binp: str, port: int, tmpdir: str | None) -> bool:
    """Launch a headful chromerpc detached and record runtime state (pid + the
    temp clone to delete) so teardown_chromerpc() can stop + clean up later."""
    chrome = _find_chrome_bin()
    cmd = [binp, "-addr", f":{port}", "-headless=false"]
    if chrome:
        cmd += ["-chrome", chrome]
    print(f"[chromerpc] launching headful: {' '.join(cmd)}")
    try:
        if os.name == "nt":
            proc = subprocess.Popen(
                cmd, cwd=os.path.dirname(os.path.dirname(binp)) or ROOT,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x00000008 | 0x00000200)  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        else:
            proc = subprocess.Popen(
                cmd, cwd=ROOT, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        print(f"[chromerpc] launch failed: {e}", file=sys.stderr)
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return False
    # Record what we own so a later --teardown-chromerpc can stop it + delete the clone.
    try:
        os.makedirs(os.path.dirname(CHROMERPC_STATE), exist_ok=True)
        with open(CHROMERPC_STATE, "w") as f:
            json.dump({"pid": proc.pid, "tmpdir": tmpdir, "port": port}, f)
    except OSError as e:
        print(f"[chromerpc] could not write runtime state: {e}", file=sys.stderr)
    for _ in range(20):
        time.sleep(1)
        if _port_open("127.0.0.1", port):
            print(f"[chromerpc] up on :{port} (pid {proc.pid})")
            time.sleep(2)  # let Chrome attach
            return True
    print("[chromerpc] did not come up in time.", file=sys.stderr)
    return False


def ensure_chromerpc(port: int = 50051) -> bool:
    """Make sure a HEADFUL chromerpc is listening on :port (Zumper/Zillow/Apartments
    + CL-contact all need it; Zillow & Apartments are bot-walled against headless).

    Resolution order: (1) already up -> reuse; (2) a prebuilt binary (env
    CHROMERPC_BIN or a repo-local bin/) -> launch it; (3) otherwise clone+build
    chromerpc from source into an OS temp dir and launch that (fully self-
    contained, no prior install). What we launch is recorded so
    teardown_chromerpc() can stop it and delete the temp clone after every stage.
    Returns True if reachable."""
    if _port_open("127.0.0.1", port):
        print(f"[chromerpc] already up on :{port}")
        return True
    binp = _find_chromerpc_bin()
    if binp:
        return _launch_chromerpc(binp, port, tmpdir=None)
    print("[chromerpc] no prebuilt binary found — cloning + building from source.")
    built = _clone_and_build_chromerpc()
    if not built:
        print("[chromerpc] could not obtain a binary — Zumper detail, the CL contact "
              "fetch, and the manual Zillow/Apartments gather all need it.", file=sys.stderr)
        return False
    binpath, tmpdir = built
    return _launch_chromerpc(binpath, port, tmpdir=tmpdir)


def teardown_chromerpc() -> None:
    """Stop the chromerpc WE launched and delete the temp clone we built. Reads the
    runtime-state file written at launch. OS-independent: process-group kill on
    POSIX, taskkill /T on Windows. A no-op if we never launched one."""
    try:
        with open(CHROMERPC_STATE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        print("[chromerpc] no runtime state — nothing to tear down.")
        return
    pid, tmpdir = state.get("pid"), state.get("tmpdir")
    if pid:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                # start_new_session made the child its own process-group leader.
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            print(f"[chromerpc] stopped pid {pid}.")
        except (ProcessLookupError, PermissionError, OSError) as e:
            print(f"[chromerpc] process {pid} already gone ({e}).")
    if tmpdir and os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir, ignore_errors=True)
        print(f"[chromerpc] deleted temp clone {tmpdir}.")
    try:
        os.remove(CHROMERPC_STATE)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="Incremental refresh of the pipeline "
                                 "(Craigslist + Zumper; Zillow/Apartments are manual).")
    ap.add_argument("--no-browser", action="store_true",
                    help="don't auto-launch chromerpc (assume it's already running)")
    ap.add_argument("--teardown-chromerpc", action="store_true",
                    help="stop the chromerpc we launched + delete its temp clone, then exit. "
                         "Run this AFTER all stages finish (it's a no-op if we didn't launch one).")
    args = ap.parse_args()

    if args.teardown_chromerpc:
        teardown_chromerpc()
        return

    # Headful chromerpc backs Zumper detail, the CL contact fetch, and the LLM's
    # MANUAL Zillow/Apartments gather (all bot-walled against headless). Bring it up
    # before the pulls. When no prebuilt binary exists it is cloned + built from
    # source into an OS temp dir; tear it down at the very end with --teardown-chromerpc.
    if not args.no_browser:
        ensure_chromerpc()

    for label, a in PRE_STEPS:
        run(label, a)

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

    print(f"\n{'='*70}\nREFRESH COMPLETE (Craigslist + Zumper) — last pull {last}")
    print(f"{to_vet} new listing(s) awaiting vetting.")
    print("MANUAL STEP: now gather today's Zillow + Apartments.com listings BY HAND "
          "via headful chromerpc — NO scripts. See MANUAL_SOURCES.md. (chromerpc is "
          "up; run scripts/refresh.py --teardown-chromerpc when ALL stages are done.)")
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
