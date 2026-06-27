/* The San Francisco Property Register — interactions */
let LISTINGS = [], MARKERS = [], MAP = null, DMAP = null, VIEW = "list", PAGE = 1;
const PAGE_SIZE = 24;

// Walk-to-BART metric (the user commutes by BART). Official gtfs coords from
// api.bart.gov — Berkeley stations + the SF commute corridor (south SF + downtown).
const BART_STATIONS = [
  ["North Berkeley", 37.873967, -122.283440], ["Downtown Berkeley", 37.870104, -122.268133],
  ["Ashby", 37.852803, -122.270062], ["Daly City", 37.706121, -122.469081],
  ["Balboa Park", 37.721585, -122.447506], ["Glen Park", 37.733064, -122.433817],
  ["24th St Mission", 37.752470, -122.418143], ["16th St Mission", 37.765062, -122.419694],
  ["Civic Center", 37.779732, -122.414123], ["Powell St", 37.784471, -122.407974],
  ["Montgomery St", 37.789405, -122.401066], ["Embarcadero", 37.792874, -122.397020],
];
function nearestBart(lat, lng) {
  if (lat == null || lng == null) return null;
  const R = 6371, rad = (d) => (d * Math.PI) / 180;
  let best = null;
  for (const [name, sla, sln] of BART_STATIONS) {
    const dp = rad(sla - lat), dl = rad(sln - lng);
    const a = Math.sin(dp / 2) ** 2 + Math.cos(rad(lat)) * Math.cos(rad(sla)) * Math.sin(dl / 2) ** 2;
    const km = 2 * R * Math.asin(Math.sqrt(a));
    if (!best || km < best.km) best = { name, km };
  }
  if (!best) return null;
  const mi = best.km * 0.621371;
  return `${best.km.toFixed(2)} km (${mi.toFixed(1)} mi) to ${best.name} BART`;
}
const SF = [37.7749, -122.4194];

const TYPE = {
  "1br":    { label: "One Bed", kind: "1 BD / 1 BA" },
  "studio": { label: "Studio",  kind: "Studio" },
  "2br_plus": { label: "2+ Bed", kind: "2+ Bed" },
  "unknown": { label: "Home",   kind: "Home" },
};
function typeOf(d) { return TYPE[d.room_type] || TYPE.unknown; }
const TEMOJI = { "likely-legit": "🟢", "unverified-amateur": "🟡", "likely-scam": "🔴" };
const SOURCE = { craigslist: "Craigslist", zumper: "Zumper" };
function sourceLabel(s) { return SOURCE[s] || (s ? s[0].toUpperCase() + s.slice(1) : "Listing"); }
let SELECTED_AREAS = new Set();

/* ----------------------------------------------------------- search state */
// ONE search box, two purely text-based signals (both instant, no network/model):
//   1. full-text  — rows that contain EVERY query term (ranked first)
//   2. fuzzy      — rows that contain SOME query term (ranked below)
// Both are ordered by a simple relevance score (title hits weighted over body).
let SEARCH_Q = "";            // current query text

/* The text we search over. The verbatim post body is NOT in the cloud (by
 * design), so we index the fields we DO have: title, area, address + Claude's
 * assessment/recommendation/flags. Good enough to find a place by feature. */
function searchText(d) {
  return [d.title, d.area, d.neighborhood, d.address, d.verdict_summary,
          d.recommendation, Array.isArray(d.red_flags) ? d.red_flags.join(" ") : "",
          typeOf(d).kind]
    .filter(Boolean).join(" · ");
}
function textRelevance(d, toks) {
  const title = (d.title || "").toLowerCase(), body = searchText(d).toLowerCase();
  let s = 0; for (const tk of toks) { if (title.includes(tk)) s += 3; else if (body.includes(tk)) s += 1; }
  return s;
}

/* Semantic color: green→amber→red by SCORE (used for match + trust, not type). */
// Match-quality tint for the card accent. NO red — red is reserved for genuine
// scam/flag/reject signals only; a merely-lower match should not read as an alarm.
function scoreColor(s) {
  if (s == null) return "#b3a892";
  if (s >= 80) return "#2f7d4f";   // strong
  if (s >= 60) return "#6f7a39";   // good
  if (s >= 40) return "#9c8f54";   // fair
  return "#b3a892";                // low — muted, not alarming
}

