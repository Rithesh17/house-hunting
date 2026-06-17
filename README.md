# SF House-Hunting Assistant

A PC-local tool that finds affordable San Francisco rentals (1BR/1BA or spacious
studios, ≤ $2,000) in safe, park-adjacent neighborhoods — **driven by Claude
Code**. Claude scrapes Craigslist, *looks at the photos and reads each post with
its own vision* to weed out scams, scores how well each fits you, pushes the
winners to **Telegram**, and plots everything on a **local map dashboard**.

> You run Claude Code and say *"fetch the latest listings."* It does the rest.
> See [`CLAUDE.md`](CLAUDE.md) for the playbook Claude follows.

## One-time setup
1. **Install Python deps**
   ```
   py -m pip install -r requirements.txt
   ```
2. **Create the database**
   ```
   py scripts/db.py init
   ```
3. **Set up Telegram alerts** (optional but recommended)
   - In Telegram, message **@BotFather** → `/newbot` → copy the **token**.
   - Message your new bot once, then open
     `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your **chat id**
     (or message **@userinfobot**).
   - Copy `.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN` and
     `TELEGRAM_CHAT_ID`.

## Daily use
1. Open Claude Code in this folder and say **"fetch the latest listings."**
   Claude runs discovery → detail+photos → vets each one → Telegram → summary.
2. Browse everything on the map:
   ```
   py scripts/serve.py        # http://localhost:8000
   ```
   Sort by fit/legit/price, filter by type/neighborhood/status, open a card for
   the full gallery + scam notes, and mark listings interested / contacted /
   rejected.

## Search the dashboard
One search bar blends two signals into a single ranked list:

![Searching the property register — exact matches first, then related by meaning](docs/search-demo.gif)

- **Full-text first** — exact keyword hits across the title, neighborhood,
  address, and Claude's assessment/recommendation. These are the surest matches,
  so they rank at the top and appear instantly as you type.
- **Semantic next** — the rest of the list is filled in by *meaning*: listings
  that are close in concept to your query (e.g. *"sunset studio"* also surfaces
  garden studios near Ocean Beach) are ranked by confidence below the exact hits.
  Only confident matches are shown.

Semantic matching runs **entirely in your browser** (transformers.js + a small
MiniLM embedding model) — no backend, no API keys, nothing added to the cloud
database. The first search downloads the model (~25 MB, cached afterward) and
builds a vector index of the listings (cached in IndexedDB, so later visits are
instant); keyword results show immediately while that loads. When a search is
active, results are ranked by relevance and the Sort control is dimmed.

## Configuration
Edit [`config.yaml`](config.yaml): price cap, the list of areas (with Craigslist
`nh` neighborhood codes) and their weights, room-type passes, the spacious-studio
sqft floor, and the Telegram notify thresholds.

## Manual / debugging commands
```
py scripts/fetch_listings.py --area "Inner Richmond" --room 1br
py scripts/fetch_detail.py 7934851693
py scripts/save_verdict.py 7934851693 --json "{...}"
py scripts/notify.py 7934851693 --force
py scripts/db.py list --status vetted
py scripts/db.py show 7934851693
```

## How it works
`fetch_listings` runs one filtered pass per (area × room type) so each Craigslist
query is tight, then paginates the whole result set. `fetch_detail` parses the
post and downloads photos locally. **Claude** views the photos + description and
writes a verdict (legit + fit). `notify` sends non-scam winners to Telegram.
`serve` is a Flask app backed by SQLite that renders the Leaflet/OpenStreetMap
dashboard. Everything runs on your PC — no cloud.
