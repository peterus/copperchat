# Spike 2A — headless schematic write round-trip

Throwaway. Not run in CI. Findings: `docs/spikes/2a-sch-write.md`.

Runs inside the pinned base image
`ghcr.io/peterus/copperchat-kicad-base:0616634034fa1a8c92c141c293342cbef2f68dff-r41da5e29cdd40d32`
(KiCad `0616634034`, kipy `87b945a0` in `/opt/venv`, stock libraries, demos);
`./run.sh CMD…` mounts this directory at `/spike` (override the image with `KICAD_IMAGE`).

1. `uv pip install --target .deps --python 3.13 pillow` — Pillow for Q9 (the image has none;
   `.deps` is on `PYTHONPATH` inside the container, gitignored).
2. `./run.sh ./make_fixtures.sh` — copies `ecc83` (ecc83-pp only) and `complex_hierarchy`
   from the image's demos and upgrades them with `kicad-cli sch upgrade`; creates
   `fixtures/blank` through the API (`CreateDocument` + `SaveDocument`) and byte-copies
   the ecc83 project symbol library + its `sym-lib-table` (`${KIPRJMOD}`) into it.
   Never hand-edits a KiCad file.
3. `./run.sh python run_all.py` — Q1–Q11; results in `results/*.json`,
   `results/summary.md`; saved projects for the viewer check in `results/files/`
   (gitignored).
4. KiCanvas (Q12, host): `cd kicanvas && npm ci --include=dev && npx playwright test
   --reporter=list` (serves the worktree root, loads the vendored
   `frontend/public/vendor/kicanvas/kicanvas.js`), then `python3 run_all.py --summary-only`.

A FAIL is a finding, not a broken harness: every FAIL is explained (with its
workaround check) in the findings document. `harness.py` follows the 1C harness
(`Recorder`, `Server`); `geom.py` holds the pin-end transform and bounding-box code
a future `geometry.py` would need.
