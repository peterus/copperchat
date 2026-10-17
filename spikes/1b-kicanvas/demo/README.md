# KiCanvas evaluation demo (patched KiCanvas, KiCad 11 files)

Throwaway code. It lets the owner try patched KiCanvas in a browser (desktop and phone)
before Phase 6B is decided. This is **not** Phase 6B. Spike results: `docs/spikes/1b-kicanvas.md`.

    ./build.sh                                            # vendor/, files/, fonts/, projects.json (all gitignored)
    setsid nohup ./serve.sh >/tmp/kicanvas-demo.log 2>&1 < /dev/null &
    # → http://localhost:8765/  and  http://<LAN-IP>:8765/
    kill "$(cat /tmp/kicanvas-demo.pid)"                  # stop (only that PID)

Checks (with the server running), from `spikes/1b-kicanvas/`:

    npm ci --include=dev && npx playwright install chromium
    timeout 3000 npx playwright test -c demo/playwright.config.ts --reporter=line   # demo/results/

## What gets built
- **KiCanvas** at `KICANVAS_COMMIT` (`b031159`) with four patches (`../patches/`), applied in order
  by `fetch_kicanvas.sh` (`KICANVAS_PATCH` now takes a space-separated list):
  1. `kicad11-footprint-transform.patch`: from the spike. Footprint `(transform …)` → `at`.
  2. `embed-select-event.patch` (new, ~10 lines): `kc-*-viewer` re-dispatches the Viewer's
     `kicanvas:select` as a bubbling, composed DOM event, so
     `embed.addEventListener("kicanvas:select", e => e.detail.item.reference)` works. No shadow-DOM walk.
  3. `embed-project-getter.patch` (new, 5 lines): `embed.project` exposes the loaded `Project`.
     The demo uses it for the hierarchical sheet list (`pages()`, `set_active_page()`).
  4. `no-external-fonts.patch` (new): removes the runtime `fonts.googleapis.com` stylesheet.
     The page self-hosts Nunito (OFL) and Material Symbols Outlined (Apache-2.0) from npm in `fonts/`.
  Bundle: 477 725 B, sha256 `f97ebe59…6ac6` (`vendor/BUILD_INFO`).
- **Files**: `prepare_files.sh` runs inside `ghcr.io/peterus/copperchat-kicad-base:0616634…`
  on copies of the image's `/usr/share/kicad/demos`. It uses `kicad-cli sch/pcb upgrade --force`,
  and nothing is hand-edited. `make_manifest.py` rejects any file whose header is not KiCad 11
  (sch ≥ 20260830, pcb ≥ 20260901, generator 10.99). It also lists a root sheet plus the sub-sheets
  it references, so orphan sheet files are left out.
  Projects: `ecc83-pp`, `ecc83-pp_v2` (small), `complex_hierarchy` (hierarchical, 2 sheet files /
  3 instances), `pic_programmer`, `video` (8 sheets, 5.9 MB board), and `jetson-agx-thor-baseboard`
  (15 sheets, **85.5 MB** board, stress test).