const TRUST = {
  "likely-legit":       { cls: "t-legit",   label: "Verified" },
  "unverified-amateur": { cls: "t-amateur", label: "Unverified" },
  "likely-scam":        { cls: "t-scam",    label: "Flagged" },
};
function trust(d) { return TRUST[d.legit_label] || { cls: "t-amateur", label: "Unrated" }; }

/* Area model (3 tiers): prime ('ok') areas are a level field ranked by match;
 * 'caution' areas (okay-but-not-prime) are surfaced but match-discounted and rank
 * as a GROUP below all prime areas; 'avoid' (unsafe) sinks to the very bottom.
 * No favorite/preferred weighting WITHIN the prime tier. */
const isAvoid = (d) => d.area_tier === "avoid";
const isCaution = (d) => d.area_tier === "caution";
/* Group rank for ordering: 0 = prime, 1 = caution, 2 = unsafe (bottom). */
const areaRank = (d) => (isAvoid(d) ? 2 : isCaution(d) ? 1 : 0);

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ----------------------------------------------------------- load */
/* Reads directly from Supabase (PostgREST) with the public anon key. One row
 * per deduped unit; photos come from remote `image_urls`, and the unit's other
 * source posts are embedded in `sources`. Read-only — no backend. */
async function load() {
  const cfg = window.DASHBOARD_CONFIG || {};
  const url = `${cfg.SUPABASE_URL}/rest/v1/listings?select=*`;
  try {
    const res = await fetch(url, {
      headers: { apikey: cfg.SUPABASE_ANON_KEY,
                 Authorization: `Bearer ${cfg.SUPABASE_ANON_KEY}` },
    });
    const rows = await res.json();
    LISTINGS = (Array.isArray(rows) ? rows : []).map(normalize);
  } catch (e) {
    LISTINGS = [];
    document.getElementById("list").innerHTML =
      `<p class="empty">Couldn't reach the listings service. Try again shortly.</p>`;
  }
  buildAreaMenu();
  render();
  openFromHash();
}

/* Shape a cloud row to what the renderer expects. */
function normalize(d) {
  d.photos = Array.isArray(d.image_urls) ? d.image_urls : [];
  d.red_flags = Array.isArray(d.red_flags) ? d.red_flags : [];
  const srcs = Array.isArray(d.sources) ? d.sources : [];
  // `sources` includes the primary; expose the OTHER posts as `duplicates`
  // (the dossier rebuilds the full best-first list from primary + duplicates).
  d.duplicates = srcs.filter((s) => s.url && s.url !== d.url);
  return d;
}

function buildAreaMenu() {
  const areas = [...new Set(LISTINGS
    .filter((d) => d.status !== "rejected" && d.status !== "removed")
    .map((d) => d.area).filter(Boolean))].sort();
  const menu = document.getElementById("area-menu");
  menu.innerHTML = areas.map((a) =>
    `<label class="area-opt"><input type="checkbox" value="${esc(a)}"${SELECTED_AREAS.has(a) ? " checked" : ""}> ${esc(a)}</label>`
  ).join("") + `<button type="button" id="area-clear" class="area-clear">Clear all</button>`;
  menu.querySelectorAll("input[type=checkbox]").forEach((cb) =>
    cb.addEventListener("change", () => {
      cb.checked ? SELECTED_AREAS.add(cb.value) : SELECTED_AREAS.delete(cb.value);
      updateAreaBtn(); PAGE = 1; render();
    }));
  document.getElementById("area-clear").addEventListener("click", () => {
    SELECTED_AREAS.clear();
    menu.querySelectorAll("input[type=checkbox]").forEach((cb) => (cb.checked = false));
    updateAreaBtn(); PAGE = 1; render();
  });
}
function updateAreaBtn() {
  const n = SELECTED_AREAS.size;
  document.getElementById("area-btn").textContent =
    (n ? `${n} area${n > 1 ? "s" : ""}` : "All areas") + " ▾";
}

function filters() {
  return {
    sort: val("sort"), type: val("filter-type"),
    maxPrice: +val("filter-price"),   // range slider, 0..2000
    minLegit: +val("filter-legit"), minFit: +val("filter-fit"),
    status: val("filter-status"),
    hideScams: document.getElementById("hide-scams").checked,
    showRejected: document.getElementById("show-rejected").checked,
  };
}
const val = (id) => document.getElementById(id).value;

