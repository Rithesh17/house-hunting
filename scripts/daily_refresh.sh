#!/bin/bash
# Daily full house-hunting refresh via HEADLESS Claude Code (launchd-invoked at 9am).
#
# Runs the WHOLE pipeline unattended — fetch (Zillow once/day, gated in refresh.py)
# + subagent vetting + dedup/purge/sync + Telegram — so Claude runs with
# --dangerously-skip-permissions (no interactive prompts). See THUMB_RULES.md.
#
# Installed as launchd agent: ~/Library/LaunchAgents/com.rithesh.househunt.refresh.plist
# Logs: ~/Library/Logs/house-hunting-refresh.log
# launchd gives a minimal env, so we set PATH explicitly (node->claude, brew->python3).

export PATH="/Users/rithesh/.nvm/versions/node/v24.11.0/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PROJECT="/Volumes/wd_office_3/Personal/house-hunting"
LOG="$HOME/Library/Logs/house-hunting-refresh.log"
mkdir -p "$(dirname "$LOG")"

# The project lives on an external volume — bail cleanly if it isn't mounted.
if ! cd "$PROJECT" 2>/dev/null; then
  echo "$(date '+%F %T') ERROR: project dir not mounted ($PROJECT) — skipping run" >> "$LOG"
  exit 0
fi
if ! command -v claude >/dev/null 2>&1; then
  echo "$(date '+%F %T') ERROR: claude CLI not on PATH — skipping run" >> "$LOG"
  exit 0
fi

echo "===== $(date '+%F %T') daily refresh START =====" >> "$LOG"

PROMPT="You are running UNATTENDED in headless mode — there is NO interactive loop to resume into. Work SYNCHRONOUSLY in this single session: run every shell command in the FOREGROUND and WAIT for it to finish before the next step. Do NOT run anything in the background, do NOT use run_in_background, and NEVER defer work with 'I'll resume when notified' — if you exit before the sequence is complete, the rest never runs. scripts/refresh.py blocks for ~10-15 minutes (it shells out to all the fetch/detail/research steps) — that is EXPECTED; just wait for it to return. Spawning vetting subagents via the Agent tool is fine — you wait for their results in-line, which is synchronous. Do not end your turn until step (4) through purge_images is fully done.

Run the full daily house-hunting refresh exactly per CLAUDE.md's Refresh section and THUMB_RULES.md, end to end: (1) python3 scripts/refresh.py (foreground, all sources including Zillow as the day's first run; wait for it to finish); (2) fill any market buckets it flags via WebSearch, then python3 scripts/market_comps.py set ..., and re-run python3 scripts/research.py --all-new; (3) vet EVERY new listing (status='new' with detail_fetched_at set) with parallel general-purpose subagents, each reading the post (python3 scripts/db.py show <id>), every photo in data/images/<id>/, and data/research/<id>.json under the two-stage rubric, writing verdicts to data/_verdicts_*.json; (4) python3 tools/apply_verdicts.py, then python3 tools/dedup.py, python3 tools/purge_db.py --execute, python3 scripts/sync_supabase.py, python3 scripts/notify.py --new, python3 tools/purge_images.py --all. Respect THUMB_RULES.md: Zillow once per day only, store only remote image URLs, stay on free tiers. If refresh reports 0 new, stop after sync. Finish with a short summary of standouts."

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
