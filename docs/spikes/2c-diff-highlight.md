# Spike 2C — diff and highlight

Status: done (2026-10-18). Code: `spikes/2c-diff-highlight/`. CI does not run spikes.

**Gate: 2C go** — with a twist: the diff layer is **not** the PNG/SVG of
`kicad-cli sch diff` (not usable) but boxes drawn by KiCanvas from the diff's JSON
(`bbox` per change, per sheet instance), which needs a small fork patch (set highlight).
The JSON also cross-checks a tool-recorded change log, and it caught three side effects
a tool log would have missed.

## Question(s)
1. `kicad-cli sch diff` between a snapshot copy and an API-edited copy: all formats (JSON, text,
   PNG, SVG), JSON shape (UUID/ref, added/removed/modified, fields), hierarchical schematics,
   runtime. Are PNG/SVG usable as a "diff layer"? Can the JSON cross-check a change log?
2. KiCanvas (vendored fork build): highlight a **set** of refs at once, clear it, across sheets;
   minimal patch if needed.

## Setup
- KiCad `0616634034fa1a8c92c141c293342cbef2f68dff` (`kicad-cli` 10.99.0) in the local image
  `ghcr.io/peterus/copperchat-kicad-base:0616634034…-r41da5e29cdd40d32`; `LANG=C`, throw-away
  `KICAD_CONFIG_HOME`; kipy from `/opt/venv` of that image (the foundation's pin). Source of the
  command: `kicad/cli/command_sch_diff.cpp` and `eeschema/diff_merge/sch_differ.*` at that commit
  (identity = `KIID_PATH` = sheet instance path + item UUID, so it is instance-aware by design).
- Fixtures `ecc83` (26 symbols, 1 sheet) and `complex_hierarchy` (3 sheet instances, `ampli_ht`
  placed twice, 119 symbol instances), copied from `backend/tests/fixtures/projects/`.
- Pairs (`make_pairs.py`): `before/` = pristine project copy (= what a snapshot copy is), `after/` =
  copy edited **only through the IPC API** (`update_items`, `create_items`, `remove_items`,
  `place_symbol_from_library`, `save`). `ecc83_edit`: value change, symbol move, wire + label +
  junction added, wire deleted, `Device:R` placed as R99. `hier_edit`: value change of R201 on a
  sub-sheet instance, wire + label added there, value change on the root. `noedit`: open + save only.
- KiCanvas: the vendored bundle (fork `peterus/kicanvas` @ `2fbd2af`, 479 820 B) as **baseline**,
  and the same commit + `patches/schematic-highlight-set.patch` (+64/−1 lines in
  `viewers/schematic/viewer.ts`, bundle 480 927 B) as **patched**. Chromium headless (Playwright
  1.64.1, SwiftShader), page `viewer/index.html`, files served from the pairs.

## Results

### `kicad-cli sch diff`

| Check | Result |
|---|---|
| Formats | PASS — `json`, `text`, `png`, `svg` all work (`png`/`svg` need `-o`); exit code 0 = identical, **5 = differences** (not an error); `--exit-code-only` works |
| Noise (`noedit`: open + `save()` vs. the original) | PASS — 0 changes, exit 0 (saving rewrites nothing the differ sees) |
| JSON shape | PASS — `{docType, path, changes[]}`; each change `{id, kind: added/removed/modified, typeName (SCH_SYMBOL, SCH_LINE, SCH_LABEL, SCH_JUNCTION, …), refdes (symbols only), bbox [x,y,w,h], properties[{name, before:{type,v}, after:{type,v}}], children[]}` |
| Identity | PASS — `id` = `/<root UUID>/<sheet instance UUID…>/<item UUID>`; the item UUID equals the API's `item.id.value` (also the legacy `00000000-…-00004549f3ad` ids). Sheet path prefix equals KiCanvas' page `sheet_path` |
| Modified items | PASS — only the changed properties are listed (`Value: 10uF → 22uF`, `Position X`). Added/removed items list all properties (11–23 entries; noise for the UI) |
| bbox | PASS — units are 100 nm (`/1e4` = mm). For a modified item it is the **after** box (moved R3: diff 181.53/78.48/10.11/13.23 mm vs. KiCanvas box of the after file 181.47/78.34/10.30/13.49, before file w = 6.26); removed → the before box |
| Hierarchy (`hier_edit`) | PASS — a change in the shared `ampli_ht` file appears **once per sheet instance** (same item UUID, different sheet path, instance-specific `refdes`: R201 under `…1333`, R301 under `…13a4`): 4 edits → 7 changes. The diff of the root file loads sub-sheets from the same directory, so both copies must be full project directories with all sheets saved |
| Runtime | PASS — 0.41–0.48 s per call (median of 7; process start dominated; ecc83 0.42 s, complex_hierarchy 0.475 s JSON, `--exit-code-only` 0.44 s) |
| PNG as a diff layer | **FAIL** — 1024 px wide, only filled rectangles and junction dots are visible (wires and outlines are hairlines and vanish); no symbol graphics, no text; the identical pair (`noedit`) yields a similar 1024×813 picture of dots |
| SVG as a diff layer | **FAIL (as user-facing)** — A3 page with grey 0.55-opacity outlines of every item's bbox plus filled rects (yellow modified, green added, red removed); no text, no symbol shapes; all sheets and both instances of a shared sheet are drawn into **one** canvas at their own sheet coordinates (the 7 changes of `hier_edit` sit on top of the root page, R201/R301 overlap exactly). Readable by an engineer as a bbox map, not by a phone user |
| Cross-check with a change log | PASS (with findings) — see below |

**Cross-check** (`harness/crosscheck.py`, key = item UUID; `results/crosscheck.json`):

| Pair | Logged | Diff changes | Matched (op agrees) | Logged, not in diff | In diff, not logged |
|---|---|---|---|---|---|
| `noedit` | 0 | 0 | – | – | – |
| `ecc83_edit` | 7 | 9 | 6 | 1 (junction) | 3 |
| `hier_edit` | 4 | 7 (2 instances) | 4 | 0 | 0 |

The mismatches are real behaviour of the headless server (`harness/collateral.py`, `results/collateral.json`):
- A **junction created by `create_items` where no ≥3-way connection exists is dropped at once** (not in
  `get_junctions` right after the call, not in the saved file) although the call succeeds.
- **`remove_items` of a wire triggers cleanup immediately** (not at save): a collinear wire was merged
  into its neighbour (one wire modified, one removed) and an existing junction was removed — three
  changes the tool never asked for.
- Updating `position` of a symbol did not move its fields along (the KiCanvas box of R3 keeps its x and
  grows by about the move distance) — consistent with 2A's "field auto-placement is skipped"; the diff
  shows only `Position X`.
- `update_items` on the **unbound** `Schematic` returns the new value but silently does not change a
  symbol on a sub-sheet (re-read: old value, file unchanged); on `sch.for_sheet(instance)` it works
  (`results/probe_subsheet_update.json`). The first `hier_edit` run lost its sub-sheet edit this way and
  the diff showed exactly one change instead of two — the diff caught it.

### KiCanvas highlight of a set (`tests/highlight.spec.ts`, 23 checks, all PASS; `results/viewer.jsonl`)

| Question | Baseline (vendored) | Patched |
|---|---|---|
| API for a set | none: `viewer.select(item)` is single; `select(["C105","C101"])` throws "Unable to select item C105,C101" | `viewer.highlight(refs\|uuids, css)` → `{found, missing}`, `viewer.highlight_boxes([{x,y,w,h}] mm)`, `viewer.clear_highlight()`, `viewer.highlighted_count` |
| Set of 5 refs on a sheet | – | all 5 found, `NOPE99` reported missing, 1049 highlight pixels vs. 0 (screenshot `results/p1-highlight-set.png`) |
| Clear | – | screenshot is **byte-identical** to the one before highlighting |
| With a selection | – | highlight survives `select(ref)`; clearing the selection keeps it |
| Across sheets | `select(ref)` finds refs on the active page only | on the root page `["C105","R201","R301"]` → found `C105`, missing `R201,R301`; after `project.set_active_page(page)` + `kicanvas:viewerload` the instance's refs are found (R201 on `ampli_ht_vertical`, R301 on `ampli_ht_horizontal` — same symbol, different ref per instance). The highlight is viewer state: the first patch version kept stale boxes after a page switch (coordinates of the other page); the patch resets it in `load()`, so the host re-applies after each `kicanvas:viewerload` |
| Items without a symbol object | `find_symbol` knows symbols/sheets only (by ref or UUID); wires, labels, junctions, deleted items cannot be found | `highlight_boxes` takes the diff's `bbox / 1e4` (mm): the box of C105 from the diff matches KiCanvas' own symbol box within 0.3 mm |
| Scale / speed | – | 10 refs: 3–10 ms; 500 boxes: 6 ms to set, 0 ms to clear |

Not covered: real touch / iOS Safari; the dark theme (`witchhazel`) — the highlight colour `#ff8c00`
was only seen on the `kicad` theme; box schematics larger than complex_hierarchy; PCB.

## Decision

**2C go** (change log cross-check + diff layer from JSON bboxes drawn by KiCanvas; PNG/SVG of
`kicad-cli sch diff` are not used). Fallback if the fork patch were refused: the same change list
without a diff layer (partial: change log + previews only).

Gate lines: `2C go | partial: change log + previews only | no-go` → **2C go**.

## Follow-ups for the implementation phases
1. **Spec §8.2**: replace "PNG/SVG diff layer" by "diff JSON → per-instance boxes in the viewer
   (added green / modified orange / removed red ghost at the before position)".
2. **Fork patch** (CopperChat group, on `copperchat` branch): productionise
   `patches/schematic-highlight-set.patch` — own overlay layer (not the selection's), colour per kind,
   theme-aware, reset on `load()`, accept symbols/sheets by ref/UUID and raw boxes. The host groups
   changes by sheet-path prefix = page `sheet_path`, calls `set_active_page`, re-applies on
   `kicanvas:viewerload`; instance refs come from the diff's `refdes`.
3. **`views` diff service**: `kicad-cli sch diff --format json before/<x>.kicad_sch after/<x>.kicad_sch`
   on two **full project directory copies** (snapshot copy and a fresh `save_copy`, both with all
   sheets saved); exit 5 means "differences", not failure; ≈0.5 s. Compact the property lists of added
   and removed items before sending them to the UI.
4. **Change log**: build it from API read-back and cross-check with the diff in the editing tests
   (UUID key, expand shared-sheet items to their instances). Surface diff entries with no logged
   counterpart as "side effects" in the change list (wire merge, junction removed) and feed the
   count into the tool result — otherwise the user sees changes the agent never mentioned.
5. **For 2A and the edit tools**: sub-sheet edits must go through `sch.for_sheet(instance)` (the
   unbound call loses them silently — verify by re-read); do not create junctions by hand, read back
   whether the connection exists; deleting a wire can merge/remove neighbours; moving a symbol may
   leave its fields behind (verify and move fields explicitly if so).
6. Re-test the highlight on the dark theme and on a phone before the Changes view ships.