/* Live value labels for the range sliders (price / trust / match). */
function syncRangeLabels() {
  document.getElementById("price-val").textContent = "$" + val("filter-price");
  const lv = +val("filter-legit"), fv = +val("filter-fit");
  document.getElementById("legit-val").textContent = lv ? lv + "+" : "any";
  document.getElementById("fit-val").textContent = fv ? fv + "+" : "any";
}

function matchesType(d, t) {
  if (!t) return true;
  if (t === "others") return d.room_type !== "1br" && d.room_type !== "studio";
  return d.room_type === t;
}

function selection() {
  const f = filters();
  let out = LISTINGS.filter((d) => {
    const hidden = d.status === "rejected" || d.status === "removed";
    if (hidden && !f.showRejected && f.status !== d.status) return false;
    if (!matchesType(d, f.type)) return false;
    if ((d.price ?? 0) > f.maxPrice) return false;
    if (f.minLegit && (d.legit_score ?? 0) < f.minLegit) return false;
    if (f.minFit && (d.fit_score ?? 0) < f.minFit) return false;
    if (f.status && d.status !== f.status) return false;
    if (f.hideScams && d.legit_label === "likely-scam") return false;
    if (SELECTED_AREAS.size && !SELECTED_AREAS.has(d.area)) return false;
    return true;
  });

  // Unified text search (instant): full-text (rows with EVERY term) rank first,
  // then fuzzy (rows with SOME term) below them. Within each tier we order by a
  // simple relevance score; unsafe ("avoid") areas still sink to the bottom.
  const q = SEARCH_Q.trim();
  if (q) {
    const toks = q.toLowerCase().split(/\s+/).filter(Boolean);
    const hits = (d) => { const t = searchText(d).toLowerCase(); return toks.filter((tk) => t.includes(tk)).length; };
    out = out.map((d) => ({ d, n: hits(d) })).filter((x) => x.n > 0);  // drop rows matching no term
    const full = (x) => (x.n === toks.length ? 0 : 1);                 // 0 = all terms, 1 = fuzzy/some
    const rel = (x) => textRelevance(x.d, toks);
    const aRank = (x) => areaRank(x.d);                               // caution below prime, avoid at bottom
    return out
      .sort((a, b) => aRank(a) - aRank(b) || full(a) - full(b) || rel(b) - rel(a))
      .map((x) => x.d);
  }

  // Area groups gate the chosen sort: unsafe ("avoid") areas always sink to the
  // bottom regardless of the selected key; every other area is a level field.
  // Within each group we use that key — by default match score.
  const byKey = {
    match: (a, b) => (b.fit_score ?? -1) - (a.fit_score ?? -1),
    legit: (a, b) => (b.legit_score ?? -1) - (a.legit_score ?? -1),
    "price-asc": (a, b) => (a.price ?? 1e9) - (b.price ?? 1e9),
    "price-desc": (a, b) => (b.price ?? -1) - (a.price ?? -1),
    newest: (a, b) => (b.first_seen_at || "").localeCompare(a.first_seen_at || ""),
  }[f.sort] || ((a, b) => (b.fit_score ?? -1) - (a.fit_score ?? -1));
  return out.sort((a, b) => areaRank(a) - areaRank(b) || byKey(a, b));
}

function render() {
  // When a search is active, results are ranked by relevance — the Sort control
  // doesn't apply, so dim it for clarity.
  const searching = !!SEARCH_Q.trim();
  const sortField = document.getElementById("sort").closest(".field");
  if (sortField) {
    sortField.style.opacity = searching ? ".45" : "";
    sortField.title = searching ? "Search results are ranked by relevance" : "";
  }
  const items = selection();
  document.getElementById("count").textContent = searching
    ? `${items.length} match${items.length === 1 ? "" : "es"}`
    : `${items.length} of ${LISTINGS.length} entries on file`;
  renderLegend();
  const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  if (PAGE > pages) PAGE = pages;
  const pageItems = items.slice((PAGE - 1) * PAGE_SIZE, PAGE * PAGE_SIZE);
  renderList(pageItems);
  renderPager(items.length, pages);
  if (MAP) renderMarkers(items);  // map shows ALL matches, not just the page
}

