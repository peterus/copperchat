# Spike 2C — diff and highlight

Throwaway code. Results: `docs/spikes/2c-diff-highlight.md`. CI never runs this.

Questions: what `kicad-cli sch diff` gives between a snapshot copy and an API-edited copy (formats,
JSON shape, hierarchy, runtime, cross-check against a tool-recorded change log), and whether the
vendored KiCanvas can highlight a *set* of refs (across sheets).

    ./run_all.sh          # everything below, needs docker (pinned kicad-base image), node, python3

| Step | What |
|---|---|
| `run.sh harness/make_pairs.py` | before/after pairs in `.work/pairs/`: `noedit` (open+save control), `ecc83_edit`, `hier_edit` (complex_hierarchy, sub-sheet instanced twice). The "after" copy is edited **only through kipy** (`update_items`, `create_items`, `remove_items`, `place_symbol_from_library`, `save`). `changelog.json` is what a tool-recorded change log would hold |
| `run.sh harness/collateral.py` | which API step causes changes nobody asked for (junction removed, wires merged) |
| `run.sh harness/probe_subsheet_update.py` | `update_items` on the unbound Schematic silently loses sub-sheet edits |
| `run.sh harness/run_diff.py`, `harness/timing.py` | `kicad-cli sch diff` in json/text/png/svg: exit codes, sizes, runtime (7 runs) |
| `harness/crosscheck.py` | change log ↔ diff JSON by item UUID → `results/crosscheck.json` |
| `build_patched.sh` | KiCanvas fork at the vendored pin + `patches/schematic-highlight-set.patch` → `.work/bundles/patched.js`; the vendored bundle is `baseline.js` |
| `npx playwright test` | `tests/highlight.spec.ts` against both bundles (`viewer/index.html`), results in `results/viewer.jsonl` and screenshots |

`run.sh` runs a harness script inside `ghcr.io/peterus/copperchat-kicad-base:0616634034…-r41da5e29cdd40d32`
(kicad-cli + kipy in `/opt/venv`, `LANG=C`, throw-away `KICAD_CONFIG_HOME`). KiCad files under
`.work/` are copies of `backend/tests/fixtures/projects/`; nothing is hand-edited or generated as text.
`results/samples/` holds one diff of every format per pair.
