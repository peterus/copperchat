# Spike 1B — KiCanvas with KiCad 11 files

Status: done (2026-10-16). Code: `spikes/1b-kicanvas/`. CI does not run spikes.

**Gate result: 1B no-go** for unpatched KiCanvas: KiCad 11 boards do not render.
A 66-line parser patch (`spikes/1b-kicanvas/patches/kicad11-footprint-transform.patch`)
fixes it, and every gate check then passes. The patched build is a supplementary
result; it does not change the mechanical gate. Recommendation and options are in
"Decision".

## Question(s)
Q1 schematic renders · Q2 board renders · Q3 hierarchical schematic renders ·
Q4 click on symbol → event with reference (DOM vs viewer level) · Q5 same for
footprints · Q6 select by reference · Q7 offline render · Q8 themes ·
plus license, vendoring/pinning, bundle size.

## Setup
- KiCanvas commit `b031159eb74aaa7eef2b026fd85d35bc05ff2095` (2026-04-29; still
  `main` on 2026-10-16, so the commit from the plan is unchanged), built with
  node v20.20.1 / npm 11.13.0. Bundle `kicanvas.js` 477451 B (112184 B gzip -9),
  sha256 `ca910f25276c3efb9aacb3a5d6341d4d9af4736d4c875fb0440d2cc856865ab7`.
  Patched build: 477859 B, sha256 `c383f003e6dcde653278997397b00f3811180b7ec235754a68597be0907d861d`.
- Build note: the host environment exports `NODE_ENV=production`. With that set,
  `npm ci` skips devDependencies and `npm run build` fails with `tsc: not found`.
  `fetch_kicanvas.sh` therefore runs `npm ci --include=dev`. Phase 6B and any
  Dockerfile must do the same.
- KiCad nightly `kicad-nightly-10.99.0.5265.g0616634034-x86_64-lite.AppImage.tar`,
  commit `0616634034fa1a8c92c141c293342cbef2f68dff` (2026-10-11). Demo files from
  `~/build/kicad/demos/{ecc83,complex_hierarchy}` were copied, then
  `kicad-cli sch upgrade --force` / `pcb upgrade --force` were run on the copies.
  Format versions: schematic `20250114 → 20260830`, board `20241229 → 20260901`,
  `generator_version "10.99"` (all 5 files PASS in `verify_files.py`). Nothing
  was hand-edited.
- Chrome Headless Shell 156.0.8078.4 via Playwright 1.64.1, SwiftShader WebGL
  (`--use-angle=swiftshader`). All runs are headless on this host. Real touch
  hardware and iOS Safari were **not** tested.
- Commands: see `spikes/1b-kicanvas/README.md`. Supplementary run:
  `KICANVAS_PATCH=patches/kicad11-footprint-transform.patch VENDOR_DIR=vendor-patched ./fetch_kicanvas.sh`
  then `KICANVAS_VENDOR=vendor-patched npx playwright test` (results go to `results-patched/`).

## Results

Gate run (unpatched KiCanvas, `results/summary.md`):

| Question | Check | Unpatched | Patched |
|---|---|---|---|
| Q1 | canvas_non_empty / no_page_errors | PASS / PASS | PASS / PASS |
| Q2 | canvas_non_empty / no_page_errors | **FAIL / FAIL** | PASS / PASS |
| Q3 | canvas_non_empty / no_page_errors | PASS / PASS | PASS / PASS |
| Q4 | viewer_found_via_shadow_dom | PASS | PASS |
| Q4 | dom_level_select_event | FAIL (expected) | FAIL (expected) |
| Q4 | viewer_level_select_event | PASS | PASS |
| Q4 | single_click_on_known_item_reports_reference | PASS | PASS |
| Q4 | first_click_with_focus_overlay_reports_reference | FAIL (finding) | FAIL (finding) |
| Q4 | touch_tap_reports_reference | PASS | PASS |
| Q5 | viewer_found_via_shadow_dom | PASS | PASS |
| Q5 | dom_level / viewer_level select event | **FAIL / FAIL** (board not rendered) | FAIL (expected) / PASS |
| Q5 | single_click_on_known_item_reports_reference | **not reached** | PASS |
| Q5 | first_click_with_focus_overlay_reports_reference | not reached | FAIL (finding) |
| Q5 | touch_tap_reports_reference | not reached | PASS |
| Q6 | select_by_reference (schematic) | PASS | PASS |
| Q7 | canvas_non_empty_offline | PASS | PASS |
| Q8 | embed_theme_attribute_applied | FAIL (finding) | FAIL (finding) |
| Q8 | preferences_theme_applied | PASS | PASS |
| Q8 | light_theme_available | PASS (schematic only) | PASS (schematic only) |

**Q1 / Q3 schematic.** KiCad 11 schematics (format 20260830) render with no
page or console errors. ecc83: 80 distinct colours, 2.8 % non-background.
complex_hierarchy root + `ampli_ht` sub-sheet as two `kicanvas-source`s: 101
colours, 2.9 %.

