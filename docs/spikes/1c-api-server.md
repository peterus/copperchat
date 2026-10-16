# Spike 1C — headless kicad-cli api-server + kipy

## Question(s)
1. Nightly AppImage + full commit. 2. Exact `kicad-cli api-server` syntax.
3. kipy connect via `ipc://` socket; which kipy revision works.
4. GetItems on schematic (symbols, labels, wires, hierarchy, netlist) and board
   (footprints, pads, tracks, vias, nets, stackup, enabled layers).
5. SaveDocument; SaveCopyOfDocument to another path incl. sub-sheets.
6. PNG/SVG export jobs (sch + pcb); pixel→mm mapping.
7. `kicad-cli sch erc` / `pcb drc --format json` shape with `LANG=C`.
8. External file change → revert/reload. 9. kill -9 / SIGSTOP detection and
   restart timing. 10. Startup time. 11. Two servers in parallel.
12. Socket permissions. 13. Lock files. 14. UUID format.

## Setup
- Nightly: `kicad-nightly-10.99.0.5265.g0616634034-x86_64.AppImage.tar`
  (from `https://downloads.kicad.org/kicad/linux/explore/nightlies/download/`;
  the listing page links files under `/download/`, not the listing path itself)
- KiCad commit: `0616634034fa1a8c92c141c293342cbef2f68dff` (`kicad-cli version`:
  `10.99.0-5265-g0616634034`, release build, built Oct 12 2026)
- Host: Debian 13 (trixie) x86_64, Linux 6.12, 6 cores; Python 3.13.12 (uv-managed); uv 0.11.31
- Invocation: the extracted AppImage's `squashfs-root/usr/bin/kicad-cli` runs
  directly when `LD_LIBRARY_PATH` includes `squashfs-root/usr/lib` (the harness
  does this); `LANG=C`/`LC_ALL=C` are set for every call.
- api-server syntax: `kicad-cli api-server --socket SOCKET_PATH PROJECT.kicad_pro` (full `--help` below)
- Socket URL: `ipc://` + absolute socket path (e.g. `ipc:///tmp/c1-sock-5n2ycbwh/api.sock`);
  kipy: `KiCad(socket_path="ipc:///abs/path.sock", client_name=..., timeout_ms=...)`
- kipy revision: `87b945a0d56b26a78bf547db9771ff9bf3a057ea` (kicad-python `main` HEAD on
  2026-10-16, version `0.9.0.dev0`; source `https://gitlab.com/kicad/code/kicad-python.git`;
  its `kicad` submodule pins KiCad `dcadf9baa30d43d7d1b3d0342345f05ec0da26e1` for the protos).
  The first candidate (`main` HEAD) passed every check, so no bisecting was needed.
  - As a uv git source (`kicad-python = { git = "...", rev = "87b945a0..." }`) a cold
    `uv sync` works but takes 4 min 28 s and leaves a 3.5 GB uv cache, because uv
    clones the whole KiCad submodule. The spike itself uses a local checkout
    (`.kicad/kicad-python`, gitignored, `git submodule update --init --depth 1`: 38 s)
    as a path source — same revision, same protos.
  - `kipy.kicad_api_version.KICAD_API_VERSION` is `"unknown"` in both builds
    (shallow submodule, no tags), so kipy's runtime version check is skipped. Harmless.
  - The protos at `dcadf9ba` are older than the nightly's `api/proto`
    (`0616634034` adds 109 lines to `board/board_jobs.proto` and touches
    `board_types.proto`, `editor_commands.proto`, `project_commands.proto`).
    Everything this spike needs works with them; see Q5 for the export jobs.
- Fixtures: `ecc83/ecc83-pp.*` (+ `fp-lib-table`, `sym-lib-table`,
  `footprints.pretty`) and `complex_hierarchy/` copied from `~/build/kicad/demos`
  (KiCad tree at `69cafb907ebc09e7722a2a464b1d466fc2e3608d`), upgraded with
  `kicad-cli sch upgrade --force` / `kicad-cli pcb upgrade --force` (both flags
  accepted). `ecc83-pp_v2.*` is deliberately not copied: two `.kicad_pro` files
  in one directory would make the project ambiguous.

