import { expect, test } from "@playwright/test";
import { ECC83_PCB, ECC83_SCH, HIER, canvasStats, openViewer, record } from "./helpers";

function rendered(s: { distinctColours: number; nonBackgroundFraction: number }): boolean {
  return s.distinctColours >= 4 && s.nonBackgroundFraction > 0.005;
}

for (const [q, name, files] of [
  ["Q1", "sch", [ECC83_SCH]],
  ["Q2", "pcb", [ECC83_PCB]],
  ["Q3", "hier", HIER],
] as const) {
  test(`${q} ${name} renders KiCad 11 file`, async ({ page }) => {
    const errors = await openViewer(page, [...files]);
    const stats = await canvasStats(page, `${name}-witchhazel.png`);
    const ok = rendered(stats);
    record(q, "canvas_non_empty", ok, stats);
    record(q, "no_page_errors", errors.page.length === 0 && errors.console.length === 0, errors);
    expect(ok).toBe(true);
    expect(errors.page).toEqual([]);
    expect(errors.console).toEqual([]);
  });
}

test("Q7 renders offline (non-localhost requests blocked)", async ({ page }) => {
  const errors = await openViewer(page, [ECC83_SCH], { offline: true });
  const stats = await canvasStats(page, "sch-offline.png");
  const external = errors.requests.filter((u) => !u.startsWith("http://127.0.0.1"));
  record("Q7", "external_requests_attempted", true, external);
  record("Q7", "canvas_non_empty_offline", rendered(stats), stats);
  expect(rendered(stats)).toBe(true);
});

test("Q8 themes", async ({ page }) => {
  const out: Record<string, unknown> = {};
  for (const theme of ["witchhazel", "kicad"]) {
    for (const [name, file] of [["sch", ECC83_SCH], ["pcb", ECC83_PCB]] as const) {
      await openViewer(page, [file], { theme });
      out[`${name}-${theme}`] = await canvasStats(page, `${name}-${theme}.png`);
      // Same theme via KiCanvas' Preferences (localStorage) instead of the embed attribute.
      await openViewer(page, [file], { prefsTheme: theme });
      out[`${name}-${theme}-prefs`] = await canvasStats(page, `${name}-${theme}-prefs.png`);
    }
  }
  const attr = (n: string) => (out[n] as any).luminance;
  record("Q8", "embed_theme_attribute_applied", attr("sch-kicad") !== attr("sch-witchhazel"), {
    "sch-witchhazel": attr("sch-witchhazel"),
    "sch-kicad": attr("sch-kicad"),
  });
  record("Q8", "preferences_theme_applied", attr("sch-kicad-prefs") !== attr("sch-witchhazel-prefs"), {
    "sch-witchhazel-prefs": attr("sch-witchhazel-prefs"),
    "sch-kicad-prefs": attr("sch-kicad-prefs"),
  });
  // Only rendered captures count: a blank (failed) canvas says nothing about the theme.
  const lums = Object.values(out).filter((s: any) => rendered(s)).map((s: any) => s.luminance);
  record("Q8", "theme_backgrounds", true, out);
  record("Q8", "light_theme_available", lums.some((l) => l > 0.5), { renderedLuminances: lums });
  record("Q8", "themes_differ", new Set(lums).size > 1, lums);
  expect(Object.keys(out)).toHaveLength(8);
});
