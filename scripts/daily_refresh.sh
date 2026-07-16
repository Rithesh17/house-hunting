#!/bin/bash
# Full house-hunting refresh + outreach via HEADLESS Claude Code (launchd, every 4h).
#
# Runs the WHOLE pipeline unattended — Stage 0 read+vet replies, Stage 1 fetch
# (Zillow once/day, gated in refresh.py) + subagent vet+enrich + dedup/purge/sync,
# Stage 2 contact+send (opt-in via OUTREACH_AUTOSEND=1), then ONE combined Telegram
# digest — so Claude runs with --dangerously-skip-permissions (no interactive
# prompts). See THUMB_RULES.md + OUTREACH.md.
#
# Self-contained: clones + builds + starts chromerpc (headless) into a temp dir for
# Stage 2's Craigslist contact-fetch, and kills the server + deletes the clone on
# exit — no manually-run server dependency. Needs go + git + system Google Chrome;
# if any are missing or the clone/build fails, Stage 2 just skips (pipeline degrades
# gracefully). An already-running :50051 server is reused and left untouched.
#
# Installed as launchd agent: ~/Library/LaunchAgents/com.rithesh.househunt.refresh.plist
# Logs: ~/Library/Logs/house-hunting-refresh.log
# launchd gives a minimal env, so we set PATH explicitly (node->claude, brew->python3,
# /usr/local/go->go for the ephemeral chromerpc build).
export PATH="/Users/rithesh/.nvm/versions/node/v24.11.0/bin:/usr/local/go/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PROJECT="/Volumes/wd_office_3/Personal/house-hunting"
LOG="$HOME/Library/Logs/house-hunting-refresh.log"
mkdir -p "$(dirname "$LOG")"
ts() { date '+%F %T'; }

# The project lives on an external volume — bail cleanly if it isn't mounted.
if ! cd "$PROJECT" 2>/dev/null; then
  echo "$(ts) ERROR: project dir not mounted ($PROJECT) — skipping run" >> "$LOG"
  exit 0
fi
if ! command -v claude >/dev/null 2>&1; then
  echo "$(ts) ERROR: claude CLI not on PATH — skipping run" >> "$LOG"
  exit 0
fi

# ---------------------------------------------------------------------------
# Ephemeral chromerpc: clone -> build -> run headless for THIS run only, then
# kill + delete on exit. No long-lived/manually-started server to depend on.
# Best-effort: any failure just leaves Stage 2 to skip (the pipeline handles a
# missing chromerpc). If a server is ALREADY up on :50051, reuse it and don't
# touch it (so a manual session is never clobbered/killed).
CHROMERPC_REPO="https://github.com/accretional/chromerpc.git"
CRPC_PORT=50051
CRPC_DIR=""   # temp clone (empty => nothing of ours to remove)
CRPC_PID=""   # our server pid (empty => not started by us / reused / failed)

cleanup_chromerpc() {
  if [ -n "$CRPC_PID" ]; then
    echo "$(ts) stopping chromerpc (pid $CRPC_PID)" >> "$LOG"
    kill -TERM "$CRPC_PID" 2>/dev/null
    for _ in $(seq 1 10); do kill -0 "$CRPC_PID" 2>/dev/null || break; sleep 1; done
    kill -KILL "$CRPC_PID" 2>/dev/null
    [ -n "$CRPC_DIR" ] && pkill -f "$CRPC_DIR/chromerpc/bin/chromerpc" 2>/dev/null
  fi
  if [ -n "$CRPC_DIR" ] && [ -d "$CRPC_DIR" ]; then
    rm -rf "$CRPC_DIR" && echo "$(ts) removed temp chromerpc clone" >> "$LOG"
  fi
}
trap cleanup_chromerpc EXIT
trap 'exit 143' TERM INT   # a launchd stop -> run the EXIT trap -> cleanup

