# KiCanvas patches

Applied in the order of `series` by `../../scripts/vendor-kicanvas.sh` to KiCanvas at
`../../public/vendor/kicanvas/KICANVAS_COMMIT`. The built bundle is committed; CI rebuilds it
and fails on any difference (job `kicanvas-reproducible`). No upstream fork exists (yet).

| Patch | Why |
|---|---|
| `kicad11-footprint-transform.patch` | KiCad 11 boards place footprints with `(transform …)` instead of `(at …)`; without it boards do not render (spike 1B). |
| `embed-select-event.patch` | Re-dispatches `kicanvas:select` as a bubbling, composed DOM event, so the host listens on `<kicanvas-embed>`. |
| `embed-project-getter.patch` | `embed.project` (pages, active sheet) for the sheet tree, breadcrumb and cross-sheet search. |
| `no-external-fonts.patch` | No runtime `fonts.googleapis.com` stylesheet (self-hosted, offline). |
| `embed-host-integration.patch` | Bubbling `kicanvas:viewerload` per page load; `theme` attribute forwarded embed → app → viewer (theme change without page reload); `controlslist="nobottomtoolbar"` (the host draws its own zoom buttons). |
| `touch-pan-zoom.patch` | Pinch zooms by the finger-distance ratio about the pinch midpoint, two-finger pan, double tap zooms 2x; wheel zoom anchors at the cursor (upstream's anchor never applied); `viewport.zoom_about()` for host buttons. |
| `kicad11-nets-by-name.patch` | KiCad 11 references nets by name and drops the net table; nets are numbered after parsing so net highlight works instead of throwing. |
| `quiet-parser-warnings.patch` | Unknown s-expression elements are logged only at debug level, and the message (which stringified the whole enclosing expression) is no longer built eagerly — an 85 MB board loads in ~23 s instead of ~130 s. |
| `schematic-sheet-instances.patch` | Schematic viewers load the page (sheet instance), not just its file, so references follow the instance (a sheet placed twice has different references) and switching between instances of one file re-renders. |
| `board-pick-footprint.patch` | A tap on a part selects the part; nets are offered only where no footprint is hit (upstream opened a context menu whenever a footprint overlapped a pad/track/zone). |

When the KiCad pin (`docker/kicad-base/KICAD_COMMIT`) moves, run the e2e suite
(`frontend/e2e/viewer.spec.ts`) — it loads the KiCad 11 fixtures in the real bundle.
