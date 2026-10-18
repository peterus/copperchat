// Q12: files written by the API load in the vendored KiCanvas (peterus/kicanvas fork).
import { mkdirSync, readdirSync, writeFileSync, existsSync } from "node:fs";
import { PNG } from "pngjs";
import { expect, test } from "@playwright/test";

const FILES = new URL("../results/files/", import.meta.url).pathname;
const OUT = new URL("../results/", import.meta.url).pathname;
const rows: { name: string; ok: boolean; evidence: unknown }[] = [];

const projects = existsSync(FILES) ? readdirSync(FILES).sort() : [];

for (const p of projects) {
  test(`kicanvas loads ${p}`, async ({ page }) => {
    const sch = readdirSync(`${FILES}${p}`).filter((f) => f.endsWith(".kicad_sch")).sort();
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("console", (m) => {
      if (m.type() === "error") errors.push(m.text());
    });
    const files = sch.map((f) => `/files/${p}/${f}`);
    await page.goto(`/index.html?files=${files.join(",")}`);
    let loaded = true;
    try {
      await page.waitForFunction(() => (window as any).__embed?.hasAttribute("loaded"), null, { timeout: 60_000 });
    } catch {
      loaded = false;
    }
    await page.waitForTimeout(1500);
    mkdirSync(`${OUT}png`, { recursive: true });
    const buf = await page.locator("kicanvas-embed").screenshot({ path: `${OUT}png/kicanvas-${p}.png` });
    const png = PNG.sync.read(buf);
    const counts = new Map<number, number>();
    for (let i = 0; i < png.data.length; i += 4) {
      const key = (png.data[i] << 16) | (png.data[i + 1] << 8) | png.data[i + 2];
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    const total = png.width * png.height;
    const bg = Math.max(...counts.values());
    const nonBg = 1 - bg / total;
    const ok = loaded && errors.length === 0 && counts.size >= 4 && nonBg > 0.002;
    rows.push({ name: `kicanvas_loads_${p}`, ok, evidence: { files: sch, loaded, errors, distinctColours: counts.size, nonBackgroundFraction: Number(nonBg.toFixed(4)) } });
    expect(ok).toBe(true);
  });
}

test.afterAll(() => {
  const out = { question: "q12_kicanvas", checks: rows.map((r) => ({ ...r, evidence: JSON.stringify(r.evidence) })), notes: { bundle: "frontend/public/vendor/kicanvas/kicanvas.js" } };
  writeFileSync(`${OUT}q12_kicanvas.json`, JSON.stringify(out, null, 2));
});