`kicad-cli api-server --help`:

```
Usage: kicad-cli api-server [--help] [--socket SOCKET_PATH] PROJECT_OR_FILE

Run the KiCad IPC API server in headless mode

Positional arguments:
  PROJECT_OR_FILE  Optional path to a .kicad_pro, .kicad_pcb, or .kicad_sch file to pre-load [nargs=0..1] [default: ""]

Optional arguments:
  -h, --help       Shows help message and exits 
  --socket         Override API socket path [nargs=0..1] [default: ""]
```

## Results

`uv run python run_all.py` on 2026-10-16 (`results/summary.md`):

| Question | Result | Failed checks |
|---|---|---|
| q01_cli | 4 pass / 0 fail |  |
| q02_connect | 12 pass / 0 fail |  |
| q03_get_items | 17 pass / 0 fail |  |
| q04_save | 8 pass / 1 fail | copy_subsheets_written |
| q05_export | 14 pass / 0 fail |  |
| q06_erc_drc | 8 pass / 0 fail |  |
| q07_lifecycle | 12 pass / 0 fail |  |

The one FAIL (`q04_save.copy_subsheets_written`) is a finding with a verified
workaround (`workaround_copy_saved_subsheets` PASS), see Q4. With
`KICAD_CLI=/nonexistent` every script prints the single SKIP line and
`run_all.py` exits 0.

### Connecting (Q2)
- The headless server **only preloads the project** when given a `.kicad_pro`;
  `GetOpenDocuments` then fails with `no handler available for request of type
  kiapi.common.commands.GetOpenDocuments` (the schematic/board handlers are only
  registered once the editor face has a document). The client must call
  `kicad.open_document("/abs/x.kicad_sch", DOCTYPE_SCHEMATIC)` and
  `kicad.open_document("/abs/x.kicad_pcb", DOCTYPE_PCB)` right after connect
  (0.32 s and 0.39 s for ecc83-pp). The harness's `Server` does this.
- After that, `get_open_documents(DOCTYPE_SCHEMATIC|DOCTYPE_PCB)` each return one
  specifier; `DOCTYPE_PROJECT` still has no handler.
- Schematic accessor: `kicad.get_schematic()` (public in this revision; it clears
  `sheet_path` so queries cover all sheets). Equivalent:
  `kipy.schematic.Schematic(kicad._client, kicad.get_open_documents(DOCTYPE_SCHEMATIC)[0])`.
  Board accessor: `kicad.get_board()`.

### GetItems coverage (Q3)

| Call | Works | Count (ecc83-pp / complex_hierarchy) | Notes |
|---|---|---|---|
| sch.get_symbols | y | 26 / 119 | `SchematicSymbolInstance(reference, value, unit, position, path)`; **all sheet instances** (root + both `ampli_ht` instances), power symbols (`#PWR…`) included, **one entry per placed unit** (U201/U301 units 1 and 2) — 70 non-power unit instances = 68 unique refs = `kicad-cli sch export netlist` component count |
| sch.get_labels | y | 0 / 9 | `LocalLabel(text, position)`; ecc83-pp simply has none |
| sch.get_lines (wires) | y | 37 / – | `SchematicLine(start, end, type=SLT_WIRE)` |
| sch.get_hierarchy | y | – / 3 sheet instances | `SheetInstance([1] complex_hierarchy (complex_hierarchy.kicad_sch))`, `[2] ampli_ht_vertical (ampli_ht.kicad_sch)`, `[3] ampli_ht_horizontal (ampli_ht.kicad_sch)` |
| sch.get_netlist | y | 13 nets (ecc83-pp) | `SchematicNet(name)` with member count per sheet path |
| board.get_footprints / pads / tracks / vias / nets | y | 15 / 33 / 59 / 0 / 14 | ecc83-pp has no vias; net 0 is the unnamed net |
| board.get_stackup / get_enabled_layers | y | stackup with silk/paste/mask/Cu layers / 20 layers | enabled layers are `BoardLayer` enum ints |

