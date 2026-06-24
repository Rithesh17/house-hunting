# THUMB_RULES.md — operating guardrails

Standing rules for this project. They override convenience: when a rule and a
quick path conflict, follow the rule. CLAUDE.md owns the *pipeline*; this owns the
*guardrails*. Read this before any fetch / sync / external API call.

## 1. Don't abuse paid APIs (the money is real)
- **Apify bills per result/event** — real money, drawn from the **$5/mo free credit
  shared across all actors**. Keep usage minimal:
  - **Zillow (`igolaizola/zillow-scraper-ppe`) at most ONCE per calendar day.**
    `refresh.py` enforces this (skips Zillow if `last_pull_zillow` is today). Don't
    pass `--force-zillow` casually — only when a pull genuinely failed.
  - Always keep `--max-items` capped (default 40) **and** the recency filter on
    (`timeOnZillow`, auto-derived from the last pull). Never do a full / no-recency
    pull without a clear reason — and say so first.
  - **Craigslist + Zumper are free** (our own scrapers) — run those any time, even
    multiple times a day. Only **Zillow** is rate-limited for cost.
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
