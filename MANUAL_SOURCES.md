# Zillow + Apartments.com + Zumper detail — gather BY HAND (no scripts, ever)

> Read this with CLAUDE.md (**BROWSER = MANUAL, ALWAYS** + the vetting rubric) and
> THUMB_RULES.md. **Every browser interaction with a listing site is manual.** Only the
> Craigslist search-list pull and the Zumper **map-API** pull are scripted (they hit
> plain HTTP endpoints, no browser). Everything that touches a rendered page —
> **Zillow, Apartments.com, the Zumper detail/body page, the Craigslist reply-contact
> reveal, and Craigslist Stage-3 flagging** — is driven BY HAND by the LLM, right after
> `refresh.py` finishes and before/around vetting.

## THE RULE (non-negotiable)
**There is NO scraper for Zillow, Apartments, Zumper detail pages, or CL contacts, and
there never will be. Do not write one, do not restore the deleted ones, do not spawn a
subagent to do it. You — the LLM in the main loop — open the pages yourself, screenshot
every step, and drive the browser one chromerpc action at a time.** The building-block
primitives live in `scripts/fetch_cl_contacts.py` (a primitives-ONLY module — no
`main`, no CLI, no loop); you compose them by hand, never in a batch.

Why this is a hard rule, not a preference:
- Both sites are bot-walled (Zillow = PerimeterX, Apartments = Akamai). A scraper
  has to either run JS (instantly detected → walled) or fake human input, and the
  moment the page markup changes the scraper silently breaks or starts lying. The
  old `fetch_zillow_cr.py` did exactly this and **broke** (it kept a `Runtime.Evaluate`
  JS call that the JS-free refactor removed → crashed every run).
- The honest signal is tiny. Filtered to **≤ price cap + last 24h**, each site
  returns only a **handful of new posts per day** (often 0–4). That is faster and
  more reliable to read by hand than to maintain a scraper for. It costs nothing
  (your own time in the browser), so there is no efficiency argument for a script.
- You have *better judgment than any scraper*: you can see a stolen-MLS watermark,
  a "150 sqft" room mislabeled "1 bed", or an avoid-area block, in one screenshot.

If you ever feel the urge to "just write a quick helper to parse the cards" — don't.
Read them by hand. A missed listing is cheap; a brittle scraper that fakes coverage
is not.