- Coordinates: kipy returns `Vector2` in **nm** (e.g. C1 at `Vector2(86360000, 57150000)` =
  86.36 mm / 57.15 mm on a 1.27 mm grid); mm = nm / 1e6 (confirmed).
- UUID format: 36-char lowercase hyphenated (`str(item.id.value)`). Items from
  pre-v6 files keep their legacy timestamp ids in that form, e.g.
  `00000000-0000-0000-0000-00004549f4be` — still full UUIDs, just mostly zeros.
- Symbol instance `path` is the sheet instance path (`/ROOT_UUID/SHEET_UUID`),
  so the same symbol in two sheet instances is two entries with different paths.
- No GetItems call was missing, so no `kicad-cli` workaround is needed.

### Save (Q4)
- SaveDocument: `sch.save()` / `board.save()` work. Even without any edit they
  **rewrite** the files: on the freshly upgraded `complex_hierarchy` the root
  `.kicad_sch` gains `(embedded_fonts no)`, the `.kicad_pcb` is re-ordered
  (7686 lines moved, same line multiset, same 364 segments), the `.kicad_pro`
  gains default settings (BOM presets etc.); the sub-sheet `ampli_ht.kicad_sch`
  only gets a new mtime. Consequence for Phase 7: record document hashes
  **after** CopperChat's own save, never before.
- Sub-sheets in SaveCopyOfDocument: **no**. `sch.save_as(dest/root.kicad_sch,
  overwrite=True, include_project=False)` writes only the root sheet
  (`SCH_EDIT_FRAME::SaveSchematicCopy` in `eeschema/api/api_handler_sch.cpp`;
  the schematic is one document, there is no per-sheet copy). The open document
  keeps its original path; originals are unchanged; the copied root is
  byte-identical to the just-saved root.
  - Trap: `kicad-cli sch export svg` on such a copy **succeeds** and plots the
    missing sub-sheet as an empty frame (74 KB vs 1.4 MB per sub-sheet SVG).
    A loadability check is not enough to detect a missing sub-sheet.
  - KicadSession.save_copy procedure: when the project is writable, `save()` the
    schematic, then `save_as()` the root into the destination and **file-copy** the
    sub-sheet files that KiCad just saved (byte copy, no content edits; sub-sheet
    names from `sch.get_hierarchy()`, relative paths preserved). Verified: with
    the copied sub-sheet, the sub-sheet SVGs are complete (1.4 MB). When the
    project is read-only (lock file present, no save allowed), the on-disk files
    are the state the agent sees (the foundation agent never edits), so file-copy
    of the root + sub-sheets is the snapshot. Board: `board.save_as(dest/x.kicad_pcb,
    overwrite=True, include_project=False)` is complete (one file).

### Export (Q5)
- PNG export: **API jobs work for both document types** with the pinned kipy:
  `RunSchematicJobExportPng` (`kipy.proto.schematic.schematic_jobs_pb2`) via
  `kicad.get_schematic().export_png(out_dir, dpi=300, sheet_mode=SJSM_ALL_SHEETS)`
  (one PNG per sheet in `out_dir`), and `RunBoardJobExportPng`
  (`kipy.proto.board.board_jobs_pb2`) via
  `kicad.get_board().export_png(out_file, plot_settings=ps, dpi=300)` with
  `ps = kipy.board_jobs.PlotSettings(); ps.layers = [...]; ps.scale = 1.0`.
  Both return `JobResult(succeeded=True, output_paths=[...])`; PNGs are RGBA with a
  transparent background (set `plot_background_color` for an opaque one),
  sized to the page (A4 at 300 dpi = 3508 × 2480 px). DPI range 72–2400.