**Q2 board: FAIL.** The board stays blank (1 colour) and the page throws
`TypeError: Cannot read properties of undefined (reading 'position')` at
`FootprintPainter.paint` (`r.at.position.x`). The `kicanvas:load` event still
fires and `loaded` is set, so a client cannot detect the failure from the
load state. The cause is a KiCad 11 format change. Each footprint's placement
`(at 141.605 99.695 90)` became

```
(transform
    (translate 141.605 99.695)
    (rotate 90)
    (scale 1 1)
)
```

so KiCanvas' `Footprint` never sets `at`. Control experiment: the
*original* (un-upgraded, KiCad 9/10 format) `ecc83-pp.kicad_pcb` and
`complex_hierarchy.kicad_pcb` load in the same build with no error. Both upgraded
boards fail the same way. Other format changes seen in the token diff: the
top-level `(net N "name")` table is gone and pads/tracks use `(net "GND")` by
name. Pad/track `net` used to be numeric, so KiCanvas' net handling (e.g. net
highlight) may also be wrong. That did not show up in rendering or picking here.

**Patch (supplementary).** `patches/kicad11-footprint-transform.patch` (66 lines,
`src/kicad/board.ts` only) adds a `FootprintTransform` item parser for
`(transform (translate x y) (rotate r) …)` and derives `Footprint.at` from it
when `(at …)` is absent. `scale` is ignored; the demo only uses `1 1`. With it the
ecc83 board renders (106 colours, 4.3 % non-background; zone fill, pads and
traces line up visually) with no page errors. Footprint picking then works.

**Q4 / Q5 events.** `kicanvas:select` is delivered **only on the `Viewer`**
(a plain `EventTarget`), not at document/window/element level, at the pinned
commit (as predicted from source). Channel `viewer`. The item constructors are
`SchematicSymbol` (schematic) and `Footprint` (board). The reference path is
`event.detail.item.reference`: `SchematicSymbol.reference` is a getter that
yields the reference without the unit letter (`U1` for unit U1A);
`Footprint.reference` is a field. The grid scan found refs C1, U1, C2, R3
(schematic) and P5, U1, P8, P4 (board, patched). A single click on a fresh
page reports exactly the expected ref. The board results come from the
patched run.

Viewer lookup path (works, `viewer_found_via_shadow_dom` PASS): breadth-first
walk from `document` through `shadowRoot`s to the first element with a `viewer`
property that has `addEventListener`. That element is `kc-schematic-viewer` /
`kc-board-viewer` inside `kicanvas-embed` → `kc-*-app` (see
`window.__findViewer` in `www/index.html`).

Interaction findings that change how Phase 6B embeds KiCanvas:
1. `controls="none"` sets `disableinteraction` on the inner viewer, and clicks
   are ignored by design. The click checks therefore run with `controls="basic"`.
2. `controls="basic"` adds a `kc-ui-focus-overlay` ("click to interact") that
   swallows the first click (`first_click_with_focus_overlay_reports_reference`
   FAIL). `controlslist="nooverlay"` removes it. With
   `controls="basic" controlslist="nooverlay"` the first click selects.
3. The viewer picks at `mouse_position`, which only `mousemove`/`panzoom`
   update; the click handler does not read its own coordinates. Real pointers and
   synthesized touch taps both move first, so this did not cause failures.
4. Touch: `touchscreen.tap` in a `hasTouch` context selects on the first tap
   (emulated Chromium; real phones not tested).

**Q6.** `viewer.select("U1")` on the schematic viewer selects U1. It emits a
viewer-level `kicanvas:select` with `ref: "U1"` and sets `viewer.selected`
(BBox, `.context` = the symbol). The board viewer's `select()` also accepts
`Footprint | string | BBox | null` (source, `src/viewers/board/viewer.ts:94`),
but that was not exercised in a test.

**Q7 offline.** The bundle injects
`https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined…&family=Nunito…`.
With every non-127.0.0.1 request aborted, the schematic still renders
identically (80 colours, 2.8 %). The only external request attempted was that
font stylesheet. Fonts affect only the UI chrome (icons in controls), not the
canvas.

**Q8 themes.** The `theme` attribute on `kicanvas-embed` is **ignored**: the
embed declares it but never forwards it to `kc-*-app`/viewer
(`embed_theme_attribute_applied` FAIL; witchhazel and kicad both give
luminance 0.07). The theme *does* apply through KiCanvas' `Preferences`
(localStorage key `kc:prefs:theme`, value `{"val":"kicad"}`, read when the
module loads): schematic background `witchhazel` [16,16,24] (lum 0.07) vs
`kicad` light (lum 0.94). Boards are dark in **both** themes (patched: black
[0,0,0], lum 0.00). KiCanvas has **no light board theme**; there are only the two
themes, `witchhazel` (default) and `kicad`. The `theme` attribute *is* honoured on
the inner `kc-*-viewer` elements (`viewer.ts` `themeObject`), so a one-line
embed patch forwarding it would also work.