## What you MAY use (and may not)
- **Drive the LOCAL headful chromerpc** (`refresh.py` already launched it on :50051;
  if it's down, see "chromerpc" below). Headful is mandatory — headless is walled.
- **Interact ONLY through chromerpc's input gRPC** (the "trigger actions"): real
  mouse moves/clicks along human Bézier paths, real wheel/scroll gestures, real
  keystrokes. The low-level primitives already exist in `scripts/fetch_cl_contacts.py`
  (`navigate`, `human_click`, `_mouse`, `_call`, plus the DOM read helpers
  `_qs/_qsa/_outer/_center`). **Reuse those primitives as building blocks** — that is
  not "a scraper", it's you issuing one browser action at a time. Compose the
  sequence yourself, watching each result.
- **Use the CDP DOM domain ONLY to READ** — to find an element's click coordinates
  (`GetBoxModel`/`_center`) and to extract text/links/prices (`QuerySelectorAll`,
  `GetOuterHTML`). **Never use the DOM to interact** (no DOM clicks, no focus, no
  dispatching synthetic events) and **NEVER `Runtime.Evaluate` / inject JS** — that is
  what trips the bot walls. Drive like a human; read with the DOM, interact with input.
- **Screenshot constantly** (`CaptureScreenshot`). After every navigate / filter
  click / scroll, take a screenshot and actually look at it to decide the next move.
  Don't fly blind off the DOM alone.
- Go **slow and human**: warm up with a scroll, wait for loads (sleep a few seconds),
  scroll the listing into view, pause between actions. You are imitating a person
  browsing, not a bot hammering.

## The workflow (per site, per region: SF and Berkeley)
1. **Navigate** to the search, **warm up** (a human wheel-scroll or two), screenshot.
2. **Set the price filter** to the cap (config `max_price`, currently $2000) by
   clicking the filter UI (or, fine, a price-capped search URL). Screenshot to confirm.
3. **Sort by newest** (Zillow: "Sort → Newest"; Apartments: "Sort → Last Updated").
   It defaults to Recommended/Best-Match — you MUST change it or you'll read stale posts.
4. **Walk the results top-down.** Because it's newest-first, read each card's recency
   and **STOP at the first listing older than ~24h** (i.e. "2 days ago" or more —
   "Updated today / N hours ago / yesterday" are in; "2 days ago" is out). Read each
   in-scope card's link + price + beds/baths + type off the DOM.
5. **Open each in-scope listing slowly**: navigate to it, wait, scroll down with a
   scroll *gesture* (see gotcha below), screenshot the photos, and read the DOM for
   price / beds / baths / sqft / address / **description** / photos / contact.
6. **Vet inline** with the CLAUDE.md rubric (look at the photos for real; catch
   shared rooms, stolen-MLS clones, kitchenette-only studios, short-term sublets).
   Drop obvious junk; for keeps, write the canonical display fields yourself.
7. **Add keeps to the DB by hand**, then they ride the normal flow. Use
   `scripts/db.py` helpers from a short `python3 -c` (NOT a saved script):
   `insert_stub` → `update_detail` (set `source='zillow'`/`'apartments'`, beds/baths/
   sqft, address, **real lat/lng read off the page**, `image_urls` JSON, description)
   → `save_verdict` (scores + summary) → `set-status … vetted` → `mark_notified`.
   The area model classifies `avoid/caution/ok` from your coords at sync time, so get
   the coords right (read Zillow's embedded `"latitude"/"longitude"`, don't guess).
8. Then `scripts/sync_supabase.py` so they publish, and fold them into the same
   vetting digest / Stage-2 as everything else.

## Zumper detail — BY HAND too (map API gives a stub only)
`refresh.py` inserts each new Zumper listing as a **stub** (coords + price + photos +
raw map fields, `description=None`) — it no longer auto-drives the detail page. For
each new `source='zumper'` row with `description IS NULL`, gather the body BY HAND:
1. `navigate` to the listing URL, wait, `warmup`, screenshot.
2. Scroll-load every lazy section with real wheel/scroll **gestures** (Zumper shows
   "One sec, gathering the property details…" until scrolled) — screenshot until the
   ABOUT section renders.
3. DOM-read the rendered text; the `fetch_zumper.py` parsers `_about_from_text` /
   `_age_to_iso` / `_contact_from_text` (and `extract_description`) turn it into
   `{description, posted_at, contact}`. `db.update_detail(conn, pid, …)` to store them.
This body is what makes the SHARED-ROOM GATE work — Zumper tags room-shares as
"1 bedroom", so a stub with only photos WILL false-positive a private-room-in-a-house.

## Craigslist contact reveal + Stage-3 flagging — BY HAND
These are covered in CLAUDE.md (Refresh steps 4a + 4c) and use the same primitives and
the same one-step-at-a-time, screenshot-every-step, ONE-pass-per-listing discipline. CL
throttles repeated reply requests per IP, so if a reveal doesn't resolve, stop and retry
on a later run — never loop.

## Recency reality
- **Zillow** cards show "Updated today / N hours ago / Updated yesterday / N days ago".
  `homedetails/...` pages have a real **description** (lazy-loaded — scroll to load it,
  then read it from the DOM). **Unclaimed building pages** (`/apartments/...`, `/b/...`)
  are sparse: address + a fact or two, often no description — record what's there and
  move on.
- **Apartments.com** lists managed buildings and has **no reliable per-card timestamp**;
  "Last Updated" sort is the best you get. Its cheap inventory is mostly **rooms**
  (labeled "Room for Rent" — drop them) and **student by-the-bed co-living** (RUMI,
  TripaLink, Berkeley Group, Ace, Wesley House, etc. — per-bed prices = shared, drop)
  and **Tenderloin/SRO** studios (avoid area). Expect **few or zero keeps**. That's fine.

## Hard gotchas (learned the hard way — save yourself the rediscovery)
- **Scrolling: `mouseWheel` events do NOT scroll Zillow's detail overlays. Use the
  scroll *gesture*** — `cdp.input.InputService/SynthesizeScrollGesture` with a negative
  `yDistance` to go down (e.g. `{x:590,y:400,yDistance:-650,preventFling:true,speed:1400}`).
  This is what makes lazy-loaded descriptions appear.
- **Coordinates:** the chromerpc viewport is ~1200×773 at scale 1, so **screenshot
  pixels == click coordinates**. Still prefer locating a button via DOM
  (`_qsa` + match its text in `_outer`, then `_center`) and clicking that center — it
  survives layout shifts. `_center` returns `{cx,cy}`; pass `(cx,cy)` to `human_click`.
- **Screenshot recipe:** `CaptureScreenshot` wants `format: "SCREENSHOT_FORMAT_JPEG"`
  (or `_PNG`), returns base64 in `data`. Decode to a file and Read it.
- Dismiss Zillow's onboarding modals ("total monthly price", "commute times") and
  Apartments' AI-advisor tooltip before interacting.
- Both need a homepage **warm-up visit** first (sets the anti-bot cookie) before the
  search URL, especially Apartments (Akamai).

## chromerpc
`refresh.py` launches a headful chromerpc on :50051 and (if no prebuilt binary) clones
+ builds it into an OS temp dir; it leaves it running for this manual gather. If you
need it standalone: a prebuilt binary may exist beside the repo, else
`python3 -c "import sys;sys.path.insert(0,'scripts');import refresh;refresh.ensure_chromerpc()"`.
When ALL stages (manual gather + vetting + outreach) are done, tear it down with
`python3 scripts/refresh.py --teardown-chromerpc`.