function renderPager(total, pages) {
  const el = document.getElementById("pager");
  if (VIEW !== "list" || total <= PAGE_SIZE) { el.innerHTML = ""; return; }
  const from = (PAGE - 1) * PAGE_SIZE + 1, to = Math.min(PAGE * PAGE_SIZE, total);
  el.innerHTML = `
    <button id="pg-prev" ${PAGE <= 1 ? "disabled" : ""}>← Prev</button>
    <span class="pg-info">${from}–${to} of ${total} · page ${PAGE}/${pages}</span>
    <button id="pg-next" ${PAGE >= pages ? "disabled" : ""}>Next →</button>`;
  const prev = document.getElementById("pg-prev"), next = document.getElementById("pg-next");
  if (prev) prev.onclick = () => { if (PAGE > 1) { PAGE--; render(); scrollTop(); } };
  if (next) next.onclick = () => { if (PAGE < pages) { PAGE++; render(); scrollTop(); } };
}
function scrollTop() { document.getElementById("list").scrollTo({ top: 0, behavior: "smooth" }); window.scrollTo({ top: 0, behavior: "smooth" }); }

/* ----------------------------------------------------------- legend */
function renderLegend() {
  const counts = { "1br": 0, studio: 0, other: 0 };
  for (const d of LISTINGS) {
    if (d.status === "rejected") continue;
    counts[d.room_type === "1br" ? "1br" : d.room_type === "studio" ? "studio" : "other"]++;
  }
  const sw = (c) => `<span class="swatch" style="background:${c};border:none"></span>`;
  document.getElementById("legend").innerHTML = `
    <span class="lg"><b>Types:</b></span>
    <span class="lg">One&nbsp;Bed · ${counts["1br"]}</span>
    <span class="lg">Studio · ${counts.studio}</span>
    <span class="lg">2+&nbsp;Bed · ${counts.other}</span>
    <span class="lg" style="margin-left:auto"><b>Match:</b> ${sw("#2f7d4f")}80+ ${sw("#7a9a2e")}60+ ${sw("#c08a1a")}40+ ${sw("#b23222")}low</span>
    <span class="lg"><b>Trust:</b> 🟢 verified&nbsp; 🟡 unverified&nbsp; 🔴 flagged</span>`;
}

/* ----------------------------------------------------------- list */
function renderList(items) {
  const el = document.getElementById("list");
  if (!items.length) { el.innerHTML = `<p class="empty">No entries match the current filters.</p>`; return; }
  el.innerHTML = "";
  items.forEach((d, i) => {
    const t = typeOf(d), tr = trust(d), scam = d.legit_label === "likely-scam";
    const photo = d.photos && d.photos[0];
    const specs = [];
    if (d.bedrooms != null) specs.push(d.bedrooms ? `${d.bedrooms} BR` : "Studio");
    if (d.bathrooms != null) specs.push(`${d.bathrooms} BA`);
    specs.push(d.sqft ? `~${d.sqft} ft²` : "ft² n/a");
    const fit = d.fit_score ?? 0;
    const card = document.createElement("article");
    card.className = "card" + (scam ? " flagged" : "") + (isAvoid(d) ? " offarea" : "");
    card.dataset.id = d.id;
    card.style.setProperty("--accent", scoreColor(d.fit_score));
    card.style.animationDelay = `${Math.min(i * 45, 600)}ms`;
    card.innerHTML = `
      <div class="ph ${photo ? "" : "none"}">
        ${photo ? `<img src="${photo}" loading="lazy" referrerpolicy="no-referrer" alt="" />` : "no photograph on file"}
        <span class="idx">NO. ${String(i + 1).padStart(2, "0")}</span>
        <span class="typeflag">${t.label}</span>
        <span class="seal ${tr.cls}">${tr.label} ${d.legit_score ?? "—"}</span>
        ${scam ? `<span class="stamp">Flagged</span>` : ""}
        <span class="priceplate"><span class="amt">$${(d.price ?? 0).toLocaleString()}</span> <span class="per">/ month</span></span>
        ${d.photos && d.photos.length > 1 ? `<span class="photocount">${d.photos.length} photos</span>` : ""}
      </div>
      <div class="body">
        <h3 class="title">${esc(d.title || "Untitled entry")}</h3>
        <div class="hood">${esc(d.area || d.neighborhood || "San Francisco")}
          ${isAvoid(d) ? `<span class="prox avoid">unsafe area</span>`
            : isCaution(d) ? `<span class="prox caution">secondary area</span>` : ""}
          <span class="srctag">${sourceLabel(d.source)}${d.dup_count > 1 ? " +" + (d.dup_count - 1) : ""}</span></div>
        <div class="specs">
          ${specs.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}
          ${d.dup_count > 1 ? `<span class="chip dup">${d.dup_count} posts</span>` : ""}
        </div>
        <div class="match">
          <div class="lab"><span>Match score</span> <b>${fit}<span style="font-size:11px;color:var(--ink-faint)">/100</span></b></div>
          <div class="bar"><i style="width:${fit}%"></i></div>
        </div>
      </div>`;
    card.addEventListener("click", () => openModal(d.id));
    el.appendChild(card);
  });
}