**License.** `LICENSE.md` = MIT ("available under the MIT License",
`check_vendor.sh` PASS) plus ISC/CC0/OFL/Apache-2.0 bundled assets, all
GPL-3.0-compatible. No `kicanvas:click` event exists in the bundle.

## Decision
- **1B no-go** (mechanical gate): Q2 FAIL and Q5 not reachable with KiCanvas
  unpatched at `b031159`. Under the index gate, Phase 6B would implement the
  own-viewer fallback (server-side render + `/api/projects/{id}/hit-test`).
- **Supplementary result:** with the 66-line
  `patches/kicad11-footprint-transform.patch` applied at build time, Q1, Q2 PASS
  and Q4, Q5 `single_click_on_known_item_reports_reference` PASS. Under the gate
  definition, that is **"1B go"** for a *patched* KiCanvas.
- **Recommendation (needs an owner/coordinator decision before Phase 6B):** take
  **"1B go with carried patch"**. Phase 6B uses KiCanvas built at `b031159`
  plus the patch(es) below, instead of building its own viewer. The patch is
  small and isolated (one parser class). An own viewer would be far more work
  (renderer, hit-testing, pan/zoom, themes). Risks: KiCanvas upstream has had no
  commit since 2026-04-29, so CopperChat would maintain the patch(es) itself and
  must re-run this spike's suite whenever the KiCad pin moves. Further KiCad 11
  format changes (net-by-name; anything that
  appears after nightly 0616634034) may need more patches. Offering the patch
  upstream is possible, but it is an outward-facing action and was not done.
- License: MIT (+ ISC/CC0/OFL/Apache-2.0 assets), GPL-3.0-compatible. Vendor
  `LICENSE.md` next to the bundle.

## Follow-ups for later phases
- **Phase 6B vendoring (on "go with carried patch"):** build at commit `b031159`
  with the recipe in `spikes/1b-kicanvas/fetch_kicanvas.sh`
  (`KICANVAS_PATCH=…`, `npm ci --include=dev`) into
  `frontend/public/vendor/kicanvas/` (`kicanvas.js`, `LICENSE.md`, `BUILD_INFO`
  incl. `patch=`). Load via `<script type="module">`. Move the patch(es) to e.g.
  `frontend/vendor-patches/kicanvas/`. Re-run this spike's Playwright suite (or a
  6B smoke test on the Phase 4 fixtures) on every `KICAD_COMMIT` bump.
- **Patch scope to consider in 6B:** (1) footprint `transform` (done, required);
  (2) forward `theme` from `kicanvas-embed` to the apps (one line), or set
  `kc:prefs:theme` in localStorage before loading the bundle; (3) optionally
  re-dispatch `kicanvas:select` on the `kicanvas-embed` element (see below);
  (4) net-by-name pads/tracks if net highlighting is needed.
- **Load-failure detection:** `loaded`/`kicanvas:load` fire even when the board
  paint throws. 6B should treat an `error` on `window` during load as a render
  failure and show the agent-view PNG instead.
- **Event wiring:** only viewer-level works. Recommended: carry a small patch
  that re-dispatches `kicanvas:select` from `kc-*-viewer` as a bubbling,
  composed DOM event on `kicanvas-embed`. Since we patch anyway, that avoids a
  fragile shadow-DOM walk that depends on internal element structure. Without
  the patch, keep the lookup helper (`__findViewer` in `www/index.html`: find the
  element whose `.viewer` has `addEventListener`, then
  `viewer.addEventListener("kicanvas:select", e => e.detail.item?.reference)`),
  and re-attach after a source reload if the viewer gets re-created (not tested).
  Embed with `controls="basic" controlslist="nooverlay"` (`none` disables
  interaction; the overlay eats the first tap). Hide the basic buttons via
  `controlslist` / CSS if unwanted.
- **Programmatic select:** `viewer.select(ref)` works on the schematic (Q6) →
  usable for "highlight REF"; board `select(string)` exists in source, untested.
- **Theming:** names `witchhazel` (dark) and `kicad`. Schematic: CopperChat
  light → `kicad`, dark → `witchhazel`. Board: **no light theme**; both are dark.
  The embed `theme` attribute is ignored at this commit (see patch scope).
- **Offline:** the bundle requests Google Fonts (Nunito, Material Symbols).
  Rendering works without them. 6B should strip/replace the injected font link
  or self-host the fonts so no external request leaks from a self-hosted
  instance.
- **Reload for `view-updated`:** not tried in this spike. 6B must test whether
  replacing `kicanvas-source` children / `src` re-renders, or whether the embed
  must be re-created (and the viewer listener re-attached).
- **Not verified here:** real touch devices / iOS Safari, GPU WebGL (only
  SwiftShader), hierarchical *board* projects beyond the two demos, large boards
  (performance).