start_chromerpc() {
  if nc -z -w2 localhost "$CRPC_PORT" 2>/dev/null; then
    echo "$(ts) chromerpc already up on :$CRPC_PORT — reusing (will not manage it)" >> "$LOG"
    return 0
  fi
  command -v go  >/dev/null 2>&1 || { echo "$(ts) go missing — Stage 2 will skip" >> "$LOG"; return 1; }
  command -v git >/dev/null 2>&1 || { echo "$(ts) git missing — Stage 2 will skip" >> "$LOG"; return 1; }
  CRPC_DIR="$(mktemp -d "${TMPDIR:-/tmp}/househunt-chromerpc.XXXXXX")" || { CRPC_DIR=""; return 1; }
  echo "$(ts) cloning chromerpc -> $CRPC_DIR" >> "$LOG"
  # Run git from the temp dir (internal disk), NOT the project on /Volumes: under
  # launchd's sandbox, git's getcwd() on the external volume is denied ("Operation
  # not permitted"), which aborted the clone. cd'ing off /Volumes avoids that.
  if ! ( cd "$CRPC_DIR" && git clone --depth 1 "$CHROMERPC_REPO" chromerpc ) >> "$LOG" 2>&1; then
    echo "$(ts) chromerpc clone failed — Stage 2 will skip" >> "$LOG"; return 1
  fi
  echo "$(ts) building chromerpc (go build)..." >> "$LOG"
  if ! ( cd "$CRPC_DIR/chromerpc" && go build -o bin/chromerpc ./cmd/chromerpc ) >> "$LOG" 2>&1; then
    echo "$(ts) chromerpc build failed — Stage 2 will skip" >> "$LOG"; return 1
  fi
  # HEADFUL (--headless=false): Craigslist withholds the reply token (__SERVICE_ID__
  # stays unresolved) for headless Chrome, so contact-fetch only works headful. A
  # launchd LaunchAgent runs in the logged-in GUI session, so a real window can open.
  echo "$(ts) starting chromerpc --headless=false --addr :$CRPC_PORT" >> "$LOG"
  ( cd "$CRPC_DIR/chromerpc" && exec ./bin/chromerpc --headless=false --addr ":$CRPC_PORT" ) >> "$LOG" 2>&1 &
  CRPC_PID=$!
  for _ in $(seq 1 45); do  # server launches Chrome before it binds the port
    if nc -z -w1 localhost "$CRPC_PORT" 2>/dev/null; then
      echo "$(ts) chromerpc ready (pid $CRPC_PID)" >> "$LOG"; return 0
    fi
    if ! kill -0 "$CRPC_PID" 2>/dev/null; then
      echo "$(ts) chromerpc exited during startup — Stage 2 will skip" >> "$LOG"; CRPC_PID=""; return 1
    fi
    sleep 1
  done
  echo "$(ts) chromerpc not ready after 45s — Stage 2 will skip" >> "$LOG"; return 1
}

echo "===== $(ts) daily refresh START =====" >> "$LOG"
start_chromerpc || echo "$(ts) continuing WITHOUT chromerpc (Stage 2 contact-fetch skipped)" >> "$LOG"

# ---------------------------------------------------------------------------
# STAGE FETCH — run the deterministic pipeline in BASH, NOT via claude. The
# headless claude kept backgrounding this ~10-15 min blocker and deferring with
# "I'll resume when notified", so its -p turn ended before vetting ever ran.
# Running it here guarantees it COMPLETES before the LLM judgment steps start.
echo "$(ts) running refresh.py (fetch/detail/research/dedup/sync)..." >> "$LOG"
python3 scripts/refresh.py >> "$LOG" 2>&1 &
RPID=$!
( sleep 1800
  kill -0 "$RPID" 2>/dev/null && { echo "$(ts) WATCHDOG: refresh.py exceeded 1800s — killing" >> "$LOG"
    kill -TERM "$RPID" 2>/dev/null; sleep 20; kill -KILL "$RPID" 2>/dev/null; } ) &
RWD=$!
wait "$RPID"; rcode=$?
kill "$RWD" 2>/dev/null
echo "$(ts) refresh.py done (exit $rcode)" >> "$LOG"

PROMPT="You are running UNATTENDED in headless mode. The deterministic FETCH (scripts/refresh.py) has ALREADY RUN in the wrapper — new listings are in the DB as status='new' with detail + photos + research bundles, and a sync already happened. Your job is the LLM JUDGMENT steps only. Work SYNCHRONOUSLY: do NOT run anything in the background, do NOT use run_in_background, and NEVER defer with 'I'll resume when notified' — if you end your turn early the rest never runs. Spawning Agent subagents is fine (you wait for their results inline). Do not end your turn until the ONE final Telegram digest is sent and images are purged. Do these in order (0 -> 1 -> 2 -> digest):

STAGE 0 (replies): python3 scripts/read_replies.py. For each item in the JSON 'replies', match it to one of 'contacted_listings' (relay address / quoted subject / address — semantic) and judge it: GOOD = available and agrees to an in-person viewing -> python3 scripts/db.py set-status <id> interested; BAD = wants money/deposit/fee before viewing, 'can't show in person', or pushes off-platform -> leave it, flag in the digest. Keep a one-line verdict per reply for the digest.

