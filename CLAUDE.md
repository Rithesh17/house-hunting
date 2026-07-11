# SF House-Hunting Assistant — Claude Code Playbook

This project finds affordable SF rentals. **Claude Code is the engine:** the user
opens a session and asks for listings; you orchestrate the Python scripts for the
mechanical work and personally do the **vision vetting + fit ranking**.

> **Read `THUMB_RULES.md` first** — standing cost/efficiency guardrails (don't abuse
> paid APIs, store only what the dashboard needs, never opt into paid tiers). They
> override convenience.
>
> **Read `MANUAL_SOURCES.md`** — **Zillow + Apartments.com are gathered BY HAND** by
> you (the LLM) driving headful chromerpc; there is no scraper and you must not write
> one. Craigslist + Zumper are the only scripted sources.
>
> **`OUTREACH.md`** covers the email outreach pipeline layered on top of the refresh
> (Stage 0 read+vet replies → Stage 1 vet+enrich listings → Stage 2 contact+send),
> the `new → vetted → contacted → interested` status model, and the single combined
> Telegram digest. The refresh cron runs it when enabled.

## The user's criteria (the bar every listing is measured against)
- **Budget:** hard cap **$2,000/month**.
- **Type:** **1 bed / 1 bath AND 2+ bedroom whole units (houses/flats) are BOTH
  top priority** — the user wants to see them both. A **spacious studio (≥450 sqft,
  full kitchen + bath)** is an acceptable fallback. **Reject shared rooms / SROs /
  tiny (~250 sqft) studios.** A 2+ bed must be the **WHOLE unit** (not a room in a
  shared house). **Scrutinize 2+ bed listings HARDER for scams:** a whole multi-
  bedroom home/flat at or under the $2,000 cap is unusually cheap for SF, so it is
  exactly the bait scammers use — demand internally-consistent photos of the whole
  unit, normal on-platform terms, and a real address before trusting it.