## Page
Project picker · Schematic/PCB tabs (one lazily created `<kicanvas-embed>` per tab, with only that
tab's files) · side panel showing the clicked/tapped reference (`@REF` chip) · sheet list for
hierarchical projects · "Highlight REF" (`viewer.select(ref)`) · Light/Dark toggle (sets
`kc:prefs:theme`, then reloads, because KiCanvas reads it only at module load) · `controls="basic"
controlslist="nooverlay nodownload"` · load-error detection · "What to try" checklist.
Responsive: below 760 px the panel moves under the viewer.

Load-error detection: any `error` / `unhandledrejection` on `window` while a tab is loading marks
the tab **failed** and shows a red banner. This is needed because KiCanvas sets `loaded` even when
paint throws. Errors after a successful load (e.g. clicking copper, see below) show an amber notice
instead and do not mark the tab failed. A tab counts as loaded once the inner `kc-*-viewer` has
`loaded`, plus 750 ms for the first paint. The demo then calls `zoom_to_board()` on boards,
because the embed's `zoom` attribute is not forwarded at this commit.

## Results (2026-10-17, Playwright 1.64 / Chrome Headless Shell, SwiftShader = CPU WebGL, i5-8500T)

All 20 checks PASS. Times are from kicanvas-embed creation until the viewer reports `loaded`, plus
CPU rasterisation in SwiftShader. A real GPU only speeds up the drawing part.

| Project | Tab | Size | Load | Click → ref in panel |
|---|---|---|---|---|
| ecc83-pp | sch / pcb | 55 kB / 161 kB | 0.12 s / 0.16 s | U1 / P3 |
| ecc83-pp_v2 | sch / pcb | 55 kB / 174 kB | 0.13 s / 0.40 s | U1 / P5 |
| complex_hierarchy | sch / pcb | 2 files / 0.9 MB | 0.20 s / 0.40 s | U101 / U102 |
| pic_programmer | sch / pcb | 2 files / 0.65 MB | 0.22 s / 0.30 s | Q2 / R10 |
| video | sch / pcb | 8 files / 5.9 MB | 0.42 s / 1.7 s | J4 / U2 |
| jetson-agx-thor | sch / pcb | 15 files / **85.5 MB** | 1.5 s / **129.5 s** | sheet "CSI" (root has only sheet boxes) / SW2 |

| Check | Result |
|---|---|
| Mobile 390×844, touch, DPR 3: tap → ref (ecc83 sch, ecc83 pcb, video pcb) | PASS (U1, P5, U23) |
| Offline: every non-local request aborted, **0 external requests**; Nunito + Material Symbols loaded | PASS |
| Theme: schematic dark lum 0.07 → light 0.94; board stays black in light (no light board theme) | PASS (as expected) |
| Hierarchy: `complex_hierarchy` sheet list `Root / ampli_ht_vertical / ampli_ht_horizontal`, switching renders the sub-sheet | PASS |
| Highlight REF `U1` on schematic and board → panel shows U1 | PASS |
| Load-error detection: board served with `(transform` renamed (network-level, file untouched) → status failed + red banner | PASS |

## What does not work / caveats (honest list)
- **85 MB board: 130 s to first render here and ~1.4 GB JS heap.** That rules it out for a phone.
  Some of the time is CPU raster (SwiftShader), but parsing 85 MB is CPU-bound anyway. KiCanvas
  also logs one `console.warn` per unknown s-expression element. The jetson board logs 16 167
  (the video board 3 625), e.g. `No definition found for element tenting / covering / plugging /
  spec_frequency / dielectric_model`, i.e. KiCad 11 tokens it doesn't know. This flood crashed the
  Playwright worker (Node OOM), so the tests now stub `console` in the page. With DevTools open,
  a browser will also be slow. A 6B patch could silence parser warnings.
- **Net highlight is broken on KiCad 11 boards.** Clicking a track/zone/pad net calls
  `getNetNumber()`, which throws `Cannot read properties of undefined (reading 'number')`,
  because KiCad 11 writes nets by name (`(net "GND")`). The demo shows an amber notice. Footprint
  selection is unaffected. Fixing this needs a further parser/painter patch (not done).
- **Boards have no light theme** (KiCanvas ships only `witchhazel` and `kicad`; boards are black in both).
- Theme switch needs a page reload (the preference is read at module load). The embed `theme` and
  `zoom` attributes are not forwarded at this commit.
- Schematic `select("REF")` only searches the **current sheet**.
- Entering a sub-sheet by double-clicking it works through KiCanvas. Getting back up needs the
  demo's sheet list (no breadcrumb in `controls="basic"`).
- Board clicks where several selectable items overlap (footprint + net) may open KiCanvas'
  own small context menu instead of selecting.
- Material Symbols woff2 is 4 MB (full icon set, for 3–4 toolbar icons). Subset it in 6B.
- **Not verified here:** real phones / iOS Safari, pinch-zoom (Playwright can't synthesise
  multi-touch), GPU WebGL. Those are for the owner to try.

Port note: `serve.sh` uses `DEMO_PORT` (default 8765), not `PORT`, because the Kontor host
exports `PORT=3001`. The spike's own Playwright config also uses 8765; stop the demo before
running the spike suite.
