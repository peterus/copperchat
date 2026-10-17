import { appendFileSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { PNG } from "pngjs";
import { test, expect, type Page } from "@playwright/test";

const RESULTS = new URL("../results/", import.meta.url).pathname;
mkdirSync(RESULTS, { recursive: true });
const PROJECTS: { id: string; board: string | null }[] = JSON.parse(
  readFileSync(new URL("../projects.json", import.meta.url), "utf8"),
);

test.beforeAll(() => rmSync(`${RESULTS}results.jsonl`, { force: true }));

// KiCanvas logs one console.warn per unknown s-expression element; the 85 MB
// board emits ~16k of them, and relaying those to the test runner exhausts the
// Node heap of the Playwright worker. Count them in-page instead.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const counts: Record<string, number> = {};
    (window as any).__console = counts;
    for (const k of ["log", "info", "warn", "debug"] as const) {
      (console as any)[k] = () => void (counts[k] = (counts[k] ?? 0) + 1);
    }
  });
});

function record(check: string, ok: boolean, evidence: unknown) {
  const row = { check, status: ok ? "PASS" : "FAIL", evidence };
  appendFileSync(`${RESULTS}results.jsonl`, JSON.stringify(row) + "\n");
  console.log(`${row.status}: ${check} :: ${JSON.stringify(evidence).slice(0, 300)}`);
}

async function open(page: Page, id: string, tab: "sch" | "pcb", timeout = 300_000) {
  const pageErrors: string[] = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  await page.goto(`/#${encodeURIComponent(id)}/${tab}`);
  await page.waitForFunction(
    (t) => ["ok", "failed", "none"].includes((window as any).__demo?.tabs?.[t]?.status),
    tab,
    { timeout },
  );
  const t = await page.evaluate((t) => {
    const s = (window as any).__demo.tabs[t];
    return { status: s.status, ms: s.ms, errors: s.errors };
  }, tab);
  const consoleCounts = await page.evaluate(() => (window as any).__console);
  return { ...t, pageErrors, consoleCounts };
}

async function stats(page: Page, file: string) {
  const buf = await page.locator(".viewer:not([hidden])").screenshot({ path: `${RESULTS}${file}` });
  const png = PNG.sync.read(buf);
  const counts = new Map<number, number>();
  for (let i = 0; i < png.data.length; i += 4) {
    const k = ((png.data[i] >> 3) << 10) | ((png.data[i + 1] >> 3) << 5) | (png.data[i + 2] >> 3);
    counts.set(k, (counts.get(k) ?? 0) + 1);
  }
  const [bgKey, bgCount] = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
  const bg = [(bgKey >> 10) << 3, ((bgKey >> 5) & 31) << 3, (bgKey & 31) << 3];
  return {
    colours: counts.size,
    nonBg: Math.round((1 - bgCount / (png.width * png.height)) * 1000) / 10,
    luminance: Math.round(((0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]) / 255) * 100) / 100,
  };
}

/** Click (or tap) a grid over the visible viewer until a symbol/footprint is selected. */
async function scanSelect(page: Page, mode: "click" | "tap", n = 9) {
  const box = (await page.locator(".viewer:not([hidden])").boundingBox())!;
  let sheetHit: any = null;
  for (let iy = 1; iy < n; iy++) {
    for (let ix = 1; ix < n; ix++) {
      const x = box.x + (box.width * ix) / n;
      const y = box.y + (box.height * iy) / n;
      const before = await page.evaluate(() => (window as any).__demo.selections.length);
      if (mode === "tap") await page.touchscreen.tap(x, y);
      else {
        await page.mouse.move(x, y);
        await page.mouse.click(x, y);
      }
      await page.waitForTimeout(60);
      const all = await page.evaluate((b) => (window as any).__demo.selections.slice(b), before);
      const sel = all.filter((s: any) => s.ref && s.kind !== "SchematicSheet");
      const sheet = all.find((s: any) => s.kind === "SchematicSheet");
      if (sheet && !sheetHit) sheetHit = { ...sheet, panel: await page.locator("#sel-ref").textContent() };
      if (sel.length) {
        const panel = await page.locator("#sel-ref").textContent();
        return { ...sel[sel.length - 1], panel, at: [ix, iy] };
      }
    }
  }
  // A root sheet made only of sheet boxes has no symbols: a selected sheet counts.
  return sheetHit && { ...sheetHit, sheetOnly: true };
}

for (const p of PROJECTS) {
  for (const tab of ["sch", "pcb"] as const) {
    if (tab === "pcb" && !p.board) continue;
    test(`render+select ${p.id} ${tab}`, async ({ page }) => {
      const r = await open(page, p.id, tab);
      const s = await stats(page, `${p.id.replace("/", "__")}-${tab}.png`);
      record(`render ${p.id} ${tab}`, r.status === "ok" && s.colours > 10 && r.errors.length === 0,
        { ...r, ...s });
      if (r.status !== "ok") return;
      const t0 = Date.now();
      const sel = await scanSelect(page, "click");
      record(`click-select ${p.id} ${tab}`, !!sel && sel.panel === sel.ref,
        { sel, scanMs: Date.now() - t0 });
    });
  }
}