/* ----------------------------------------------------------- map */
function initMap() {
  MAP = L.map("map", { scrollWheelZoom: true }).setView(SF, 12.4);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
    maxZoom: 20, attribution: "© OpenStreetMap, © CARTO",
  }).addTo(MAP);
}
function renderMarkers(items) {
  MARKERS.forEach((m) => MAP.removeLayer(m));
  MARKERS = [];
  const dot = { "likely-legit": "#3a7d44", "unverified-amateur": "#b9821b", "likely-scam": "#b23222" };
  for (const d of items) {
    if (d.lat == null || d.lng == null) continue;
    const scam = d.legit_label === "likely-scam";
    const icon = L.divIcon({
      className: "",
      html: `<div class="pin ${scam ? "flagged" : ""}" style="background:${scam ? "" : scoreColor(d.fit_score)}">
               <span class="tdot" style="background:${dot[d.legit_label] || "#b9821b"}"></span>
               $${(d.price ?? 0).toLocaleString()}</div>`,
      iconSize: [0, 0], iconAnchor: [0, 0],
    });
    const m = L.marker([d.lat, d.lng], { icon, riseOnHover: true }).addTo(MAP);
    m.bindPopup(`<div class="pop"><div class="pprice">$${(d.price ?? 0).toLocaleString()}</div>
      <div class="ptitle">${esc((d.title || "").slice(0, 70))}</div>
      <a href="#" onclick="openModal('${d.id}');return false;">Open dossier →</a></div>`);
    MARKERS.push(m);
  }
}

function setView(view) {
  VIEW = view;
  document.getElementById("view-list").classList.toggle("active", view === "list");
  document.getElementById("view-map").classList.toggle("active", view === "map");
  document.getElementById("view-featured").classList.toggle("active", view === "featured");
  document.getElementById("list").classList.toggle("hidden", view !== "list");
  document.getElementById("map").classList.toggle("hidden", view !== "map");
  document.getElementById("featured").classList.toggle("hidden", view !== "featured");
  // filters only apply to the Index/Atlas; dim them on the curated Featured page
  document.querySelector(".filters").classList.toggle("muted", view === "featured");
  if (view === "map") {
    if (!MAP) initMap();
    setTimeout(() => { MAP.invalidateSize(); render(); }, 60);
  } else if (view === "featured") {
    renderFeatured();
  } else {
    render();
  }
}

/* ----------------------------------------------------------- featured */
/* The best of the ledger by BOTH scores: legit + fit must each clear a bar.
 * Unsafe areas are excluded; the rest are ranked by combined strength (then
 * price). A short, curated page. */
function featuredItems() {
  return LISTINGS
    .filter((d) => d.status !== "rejected" && d.status !== "removed" &&
      d.legit_label !== "likely-scam" && !isAvoid(d) && !isCaution(d) &&
      (d.fit_score ?? 0) >= 70 && (d.legit_score ?? 0) >= 70)
    .sort((a, b) =>
      ((b.fit_score + b.legit_score) - (a.fit_score + a.legit_score)) ||  // best match+trust
      ((a.price ?? 1e9) - (b.price ?? 1e9)))
    .slice(0, 12);
}

