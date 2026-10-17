import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import { test, type Page } from "@playwright/test";
import { PNG } from "pngjs";

const RESULTS = new URL("../results/", import.meta.url).pathname;
mkdirSync(RESULTS, { recursive: true });
writeFileSync(`${RESULTS}viewer.jsonl`, "");
function record(q: string, check: string, ok: boolean, evidence: unknown) {
  const row = { q, check, status: ok ? "PASS" : "FAIL", evidence };
  appendFileSync(`${RESULTS}viewer.jsonl`, JSON.stringify(row) + "\n");
  console.log(`${row.status}: ${q}/${check} :: ${JSON.stringify(evidence).slice(0, 400)}`);
}

const AFTER = "/.work/pairs/hier_edit/after";
const HIER = [`${AFTER}/complex_hierarchy.kicad_sch`, `${AFTER}/ampli_ht.kicad_sch`];

async function open(page: Page, bundle: "baseline" | "patched", files = HIER) {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(`/viewer/index.html?bundle=${bundle}&files=${files.join(",")}`);
  await page.waitForFunction(() => (window as any).__embed?.hasAttribute("loaded"), null, { timeout: 90_000 });
  await page.waitForTimeout(2000);
  return errors;
}

/** Pixels of an exact colour family (orange highlight) and a hash of the whole canvas. */
async function shot(page: Page, name: string) {
  const buf = await page.locator("kicanvas-embed").screenshot({ path: `${RESULTS}${name}.png` });
  const png = PNG.sync.read(buf);
  let orange = 0;
  for (let i = 0; i < png.data.length; i += 4) {
    const [r, g, b] = [png.data[i], png.data[i + 1], png.data[i + 2]];
    if (r > 200 && g > 90 && g < 200 && b < 120 && r - b > 120) orange++;
  }
  const { createHash } = await import("node:crypto");
  return { orange, sha: createHash("sha1").update(buf).digest("hex").slice(0, 12) };
}

const viewerCall = (page: Page, fn: string, ...args: unknown[]) =>
  page.evaluate(([f, a]) => { const v = (window as any).__viewer(); return v[f as string](...(a as unknown[])); }, [fn, args] as const);

test.describe.configure({ mode: "serial" });

test("baseline (vendored fork bundle): no multi-select / highlight API", async ({ page }) => {
  const errors = await open(page, "baseline");
  const api = await page.evaluate(() => {
    const v = (window as any).__viewer();
    return { highlight: typeof v.highlight, highlight_boxes: typeof v.highlight_boxes, selectLen: v.select.length };
  });
  record("2C-viewer", "baseline_has_no_highlight_api", api.highlight === "undefined" && api.highlight_boxes === "undefined", api);
  // single select works
  await viewerCall(page, "select", "C105");
  const one = await page.evaluate(() => (window as any).__viewer().selected?.context?.reference ?? null);
  record("2C-viewer", "baseline_single_select_by_ref", one === "C105", one);
  // select(array of refs): what happens?
  const arr = await page.evaluate(() => {
    try { const v = (window as any).__viewer(); v.select(["C105", "C101"]); return { threw: false, selected: v.selected ? "something" : null }; }
    catch (e) { return { threw: true, msg: String(e).slice(0, 120) }; }
  });
  record("2C-viewer", "baseline_select_array_is_not_multi_select", arr.selected !== "something", arr);
  // select by uuid of a symbol
  const sel = await page.evaluate(() => { const v = (window as any).__viewer(); v.select("C105"); return v.selected?.context?.uuid ?? null; });
  record("2C-viewer", "baseline_symbol_uuid_available", typeof sel === "string" && sel.length > 10, sel);
  record("2C-viewer", "baseline_no_page_errors", errors.length === 0, errors);
});

