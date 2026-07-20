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

**Zumper page TYPES differ (learned 2026-07):**
- **`/apartment-buildings/p<id>/...` = an ILS multi-unit BUILDING page.** It has NO
  per-unit description body — the "property overview" / "About" widgets stay stuck on
  "One sec, gathering the property overview…" no matter how you scroll (they never
  hydrate to real text). What you CAN read is real and sufficient: the H1 name, the
  street address + neighborhood, the manager (e.g. "Bayview Property Managers"), the
  rent RANGE + bed range (e.g. "Studios–2, 1 bath, $1,750–$2,500"), "Updated N ago",
  and the photos. Record those, mark it a professionally-managed building (not a shared
  room), and move on — don't burn time trying to force the overview to load.
- **`/listings/<id>p/...` = a single-unit listing page** — this one DOES have a real
  About/description body; scroll (`F.scroll`) until it renders, then read it.
- Accept the OneTrust cookie banner first (`button#onetrust-accept-btn-handler`), and
  dismiss the "TAKE OUR SURVEY" popup, before reading.

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
  "Last Updated" sort is the best you get. **The `?sk=newest` (or any URL sort param) is
  IGNORED — the page silently stays on "Default" (Best Match).** You MUST set it in the
  UI: click the sort button (`#sortSearchIcon`, top-right), then in `ul.sortMenu` click the
  `li.searchResultSortOption` whose text is **"Last Updated"** (options: Default, Rent low→high,
  Rent high→low, Video, Virtual Tour, Last Updated). Confirm the card ORDER changed (the
  visible "Sort" label text in the DOM is unreliable — judge by the reordered cards).
  Because cards carry no date, open the top few and read the detail page (open-house dates
  in the body, or "Available <date>") to judge whether a listing is genuinely new-in-window;
  managed buildings re-"update" constantly, so newest-by-update ≠ newly-posted. Its cheap inventory is mostly **rooms**
  (labeled "Room for Rent" — drop them) and **student by-the-bed co-living** (RUMI,
  TripaLink, Berkeley Group, Ace, Wesley House, etc. — per-bed prices = shared, drop)
  and **Tenderloin/SRO** studios (avoid area). Expect **few or zero keeps**. That's fine.

## Hard gotchas (learned the hard way — save yourself the rediscovery)
- **Scrolling: `mouseWheel`/`DispatchMouseEvent` does NOT scroll ANY page** — chromerpc's
  Input proto drops the `deltaX/deltaY` fields, so CDP rejects the wheel with
  "'deltaX' and 'deltaY' are expected for mouseWheel event" and nothing moves (the old
  `warmup()` wheel loop was a silent no-op for this reason). **Use `F.scroll(dy)`** —
  the primitive added to `fetch_cl_contacts.py`, which calls
  `cdp.input.InputService/SynthesizeScrollGesture` (`y_distance = -dy`, so `dy>0` scrolls
  DOWN). e.g. `F.scroll(700)` a few times with `time.sleep` between to let lazy sections
  hydrate. `warmup()` now uses it too. Message fields are snake_case
  (`x, y, x_distance, y_distance, x_overscroll, y_overscroll, prevent_fling, speed,
  gesture_source_type`). This is what makes lazy-loaded descriptions appear.
- **Coordinates:** the chromerpc viewport is ~1200×773 at scale 1, so **screenshot
  pixels == click coordinates**. Still prefer locating a button via DOM
  (`_qsa` + match its text in `_outer`, then `_center`) and clicking that center — it
  survives layout shifts. `_center` returns `{cx,cy}`; pass `(cx,cy)` to `human_click`.
- **DB writes need `conn.commit()`** — `db.connect()` opens a plain sqlite3 connection
  with the default isolation level (implicit transaction, NO autocommit). `insert_stub`
  / `update_detail` / `save_verdict` / `set_status` do NOT commit internally, so a
  one-off `py -c "... db.update_detail(...)"` that just exits ROLLS BACK and silently
  loses the write. **Always end a store with `conn.commit()`** (and do all the stores in
  one process, committing once at the end). Verify with a follow-up SELECT.
- **`db.mark_notified(conn, post_id)` takes a SINGLE id string, not a list** — calling it
  with `[id]` raises "type 'list' is not supported" and aborts the script before commit.
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