function renderFeatured() {
  const el = document.getElementById("featured");
  const items = featuredItems();
  if (!items.length) {
    el.innerHTML = `<p class="empty">No entries clear the featured bar yet
      (needs trust ≥ 70 and match ≥ 70).</p>`;
    return;
  }
  el.innerHTML = `
    <div class="feat-head">
      <h2>Featured</h2>
      <p>The strongest entries on file — high match <em>and</em> high trust in
        safe areas, best first. ${items.length} of them.</p>
    </div>
    <div class="feat-grid">` +
    items.map((d, i) => {
      const t = typeOf(d), tr = trust(d);
      const photo = d.photos && d.photos[0];
      const specs = [];
      if (d.bedrooms != null) specs.push(d.bedrooms ? `${d.bedrooms} BR` : "Studio");
      if (d.bathrooms != null) specs.push(`${d.bathrooms} BA`);
      if (d.sqft) specs.push(`~${d.sqft} ft²`);
      return `
      <article class="featcard" data-id="${esc(d.id)}" style="--accent:${scoreColor(d.fit_score)}">
        <div class="featrank">${String(i + 1).padStart(2, "0")}</div>
        <div class="featph ${photo ? "" : "none"}">
          ${photo ? `<img src="${photo}" loading="lazy" referrerpolicy="no-referrer" alt="" />`
                  : "no photograph"}
          <span class="featprice">$${(d.price ?? 0).toLocaleString()}</span>
        </div>
        <div class="featbody">
          <h3>${esc(d.title || "Untitled entry")}</h3>
          <div class="feathood"><span class="pin">◈</span> ${esc(d.area || d.neighborhood || "San Francisco")}
            <span class="srctag">${esc(sourceLabel(d.source))}</span></div>
          <div class="featspecs"><span class="chip kind">${esc(t.kind)}</span>
            ${specs.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}</div>
          <div class="featscores">
            <span class="featpill"><b>${d.legit_score ?? "—"}</b> trust ${TEMOJI[d.legit_label] || "⚪"}</span>
            <span class="featpill"><b>${d.fit_score ?? "—"}</b> match ⭐</span>
          </div>
          ${d.verdict_summary ? `<p class="featsum">${esc(d.verdict_summary)}</p>` : ""}
          ${d.recommendation ? `<p class="featrec">→ ${esc(d.recommendation)}</p>` : ""}
        </div>
      </article>`;
    }).join("") + `</div>`;
  el.querySelectorAll(".featcard").forEach((c) =>
    c.addEventListener("click", () => openModal(c.dataset.id)));
}

/* Open a listing's dossier straight from a #id=<id> deep link (Telegram). */
function openFromHash() {
  const m = (location.hash || "").match(/id=([^&]+)/);
  if (!m) return;
  const id = decodeURIComponent(m[1]);
  if (LISTINGS.find((x) => x.id === id)) openModal(id);
}

/* ----------------------------------------------------------- dossier modal */
/* Stage-2 cross-check (DRE / ownership / price / duplicates), when present. Each
 * entry is {outcome, note}; outcome drives the icon. Verification only ever adds
 * confidence — absence of it is neutral, not negative. */
function verificationHtml(v) {
  if (!v || typeof v !== "object") return "";
  const MARK = { verified: "ok", match: "ok", boost: "ok", ok: "ok", plausible: "ok",
    confirmed: "ok", flag: "bad", mismatch: "bad", scam: "bad", fraud: "bad",
    implausible: "bad", flood: "bad", neutral: "na", unverified: "na", unknown: "na" };
  const GLYPH = { ok: "✓", bad: "✕", na: "–" };
  const rows = Object.entries(v).map(([k, val]) => {
    const o = (val && typeof val === "object") ? val : { note: String(val) };
    const cls = MARK[(o.outcome || "").toLowerCase()] || "na";
    const note = esc(o.note || o.outcome || "");
    return `<li><span class="vmark ${cls}">${GLYPH[cls]}</span> <b>${esc(k)}</b>${note ? ": " + note : ""}</li>`;
  }).join("");
  return rows ? `<div class="infoblock verif"><h4>Verification</h4><ul>${rows}</ul></div>` : "";
}

