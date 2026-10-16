import { expect, test, type Page } from "@playwright/test";
import { ECC83_PCB, ECC83_SCH, INTERACTIVE, PCB_REFS, SCH_REFS, openViewer, record } from "./helpers";

type Ev = { via: string; type: string; ref: string | null; kind: string | null };

async function events(page: Page): Promise<Ev[]> {
  return page.evaluate(() => (window as any).__events as Ev[]);
}

async function gridScan(page: Page, known: string[]) {
  const box = (await page.locator("kicanvas-embed").boundingBox())!;
  const hits: { x: number; y: number; ref: string; via: string; kind: string | null }[] = [];
  for (let gy = 1; gy < 18; gy++) {
    for (let gx = 1; gx < 28; gx++) {
      const x = box.x + (box.width * gx) / 28;
      const y = box.y + (box.height * gy) / 18;
      const before = (await events(page)).length;
      await page.mouse.click(x, y);
      await page.waitForTimeout(25);
      for (const e of (await events(page)).slice(before)) {
        if (e.type === "kicanvas:select" && e.ref && known.includes(e.ref)) {
          hits.push({ x, y, ref: e.ref, via: e.via, kind: e.kind });
        }
      }
    }
  }
  return hits;
}

for (const [q, file, known] of [
  ["Q4", ECC83_SCH, SCH_REFS],
  ["Q5", ECC83_PCB, PCB_REFS],
] as const) {
  test(`${q} click emits select event with reference (${file})`, async ({ page, browser }) => {
    // controls="none" sets disableinteraction on the inner viewer (clicks ignored by design).
    await openViewer(page, [file], INTERACTIVE);
    const attached = await page.evaluate(() => (window as any).__attachViewer());
    record(q, "viewer_found_via_shadow_dom", attached, { attached });

    const hits = await gridScan(page, [...known]);
    const channels = [...new Set(hits.map((h) => h.via))];
    record(q, "dom_level_select_event", channels.some((c) => c !== "viewer"), channels);
    record(q, "viewer_level_select_event", channels.includes("viewer"), {
      channels,
      refs: [...new Set(hits.map((h) => h.ref))],
      kinds: [...new Set(hits.map((h) => h.kind))],
    });
    expect(hits.length).toBeGreaterThan(0);

    // Deterministic re-check: fresh page, one click at the first hit's position.
    const target = hits[0];
    await openViewer(page, [file], INTERACTIVE);
    await page.evaluate(() => (window as any).__attachViewer());
    await page.mouse.click(target.x, target.y);
    await page.waitForTimeout(100);
    const ev = (await events(page)).filter((e) => e.type === "kicanvas:select");
    const ok = ev.some((e) => e.ref === target.ref);
    record(q, "single_click_on_known_item_reports_reference", ok, { target, ev });

    // Same click with the default focus overlay (controls="basic" without "nooverlay").
    await openViewer(page, [file], { controls: "basic" });
    await page.evaluate(() => (window as any).__attachViewer());
    await page.mouse.click(target.x, target.y);
    await page.waitForTimeout(100);
    const overlay = (await events(page)).filter((e) => e.type === "kicanvas:select");
    record(q, "first_click_with_focus_overlay_reports_reference", overlay.some((e) => e.ref === target.ref), {
      target,
      ev: overlay,
    });

    // Touch: a tap on a touch device (tap -> @REF on phones).
    const ctx = await browser.newContext({ hasTouch: true, isMobile: false, viewport: { width: 1400, height: 900 } });
    const tpage = await ctx.newPage();
    await openViewer(tpage, [file], INTERACTIVE);
    await tpage.evaluate(() => (window as any).__attachViewer());
    const taps: Ev[][] = [];
    for (let i = 0; i < 2; i++) {
      await tpage.touchscreen.tap(target.x, target.y);
      await tpage.waitForTimeout(300);
      taps.push((await events(tpage)).filter((e) => e.type === "kicanvas:select"));
    }
    record(q, "touch_tap_reports_reference", taps.some((t) => t.some((e) => e.ref === target.ref)), {
      target,
      afterFirstTap: taps[0],
      afterSecondTap: taps[1],
    });
    await ctx.close();
    expect(ok).toBe(true);
  });
}

test("Q6 programmatic select by reference (schematic)", async ({ page }) => {
  await openViewer(page, [ECC83_SCH], INTERACTIVE);
  await page.evaluate(() => (window as any).__attachViewer());
  const result = await page.evaluate(() => {
    const v = (window as any).__findViewer();
    v.select("U1");
    const sel = v.selected;
    return { hasSelected: !!sel, ref: (window as any).__refOf(sel?.context) };
  });
  const ev = (await events(page)).filter((e) => e.via === "viewer" && e.ref === "U1");
  record("Q6", "select_by_reference", result.ref === "U1" && ev.length > 0, { result, ev });
  expect(result.ref).toBe("U1");
});
