# KiCanvas changes → fork `peterus/kicanvas`

CopperChat's KiCanvas changes no longer live here as patch files. They are commits on branch
[`copperchat`](https://github.com/peterus/kicanvas/tree/copperchat) of the fork
[peterus/kicanvas](https://github.com/peterus/kicanvas) (upstream:
[theacodes/kicanvas](https://github.com/theacodes/kicanvas), `main` of the fork mirrors it).
The branch is upstream `b031159` plus one commit per former patch; `COPPERCHAT.md` there
explains each commit.

`../../scripts/vendor-kicanvas.sh` builds the fork at the full SHA in
`../../public/vendor/kicanvas/KICANVAS_COMMIT`. The built bundle is committed; CI rebuilds it
and fails on any difference (job `kicanvas-reproducible`). The switch from patches to the fork
did not change the bundle (same sha256).

| Former patch | Group | Why |
|---|---|---|
| `kicad11-footprint-transform` | upstream candidate | KiCad 11 boards place footprints with `(transform …)` instead of `(at …)`; without it boards do not render (spike 1B). |
| `kicad11-nets-by-name` | upstream candidate | KiCad 11 references nets by name and drops the net table; nets are numbered after parsing so net highlight works instead of throwing. |
| `quiet-parser-warnings` | upstream candidate | Unknown s-expression elements are logged only at debug level, and the message (which stringified the whole enclosing expression) is no longer built eagerly — an 85 MB board loads in ~23 s instead of ~130 s. |
| `schematic-sheet-instances` | upstream candidate | Schematic viewers load the page (sheet instance), not just its file, so references follow the instance (a sheet placed twice has different references) and switching between instances of one file re-renders. |
| `touch-pan-zoom` | upstream candidate | Pinch zooms by the finger-distance ratio about the pinch midpoint, two-finger pan, double tap zooms 2x; wheel zoom anchors at the cursor (upstream's anchor never applied); `viewport.zoom_about()` for host buttons. |
| `embed-select-event` | CopperChat | Re-dispatches `kicanvas:select` as a bubbling, composed DOM event, so the host listens on `<kicanvas-embed>`. |
| `embed-project-getter` | CopperChat | `embed.project` (pages, active sheet) for the sheet tree, breadcrumb and cross-sheet search. |
| `no-external-fonts` | CopperChat | No runtime `fonts.googleapis.com` stylesheet (self-hosted, offline). |
| `embed-host-integration` | CopperChat | Bubbling `kicanvas:viewerload` per page load; `theme` attribute forwarded embed → app → viewer (theme change without page reload); `controlslist="nobottomtoolbar"` (the host draws its own zoom buttons). |
| `board-pick-footprint` | CopperChat | A tap on a part selects the part; nets are offered only where no footprint is hit (upstream opened a context menu whenever a footprint overlapped a pad/track/zone). |

The upstream candidates also exist as topic branches on upstream `main` in the fork
(`upstream/<name>`); no pull request against theacodes/kicanvas has been opened.

To change KiCanvas: commit to `copperchat` in the fork (never force-push over a pinned SHA),
put the new full SHA into `KICANVAS_COMMIT`, run `./scripts/vendor-kicanvas.sh &&
./scripts/check-kicanvas.sh` from `frontend/` and commit the rebuilt bundle.

When the KiCad pin (`docker/kicad-base/KICAD_COMMIT`) moves, run the e2e suite
(`frontend/e2e/viewer.spec.ts`) — it loads the KiCad 11 fixtures in the real bundle.
