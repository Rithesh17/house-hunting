/* Interactive UI verification: drives the dashboard like a user and screenshots
 * each step, while capturing any console / page errors. */
import { chromium } from "playwright";
import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const BASE = "http://127.0.0.1:8000";
const DIR = path.join(__dirname, "screenshots");
mkdirSync(DIR, { recursive: true });
const shot = (p, n) => p.screenshot({ path: path.join(DIR, n) });
const PY = process.platform === "win32" ? "py" : "python3";
const server = spawn(PY, ["scripts/serve.py"], { cwd: ROOT, stdio: "ignore" });

async function wait() { for (let i=0;i<50;i++){ try{ if((await fetch(`${BASE}/api/listings`)).ok) return true; }catch{} await new Promise(r=>setTimeout(r,400)); } }

const errors = [];
const steps = [];
const log = (s, ok, d="") => { steps.push({s,ok,d}); console.log(`${ok?"✓":"✗"} ${s}${d?"  — "+d:""}`); };

await wait();
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
p.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
p.on("pageerror", (e) => errors.push(String(e)));

try {
  await p.goto(BASE, { waitUntil: "networkidle" });
  await p.waitForSelector("#list .card");
  await p.waitForTimeout(900);
  log("List view renders", (await p.locator("#list .card").count()) > 0,
      `${await p.locator("#list .card").count()} cards`);
  await shot(p, "ui-01-list.png");

  // type filter -> Studio
  await p.selectOption("#filter-type", "studio");
  await p.waitForTimeout(400);
  const studio = await p.locator("#list .card").count();
  const allKinds = await p.locator("#list .card .chip.kind").allInnerTexts();
  log("Filter → Studio works", allKinds.every((k) => /studio/i.test(k)),
      `${studio} cards, kinds: ${[...new Set(allKinds)].join("/")}`);
  await shot(p, "ui-02-studio.png");
  await p.selectOption("#filter-type", "");
  await p.waitForTimeout(300);

  // reveal flagged listings (uncheck "hide flagged") -> expect a stamp
  await p.uncheck("#hide-scams");
  await p.waitForTimeout(400);
  const stamps = await p.locator("#list .card .stamp").count();
  log("Reveal flagged shows stamped cards", stamps > 0, `${stamps} flagged stamps`);
  await shot(p, "ui-03-flagged.png");
  await p.check("#hide-scams");
  await p.waitForTimeout(300);

  // open dossier
  await p.locator("#list .card").first().click();
  await p.waitForSelector("#modal:not(.hidden)");
  await p.waitForTimeout(500);
  log("Dossier opens with gallery + scores",
      (await p.locator("#modal .gallery img").count()) > 0 &&
      (await p.locator("#modal .scorebox").count()) === 2,
      `${await p.locator("#modal .gallery img").count()} photos`);
  await shot(p, "ui-04-dossier.png");

  // change disposition to "contacted" and confirm active + persisted
  const id = await p.locator("#list .card").first().getAttribute("data-id");
  await p.locator("#modal .status-row button", { hasText: "contacted" }).click();
  await p.waitForTimeout(400);
  const active = await p.locator("#modal .status-row button.active").innerText();
  const persisted = (await (await fetch(`${BASE}/api/listings`)).json())
    .find((d) => d.id === id)?.status;
  log("Disposition click updates + persists", /contacted/i.test(active) && persisted === "contacted",
      `active=${active.trim()} db=${persisted}`);
  await shot(p, "ui-05-status.png");
  await p.locator("#modal-close").click();
  await p.waitForTimeout(200);

  // map view + click a pin -> popup
  await p.locator("#view-map").click();
  await p.waitForSelector("#map .leaflet-marker-icon .pin");
  await p.waitForTimeout(1000);
  const pins = await p.locator("#map .leaflet-marker-icon .pin").count();
  await p.locator("#map .leaflet-marker-icon .pin").first().click({ force: true });
  await p.waitForSelector(".leaflet-popup-content .pop");
  await p.waitForTimeout(500);
  log("Map pins render + popup opens on click", pins > 0,
      `${pins} pins, popup: ${await p.locator(".leaflet-popup-content .pprice").innerText()}`);
  await shot(p, "ui-06-map-popup.png");

  // open dossier from the popup link
  await p.locator(".leaflet-popup-content a").click();
  await p.waitForSelector("#modal:not(.hidden)");
  await p.waitForTimeout(400);
  log("Popup → dossier link works", await p.locator("#modal .d-price").count() > 0);

  log("No console / page errors", errors.length === 0, errors.slice(0,3).join(" | ") || "clean");
} catch (e) {
  log("verification run", false, String(e));
} finally {
  await b.close();
  server.kill();
}

const failed = steps.filter((s) => !s.ok);
console.log(`\n${steps.length - failed.length}/${steps.length} interaction checks passed.`);
process.exit(failed.length ? 1 : 0);
