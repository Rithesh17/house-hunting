# SF House-Hunting Assistant — Claude Code Playbook

This project finds affordable SF rentals. **Claude Code is the engine:** the user
opens a session and asks for listings; you orchestrate the Python scripts for the
mechanical work and personally do the **vision vetting + fit ranking**.

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
- **Areas — LEVEL FIELD except unsafe.** Every SF area is treated equally; there
  is no favorite/preferred weighting. The ONLY area distinction is **unsafe**
  (refined from 2023-25 crime data + local reporting + Reddit; SF crime hit a
  23-year low in 2024, so the avoid set is deliberately tight): **AVOID**
  Tenderloin + its edges (Lower Nob Hill / Tendernob, Polk Gulch — but NOT the Nob
  Hill crest, which is safe), SoMa (esp. the 6th-St corridor), Civic Center/Mid-
  Market, Union Square/Downtown, Financial District core, Chinatown, Bayview/
  Hunters Point, and Visitacion Valley/Sunnydale. The broader **Mission** and
  **Western Addition** are NOT blanket-avoided (crime down / block-by-block; only
  the 16th-&-Mission plaza is a micro-hotspot). Everything calm/residential
  (Richmond, the Sunset, Noe Valley, Pac Heights, Cole Valley, Glen Park, West
  Portal, Hayes Valley, etc.) is good — none preferred over another; the user
  likes parks nearby, young life, and streets safe to walk at night.
- **Ranking = by MATCH score; unsafe areas sink to the bottom.** All non-unsafe
  areas are a level field ranked by match (fit), then trust (legit). **Unsafe
  ("avoid") areas** sink to the bottom (badged "unsafe area", excluded from
  Featured + Telegram alerts). No favorites float; no proximity-to-work ordering.
- **Area model is deterministic** (`scripts/geo.py` + the `unsafe:` block in
  `config.yaml`): each listing is classified into a **binary** tier —
  `avoid` (unsafe) or `ok`. Classification uses the listing's ACTUAL location:
  when the post gives a street address, `fetch_detail.py` geocodes it to PRECISE
  coords + a neighbourhood name; otherwise the post's area text is used. A listing
  is `avoid` if its neighbourhood NAME matches the unsafe list OR its coords fall
  in an unsafe zone (a coordinate backstop). Subagents score `fit_score` on the
  UNIT itself (type/size/condition/value) and do NOT weight the neighborhood — the
  area model owns area. `area_tier` is computed at sync time and stored in Supabase.
- Searches are configured in `config.yaml` (areas, room-type passes, price cap,
  notify thresholds, and the `unsafe:` area model).

## Trigger
When the user says anything like *"fetch the latest listings,"* *"check
Craigslist,"* *"any new places today?"* — run the pipeline below end to end.

## Sources
- **Craigslist** (`scripts/fetch_listings.py` + `fetch_detail.py`) — the backbone.
- **Zumper** (`scripts/fetch_zumper.py`) — pulls SF via its internal map API
  (POST `/api/svc/inventory/v1/listables/maplist/pins`, recursive box subdivide),
  filters to ≤ max_price, inserts `source='zumper'` rows ready to vet. Zumper
  image URLs MUST be `https://img.zumpercdn.com/<id>/1280x960?dpr=1&fit=crop&h=542&q=76&w=991`
  (bare sizes 404); embed with `referrerpolicy="no-referrer"`. The map API has NO
  description, and detail pages are behind a JS bot-challenge, so the script
  loads each new listing in **headless Chromium (Playwright)** and extracts the
  body from the page's `application/ld+json` (`DescriptionFetcher`). This is
  essential: Zumper tags room-shares as "1 bedroom", so without the body a
  private-room-in-a-shared-unit is indistinguishable from a real 1BR (see the
  SHARED-ROOM GATE). If Playwright/Chromium is missing the fetch degrades to
  `description=None` (pipeline still runs); some legit listings also simply have
  no body, which is fine.
Both feed the SAME downstream (vetting, dedup, dashboard). `tools/dedup.py` merges
the same unit ACROSS sources (by shared image id, address, OR rounded coords +
price + room_type) into one tile; the dossier lists each source's link.

## Refresh — the one command (when the user says "refresh" / "fetch latest")
This is the full cycle. Run it end to end:
1. `py scripts/refresh.py` — does the deterministic plumbing incrementally:
   **hydrate local DB from Supabase** + Craigslist pull + Zumper pull +
   detail/photos/gates + prune dead links + dedupe. It prints how many NEW
   listings need vetting. The hydrate step (`scripts/hydrate_from_supabase.py`)
   pulls the cloud read-model back into the local SQLite so a fresh checkout (or
   a machine that lost the gitignored `data/listings.db`) resumes WITHOUT
   re-vetting everything or letting the final sync delete cloud rows that are
   merely missing locally. It is `INSERT OR IGNORE` (existing local rows are
   never clobbered), so on a healthy local DB it is a quick no-op.