- **Two hard requirements for ANY pick (added — not just a preference):**
  1. **LONG-TERM only.** Reject short-term / summer / date-bounded sublets ("July to
     mid-August", "Jul 1–Oct 30", "2-month min", "/week", "nightly", "vacation
     rental"). A standard 12-month-ish lease only; a move-in *date* (e.g. "available
     July 1") is fine, a fixed *end* date is not.
  2. **FULL private bathroom, and a real private kitchen — with ONE kitchenette
     exception.** Reject any **shared / common bathroom or kitchen** outright. A
     **kitchenette / "light cooking area" / no-oven / no-stove / hot-plate / wet-bar**
     is normally a reject too — **EXCEPT** when ALL of these hold: the unit is a
     **whole 1 bed / 1 bath** (not a studio, not 2+ bed), it is **genuinely spacious /
     large (not small or cramped)**, and the kitchenette is the **SOLE** caveat. In
     that one case it MAY be KEPT and surfaced for a **manual** look (contact by hand,
     not the clean auto-send profile). For a **studio, or ANY small / cramped unit, a
     kitchenette is a HARD reject — no exception.** Always record the kitchen type
     (full kitchen vs kitchenette/no-oven) in `verdict_summary`.
- **AUTO-SEND is 1BR/1BA ONLY.** The cron auto-emails ONLY `room_type=='1br'` (real
  1 bed / 1 bath) units. **Studios and 2+ bed are still surfaced on the dashboard at
  full score (scores are NOT penalized) but are NEVER auto-emailed** — contact those
  manually. Enforced in code: `send_email.py --auto` refuses any non-`1br` listing
  (a hard guard beside the `OUTREACH_AUTOSEND` gate); a manual send omits `--auto`.
- **Areas — THREE tiers (`avoid` / `caution` / `ok`).** Prime residential SF is a
  LEVEL field (no favorite among Richmond/Sunset/Noe/etc.); two distinctions sit on
  top (refined from 2024-26 crime data + local reporting + Reddit; SF crime hit a
  23-year low in 2024):
  - **`avoid` (unsafe — sunk, badged, excluded from picks, DELETED by purge):**
    Tenderloin, **Lower Nob Hill / Tendernob**, **Chinatown** (the one SF nhood
    where violent crime ROSE in 2025, +39%), the SoMa-6th-St / Mid-Market / Union-
    Square-Downtown core, Bayview/Hunters Point, and the **Sunnydale** projects.
  - **`caution` (okay-but-not-prime — SURFACED + kept, but match-discounted ×0.7 and
    ranked as a GROUP below every `ok` area):** **upper Polk** (lower Polk stays
    avoid via the Tenderloin zone), **Financial District / Jackson Square** (low
    resident violence — daytime property crime + dead-at-night), the **eastern SoMa
    waterfront** (Rincon Hill / South Beach / East Cut), and **Visitacion Valley's
    residential remainder** (only the Sunnydale complex is avoid).
  - **`ok` (prime, level field):** everything calm/residential (Richmond, the
    Sunset, Noe Valley, Pac Heights, Cole Valley, Glen Park, West Portal, Hayes
    Valley, etc.). The broader **Mission** and **Western Addition** are NOT
    blanket-avoided. The user wants safe-to-walk-at-night first; parks/young-life
    are a bonus.
- **Ranking = grouped by tier, then MATCH.** `ok` first (level field by match (fit)
  then trust), then `caution` as a group below them, then `avoid` at the bottom
  (badged, excluded from Featured + Telegram). No favorites float; no proximity
  ordering.
- **MATCH is area-aware; TRUST is not.** The subagent scores `fit_score` on the
  UNIT alone; at publish time `geo.display_match()` folds area in — `avoid` reads as
  a LOW match (×0.3, ≤30), `caution` as a discounted one (×0.7), `ok` full. So a
  nice unit in a lesser-for-you area is a lesser fit. **Trust (`legit_score`) is
  never penalized for area** — it measures scam-risk only (a real property-manager
  studio in the Tenderloin is high-trust + low-match, excluded from picks anyway).
- **Area model is deterministic** (`scripts/geo.py` + the `unsafe:` and `caution:`
  blocks in `config.yaml`): each listing is classified `avoid` / `caution` / `ok`
  from its ACTUAL location — when the post gives a street address, `fetch_detail.py`
  geocodes it to PRECISE coords + a neighbourhood name; otherwise the post's area
  text is used. A listing is `avoid` if its NAME matches the unsafe list OR its
  coords fall in an unsafe zone; else `caution` if it matches the caution names/
  zones; else `ok`. **AVOID always wins over caution** (lower Polk is in the
  Tenderloin zone → avoid even though "Polk Gulch" is a caution name). Subagents
  score `fit_score` on the UNIT only — the area model owns area. `area_tier` is
  computed at sync time and stored in Supabase.
- Searches are configured in `config.yaml` (areas, room-type passes, price cap,
  notify thresholds, and the `unsafe:` + `caution:` area model).

## Trigger
When the user says anything like *"fetch the latest listings,"* *"check
Craigslist,"* *"any new places today?"* — run the pipeline below end to end.

## Sources
- **Craigslist** (`scripts/fetch_listings.py` + `fetch_detail.py`) — the backbone.
  NOTE (2026): CL changed search-result URLs to `www.craigslist.org/view/d/<slug>/
  <alphanumeric-id>` (no `/apa/` category code, no `.html`). `common.post_id_from_url`
  handles both formats; losing the category code means the old `roo` URL gate is gone,
  so `fetch_listings.looks_like_room()` drops obvious rooms by TITLE (word-boundary
  phrases; "1 bedroom"/"sunroom" never match) to stop the room flood — the subagent
  shared-room gate is still the backstop. `fetch_detail` reads the new page's
  `<time datetime>` for `posted_at`.
- **Zumper** (`scripts/fetch_zumper.py`) — pulls SF via its internal map API
  (POST `/api/svc/inventory/v1/listables/maplist/pins`, recursive box subdivide),
  filters to ≤ max_price, inserts `source='zumper'` rows ready to vet. Zumper
  image URLs MUST be `https://img.zumpercdn.com/<id>/1280x960?dpr=1&fit=crop&h=542&q=76&w=991`
  (bare sizes 404); embed with `referrerpolicy="no-referrer"`. The map API has NO
  description, and detail pages render client-side + lazy-load on scroll, so for
  each new listing the script drives **chromerpc** (`chromerpc_zumper_detail`):
  navigate → scroll-load every section → read the **ABOUT body** + posted age +
  best-effort **contact** (name/routed phone/extension, scanned anywhere on the
  page since Zumper places it inconsistently). This is essential: Zumper tags
  room-shares as "1 bedroom", so without the body a private-room-in-a-shared-unit
  is indistinguishable from a real 1BR — photos-only vetting WILL false-positive
  them (see the SHARED-ROOM GATE). The body also yields a posted date, so Zumper
  rows become recency-scopable. Prereq: chromerpc on :50051 (the refresh cron
  starts it). If chromerpc is down it falls back to the old Playwright path
  (`DescriptionFetcher`, usually a no-op here) → `description=None`; pipeline
  still runs but room-shares can't be caught from the body that run.
- **Zillow + Apartments.com** — **NOT scripted. Gathered BY HAND by the LLM** through
  headful chromerpc every run (after `refresh.py`, before vetting). There is no
  scraper for either and you must not write one or delegate to a subagent —
  **see `MANUAL_SOURCES.md`** for the full how-to and the hard "no script" rule. Both
  are bot-walled (Zillow PerimeterX, Apartments Akamai) and brittle to scrape, and
  filtered to ≤cap + last-24h they're only a handful of posts/day — cheaper and more
  reliable to read by hand. The old `fetch_zillow_cr.py` / `fetch_apartments_cr.py` /
  Apify `fetch_zillow.py` were **deleted** for this reason (the Zillow one had already
  broken on a leftover JS-eval call). You drive chromerpc's input gRPC for interaction
  (human mouse/scroll/keys), read the DOM only to extract info, screenshot every step,
  sort newest, stop at the first >24h post, vet inline, then add keeps to the DB by
  hand (`source='zillow'`/`'apartments'`, real coords) so they join the same flow.
All sources feed the SAME downstream (vetting, dedup, dashboard). `tools/dedup.py`
merges the same unit ACROSS sources (by shared image id, address, OR rounded coords +
price + room_type) into one tile; the dossier lists each source's link.

## Refresh — the one command (when the user says "refresh" / "fetch latest")
This is the full cycle. Run it end to end:
1. `py scripts/refresh.py` — does the deterministic plumbing incrementally:
   **launch headful chromerpc (auto)** + **hydrate local DB from Supabase** +
   Craigslist pull + Zumper pull + detail/photos/gates + prune dead links + dedupe +
   **Stage-2 research bundles** (`scripts/research.py --all-new` →
   `data/research/<id>.json`). It prints how many NEW listings need vetting AND which
   market buckets need a web lookup. **It does NOT pull Zillow or Apartments** — those
   are gathered BY HAND next (step 1b). refresh.py auto-launches chromerpc headful if
   it's not already on :50051 (it self-clones+builds chromerpc if no binary is found;
   `CHROME_BIN` in `.env` overrides Chrome detection); headful is REQUIRED — chromerpc
   backs Zumper detail, the CL contact fetch, and the manual gather. `--no-browser`
   skips the auto-launch.
