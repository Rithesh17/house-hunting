# Outreach pipeline — 3 stages (read this with CLAUDE.md + THUMB_RULES.md)

The assistant can **reach out to landlords by email and triage their replies** —
fully inside the same cron-driven loop, orchestrated by Claude (the engine). Three
stages, all built on thin scripts + Claude's judgment. **RUN ORDER is 0 → 1 → 2:**
first read what's come back, then vet the new listings, then send new emails.

```
Stage 0  read + vet replies      (read_replies -> Claude matches + judges)
Stage 1  vet new listings        (subagents) + enrich + fetch_cl_contacts --all-vetted
                                  (relay + phone for EVERY kept CL row -> dashboard)
Stage 2  DECIDE who to email + send_email (CL-only; relays already captured in Stage 1)
                                  -> one combined Telegram digest at the end
```

## The single source of truth is the DB `status` — NO side-files
We do **not** keep any cross-run state files (no outreach log, no sent list). The
listing lifecycle lives entirely in the `status` column, which syncs to Supabase and
survives the hydrate round-trip, so it is consistent across every run and machine:

```
new -> vetted -> contacted -> interested        (rejected / removed are terminal)
```
- `vetted` — scored, kept, on the dashboard.
- `contacted` — we emailed the landlord (set by `send_email.py` on a successful send).
  This is the **resend guard, enforced at the UNIT level** (not just the row): a relist
  is a NEW post id, so `send_email.py` refuses to send when the SAME UNIT was already
  contacted — detected by the dedup cluster (`dup_group`), the same normalized street
  address, OR the same captured phone. So a repost (new id, different title, even a
  changed price) is never double-emailed. `--force` overrides. Re-vetting never
  downgrades it (`save_verdict` keeps non-`new` status). `tools/dedup.py` also folds
  price-changed reposts into one cluster (price-tolerant address/coord keys + a phone
  key) so the dashboard shows one tile and the guard has the cluster to check.
- `interested` — a reply came back and Claude judged it good (real unit, willing to
  show in person). Set by the orchestrator after Stage 0.

Because status rides through `sync_supabase.py` (it only skips `rejected`/`removed`)
and `hydrate_from_supabase.py` (preserves `status`), a fresh checkout or the next
cron tick already knows who we've contacted. **Always run `sync_supabase.py` after a
status change** so the cloud (and thus the next run) sees it.

## Stage 0 — read replies, then match + triage them (runs FIRST)
`python3 scripts/read_replies.py` pulls UNREAD inbox messages over Gmail IMAP and
keeps the ones that LOOSELY look rental-related (a reply, a Craigslist relay sender, a
street-address-ish string, or rental keywords). It deliberately does NOT hard-match a
reply to a listing — that's brittle, and a Claude instance is already here for Stage 0
to vet each reply, so **Claude does the matching too**. The script emits one JSON
object: `contacted_listings` (a compact reference of everyone we've emailed — id,
title, address, price, relay) and `replies` (the loosely-relevant inbound messages).
It reads ALL inbound mail **since the last run** (regardless of read/unread), so a
reply you already opened in Gmail is still covered. The cutoff is a DB-meta marker
(`last_reply_read`), advanced each run; messages are fetched with `BODY.PEEK` so this
never changes their Gmail read state. (First run looks back 7 days.) Re-vetting a
seen reply is harmless. `--since N` scans the last N days without moving the marker.

For each reply, **Claude matches it to a listing** (using the relay address, the
quoted subject/body, the address — semantic, not exact) **and judges it** (asymmetric,
same spirit as listing vetting):
- **Good (→ set `status='interested'` on the matched listing):** confirms it's
  available, offers/agrees to an in-person viewing, answers questions, normal terms.
- **Bad (→ leave/flag, do NOT advance):** asks for money / deposit / application fee /
  wire / gift card BEFORE any viewing; "I'm out of the country, can't show it"; refuses
  a live tour (a pre-recorded "video tour" or lockbox is NOT live); pushes off-platform
  immediately; asks for SSN or a big upfront fee.
- Mixed/unclear → surface it in the digest, no status change.