- SVG export: API `export_svg` works for both (board also with
  `fit_page_to_board=True`); `kicad-cli sch export svg` and
  `kicad-cli pcb export svg --layers F.Cu,B.Cu,Edge.Cuts --mode-single` work too
  (SVG carries mm `width`/`height` + `viewBox`).
- Pixel to mm: `x_mm = px * 25.4 / dpi`, `y_mm = py * 25.4 / dpi` — the PNG is
  the full page and both editors use page coordinates (origin = page top-left).
  Board: holds **only with `PlotSettings.scale = 1.0`**; the proto default `0`
  auto-scales the board to fill the page (ecc83-pp: 52 mm board plotted 177 mm
  wide). With scale 1 the Edge.Cuts content box is (121.16, 90.09)–(173.48, …) mm
  vs. the API bounding box (121.22, 90.11)–(173.42, 136.59) mm — off by half the
  edge line width (0.06 mm). Schematic: page-sized PNG; C1 at (86.36, 57.15) mm
  ↦ px (1020, 675). Boards larger than or placed outside the page are clipped by
  this formula's page; Phase 4 must crop to the board bbox, not assume it fits.
- Not needed: no rasterisation fallback (`kicad-cli … svg` + cairosvg) is
  required at this commit, but it remains available (`cli_sch_svg`/`cli_pcb_svg`
  PASS).

### ERC/DRC JSON (Q6)
- Commands (with `LANG=C`): `kicad-cli sch erc --format json --units mm -o erc.json <root.kicad_sch>`
  and `kicad-cli pcb drc --format json --units mm --schematic-parity -o drc.json <board.kicad_pcb>`.
  `--units mm` is accepted.
- ERC/DRC JSON units: `coordinate_units: "mm"`; positions are `{"x": mm, "y": mm}`.
- ERC key paths: top `$schema, coordinate_units, date, ignored_checks,
  included_severities, kicad_version, sheets, source`; `sheets[] = {path, uuid_path,
  violations[]}`; `violations[] = {type, severity, description, items[]}`;
  `items[] = {description, pos{x,y}, uuid}` (full 36-char UUID). Sample:
  `{"type": "footprint_link_issues", "severity": "warning", "description": "The current configuration does not include the footprint library 'Capacitor_THT'", "items": [{"description": "Symbol C1 [CP]", "pos": {"x": 86.36, "y": 57.15}, "uuid": "00000000-0000-0000-0000-00004549f4be"}]}`
- DRC key paths: top adds `violations[]`, `unconnected_items[]`,
  `schematic_parity[]` (same entry shape as ERC violations). Sample:
  `{"type": "silk_edge_clearance", "severity": "warning", "description": "Silkscreen clipped by board edge", "items": [{"description": "Segment on Edge.Cuts", "pos": {"x": 121.285, "y": 136.525}, "uuid": "9b19456e-3ad1-4dde-92c4-23451559e1ba"}, …]}`
  ecc83-pp: 2 violations, 0 unconnected, 10 parity entries.
- Descriptions are English/ASCII with `LANG=C`.
- Exit codes: `0` by default even with violations; with `--exit-code-violations`
  both return `5` when violations exist. Phase 4 should parse the JSON and treat
  only a missing/unparseable report as a tool failure.
- Library tables matter: with an empty KiCad config (no global
  `fp-lib-table`/`sym-lib-table`) ERC reports `footprint_link_issues` for every
  footprint from the stock libraries (`Capacitor_THT`, `Resistor_THT`) and DRC
  parity reports `footprint_symbol_mismatch`. Phase 2's image must install the
  stock symbol/footprint libraries **and** global library tables in the runtime
  user's KiCad config, or these findings will be noise in every report.