1b. **Manual Zillow + Apartments.com gather — BY HAND, NO scripts** (see
   `MANUAL_SOURCES.md`). With chromerpc up, you (the LLM) open both sites yourself for
   SF + Berkeley, set the price cap, sort newest, walk the results until you pass 24h,
   open each recent post slowly (screenshot + DOM-read), vet inline, and insert keeps
   into the DB (`source='zillow'`/`'apartments'`, real coords). NEVER write a scraper
   or spawn a subagent for this — it's a tiny daily list. When ALL stages are done,
   `py scripts/refresh.py --teardown-chromerpc`.
   **A launchd cron runs this cycle (currently disabled — user triggers manually).** Cron runs use
   `notify.py --new --quiet-if-empty` so only real new picks
   ping Telegram (no repeated "nothing new" notes). See THUMB_RULES.md. The
   hydrate step (`scripts/hydrate_from_supabase.py`) pulls the cloud read-model
   back into local SQLite so a fresh checkout resumes WITHOUT re-vetting everything
   or letting the final sync delete cloud rows that are merely missing locally
   (`INSERT OR IGNORE`; a quick no-op on a healthy DB).
1a. **Fill market buckets** research flagged: do ONE `WebSearch` per
   `(area_group, room_type)` (e.g. "Inner Richmond 1BR average rent"), then
   `py scripts/market_comps.py set <group> <room_type> <low> <median> <high> web:<month>`,
   and re-run `py scripts/research.py --all-new` so price ranges attach to bundles.
2. **Vet the new ones with subagents** (the step that needs Claude's vision +
   judgment). Use `py tools/batches.py` to group the new ids; spawn parallel
   `general-purpose` subagents. Each reads the post (`py scripts/db.py show <id>`),
   **every photo** in `data/images/<id>/`, AND the **research bundle**
   `data/research/<id>.json`, then applies the **two-stage rubric below** (Stage 1
   = lenient post screen; Stage 2 = semantic cross-check of the bundle) and
   **Writes** verdicts to `data/_verdicts_*.json`.
3. `py tools/apply_verdicts.py` (merges the batch files; reject rooms/etc., keep
   scams flagged), then delete the batch files.