test.describe("mobile", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, deviceScaleFactor: 3 });
  for (const [id, tab] of [["ecc83/ecc83-pp", "sch"], ["ecc83/ecc83-pp", "pcb"], ["video/video", "pcb"]] as const) {
    test(`mobile tap-select ${id} ${tab}`, async ({ page }) => {
      const r = await open(page, id, tab);
      const s = await stats(page, `mobile-${id.replace("/", "__")}-${tab}.png`);
      await page.screenshot({ path: `${RESULTS}mobile-full-${id.replace("/", "__")}-${tab}.png` });
      const sel = r.status === "ok" ? await scanSelect(page, "tap") : null;
      record(`mobile tap-select ${id} ${tab}`, r.status === "ok" && !!sel && sel.panel === sel.ref,
        { status: r.status, ms: r.ms, ...s, sel });
    });
  }
});

test("offline: no external requests, fonts self-hosted", async ({ page, baseURL }) => {
  const origin = new URL(baseURL!).origin;
  const external: string[] = [];
  page.on("request", (q) => {
    if (new URL(q.url()).origin !== origin) external.push(q.url());
  });
  await page.route((url) => url.origin !== origin, (route) => route.abort());
  const r = await open(page, "ecc83/ecc83-pp", "pcb");
  const fonts = await page.evaluate(async () => {
    await document.fonts.ready;
    return {
      nunito: document.fonts.check('16px "Nunito"'),
      symbols: document.fonts.check('24px "Material Symbols Outlined"', "fullscreen"),
    };
  });
  record("offline no external requests", r.status === "ok" && external.length === 0,
    { status: r.status, external, fonts });
});

test("theme toggle: light schematic, board stays dark", async ({ page }) => {
  await open(page, "ecc83/ecc83-pp", "sch");
  const dark = await stats(page, "theme-dark-sch.png");
  await page.click("#theme");
  await page.waitForLoadState("load");
  await page.waitForFunction(() => (window as any).__demo?.tabs?.sch?.status === "ok", null, { timeout: 60_000 });
  const pref = await page.evaluate(() => localStorage.getItem("kc:prefs:theme"));
  const light = await stats(page, "theme-light-sch.png");
  await page.click('.tabs button[data-tab="pcb"]');
  await page.waitForFunction(() => (window as any).__demo?.tabs?.pcb?.status === "ok", null, { timeout: 60_000 });
  const lightPcb = await stats(page, "theme-light-pcb.png");
  record("theme toggle", dark.luminance < 0.2 && light.luminance > 0.8,
    { pref, dark: dark.luminance, light: light.luminance, lightPcb: lightPcb.luminance });
  await page.click("#theme"); // restore dark for later tests (localStorage is per context anyway)
});

test("hierarchy: sheet list navigates sub-sheets", async ({ page }) => {
  const r = await open(page, "complex_hierarchy/complex_hierarchy", "sch");
  const options = await page.locator("#sheet option").allTextContents();
  const values = await page.locator("#sheet option").evaluateAll((o) => o.map((x) => (x as HTMLOptionElement).value));
  await page.selectOption("#sheet", values[1]);
  await page.waitForTimeout(1500);
  const active = await page.evaluate(() => (window as any).__demo.tabs.sch.embed.project.active_page.project_path);
  const s = await stats(page, "hier-subsheet.png");
  record("hierarchy sheet navigation", r.status === "ok" && options.length >= 2 && active === values[1] && s.colours > 10,
    { options, active, ...s });
});

test("highlight REF by name (schematic + board)", async ({ page }) => {
  const out: Record<string, unknown> = {};
  for (const tab of ["sch", "pcb"] as const) {
    await open(page, "ecc83/ecc83-pp", tab);
    await page.fill("#find-ref", "U1");
    await page.click("#find button");
    await page.waitForTimeout(300);
    out[tab] = await page.locator("#sel-ref").textContent();
  }
  record("highlight REF", out.sch === "U1" && out.pcb === "U1", out);
});

test("load-error detection: corrupted board is reported, not shown as loaded", async ({ page }) => {
  // Network-level corruption for the test only; the file on disk is untouched.
  await page.route("**/ecc83-pp.kicad_pcb", async (route) => {
    const res = await route.fetch();
    const body = (await res.text()).replace(/\(transform/g, "(xform");
    await route.fulfill({ response: res, body });
  });
  const r = await open(page, "ecc83/ecc83-pp", "pcb");
  const banner = await page.locator("#banner").textContent();
  record("load-error detection", r.status === "failed" && (await page.locator("#banner").isVisible()),
    { ...r, banner });
});