- Config isolation: kicad-cli writes `~/.config/kicad/10.99/*.json` on first
  run. The harness sets `KICAD_CONFIG_HOME=.kicad/config` (spike-local) so runs
  do not touch the user's config; Phase 3's launcher should set
  `KICAD_CONFIG_HOME` under `/data` explicitly.

### Lifecycle (Q7)
- Startup: 0.21 s, 0.21 s, 0.21 s (process start → socket answers `ping`), plus
  `open_document` 0.32–0.33 s (sch) and 0.35–0.36 s (pcb) for ecc83-pp. The
  `startup_under_15s` check passes with a wide margin; C4's
  `kicad_start_timeout_s = 60` is generous.
- Lock files: the headless server creates **only** `~PROJECT.kicad_pro.lck`
  (e.g. `~ecc83-pp.kicad_pro.lck`), no per-sheet/board lock; content is JSON
  `{"hostname": …, "token": …, "username": …}`. It is removed on SIGTERM and
  **left behind** after kill -9. A second api-server on the same project (lock
  present) **opens it anyway** — the lock is advisory and not enforced by the
  headless server. Consequences for Phase 3: `lock_status` must look at
  `~STEM.kicad_pro.lck` (as well as `~FILE.lck` for sch/pcb in case the GUI
  writes those), must be evaluated **before** CopperChat starts its own server
  (afterwards CopperChat's own lock is present), and should treat a lock whose
  `token`/host is CopperChat's own (or stale after a crash) as not-locked; on
  restart after kill -9 the stale lock is simply overwritten.
- Socket permissions: socket file `0o755` (owner-writable only), inside a
  `0o700` directory created by the harness → not world-writable. C4's
  `/tmp/copperchat/` directory at `0700` is what actually protects it.
- External change: no auto reload (`get_symbols()` still returns the old
  content 2 s after the file was replaced on disk). `sch.revert()` reloads from
  disk in 0.06 s (26 → 27 symbols after replacing the file); `board.revert()`
  works too. Phase 7's hash check should call `revert()` on the changed document.
- kill -9: detected immediately on the next call as
  `kipy.errors.ConnectionError: Failed to connect to KiCad: Connection refused`;
  stale socket file left behind (yes) and stale lock (yes). Restart on the
  **same socket path** works (the server replaces the stale socket): 0.96 s
  including both `open_document` calls.
- SIGSTOP (hang): with a client that is already connected, the call fails after
  exactly `timeout_ms` (2.00 s, `ConnectionError: Error receiving reply from KiCad:
  Timed out`) and the same client works again after SIGCONT. A **fresh** client's
  first call dials with `block_on_dial=True`, which `timeout_ms` does not bound:
  it failed only after 10.00 s, and after SIGCONT a new client still timed out.
  → C4 must enforce its own deadline (worker thread + `asyncio.wait_for`), keep
  one long-lived connected client per session, and **restart** the server on any
  timeout instead of retrying (matches C4's "restart, then raise
  `KicadUnavailable`").
- Parallel servers: two servers on different projects/sockets work side by side
  (26 vs 119 symbols).
- kipy also ships `KiCad(headless=True, kicad_cli_path=…, file_path=…)`
  (`kipy.server.KiCadServer`), which starts `kicad-cli api-server` itself; C4's
  own `ServerLauncher` remains preferable (explicit socket path, restart control).

## Decision

- Gate: **1C partial: SaveCopyOfDocument writes only the root sheet of a
  hierarchical schematic → workaround: `SaveDocument` (`sch.save()`) on the open
  schematic, then `SaveCopyOfDocument` for the root (`sch.save_as(dest, overwrite=True,
  include_project=False)`) plus a byte copy of the sub-sheet files KiCad has just
  saved (names from `sch.get_hierarchy()`); read-only projects copy the on-disk
  files.** Every other core operation passes on the API itself: GetItems on
  schematic (all sheets) and board, SaveDocument, SaveCopyOfDocument (root sheet,
  board), PNG export via API jobs for both document types, plus SVG, ERC/DRC JSON,
  revert, crash/hang detection and restart. No `kicad-cli` workaround is needed.
- Recommended KICAD_COMMIT: `0616634034fa1a8c92c141c293342cbef2f68dff`
  (the tested nightly `10.99.0-5265-g0616634034`).
- kipy revision for backend `[tool.uv.sources]` and the base image:
  `87b945a0d56b26a78bf547db9771ff9bf3a057ea`
  (`kicad-python = { git = "https://gitlab.com/kicad/code/kicad-python.git", rev = "87b945a0d56b26a78bf547db9771ff9bf3a057ea" }`).

## Follow-ups

- **Phase 2:** `docker/kicad-base/KICAD_COMMIT` =
  `0616634034fa1a8c92c141c293342cbef2f68dff`; install kipy at
  `87b945a0d56b26a78bf547db9771ff9bf3a057ea`. Building kipy from the uv git
  source clones the whole KiCad repo as a submodule (4.5 min, 3.5 GB cache);
  cheaper: clone kicad-python at the revision, `git submodule update --init
  --depth 1` (38 s), then build/install it, or point its `kicad` submodule at the
  KiCad tree the image already builds from. Install the stock symbol/footprint
  libraries **and** global `sym-lib-table`/`fp-lib-table` for the runtime user,
  else ERC/DRC report `footprint_link_issues`/`footprint_symbol_mismatch` for every
  stock footprint. Smoke test: `kicad-cli api-server --socket S PROJECT.kicad_pro`
  then `ping`, `open_document` (sch + pcb), `get_schematic().get_symbols()`.
- **Phase 3 (C4):**
  - Launch: `kicad-cli api-server --socket /tmp/copperchat/ID.sock PROJECT.kicad_pro`
    with `LANG=C`, `LC_ALL=C` and `KICAD_CONFIG_HOME` under `/data`; ready when the
    socket answers `ping` (~0.2 s); **then `open_document` for the `.kicad_sch` and
    `.kicad_pcb`** (preloading the project does not open them).
  - Accessors: `KicadHandle.schematic()` = `kicad.get_schematic()`;
    `KicadHandle.board()` = `kicad.get_board()`.
  - Health check: `ping()` on a long-lived, already-connected client;
    `timeout_ms` bounds calls only after the first successful dial, so `call()`
    keeps its own deadline (worker thread + `asyncio.wait_for`) and restarts on
    timeout (a SIGSTOP'd server is not reliably usable afterwards).
    kill -9 → immediate `kipy.errors.ConnectionError` (connection refused);
    restart on the same socket path ~1 s incl. opening both documents.
  - `lock_status`: KiCad's lock is `~STEM.kicad_pro.lck` (JSON with hostname,
    token, username); check it **before** starting CopperChat's own server (which
    creates the same file), ignore CopperChat's own/stale locks; the headless
    server does not enforce locks.
  - `save_copy(doc, dest)`: procedure from Q4 (root via API + byte copy of KiCad-saved
    sub-sheets; board via API).
- **Phase 4:** no GetItems gaps. `get_symbols()` returns one entry per placed unit
  per sheet instance (dedupe by reference for part counts); positions in nm →
  mm = nm / 1e6; ERC/DRC JSON paths and exit codes from Q6 (parse the JSON;
  exit code 0 by default). `API_PNG_EXPORT = True`; `PIXEL_MAPPING_KNOWN = True`
  with board `PlotSettings.scale = 1.0` (`x_mm = px * 25.4 / dpi`).
- **Phase 7:** `mode=kicad` uses the API PNG jobs (Q5); hash documents after
  CopperChat's own save (save normalises files); external change → `revert()`.
- **Phase 6B:** KiCanvas snapshots of hierarchical schematics need the Q4
  procedure (root copy + sub-sheet files); a missing sub-sheet is not detected by
  `kicad-cli` (it plots it empty).