test("patched: highlight a set of refs on the active sheet, clear, coexist with selection", async ({ page }) => {
  const errors = await open(page, "patched");
  const none = await shot(page, "p0-none");
  const refs = await page.evaluate(() => {
    const v = (window as any).__viewer();
    return [...v.schematic.symbols.values()].map((s: any) => s.reference).filter((r: string) => /^[A-Z]+\d+$/.test(r) && !r.startsWith("#"));
  });
  record("2C-viewer", "root_sheet_refs_available", refs.length > 5, { n: refs.length, sample: refs.slice(0, 6) });
  const pick = ["C105", ...refs.filter((r: string) => r !== "C105").slice(0, 4)];
  const t0 = Date.now();
  const res = await viewerCall(page, "highlight", [...pick, "NOPE99"]);
  const ms = Date.now() - t0;
  await page.waitForTimeout(500);
  const on = await shot(page, "p1-highlight-set");
  record("2C-viewer", "highlight_set_found_and_missing", res.found.length === pick.length && res.missing.length === 1 && res.missing[0] === "NOPE99", { res, ms });
  record("2C-viewer", "highlight_set_visible", on.orange > none.orange + 200, { none: none.orange, on: on.orange });
  const cnt = await page.evaluate(() => (window as any).__viewer().highlighted_count);
  record("2C-viewer", "highlighted_count", cnt === pick.length, cnt);
  // selection coexists
  await viewerCall(page, "select", "C105");
  await page.waitForTimeout(500);
  const both = await shot(page, "p2-highlight-plus-selection");
  const stillN = await page.evaluate(() => (window as any).__viewer().highlighted_count);
  record("2C-viewer", "highlight_survives_select", stillN === pick.length && both.orange > none.orange + 200, { stillN, both: both.orange });
  // clear
  await viewerCall(page, "select", null);
  await viewerCall(page, "clear_highlight");
  await page.waitForTimeout(500);
  const off = await shot(page, "p3-cleared");
  record("2C-viewer", "clear_restores_pixels", off.sha === none.sha, { none: none.sha, off: off.sha });
  // perf: all refs on the sheet
  const t1 = Date.now();
  const all = await viewerCall(page, "highlight", refs);
  const msAll = Date.now() - t1;
  record("2C-viewer", "highlight_all_refs_perf", all.found.length === refs.length && msAll < 1000, { n: refs.length, ms: msAll });
  await viewerCall(page, "clear_highlight");
  record("2C-viewer", "no_page_errors", errors.length === 0, errors);
});

test("patched: highlight across sheets (instances) — host drives the sheet switch", async ({ page }) => {
  const errors = await open(page, "patched");
  const pages = await page.evaluate(() => [...(window as any).__embed.project.pages()].map((p: any) => ({ file: p.filename, sheet_path: p.sheet_path, name: p.name, project_path: p.project_path })));
  record("2C-viewer", "project_pages", pages.length === 3, pages);
  // the change set as a tool would hold it: per sheet instance, refs from the diff JSON
  const want = [
    { sheet: "/5b9623a5-6d01-41fc-9865-e1bc779418c8", refs: ["C105"] },
    { sheet: "/5b9623a5-6d01-41fc-9865-e1bc779418c8/00000000-0000-0000-0000-00004b3a1333", refs: ["R201"] },
    { sheet: "/5b9623a5-6d01-41fc-9865-e1bc779418c8/00000000-0000-0000-0000-00004b3a13a4", refs: ["R301"] },
  ];
  // 1. on the root page the other sheets' refs are reported missing
  const rootTry = await viewerCall(page, "highlight", ["C105", "R201", "R301"]);
  record("2C-viewer", "other_sheet_refs_reported_missing", rootTry.found.join() === "C105" && rootTry.missing.join() === "R201,R301", rootTry);
  // 2. per page: switch, wait for viewerload, highlight the page's refs
  const perPage: unknown[] = [];
  for (const w of want) {
    await page.evaluate((sp) => {
      const e = (window as any).__embed; const pg = [...e.project.pages()].find((p: any) => p.sheet_path === sp);
      window.__loaded = false; e.addEventListener("kicanvas:viewerload", () => { window.__loaded = true; }, { once: true });
      e.project.set_active_page(pg);
    }, w.sheet);
    await page.waitForFunction(() => (window as any).__loaded === true, null, { timeout: 60_000 });
    await page.waitForTimeout(1500);
    const r = await viewerCall(page, "highlight", w.refs);
    await page.waitForTimeout(400);
    const s = await shot(page, `p4-${w.refs[0]}`);
    perPage.push({ sheet: w.sheet.slice(-8), res: r, orange: s.orange });
  }
  record("2C-viewer", "per_instance_highlight", perPage.every((p: any) => p.res.missing.length === 0 && p.orange > 100), perPage);
  // 3. switching page: is the highlight (a viewer-level state) cleared / carried?
  const before = await page.evaluate(() => (window as any).__viewer().highlighted_count);
  await page.evaluate(() => {
    const e = (window as any).__embed; window.__loaded = false;
    e.addEventListener("kicanvas:viewerload", () => { window.__loaded = true; }, { once: true });
    e.project.set_active_page([...e.project.pages()].find((p: any) => p.name === "Root" || p.sheet_path.split("/").length === 2));
  });
  await page.waitForFunction(() => (window as any).__loaded === true, null, { timeout: 60_000 });
  await page.waitForTimeout(1500);
  const afterSwitch = await page.evaluate(() => (window as any).__viewer().highlighted_count);
  const sameViewer = await page.evaluate(() => (window as any).__viewer().constructor.name);
  record("2C-viewer", "highlight_cleared_on_page_switch", before > 0 && afterSwitch === 0, { before, afterSwitch, note: "unpatched-load state was stale (1 after switch) in the first run; patch resets in load()", viewer: sameViewer });
  record("2C-viewer", "no_page_errors", errors.length === 0, errors);
});

