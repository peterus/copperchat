# Changelog

All notable changes to CopperChat are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Container images:
`ghcr.io/peterus/copperchat:<version>`.

## [Unreleased]

Sub-project 2: **schematic editing**. The agent changes schematics through KiCad's IPC API
only; boards stay read-only.

### Added

- **Editing tools.** `sch_place_symbol`, `sch_set_fields`, `sch_find_space`, `sch_annotate`,
  `sch_connect` (computed orthogonal routes with T-joins, label fallback), `sch_add_label`,
  `sch_add_power`, `sch_no_connect`, `sch_add_wire`, `sch_rename_net`, `sch_reroute`,
  `sch_move` (drags wires), `sch_delete` (with attached stubs, labels and power symbols),
  `sch_duplicate`, `sch_add_sheet`, `sch_add_sheet_pin`, `sch_get_pin`, `sch_changes`. Every
  mutating call is one verified change: prepared first (refusals change nothing and take no
  snapshot), written in KiCad commits, read back against KiCad's netlist and undone on a
  mismatch.
- **Library tools** `lib_search_symbols`, `lib_get_symbol`, `lib_search_footprints` (symbol
  libraries loaded eagerly, footprint libraries on demand), `parts_get_notes`, and layered
  **part preferences** (default: passives 0805; deviations are flagged).
- **Plans.** Larger changes are proposed as a plan card (Approve / Reject / Resume / Roll back /
  Abort); steps run in order, each ends with an ERC delta (new errors fixed or justified) and a
  checkpoint. Change budget per turn (default 20 logical changes, at most 3 deletions of
  existing parts) and plan policy in **Settings › Editing**.
- **Visual control.** Change log per turn and step, `kicad-cli sch diff` before/after layer,
  highlights of added/modified/removed items in the viewer (KiCanvas fork), a **Changes** panel,
  plan steps nested in the history.
- **Skill `kicad-schematic-editing`** (tool order, pin addressing, rails and labels, plans, ERC
  policy, self-check, missing parts); placement recipes in `kicad-schematic-style`.
- **Tests.** Golden scenario tests build three reference circuits (LED, LDO, MCU with an I2C
  sensor on a sub-sheet) from an empty project through the tools against real KiCad; undo and
  rollback tests; edited KiCad-saved fixtures load in KiCanvas; an opt-in live eval
  (`pytest -m live_sdk tests/live/test_editing_eval.py`, results in `backend/tests/live/results/`).

### Changed

- The agent's system prompt allows schematic edits (boards stay read-only).
- Database migration `0006_editing` (editing settings, change log, ERC results, plans).

## [0.1.1] - 2026-10-17

Smaller container images and a faster, branch-safe kicad-base CI build. No feature changes.

### Changed

- **Smaller images.** The `kicad-base` image shrinks from 2.97 GB to 1.95 GB and the app image
  from 3.76 GB to 2.54 GB (compressed `kicad-base`: 663 MB to 425 MB). Removed: debug symbols,
  docs and locales, Mesa/LLVM drivers (KiCad renders on the CPU headless), OpenCASCADE sample
  data and all KiCad demos except the test fixtures; `uv` is no longer shipped in the app image.
  STEP/GLB export, 3D render, DRC/ERC, exports, the api-server, kipy and `pdftoppm` are unchanged.
- **Recipe-pinned `kicad-base` tags.** Branches publish `<commit>-r<recipe>`; only `main`
  publishes `<commit>`, and it always builds what it publishes.
- **Shared registry ccache.** `main` keeps a `:ccache` image that branches seed from, so a
  warm `kicad-base` build takes minutes instead of about 2 hours.

### Fixed

- The `ldd` closure check covers `*.kiface` files, the kipy venv, Python extension modules and
  git, and fails if a path-exclude removes a linked library.
- Release image pushes pass the `kicad-base` build argument.
- The end-to-end smoke test no longer collides between concurrent runs.

## [0.1.0] - 2026-10-17

First public release: the **foundation** — chat with a read-only AI agent about your KiCad
projects, in the browser on desktop, iPad and iPhone.

### Added

- **Chat agent with read-only KiCad tools.** The agent inspects schematics and boards through
  KiCad 11's own API (`kicad-cli api-server` + kipy), runs ERC/DRC, renders annotated
  "agent views" and looks at them. It has no file, shell or web tools of its own. Message
  queue, Stop, resume after rate limits, usage display, image/PDF attachments.
- **Viewer.** Schematic and board viewer based on KiCanvas, patched for the KiCad 11 file
  format, touch gestures and host integration (sheet tree, layers, pinch zoom); tap a part to
  mention it in the chat (`@R12`).
- **History, undo and git.** Autosave after every turn, per-turn undo snapshots, detection of
  external changes, crash recovery; git status, diff summary and commit — only on request,
  never push or history rewriting.
- **Skills and part researcher.** Built-in, user and per-project skills; a separate, guarded
  part-researcher agent (`research_part`, public web only) that collects datasheets and
  pinout/application notes under `docs/parts/<MPN>/` in the project.
- **Uploads** of images and PDFs per project (the agent can read PDF pages).
- **Project picker** over the mounted projects folder (any depth); projects open in the
  KiCad GUI open read-only with a banner.
- **Installable app (PWA) and Web Push** when a run finishes, the agent needs you or a
  rate-limit window reopens.
- **Security.** App login always on (argon2, rate-limited), strict Content-Security-Policy,
  WebSocket origin check, push endpoints restricted to the browsers' push services.
- **Deployment.** Docker image `ghcr.io/peterus/copperchat` (KiCad 11 built from source,
  runs as uid 1000), `docker-compose.yml` with an optional Cloudflare Tunnel profile
  (`docker compose --profile tunnel up -d`), `/api/health` with app and KiCad version.

### Known limitations

- **No editing yet:** the agent cannot change schematics or boards (planned for the next
  sub-project, with plan-before-edit).
- One user per instance.
- No light theme for the board viewer.
- Web Push delivery is not tested in CI, and the app has not been tested on a real iPhone
  yet.
- The optional `cloudflared` service uses the unpinned `cloudflare/cloudflared:latest` image.
- The KiCanvas patches live in this repository (`frontend/vendor-patches/kicanvas/`), not in
  an upstream fork; the main JavaScript chunk is large (~590 kB).

[Unreleased]: https://github.com/peterus/copperchat/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/peterus/copperchat/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/peterus/copperchat/releases/tag/v0.1.0