After judging, `python3 scripts/sync_supabase.py` publishes any `interested` flips.
Carry the per-reply verdicts forward — they become the "replies" lines of the single
digest at the end.

## Stage 1 — semantic detail entry (canonical fields, decided by the LLM)
The scraped fields are often wrong or empty: an address only in the body, a price
quoted weekly, a mis-tagged room type, a junk title. Since the vetting subagent has
**already read the body + photos + research bundle**, it is the best authority on the
real values — so it returns an `enrich` block and we **trust it over the scraped
value**. Only a handful of listings are vetted per run, so this is cheap.

The subagent adds `enrich` to its verdict with the CANONICAL value of any display
field it can correct (omit a field it can't improve):
```json
"enrich": {
  "title": "Bright 1BR in Inner Richmond",
  "price": 1800,                     // ALWAYS a real monthly number (convert weekly x ~4.33)
  "bedrooms": 1, "bathrooms": 1,
  "sqft": 600,                       // measured or photo-estimated
  "room_type": "1br",               // studio | 1br | 2br_plus | room
  "housing_type": "apartment",
  "area": "Inner Richmond",
  "neighborhood": "Inner Richmond",
  "address": "333 9th Ave, San Francisco, CA",
  "lat": 37.7765, "lng": -122.4660  // only if confidently known/geocoded
}
```
`tools/apply_verdicts.py` writes each present field over the stored value (with type
coercion). Notes:
- **Price is normalized to MONTHLY.** If the true monthly exceeds the $2,000 cap,
  the subagent ALSO sets `disposition:"reject"`, `reject_reason:"over $2000/mo cap"`.
- **Area follows the address.** Changing `area`/`neighborhood`/`address`/coords makes
  `sync_supabase.py` re-run `geo.classify`, so the avoid/caution/ok tier and the
  dashboard area update automatically. (Coords drive the unsafe-zone backstop, so set
  `lat`/`lng` when you geocode a newly-found address; otherwise name-matching still
  works off the corrected `area`/`neighborhood` text.)
- This is **detail correction, not re-judgement** — fit/trust scoring rules are
  unchanged (see CLAUDE.md). It only makes the dashboard show the true facts.

## Stage 2 — DECIDE who to email + send (Craigslist only)
Only Craigslist exposes a reply relay; Zillow/Zumper rows are surfaced for manual
contact, not auto-emailed. **The relay (+ phone) was already captured in Stage 1**
(`fetch_cl_contacts --all-vetted` grabs it for EVERY kept CL row), so Stage 2 does
NOT fetch contacts — it just PICKS which of the already-contactable listings to email
and sends. (Fetching ALL kept CL in Stage 1 means the dashboard shows contact info
for every good listing, not only the ones we auto-email — see CLAUDE.md step 4c.)

For each **new, qualifying** CL pick this run (see bar below) that is not yet
`contacted`:
1. **Relay** — it MUST already be on the row from Stage 1 (`reply_email`, captured by
   `fetch_cl_contacts --all-vetted`, which also auto-reveals any in-body "click for
   contact" phone/email). **Stage 2 does NO gathering** — if a relay is still missing
   (CL throttled it during Stage 1), SKIP that pick this run and just list it as
   "relay pending"; it is re-fetched on the next Stage-1 contact pass. Never run
   `fetch_cl_contacts` in Stage 2. (ALL information gathering — details, photos,
   contact email/phone, for EVERY site — happens in Stage 1.)
2. **Compose a human email** — read `sensitive/email_body.json` (NEVER committed),
   pick/adapt a template, inject the real post URL into `{post_url}`, and follow its
   `style_rules`: plain casual prose, NO em-dashes/emojis/semicolons, NO LLM-tells,
   2–6 short sentences, vary the wording, paste the link bare on its own line. Always
   include: interest + ask if available, ask for an in-person viewing, move-in ~Aug
   1, a SHORT about-me, the post link. Subject: `Interested in this: <address>` (or
   the listing title). Never share PII beyond the first name.
3. **Send** — `python3 scripts/send_email.py --auto --listing <id> --subject "..."
   --body-file /tmp/<id>.txt`. It sends via authenticated Gmail SMTP (real
   SPF/DKIM/DMARC, no bot headers) and flips the listing to `contacted`. `--auto`
   makes it send ONLY if `OUTREACH_AUTOSEND=1` in `.env` (else it refuses + says why
   — note the pick as "ready"). The unit-level guard makes a re-send a no-op. Use
   `--dry-run` to preview. (Manual sends omit `--auto` and always proceed.)
4. `python3 scripts/sync_supabase.py` so `contacted` lands in the cloud.

**Auto-send bar (conservative).** Email only picks that are: non-scam
(`legit_label != likely-scam`), `area_tier == ok`, `fit_score >= 80`,
`legit_score >= 70`, **`room_type == "1br"` (a real 1 bed / 1 bath unit ONLY)**,
Craigslist source, status `vetted` (never re-contact). **Cap 2 per run.** Scams are
never emailed. When in doubt, don't send — a missed email is cheap; a bad one isn't.

**1BR/1BA-ONLY auto-send (enforced in code).** Auto-send is restricted to 1 bed /
1 bath units. **Studios and 2+ bed are surfaced on the dashboard at full score
(their scores are NOT penalized) but are NEVER auto-emailed** — contact those
manually if wanted. `send_email.py --auto` refuses any non-`1br` listing (a hard
guard alongside the `OUTREACH_AUTOSEND` gate); a manual send omits `--auto` and is
unaffected, so you can still email a great studio when you choose to.

**Auto-send is OPT-IN, gated in code (not the prompt).** `send_email.py --auto` reads
`OUTREACH_AUTOSEND` from `.env` (via `load_dotenv`, NOT the shell env — so setting it
in `.env` is what counts):
- `OUTREACH_AUTOSEND` unset / `0` (DEFAULT): `--auto` refuses to send; the cron fetches
  the relay and lists the ready-to-email picks in the digest only. You send the ones
  you want manually (the `send_email.py` line, without `--auto`).
- `OUTREACH_AUTOSEND=1` in `.env`: the cron sends to qualifying picks automatically
  (still capped at 2/run + scam-blocked + unit-level one-per-listing).

## The single combined Telegram digest (ONE message, shorthand)
Send exactly **ONE** Telegram message at the very end of the pass — never separate
pings per stage. Claude composes it from the three things it gathered this run and
sends it once with `python3 scripts/notify.py --message "<text>"`:
1. **New picks** — get them with `python3 scripts/notify.py --list-new` (qualifying,
   un-notified, deduped primaries as JSON; no send, no mark). Render each as one
   shorthand line: name · $price/type · area · 🟢/🟡 trust · ⭐ fit · `#id` link.
2. **Emails** — one shorthand line per pick contacted/ready this run ("✉️ sent" or
   "✉️ ready: <id>").
3. **Replies** — one shorthand line per reply with Claude's Stage-0 verdict
   (👍 good / ⚠️ wants money first / 🤔 unclear) + which listing.

Compose all three into a single short text (skip an empty section; if ALL three are
empty, stay silent — same spirit as `--quiet-if-empty`), end it with the dashboard
link, then send the ONE message. **After it sends, mark the included new picks
notified** so they don't repeat next run: `python3 scripts/notify.py --mark-notified
<id> <id> ...`. Never include images. Scams never appear.

(`notify.py --new` still exists for a standalone picks-only digest, but the outreach
cron does NOT use it — it would be a second ping. Use `--list-new` + one `--message`.)

## Prerequisites / guardrails
- `.env`: `GMAIL_USER` + `GMAIL_APP_PASSWORD` (a Gmail **App Password**, 2FA on — not
  the account password). Same credential is used for SMTP send and IMAP read.
- `sensitive/email_body.json` holds the private outreach details + style — **NEVER
  commit it** (it is gitignored under `sensitive/`).
- Stage 2 needs chromerpc running locally for the relay fetch; it's best-effort and
  skipped (logged) when down.
- Volume stays tiny by construction (only new top picks, one email per listing ever,
  cap 2/run), which is what keeps a real Gmail account in good standing.