test("patched: highlight_boxes from kicad-cli diff bboxes (wires/labels, deleted items) line up with symbol bboxes", async ({ page }) => {
  const errors = await open(page, "patched");
  // diff JSON bbox is [x,y,w,h] in 100 nm units: /1e4 -> mm
  const diff = await (await page.request.get("/.work/pairs/hier_edit/diff.json")).json();
  const c105 = diff.changes.find((c: any) => c.refdes === "C105");
  const mm = (b: number[]) => ({ x: b[0] / 1e4, y: b[1] / 1e4, w: b[2] / 1e4, h: b[3] / 1e4 });
  const vb = await page.evaluate(() => { const v = (window as any).__viewer(); v.select("C105"); const b = v.selected; return { x: b.x, y: b.y, w: b.w, h: b.h }; });
  const db = mm(c105.bbox);
  const close = (a: number, b: number) => Math.abs(a - b) < 3;
  record("2C-viewer", "diff_bbox_matches_viewer_symbol_bbox", close(db.x, vb.x) && close(db.y, vb.y) && close(db.w, vb.w) && close(db.h, vb.h), { diff_mm: db, viewer_mm: vb });
  // the root-sheet changes of the diff as boxes
  const rootChanges = diff.changes.filter((c: any) => c.id.split("/").length === 3);
  const n = await viewerCall(page, "highlight_boxes", rootChanges.map((c: any) => mm(c.bbox)));
  await page.waitForTimeout(500);
  const s = await shot(page, "p5-diff-boxes");
  // scale: 500 boxes in one call (a large edit), then clear
  const big = await page.evaluate(() => {
    const v = (window as any).__viewer(); const boxes = [];
    for (let i = 0; i < 500; i++) boxes.push({ x: 20 + (i % 25) * 10, y: 20 + Math.floor(i / 25) * 8, w: 6, h: 4 });
    const t = performance.now(); v.highlight_boxes(boxes); const set = performance.now() - t;
    const t2 = performance.now(); v.clear_highlight(); return { n: boxes.length, set_ms: Math.round(set), clear_ms: Math.round(performance.now() - t2) };
  });
  record("2C-viewer", "highlight_500_boxes_perf", big.set_ms < 500, big);
  record("2C-viewer", "highlight_boxes_visible", n === rootChanges.length && s.orange > 100, { n, orange: s.orange });
  record("2C-viewer", "no_page_errors", errors.length === 0, errors);
});

test("diff bbox of a MOVED symbol: before-box, after-box or union?", async ({ page }) => {
  const diff = await (await page.request.get("/.work/pairs/ecc83_edit/diff.json")).json();
  const r3 = diff.changes.find((c: any) => c.refdes === "R3");
  const db = { x: r3.bbox[0] / 1e4, y: r3.bbox[1] / 1e4, w: r3.bbox[2] / 1e4, h: r3.bbox[3] / 1e4 };
  const boxes: Record<string, any> = {};
  for (const side of ["before", "after"]) {
    await open(page, "patched", [`/.work/pairs/ecc83_edit/${side}/ecc83-pp.kicad_sch`]);
    boxes[side] = await page.evaluate(() => { const v = (window as any).__viewer(); v.select("R3"); const b = v.selected; return { x: b.x, y: b.y, w: b.w, h: b.h }; });
  }
  const close = (a: any, b: any) => ["x", "y", "w", "h"].every((k) => Math.abs(a[k] - b[k]) < 3);
  const which = close(db, boxes.after) ? "after" : close(db, boxes.before) ? "before" : "other";
  record("2C-viewer", "moved_symbol_diff_bbox_is_after_box", which === "after", { diff_mm: db, ...boxes, which });
});
