# Region profiles — run the house hunt for ANY city / region

The pipeline is region-agnostic. **One YAML file per search region** lives here, and
`config.yaml` → `active_region: <slug>` (or `$HH_REGION=<slug>` for one run) picks
which one a run targets. Every script reads the active profile through
`common.load_config()` (`cfg["region"]`).

| profile | what it is |
|---|---|
| `sf.yaml` | **Legacy** San Francisco (+ one-off Berkeley) — `legacy: true`. The SF/Berkeley rules stay **verbatim** in `config.yaml` (`areas`/`unsafe`/`caution`/`berkeley`) and in the scripts' original SF code paths. Never rewrite them into a profile. |
| `menlo_park.yaml` | Menlo Park + Atherton + Palo Alto + Redwood City + Portola Valley/Ladera (default since 2026-11-06). |

## Starting a run for a new region (agent playbook)

Runs are **agent-assisted**: the user says *where* and *what*, the agent (you) turns
that into a profile — no CLI flags to wire. When the user says something like
*"look for housing in and around <city>"*:

1. **Gather the user's inputs** (ask only for what is missing or ambiguous):
   - **city / region**, and which neighbouring towns count as "around" it;
   - **areas to avoid / treat with caution** (else YOU propose them — see step 2);
   - **price**: the real budget (`budget_target`) and how far to stretch the
     pull/surface cap (`max_price`). Check the local market first — if the
     budget is far below typical rent, say so and ask whether to stretch;
   - **unit types** wanted (default: 1BR/1BA primary, whole 2+ bed also top,
     spacious studio fallback, rooms/SROs rejected — the CLAUDE.md criteria);
   - whether to **email** (outreach) or only surface.
2. **Research the area model** with `WebSearch` (crime data + local reporting +
   Reddit), the same way the SF model was built. Propose `avoid` / `caution` towns
   or neighbourhoods to the user and cite sources in the YAML comments.
3. **Write `regions/<slug>.yaml`** (copy `menlo_park.yaml` — schema below), set
   `active_region: <slug>` in `config.yaml`, and smoke-test:
   `cd scripts && python3 -c "import common,geo; ..."` — classify a few known
   coords (one per tier, one outside `bounds`) before pulling.
4. **Fresh start?** If the user asks to start over: back up `data/listings.db` and
   a JSON export of the Supabase `listings` table to `data/backups/`, then
   clear both (cloud: `DELETE /rest/v1/listings?id=not.is.null` with the
   service-role key; local: delete the DB and `python3 scripts/db.py init`).
   Otherwise the regions coexist in one DB (each has its own watermarks).
5. **Run the normal Refresh cycle** from CLAUDE.md — `refresh.py` picks the
   profile's Craigslist + Zumper search and adds the `tools/locate.py`
   reverse-geocode step; the manual Zillow/Apartments gather walks the profile's
   `manual_sources` URLs. For a brand-new region, **don't stop at a watermark**:
   walk ALL current results (no posted-date cutoff) unless the user says otherwise.
6. Vetting, dedup, purge, sync, outreach are unchanged. Brief subagents with the
   profile's budget/area facts (e.g. "over `budget_target` but ≤ `max_price` =
   keep, flag *over budget* in `verdict_summary`").

## Schema (non-legacy profile)

```yaml
slug: menlo_park                 # file name; watermarks are keyed by it
label: "Menlo Park & nearby"     # display / default area name
state: CA
budget_target: 2000              # the user's real budget; above it = flagged "over budget"
max_price: 2300                  # pull + surface cap; above it = reject
wanted: {primary: [1br], also_top: [2br_plus], fallback: [studio], reject: [room, sro, shared]}
center: [lat, lng]               # map default
bounds: {lat: [min, max], lng: [min, max]}   # OBJECTIVE gate: coords outside = auto-reject
cities: [...]                    # in-scope towns (lowercase)
drop_cities: [...]               # discovery drops posts whose location text names these
default_area_name: "..."
reverse_geocode: true            # tools/locate.py stamps each row's real town (Nominatim)
areas:                           # display label + market-comp group; LONGEST keyword wins
  - {name: "Palo Alto", group: palo_alto, match: ["palo alto", "college terrace"]}
unsafe:  {names: [...], zones: [{name, lat, lng, radius_km}]}   # -> area_tier avoid (purged)
caution: {names: [...], zones: [...]}                           # -> caution (x0.7 match)
craigslist: {site: https://sfbay.craigslist.org, subarea: pen,
             params: {postal: 94025, search_distance: 6}}       # any extra CL query params
zumper: {box: {maxLat, minLat, maxLng, minLng}, url: menlo-park-ca,
         urls: [menlo-park-ca, palo-alto-ca, ...],   # API scopes pins to ONE city slug:
         page: https://www.zumper.com/apartments-for-rent/menlo-park-ca}  # list every town
manual_sources: {zillow: [urls], apartments: [urls]}            # walked BY HAND (chromerpc)
owner_lookup: none               # DataSF parcel check is SF-only; `none` = neutral block
```

What the profile changes (everything else is shared):
`common.load_config` overlays `max_price`, `budget_target`, `areas`, `unsafe`,
`caution`, `unspecified_area_name`; `geo.classify` skips the SF-only East Bay/BART
branch; `filters` + `fetch_detail` use `bounds`; `fetch_listings --region profile` /
`fetch_zumper --region profile` use the source blocks; `market_comps.area_group`
uses `areas[].group`; `research` uses `owner_lookup`; the dashboard frames whatever
coords it gets and filters by town. Craigslist outside the SF Bay site: set
`craigslist.site` (e.g. `https://seattle.craigslist.org`) and its `subarea`.
