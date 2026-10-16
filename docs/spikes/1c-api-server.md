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

## Decision

## Follow-ups
