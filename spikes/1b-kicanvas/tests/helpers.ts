import { appendFileSync, mkdirSync } from "node:fs";
import { PNG } from "pngjs";
import type { Page } from "@playwright/test";

// KICANVAS_VENDOR=vendor-patched runs the supplementary experiment into results-patched/.
export const VENDOR = process.env.KICANVAS_VENDOR ?? "vendor";
export const RESULTS = new URL(
  VENDOR === "vendor" ? "../results/" : `../results-${VENDOR.replace(/^vendor-/, "")}/`,
  import.meta.url,
).pathname;
mkdirSync(RESULTS, { recursive: true });

export function record(question: string, check: string, ok: boolean, evidence: unknown): void {
  const row = { question, check, status: ok ? "PASS" : "FAIL", evidence };
  appendFileSync(`${RESULTS}results.jsonl`, JSON.stringify(row) + "\n");
  console.log(`${row.status}: ${question}/${check} :: ${JSON.stringify(evidence).slice(0, 300)}`);
}

export type Errors = { console: string[]; page: string[]; requests: string[] };

export async function openViewer(
  page: Page,
  files: string[],
  opts: {
    theme?: string;
    prefsTheme?: string;
    controls?: string;
    controlslist?: string;
    offline?: boolean;
  } = {},
): Promise<Errors> {
  const errors: Errors = { console: [], page: [], requests: [] };
  page.on("console", (m) => m.type() === "error" && errors.console.push(m.text()));
  page.on("pageerror", (e) => errors.page.push(String(e)));
  page.on("request", (r) => errors.requests.push(r.url()));
  if (opts.offline) {
    await page.route(/^https?:\/\/(?!127\.0\.0\.1)/, (route) => route.abort());
  }
  const q = new URLSearchParams({ files: files.join(","), controls: opts.controls ?? "none" });
  if (opts.theme) q.set("theme", opts.theme);
  if (opts.prefsTheme) q.set("prefstheme", opts.prefsTheme);
  if (opts.controlslist) q.set("controlslist", opts.controlslist);
  if (VENDOR !== "vendor") q.set("vendor", VENDOR);
  await page.goto(`/www/index.html?${q}`);
  await page.waitForFunction(() => (window as any).__embed?.hasAttribute("loaded"), null, {
    timeout: 60_000,
  });
  await page.waitForTimeout(1500); // first paint after load
  return errors;
}

/** Screenshot the embed; return pixel stats. Background = most frequent colour. */
export async function canvasStats(page: Page, file: string) {
  const buf = await page.locator("kicanvas-embed").screenshot({ path: `${RESULTS}${file}` });
  const png = PNG.sync.read(buf);
  const counts = new Map<number, number>();
  for (let i = 0; i < png.data.length; i += 4) {
    const key = ((png.data[i] >> 3) << 10) | ((png.data[i + 1] >> 3) << 5) | (png.data[i + 2] >> 3);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const total = png.width * png.height;
  const [bgKey, bgCount] = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
  const bg = [(bgKey >> 10) << 3, ((bgKey >> 5) & 31) << 3, (bgKey & 31) << 3];
  const luminance = (0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]) / 255;
  return {
    distinctColours: counts.size,
    nonBackgroundFraction: 1 - bgCount / total,
    background: bg,
    luminance: Math.round(luminance * 100) / 100,
  };
}

export const ECC83_SCH = "/files/ecc83/ecc83-pp.kicad_sch";
export const ECC83_PCB = "/files/ecc83/ecc83-pp.kicad_pcb";
export const HIER = [
  "/files/complex_hierarchy/complex_hierarchy.kicad_sch",
  "/files/complex_hierarchy/ampli_ht.kicad_sch",
];
/** Interactive embed: "none" disables interaction; "basic" adds a click-to-focus overlay
 * that swallows the first click unless controlslist contains "nooverlay". */
export const INTERACTIVE = { controls: "basic", controlslist: "nooverlay" } as const;

export const SCH_REFS = ["U1", "R1", "R2", "R3", "R4", "C1", "C2"];
export const PCB_REFS = ["U1", "R1", "R2", "R3", "R4", "C1", "C2",
  "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"];
