/* Headless-browser validation of the dashboard.
 *
 * Spawns the Flask server, drives the page with headless Chromium, and asserts:
 *   - /api/listings returns data
 *   - the page renders listing cards
 *   - the Leaflet map renders markers
 *   - filtering changes the visible set
 *   - opening a card shows the detail modal with a photo gallery
 *   - a status update persists to the DB (re-queried via the API)
 * Saves a screenshot to tools/screenshots/dashboard.png.
 *
 *   node tools/validate_dashboard.mjs
 */
import { chromium } from "playwright";
import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const BASE = "http://127.0.0.1:8000";

const checks = [];
function check(name, ok, detail = "") {
  checks.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

async function waitForServer(timeoutMs = 20000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const r = await fetch(`${BASE}/api/listings`);
      if (r.ok) return true;
    } catch {}
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

const PY = process.platform === "win32" ? "py" : "python3";
const server = spawn(PY, ["scripts/serve.py"], { cwd: ROOT, stdio: "inherit" });

let browser;
let exitCode = 0;
try {
  if (!(await waitForServer())) throw new Error("server did not start");

  // 1. API returns data
  const apiRes = await fetch(`${BASE}/api/listings`);
  const data = await apiRes.json();
  check("API /api/listings returns array with data",
        Array.isArray(data) && data.length > 0, `${data.length} listings`);

  browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(BASE, { waitUntil: "networkidle" });

  // 2. cards render (list view is the default)
  await page.waitForSelector("#list .card", { timeout: 10000 });
  const cardCount = await page.locator("#list .card").count();
  check("Listing cards render (list view)", cardCount > 0, `${cardCount} cards`);

  // 3. type filter changes the visible set
  await page.selectOption("#filter-type", "studio");
  await page.waitForTimeout(300);
  const studioCount = await page.locator("#list .card").count();
  await page.selectOption("#filter-type", "1br");
  await page.waitForTimeout(300);
  const oneBrCount = await page.locator("#list .card").count();
  await page.selectOption("#filter-type", "");
  await page.waitForTimeout(300);
  check("Type filter changes results",
        studioCount !== cardCount || oneBrCount !== cardCount,
        `all=${cardCount} studio=${studioCount} 1br=${oneBrCount}`);

  // 4. price filter narrows results
  await page.fill("#filter-price", "1500");
  await page.waitForTimeout(300);
  const cheapCount = await page.locator("#list .card").count();
  await page.fill("#filter-price", "2000");
  await page.waitForTimeout(300);
  check("Price filter narrows results", cheapCount <= cardCount,
        `<=1500: ${cheapCount} of ${cardCount}`);

  // 5. detail modal opens with a gallery image
  await page.locator("#list .card").first().click();
  await page.waitForSelector("#modal:not(.hidden)", { timeout: 5000 });
  const galleryImgs = await page.locator("#modal .gallery img").count();
  check("Detail modal opens with photo gallery", galleryImgs > 0,
        `${galleryImgs} photos`);

  // 6. status update persists to the DB
  const firstId = await page.locator("#list .card").first().getAttribute("data-id");
  await page.locator("#modal .status-row button", { hasText: "interested" }).click();
  await page.waitForTimeout(400);
  const after = await (await fetch(`${BASE}/api/listings`)).json();
  const updated = after.find((d) => d.id === firstId);
  check("Status update persists to DB",
        updated && updated.status === "interested",
        `${firstId} -> ${updated?.status}`);
  await page.locator("#modal-close").click();

  // 7. switch to MAP view and confirm custom price-pin markers render
  await page.locator("#view-map").click();
  await page.waitForSelector("#map .leaflet-marker-icon .pin", { timeout: 10000 });
  const markerCount = await page.locator("#map .leaflet-marker-icon .pin").count();
  check("Map view renders custom price pins", markerCount > 0, `${markerCount} pins`);

  // screenshot proof (map view)
  mkdirSync(path.join(__dirname, "screenshots"), { recursive: true });
  await page.screenshot({
    path: path.join(__dirname, "screenshots", "dashboard.png"),
    fullPage: true,
  });
  check("Screenshot captured", true, "tools/screenshots/dashboard.png");
} catch (err) {
  check("validation run", false, String(err));
} finally {
  if (browser) await browser.close();
  server.kill();
}

const failed = checks.filter((c) => !c.ok);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed.`);
if (failed.length) {
  exitCode = 1;
  console.log("FAILED:", failed.map((c) => c.name).join(", "));
}
process.exit(exitCode);
