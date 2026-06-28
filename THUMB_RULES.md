# THUMB_RULES.md — operating guardrails

Standing rules for this project. They override convenience: when a rule and a
quick path conflict, follow the rule. CLAUDE.md owns the *pipeline*; this owns the
*guardrails*. Read this before any fetch / sync / external API call.

## 1. Don't abuse paid APIs (the money is real)
- **All four sources are now FREE** (our own scrapers): Craigslist + Zumper over
  HTTP, Zillow + Apartments.com via the LOCAL headful **chromerpc** browser
  (`fetch_zillow_cr.py` / `fetch_apartments_cr.py`). Run any of them any time. The
  paid Apify Zillow actor (`fetch_zillow.py`) is RETIRED from the default flow —
  keep it only as a manual fallback; don't call it without a clear reason (it bills
  per result against the $5/mo credit).
- **Be polite to the bot-walled sites (Zillow PerimeterX, Apartments Akamai).**
  chromerpc MUST run headful (`-headless=false`) or they hard-block; and even then,
  pace like a human — the adapters warm up on the homepage, page through modestly,
  and cap new detail fetches per run (`--max-detail`). Don't loop aggressively or
  you'll re-trigger the captcha wall. Per-run dedup + blocklist mean steady-state
  runs only fetch genuinely-new listings.
- **Confirm an actor's output is REAL before trusting it or quoting its cost.**
  `epctex/apartments-scraper-api` returns `{"demo": true}` placeholders **and still
  bills** — we abandoned it. Inspect a couple of items' *content*, not just counts.
- **Reading a stored Apify dataset/run is FREE** (it's retrieving prior output, not
  re-scraping). To inspect results, read the dataset by run id via the API — do NOT
  re-run the actor.
- **Probe cheaply:** validate input/schema with the smallest `maxItems` (1–3) and a
  `maxTotalChargeUsd` ceiling. Read the actor's input-schema + pricing via the free
  metadata API before guessing the input shape.

## 2. Don't store unnecessary data (keep the DB + cloud tiny — both free-tier)
- **Images: store only the remote source URL** (`image_urls`), never the bytes.
  CL / Zumper / Zillow all hotlink the source CDN (dashboard embeds with
  `referrerpolicy="no-referrer"`). Local downloads in `data/images/<id>/` are
  TRANSIENT — for subagent vision-vetting only; `purge_images.py --all` after.
- **Don't upload verbatim post bodies** to the cloud; the dossier links out to the
  source post.
- **The cloud read-model is minimal** (one row per deduped unit: identity + display
  essentials + verdict). Don't add columns/blobs the dashboard doesn't render.
- **Purge aggressively:** `purge_db.py --execute` deletes rejected/removed +
  low-trust (<40) + unsafe-area rows and **blocklists their ids** so they're never
  re-pulled / re-vetted / re-billed.

## 3. Don't make decisions that cost us money on any platform
- **Stay on free tiers:** Vercel Hobby, Supabase free, Apify $5/mo credit. Never opt
  into paid compute / plans / higher tiers.
- **No redundant external calls.** Recency filters + dedup + blocklist exist to avoid
  re-fetching (and re-paying for) data we already have. Honor them.
- **Redeploy Vercel ONLY when `dashboard/` view code changes** — data-only updates
  need just `sync_supabase.py` (no build/redeploy). Deploy from the repo root
  (`vercel deploy --prod --yes`).
- **`WebSearch` / `WebFetch` cost us nothing** — prefer them for one-off lookups
  (market comps, cross-listing checks) over any paid scraper.

## 4. Fail safe + be honest about cost
- If an action could cost money and you're unsure, SAY SO and offer the cheap path
  first.
- Cap blast radius: small `maxItems`, a `maxTotalChargeUsd` ceiling, one probe
  before a batch.
- Report what was actually spent after running a paid actor.

## Daily routine (baked in)
- **Once/day (9am cron): full refresh, ALL sources incl. Zillow** — `refresh.py`
  (Zillow runs because it's the day's first), then vet → `apply_verdicts` → `dedup`
  → `purge_db --execute` → `sync_supabase` → `notify.py --new` → `purge_images --all`.
- **Any other refresh during the day: Craigslist + Zumper only** — `refresh.py`
  auto-skips Zillow (already pulled today). No flag needed.
