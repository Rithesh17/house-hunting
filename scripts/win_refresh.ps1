# Full house-hunting refresh + outreach via HEADLESS Claude Code — Windows Task
# Scheduler, EVERY 6 HOURS. Windows equivalent of the Mac launchd daily_refresh.sh.
#
# Runs the deterministic FETCH (scripts/refresh.py — incremental from last_pull, so
# a 6-hourly run only pulls what's NEW since the previous run), then hands the LLM +
# by-hand-browser judgment stages to a headless `claude` invocation. Per the project's
# "BROWSER = MANUAL, ALWAYS" rule, ALL browser interaction (Zillow / Apartments /
# Zumper detail / CL contact reveal / CL scam flagging) is driven BY HAND by that
# agent, one chromerpc action at a time — there is no scraper/batch loop.
#
# MUST run in an INTERACTIVE (logged-on) session: the bot-walled sites need HEADFUL
# chromerpc, which needs a desktop. The scheduled task is registered with
# "run only when user is logged on". If the machine is locked/off at fire time, that
# run is skipped and the next 6-hourly run picks up the backlog (pipeline is
# incremental + idempotent).
#
# Auto-send is SF-only + Craigslist 1BR/1BA only, gated by OUTREACH_AUTOSEND=1 in
# .env and enforced in send_email.py --auto (Berkeley / studios / 2+bed / Zillow /
# Zumper / Apartments are NEVER auto-emailed — surfaced on the dashboard only).

$ErrorActionPreference = 'Continue'
$PROJECT = 'C:\Users\rithh\Documents\Personal\house-hunting'
$LOG     = Join-Path $PROJECT 'data\refresh_cron.log'
$env:PATH = "$env:APPDATA\npm;$env:PATH"   # claude shim lives here; py launcher is global
Set-Location $PROJECT
function ts { (Get-Date).ToString('yyyy-MM-dd HH:mm:ss') }
"$(ts)  ===== 6-hourly refresh START =====" | Out-File -Append -Encoding utf8 $LOG

# 1) Deterministic fetch (auto-launches headful chromerpc on :50051, leaves it up).
"$(ts)  running scripts/refresh.py ..." | Out-File -Append -Encoding utf8 $LOG
py scripts/refresh.py *>> $LOG

# 2) LLM + by-hand-browser stages via headless Claude Code.
$PROMPT = @'
You are running UNATTENDED (headless) for a 6-hourly house-hunting refresh. scripts/refresh.py has ALREADY RUN in the wrapper: Craigslist + Zumper map-API pulls are done (Zumper rows are STUBS with description=None), detail/photos/objective-gates/dedup/research-bundles are done, an initial sync happened, and headful chromerpc is UP on :50051. Do the LLM + BY-HAND-BROWSER stages, SYNCHRONOUSLY: do NOT run anything in the background, do NOT use run_in_background, and NEVER defer with "I'll resume when notified" — if you end your turn early the rest never runs (spawning Agent subagents for VETTING is fine — you wait for them inline). Obey CLAUDE.md, especially "BROWSER = MANUAL, ALWAYS" (EVERY browser interaction is by hand, ONE chromerpc action at a time, screenshot every step and on any blocker, NO scripts/loops, NO DOM/JS interaction — DOM read-only to locate/extract), plus MANUAL_SOURCES.md, OUTREACH.md, THUMB_RULES.md. Do NOT end your turn until the ONE final Telegram digest is sent, images purged, and chromerpc torn down. In order:

STAGE 0 (replies): py scripts/read_replies.py. Match each item in 'replies' to a 'contacted_listings' entry and judge: GOOD = available and agrees to an in-person viewing -> py scripts/db.py set-status <id> interested; BAD = wants money/deposit/fee before a viewing, "can't show in person", or pushes off-platform -> leave it, flag in the digest. One-line verdict per reply for the digest.

