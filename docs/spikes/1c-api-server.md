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
- api-server syntax: `kicad-cli api-server --socket <socket path> <project.kicad_pro>` (full `--help` below)
- Socket URL: `ipc://<absolute socket path>` (e.g. `ipc:///tmp/c1-sock-5n2ycbwh/api.sock`);
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

<details><summary>kicad-cli api-server --help</summary>

```
Usage: kicad-cli api-server [--help] [--socket SOCKET_PATH] PROJECT_OR_FILE

Run the KiCad IPC API server in headless mode

Positional arguments:
  PROJECT_OR_FILE  Optional path to a .kicad_pro, .kicad_pcb, or .kicad_sch file to pre-load [nargs=0..1] [default: ""]

Optional arguments:
  -h, --help       Shows help message and exits 
  --socket         Override API socket path [nargs=0..1] [default: ""]
```
</details>

## Results

### Connecting (Q2)
- The headless server **only preloads the project** when given a `.kicad_pro`;
  `GetOpenDocuments` then fails with `no handler available for request of type
  kiapi.common.commands.GetOpenDocuments` (the schematic/board handlers are only
  registered once the editor face has a document). The client must call
  `kicad.open_document("<abs>.kicad_sch", DOCTYPE_SCHEMATIC)` and
  `kicad.open_document("<abs>.kicad_pcb", DOCTYPE_PCB)` right after connect
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
- Symbol instance `path` is the sheet instance path (`/<root uuid>/<sheet uuid>`),
  so the same symbol in two sheet instances is two entries with different paths.
- No GetItems call was missing, so no `kicad-cli` workaround is needed.

## Decision

## Follow-ups