STAGE 1 (vet + enrich): (a) If the refresh.py log above flagged market buckets needing a lookup, do ONE WebSearch per (area,room_type) + python3 scripts/market_comps.py set ..., then python3 scripts/research.py --all-new. (b) Vet EVERY status='new' listing that has detail_fetched_at + photos, with parallel general-purpose subagents — batch the ids (~5-6 per subagent; use py tools/batches.py or query the ids), each subagent reads python3 scripts/db.py show <id>, EVERY photo in data/images/<id>/, and data/research/<id>.json under the two-stage rubric (SHARED-ROOM GATE first!), returning a verdict with an 'enrich' block (address from body, MONTHLY price, correct room_type/bed-bath, clean title) written to data/_verdicts_*.json. NOTE: a broken earlier run may have left a LARGE backlog of status='new' CL rows — vet them ALL in parallel batches; most will be rooms/scams (reject accordingly). (c) python3 tools/apply_verdicts.py, then python3 tools/dedup.py, python3 tools/purge_db.py --execute. (d) CONTACT REVEAL FOR ALL KEPT CL — BY HAND, NO SCRIPT (BROWSER = MANUAL, ALWAYS): reveal the reply relay (+ phone, + any in-body 'click for contact') for EVERY surviving vetted Craigslist row so the dashboard shows contact for all of them, not just the ones we email. There is NO batch script (the old fetch_cl_contacts.py --all-vetted loop was removed — it got the IP throttled). Do each ONE BY HAND: import fetch_cl_contacts as F; per listing navigate -> warmup -> screenshot -> _center(_qs('button.reply-button')) -> human_click -> screenshot -> _read_reply_panel (and reveal_in_body if body-gated), then db.update_detail the reply_email/phone/contact_name. ONE pass per listing, spaced, prioritise SF; if CL throttles (F._reply_uninit()) stop and let the rest retry next run (needs headful chromerpc on :50051). (e) python3 scripts/sync_supabase.py.

STAGE 2 (DECIDE who to email + send; Craigslist only, 1BR/1BA ONLY): the relays were ALREADY captured in Stage 1d, so DO NOT fetch contacts here — just pick who to email. dedup has already folded any repost of a previously-contacted unit into its cluster, so consider only primaries whose UNIT is not yet contacted. For each NEW qualifying CL pick this run (non-scam, area_tier ok, fit>=80, legit>=70, and room_type=='1br' — a real 1 bed/1 bath unit; STUDIOS and 2+ BED stay on the dashboard at full score but are NEVER auto-emailed, so skip them here): if its reply_email is somehow still missing, reveal it BY HAND (the Stage 1d manual chromerpc steps for that one id) — NEVER a batch script; otherwise read sensitive/email_body.json, hand-author a plain human email per its style_rules (subject 'Interested in this: <address>'), and send with: python3 scripts/send_email.py --auto --listing <id> --subject ... --body-file /tmp/<id>.txt. The --auto flag makes the script send ONLY if OUTREACH_AUTOSEND=1 is set in .env AND the unit is room_type=='1br' (else it refuses and prints why — that is fine, just note the pick as 'ready to email' or 'studio/2+bed, manual only'); the script ALSO refuses any unit already contacted (same dedup cluster / address / phone), so a relist can never be double-emailed. On a real send it flips status to contacted. python3 scripts/sync_supabase.py after. Cap 2 sends per run. Scams are NEVER emailed.

FINAL — exactly ONE combined Telegram digest, no separate pings: python3 scripts/notify.py --list-new gives the qualifying new picks as JSON. Compose ONE short shorthand message with up to three sections — new picks (name, \$price/type, area, trust, fit, #id link), emails (sent or ready this run), replies (Stage-0 verdicts) — skipping any empty section and ending with the dashboard link. Send it once: python3 scripts/notify.py --message '<text>'. If ALL three sections are empty, send nothing. After it sends, python3 scripts/notify.py --mark-notified <ids...> for the new picks you included. Finally python3 scripts/purge_images.py --all. Respect THUMB_RULES.md (Zillow once/day, only remote image URLs, free tiers). End with a short summary of standouts."

# `claude -p` is non-interactive: it runs to completion and exits on its own. The
# watchdog is a safety net — if the headless run ever HANGS, hard-kill it (and its
# process group) so no Claude instance is ever left lingering after this job.
TIMEOUT=3600  # 1 hour hard cap
claude -p "$PROMPT" --dangerously-skip-permissions >> "$LOG" 2>&1 &
CLAUDE_PID=$!
( sleep "$TIMEOUT"
  if kill -0 "$CLAUDE_PID" 2>/dev/null; then
    echo "$(date '+%F %T') WATCHDOG: run exceeded ${TIMEOUT}s — terminating" >> "$LOG"
    kill -TERM "$CLAUDE_PID" 2>/dev/null; sleep 30; kill -KILL "$CLAUDE_PID" 2>/dev/null
  fi ) &
WATCHDOG_PID=$!
wait "$CLAUDE_PID"; code=$?
kill "$WATCHDOG_PID" 2>/dev/null  # claude finished first -> cancel the watchdog
# belt-and-suspenders: reap any stray headless claude from THIS run if still alive
kill -0 "$CLAUDE_PID" 2>/dev/null && kill -KILL "$CLAUDE_PID" 2>/dev/null
echo "===== $(date '+%F %T') daily refresh END (exit $code) =====" >> "$LOG"
