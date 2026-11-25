# CopperChat Board (SP4) Implementation Plan (index)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This file is the **index**. Each phase lives in its own file under
> `docs/superpowers/plans/board/` and is executed by one child session as **exactly one
> PR**. A phase executor reads, in this order: this index, the spec, `docs/spikes/4a-board.md`
> (prototypes in `spikes/4a-board/` — **port, never import**), `docker/kicad-base/patches/README.md`,
> then its own phase file. The footprints index (`2026-11-17-footprints-plan.md`, FP-C1…FP-C9),
> the editing index (`2026-10-18-editing-plan.md`, E-C1…E-C13) and the foundation index
> (`2026-10-15-foundation-plan.md`, C1…C8) stay binding for everything this plan does not change.

**Goal:** Let the in-chat agent take a schematic to an orderable JLCPCB board through a
step-by-step board session — netlist import, outline/rules/stackup, block placement, own
octilinear router + Freerouting fill, planes/fanout/stitching, quality metrics with hard gates,
two server-enforced approvals, fab package — with every write through the KiCad IPC API,
compensated on failure and undoable from the History panel.

**Architecture:** A pure-Python core `copperchat/board/` (frozen `BoardModel` snapshot,
classification, placer, router, post-processor, metrics, SVG wrapper, fab planning) that never
imports kipy, and a KiCad layer `copperchat/kicad_tools/board/` (model loader, raw-protobuf API
wrappers for spike gaps G3/G5, the `BoardChangeSet` engine with stage commits and KIID-diff
compensation, and the `pcb_*` ToolSpecs). CPU-heavy work runs in a one-process worker pool;
KiCad calls stay on the session worker thread. Freerouting 2.5.0 runs as a sandboxed JVM
subprocess from the app image. Two approval checkpoints (placement → routing, board → fab) are
hash-bound rows in a new migration `0013_board.sql`.

**Tech Stack:** Python 3.13, FastAPI, SQLite migrations, kipy fork `peterus/kicad-python`
`e0c9c8e44edc3e44db3c97582a29c8a446b832c3` (raw protobuf for DRC task / Specctra / document
jobs), KiCad `52b43fb8fc8e11fdab5d683eaefc91a49f9cbcd7` + patches 0001–0004 (+ 0005 in P2b),
`kicad-cli` (netlist, SVG, DRC parity, gerbers/drill/pos/bom, 3D), CairoSVG (existing raster
worker), Freerouting 2.5.0 JAR + Eclipse Temurin 25 JRE (app image only), React/TypeScript
(session dialog, change highlight). **No new runtime Python dependency; no new dev
dependency** (property tests use seeded `random.Random` loops, plan call 3).