function openModal(id) {
  const d = LISTINGS.find((x) => x.id === id);
  if (!d) return;
  const t = typeOf(d), tr = trust(d);
  const gallery = (d.photos || []).map((p) => `<img src="${p}" alt="" referrerpolicy="no-referrer" onclick="window.open('${p}','_blank')" />`).join("");
  const flags = (d.red_flags && d.red_flags.length)
    ? `<div class="infoblock flags"><h4>Red flags</h4><ul>${d.red_flags.map((f) => `<li>${esc(f)}</li>`).join("")}</ul></div>` : "";
  const verif = verificationHtml(d.verification);
  const infoCols = [verif, flags].filter(Boolean);
  const infoHtml = infoCols.length
    ? `<div class="info-grid cols-${infoCols.length}">${infoCols.join("")}</div>` : "";
  const specs = [];
  if (d.bedrooms != null) specs.push(d.bedrooms ? `${d.bedrooms} Bed` : "Studio");
  if (d.bathrooms != null) specs.push(`${d.bathrooms} Bath`);
  specs.push(d.sqft ? `~${d.sqft} ft²` : "size n/a");

  // all source posts for this unit (primary + duplicate reposts), best first
  const sources = [
    { url: d.url, price: d.price, fit_score: d.fit_score, legit_score: d.legit_score,
      legit_label: d.legit_label, area: d.area, source: d.source, primary: true },
    ...(d.duplicates || []),
  ].sort((a, b) => (b.fit_score ?? -1) - (a.fit_score ?? -1));
  const nSites = new Set(sources.map((s) => s.source)).size;
  const emailMatch = (d.contact || "").match(/[\w.+-]+@[\w-]+\.[\w.-]+/);
  // prefer the relay email we fetched via the reply flow, else any email in the post
  const email = d.reply_email || (emailMatch ? emailMatch[0] : null);
  const cRows = [];
  if (d.contact_name) cRows.push(["Name", esc(d.contact_name)]);
  if (d.contact_details) cRows.push(["Phone", esc(d.contact_details).replace(/\n/g, "<br>")]);
  else if (d.phone) cRows.push(["Phone", `<a href="tel:${esc(d.phone)}">${esc(d.phone)}</a>`]);
  if (email) cRows.push(["Email",
    `<a href="mailto:${esc(email)}">${esc(email)}</a>${d.reply_email ? ` <span class="relaytag">CL relay</span>` : ""}`]);
  const contactsHtml = cRows.length ? `
    <div class="contacts">
      <h4>Contact</h4>
      ${cRows.map(([k, v]) => `<div class="crow"><span class="ck">${k}</span><span class="cv">${v}</span></div>`).join("")}
    </div>` : "";
  const sourcesHtml = sources.length > 1 ? `
    <div class="sources">
      <h4>${sources.length} source posts${nSites > 1 ? " · " + nSites + " sites" : ""}</h4>
      ${sources.map((s, i) => `<a class="srclink" href="${s.url}" target="_blank" rel="noopener">
        <span class="srcrank">${i + 1}</span>
        <span class="srcsite">${esc(sourceLabel(s.source))}</span>
        <b>$${(s.price ?? 0).toLocaleString()}</b>
        <span class="srcmeta">trust ${s.legit_score ?? "?"} · match ${s.fit_score ?? "?"} · ${esc(s.area || "")}</span>
        <span class="srcgo">↗</span></a>`).join("")}
    </div>` : "";

  document.getElementById("modal-content").innerHTML = `
    <div class="d-head">
      <div class="d-kicker">${t.label.toUpperCase()} <span class="ksep">·</span> ${esc(d.neighborhood || d.area || "San Francisco")}</div>
      <div class="d-price">$${(d.price ?? 0).toLocaleString()}<span class="permo">/mo</span></div>
      <div class="d-title">${esc(d.title || "")}</div>
      ${d.address ? `<div class="d-addr">${esc(d.address)}</div>` : ""}
    </div>
    <div class="statbar">
      <div class="stat"><span class="sv">${d.legit_score ?? "—"}</span><span class="sk">Trust</span></div>
      <div class="stat"><span class="sv">${d.fit_score ?? "—"}</span><span class="sk">Match</span></div>
      <div class="stat"><span class="sv vt ${tr.cls}">${tr.label}</span><span class="sk">Vetting</span></div>
      <div class="stat"><span class="sv">${esc(d.status || "new")}</span><span class="sk">Status</span></div>
      <div class="stat wide"><span class="sv">${esc(specs.join(" · "))}</span><span class="sk">Unit</span></div>
    </div>
    <div class="gallery">${gallery || "<i>no photographs on file</i>"}</div>
    ${(d.lat != null && d.lng != null)
      ? `<div class="locblock"><h4>Location</h4><div id="d-map" class="d-map"></div>
           ${nearestBart(d.lat, d.lng) ? `<div class="locnote bart">🚇 ${nearestBart(d.lat, d.lng)}</div>` : ""}
           <div class="locnote">Approximate — pin is block / neighborhood accurate, not the exact unit.</div></div>`
      : (d.address
        ? `<div class="locblock"><h4>Location</h4><a class="loclink" href="https://www.openstreetmap.org/search?query=${encodeURIComponent((d.address || "") + ", San Francisco, CA")}" target="_blank" rel="noopener">View ${esc(d.address)} on OpenStreetMap ↗</a></div>`
        : "")}
    ${d.verdict_summary ? `<div class="infoblock"><h4>Assessment</h4><p>${esc(d.verdict_summary)}</p></div>` : ""}
    ${infoHtml}
    ${d.recommendation ? `<div class="infoblock rec"><h4>Recommendation</h4><p>${esc(d.recommendation)}</p></div>` : ""}
    ${contactsHtml}
    ${sourcesHtml}
    <div class="d-foot">
      <a class="origin" href="${d.url}" target="_blank" rel="noopener">View original on ${esc(sourceLabel(d.source))} ↗</a>
    </div>`;
  const m = document.getElementById("modal");
  m.classList.remove("hidden"); m.setAttribute("aria-hidden", "false");
  // small location map (free CartoDB/OSM tiles; coords are block-accurate)
  if (DMAP) { DMAP.remove(); DMAP = null; }
  if (d.lat != null && d.lng != null) {
    setTimeout(() => {
      const el = document.getElementById("d-map");
      if (!el || DMAP) return;
      DMAP = L.map(el, { scrollWheelZoom: false, attributionControl: false }).setView([d.lat, d.lng], 15);
      L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", { maxZoom: 19 }).addTo(DMAP);
      L.circleMarker([d.lat, d.lng], { radius: 8, weight: 2, color: "#fff",
        fillColor: scoreColor(d.fit_score), fillOpacity: 1 }).addTo(DMAP);
      DMAP.invalidateSize();
    }, 80);
  }
}