STAGE 1 — MANUAL GATHER (BY HAND, BROWSER = MANUAL, ALWAYS). Read only what is NEW since the last run: sort newest-first and STOP once you pass the previous run's time (~6h ago). 1a) Zillow (SF + Berkeley) and Apartments.com (SF + Berkeley): open each site yourself, set price cap $2000, sort newest, walk the results, open each recent post slowly (screenshot + DOM-read), vet inline with the rubric, and insert keeps to the DB by hand (source='zillow'/'apartments', real coords). 1b) Zumper detail: for EVERY new source='zumper' row with description IS NULL, drive chromerpc by hand to its detail page, scroll-load the ABOUT body + posted age + contact, and db.update_detail it — the body is required for the SHARED-ROOM GATE. Use the fetch_cl_contacts primitives (import fetch_cl_contacts as F). If chromerpc's Chrome crashes (it can under load), relaunch it (py scripts/refresh.py auto-relaunches, or ensure_chromerpc) and continue.

STAGE 2 (vet + enrich): (a) if refresh.py flagged market buckets, do ONE WebSearch per (area,room_type) + py scripts/market_comps.py set ..., then py scripts/research.py --all-new. (b) Vet EVERY status='new' listing that has detail_fetched_at + photos, with parallel general-purpose subagents — batch ids (~5-6 each; use py tools/batches.py), each subagent reads py scripts/db.py show <id>, EVERY photo in data/images/<id>/, and data/research/<id>.json under the two-stage rubric (SHARED-ROOM GATE first!), writing a verdict with an 'enrich' block (address from body, MONTHLY price, correct room_type/bed-bath, clean title) to data/_verdicts_*.json. (c) py tools/apply_verdicts.py; delete the batch files; py tools/dedup.py.

STAGE 3 (flag CL scams, BY HAND): for every Craigslist row confirmed likely-scam this run (or a scam-tell reject), drive chromerpc by hand to click its flag link (CLAUDE.md step 4a) BEFORE purging. Then py tools/purge_db.py --execute.

STAGE 4 (CL contact reveal, BY HAND, ALL kept CL): reveal the reply relay (+ phone, + any in-body reveal) for EVERY surviving vetted Craigslist row so the dashboard carries contact for all of them. NO batch script. import fetch_cl_contacts as F; per listing: navigate(url) -> sleep -> warmup() -> screenshot -> _center(_qs('button.reply-button')) -> human_click -> sleep -> screenshot -> _read_reply_panel(out) (and reveal_in_body(out) if body-gated), then db.update_detail(conn, pid, dict(reply_email=..., phone=..., contact_name=...)). ONE pass per listing, spaced out, PRIORITISE SF; if CL throttles (F._reply_uninit() true / no reply token) STOP and let the rest retry next run. Then py scripts/sync_supabase.py.

STAGE 5 (email — SF ONLY, Craigslist 1BR/1BA ONLY): for each NEW qualifying CL pick this run (non-scam, area ok, fit>=80, legit>=70, room_type=='1br', AND located IN SAN FRANCISCO — NOT Berkeley, NOT Daly City): read sensitive/email_body.json, hand-author a plain human email per its style_rules (subject "Interested in this: <address>"), send with py scripts/send_email.py --auto --listing <id> --subject "..." --body-file <tmpfile>. --auto sends only if OUTREACH_AUTOSEND=1 AND room_type=='1br', and refuses any already-contacted unit (dedup cluster / address / phone). STUDIOS, 2+BED, and every Berkeley / Zillow / Zumper / Apartments row are NEVER auto-emailed — surface only. Cap 2 sends per run. Scams never emailed. py scripts/sync_supabase.py after.

FINAL — exactly ONE combined Telegram digest: py scripts/notify.py --list-new gives qualifying new picks as JSON. Compose ONE short message with up to three sections — new picks (name, $price/type, area, trust, fit, #id link), emails (sent or ready this run), replies (Stage-0 verdicts) — skip any empty section, end with the dashboard link. Send once: py scripts/notify.py --message "<text>". If all sections empty, send nothing. After it sends, py scripts/notify.py --mark-notified <ids...> for the picks you included. Then py tools/purge_images.py --all and py scripts/refresh.py --teardown-chromerpc. End with a short summary of standouts.
'@

"$(ts)  invoking headless claude ..." | Out-File -Append -Encoding utf8 $LOG
& claude --dangerously-skip-permissions -p $PROMPT *>> $LOG

# Safety net: make sure chromerpc is down even if the agent didn't tear it down.
py scripts/refresh.py --teardown-chromerpc *>> $LOG 2>&1
"$(ts)  ===== refresh END =====`n" | Out-File -Append -Encoding utf8 $LOG