2. **Vet the new ones with subagents** (the only step that needs Claude's vision).
   Use `py tools/batches.py` to group the new ids; spawn parallel
   `general-purpose` subagents that Read each listing's photos + description and
   **Write** verdicts to `data/_verdicts_*.json` (see rubric below).
3. `py tools/apply_verdicts.py` (merges the batch files; reject rooms/etc., keep
   scams flagged), then delete the batch files.
4. `py tools/dedup.py` (re-cluster with the new verdicts so primaries are best).
5. `py scripts/sync_supabase.py` — **publish to the cloud** so the public
   dashboard updates (re-run after vetting so new scores/dedup land). `refresh.py`
   already runs this once at the end; run it again here after `apply_verdicts`.
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
3. **Vet survivors with subagents (the judgment layer).**
   Vetting = looking at the photos + reading the description with real vision.
   This is where rooms/scams/fit are decided. Subagents also return a
   `disposition` ("keep"/"reject") + `reject_reason`; `apply_verdicts.py` rejects
   rooms/wanted/out-of-area while keeping scams visible-but-flagged.
   For a full run, **fan this out across parallel subagents** so it's fast:
   - Split the new listing ids into small groups (~3 per subagent) and spawn
     several `general-purpose` subagents **in parallel** (multiple Agent calls in
     one message).
   - Use `py tools/batches.py` to print survivor ids grouped into batches
     (preferred areas + price first). Give each subagent its ids + the rubric +
     schema. Each subagent: for each id run `py scripts/db.py show <id>`, **Read
     every image** in `data/images/<id>/` (multimodal), apply the rubric, and
     **WRITE its results with the Write tool to `data/_verdicts_w_<letter>.json`**
     (a JSON array; NOT the DB — avoids concurrent SQLite writes).
   - Each verdict includes `disposition` ("keep"/"reject") + `reject_reason`,
     `category`, scores, `sqft_estimate`, `verdict_summary`, `recommendation`.
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

## Vetting rubric
**Separate "polish" from "legitimacy." Never reject a post just for being
amateurish.** A real non-tech landlord may post two blurry phone photos and a
terse description — that is normal and should pass (mark `low_polish: true`, do
NOT lower `legit_score` for it). The user explicitly wants to see these.

Lower `legit_score` only for **true scam signals**:
- Price clearly too good to be true for that unit/neighborhood.
- Stock / watermarked / MLS / obviously stolen photos; photos internally
  inconsistent (different units stitched together).
- "I'm out of the country / can't show it in person."
- Wire / Zelle / gift-card / deposit demanded **before any viewing**.
- Immediately pushes you off-platform; asks for SSN or big app fee upfront.
- The same photos reused across multiple posts.

Set `legit_label`: `likely-legit` (🟢), `unverified-amateur` (🟡, plausible but
unproven — keep & surface), or `likely-scam` (🔴, filtered from notifications).

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
When the description is MISSING or thin (common for **Zumper** rows — they often
have no body text), DO NOT assume the "1br" tag is real. A bare "1 bedroom" with
no description and only interior room photos is **unverified — never send it as a
top pick**; mark `legit_label:"unverified-amateur"` and call out in
`verdict_summary` that share-vs-unit is unconfirmed. Prefer cross-checking the
source URL/original post. **It is fine to send NOTHING for the day — a missed
shared room is worse than an empty result.**

**Fit score (0–100):** 1BR/1BA **and a WHOLE 2+ bedroom unit (house/flat)** are
BOTH top tier (big boost) — do NOT penalize a listing for having 2+ bedrooms;
the extra space is wanted. Spacious studio (≥450 sqft) = second tier;
small/cramped studio penalized; shared rooms/SROs rejected. A 2+ bed only earns
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
  "recommendation": "Strong match — email today to schedule a viewing."
}
```
`is_1br1ba` is literal (true only for an actual 1BR/1BA). A **whole 2+ bed** is
NOT a 1BR/1BA, so `is_1br1ba:false`, but it is still top-priority — give it a
**high `fit_score`** and set `room_type:"2br_plus"` (the dashboard badges it
"2+ Bed"). Don't conflate `is_1br1ba:false` with low fit.

## Scripts
- `scripts/fetch_listings.py` — multi-pass discovery → DB stubs.
- `scripts/fetch_detail.py` — parse post + download photos.
- `scripts/save_verdict.py` — persist your verdict.
- `scripts/notify.py` — Telegram cards (thresholded; scams blocked).
- `scripts/sync_supabase.py` — publish the minimal cloud read-model to Supabase.
- `scripts/serve.py` — local map dashboard of the LOCAL db (http://localhost:8000).
- `scripts/db.py` — SQLite schema + CLI (`init`, `list`, `show`, `set-status`).
- `dashboard/` — static public dashboard (Vercel) reading Supabase via anon key.

## Notes
- Be polite to Craigslist (the scripts set a UA + delay). Fetch details only for
  new, in-area posts.
- `lat/lng` come from the post and are block/neighborhood accurate, not exact.
- Future: add Zillow/Zumper adapters behind the same fetch → vet → dashboard flow.