4. `py tools/dedup.py` (re-cluster with the new verdicts so primaries are best).
4a. **Stage 3 — FLAG confirmed scams on Craigslist (MANUAL, headful, NO scripts).**
   For every **Craigslist** row this run confirmed as a scam (verdict
   `legit_label == "likely-scam"`, OR a `disposition:"reject"` whose reason is a scam
   tell — cloned/undercut, stolen photos, off-platform+fee, fake license), YOU (the
   LLM) drive chromerpc to click the post's **flag** link by hand — one post at a time,
   screenshotting every step. This MUST happen **before** step 4b (purge deletes the
   scam rows). **STRICTLY NO automation script** and **NO page JS / DOM interaction** —
   exactly like the manual Zillow/Apartments gather. Reuse the Stage-1 chromerpc
   primitives (`scripts/fetch_cl_contacts.py`: `navigate`, `warmup`, `human_click`,
   `screenshot`, the CDP-DOM readers) from one-off `py -c` calls, deciding each next
   action from the screenshot. **Use your OWN chromerpc instance** (parallel agents
   share `:50051` — never drive their browser): launch a private headful instance on a
   spare port and set `CHROMERPC_ADDR=localhost:<port>` (e.g. 50071) for every call, and
   in Python `import fetch_cl_contacts as F; F.GRPC='localhost:<port>'`. Flag flow per
   post: `navigate(url)` → wait → `warmup()` → `screenshot` (confirm it's the right,
   still-live post) → locate `div.flag[role=button]` via CDP DOM and read its box-model
   center (**AVOID `div.banish`** — that's "hide this posting", NOT a flag) →
   `human_click` that center → verify success: `div.flag` loses its box model AND
   `div.unflag` ("flagged") gains one, and the toolbar screenshot shows a solid ⚑ +
   purple "flagged". CL flagging is a **single click** (no reason submenu — the reasons
   live only in the tooltip). Space posts out; don't hammer. Tear down ONLY your own
   instance afterward (never `refresh.py --teardown-chromerpc`, which stops the shared
   one). Non-CL scams (Zillow/Apartments) are out of scope — CL is the only flaggable
   source here.
4b. `py tools/purge_db.py --execute` — DELETE rejected/removed + low-trust(<40) +
   unsafe-area rows (blocklisted so they don't re-surface), keeping ok-area trust≥40.
   Keeps the DB + dashboard to only what's worth seeing.
4c. **Stage-1 contact-fetch (ALL kept CL, not just the ones we email).**
   `py scripts/fetch_cl_contacts.py --all-vetted` — once vetting has settled the good
   listings, grab the reply relay (+ any phone) for EVERY surviving `vetted`
   Craigslist row so the dashboard carries contact info for all of them. It already
   drives chromerpc **like a human — real Bézier mouse + no page JS**, reading the DOM
   only to locate elements. Two rules to avoid the blocks we hit before: (1) run it on
   **YOUR OWN chromerpc instance** — set `CHROMERPC_ADDR=localhost:<port>` — never the
   shared `:50051` (parallel agents drive it, and reading a co-tenant's tab both
   corrupts your reads and looks bot-like); (2) **ONE pass per listing, spaced out**
   (`--delay`), never re-hammer. CL throttles repeated reply requests, so a big batch
   may only resolve some — the unfetched rows are reselected on a later run; for a
   stubborn few, reveal them **by hand** with the same primitives (`navigate` →
   `warmup` → human-click `button.reply-button` → read the panel) rather than re-running
   the batch. (Stage 2 then just DECIDES which of these already-contactable picks to
   email — it does NOT fetch.)
5. `py scripts/sync_supabase.py` — **publish to the cloud** so the public
   dashboard updates (re-run after vetting/purge so new scores/dedup land + purged
   rows drop). `refresh.py` already runs this once at the end; run it again here.
6. **Telegram — ONE digest of the qualifying NEW picks from this fetch.**
   `py scripts/notify.py --new` sends a single short, text-only message of the
   un-notified, qualifying picks from this fetch (NOT the top-N overall): each =
   name + price/type + area + trust/match + a one-line summary + a dashboard
   deep-link (`#id=<id>`), with a footer linking the full ledger. NO images. If
   NOTHING new qualifies, it sends a brief "no new postings worth a look this
   round" note (still ending with the dashboard link) so the user knows the fetch
   ran. (Or pass explicit ids: `py scripts/notify.py <id> <id> ... --force`;
   `--top N` still exists for a best-overall digest.) Never use `--all-qualifying`
   for routine sends. Scams are always blocked.
7. `py tools/purge_images.py --all` — drop the transient local photos.
8. Summarize the standouts in chat; public dashboard at the Vercel URL
   (`VERCEL_DASHBOARD_URL` in `.env`).
If `refresh.py` reports 0 new, just stop — nothing to vet (the cloud was still
re-synced for prunes/dedup).

## Architecture — local engine, cloud read-model
The pipeline runs **locally** here (Claude Code): fetch → vet → dedup write to the
local SQLite (`data/listings.db`, the source of truth, gitignored). `sync_supabase.py`
then pushes a **minimal** read-model to **Supabase** (Postgres): one row per
deduped unit with identity + display essentials + remote `image_urls` + Claude's
verdict (scores/summary/recommendation/red_flags) + an embedded `sources` list.
The cloud carries ONLY listings worth seeing — `sync` skips `rejected` + `removed`
(`SKIP_STATUS`), and `tools/purge_db.py` deletes rejected/removed, low-trust
(`legit_score<40`), and unsafe-area (`avoid`) rows from the local DB entirely
(blocklisting their ids so they don't re-surface). So the dashboard = ok-area,
non-rejected units (flagged-scam rows at trust ≥ 40 are kept, badged, hidden from
picks).
Because the local DB is gitignored and not always present, the cloud doubles as a
**recovery backup**: `scripts/hydrate_from_supabase.py` (step 0 of `refresh.py`)
rebuilds the local DB from Supabase — every cloud unit becomes a full local row
(marked detail-fetched + vetted + already-notified so it is not reprocessed), and
each unit's folded `sources` become `status='removed'` stubs so their post ids
suppress re-discovery. It is `INSERT OR IGNORE`, so it only fills in missing rows.
The verbatim post body is NOT uploaded — the dossier links out to the source post
(keeps the free-tier DB small). Schema: `supabase/migrations/0001_init.sql`
(RLS = anon SELECT only; the local sync writes with the service_role key).
The **dashboard is a static site on Vercel** (`dashboard/`, Hobby/free) that reads
Supabase directly with the public anon key (`dashboard/config.js`). It is
**read-only** — manage status locally (`py scripts/db.py set-status …`) then
`sync_supabase.py`. **Redeploy Vercel ONLY when the dashboard view changes**
(edits in `dashboard/`): the Vercel project's **Root Directory = `dashboard`**,
so deploy from the **repo root** with `vercel deploy --prod --yes` (NOT
`vercel deploy ./dashboard` — that double-nests the root dir and the build hangs).
A `git push` to `main` also auto-deploys via the GitHub integration. Data-only
updates need just `sync_supabase.py` — no redeploy. Keep both on free tier (no
paid Vercel/Supabase compute). The local Flask `serve.py` remains for offline
viewing of the local DB but is no longer the dashboard's data source.

## Pipeline detail (philosophy: pull broad, filter by JUDGMENT via subagents)
1. **Discover broadly (almost all of SF).**
   `py scripts/fetch_listings.py`  (+ `py scripts/fetch_zumper.py` for Zumper)
   ONE broad pass over `sfc/hhh` with `max_price` + `excats` (config) — no
   server-side bedroom filter (it wrongly excludes sublets with unset fields).
   Keeps ALL SF neighborhoods (not just preferred ones); drops only non-rental
   categories (block-list by post-URL code: office/parking/for-sale/vacation/
   swap) and clearly out-of-SF cities. room_type is left "unknown" here and
   derived later from the parsed bedroom count.
   INCREMENTAL: the run records `last_pull` (meta table) and is incremental by
   construction — already-seen post ids are skipped (id-dedup) and, since CL
   sorts newest-first, it stops once it hits a fully-seen page. So re-runs only
   add genuinely new posts (then only those go to detail+vetting).
2. **Pull details + photos; OBJECTIVE gates only.**
   `py scripts/fetch_detail.py --all-new`
   Fetches each post's full description + coords + address + photos, derives
   room_type, geocodes an address when map coords are missing, and extracts any
   phone/email from the body. It auto-rejects ONLY on objective, no-false-
   positive signals (`filters.objective_reject_reason` + a category check):
   - Craigslist's own **`roo` (rooms/shared) category** — the poster's explicit
     declaration (skipped before download);
   - **no photos**;
   - **outside SF by coordinates** (authoritative).
   Everything else — private rooms hiding as "in-law apartments", scams, fit,
   neighborhood safety — is judged by SUBAGENTS, not scripts. (We do NOT use the
   brittle keyword room/scam scripts in the main flow; they caused false drops.)
3. **Vet survivors with subagents (the judgment layer) — TWO STAGES.**
   Stage 1 = looking at photos + reading the description with real vision; Stage 2
   = semantically cross-checking the scripted research bundle
   `data/research/<id>.json` (DRE name-match, real parcel, price ratio, duplicate
   siblings). Scripts FETCH the facts; the subagent VERIFIES them. This is where
   rooms/scams/fit are decided. Subagents also return a `disposition`
   ("keep"/"reject") + `reject_reason`; `apply_verdicts.py` rejects
   rooms/wanted/out-of-area while keeping scams visible-but-flagged.
   For a full run, **fan this out across parallel subagents** so it's fast:
   - Split the new listing ids into small groups (~3 per subagent) and spawn
     several `general-purpose` subagents **in parallel** (multiple Agent calls in
     one message).
   - Use `py tools/batches.py` to print survivor ids grouped into batches
     (preferred areas + price first). Give each subagent its ids + the rubric +
     schema. Each subagent: for each id run `py scripts/db.py show <id>`, **Read
     every image** in `data/images/<id>/` (multimodal), **read the research bundle
     `data/research/<id>.json`** for the Stage-2 cross-check, apply the rubric, and
     **WRITE its results with the Write tool to `data/_verdicts_w_<letter>.json`**
     (a JSON array; NOT the DB — avoids concurrent SQLite writes).
   - Each verdict includes `disposition` ("keep"/"reject") + `reject_reason`,
     `category`, scores, `sqft_estimate`, `verdict_summary`, `recommendation`,
     and `verification` (the Stage-2 outcomes).
   - Apply all batches at once: `py tools/apply_verdicts.py` (merges every
     `data/_verdicts*.json`; rejects rooms/wanted/out-of-area, keeps scams
     flagged). Delete the batch files after.
   **You (the orchestrator) review before sending anything.**
4. **Prune dead links, dedupe, free disk.**
   `py scripts/check_links.py` GETs each existing post and marks taken-down ones
   `removed` (404 / "deleted" / "expired") so the dashboard drops them. Then
   `py tools/dedup.py` clusters reposts of the same unit (shared CL image ids /
   address+price+type) and tags each with `dup_group` (best = primary). Then
   `py tools/purge_images.py` deletes the locally-cached photos (the dashboard
   embeds remote CL image URLs stored in `image_urls`; local files are only
   needed transiently during vetting).
5. **Notify the winners.**
   `py scripts/notify.py --all-qualifying` (or `<id> --minimal --force` for a
   curated set) — Telegram cards include a summary + original link; scams blocked.
6. **Summarize in chat.** Rank by match; surface standouts; dashboard at
   http://localhost:8000 (`py scripts/serve.py`) shows one tile per unit with the
   duplicate source links inside (best first).

## Vetting rubric — TWO STAGES, ASYMMETRIC
**Core rule: verification only ever RAISES trust; only positive scam evidence
LOWERS it; "can't tell" is NEUTRAL and the listing is still surfaced.** Never push
an honest-but-unprovable post toward scam — small landlords, subletters, amateur,
unlicensed, and cheap posts are exactly what the user wants to see. Read the post
(`py scripts/db.py show <id>`), EVERY photo in `data/images/<id>/`, AND the
research bundle `data/research/<id>.json`. Score two independent things: a FRAUD
level (only the Stage-1/Stage-2 scam signals raise it) and a CONFIDENCE level
(only verifications raise it). `legit_label`: `likely-legit` (🟢), `unverified-
amateur` (🟡 plausible but unproven — KEEP & surface), `likely-scam` (🔴 filtered
from alerts).

**Stage 1 — the post + photos.** Separate "polish" from "legitimacy." NEVER lower
`legit_score` for amateurism (two blurry phone photos + terse text is normal →
`low_polish:true`), for a missing license, for no sqft, or for below-market price
by itself. Lower it ONLY for true scam signals:
- Stolen / stock / watermarked / MLS photos; photos internally inconsistent.
- "Out of the country / can't show in person," or refuses any LIVE tour — a
  pre-recorded "video tour" or a lockbox self-showing is NOT live verification
  and is itself a scam tell.
- Wire / Zelle / gift-card / deposit or application fee demanded BEFORE a viewing.
- Pushes off-platform immediately; asks SSN / large fee upfront.
- Price PHYSICALLY implausible (graded, like-for-like — see Stage 2).

**SHARED-ROOM GATE (reject hard — the #1 false-positive to catch).** A private
room in a shared unit is NOT a 1BR, no matter what the title, the source's
metadata, or `room_type` says. Listing feeds LIE about this: Zumper/Craigslist
routinely tag a room-share as "1 bedroom," and the photos alone (one tidy
bedroom) look identical to a real studio/1BR. **You must read the FULL
description and treat any of these as a shared room → `category:"room"`,
`disposition:"reject"`, `reject_reason:"shared room"`, `is_1br1ba:false`:**
- "room for rent", "private room", "ONE person only", "room is private but…"
- shared / common kitchen OR bathroom; "shared bath", "common areas"
- a roommate / housemate / host already occupies another room in the unit
- "utilities divided/split evenly", "rent the other room separately"
- in-law / downstairs unit described as a single room with a kitchenette only
- **`source_signals.is_room_for_rent: true`** in the research bundle — Zillow's OWN
  room-share flag (so is `is_rent_by_bed`). Treat it as a strong shared-room signal
  even if the photos look like a whole unit.
When the description is MISSING or thin (common for **Zumper** rows — they often
have no body text), DO NOT assume the "1br" tag is real. A bare "1 bedroom" with
no description and only interior room photos is **unverified — never send it as a
top pick**; mark `legit_label:"unverified-amateur"` and call out in
`verdict_summary` that share-vs-unit is unconfirmed. Prefer cross-checking the
source URL/original post. **It is fine to send NOTHING for the day — a missed
shared room is worse than an empty result.**

**Stage 2 — semantic cross-check of `data/research/<id>.json`** (the script only
FETCHED facts; YOU match/verify — semantic, not exact-string):
- `dre`: if the post cites a license, does the licensed `name` semantically match
  the lister's name? `status` active? → match = boost; name MISMATCH / revoked /
  fake = strong scam flag (a real # under a different name = a stolen license).
  NO license = NEUTRAL (small landlords / subletters don't have one).
- `owner`: is `owner.match` a real residential parcel, and do its `use`/`units`
  fit the post? (a "whole 3BR house" vs a 1-unit condo, or an address that's not a
  parcel = flag.) Owner NAME isn't in free SF data — only flag clear contradictions.
- `market.ratio_vs_median`: `≳0.85` normal · `0.6–0.85` plausible (rent control /
  deals → neutral) · `<~0.55` implausible → fraud evidence. Like-for-like
  (room↔room, 1br↔1br). Cheap ALONE is never a verdict.
  - `market.ratio_vs_zestimate` (Zillow rows): same grading vs Zillow's per-listing
    `rent_zestimate` — a free, unit-specific market reference (no web lookup needed).
- `source_signals` (Zillow): besides the room flags, `price_history` can reveal the
  unit's real prior rent (e.g. listed $2,800 in 2024 vs "$1,200" now → clone/undercut
  evidence, same logic as web_checks); `listed_by` is the listing agent/brokerage to
  sanity-check vs the post + DRE; `parcel_number` is the real APN for the owner check.
- `siblings`: same-address/coords reposts can be LEGIT (owner re-posting with a
  better title — same price/beds/photos) OR a SCAM FLOOD (many posts with
  differing prices/photos/contact). A shared photo or contact at a DIFFERENT
  address = stolen photos / spam ring = strong flag. Judge semantically.
- `web_checks` — **run these with `WebSearch`** (the bundle suggests the queries):
  search the address to see if the SAME property is listed ELSEWHERE. What matters
  is a **contradiction**, NOT mere presence (aggregators like Apartments.com /
  HotPads often just re-host the Craigslist post — ignore those echoes):
  - **FLAG** only if the same unit shows a **much higher real rent** (a big gap —
    e.g. $1,650 here vs $2,800 there → cloned + undercut bait) OR is listed **FOR
    SALE** (photos lifted from a sale listing). This caught 3870 Sacramento & 133
    Caine Ave. A much-higher price counts EVEN IF that listing is now "unavailable"
    — it still reveals the unit's real rent, which makes the cheap post implausible.
  - **NEUTRAL / fine** (do NOT flag): no other listing at all (landlords often
    post only on Craigslist); the other listing merely shows "unavailable / off-
    market / expired" at a **comparable** price; or only aggregator echoes of this
    same post. Comparable price = corroboration (a small boost).
  Also search the phone / agent name for reuse across unrelated listings or
  scam-report hits.
Record each check in the verdict's `verification` object. A confirmed cross-check
(DRE match, real parcel, consistent siblings) → `likely-legit`; a contradiction →
`likely-scam`; neither → `unverified-amateur` (still surfaced). **Contact-stage
tip (manual): before sending money/info, the killer test is whether they'll do a
LIVE tour — paste their reply and re-check.**

**Fit score (0–100):** 1BR/1BA **and a WHOLE 2+ bedroom unit (house/flat)** are
BOTH top tier (**~82–95**) — do NOT penalize a listing for having 2+ bedrooms;
the extra space is wanted. A decent **studio is a SOLID second tier (~65–80)** — we
prefer 1BR/1BA, but **do NOT punish studios too hard**: a clean, self-contained
studio (esp. ≥450 sqft) should land ~70+ so a high-trust one still clears the notify
bar; only a genuinely **small/cramped/SRO-ish** studio drops below ~55. Shared
rooms/SROs are rejected outright. A 2+ bed only earns
top tier if it's the **whole unit** — a "room in a 3BR" is still a shared room
(reject). **Do NOT weight the neighborhood in `fit_score`** — score the UNIT
itself (type/size/condition/value); the deterministic area model owns area
(unsafe areas sink, everything else is equal). Bonus for price headroom under
$2,000.

**2+ bed scam scrutiny:** a whole multi-bedroom home/flat at ≤$2,000 is unusually
cheap for SF and a classic scam bait. Apply the scam signals MORE strictly here:
the photos must show a coherent whole unit (kitchen + every bedroom + bath of the
SAME home), the terms must be normal/on-platform, and the address must be real.
If the body actually offers a "furnished room" while the title claims a whole
2BR/3BR home, it's a shared-room scam → reject. When the whole-unit claim can't be
confirmed, keep it visible but mark `legit_label:"unverified-amateur"` and say so.

**Square footage policy:** NEVER drop a listing just because it has no sqft in
the description — most legit posts omit it. Instead **estimate sqft from the
photos** (room proportions, furniture scale) and record it in `sqft_estimate`.
Only penalize "spaciousness" when the unit clearly looks cramped; when unsure,
do not penalize.

### Verdict JSON
```json
{
  "legit_score": 88,
  "legit_label": "likely-legit",
  "red_flags": [],
  "low_polish": false,
  "fit_score": 82,
  "is_1br1ba": true,
  "room_type": "1br",
  "verdict_summary": "Real 1BR in Inner Richmond, consistent photos, normal terms.",
  "recommendation": "Strong match — email today to schedule a viewing.",
  "verification": {
    "dre":       {"outcome": "match",      "note": "DRE 01234567 = lister's name, active"},
    "owner":     {"outcome": "ok",         "note": "real 3-unit residential parcel"},
    "price":     {"outcome": "plausible",  "note": "$1,950 vs ~$2,900 median (ratio 0.67)"},
    "duplicates":{"outcome": "ok",         "note": "no flood; single post"}
  },
  "enrich": {
    "address": "333 9th Ave, San Francisco, CA", "price": 1950,
    "room_type": "1br", "sqft": 600, "neighborhood": "Inner Richmond"
  }
}
```
`verification` is the Stage-2 cross-check (omit a key you couldn't check; each is
`{outcome, note}` where outcome ∈ verified/match/ok/plausible · neutral/unverified
· flag/mismatch/scam/flood/implausible). It is stored, synced, and shown on the
dashboard. Absent verification is fine — it just leaves trust unchanged.
`enrich` is the **MANDATORY detail entry — the ONLY source of every display field.**
The scrapers DO NOT auto-map fields anymore (that brittle logic was removed — it
mislabeled real units, e.g. Zillow "Apartment for rent"). They fetch ONLY raw data
(the verbatim description / page text / API blob in `source_signals.raw`, the photos,
and coords for the area model). **You — having read the description, every photo, and
the research bundle — are the authority on the listing's real details, so you MUST
author EVERY display field here for EVERY listing (all sources: CL, Zumper, Zillow).**
Read the raw values (CL `source_signals.raw.raw_attrs`, the description, the body; or
Zillow `source_signals.raw.building_address`/`unit_address`/`bedrooms`/`bathrooms`/
`living_area_sqft`/`home_type`/`api_monthly_price`) and write the canonical value for:
`title` (a clean human title, e.g. the street address or a short descriptor — NOT a
placeholder), `price` (the real MONTHLY number; normalize any weekly/nightly quote),
`bedrooms`, `bathrooms`, `room_type` (`studio`/`1br`/`2br_plus`), `housing_type`,
`sqft` (estimate from photos if absent), `address`, `neighborhood`. `apply_verdicts.py`
writes these as the canonical row. Allowed keys: title, price, bedrooms, bathrooms,
sqft, room_type, housing_type, area, neighborhood, address, lat, lng. Fill every key
you can determine; simply omit one you genuinely cannot (e.g. an undisclosed
address — that is FINE, **a missing/undisclosed address is NOT a reject reason** on
its own; keep the listing and leave `address` unset. Many legit posts (in-law units,
privacy-minded owners) withhold the exact street until contact — surface them, and
just ask for the address when reaching out).
If the true MONTHLY price exceeds the $2,000 cap, also set `disposition:"reject"`,
`reject_reason:"over $2000/mo cap"`. Changing area/address/coords re-classifies
avoid/caution/ok at sync time. Scoring rules are unchanged. **See `OUTREACH.md`** for
the contact/email pipeline that consumes these.
`is_1br1ba` is literal (true only for an actual 1BR/1BA). A **whole 2+ bed** is
NOT a 1BR/1BA, so `is_1br1ba:false`, but it is still top-priority — give it a
**high `fit_score`** and set `room_type:"2br_plus"` (the dashboard badges it
"2+ Bed"). Don't conflate `is_1br1ba:false` with low fit.

## Scripts
- `scripts/fetch_listings.py` — multi-pass discovery → DB stubs.
- `scripts/fetch_detail.py` — parse post + download photos (+ phone/email/DRE#).
- **Zillow + Apartments.com have NO script** — gathered BY HAND via chromerpc; see
  `MANUAL_SOURCES.md`. (The old `fetch_zillow_cr.py`, `fetch_apartments_cr.py`, and the
  retired Apify `fetch_zillow.py` were deleted — do not recreate them.)
- `scripts/research.py` — Stage-2: assemble `data/research/<id>.json` (DRE + owner
  + market range + duplicate siblings) for the vetting subagent to cross-check.
- `scripts/verify_dre.py` — look up a CA DRE license (name/status/broker/discipline)
  via the free public lookup; `extract_dre()` parses license #s from a post body.
- `scripts/owner_lookup.py` — assessor parcel facts for an address/coords (DataSF).
- `scripts/market_comps.py` — cache of external market-rent ranges per
  (area_group, room_type); filled by the orchestrator via WebSearch (`set`).
- `tools/apply_verdicts.py` — merge batch verdicts into the DB (rooms/etc rejected);
  also applies the verdict's `enrich` block (semantic detail entry — see OUTREACH.md).
- `tools/purge_db.py` — DELETE listings we don't keep (`status` rejected/removed,
  trust `legit_score<40`, OR unsafe `area_tier=='avoid'`) and add their ids to a
  **blocklist** so they're never re-pulled/re-vetted; keeps ok-area + trust≥40
  (incl. flagged-scam rows at trust≥40). Dry-run by default; `--execute` to delete,
  then `sync_supabase.py` drops them from the cloud. Run after vetting each refresh.
- `scripts/notify.py` — Telegram digest (thresholded; scams blocked). For the
  outreach cron: `--list-new` (qualifying new picks as JSON, no send), `--message
  "<text>"` (send ONE combined digest), `--mark-notified <ids>` (after that send).
- `scripts/send_email.py` — Stage-2 outreach send (Gmail SMTP). Thin: Claude hand-
  authors the human body from `sensitive/email_body.json`; this just sends it (real
  SPF/DKIM/DMARC, no bot headers) and flips the listing to `status='contacted'`
  (the only resend guard — refuses an already-contacted listing without `--force`).
  `--listing <id>` resolves the relay + logs status; `--dry-run` previews. See OUTREACH.md.
- `scripts/read_replies.py` — Stage-0 reply reader (Gmail IMAP). Pulls UNREAD inbox
  messages, keeps the loosely rental-relevant ones, and emits `{contacted_listings,
  replies}` JSON for Claude to MATCH + judge (the script does no brittle matching).
  Marks fetched mail `\Seen`; `--since N` / `--keep-unseen`. See OUTREACH.md.
- `scripts/fetch_cl_contacts.py` — ON-DEMAND (not in the auto-refresh): reveal a
  Craigslist listing's reply contact (relay email + phone) by driving the LOCAL
  **chromerpc** browser with raw CDP + human-like Bézier mouse clicks (CL hides
  contact behind the JS reply button). Coords come from the DOM bbox (buttons shift
  with viewport; fixed pixels fail), the reply panel loads async (we poll), and it
  enumerates the reply-option-headers (email/call/text) clicking each. ALSO handles
  the in-body "click to reveal contact" pattern (`reveal_in_body()`): if the post
  BODY gates a phone/email behind a click/blocked section, it finds the trigger,
  human-clicks it, waits, and reads the revealed number — self-skipping when there's
  none. Stores `reply_email` + `phone` on the row. Prereq: `cd chromerpc && ./bin/chromerpc
  -headless -addr :50051 &`. **ONE pass per listing** — CL throttles repeated reply
  requests per IP (a batch of 20 got ~11; the rest throttle/expire and are retried
  on a later run since unfetched rows are reselected). `--all-vetted` | `<ids>` |
  `--force` | `--delay N`.
- `scripts/sync_supabase.py` — publish the minimal cloud read-model to Supabase.
- `scripts/serve.py` — local map dashboard of the LOCAL db (http://localhost:8000).
- `scripts/db.py` — SQLite schema + CLI (`init`, `list`, `show`, `set-status`).
- `dashboard/` — static public dashboard (Vercel) reading Supabase via anon key.

## Notes
- Be polite to Craigslist (the scripts set a UA + delay). Fetch details only for
  new, in-area posts.
- `lat/lng` come from the post and are block/neighborhood accurate, not exact.
- Zillow + Apartments.com are MANUAL (no scraper) — see `MANUAL_SOURCES.md`. All
  sources still converge on the same vet → dedup → dashboard flow.
