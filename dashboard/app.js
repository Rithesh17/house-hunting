/* The San Francisco Property Register — interactions */
let LISTINGS = [], MARKERS = [], MAP = null, VIEW = "list", PAGE = 1;
const PAGE_SIZE = 24;
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

/* Semantic color: green→amber→red by SCORE (used for match + trust, not type). */
function scoreColor(s) {
  if (s == null) return "#9a8e79";
  if (s >= 80) return "#2f7d4f";
  if (s >= 60) return "#7a9a2e";
  if (s >= 40) return "#c08a1a";
  return "#b23222";
}

const TRUST = {
  "likely-legit":       { cls: "t-legit",   label: "Verified" },
  "unverified-amateur": { cls: "t-amateur", label: "Unverified" },
  "likely-scam":        { cls: "t-scam",    label: "Flagged" },
};
function trust(d) { return TRUST[d.legit_label] || { cls: "t-amateur", label: "Unrated" }; }

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ----------------------------------------------------------- load */
async function load() {
  LISTINGS = await (await fetch("/api/listings")).json();
  buildAreaMenu();
  render();
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
    maxPrice: +val("filter-price") || Infinity,
    minLegit: +val("filter-legit"), minFit: +val("filter-fit"),
    status: val("filter-status"),
    hideScams: document.getElementById("hide-scams").checked,
    showRejected: document.getElementById("show-rejected").checked,
  };
}
const val = (id) => document.getElementById(id).value;

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
  const prio = (d) => (d.room_type === "1br" ? 0 : d.room_type === "studio" ? 1 : 2);
  const cmp = {
    fit: (a, b) => prio(a) - prio(b) || (b.fit_score ?? -1) - (a.fit_score ?? -1),
    legit: (a, b) => (b.legit_score ?? -1) - (a.legit_score ?? -1),
    "price-asc": (a, b) => (a.price ?? 1e9) - (b.price ?? 1e9),
    "price-desc": (a, b) => (b.price ?? -1) - (a.price ?? -1),
    newest: (a, b) => (b.first_seen_at || "").localeCompare(a.first_seen_at || ""),
  }[f.sort];
  return out.sort(cmp);
}

function render() {
  const items = selection();
  document.getElementById("count").textContent =
    `${items.length} of ${LISTINGS.length} entries on file`;
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
    card.className = "card" + (scam ? " flagged" : "");
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
        ${d.photos && d.photos.length > 1 ? `<span class="photocount">${d.photos.length} ▦</span>` : ""}
      </div>
      <div class="body">
        <h3 class="title">${esc(d.title || "Untitled entry")}</h3>
        <div class="hood"><span class="pin">◈</span> ${esc(d.area || d.neighborhood || "San Francisco")}
          <span class="srctag">${sourceLabel(d.source)}${d.dup_count > 1 ? " +" + (d.dup_count - 1) : ""}</span></div>
        <div class="specs">
          <span class="chip kind">${esc(t.kind)}</span>
          ${specs.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}
          ${d.dup_count > 1 ? `<span class="chip dup">📑 ${d.dup_count} posts</span>` : ""}
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
  document.getElementById("list").classList.toggle("hidden", view !== "list");
  document.getElementById("map").classList.toggle("hidden", view !== "map");
  if (view === "map") {
    if (!MAP) initMap();
    setTimeout(() => { MAP.invalidateSize(); render(); }, 60);
  } else {
    render();
  }
}