function closeModal() {
  const m = document.getElementById("modal");
  m.classList.add("hidden"); m.setAttribute("aria-hidden", "true");
  if (DMAP) { DMAP.remove(); DMAP = null; }
}

/* ----------------------------------------------------------- init */
function init() {
  document.getElementById("today").textContent = new Date().toLocaleDateString("en-US",
    { weekday: "long", month: "long", day: "numeric" });
  ["sort", "filter-type", "filter-price", "filter-legit", "filter-fit",
   "filter-status", "hide-scams", "show-rejected"]
    .forEach((id) => document.getElementById(id)
      .addEventListener("input", () => { syncRangeLabels(); PAGE = 1; render(); }));
  syncRangeLabels();
  // ---- search ----
  const searchEl = document.getElementById("search");
  const clearEl = document.getElementById("search-clear");
  function applySearch() {
    SEARCH_Q = searchEl.value;
    const q = SEARCH_Q.trim();
    clearEl.classList.toggle("hidden", !q);
    PAGE = 1;
    if (q && VIEW !== "list") setView("list");  // search lives in the Index
    render();                                   // instant — pure text matching
  }
  searchEl.addEventListener("input", applySearch);
  clearEl.addEventListener("click", () => { searchEl.value = ""; applySearch(); searchEl.focus(); });

  document.getElementById("view-list").addEventListener("click", () => setView("list"));
  document.getElementById("view-map").addEventListener("click", () => setView("map"));
  document.getElementById("view-featured").addEventListener("click", () => setView("featured"));
  window.addEventListener("hashchange", openFromHash);
  const areaBtn = document.getElementById("area-btn");
  areaBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    document.getElementById("area-menu").classList.toggle("hidden");
  });
  document.addEventListener("click", (e) => {
    const menu = document.getElementById("area-menu");
    if (menu && !menu.contains(e.target) && e.target !== areaBtn) menu.classList.add("hidden");
  });
  document.getElementById("modal-close").addEventListener("click", closeModal);
  document.getElementById("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
  window.openModal = openModal;
  load();
}
document.addEventListener("DOMContentLoaded", init);