**Spec:** `docs/superpowers/specs/2026-11-24-board-design.md` (approved by the owner
2026-11-25, PR #97; owner decisions §15.1, D1–D5 in §15.3; spec calls 1–67 in §15.2).
Spike: `docs/spikes/4a-board.md`.

---

## Phase overview

| # | Phase file | Goal (one line) | Depends on | Wave |
|---|---|---|---|---|
| P1a | `board/phase-p1a-core-write-path.md` | `BoardModel` + hashes + geometry, `kicad_tools/board/{read,api}.py` (G3/G5), `BoardChangeSet` engine (stages, read-back, KIID compensation, board creation), per-`DocKind` commit/modified, serialised CPU worker, JLC `FabPreset` data, history `.kicad_dru` + absent entries + restore re-discovery, `COPPERCHAT_BOARD_TOOLS`, `BOARD_KINDS`, DRC-in-open-commit probe | — | 1 |
| P1b | `board/phase-p1b-synth-fixture.md` | `sp4_synth` fixture (≈ 25 SMD parts, 2 sheets) generated through CopperChat's schematic engine + KiCad API, generator script, `refresh-fixtures.sh`, `synth_session` fixture | — | 1 |
| P1c | `board/phase-p1c-session-views.md` | Board session type behind the flag, `/api/features`, profiles (`PCB_READ`, `PCB_WRITES`, `PLAN_TOOLS`, `FP_WRITES`, `doc_patch_section`), prompt, `board.md` v2 + write policy, `pcb_status`, `pcb_render` (+ board SVG limits), live board view (G8), `session-board` skeleton | P1a | 2 |
| P2 | `board/phase-p2-sync-setup.md` | `pcb_sync_from_schematic` (create + reopen, staging area), `pcb_set_outline`, `pcb_set_rules` (no stackup; applies P1a's JLC presets), layer-type probe | P1b, P1c | 3 |
| P2b | `board/phase-p2b-stackup-patch.md` | KiCad patch 0005 `UpdateBoardStackup` handler (G1) + QA test, kicad-base rebuild, `pcb_set_rules.stackup` presets | P2 | 4 (any time after P2) |
| P3a | `board/phase-p3a-analysis-metrics.md` | `classify.py`, `boardmd.py` parser, bend geometry (one M4/M7 definition), `metrics.py` M1–M14, `pcb_analyze`, `pcb_metrics` | P1b, P1c | 3 |
| P3b | `board/phase-p3b-checks-checkpoints.md` | `pcb_check`, migration `0013_board.sql`, `board_reviews` evidence, `pcb_approve` + shared approval guards, checkpoint guards for later tools, `pcb_status` checkpoint fields | P3a, P2 | 4 |
| P4 | `board/phase-p4-placement.md` | placer (cluster/layout/legalise/score/pack), `pcb_define_blocks`, `pcb_place_block`, `pcb_move`, `pcb_legalize`, routed-part refusal + `unroute: true` | P2, P3b | 5 |
| P5a | `board/phase-p5a-router-core.md` | pure octilinear router: obstacle grid, A* with 0°/45° turns only, vias, pad entry, simplify, coupled pairs, property tests (M4 = M7 = 0) | P3a | 4 |
| P5b | `board/phase-p5b-route-tools.md` | `pcb_route` (DRC-verified per connection, locked), `pcb_unroute` (incl. `unlock_only`), router ↔ KiCad wiring, placement checkpoint enforcement | P4, P5a | 6 |
| P6a | `board/phase-p6a-freerouting-runtime.md` | JRE 25 + Freerouting JAR in the app image + CI (one checksum-verified fetch script), sandboxed runner, `COPPERCHAT_FREEROUTING`, pure post-processor `router/chamfer.py` | P3a | 4 |
| P6b | `board/phase-p6b-route-fill.md` | `pcb_route_fill`: DSN/SES round trip, full-copper snapshot + compensation, GND deletion scoped to new KIIDs, post-processing + rip-and-reroute, ladder (passes, `relock_nets`), 20 % rejection rule | P5b, P6a | 7 |
| P7 | `board/phase-p7-copper.md` | `pcb_zones`, `pcb_fanout`, `pcb_stitch` (locked, DRC-legalised), `pcb_tidy_silk` | P5b | 7 |
| P8 | `board/phase-p8-fab.md` | `pcb_fab_export` (board checkpoint, atomic two-step swap), `pcb_export_3d`, shared safe output path helper, fab report | P3b, P7 | 8 |
| P9a | `board/phase-p9a-golden-odh.md` | end-to-end golden on `sp4_synth` (`kicad_e2e` marker + own CI job), ODH dry-run script + report, memory measurement | P6b, P7, P8 | 9 |
| P9b | `board/phase-p9b-skills-release.md` | `session-board` full flow, `board-layout` skill, `copperchat-tools`, prompt, live agent eval, README/CHANGELOG/architecture, **flag default on** | P9a | 10 |
| P10 | `board/phase-p10-3d-models.md` | optional (owner OK): on-demand 3D model cache, STEP with parts | P8 | — |

**Why the spec's phases were split** (plan call 1): the spec's P1 alone touches the session,
history, the adapter, profiles, prompt, design docs, views, the live view, the frontend and a
fixture — far more than a reviewer can clear in three rounds, so it is cut into the write-path
core (P1a), the fixture (P1b, parallel) and the session/view surface (P1c). P3 is split into
pure analysis/metrics (P3a) and the DB-backed checks/approvals (P3b) because the guards need
their own review. P5 and P6 are split into a pure algorithm phase (P5a, P6a: unit/property
tests, infra) and the KiCad tool phase (P5b, P6b: DRC loop, compensation), which also lets the
pure phases start early (wave 4). P9 is split so the flag only turns on (P9b) after the golden
and the ODH dry run (P9a) are merged. Every split phase is useful on its own and leaves `main`
releasable (all write tools stay behind `COPPERCHAT_BOARD_TOOLS=off` until P9b).

### Execution waves (a phase starts once every phase it depends on is merged)

| Wave | Phases | Note |
|---|---|---|
| 1 | P1a, P1b | disjoint files (table below) |
| 2 | P1c | |
| 3 | P2, P3a | shared files only append-only (below) |
| 4 | P3b, P5a, P6a, P2b | P2b additionally needs the owner's go (see "Provisional"); P5a/P6a are pure/infra |
| 5 | P4 | |
| 6 | P5b | |
| 7 | P6b, P7 | P6b's "locked fanout survives a re-fill" test creates locked vias itself (no P7 import) |
| 8 | P8 | |
| 9 | P9a | |
| 10 | P9b | flag default on |
| — | P10 | only with owner OK |

### File ownership while phases run in parallel

`backend/src/copperchat/` is abbreviated `cc/`. A phase owns the files listed for it; "append"
means the file is shared and the phase only adds lines (rebase on `main` and keep both sides).

| Area | Owner (wave) | Others |
|---|---|---|
| `cc/board/{__init__,model,geom,worker,presets,_selftest}.py` (`presets.py`: `FabPreset` + `PRESETS`, pure data), `cc/kicad_tools/board/{__init__,read,api,engine,compensate}.py` (engine incl. `BoardStage.compensable` / `undo`), `cc/kicad_session/session.py`, `cc/history/{store,restore,service}.py`, `cc/changes/{repo,diff}.py`, `frontend/src/components/changes/highlight.ts`, `cc/config.py` (`board_tools`), `cc/tools/spec.py` + `cc/agent/{engine,sdk_backend,fake}.py` + `cc/history/hooks.py` (`ToolContext.reload_project`, `refresh_project`, `on_project_changed` only), `cc/agent/sdk_tools.py` (`_two_phase_steps` generation re-baseline after a session replacement only), `cc/kicad_tools/edit/engine.py` (refusal) | P1a (1) | P1b none |
| `backend/tests/fixtures/projects/sp4_synth/**`, `scripts/make-sp4-synth.py`, `scripts/refresh-fixtures.sh`, `backend/tests/support/kicad_project.py` (`synth_session`), `backend/tests/kicad/board/goldens/sp4_synth_summary.json` | P1b (1) | P1a none |
| `cc/agent/{profiles,sdk_backend,fake}.py`, `cc/agent/sdk_tools.py` (board publish), `cc/history/{live,hooks}.py`, `cc/views/{snapshot,footprint_render,_raster_worker}.py` (`_raster_worker`: CPU/memory limits from argv), new `cc/views/board_render.py`, `cc/board/render.py`, `cc/web/routes_conversations.py`, `frontend/src/components/sessions/NewSessionDialog.tsx`, `cc/design_docs/service.py`, `cc/skills/builtin/design-docs/templates/board.md`, new `cc/skills/builtin/session-board/`, `cc/kicad_tools/board/{specs,status,views}.py`, `cc/tools/registry.py`, `cc/web/app.py` (`/api/features` registration), new `frontend/src/api/features.ts`, `cc/history/service.py` (pcb branch of the turn-end view refresh) | P1c (2) | — |
| `cc/kicad_tools/board/{sync,setup}.py`, `cc/kicad_tools/board/specs_setup.py`, `cc/board/staging.py` (consumes P1a's `presets.py`) | P2 (3) | `board/specs.py` append |
| `cc/board/{classify,boardmd,metrics}.py`, `cc/board/jobs.py` (the one module of `run_cpu` job functions: `analyse_job`, `metrics_job`), `cc/board/geom.py` (bend analysis, append), `cc/kicad_tools/board/{analysis,specs_analysis}.py` | P3a (3) | `board/specs.py` append; `cc/board/jobs.py` append (P4 `place_block_job` / `legalize_job`, P5b/P6b if needed) |
| `cc/db/migrations/0013_board.sql`, `cc/db/repos/board.py`, new `cc/approvals.py`, `cc/kicad_tools/footprints/approve.py` (refactor to `cc/approvals.py`), `cc/editing/gate.py` (`PLAN_EXEMPT_TOOLS`), `cc/kicad_tools/board/{check,approve,checkpoints,specs_check}.py`, `board/status.py` + `board/views.py` (evidence/checkpoint fields) | P3b (4) | `board/specs.py` append |
| `cc/board/router/{__init__,grid,astar,simplify,pairs}.py` — **only P5a creates `cc/board/router/__init__.py`** | P5a (4) | P6a must not create or edit `__init__.py` (it adds `chamfer.py`, imported by full path); if P5a is not merged yet, P6a creates an empty `__init__.py` only if absent and keeps P5a's version on rebase |
| `scripts/fetch-freerouting.sh`, `docker/freerouting.lock`, `docker/app/Dockerfile`, `.github/workflows/ci.yml` (kicad job cache + fetch step), `cc/board/freerouting.py`, `cc/board/router/chamfer.py`, `cc/config.py` (freerouting settings, append), `LICENSES/` notices, `scripts/app-image-tools.sh`, `.gitignore` | P6a (4) | — |
| `docker/kicad-base/patches/0005-*.patch`, `docker/kicad-base/patches/README.md`, `cc/kicad_tools/board/setup.py` (stackup branch), `cc/kicad_tools/board/specs_setup.py` (stackup enum), `cc/board/presets.py` (append `StackupPreset`, `STACKUPS`), `docs/kicad-patches/0005-board-stackup-FACTS.md` | P2b (4) | runs after P2 merged |
| `cc/board/placer/**`, `cc/kicad_tools/board/{place,unroute_ops,specs_place}.py` | P4 (5) | — |
| `cc/kicad_tools/board/{route,specs_route}.py` | P5b (6) | — |
| `cc/kicad_tools/board/{fill,specs_fill}.py`, `cc/agent/profiles.py` + `cc/agent/{sdk_backend,fake}.py` (freerouting filter + call sites) | P6b (7) | — |
| `cc/board/{planes,silk}.py`, `cc/kicad_tools/board/{zones,fanout,stitch,silk,specs_copper}.py` | P7 (7) | — |
| `cc/board/{fab,paths}.py`, `cc/kicad_tools/board/{fab,specs_fab}.py` | P8 (8) | — |
| `backend/tests/kicad_e2e/**`, `backend/pyproject.toml` (marker), `backend/tests/support/sharding.py` / `tests/unit/test_sharding.py`, `ci.yml` (`kicad-e2e` job), `scripts/odh-dry-run.py`, `scripts/update-goldens.sh` | P9a (9) | — |
| `cc/skills/builtin/{session-board,board-layout,copperchat-tools}/`, `cc/agent/sdk_backend.py` (prompt), `cc/config.py` (default on), README, `docs/architecture.md`, `.env.example`, `backend/tests/live/test_board_eval.py` | P9b (10) | — |
| **Shared, append-only (all phases):** `CHANGELOG.md` (`[Unreleased]` lines), this index's "As built" section (one `### As built (P#)` per phase), `backend/tests/kicad/board/conftest.py` (P1a creates it with `board_ctx`; P3a `synth_board`, P3b `synth_setup`, P4 `place_synth`, **P5b the shared rigs `placed_synth` / `routed_synth`** that P6b, P7, P8 build on), `backend/tests/unit/board/conftest.py` | every phase | add lines only; on rebase keep both sides; never rewrite another phase's helper |

P3a's kicad tests need a board on `sp4_synth` before P2 exists: they use a test-only
`synth_board` helper in `backend/tests/kicad/board/conftest.py` (kicad-cli netlist →
`create_board_document` → `import_netlist`), which later phases may reuse.
Every phase may add new files under `backend/tests/unit/board/` and `backend/tests/kicad/board/`
with phase-specific names (`test_<module>.py`); conftest additions in those two directories are
append-only (row "Shared, append-only" above). Kicad rigs from P5b on use **one** setup
sequence — P5b's `placed_synth` / `routed_synth`: sync → outline → `pcb_set_rules {layers: 4,
preset: "jlcpcb-4l", …}` → blocks/place → legalize → render placement + `pcb_check quick` →
placement approved through `pcb_approve` → `pcb_route` critical. **Each phase updates this
index in its own PR**: section "As built (P#)" and any contract deviation in place.

---

## Global Constraints

Every task implicitly includes these (plus the footprints, editing and foundation Global
Constraints). Values are copied from the spec.

- **Write path = KiCad IPC API only.** CopperChat never writes `.kicad_pcb`, `.kicad_pro`,
  `.kicad_dru`, footprint or library files, never generates S-expressions, never calls
  `ParseAndCreateItemsFromString`. `kicad-cli` only exports (netlist, DSN, SVG, DRC report, 3D,
  fab files). The `.ses` enters the board only via `ImportSpecctraSession`. Exported DSN text may
  be edited **only as a last-resort fallback** (owner D4) after the layer type and Freerouting
  CLI options. File operations on KiCad data: history snapshot/restore only (incl. deleting the
  `.kicad_pcb` / `.kicad_dru` recorded **absent** in a manifest, §6.4), plus `remove_created`
  (P1a): a failed tool call deletes a `.kicad_pcb` / `.kicad_dru` that **the same call** created,
  with restore's checks (`lstat` of the **unresolved** path inside the project dir: only a
  regular file is unlinked — through the unresolved path; symlinks and non-regular files are
  skipped and logged, never followed).
- **Fixtures are KiCad-generated** (KiCad demos, API/engine-created projects, KiCad-saved
  files). Never hand-edited, also not in tests. Goldens are CopperChat JSON output.
- **Nothing on KiCad's GitLab, mailing list or Discord, nor on Freerouting's GitHub.** KiCad AI
  policy (C14): patches carry the trailer `Assisted-by: Claude` (not `Co-Authored-By`) and the
  placeholder subject `TODO-owner-write-message …`; every human-readable upstream text is
  written by the owner.
- **Never request GitHub Copilot reviews.** Review = `kontor-recipes:review-loop`.
- **No design-specific code**: no reference designator, MPN or OpenDriveHub net name in tools,
  metrics, skills or non-fixture tests (tests on `sp4_synth` may name its refs).
- Pinned KiCad `52b43fb8fc8e11fdab5d683eaefc91a49f9cbcd7` + patches 0001–0004 (0005 from P2b);
  kipy `e0c9c8e44edc3e44db3c97582a29c8a446b832c3` — no kipy fork change (spec call 5).
- **Units mm, degrees, board coordinates (x right, y down).** References `maxLength 32`, net
  names `maxLength 256`; every list input has an explicit `maxItems` (spec §7 values: refs ≤ 200,
  nets ≤ 50, `highlight_nets` ≤ 20, `items` ≤ 200); every number has finite bounds
  (coordinates |v| ≤ 1000 mm, sizes/widths 0 < v ≤ 100 mm, rotations finite) — plan call 6.
- Schemas: `_obj` with `additionalProperties: false`; errors `StructuredToolError(what, why,
  suggestion, code=…, details=…)` (`cc/errors.py:46`); results `ToolResult(data, images,
  changed_docs={"pcb"}, text)`.
- **Tool error = nothing written** (§6.3): single-commit tools drop their commit; multi-stage
  tools compensate by KIID diff (full-data snapshot for `pcb_route_fill`); compensation runs
  shielded (`_uncancellable`, `cc/kicad_session/session.py:145`); a failed compensation raises
  `DocumentInconsistent` (`cc/errors.py:96`). The only exception: a failed **real** netlist
  import after a clean dry run → "undo the turn from the History panel".
- **Owner hard thresholds (D2) — change only with the owner's OK:** M4 any segment off
  0/45/90/135/… by more than ±0.5°, any 90° or acute direction change, any off-pad 90°
  T-junction → fail; M5 a signal connection with detour > 2.0 **and** > 5 mm extra → fail; M7
  any micro-jog or too-short chamfer → fail; M8 decoupling path > 5 mm or other side → fail;
  M1, M2 > 0, M3 errors / unaccepted warnings > 0, M12 width violation, M13 violation, M14
  JLCPCB preset violation without a recorded custom-rule exception → fail.
- Chamfer/jog geometry (spec §12 notes, the only definitions): chamfer = same-sense 45° pair
  with straight piece ≥ `max(1.0 × width, 0.25 mm)`; too-short chamfer = same-sense pair below
  that; micro-jog = opposite-sense pair closer than `2 × width`. One implementation
  (`cc/board/geom.py`, B-C5) used by metrics, router and post-processor.
- Budget (`EditingSettings.change_budget`, default 20): `pcb_sync_from_schematic` 3,
  `pcb_set_outline` 2, `pcb_set_rules` 2, `pcb_define_blocks` 1, `pcb_place_block` 1,
  `pcb_move` `ceil(n/10)`, `pcb_legalize` 1, `pcb_route` `ceil(n/5)`, `pcb_route_fill` 5,
  `pcb_unroute` 1, `pcb_zones` 1 per zone, `pcb_fanout` 2, `pcb_stitch` 2, `pcb_tidy_silk` 1,
  `pcb_approve` 1, `pcb_fab_export` 0, `pcb_export_3d` 0; read tools 0.
- Flags: `COPPERCHAT_BOARD_TOOLS` (`Settings.board_tools`, **default off until P9b**) removes the
  explicit `PCB_WRITES` name set from every profile incl. Free and hides the Board session type
  (`POST type=board` → 400 `session_type_unavailable`); `PCB_READ` and the old read tools stay.
  `COPPERCHAT_FREEROUTING` (`Settings.freerouting`, default on) removes only `pcb_route_fill`;
  a missing JRE/JAR makes `pcb_route_fill` refuse `freerouting_unavailable` and its tests skip
  with a reason **locally only — never in CI**.
- Freerouting: 2.5.0 JAR + Temurin 25 JRE (linux x64) in the **app image** only, both pinned by
  SHA-256 in `docker/freerouting.lock`, fetched by one script for Dockerfile and CI;
  `--gui.enabled=false`, `-Xmx1g -XX:MaxMetaspaceSize=256m -XX:ActiveProcessorCount=2`;
  limits set by util-linux `prlimit` before `exec` (no `preexec_fn`): `RLIMIT_CPU` = 2 ×
  timeout + 30 s (two active processors for the whole timeout + margin — the spec's "timeout +
  margin"), `RLIMIT_NOFILE` 256, `RLIMIT_FSIZE` 512 MiB, **no `RLIMIT_AS`**; SIGXCPU/SIGKILL
  not sent by us count as `timed_out`; timeout default 300 s, max 600 s; no network
  (`unshare -n` when permitted, else options + a no-connection test); killed on turn stop.
  `pcb_route_fill`: Freerouting gets `timeout_s`; post-processing + rip-and-reroute get a
  separate 120 s deadline. `pcb_route`: total deadline 300 s.
- Renders: PNG ≤ 1600 px long side; ≤ 3 images per step report; SVG through the footprint
  sanitiser with board limits (B-C9); every injected string XML-escaped.
- Fab: `<project>/fab/jlcpcb/` and `<project>/fab/3d/`, refused if `fab/` or the target is a
  symlink or resolves outside the project; written to a temp dir and swapped in two steps.
- Live view: board publishes **once per tool call from the adapter after `apply`**, never from
  the per-commit hook while a board tool is applying.
- Long commands always with `timeout`; KiCad tests in `ghcr.io/peterus/copperchat-kicad-base`
  as in the footprints plan's Phase Conventions.

## Review Focus

Failure modes most likely to bite a user although no happy-path test hits them. Each names the
phase/task that pins it.

1. **A re-fill that silently destroys earlier work** — `ImportSpecctraSession` deletes or
   re-creates unlocked items and may move footprints; ground deletion then removes fanout
   stubs, or a failed fill leaves half the copper. Expected: locked fanout/stitch/critical
   items keep KIIDs and positions, `placement_hash` unchanged, a failed or cancelled fill
   restores the full pre-fill copper. Tests: P6b Task 3 (`test_refill_keeps_locked_vias`,
   `test_fill_keeps_placement_hash`, `test_fill_failure_restores_full_copper`,
   `test_fill_cancel_compensates`).
2. **An approval that survives a change** — outline edited, a part locked/unlocked, silk tidied,
   a custom rule added, an undo back to an older approved state, a move away and back after
   the evidence (ABA), or a move during the fab export. Expected: the checkpoint is stale or
   the approval refused, `pcb_route` / `pcb_fab_export` refuse. Tests: P1a Task 1 (hash
   coverage), P3b Task 3 (`test_older_approval_not_revived`, `test_dru_change_invalidates_board`,
   `test_aba_in_approving_turn_refused`), P8 Task 3 (`test_concurrent_move_blocks_or_refuses`).
3. **A 90° corner that slips through** — an off-pad T-junction, a chamfer of 0.1 mm on a 0.2 mm
   track, a corner hidden by a pad-entry exemption, Freerouting's non-octilinear tail.
   Expected: M4/M7 fail, the post-processor rips and re-routes. Tests: P3a Task 2
   (`test_t_junction_90_fails`, `test_short_chamfer_fails`, `test_pad_entry_only_exempt_at_pad`),
   P6a Task 4, P6b Task 4.
4. **Undo of a board the tool created** — the History restore must delete the created
   `.kicad_pcb` (and a first `.kicad_dru`) and reopen with a re-discovered project.
   Expected: session starts, `docs_of == {"sch"}`, no "could not open" error. Tests: P1a
   Task 5 (unit), P2 Task 1 (`test_sync_create_then_undo`).
5. **A forged placement/board approval** — approval in the same turn, from a server-generated
   message, with a quote the user never wrote, with the other stage's hash, after a board
   write that followed the evidence, or while a plan runs. Expected: `approval_not_allowed`
   with `details.guard`. Tests: P3b Task 3.
6. **A runaway job** — a huge routing window, a CPU-bound JVM, a raster bomb or two jobs at
   once. Expected: router windows over 4,000,000 cells/layer fail `too_large`, the CPU worker
   is serialised and capped by `RLIMIT_AS`, the JVM by `prlimit`, the raster worker by CPU and
   memory limits. Tests: P1a Task 6 (`test_concurrent_calls_serialised`), P5a Task 1/5
   (`test_window_over_cap_raises`, `test_large_board_budget`), P6a Task 3
   (`test_limits_applied`, `test_cpu_limit_maps_to_timed_out`), P1c Task 5
   (`test_rasterise_passes_cpu_and_memory`).

---

## Interface Contracts

Names and types every phase relies on. Signatures are binding; a phase that has to deviate
updates this section in its own PR and adds an "As built" bullet.

### B-C1. Configuration and flags (P1a, P6a)

```python
# cc/config.py — Settings
board_tools: bool = Field(default=False, validation_alias="COPPERCHAT_BOARD_TOOLS")      # P1a; P9b → True
freerouting: bool = Field(default=True, validation_alias="COPPERCHAT_FREEROUTING")        # P6a
freerouting_home: Path = Field(default=Path("/opt/freerouting"),
                               validation_alias="COPPERCHAT_FREEROUTING_HOME")            # P6a: <home>/jre/bin/java, <home>/freerouting-2.5.0.jar
```

`cc/agent/profiles.py` (P1c): `profile_specs(conversation_type, specs, *, footprint_tools=True,
board_tools=False, freerouting=True)`; callers `cc/agent/sdk_backend.py` and `cc/agent/fake.py`
pass the settings. P1c adds `board_tools` (filter: drop `s.name in PCB_WRITES` when
`not board_tools`); **P6b** adds the `freerouting` argument at both call sites (drop
`pcb_route_fill` when `not freerouting`), because `Settings.freerouting` exists only from P6a.

### B-C2. Board model and hashes — `cc/board/model.py` (P1a)

Pure, frozen dataclasses, mm floats, no kipy import.

```python
Point = tuple[float, float]
Side = Literal["top", "bottom"]
@dataclass(frozen=True) class Rect:     x: float; y: float; w: float; h: float     # used by tool regions, renders, placer
@dataclass(frozen=True) class Pad:      ref: str; number: str; net: str; x: float; y: float
                                         w: float; h: float; rot: float; shape: str
                                         layers: frozenset[str]   # incl. F/B.Paste and F/B.Mask (M14 "paste on all SMD pads")
                                         smd: bool; drill: float | None
@dataclass(frozen=True) class Part:     ref: str; kiid: str; lib_id: str; value: str
                                         x: float; y: float; rot: float; side: Side; locked: bool
                                         courtyard: tuple[Point, ...]   # board coords, () if none
                                         pads: tuple[Pad, ...]; sheet: str; symbol_path: str
                                         texts: tuple[TextItem, ...]; group: str | None
                                         rule_areas: tuple[tuple[Point, ...], ...] = ()   # footprint-embedded keepouts (antenna detection)
@dataclass(frozen=True) class Track:    kiid: str; net: str; layer: str; start: Point; end: Point
                                         width: float; locked: bool
@dataclass(frozen=True) class ArcTrack: kiid: str; net: str; layer: str; start: Point; mid: Point
                                         end: Point; width: float; locked: bool
@dataclass(frozen=True) class Via:      kiid: str; net: str; at: Point; diameter: float
                                         drill: float; locked: bool
@dataclass(frozen=True) class Zone:     kiid: str; name: str; net: str; layers: tuple[str, ...]
                                         outline: tuple[Point, ...]; priority: int
                                         rule_area: bool; keepout: frozenset[str]   # {"tracks","vias","copper","footprints"}
                                         settings: tuple[tuple[str, str], ...]      # clearance, thermal, fill mode
                                         filled_area_mm2: float
                                         filled: tuple[tuple[Point, ...], ...] = ()   # filled polygons (M11 plane voids); excluded from board_hash
@dataclass(frozen=True) class Outline:  kind: Literal["rect", "other", "none"]
                                         x: float; y: float; w: float; h: float; corner_radius: float
                                         polygon: tuple[Point, ...]
@dataclass(frozen=True) class NetClassInfo: name: str; track_width: float; clearance: float
                                         via_diameter: float; via_drill: float
                                         diff_pair_width: float | None; diff_pair_gap: float | None
                                         patterns: tuple[str, ...]
@dataclass(frozen=True) class BoardModel:
    parts: tuple[Part, ...]; tracks: tuple[Track, ...]; arcs: tuple[ArcTrack, ...]
    vias: tuple[Via, ...]; zones: tuple[Zone, ...]; graphics: tuple[GraphicItem, ...]
    outline: Outline; copper_layers: tuple[str, ...]; layer_types: tuple[tuple[str, str], ...]
    rules: tuple[tuple[str, float], ...]; net_classes: tuple[NetClassInfo, ...]
    net_class_of: tuple[tuple[str, str], ...]   # (net, class)
    groups: tuple[tuple[str, tuple[str, ...]], ...]  # (group name, refs)
    dru_text: str; stackup: tuple[tuple[str, str], ...]
    def part(self, ref: str) -> Part: ...           # KeyError if missing
    def placed(self, part: Part) -> bool: ...       # courtyard fully inside outline; False if outline.kind == "none"

def placement_hash(m: BoardModel) -> str   # sha256 hex of canonical JSON: sorted (ref, round(x,2), round(y,2), round(rot % 360,1), side, locked) + outline (rounded 0.01) + rule areas (name, layers, keepout, outline rounded 0.01)
def board_hash(m: BoardModel) -> str       # sha256 hex: parts incl. lib_id/value/texts, tracks, arcs, vias (rounded 0.001, widths, locked), zones minus fill (outline, net, layers, priority, settings), graphics, rules, net classes + assignments, dru_text (newlines normalised), copper_layers, layer_types, stackup. KIID-independent.
```

`TextItem(kind, text, layer, at, size, thickness, visible)` and `GraphicItem(layer, kind, points,
width)` are pure too. `cc/board/geom.py` (P1a): `octi_dist(a, b) -> float`,
`polygon_area(pts)`, `point_in_polygon(p, pts)`, `polygon_inside(inner, outer) -> bool`,
`polygons_overlap(a, b) -> bool` (SAT for convex, edge test otherwise), `convex_hull(pts)`,
`rect_poly(x, y, w, h, r=0.0)`; P3a appends B-C5.

### B-C3. KiCad layer — `cc/kicad_tools/board/{read,api}.py` (P1a)

```python
# read.py
def load_board_model(h: KicadHandle, project: Project) -> BoardModel   # sync; run via session.call; one API read; dru_text read from <project>.kicad_dru if present
async def board_model(ctx: ToolContext) -> BoardModel                   # session.call(lambda h: load_board_model(h, ctx.project))
def require_board(ctx: ToolContext) -> None                             # raises StructuredToolError(code="no_board") when "pcb" not open

# api.py — raw protobuf for G3/G5, each sync and called inside session.call
@dataclass(frozen=True) class DrcMarker: type: str; severity: Literal["error","warning","exclusion"]
                                         text: str; kiids: tuple[str, ...]; pos: Point | None
@dataclass(frozen=True) class DrcResult: markers: tuple[DrcMarker, ...]; unconnected: int; seconds: float
                                         # unconnected = number of unconnected-items markers (DrcTaskResult has no count at the pin)
def run_drc_task(h: KicadHandle, *, timeout_s: float = 60.0) -> DrcResult    # StartDrcTask → GetTaskStatus → GetDrcTaskResult
def export_specctra_dsn(h: KicadHandle, dest: Path) -> None                  # RunBoardJobExportSpecctraDsn (live board)
def import_specctra_session(h: KicadHandle, ses: Path) -> None               # ImportSpecctraSession
def save_document_to_string(h: KicadHandle) -> str                           # SaveDocumentToString
def create_board_document(h: KicadHandle, path: Path) -> None                # CreateDocument(DOCTYPE_PCB) + save; caller reopens
def board_project(h: KicadHandle) -> Project_kipy                            # Board.get_project() with trailing "/" fix (G3)
def set_group_name(group_proto: Any, name: str) -> None                      # sets proto.name (kipy Group.name has no setter)
```

### B-C4. Write path — `cc/kicad_tools/board/engine.py` (P1a)

```python
BOARD_SHEET = SheetRef(path="", page="", file="", name="<board>")
@dataclass(frozen=True) class BoardStage:
    label: str
    run: Callable[[BoardStageContext], Awaitable[None]]
    commit: bool = True          # wrap in session.commit(f"copperchat: {tool}", doc="pcb")
    compensable: bool = True     # False: not revertible item by item (netlist import) → sync_failed + "undo via History"
    undo: Callable[[BoardStageContext], Awaitable[None]] | None = None   # non-item state (rules, net classes, layer count, stackup): restores the pre-call values
@dataclass class BoardStageContext:
    ctx: ToolContext; report: dict[str, Any]; created: set[str]   # KIIDs created by this call
    async def call(self, fn: Callable[[KicadHandle], T]) -> T: ...  # session.call
    async def drc(self) -> DrcResult: ...                           # api.run_drc_task
    async def model(self) -> BoardModel: ...
@dataclass(kw_only=True) class BoardChangeSet(ChangeSet):          # base fields defaulted: sheet=BOARD_SHEET, stages=[], pre_images={}, intent=NetIntent()
    kind: str                         # change-log kind, member of BOARD_KINDS
    board_stages: list[BoardStage]
    verify: Callable[[BoardModel, dict[str, Any]], list[str]] | None = None   # read-back; non-empty → compensate + raise
    snapshot: Literal["ids", "full"] = "ids"
    creates_board: bool = False       # P2 sync on a boardless project
    saves_project: bool = False       # rules/net classes: explicit project save if a board save does not persist them (P2 verifies)
    unrouted_by_call: list[str] = field(default_factory=list)  # nets this call unrouted (moves with unroute: true)
    fail_after: int | None = None     # test hook: raise after stage n (1-based)
async def apply_board_change_set(ctx: ToolContext, cs: BoardChangeSet) -> ToolResult
# runs inside ctx.session.applying(); takes the compensation snapshot first; one commit per
# stage with commit=True (commit=False stages open their own commits); drop_commit on error
# inside a stage; on any error after the first stage STARTED → compensate() (+ stage undo); verify after the last stage; result changed_docs={"pcb"}, data =
# cs.report + {"step_report": ...}; records one change-log entry (kind, sheet_path="", counts).
```

`cc/kicad_tools/edit/engine.py::apply_change_set` refuses `BoardChangeSet` with `TypeError` (as it
refuses `FootprintChangeSet`). Two-phase board tools use `edit_spec(..., prepare, apply)` where
`apply = lambda ctx, cs: apply_board_change_set(ctx, cs)`; `sdk_tools._apply` gets no new branch.

`cc/kicad_tools/board/compensate.py` (P1a):

```python
@dataclass(frozen=True) class CopperSnapshot:          # "ids": kiids only; "full": + item data
    track_ids: frozenset[str]; via_ids: frozenset[str]; zone_ids: frozenset[str]
    footprint_ids: frozenset[str]; group_ids: frozenset[str]; graphic_ids: frozenset[str]
    positions: tuple[tuple[str, float, float, float, Side, bool], ...]   # (kiid, x, y, rot, side, locked)
    tracks: tuple[Track | ArcTrack, ...] = (); vias: tuple[Via, ...] = ()
def take_snapshot(h: KicadHandle, mode: Literal["ids", "full"]) -> CopperSnapshot
def compensate(h: KicadHandle, snap: CopperSnapshot) -> list[str]   # removes items not in snap, restores positions; "full" also recreates missing tracks/vias (data equal); returns leftovers (empty = clean)
```

`cc/kicad_session/session.py` (P1a): `commit(self, label, *, doc: DocKind = "sch", dry_run=False)`;
`self.modified_docs: set[DocKind]` (pushed commit adds `doc`, save of `doc` removes it); the old
`modified` becomes a property `"sch" in self.modified_docs`.

`cc/board/worker.py` (P1a): `async def run_cpu(fn: Callable[..., T], *args: Any, timeout_s: float,
**kwargs: Any) -> T` (keyword arguments pass through to `fn`) — `ProcessPoolExecutor(max_workers=1,
mp_context=spawn, initializer=…)` per server process, created lazily; the initializer sets
`RLIMIT_AS` (3 GiB, as `cc/views/_raster_worker.py` does; measured peak recorded); jobs are
**serialised** by an `asyncio.Lock` and `timeout_s` starts when the caller's own job starts
(never while queued); on timeout the worker is killed and replaced, raising
`StructuredToolError(code="board_compute_timeout")`; a cancel kills the worker only while the
caller's own job runs; `fn` must be a module-level function of `cc/board/**` (job functions
live in `cc/board/jobs.py`, P3a, or are B-C12 entry points) with picklable (frozen dataclass)
arguments.

### B-C5. Bend geometry — `cc/board/geom.py` (P3a, append)

```python
@dataclass(frozen=True) class BendReport:
    non_octilinear: tuple[Point, ...]   # segment midpoints off 0/45/90/… by > 0.5°
    ninety: tuple[Point, ...]           # vertices with a 90° (± 0.5°) direction change
    acute: tuple[Point, ...]            # vertices with a change ≥ 134.5° (135° or sharper)
    off_angle: tuple[Point, ...]        # every other failing vertex: δ > 0.5° and |δ − 45°| > 0.5° and not ninety/acute (e.g. 46°, 60°, 90.8°)
    t_junctions: tuple[Point, ...]      # off-pad branch meeting at 90°
    micro_jogs: tuple[Point, ...]
    short_chamfers: tuple[Point, ...]
    bends: int                          # 45° bends (pad entries excluded)
def polylines(model: BoardModel, net: str, layer: str) -> list[tuple[tuple[Point, ...], float]]  # (points, width); joins collinear segments sharing an endpoint, splits at vias/pads/branches
def analyse_bends(points: Sequence[Point], width: float, *, pad_ends: tuple[bool, bool]) -> BendReport
def analyse_tracks(model: BoardModel) -> BendReport         # all nets/layers, adds T-junctions
ANGLE_TOL_DEG = 0.5; CHAMFER_MIN = lambda w: max(1.0 * w, 0.25); JOG_MIN = lambda w: 2.0 * w
```

Vertex classification has no gaps: δ ≤ 0.5° collinear, |δ − 45°| ≤ 0.5° a 45° bend, every
other vertex fails as `ninety`, `acute` or `off_angle`; M4 hard = non-octilinear + ninety +
acute + off_angle + T-junctions. Router (P5a) and post-processor (P6a) import `analyse_bends`;
their property tests assert every failing field is empty (incl. `off_angle`); nothing else
defines these terms.

### B-C6. Classification and `board.md` — `cc/board/{classify,boardmd}.py` (P3a)

```python
PartClass = Literal["anchor","connector","decoupling_cap","switching_regulator","inductor",
                    "crystal","module_with_antenna","mounting_hole","fiducial","test_point","other"]
NetKind = Literal["ground","supply","switch_node","diff_pair","crystal","high_current","signal"]
@dataclass(frozen=True) class Decoupling: cap: str; ic: str; pin: str; supply_net: str; ground_net: str
@dataclass(frozen=True) class Switcher:   ic: str; inductor: str; sw_net: str; vin_net: str
                                          in_caps: tuple[str, ...]; out_caps: tuple[str, ...]; fb_parts: tuple[str, ...]
@dataclass(frozen=True) class Crystal:    ref: str; ic: str; load_caps: tuple[str, ...]; nets: tuple[str, ...]
@dataclass(frozen=True) class DiffPair:   p: str; n: str; anchor: str
@dataclass(frozen=True) class BlockProposal: name: str; anchor: str | None; refs: tuple[str, ...]; sheet: str
@dataclass(frozen=True) class Classification:
    part_class: Mapping[str, PartClass]; net_kind: Mapping[str, NetKind]
    decoupling: tuple[Decoupling, ...]; switchers: tuple[Switcher, ...]; crystals: tuple[Crystal, ...]
    pairs: tuple[DiffPair, ...]; blocks: tuple[BlockProposal, ...]; critical_nets: tuple[str, ...]
    overrides_applied: tuple[str, ...]
def classify(model: BoardModel, doc: BoardDoc | None = None) -> Classification

@dataclass(frozen=True) class BoardDoc:
    edges: tuple[EdgeRow, ...]            # | Ref | Edge/position | Reason |
    blocks: tuple[BlockRow, ...]          # | Block | Parts | Region | Notes |
    class_overrides: tuple[ClassOverride, ...]   # | Ref | Class | Reason |
    net_classes: tuple[NetClassRow, ...]  # | Class | Width | Clearance | Via | Nets |
    accepted: tuple[AcceptedWarning, ...] # | DRC type | Items/refs | Reason | Accepted by |
    allowed_sides: frozenset[Side]        # from Summary "Assembly:" line; default {"top"} (plan call 9)
    preset: str | None                    # from Summary "Preset: jlcpcb-4l"; else preset_for() picks by copper layer count
    errors: tuple[str, ...]               # unparseable rows (never raises)
def parse_board_md(text: str) -> BoardDoc
async def read_board_doc(ctx: ToolContext) -> BoardDoc   # empty BoardDoc when board.md is missing
```

### B-C7. Metrics — `cc/board/metrics.py` (P3a)

```python
MetricId = Literal["M1",…,"M14"]; Verdict = Literal["pass","review","fail","n/a"]
@dataclass(frozen=True) class Offender: what: str; refs: tuple[str, ...]; nets: tuple[str, ...]; at: Point | None; value: float | None
@dataclass(frozen=True) class Metric: id: MetricId; value: float | int | None; verdict: Verdict
                                     hard: str | None; review: str | None; offenders: tuple[Offender, ...]   # ≤ 20 per metric, + omitted count
                                     omitted: int = 0
@dataclass(frozen=True) class MetricsReport: metrics: tuple[Metric, ...]; hard_failures: int; reviews: int
                                            stage: Literal["placement","routed"]
def compute_metrics(model: BoardModel, cls: Classification, doc: BoardDoc, *,
                    drc: DrcResult | None, parity: tuple[DrcMarker, ...] | None,
                    preset: FabPreset | None, only: frozenset[str] | None = None) -> MetricsReport
def connections(model: BoardModel, net: str) -> list[tuple[Pad, Pad]]   # octilinear MST over the net's pads
def routed_length(model: BoardModel, a: Pad, b: Pad) -> float | None     # shortest path in the net's track/via graph, vias 0
```

DRC type names are normalised (lower case, `drce_` prefix stripped) so API enum names and
`kicad-cli` JSON strings (`silk_overlap`) match the same `board.md` Checks rows (P3a).
`stage` = `"routed"` when any track exists on a non-ground net, else `"placement"` (M8/M9 use
octilinear estimates at placement stage, routed lengths after; spec §12 notes). Thresholds
are module constants `HARD` / `REVIEW` with the spec §12 values; review thresholds may be tuned
on fixtures (recorded in "As built"), hard ones never.

### B-C8. Presets — `cc/board/presets.py` (P1a; stackups P2b)

```python
@dataclass(frozen=True) class FabPreset:
    id: Literal["jlcpcb-2l","jlcpcb-4l","jlcpcb-6l"]; layers: int
    min_track: float; min_space: float; pref_track: float
    min_via_drill: float; min_via_diameter: float; min_drill: float; min_npth: float
    min_annular: float; copper_to_edge: float; hole_to_hole: float   # one KiCad rule → 0.45 (pad); via 0.2 implied
    silk_min_height: float; silk_min_line: float; thickness: float; outer_copper_oz: float
PRESETS: Mapping[str, FabPreset]
STACKUPS: Mapping[str, StackupPreset]   # P2b: "jlc-2l-1.6", "jlc-4l-1.6-7628", "jlc-6l-1.6"
```

Values: see "Calls made by the plan author", call 8 (verified 2026-11-25 against
jlcpcb.com/capabilities/pcb-capabilities); P1a pins them in `test_presets.py`. `FabPreset` is
what M14 checks (hole-to-edge uses `copper_to_edge`, measured from the hole edge); P2 applies
it in `pcb_set_rules {preset}` (a preset never changes the layer count — callers pass
`layers`).

### B-C9. Renders — `cc/views/board_render.py` + `cc/board/render.py` (P1c)

```python
BoardView = Literal["top","bottom","layer","placement","3d"]
@dataclass(frozen=True) class BoardRender: png: Path; svg: Path | None; placement_hash: str; board_hash: str
async def render_board(ctx: ToolContext, view: BoardView, *, layer: str | None = None,
                       region: Rect | None = None, highlight_nets: Sequence[str] = (),
                       markers: Sequence[Marker] = (), angle: str = "top") -> BoardRender
# cc/board/render.py (pure): wrap_svg(svg_text, *, extent, scale_bar=True, hulls, markers, title) -> str
BOARD_SVG_LIMITS = SvgLimits(max_bytes=24 MiB, max_elements=200_000, max_coords=2_000_000,
                             max_element_coords=50_000, max_span=…)   # P1c measures on fixtures, may tune
# footprint_render.rasterise gains max_side_px / timeout_s / memory_bytes keyword arguments (defaults = today's);
# it passes CPU (= ceil(timeout_s)) and memory limits to _raster_worker as two extra argv items
```

`cc/views/footprint_render.py` gains a `SvgLimits` dataclass (default = today's constants incl. `MAX_SPAN`) passed
to `load_kicad_svg(..., limits=)`; footprint behaviour unchanged. PNGs/SVGs go to the existing
`agent_dir(project)/"renders"` and are served by the existing renders route.

### B-C10. Checkpoints — `0013_board.sql`, `cc/db/repos/board.py`, `cc/kicad_tools/board/checkpoints.py` (P3b)

```sql
CREATE TABLE board_checkpoints (id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, board_file TEXT NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('placement','board')), hash TEXT NOT NULL,
  conversation_id TEXT NOT NULL, run_id TEXT NOT NULL, user_quote TEXT NOT NULL, approved_at TEXT NOT NULL);
CREATE INDEX board_checkpoints_latest ON board_checkpoints(project_id, board_file, stage, id);
CREATE TABLE board_reviews (id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, board_file TEXT NOT NULL,
  conversation_id TEXT, stage TEXT, placement_hash TEXT NOT NULL, board_hash TEXT NOT NULL, run_id TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('render','check')), view TEXT, check_level TEXT CHECK (check_level IN ('quick','full')),
  hard_failures INTEGER, m1_pass INTEGER, created_at TEXT NOT NULL);
CREATE INDEX board_reviews_placement ON board_reviews(project_id, board_file, placement_hash);
CREATE INDEX board_reviews_board ON board_reviews(project_id, board_file, board_hash);
```

```python
class BoardCheckpointRepo: approve(...) -> int; latest(project_id, board_file, stage) -> Checkpoint | None
class BoardReviewRepo: record_render(...); record_check(...); evidence(project_id, board_file, conversation_id, *, placement_hash=None, board_hash=None) -> list[Review]; last_full_check(project_id, board_file, board_hash) -> Review | None
def checkpoint_state(db, project, model) -> dict[str, CheckpointState]   # {"placement": {...approved, stale}, "board": {...}}
async def require_placement_approved(ctx: ToolContext, model: BoardModel) -> None   # → placement_not_approved
async def require_board_approved(ctx: ToolContext, model: BoardModel) -> None       # → not_approved (also when last full check for the hash has hard failures)
```

`board_file` = the board path relative to the project dir (posix). Only the **latest** row per
(project, board_file, stage) counts. `cc/approvals.py` (P3b) holds the guards shared with
`fp_approve`: `normalise(text)`, `quote_in_message(quote, message) -> bool`,
`human_messages_after(ctx, since_iso) -> list[Message]`, `plan_running(ctx) -> bool`.
`pcb_approve` guards (P3b, each `approval_not_allowed` with its own `details.guard`): 1 hash
of the stage, 2 evidence in this conversation for the hash, 3 evidence passes, **4 no write
after evidence** (no change-log entry of a `BOARD_KINDS` kind other than `board_approve` for
this project newer than the newest evidence row used — the ABA guard, mirroring
`fp_approve`'s `last_geometry_write_at` check), 5 a later human message, 6 the quote in it,
7 no plan running.

### B-C11. Placer — `cc/board/placer/` (P4)

```python
# Move is defined in cc/board/staging.py (P2 uses it first, for the staging area); the placer re-exports it
@dataclass(frozen=True) class Move: ref: str; x: float; y: float; rot: float; side: Side
@dataclass(frozen=True) class PlaceResult: moves: tuple[Move, ...]; score: PlacementScore
@dataclass(frozen=True) class DoesNotFit: needed_mm2: float; available_mm2: float; refs: tuple[str, ...]
def place_block(model, cls, block_refs, region: Rect, *, side: Side, rotation: int | None,
                keep_channels: float = 0.5, fixed: Sequence[Move] = (),
                allowed_sides: frozenset[Side], edge: Literal["top","bottom","left","right"] | None = None,
                target: Point | None = None) -> PlaceResult | DoesNotFit
def legalize(model, moves: Sequence[Move], *, movable: frozenset[str], max_shift: float | None,
             keepouts: Sequence[tuple[Point, ...]], edge_margin: float) -> tuple[Move, ...] | DoesNotFit
def score_placement(model, cls, doc) -> PlacementScore   # M1/M8/M9/M10 + airwire + max pin density per 5×5 mm cell
# cc/board/staging.py (P2, used by P2 sync and P4):
def staging_positions(model: BoardModel, refs: Sequence[str]) -> tuple[Move, ...]   # grid right of the outline / of placed parts / of (0,0), spec §7.2
```

Placement grid 0.5 mm × 4 rotations, legaliser snap 0.05 mm, default channel 0.5 mm.
Determinism: same input → same output (no unseeded randomness, stable sort keys).

### B-C12. Router — `cc/board/router/` (P5a) and post-processor (P6a)

```python
@dataclass(frozen=True) class RouteRequest: net: str; a: Pad; b: Pad; width: float; clearance: float
                                            layers: tuple[str, ...]; allow_vias: bool; via: tuple[float, float]  # (diameter, drill)
@dataclass(frozen=True) class RoutePath: segments: tuple[tuple[str, Point, Point], ...]   # (layer, start, end)
                                         vias: tuple[Point, ...]; length: float
MAX_CELLS_PER_LAYER = 4_000_000
class RouteTooLarge(Exception): cells: int   # build_grid refuses a larger window; the caller reports the connection failed "too_large"
def build_grid(model: BoardModel, req: RouteRequest, *, pitch: float = 0.05, margin: float = 0.0) -> ObstacleGrid
def route(grid: ObstacleGrid, req: RouteRequest, *, max_expansions: int = 2_000_000) -> RoutePath | None
def route_pair(model, p: RouteRequest, n: RouteRequest, gap: float) -> tuple[RoutePath, RoutePath] | None
def solve(model: BoardModel, req: RouteRequest, *, margin: float = 0.0, max_expansions: int = 2_000_000) -> RoutePath | None   # run_cpu entry point
def solve_pair(model, p, n, gap, *, margin: float = 0.0) -> tuple[RoutePath, RoutePath] | None                                 # run_cpu entry point
def pair_coupling(a: RoutePath, b: RoutePath) -> dict[str, float]   # keys length_mismatch, gap_min, gap_max (centre-to-centre over the coupled stretch; M12 reporting)
def prune_model(model: BoardModel, req: RouteRequest | tuple[RouteRequest, RouteRequest]) -> BoardModel   # only items intersecting the retry window; callers pass it to solve/solve_pair
# cc/kicad_tools/board/route.py (P5b), reused by P6b rip-and-reroute and ladder rung (c):
async def route_connections(sctx: BoardStageContext, plans: Sequence[ConnPlan], *, lock: bool,
                            deadline: float | None = None) -> RouteOutcome   # deadline = time.monotonic() value; connections not started by then → failed "timeout"
#   RouteOutcome(routed: tuple[str, ...], failed: tuple[tuple[str, str], ...]); connection key "{net}:{refA}.{padA}-{refB}.{padB}"
#   failure reasons: no_path, too_large, timeout, drc_rejected: <text>
# chamfer.py (P6a)
@dataclass(frozen=True) class PostResult: replace: tuple[tuple[str, tuple[Track, ...]], ...]  # (old kiid, new tracks)
                                          # (kiid, ()) = delete (merges / duplicates)
                                          rip: tuple[str, ...]   # connection keys "{net}:{refA}.{padA}-{refB}.{padB}"
                                          reasons: tuple[tuple[str, str], ...] = ()   # (key, reason)
                                          merged: int = 0; chamfered: int = 0
def postprocess(model: BoardModel, kiids: frozenset[str]) -> PostResult
```

### B-C13. Freerouting runner — `cc/board/freerouting.py` (P6a)

```python
@dataclass(frozen=True) class FreeroutingRuntime: java: Path; jar: Path; version: str
def find_runtime(settings: Settings) -> FreeroutingRuntime | None   # verifies files + version string
@dataclass(frozen=True) class FreeroutingRun: returncode: int; seconds: float; log_tail: str; timed_out: bool
async def run_freerouting(rt: FreeroutingRuntime, dsn: Path, ses: Path, *, timeout_s: float,
                          passes: int, workdir: Path, extra_args: Sequence[str] = ()) -> FreeroutingRun   # killed on CancelledError
```

### B-C14. Error codes (all phases)

`board_tools_disabled`, `no_board`, `schematic_unsaved`, `sync_failed`, `invalid_argument`,
`not_found` (ref/net/block), `unknown_block`, `stackup_unavailable`, `does_not_fit`,
`part_routed`, `placement_not_approved`, `not_approved` (`details.reason` ∈ {`missing`,
`stale`, `check_failed`, `changed_during_export`}), `approval_not_allowed`
(`details.guard` 1–7, B-C10), `freerouting_unavailable`, `fill_not_acceptable` (`details.reason` ∈ {`rip_ratio`,
`timeout`, `freerouting_error`}), `board_compute_timeout`,
`drc_rejected`, `verification_failed` (existing, `cc/kicad_tools/edit/engine.py`), `output_path_unsafe`, `render_failed`, `export_failed`,
`session_type_unavailable` (HTTP 400), and the existing `document_inconsistent`, `read_only`,
`save_failed`, `plan_required`, `plan_step_required`. Change-log kinds (`BOARD_KINDS`,
`cc/changes/repo.py` + `highlight.ts`): `board_` + tool name without `pcb_`, for every
`PCB_WRITES` tool that changes documents, plus `board_approve`.

---

## As built

(Each phase adds its section here in its own PR.)

## Residuals

Open after P9b (collected from every phase's "As built"; nothing here blocks the release).

**Known from the spec (non-goals / deferred)**
- Fast interfaces (USB HS, DDR, Ethernet) and impedance-controlled routing; blind/buried/micro
  vias; panelisation; JLC assembly validation (CPL rotation offsets, LCSC numbers).
- Polygon outlines (v1 = rounded rectangle), DXF/STEP mechanical import, automatic case fit
  (owner D3; P10 only adds parts to STEP).
- KiCad interactive router headless (G4, upstream feature request by the owner); stale headless
  schematic jobs (G2, workaround); kipy wrappers for G3/G5 (raw protobuf, upstreaming optional,
  owner business); patch 0005 upstream MR (owner).
- "Approve" button in the chat UI (with the footprint button).
- Fab outputs are not snapshotted (regenerated on demand).

**Process / CI**
- Freerouting behaviour pinned to 2.5.0; a version bump needs the P6 tests re-run and the
  golden refreshed (`UPDATE_GOLDENS=1`, explanation in the PR).

---

## Decided by owner

Binding (spec §15.1 and §15.3, 2026-11-23 … 2026-11-25):

1. SP4 = the board; the agent does everything, the user steers only by chat prompts.
2. Quality bar strict ("order without rework"); all parts placed, no long traces, straight
   segments, **only 45° direction changes — no 90°, no acute**, clean straight traces, logical
   grouping.
3. Step-by-step flow; the agent asks design questions; images + check report and wait for an ok
   after each major step (at least after placement, before routing).
4. Generic tools and skills; rules per part class.
5. Approach A (agent-led placement with helpers; own router for critical/short nets, locked;
   Freerouting fill post-processed or rejected; planes/fanout/pours/stitching; self-check loop).
6. JLCPCB; self-assembly (hot plate + stencil) for the acceptance board; layer count proposed,
   not hard-coded.
7. Acceptance: OpenDriveHub transmitter main board in PROD after the release.
8. D1 default 4-layer stack Sig / GND / Sig+Power / Sig; D2 hard thresholds as written; D3 3D
   models / case fit deferred to optional P10; D4 DSN text editing only as last resort; D5 PROD
   memory ≈ 4.5–5 GB per active board project accepted, documented (P9b).

## Calls made by the plan author

Each is pinned by a test or a task in the named phase.

1. **Phase splits** P1 → P1a/P1b/P1c, P3 → P3a/P3b, P5 → P5a/P5b, P6 → P6a/P6b, P9 → P9a/P9b
   (reasons in "Phase overview").
2. **P2b does not wait for the owner's upstream text**: the patch is carried with the
   placeholder subject (`0005-TODO-owner-write-message-board-stackup-handler.patch`) exactly like
   0001–0004 (C14, patches README); the owner's text is needed only for an upstream MR. The
   spec's order line ("P2b whenever the owner has written the patch message") is therefore read
   as "whenever the owner gives the go" — listed under "Provisional".
3. **No new dependency**: property tests are seeded `random.Random(seed)` loops over ≥ 200 cases
   (seed printed on failure); polygon geometry is stdlib (`cc/board/geom.py`).
4. **`BOARD_KINDS` enumerated up front in P1a** (all v1 tools are fixed by spec §7), with a P1c
   unit test that every registered two-phase `PCB_WRITES` tool maps to a kind.
5. **`PCB_READ` / `PCB_WRITES` defined completely in P1c** (all spec §7 names); later phases only
   register specs. `profile_specs` intersects with registered specs, so unregistered names are
   harmless.
6. **Input bounds** for all board tools: coordinates |v| ≤ 1000 mm, sizes/widths/clearances
   0 < v ≤ 100 mm (drills ≤ 10 mm; board outline width/height 5–500 mm), rotations finite (normalised mod 360), timeouts within the
   spec maxima, `region` w/h > 0, polygons ≤ 256 points, names `^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,63}$`
   for blocks/zones/rules; references and nets are validated against the board, not by a
   pattern (unknown → `not_found` with `details.closest`).
7. **`pcb_approve` is a two-phase `edit_spec`** whose apply writes only DB rows and the change log
   (`changed_docs` empty, `Logical(1)`), so the budget and `PLAN_EXEMPT_TOOLS` (spec §7.3) apply.
   `pcb_fab_export` / `pcb_export_3d` are plain `ToolSpec`s (`mutating=True`,
   `changes_documents=False`) — they write no KiCad data, cost 0 and log no change entry.
8. **JLCPCB preset verified 2026-11-25** against jlcpcb.com/capabilities/pcb-capabilities and
   jlcpcb.com/impedance. The spec's conservative values are kept where JLC allows less (track/
   space 0.127 mm min, 0.15 preferred — JLC: 0.10 (2L) / 0.09 (4L+); via 0.3 / 0.6 mm and min
   drill 0.3 — JLC: 0.15 / 0.25; copper-to-edge 0.3 — JLC: 0.2). **Two spec values are below
   JLC's current minimum and are raised**: annular ring 0.13 → **0.15 mm (4/6 layers), 0.18 mm
   (2 layers)** (JLC absolute minimum; recommended 0.20 / 0.25), so the 2-layer preset's minimum via
   diameter becomes **0.66 mm** (0.3 drill + 2 × 0.18); silk text height 0.8 → **1.0 mm**
   (JLC "40 mil (1.0 mm)"; line 0.15 unchanged). Added from the page: NPTH ≥ 0.5 mm, pad
   hole-to-hole ≥ 0.45 mm, via hole-to-hole ≥ 0.2 mm. Thickness 1.6 mm, 1 oz outer. These make
   M14 stricter, never weaker — listed under "Provisional" because M14 is an owner hard gate
   (D2). `pcb_tidy_silk` uses the preset's `silk_min_height` (1.0 mm), not the spec's 0.8 mm.
   Stackups: `JLC04161H-7628` = Cu 0.035 / 7628 prepreg 0.2104 / Cu 0.0152 / core 1.065 /
   Cu 0.0152 / 7628 prepreg 0.2104 / Cu 0.035 mm; `jlc-6l-1.6` = `JLC06161H-3313` (first named
   6-layer stackup; JLC names no default); P2b records the per-layer values.
9a. **Plane layers during the fill** (spec §10.3, call 48): the pinned protos have no settable
    copper layer type (found while planning; P2's probe records the result). If P2 confirms
    "not settable", P6b uses the spec's fallbacks in order — Freerouting CLI options, then (last
    resort, owner D4) editing the exported DSN text — and records which one worked.
9. **Allowed SMD sides** come from `board.md` → Summary line `Assembly: <method>; sides: top|top+bottom`
   (default `top` when absent — the hot-plate default of spec §8.2); M1 and the placer read it.
10. **Change-log kind naming**: `board_` + tool name without `pcb_` (e.g. `board_move`).
11. **Freerouting lock file** `docker/freerouting.lock` (shell `KEY=value`: `FREEROUTING_VERSION`,
    `FREEROUTING_URL`, `FREEROUTING_SHA256`, `TEMURIN_URL`, `TEMURIN_SHA256`) read by
    `scripts/fetch-freerouting.sh <dest>`; the Dockerfile and the CI kicad job call it; CI caches
    `<dest>` with `actions/cache` keyed on the lock file hash.
12. **`kicad_e2e` marker** registered in `backend/pyproject.toml`; the 8 `kicad` shards run
    `-m "kicad and not kicad_e2e"`; a new `kicad-e2e` CI job runs `-m kicad_e2e` on the pinned
    base image only.
13. **Restore re-discovery**: `restore._reload` calls `get_project(root, project.id)` after the
    file operations and passes the fresh `Project` to `sessions.reopen` and `publish_view`; the
    tool-side equivalent after board creation is `ctx.reload_project() -> tuple[Project,
    KicadSession]` (new `ToolContext` callback, P1a; reopening replaces the session), and the
    engine assigns both onto `ctx` for the rest of the call. `RunEngine.project` and
    `HistoryHooks.project` are fixed at creation (`cc/agent/engine.py`, `cc/history/hooks.py`), so
    P1a adds `RunEngine.refresh_project(project)` and a hooks `on_project_changed(project)`, called
    by both paths.
14. **Board evidence recording** happens only when `ctx.conversation_id` is set (as for
    footprints); evidence rows carry both hashes of the rendered/checked state.

15. **Classification refinements of spec call 20** (found on `sp4_synth`, P3a): a switch node
    tolerates a bootstrap cap between SW and the IC's boot pin ("only that IC and one
    inductor" otherwise never matches); a switcher's output caps are the caps on the
    inductor's other net **on the switcher's sheet** (else the nearer anchor), so an MCU's
    decoupling caps on the same rail are not taken as buck output caps.
16. **Fill defaults** (P6b): `pcb_route_fill` gets an optional `plane_layers` input (default per
    D1: `In1.Cu` on 4 layers; `In1.Cu` + `In4.Cu` on 6 layers; none on 2 layers); default
    `passes` 30; M6 alone never triggers a rip (a rip needs detour > 1.5 as well).
17. **BOM header** (P8): `Comment, Designator, Footprint, LCSC Part #` — the fourth column falls
    back to the MPN field when no LCSC field exists.

## Provisional — needs owner confirmation

1. **JLC preset tightening** (call 8): annular ring 0.15 / 0.18 mm and silk text height 1.0 mm
   instead of the spec's 0.13 mm / 0.8 mm, to match JLC's current published minimums. Stricter
   only. Until the owner answers, P1a implements the stricter values (a looser M14 could pass a
   board JLC rejects).
2. **M1 vs. edge connectors** (spec contradiction): M1 (hard) requires every courtyard fully
   inside the outline, but §9.3/M13 let a connector (and a module antenna) sit "on or
   overhanging the outline as the footprint requires". A USB-C footprint whose courtyard must
   overhang would fail M1. Recommended: a part listed in `board.md` → Placement edges may
   overhang **its assigned edge only** (M13 then checks that edge); everything else as
   written. Until the owner answers, P3a implements M1 exactly as written (stricter) behind a
   one-line switch `M1_EDGE_OVERHANG = False` so the change is small.
3. **P2b start** (call 2): carry patch 0005 with the placeholder subject as soon as P2 is
   merged (recommended), or wait for the owner's commit text.

---

## Phase Conventions (apply to every phase)

- **Branch/PR:** one PR per phase, title `board phase <P#>: <short goal>`, via `gh` CLI. Commits
  end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (KiCad patch commits: the
  trailer `Assisted-by: Claude` instead), PR bodies with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- **TDD:** failing test → see it fail → minimal code → pass → commit, per task.
- **Commands** (from `backend/`): `uv run pytest -m "not kicad"`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy src`; frontend `npm run lint`, `npx vitest run`,
  `npm run typecheck` in `frontend/`; KiCad tests `uv run pytest -m kicad tests/kicad/board`
  inside `ghcr.io/peterus/copperchat-kicad-base:<KICAD_COMMIT>` (invocation as in
  `tests/kicad/test_kicad_api_patches.py`). Long commands always with `timeout`.
- **Real-KiCad tests** live in `backend/tests/kicad/board/` and use `ecc83_session`,
  `blank_session`, `blank_nopcb_session` (`tests/support/kicad_project.py`) and, from P1b,
  `synth_session` (schematic only, no board committed; P2's sync creates the board). A board server costs ≈ 1.9 GB: share one
  module-scoped server where isolation allows; CI shards the kicad job 8×.
- **Unit tests** in `backend/tests/unit/board/` (new). Never mock KiCad in a `-m kicad` test.
- **Review:** after CI is green, `kontor-recipes:review-loop --max-rounds 3
  --dimensions bugs+tests,security+performance`, a fresh reviewer child per round; pure style
  nits deferred after round 2; cap hit with code changes → one `--review-only` verify pass.
  **Never Copilot.**
- **Done report:** `kontor_send_to_session(<coordinator>, status:'done')` with PR, CI, review
  result and deviations; contract deviations are reflected in this index in the same PR
  ("As built (P#)").
- **Never:** hand-write or hand-edit a KiCad file (also not fixtures or tests), act on KiCad's
  GitLab or Freerouting's GitHub, request Copilot, use background subagents, `pkill -f` /
  `killall` (only own PIDs).
- **No placeholders** in merged code. Read tools never take the edit lock.