/* ----------------------------------------------------------- dossier modal */
function openModal(id) {
  const d = LISTINGS.find((x) => x.id === id);
  if (!d) return;
  const t = typeOf(d), tr = trust(d);
  const gallery = (d.photos || []).map((p) => `<img src="${p}" alt="" referrerpolicy="no-referrer" onclick="window.open('${p}','_blank')" />`).join("");
  const flags = (d.red_flags && d.red_flags.length)
    ? `<div class="flags"><h4>⚑ Red flags</h4><ul>${d.red_flags.map((f) => `<li>${esc(f)}</li>`).join("")}</ul></div>` : "";
  const statusBtns = ["new", "vetted", "interested", "contacted", "rejected"]
    .map((s) => `<button class="${d.status === s ? "active" : ""}" onclick="setStatus('${d.id}','${s}')">${s}</button>`).join("");
  const specs = [];
  if (d.bedrooms != null) specs.push(d.bedrooms ? `${d.bedrooms} Bed` : "Studio");
  if (d.bathrooms != null) specs.push(`${d.bathrooms} Bath`);
  specs.push(d.sqft ? `~${d.sqft} ft²` : "ft² n/a");

  // all source posts for this unit (primary + duplicate reposts), best first
  const sources = [
    { url: d.url, price: d.price, fit_score: d.fit_score, legit_score: d.legit_score,
      legit_label: d.legit_label, area: d.area, source: d.source, primary: true },
    ...(d.duplicates || []),
  ].sort((a, b) => (b.fit_score ?? -1) - (a.fit_score ?? -1));
  const nSites = new Set(sources.map((s) => s.source)).size;
  const emailMatch = (d.contact || "").match(/[\w.+-]+@[\w-]+\.[\w.-]+/);
  const email = emailMatch ? emailMatch[0] : null;
  const contactsHtml = `
    <div class="contacts">
      <h4>Contact</h4>
      ${d.phone ? `<div class="crow"><span class="cic">📞</span> <a href="tel:${esc(d.phone)}">${esc(d.phone)}</a></div>` : ""}
      ${email ? `<div class="crow"><span class="cic">✉️</span> <a href="mailto:${esc(email)}">${esc(email)}</a></div>` : ""}
      <div class="crow"><span class="cic">🔗</span> <a href="${d.url}" target="_blank" rel="noopener">Reply on Craigslist ↗</a></div>
      ${(!d.phone && !email) ? `<div class="cnote">No direct phone/email in the post — use the Craigslist reply button (relay email).</div>` : ""}
    </div>`;
  const sourcesHtml = sources.length > 1 ? `
    <div class="sources">
      <h4>${sources.length} source posts${nSites > 1 ? " · across " + nSites + " sites" : ""} · best first</h4>
      ${sources.map((s, i) => `<a class="srclink" href="${s.url}" target="_blank" rel="noopener">
        <span class="srcrank">${i + 1}</span>
        <span class="srcsite">${esc(sourceLabel(s.source))}</span>
        <b>$${(s.price ?? 0).toLocaleString()}</b>
        <span>${TEMOJI[s.legit_label] || "⚪"} ${s.legit_score ?? "?"} · match ${s.fit_score ?? "?"} · ${esc(s.area || "")}</span>
        <span class="srcgo">↗</span></a>`).join("")}
    </div>` : "";

  document.getElementById("modal-content").innerHTML = `
    <div class="d-kicker">
      <span style="color:var(--ink);font-weight:700">${t.label.toUpperCase()}</span> ·
      <span>${esc(d.neighborhood || d.area || "San Francisco")}</span> ·
      <span class="seal ${tr.cls}" style="position:static;border:none;background:none;padding:0">${tr.label} ${d.legit_score ?? "—"}%</span>
    </div>
    <div class="d-price">$${(d.price ?? 0).toLocaleString()}<span style="font-size:18px;color:var(--ink-faint);font-family:var(--mono)"> /mo</span></div>
    <div class="d-title">${esc(d.title || "")}</div>
    ${d.address ? `<div class="d-addr">📍 ${esc(d.address)}</div>` : ""}
    <div class="d-meta">${specs.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}</div>
    <div class="gallery">${gallery || "<i>no photographs on file</i>"}</div>
    <div class="verdict">
      <div class="vblock">
        ${d.verdict_summary ? `<h4>Assessment</h4><p>${esc(d.verdict_summary)}</p>` : ""}
        ${flags}
        ${d.recommendation ? `<h4>Recommendation</h4><p>${esc(d.recommendation)}</p>` : ""}
        <h4>The listing, verbatim</h4>
        <div class="desc">${esc(d.description || "(no description on file)")}</div>
      </div>
      <div class="vblock">
        <h4>Scores</h4>
        <div class="scores">
          <div class="scorebox"><div class="n">${d.legit_score ?? "—"}</div><div class="k">Trust</div></div>
          <div class="scorebox"><div class="n">${d.fit_score ?? "—"}</div><div class="k">Match</div></div>
        </div>
        <h4 style="margin-top:16px">Disposition</h4>
        <div class="status-row">${statusBtns}</div>
      </div>
    </div>
    ${contactsHtml}
    ${sourcesHtml}
    <div class="d-foot">
      <a class="origin" href="${d.url}" target="_blank" rel="noopener">View original ↗</a>
    </div>`;
  const m = document.getElementById("modal");
  m.classList.remove("hidden"); m.setAttribute("aria-hidden", "false");
}

async function setStatus(id, status) {
  const res = await fetch(`/api/listings/${id}/status`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  if (res.ok) {
    const d = LISTINGS.find((x) => x.id === id);
    if (d) d.status = status;
    openModal(id); render();
  }
}
function closeModal() {
  const m = document.getElementById("modal");
  m.classList.add("hidden"); m.setAttribute("aria-hidden", "true");
}

/* ----------------------------------------------------------- init */
function init() {
  document.getElementById("today").textContent = new Date().toLocaleDateString("en-US",
    { weekday: "long", month: "long", day: "numeric" });
  ["sort", "filter-type", "filter-price", "filter-legit", "filter-fit",
   "filter-status", "hide-scams", "show-rejected"]
    .forEach((id) => document.getElementById(id)
      .addEventListener("input", () => { PAGE = 1; render(); }));
  document.getElementById("view-list").addEventListener("click", () => setView("list"));
  document.getElementById("view-map").addEventListener("click", () => setView("map"));
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
  window.openModal = openModal; window.setStatus = setStatus;
  load();
}
document.addEventListener("DOMContentLoaded", init);
