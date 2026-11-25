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
| `cc/board/{__init__,model,geom,worker,presets,_selftest}.py` (`presets.py`: `FabPreset` + `PRESETS`, pure data), `cc/kicad_tools/board/{__init__,read,api,engine,compensate}.py` (engine incl. `BoardStage.compensable` / `undo`), `cc/kicad_session/session.py`, `cc/history/{store,restore,service}.py`, `cc/changes/{repo,diff}.py`, `frontend/src/components/changes/highlight.ts`, `cc/config.py` (`board_tools`), `cc/tools/spec.py` + `cc/agent/{engine,sdk_backend,fake}.py` + `cc/history/hooks.py` (`ToolContext.reload_project` + `ToolContext.reloaded_generation`, `refresh_project`, `on_project_changed` only), `cc/agent/sdk_tools.py` (`_two_phase_steps` generation re-baseline after a session replacement only), `cc/kicad_tools/edit/engine.py` (refusal) | P1a (1) | P1b none |
| `backend/tests/fixtures/projects/sp4_synth/**`, `scripts/make-sp4-synth.py`, `scripts/refresh-fixtures.sh`, `backend/tests/support/kicad_project.py` (`synth_session`), `backend/tests/kicad/board/goldens/sp4_synth_summary.json` | P1b (1) | P1a none |
| `cc/agent/{profiles,sdk_backend,fake}.py`, `cc/agent/sdk_tools.py` (board publish), `cc/history/{live,hooks}.py`, `cc/views/{snapshot,footprint_render,_raster_worker}.py` (`_raster_worker`: CPU/memory limits from argv), new `cc/views/board_render.py`, `cc/board/render.py`, `cc/web/routes_conversations.py`, `frontend/src/components/sessions/NewSessionDialog.tsx`, `cc/design_docs/service.py`, `cc/skills/builtin/design-docs/templates/board.md`, new `cc/skills/builtin/session-board/`, `cc/kicad_tools/board/{specs,status,views}.py`, `cc/tools/registry.py`, `cc/web/app.py` (`/api/features` registration), new `frontend/src/api/features.ts`, `cc/history/service.py` (pcb branch of the turn-end view refresh) | P1c (2) | — |
| `cc/kicad_tools/board/{sync,setup}.py`, `cc/kicad_tools/board/specs_setup.py`, `cc/board/staging.py` (consumes P1a's `presets.py`) | P2 (3) | `board/specs.py` append |
| `cc/board/{classify,boardmd,metrics}.py`, `cc/board/jobs.py` (the one module of `run_cpu` job functions: `analyse_job`, `metrics_job(model, doc, drc, preset, only, parity=None)` — builds the `Classification` itself and passes `parity` to `compute_metrics`; every metrics computation (P3b `pcb_check`, P6b fill report, P7 `pcb_zones`, P8 fab report) is `run_cpu(metrics_job, …, only=…)`, never `compute_metrics` directly — plus `strip_fills`), `cc/board/geom.py` (bend analysis, append), `cc/kicad_tools/board/{analysis,specs_analysis}.py` | P3a (3) | `board/specs.py` append; `cc/board/jobs.py` append (P4 `place_block_job` / `legalize_job`, P5b/P6b if needed, P7 `fanout_candidates_job` / `stitch_candidates_job`) |
| `cc/db/migrations/0013_board.sql`, `cc/db/repos/board.py`, new `cc/approvals.py`, `cc/kicad_tools/footprints/approve.py` (refactor to `cc/approvals.py`), `cc/editing/gate.py` (`PLAN_EXEMPT_TOOLS`), `cc/kicad_tools/board/{check,approve,checkpoints,specs_check}.py`, `board/status.py` + `board/views.py` (evidence/checkpoint fields), `cc/changes/repo.py` (add in-transaction helper `ChangeRepo.add_many_in_transaction` only) | P3b (4) | `board/specs.py` append |
| `cc/board/router/{__init__,grid,astar,simplify,pairs}.py` — **only P5a creates `cc/board/router/__init__.py`** | P5a (4) | P6a must not create or edit `__init__.py` (it adds `chamfer.py`, imported by full path); if P5a is not merged yet, P6a creates an empty `__init__.py` only if absent and keeps P5a's version on rebase |
| `scripts/fetch-freerouting.sh`, `docker/freerouting.lock`, `docker/app/Dockerfile`, `.github/workflows/ci.yml` (kicad job cache + fetch step), `cc/board/freerouting.py`, `cc/board/router/chamfer.py`, `cc/config.py` (freerouting settings, append), `LICENSES/` notices, `scripts/app-image-tools.sh`, `.gitignore` | P6a (4) | — |
| `docker/kicad-base/patches/0005-*.patch`, `docker/kicad-base/patches/README.md`, `cc/kicad_tools/board/setup.py` (stackup branch), `cc/kicad_tools/board/specs_setup.py` (stackup enum), `cc/board/presets.py` (append `StackupPreset`, `STACKUPS`), `docs/kicad-patches/0005-board-stackup-FACTS.md` | P2b (4) | runs after P2 merged |
| `cc/board/placer/**`, `cc/kicad_tools/board/{place,unroute_ops,specs_place}.py` | P4 (5) | — |
| `cc/kicad_tools/board/{route,specs_route}.py` | P5b (6) | — |
| `cc/kicad_tools/board/{fill,specs_fill}.py`, `cc/agent/profiles.py` + `cc/agent/{sdk_backend,fake}.py` (freerouting filter + call sites) | P6b (7) | — |
| `cc/board/{planes,silk}.py`, `cc/kicad_tools/board/{zones,fanout,stitch,silk,specs_copper}.py` | P7 (7) | `cc/board/jobs.py` append (`fanout_candidates_job`, `stitch_candidates_job`) |
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
  `COPPERCHAT_FREEROUTING` (`Settings.freerouting`, default on) removes only `pcb_route_fill`
  (so does a CLI tool-call limit below 495 s, P6b `FILL_ALLOWED`, startup log line);
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
  separate 120 s deadline. `pcb_route`: total deadline 300 s. Deadline-bound loops check the
  deadline before **every** margin attempt and bound each step by the remaining time: each
  `run_cpu` gets `timeout_s = min(30, remaining)` and `wait_s = remaining`, each DRC
  `min(60, remaining)`; worst case = deadline + one DRC (≤ 60 s). `pcb_route_fill`'s whole
  sum: `timeout_s` (≤ 600) + round-trip I/O 60 (`IO_BUDGET_S`, DSN export + SES import only,
  an `io_spent` accumulator timed around those two calls — Freerouting's runtime never counts)
  + stage-4 refill 105 (own budget: `refill_call_timeout(REFILL_TIMEOUT_S)` = 60 + 30 + 15)
  + post 120 + DRC 60 + final metrics 60 (fixed `timeout_s=30, wait_s=30`; timeout →
  `metrics: null`, not an error) = `FILL_WORST_CASE_S` 1005 s (P6b). `pcb_fanout`: overall
  `FANOUT_DEADLINE_S = 300`, `pcb_stitch`: `STITCH_DEADLINE_S = 180` (P7) — checked before
  each round/stage; each refill bounded by `min(REFILL_TIMEOUT_S, remaining)` (floor
  `REFILL_MIN_S` 20 s: less remaining → refill step skipped, reported `timeout`), each DRC
  `min(60, remaining)`; pads/vias left at the deadline → reported with reason `timeout`;
  worst case = deadline + one cleanup refill (≤ 105 s).
- **Tool worst-case durations** (from the constants above; P6b Step 2 compares each with the
  CLI's tool-call limit `L`):

  | Tool | Worst case | Sum |
  |---|---|---|
  | `pcb_route_fill` | 1005 s | `timeout_s` 600 + `FILL_FIXED_S` 405 (I/O 60 + refill 105 + post 120 + DRC 60 + metrics 60) |
  | `pcb_fanout` | 405 s | `FANOUT_DEADLINE_S` 300 + cleanup refill 105 |
  | `pcb_route` | 360 s | `ROUTE_DEADLINE_S` 300 + one DRC 60 |
  | `pcb_stitch` | 285 s | `STITCH_DEADLINE_S` 180 + cleanup refill 105 |

  If `L` is lower than a worst case + 60 s, set `MCP_TOOL_TIMEOUT` (≥ 1,065,000 ms = 1005 s
  + 60 s); if that does not lift it, scale down: every deadline is capped so its tool's
  worst case ≤ `L − 60` (`timeout_s` max = `L − 465`, `ROUTE_DEADLINE_S` = `L − 120`,
  `FANOUT_DEADLINE_S` / `STITCH_DEADLINE_S` = `L − 165`, each `min` with its nominal value);
  if the capped `timeout_s` max falls below its schema minimum 30 (`L` < 495 s),
  `pcb_route_fill` is disabled — removed from the profiles exactly like
  `COPPERCHAT_FREEROUTING=off`, with one startup log line — and reported to the coordinator as
  an owner decision. `L` and every cap are recorded in "As built (P6b)".
- Renders: PNG ≤ 1600 px long side; ≤ 3 images per step report; SVG through the footprint
  sanitiser with board limits (B-C9); every injected string XML-escaped. Retention: board PNGs
  kept; board SVGs (+ `.svg.json` sidecars) beyond the newest 20 per project pruned at each
  `pcb_render` (P1c).
- Fab: `<project>/fab/jlcpcb/` and `<project>/fab/3d/`, refused if `fab/` or the target is a
  symlink or resolves outside the project (validated **before** the stale-leftover sweep, which
  only ever runs in the validated `fab/`); written to a temp dir and swapped in two steps; BOM/CPL
  text cells starting with `=`, `+`, `-`, `@`, tab or CR get a leading `'` (P8).
- Live view: board publishes **once per tool call from the adapter after `apply`**, never from
  the per-commit hook while a board tool is applying.
- **Every long KiCad call passes an explicit timeout** (B-C4): `session.call`'s default
  (`kicad_api_timeout_s`, 30 s) restarts the api-server on expiry, so DRC calls pass
  `timeout=drc_call_timeout(t)` (= `t + 30 + 15`) and every zone refill goes through
  `api.refill(h, timeout_s=t)` with `timeout=refill_call_timeout(t)` (= `t + 30 + 15`; B-C3:
  kipy's `refill_zones` returns silently at `max_poll_seconds` while KiCad may still be
  filling, so `refill` probes busy state and waits ≤ 30 s, else `refill_timeout`; no other API
  call is sent while KiCad is busy) (precedent `cc/kicad_tools/checks.py`, `* 4`).
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
                                         filled: tuple[tuple[Point, ...], ...] = ()   # filled polygons' outer outlines (M11 plane voids); excluded from board_hash
                                         filled_holes: tuple[tuple[tuple[Point, ...], ...], ...] = ()   # P3a: holes per filled polygon, index-aligned with filled; excluded from board_hash
                                         filled_layers: tuple[str, ...] = ()   # P3a: copper layer of each filled polygon, index-aligned with filled; excluded from board_hash
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
    dru_marker: str = ""                         # "" normally; "oversize:<st_size>:<st_mtime_ns>" when .kicad_dru > 1 MiB (dru_text then "") — in board_hash
    warnings: tuple[str, ...] = ()               # load warnings (e.g. .kicad_dru not a regular file / oversize); in no hash
    texts: tuple[TextItem, ...] = ()             # board-level texts/text boxes (As built P1a); in board_hash (sorted, KIID-free)
    def part(self, ref: str) -> Part: ...           # KeyError if missing
    def placed(self, part: Part) -> bool: ...       # courtyard fully inside outline; False if outline.kind == "none"

def placement_hash(m: BoardModel) -> str   # sha256 hex of canonical JSON: sorted (ref, round(x,2), round(y,2), round(rot % 360,1), side, locked) + outline (rounded 0.01) + rule areas (name, layers, keepout, outline rounded 0.01)
def board_hash(m: BoardModel) -> str       # sha256 hex: parts incl. lib_id/value/texts, tracks, arcs, vias (rounded 0.001, widths, locked), zones minus fill (outline, net, layers, priority, settings), graphics, rules, net classes + assignments, dru_text (newlines normalised), dru_marker, copper_layers, layer_types, stackup. KIID-independent.
```

`TextItem(kind, text, layer, at, size, thickness, visible)` and `GraphicItem(layer, kind, points,
width)` are pure too. `cc/board/geom.py` (P1a): `octi_dist(a, b) -> float`,
`polygon_area(pts)`, `point_in_polygon(p, pts)`, `polygon_inside(inner, outer) -> bool`,
`polygons_overlap(a, b) -> bool` (SAT for convex, edge test otherwise), `convex_hull(pts)`,
`rect_poly(x, y, w, h, r=0.0)`; P3a appends B-C5.

### B-C3. KiCad layer — `cc/kicad_tools/board/{read,api}.py` (P1a)

```python
# read.py
def load_board_model(h: KicadHandle, project: Project) -> BoardModel   # sync; run via session.call; one API read; dru_text read from <project>.kicad_dru if present: lstat → regular file only (symlink/FIFO/dir → absent + model warning), os.open(O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_NOCTTY) + fstat regular (as cc/design_docs/paths.py _READ_FLAGS), cap DRU_MAX_BYTES = 1 MiB (bigger → dru_text "", dru_marker set, warning)
def read_dru(path: Path) -> tuple[str, str, tuple[str, ...]]           # (dru_text, dru_marker, warnings); pure, no kipy (P1a)
async def board_model(ctx: ToolContext) -> BoardModel                   # session.call(lambda h: load_board_model(h, ctx.project))
def require_board(ctx: ToolContext) -> None                             # raises StructuredToolError(code="no_board") when "pcb" not open

# api.py — raw protobuf for G3/G5, each sync and called inside session.call
@dataclass(frozen=True) class DrcMarker: type: str; severity: Literal["error","warning","exclusion"]
                                         text: str; kiids: tuple[str, ...]; pos: Point | None
@dataclass(frozen=True) class DrcResult: markers: tuple[DrcMarker, ...]; unconnected: int; seconds: float
                                         # unconnected = number of unconnected-items markers (DrcTaskResult has no count at the pin)
def run_drc_task(h: KicadHandle, *, timeout_s: float = 60.0) -> DrcResult    # StartDrcTask → GetTaskStatus → GetDrcTaskResult
class DrcTimeout(StructuredToolError): ...                                   # code "drc_timeout"; still TS_RUNNING at timeout_s (no cancel at the pin: the next call waits ≤ 30 s for it within its own budget, P1a)
DRC_STALE_GRACE_S = 30.0
def drc_call_timeout(timeout_s: float) -> float                              # timeout_s + DRC_STALE_GRACE_S + 15: the session.call timeout around run_drc_task (B-C4 rule)
# The timed-out task lives on the SESSION, not the handle (session.call builds a fresh KicadHandle per call):
# KicadSession.pending_drc: tuple[int, str] | None = (generation, task id); KicadHandle(kicad, open_docs, *, generation=0, drc_slot=None)
# exposes it as h.drc_slot.get()/set(); _kill()/start clear it; run_drc_task ignores an entry from another generation (P1a)
REFILL_BUSY_GRACE_S = 30.0; REFILL_MIN_S = 20.0; REFILL_TIMEOUT_S = 60.0   # (REFILL_TIMEOUT_S moved here from P6b/P7: one definition)
def refill(h: KicadHandle, *, timeout_s: float) -> None                      # THE only zone refill (P1a): board.refill_zones(block=True, max_poll_seconds=timeout_s), then busy probe h.kicad.ping() (kipy Ping → Empty): returns = idle; ApiError code AS_BUSY or kipy ConnectionError = busy → poll ping every 0.5 s ≤ REFILL_BUSY_GRACE_S; still busy → RefillTimeout. Sends nothing but Ping while busy
class RefillTimeout(StructuredToolError): ...                                # code "refill_timeout"; details {timeout_s, grace_s}; sets the session's busy slot (generation) so the next session.call first waits for idle (P1a)
def refill_call_timeout(timeout_s: float) -> float                           # timeout_s + REFILL_BUSY_GRACE_S + 15: the session.call timeout around refill (B-C4 rule)
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
    async def call(self, fn: Callable[[KicadHandle], T], *, timeout: float | None = None) -> T: ...  # session.call (As built P1a: timeout passed through)
    async def drc(self, *, timeout_s: float = 60.0) -> DrcResult: ... # session.call(lambda h: api.run_drc_task(h, timeout_s=timeout_s), timeout=drc_call_timeout(timeout_s)); deadline-bound callers pass min(60, remaining)
    async def refill(self, *, timeout_s: float = REFILL_TIMEOUT_S) -> None: ...  # session.call(lambda h: api.refill(h, timeout_s=timeout_s), timeout=refill_call_timeout(timeout_s)); the only way stages refill zones
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
    report: dict[str, Any] = field(default_factory=dict)   # As built (P1a): stages fill it; returned as data (+ step_report)
    temp_dirs: list[Path] = field(default_factory=list)   # scratch dirs owned by this change set (P6b fill-<uuid>); the engine rmtree's every one still present in its finally (shielded), on success, failure and cancel
async def apply_board_change_set(ctx: ToolContext, cs: BoardChangeSet) -> ToolResult
# runs inside ctx.session.applying(); takes the compensation snapshot first; one commit per
# stage with commit=True (commit=False stages open their own commits); drop_commit on error
# inside a stage; on any error after the first stage STARTED → compensate() (+ stage undo); verify after the last stage; result changed_docs={"pcb"}, data =
# cs.report + {"step_report": ...}; records one change-log entry (kind, sheet_path="", counts).
```

**Every long KiCad call passes an explicit timeout.** `session.call` without `timeout=` uses
`settings.kicad_api_timeout_s` (30 s, `cc/config.py`) and restarts the api-server on expiry
(`cc/kicad_session/session.py` `call`). So every board `session.call` that can exceed it
passes `timeout=` explicitly (precedent: `cc/kicad_tools/checks.py` passes
`kicad_api_timeout_s * 4`): DRC (`BoardStageContext.drc`, every direct `run_drc_task`) →
`drc_call_timeout(timeout_s)` = `timeout_s + 30 + 15` (incl. the 30 s stale-task grace);
every zone refill (P6b, P7) goes through `api.refill(h, timeout_s=t)` (never a bare
`refill_zones`) → `refill_call_timeout(t)` = `t + 30 + 15` (incl. the 30 s busy grace).
Deadline-bound refills use `t = min(REFILL_TIMEOUT_S, remaining)` with a floor: remaining <
`REFILL_MIN_S` (20 s) → the refill step is skipped and reported as `timeout` instead of
starting a too-short refill. P1a tests that `drc` and `refill` pass the larger timeouts.

`cc/kicad_tools/edit/engine.py::apply_change_set` refuses `BoardChangeSet` with `TypeError` (as it
refuses `FootprintChangeSet`). Two-phase board tools use `edit_spec(..., prepare, apply)` where
`apply = lambda ctx, cs: apply_board_change_set(ctx, cs)`; `sdk_tools._apply` gets no new branch.
Board creation (`creates_board`) calls `ctx.reload_project() -> tuple[Project, KicadSession,
int]` (the `int` = the new session's `generation` captured at reload time) and records it in
`ctx.reloaded_generation: int | None`; `_two_phase_steps` re-baselines its restart check to
that value (call 13).

`cc/kicad_tools/board/compensate.py` (P1a):

```python
@dataclass(frozen=True) class CopperSnapshot:          # "ids": kiids only; "full": + item data
    track_ids: frozenset[str]; via_ids: frozenset[str]; zone_ids: frozenset[str]
    footprint_ids: frozenset[str]; group_ids: frozenset[str]; graphic_ids: frozenset[str]
    positions: tuple[tuple[str, float, float, float, Side, bool], ...]   # (kiid, x, y, rot, side, locked)
    tracks: tuple[Track | ArcTrack, ...] = (); vias: tuple[Via, ...] = ()
    raw: ... = ()   # As built (P1a): stored protobufs ("full"), so re-created vias keep padstack/type; re-created items keep their KIIDs
def take_snapshot(h: KicadHandle, mode: Literal["ids", "full"]) -> CopperSnapshot
def compensate(h: KicadHandle, snap: CopperSnapshot) -> list[str]   # removes items not in snap, restores positions; "full" also recreates missing tracks/vias (data equal); returns leftovers (empty = clean)
```

`cc/kicad_session/session.py` (P1a): `commit(self, label, *, doc: DocKind = "sch", dry_run=False)`;
`self.modified_docs: set[DocKind]` (pushed commit adds `doc`, save of `doc` removes it); the old
`modified` becomes a property `"sch" in self.modified_docs`; `self.pending_drc: tuple[int, str] |
None` (the timed-out DRC task, B-C3; cleared by `_kill()` / every start) reaches the worker
thread through `KicadHandle(..., generation=, drc_slot=)`.

`cc/board/worker.py` (P1a): `async def run_cpu(fn: Callable[..., T], *args: Any, timeout_s: float,
wait_s: float | None = None, **kwargs: Any) -> T` (keyword arguments other than `timeout_s` /
`wait_s` pass through to `fn`; `wait_s` = maximum time queued for the lock, exceeded →
`board_compute_timeout` without submitting; deadline-bound callers pass their remaining time) — `ProcessPoolExecutor(max_workers=1,
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
BOARD_CLI_LOCK = asyncio.Semaphore(1)   # process-wide; every board kicad-cli call runs inside it with an explicit timeout, like footprint_render._render_lock:
                                        # svg 60 s, 3d render 120 s (P1c), parity DRC 120 s (P3b), fab gerbers 120 s / drill 60 s / pos 60 s / bom 60 s (P8, lock per command),
                                        # step/glb export 180 s (P8 pcb_export_3d, P10 with models)
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
this project newer than the **oldest** evidence row used (min over the required render/check
timestamps) — the ABA guard, mirroring `fp_approve`'s `min(rendered_at, checked_at) <=
written_at` check in `cc/kicad_tools/footprints/approve.py`), 5 a later human message, 6 the quote in it,
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
def build_grid(model: BoardModel, req: RouteRequest, *, pitch: float = 0.05, margin: float = 0.0,
               window_mm: float | None = None) -> ObstacleGrid   # None → WINDOW_MM (6.0); solve/solve_pair retry once with 2 × WINDOW_MM
def route(grid: ObstacleGrid, req: RouteRequest, *, max_expansions: int = 2_000_000) -> RoutePath | None
def route_pair(model, p: RouteRequest, n: RouteRequest, gap: float) -> tuple[RoutePath, RoutePath] | None
def solve(model: BoardModel, req: RouteRequest, *, margin: float = 0.0, max_expansions: int = 2_000_000) -> RoutePath | None   # run_cpu entry point; owns the one window-doubling retry (route never retries)
def solve_pair(model, p, n, gap, *, margin: float = 0.0) -> tuple[RoutePath, RoutePath] | None                                 # run_cpu entry point
def pair_coupling(a: RoutePath, b: RoutePath) -> dict[str, float]   # keys length_mismatch, gap_min, gap_max (centre-to-centre over the coupled stretch; M12 reporting)
def prune_model(model: BoardModel, req: RouteRequest | tuple[RouteRequest, RouteRequest]) -> BoardModel   # only items intersecting the retry window; callers pass it to solve/solve_pair
# cc/kicad_tools/board/route.py (P5b), reused by P6b rip-and-reroute and ladder rung (c):
async def route_connections(sctx: BoardStageContext, plans: Sequence[ConnPlan], *, lock: bool,
                            deadline: float | None = None) -> RouteOutcome   # deadline = time.monotonic() value; checked before every margin attempt; connections not started by then → failed "timeout"; each run_cpu gets timeout_s=min(30, remaining), wait_s=remaining, each DRC min(60, remaining); DrcTimeout → that connection failed "timeout" (its items removed), others kept; worst case deadline + one DRC (≤ 60 s)
#   RouteOutcome(routed: tuple[str, ...], failed: tuple[tuple[str, str], ...]); connection key "{net}:{refA}.{padA}-{refB}.{padB}"
#   failure reasons: no_path, too_large, timeout, drc_rejected: <text>
# chamfer.py (P6a)
@dataclass(frozen=True) class PostResult: replace: tuple[tuple[str, tuple[Track, ...]], ...]  # (old kiid, new tracks)
                                          # (kiid, ()) = delete (merges / duplicates)
                                          rip: tuple[str, ...]   # connection keys "{net}:{refA}.{padA}-{refB}.{padB}"
                                          reasons: tuple[tuple[str, str], ...] = ()   # (key, reason); P6a adds reason "disconnected" (safety net)
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
`timeout`, `freerouting_error`, `io_timeout`}), `board_compute_timeout`, `refill_timeout`
(`RefillTimeout`, B-C3: KiCad still busy filling after `timeout_s` + 30 s grace),
`drc_rejected`, `drc_timeout` (`DrcTimeout`, B-C3; deadline-bound callers map it per
connection/stage instead of failing the call — P5b, P6b), `verification_failed` (existing, `cc/kicad_tools/edit/engine.py`), `output_path_unsafe`, `render_failed`, `export_failed`,
`session_type_unavailable` (HTTP 400), and the existing `document_inconsistent`, `read_only`,
`save_failed`, `plan_required`, `plan_step_required`. Change-log kinds (`BOARD_KINDS`,
`cc/changes/repo.py` + `highlight.ts`): `board_` + tool name without `pcb_`, for every
`PCB_WRITES` tool that changes documents, plus `board_approve`.

---

## As built

(Each phase adds its section here in its own PR.)

### As built (P3b)

**Measured / recorded** (`tests/kicad/board/test_check.py::test_full_runs_parity_and_m14`,
`record_property`; `sp4_synth` after sync, outline 60 × 45 mm, rules `jlcpcb-4l`, 26 parts in
the staging area, unrouted, local kicad-base image)
- `pcb_check quick` ≈ 0.50 s, `pcb_check full` ≈ 9.0 s (copy + `kicad-cli pcb drc
  --schematic-parity` under `BOARD_CLI_LOCK`, 120 s limit).

**Guards** (`cc/kicad_tools/board/approve.py`, each `approval_not_allowed` with `details.guard`,
evaluated in this order): 1 hash of the stage (`details.other_stage = true` for the other
stage's current hash); 2 evidence in this conversation for the hash (`details.missing` lists
the missing pieces; also refused without a conversation or database); 3 evidence passes
(placement: newest check for the hash has `m1_pass = 1`; board: newest full check
`hard_failures = 0`); **4 no write after evidence** (no change-log row of a `BOARD_KINDS` kind
other than `board_approve` for the project with `created_at >=` the OLDEST evidence row used;
ties refuse); 5 a user-typed message of this run later than the NEWEST evidence row; 6 the
quote in one of them; 7 no running plan. `apply` re-reads the board, then inside one
`db.transaction()` re-checks guards 1 and 4 (`evidence_since` carried in the change set
summary), inserts the checkpoint row and the `board_approve` change row
(`ChangeRepo.add_many_in_transaction`), then notifies `on_changes_recorded`.

**Contract deviations / additions**
- `checkpoint_state` is **async** (`await checkpoint_state(db, project, model)`; the DB is
  async). `CheckpointState` fields: `approved`, `stale`, `hash`, `approved_at`,
  `conversation_id`. `require_*_approved` details: `placement_not_approved {stale,
  placement_hash}`, `not_approved {reason ∈ missing|stale|check_failed, board_hash}`.
- `cc/approvals.py`: `human_messages_after(ctx, since_iso: str | None) -> list[MessageRow]`
  (`created_at > since_iso`), `plan_running(ctx) -> bool`, `normalise`, `quote_in_message`,
  `MIN_QUOTE_WORDS`/`MIN_QUOTE_CHARS`; `footprints/approve.py` imports them (re-exports
  `normalise`, `quote_in_message`); footprint tests unchanged.
- `cc/kicad_tools/board/checkpoints.py` also exports `board_written_since(db, project_id,
  since_iso) -> bool` (one bounded SQL query over `changes`, kinds `WRITE_KINDS = BOARD_KINDS −
  {board_approve}`) — guard 4 and the evidence recorders use it instead of loading
  `ChangeRepo.since` rows.
- `BoardReviewRepo.last_check(project_id, board_file, conversation_id)` (for `pcb_status`).
- `record_unless_written(db, project_id, since_iso, record)` (checkpoints.py): the evidence
  recorders run "no board write logged since the model read" and the insert in ONE
  transaction, so a write logged afterwards always has `created_at >=` the evidence row
  (ChangeRepo now stamps `created_at` inside the transaction) and guard 4 sees it.
- `require_board_approved` uses the NEWEST full check of the board hash (must have 0 hard
  failures), like guard 3 — stricter than "a passing full check exists".
- Evidence recording: `pcb_render` records a `render` row only for a whole-board render
  (`region` absent: a zoomed render shows part of the board) and only when no board write was
  logged since its model read; `pcb_check` records nothing when the hashes before/after differ
  **or** a board write was logged meanwhile (`stale: true` in the result).
- `pcb_check` result: `level`, `errors` / `warnings` (each `{type, severity, text, refs, x,
  y}`, warnings + `accepted`; ≤ 50 + `<key>_omitted`), `parity` (null for quick), the
  `pcb_metrics` fields (`stage`, `metrics`, `hard_failures`, `reviews`, both hashes),
  `verdict`, `stale`, `seconds`, full adds `cli_crosscheck {only_cli, only_api}`.
- **`cli_crosscheck` compares per normalised DRC type (count difference), not by KIID set:**
  pad KIIDs differ between the live board and the copy kicad-cli reads (probe on synth: 162
  identical non-unconnected violations, all pad-KIID sets different), so a KIID comparison
  was pure noise.
- `pcb_check full` copy dir: `.kicad-agent/board/check/<uuid>/` with `save_copy("sch", …,
  save_modified=False)` (an unsaved schematic → `schematic_unsaved`, never a save), the
  board copy, byte copies of `.kicad_pro`, `fp-lib-table`, `sym-lib-table` (regular files
  only, `O_NOFOLLOW`, ≤ 16 MiB); the JSON report is read only as a regular file ≤ 32 MiB;
  timeout, no report, a bad report or a copy failure → `drc_rejected`. Without a schematic →
  `drc_rejected`.
- `pcb_approve` also calls `require_board_tools` (`board_tools_disabled`) and `require_board`.
- `cc/editing/gate.py`: `PLAN_EXEMPT_TOOLS = frozenset({"fp_approve", "pcb_approve"})`.

**Residuals for later phases**
- M3 acceptance maps marker KIIDs to refs via **part** KIIDs only (P3a): on `sp4_synth` every
  warning names pads or footprint fields (`copper_outside_board`, `silk_edge_clearance`), so
  such warnings can never be `accepted` through `board.md`. `test_accepted_warning_marked`
  therefore adds one part-level warning to the real DRC result. **P7 (`pcb_tidy_silk`) / P9a**
  should map pad and field KIIDs to their footprint (needs pad/text KIIDs in the model, P1a
  file).
- The board approval is tested with evidence rows recorded through the repository (a full
  check without hard failures needs a routed board; P9a's golden run covers the real path).
- Board writes log their change row through `record_applied`, which swallows insert errors
  (P1a engine): a write whose change row failed is invisible to guard 4 (the hash guard still
  applies). Never seen; inherited.
- Guard 4 and the evidence recorders order events by wall-clock `created_at` (ms, UTC);
  a backward clock step between the evidence and a later write could hide that write from
  guard 4 (the hash guard 1 still applies). Comparing change-log ids instead would need an
  extra column; left as is.
- `pcb_check full` refuses (`drc_rejected`) when kicad-cli reports "Failed to fetch
  schematic netlist" (it exits 0 with an empty parity list then); the JSON report is capped
  at 8 MiB.
- **P5b/P6b/P7** call `require_placement_approved(ctx, model)`, **P8** `require_board_approved`
  (both async, fail closed without a database).

### As built (P6a)

**Lock values** (`docker/freerouting.lock`, checked 2026-11-25)
- Freerouting 2.5.0 JAR: `https://github.com/freerouting/freerouting/releases/download/v2.5.0/freerouting-2.5.0.jar`,
  SHA-256 `f6f51bb02245e8e717f9359bd260cc9c5c0b1bc0acc8b7cb2cd5b8ffeb5de3c7`, 64,599,107 B.
  Freerouting publishes no checksum file for the JAR: the hash was computed from a download (it
  equals GitHub's release-asset digest).
- Temurin **25.0.5.1+2** JRE (newest 25.0.x GA, linux x64 hotspot):
  `https://github.com/adoptium/temurin25-binaries/releases/download/jdk-25.0.5.1%2B2/OpenJDK25U-jre_x64_linux_hotspot_25.0.5.1_2.tar.gz`,
  SHA-256 `2edef9e4f81ec5ee426d023336d4cfbd7b4644e99884e7a73a6a867b70b17acf`, 61,751,939 B (the
  release's `.sha256.txt` and a recomputed hash agree). `java -version`: `openjdk version "25.0.5.1" 2026-11-17 LTS`.
- Installed tree: `jre/` 200,078,669 B + JAR; manifest 123 entries; real HTTPS fetch ≈ 10 s, second run `up to date`.

**Measured / recorded**
- App image size (`docker image inspect .Size`, same base and context; this Docker host uses the
  containerd snapshotter, so `.Size` is the compressed content size): 600,684,570 B → 723,458,259 B,
  **+122.8 MB** compressed; `/opt/freerouting` in the image is 264.7 MB uncompressed (spec G6
  expected ≈ +190 MB).
- `prlimit` (`/usr/bin/prlimit`) and `unshare` (`/usr/bin/unshare`) exist in kicad-base and the app
  image; `unshare -r -n -- true` as uid 1000 under default `docker run` fails ("Operation not
  permitted") → CI and the default PROD container run in mode **options+no-dns** (the runtime test
  prints and records `freerouting_network_mode`).
- ecc83 DSN (API export), `passes=5`: ≈ 6–10 s wall (JVM start + DSN load ≈ 1.7 s before routing
  starts, so `test_timeout_kills` with `timeout_s=1` always kills a live JVM). Same run inside
  `copperchat:p6a` as uid 1000 through `find_runtime(Settings())`: rc 0, 10.0 s.
- Passes option confirmed: `--router.autorouter.max_passes=<n>` (`-mp` / `--router.max_passes` are
  deprecated aliases in 2.5.0). Every option we pass is a known 2.5.0 settings name — an unknown
  one makes Freerouting log `Unknown settings property: <name>`, which `test_jvm_starts_under_limits`
  asserts is absent.

**No-network design (deviation: stronger than planned)**
- Freerouting 2.5.0 starts an **unconditional version check** (`app.freerouting.util.VersionChecker`
  → `https://api.github.com/repos/freerouting/freerouting/releases/latest`, started in
  `Freerouting.main` with no setting to switch it off); with analytics left on it also contacts
  `api.freerouting.app`. The planned proxy blackhole at 127.0.0.1:9 would still have opened a local
  TCP socket for it, which the planned fd-scan test rejects (found by that test: 2 CONNECTs to
  `api.github.com` at the listener).
- Built: `-Djdk.net.hosts.file=<workdir>/.hosts-empty` (an empty file: the JVM's only name source,
  so no host name resolves and no DNS query is sent) and `PROXY_BLACKHOLE` with host
  `proxy.invalid` (RFC 2606, never resolves) and port 9 (`proxy_blackhole(port)` helper, the
  runner's `proxy_port` keyword for the test). Result: the version check fails before any socket
  exists; `test_no_network_attempt` (listener on the proxy port + fd scan of every thread every
  50 ms): 0 connections, no inet socket. New control `test_detector_sees_unhardened_version_check`
  (same argv without the hosts file, proxy at a local listener) proves the method sees the version
  check's `CONNECT api.github.com:443`.
- `NO_NETWORK_OPTIONS` (exact): `--profile.allow_telemetry=false`, `--profile.allow_contact=false`,
  `--usage_and_diagnostic_data.disable_analytics=true`, `--api_server.enabled=false`,
  `--mcp_server.enabled=false` (plus `--gui.enabled=false`). Freerouting does **not** persist CLI
  options into its settings file, so the runner also pre-seeds it: `<workdir>/.config/freerouting/freerouting.json`
  (`$HOME/.config/freerouting/`, HOME = workdir; `SETTINGS_SEED` with `version: "2.5.0"` and a nil
  profile id — a file without `version` is reset to defaults, an invalid id crashes) — the file
  `test_settings_file_has_analytics_off` reads. Freerouting's own log goes to
  `<workdir>/.local/state/freerouting/logs/freerouting.log` besides our `<workdir>/freerouting.log`.

**Contract deviations / additions**
- B-C13: `FreeroutingRuntime.prlimit: Path | None = None` (the `prlimit` path from `shutil.which`
  at `find_runtime` time; `None` only with `ALLOW_PRLIMIT_FALLBACK = True`, default False — the
  `resource.prlimit` fallback is implemented and unit-tested but never active); `run_freerouting(...,
  proxy_port=PROXY_PORT)` keyword (tests only). `find_runtime` caches per (home, java mtime, jar
  mtime, prlimit path). `timeout_s` ≤ 0 / NaN / inf → `ValueError` (P6b validates its schema first);
  > 600 → 600; `passes` clamped 1..100.
- `JAVA_OPTS` gains `-XX:-UsePerfData` (no `/tmp/hsperfdata_*`) and `-XX:+ExitOnOutOfMemoryError`;
  the command adds `-Djava.io.tmpdir=<workdir>` next to `-Duser.home`; `stdin` is `/dev/null`.
- A non-`CancelledError` exception while waiting (e.g. the fallback `prlimit` failing) also kills and
  reaps the group before re-raising.
- Fetch script: the lock file is **parsed**, not `source`d (only the five keys; values validated:
  version, `https://`/`file://` URL, 64-hex hashes, no shell metacharacters), so `FREEROUTING_LOCK`
  cannot inject code; the Dockerfile stage sets `FREEROUTING_LOCK=/tmp/fr/freerouting.lock`. The
  JRE is extracted with Python `tarfile` (`filter="data"`, single top directory stripped; absolute
  paths, `..` and links leaving the tree refused). The tree check also refuses regular files under
  `jre/` not in the manifest and requires the manifest's JAR line to equal the lock hash. Extra
  tests `test_extra_file_in_tree_reinstalled`, `test_unsafe_tar_member_refused`. `.dockerignore`
  gains `.cache` (a local CI-style cache is not sent as build context).
- `postprocess` (B-C12) as specified; module docstring documents it. Rip attribution: a defect on
  touched tracks rips every connection whose shortest routed path (the M5 copper graph) uses one of
  those tracks; none → every connection of the net using that layer; none → every connection of the
  net. Off-pad T-junctions rip with reason `ninety_unfixable` (the reason list has no
  `t_junction`). Connections shorter than `M5_MIN_MM` skip the detour checks (as M5); ground nets
  via `classify._ground_nets`. A polyline with any unfixable corner is left unchanged and ripped —
  never partly chamfered; adjacent corners sharing a leg are solved jointly; a corner with only one
  leg in `kiids` is ripped. Diagonals placed earlier count as obstacles. `postprocess` imports
  private helpers of `geom` / `metrics` / `classify` (no change to those files). 1,500 nets / 6,000
  tracks / 4,500 corners ≈ 2.75 s. Property test: 200 seeded random rectilinear polylines → every
  non-ripped polyline has M4 = M7 = 0 (`analyse_bends`, incl. `off_angle`).
- Chamfer test geometry adjusted where the plan's literal shape did not exercise the rule:
  `test_short_leg_rips` uses 0.15 mm legs and Z/U shapes with a 0.2 mm middle leg (a plain 0.2 mm L
  at w = 0.2 is chamferable with c = 0.25); the micro-jog / short-chamfer / valid-chamfer tests put
  the 45° pair between two other bends (a pair at both pad ends is exempt as pad entries in
  `analyse_bends`); `test_chamfer_shrinks_near_obstacle` asserts c = 0.4 (= 2w) for a 0.5 × 0.5 mm
  other-net pad 0.52 mm from the vertex.
- `cc/board/router/__init__.py` created empty (P5a not merged yet); on rebase P5a's version wins.

**Review round 1 (PR #104) changes**
- Chamfer output is on the KiCad 1 nm grid: the corner vertex is rounded to nm, each axis step
  of the offset rounded **up** to whole nm (diagonal exactly 45°, length ≥ c); every feasibility
  and `analyse_bends` check runs on the snapped points (before: c = CHAMFER_MIN could become a
  too-short chamfer after KiCad's rounding). Property test: 500 seeded cases incl. diagonal legs,
  other-net pads near corners, untouched tracks, output rounded to nm.
- Step-3 attribution diffs against `analyse_tracks` of the untouched copper alone (a defect whose
  reported midpoint lies on an untouched track is still ours). Same-net arc ends and other track
  ends are never moved (a 90° there → `ninety_unfixable`); a connection routed before and not
  after `postprocess` is ripped with the new reason **`disconnected`** (B-C12 reason list).
- Runner: `prlimit … --core=0:0` (+ `RLIMIT_CORE` in the fallback) and `-XX:-CreateCoredumpOnCrash`;
  `extra_args` come before `--gui.enabled=false` + `NO_NETWORK_OPTIONS`, and an extra arg with one
  of those keys (`LOCKED_OPTION_KEYS`) raises `ValueError`; `MAX_CONCURRENT_RUNS = 2` (one
  semaphore per event loop; the slot wait counts against `timeout_s`, no slot in time →
  `returncode -1, timed_out`); `unshare_path()` (absolute path, probed via `asyncio.to_thread`);
  a failed `find_runtime` probe is cached 60 s only (`FAILURE_TTL_S`). `find_runtime` stays sync:
  async callers (P6b) call it via `asyncio.to_thread`. An external SIGKILL still counts as
  `timed_out` (plan rule; an OOM kill is reported as a timeout).
- CI caches only `.cache/freerouting/.dl` (the hash-verified downloads); the installed tree is
  re-extracted on every CI run, so a poisoned cache cannot carry a modified JVM. The fetch script
  clears its temp paths before use (never writes through a pre-existing symlink) and treats a
  symlinked `.lock` / manifest / JAR / `jre` as not installed.

**Review rounds 2–4 (PR #104) changes**
- Chamfer clearance per obstacle: `max(own net clearance, obstacle net's class clearance,
  min_clearance)`. Runner makes `freerouting_home`, `workdir`, `dsn`, `ses` absolute and removes a
  stale `.ses` before a run.
- `postprocess` performance: step-3 attribution, baseline suppression, merge outer ends and pad-end
  lookups bucketed (`geom._Grid` / `_Near`); merge joints keyed per (net, layer). 4,000 defective
  tracks ≈ 1.1 s, 1,600-pad comb ≈ 1.5 s (rest is `metrics._mst`, all pad pairs — P6b/P9a may
  bucket it).
- Sub-`_JOIN_TOL` (2 µm) touched pieces / gaps are closed at one nm point in step 1 (untouched,
  arc and via ends never move). **Safety net (owner rule):** step 3 runs `analyse_bends` on every
  nm-rounded output polyline that contains touched copper and rips every failure not in the
  untouched baseline; touched ends within 2 µm of another end but not on it rip `non_octilinear`.
  A float straight piece of exactly 0.25 mm between a same-sense pair rips `short_chamfer` (it falls
  below the minimum after nm rounding). Property test: 4,000 seeds in the suite; a one-off
  10,000-seed run had 0 failures.

**Residuals for later phases**
- `analyse_bends` (B-C5) exempts the first and last bend of a pad-ended polyline from the
  micro-jog / short-chamfer checks, so a defective 45° pair directly at both pads is never
  flagged by M7 nor by `postprocess` (P6b / owner: decide whether pad-entry pairs need a check).
- A Freerouting version bump must re-check the network behaviour (`test_no_network_attempt` +
  the control) and `NO_NETWORK_OPTIONS` names (`Unknown settings property` guard).
- `postprocess` relies on private helpers of `geom` / `metrics` / `classify`; a refactor there
  must keep them or move them public.

### As built (P3a)

**Measured / recorded**
- `pcb_metrics` on `sp4_synth` (`synth_board`, 26 parts, no outline, unrouted; includes the
  API DRC task): **median 0.273 s warm** (5 calls: 0.256 / 0.252 / 0.273 / 0.327 / 0.303 s),
  local kicad-base image; `test_metrics_budget` records `pcb_metrics_s` (bound 2.0 s).
- `compute_metrics` in-process on a seeded 200-part board (1,791 segments, 282 vias, In1.Cu
  GND plane with an antipad per via, `jlcpcb-4l`): 0.32–0.33 s (bends ≈ 0.15 s, M10 0.08–0.2 s).
  On 2,400 two-pad nets / 19,200 segments / 600 vias (`test_timing_2400_nets_19k_segments`,
  bound 3 s): 1.36 s (was 4.4 s before per-net grouping of tracks/arcs/vias and the bucketed
  part/via lookup for the bends sub-models).
  `classify` on a 200-part / ~284-net random circuit ≈ 7 ms; 1,000 decoupling caps × 4,000
  anchor supply pins 0.06 s (was 7.9 s; keys precomputed, grid ring search). `_Nearest`
  starts the ring search at the pads' cell bounds and visits only in-bounds cells (≤ 64 pads:
  linear scan): 100 queries ~1,000 mm from every pad 0.016 s (was 11.8 s).
- Review round 2 (bucketed geometry): M4/M7 on 2,000 GND stubs (4,000 F.Cu GND segments, 4,000
  pads, 2,000 vias; `test_timing_gnd_stubs_4000_segments_m4_m7`, bound 2 s) 0.15 s (was ≈ 23 s):
  `geom._graph` buckets its splitters, pads and vias in a uniform grid (`geom._Grid`, reused by
  `metrics`) and is built once per (net, layer) for both polylines and the node checks. M10 on
  200 blocks / 3,801 parts 0.012 s (was 0.86 s; courtyard bboxes bucketed once).
- No review threshold tuned; `HARD` / `REVIEW` are the spec §12 values (`test_hard_thresholds_verbatim`).
- Review round 4 (5,000 parts): M1/M10 share one `placed` per part (`_Ctx.placed`, outline
  polygon built once in `_placer`, identical to `BoardModel.placed`; a courtyard strictly inside
  the rounded rect's two inner rectangles short-cuts the polygon test): 5,000 parts on an r = 3
  outline, M1 + M10 (`test_m1_m10_timing_5000_parts_rounded_outline`, bound 3 s) 1.6 s (was
  34 s). `bends()` shares one pad/via index of the whole board (`geom.TrackIndex`, optional
  `analyse_tracks(model, index=...)`) across the per-net runs, results equal to the per-net
  whole-board semantics (`test_bends_per_net_equal_whole_board_semantics`): 5,000 parts +
  250 board-spanning nets (`test_bends_timing_5000_parts_long_nets`, bound 3 s) 0.49 s (was 3.5 s).

**Owner decisions applied (2026-11-25)** — "Provisional" items 1–3 marked resolved above.
`M1_EDGE_OVERHANG = True`: `placed_with_overhang(model, part, edge)` clips the courtyard to the
inner side of the assigned edge (outline bbox line; one-half-plane Sutherland–Hodgman) and
requires the rest inside the outline (rounded rect via `rect_poly`, "other" via the polygon).
M13's "overhanging it" uses the same clip. Both switch settings are tested.

**Contract deviations / additions**
- B-C2 (P1a files, minimal): `Zone.filled_holes` (holes per filled polygon, index-aligned with
  `filled`, read in `read.py::_zone`; not hashed) — the P1a residual "M11 needs holes" — and
  `Zone.filled_layers` (the copper layer of each filled polygon, index-aligned with `filled`,
  same `_layer()` names as `layers`; not hashed).
- `cc/board/jobs.py`: `analyse_job(model, doc, scope="all")` (scope filtered in the worker),
  `metrics_job(model, doc, drc, preset, only, parity=None) -> MetricsReport`, `strip_fills`
  (also clears `filled_holes` / `filled_layers`). Result serialisation in `analysis.py::metrics_data`.
- `preset_for(model, doc)` in `cc/kicad_tools/board/analysis.py` (B-C6 rule).
- `pcb_analyze` result: each list/mapping `K` ≤ 500 entries plus an int `K_omitted` (always
  present); scopes parts / nets / blocks / all; `board_md_errors` in every scope. Each block
  lists ≤ `MAX_BLOCK_REFS` (200) refs plus `refs_omitted` (always present); each
  `board_md_errors` string is cut to `MAX_ERROR_CHARS` (200) characters (boardmd quotes cells).
- Argument validation before `require_board`: an unknown / non-string `scope` (`pcb_analyze`)
  and an empty, non-list, unknown-id or duplicate `only` (`pcb_metrics`; schema `minItems: 1`)
  raise `StructuredToolError` code `invalid_argument`.
- `pcb_metrics` result: `stage, metrics[{id, value, verdict, hard, review, offenders[{what,
  refs, nets, at, value}], omitted}], hard_failures, reviews, placement_hash, board_hash,
  seconds`. The DRC task runs only when `only` includes M2 or M3 (or `only` is absent).
- DRC type normalisation (M3 ↔ `board.md` Checks): casefold, `drce_` prefix stripped, spaces →
  `_`, so API enum names (`DRCE_SILK_OVERLAP`, P1a lower-cases without `DRCET_`) and CLI JSON
  types (`silk_overlap`) match the same row. KIID → ref via part KIIDs only; a warning whose
  KIIDs resolve to no ref is never accepted.
- `board.md` parser: errors capped at 50 + one "<k> more errors omitted" line; tables are
  recognised by their header set in any section; `Preset:` / `Assembly: …; sides:` only in
  `## Summary`.
- `board.md` ref length: a Ref cell longer than 32 characters (refs `maxLength` 32) rejects the
  row with an error; Blocks Parts / Checks Items entries over 32 are dropped with an error each.
- M11 fill per layer counts the union: when ≥ 2 zones with polygons exactly on the layer have
  overlapping bboxes, their fill is the exact union area (`_union_area`, vertical slab sweep
  incl. edge crossings), else the plain sum; ratio clamped ≤ 1.0. `_PlaneVoids.void_at` and
  M14 `_excepted` (condition token set, regex fallback for non-word refs) are bucketed/indexed.
- Switcher bootstrap refinement (call 15) as planned: a 2-pad cap between SW and a net holding
  only the IC and that cap is tolerated; out caps by sheet, else strictly nearer (part-centre
  `octi_dist`) to the switcher than to the rival anchor.
- M14 `paste_missing`: a copper SMD pad also counts as pasted when a paste-only pad (F.Paste /
  B.Paste on that side, no copper layer) of the same part has its centre inside it (KiCad
  `Package_DFN_QFN` exposed pads get their paste from such sub-pads). Solder jumpers — ref
  `JPn` or library nickname `Jumper*` (case-insensitive) with no pasted pad at all — are exempt
  (KiCad SolderJumper pads are paste-free by design); everything else unchanged.
- M12 composite effective net classes ("Default,Power", as read stores a net in several
  classes): the widest `track_width` among the known constituents applies (KiCad takes each
  field from the highest-priority constituent; widest is the conservative choice for a
  minimum-width check); unknown constituents ignored, none known → skipped as before.
- Blocks: a leftover part first joins the block that already owns one of its signal-net
  neighbours (found on `sp4_synth`: D1 in the PA4 → R4 → D1 chain went to J1's block by a
  GND-count tie), then the "most shared nets" fallback.
- `primary_ground(cls, model)`: ground net with most pads, ties by name, None without one.
- M4 / B-C5: the direction change at a degree-2 node where polylines are split (width change
  / neck-down, same-layer via, closed-loop start) is classified too (90° / acute / off-angle
  fail, 45° counts as a bend) unless the node lies in a same-net pad; non-octilinear is
  tested per raw `Track` (before the ≤ 0.5° collinear merge), arcs as before.
- M4 counts each failing item: a 60° vertex between octilinear legs is impossible, so such a
  vertex also brings one non-octilinear leg → `test_m4_off_angle_counts` asserts value **2**
  (one `off_angle` + one `non_octilinear` offender), verdict fail (plan text said 1).
- M11 is per plane **layer** (spec §12), not per zone: the fill ratio sums the filled area of
  every ground zone on the layer, and one void structure is built from all their polygons on
  it (two touching zones are one plane: a track over the seam crosses no void; segments are
  cut at every fill / void boundary and each piece located). Offenders: `fill <layer>`,
  `void crossing: <net> over <layer> <zone> <i> hole <j>` / `outer`, `zone not filled`.
- M11 antipads: a hole whose boundary lies within the union of the discs (centre, via radius
  / plated pad half-diagonal + `ANTIPAD_MARGIN_MM` 1.0 mm) of the vias / plated pads near it
  **and whose area is at most those discs' total area** is their antipad (incl. merged
  antipads of close vias) / thermal-relief gap, not a void; a slot reaching beyond those
  discs (e.g. between two vias 6 mm apart), a removed island ringed by vias (area rule),
  empty holes and the band between the outline and the fills are voids. Each plane layer
  uses only its own polygons (`Zone.filled_layers`); without per-layer data a zone's polygons
  are all used and its fill area is divided by its layer count.
- Routed length graph (`_NetGraph`): tracks are also split at same-net pad centres on their
  layer (a track through several pads), and a via whose centre lies in a same-net copper pad
  joins it (via-in-pad).
- M8 with duplicate pad numbers: the same-number pad on the decoupling's supply net nearest
  the cap's supply pad (classify's choice). M10 without `blk:*` groups skips the anchor-less
  `misc` block.
- Supply-name rule (`_SUPPLY`) accepts decimal voltages (`+3.3V`, `1.8V`, `-2.5V`; `1V8` as
  before). The ground rule keeps `*GND*` (spec §9.1).
- M14: silk text bbox is axis-aligned (`TextItem` has no angle): `len × size_x` wide, `lines ×
  size_y` high; fiducials and test points (KiCad `TestPoint_Pad_*` have no paste by design) are exempt from
  "paste on every SMD pad"; board rules compared to the
  preset with a 1e-4 mm tolerance; a custom-rule exception needs every offender ref named in a
  `(condition …)` of `.kicad_dru`.
- M3 parity: `not_in_schematic` / `extra_footprint` markers whose refs are all mounting holes
  produce no count (holes placed by `pcb_set_outline`).
- `NetKind` `high_current` is never assigned (no generic rule yet).
- Test rig: `synth_board` (module scope, `loop_scope="module"`) yields a `KicadSession` on a
  copy of `sp4_synth` with a board built through the API (kicad-cli netlist → create → reopen
  → `import_netlist` → save); tests build `edit_ctx(synth_board)` / `board_ctx(...)`.

**Residuals for later phases**
- Golden `sp4_synth_classification.json` depends on where KiCad's import puts footprints
  (`Net-(U3-VBST)` is critical by the ≤ 10 mm two-pad rule; C1–C3 all serve U1 pin 5, the
  nearest of the two `+3V3` pins); stable over 3 runs. P2's sync may move parts → refresh with
  `UPDATE_GOLDENS=1` if its placement differs.
- On `sp4_synth` the ESD part U2 (6 pads) is an anchor with its own block, and the USB pair's
  anchor is J1 (most pads). The agent edits blocks (S3); P4 may want "protection next to its
  connector".
- M9 measures in-cap VIN pad → IC VIN pin only (spec also names the GND pins).
- Silk text rotation is not modelled (needs a `TextItem` angle; P7 `pcb_tidy_silk`).

### As built (P2)

**Probes / measured** (`tests/kicad/board/test_{sync,outline,rules}.py`)
- **Rules persistence (spec §6.2): persisted by the board save.** After `pcb_set_rules` and the
  adapter's `save("pcb")`, `.kicad_pro` has `board.design_settings.rules.min_clearance == 0.127`
  and the net class + pattern in `net_settings` (`test_rules_persist_to_kicad_pro`).
  `BoardChangeSet.saves_project` stays `False`; the engine has no project save.
- **Copper layer type (spec §10.3, call 9a): not settable** at the pin (`BoardStackupLayer.type`
  knows copper/dielectric/silk/mask/paste only; no command carries signal/power/mixed/jumper) —
  `test_probe_layer_type_power`, `record_property("layer_type_settable", False)`. **P6b uses the
  spec's fallbacks** (Freerouting CLI options, then DSN text only as the owner-D4 last resort).
- **`import_netlist(delete_extra_footprints=True)` spares the tool holes** (`not_in_schematic`,
  `MountingHole*`): after a re-sync they keep KIIDs and positions (`test_resync_keeps_tool_holes`);
  no snapshot/re-create path in the sync.
- Sync runtime on `sp4_synth` (board creation + dry run + import + staging, 26 parts): ≈ 8 s.

**Contract deviations / additions**
- `require_board_tools(ctx)` (`board_tools_disabled`) lives in `cc/kicad_tools/board/setup.py`;
  later board writes import it from there. `staging.py` also exports `natural_key` and
  `part_box` (courtyard box, else pads, else origin).
- `pcb_sync_from_schematic {dry_run: true}`: `prepare` returns a stage-less change set with
  `Logical(0)` and the report; `apply_sync` returns it as data with `changed_docs` empty (no
  engine run, no change-log row; the adapter's turn snapshot is still taken). Without a board
  the dry run lists the netlist's refs (`creates_board: true`).
- Only footprints whose **ref** is new go to the staging area: a footprint KiCad exchanges
  (changed `lib_id`, new KIID, same ref) keeps its place.
- Pad/pin mismatches: KiCad reports none as a warning, and the exported netlist already drops
  symbol pins without a pad. The only signal is the existing-board dry run's
  `Disconnect <ref> pin <n>.` info lines; the report lists them as warnings with the
  `fp_check` suggestion (deduplicated with the real import's warning lines). On a board created
  by the same call there is no such signal. KiCad also marks such a footprint "Do not place".
- `pcb_set_outline`: every deletion (older Edge.Cuts shapes, older tool holes, same-named rule
  areas) sits in a last stage "remove replaced items", after all additions were pushed — a
  failure before it leaves only additions (compensated), a failure in it drops its commit
  (`test_rerun_fail_after_restores`). The holes stage is `commit=False` with two own commits: a
  footprint placed with `place_footprint_from_library` cannot be updated in the same commit
  (`ISC_NONEXISTENT`), so ref/locked/attributes are set after the push. Extra
  `invalid_argument` checks: one of polygon/rect per keepout, unique keepout names,
  `2 × inset < min(w, h)`, hole positions inside the outline, keepout layers enabled
  (`all_copper` expanded). `verify` also fills `unplaced` and `placement_hash {before, after,
  changed}`. Tool-placed holes carry the description `copperchat:tool_hole` (plus the three
  attributes): the attributes alone cannot tell them from a hand-placed hole (stock
  MountingHole footprints already exclude BOM/position files, users tick "not in schematic"), so
  only marked holes are replaced; other symbol-less holes are kept and reported as `holes.kept`.
- `pcb_set_rules`: all four stages are `commit=False` (rules, classes, layer count, custom rules
  are no board items), each with an `undo`. Input is checked in `parse_rules_args` (unique
  names, via drill < diameter, min ≤ max, no control characters); `overrides` without `preset`
  are checked against the JLCPCB preset of the target layer count and only the overrides are
  written; a pattern belongs to one class, so giving it to a named class takes it from its
  previous class (also an unnamed one) — listed in `patterns_moved`; an existing `.kicad_dru`
  KiCad cannot parse is refused (`invalid_argument`), never
  overwritten. `setup.RULE_NAME` mirrors `specs_setup.NAME_PATTERN` (import cycle; a unit test
  keeps them equal). `stackup` → `stackup_unavailable` in a separate `plan_stackup` (P2b's hook).
- Test rig: `synth_rig` / `SynthRig` in `tests/kicad/board/conftest.py` (sp4_synth through a
  manager with board tools on, test conversation, a `HistoryService` with `restore_turn(ctx)`).
  `tests/unit/agent/test_profiles.py`: the Free "everything" assertions pass
  `board_tools=True` (the first registered `PCB_WRITES` tool is dropped by the default flag).

**Residuals for later phases**
- A failure of `pcb_set_outline`'s read-back `verify` (after the last stage pushed the
  deletions) raises `DocumentInconsistent`: compensation re-creates tracks/vias only
  (`"full"`), not graphics, footprints or zones. Never seen; undo via History.
- `read.py` `net_class_of` takes KiCad's combined class name ("Effective for net: +3V3") for a
  net with a pattern-assigned class instead of the class itself (KiCad's own lookup shows the
  class). **P3a (M12) / P5b** should map it to the constituent class (P1a file, not changed here).
- `pcb_set_rules` read-back of a custom rule checks its name and `(condition "…")` only, not
  the constraint type or min/max values KiCad wrote.
- A custom-rules undo whose restore raises makes the engine raise `DocumentInconsistent` before
  the earlier stages' undos (net classes, design rules, layer count) ran (engine order, P1a);
  the turn ends and the last save is reopened. Never seen; the restore has a 120 s timeout.
- The staging area wraps into a new band below once a column would pass x = 2000 mm (KiCad's
  int32-nm range is ±2147 mm).
- The tool-hole mark is the footprint description: a tool hole duplicated in pcbnew carries it
  (and is replaced by the next `pcb_set_outline {holes}`), and "update footprint from library"
  resets it (the hole then counts as hand-placed and is kept, so a re-run adds another).
- `test_parity_clean_after_set_outline` passes, but `sp4_synth`'s parity report was empty
  before the outline too; that a hole without the attributes would raise a parity item is
  unverified.

### As built (P1c)

**Measured / recorded** (`tests/kicad/board/test_render.py`, `record_property`; ecc83 only — P9a
re-measures on a large 4-layer board)

| Board | SVG bytes | Elements | Coord pairs | Max per element | Span | Raster s | Raster peak RSS |
|---|---|---|---|---|---|---|---|
| ecc83 top | 49,243 | 877 | 1,928 | 4 | 46.5 | 0.71 | 73.5 MB |
| ecc83 + GND pours (F.Cu, B.Cu), top | 77,674 | 878 | 3,519 | 1,591 | 65.5 | 0.71 | 73.7 MB |
| ecc83 + GND pours, bottom | 55,017 | 338 | 2,752 | 1,834 | 42.9 | 0.64 | 72.1 MB |

- `BOARD_SVG_LIMITS` keeps the B-C9 values (24 MiB, 200,000 elements, 2,000,000 coordinate pairs,
  50,000 per element; all far above 4× the measurements), `max_span = 20,000`.
- `BOARD_RASTER_TIMEOUT_S = 30` (≥ 3× measured, under the 60 s cap), `BOARD_RASTER_MEMORY_BYTES =
  2 GiB` (`RLIMIT_AS` counts every mapping, so it stays well above the 74 MB RSS; footprint worker:
  1.5 GiB). Other constants: `BOARD_SVG_TIMEOUT_S = 60`, `BOARD_3D_TIMEOUT_S = 120`,
  `BOARD_MAX_SIDE_PX = 1600`, `KEEP_BOARD_SVGS = 20`, `SIMPLIFY_TOL_MM = 0.02`,
  `SIMPLIFY_BUDGET = 5,000,000` point-distance evaluations per `simplify_paths` call (RDP is
  O(n²) on adversarial input; past the budget the rest of the SVG stays unsimplified).
- `pcb_render` wall time 1.4–2.1 s per view on ecc83; 3D iso PNG 1568 px long side.

**Contract deviations / additions**
- B-C1: `profile_specs(..., footprint_tools=True, board_tools=False, freerouting=True)`;
  `freerouting` is accepted and unused — **P6b** passes `freerouting=settings.freerouting` at both
  call sites (`sdk_backend.py`, `fake.py`) and adds the filter line. `PROFILES["free"]` also lists
  `PCB_READ | PCB_WRITES` (Free still gets every registered tool; the flag still drops
  `PCB_WRITES`). The profile/skill registry tests exempt not-yet-registered `PCB_READ | PCB_WRITES`
  names; each later phase's registration makes them checked.
- `session-board` skill is offered in Board sessions only (`sessions: board`); the flag-on prompt's
  `_EDITING` line points board edits to a Board session. Whether Free also gets the skill (needs a
  flag check in skill assembly) is left to **P9b**.
- Board write policy: `board.md` patch (any section, `create=true`) and append; `decisions.md`
  append only (`doc_write` → the existing `doc_append_only`).
- `_inconsistent_error` (P1a residual, done): a `PCB_WRITES` tool's leftovers are worded as board
  items ("These board items differ from the state before this edit: …"), a leftover created
  `.kicad_pcb` / `.kicad_dru` as "The board file this call created is still there: …" plus the
  History-undo hint; schematic/footprint wording unchanged.
- B-C9: `rasterise(..., timeout_s=None)` (`None` → `RASTER_TIMEOUT_S` at call time, so tests that
  patch the module constant still work); footprint argv byte-identical. The worker parses argv
  before setting limits; bad argv → exit 2; CPU ≤ 120 s, memory ≤ 3 GiB accepted.
  `wrap_svg(svg_text, *, extent: Rect | None, scale_bar_on, hulls, markers, title, mirror,
  highlight, highlight_nets, board_size) -> str` with pure `Hull`, `Marker`, `HighlightShape`,
  `block_hulls`, `highlight_shapes`; the wrapped SVG passes `load_kicad_svg` a second time. The
  origin cross is drawn at the view's top-left corner, labelled with its board coordinates.
  `render_board(..., model=None)` (avoids a second board read); the board copy is taken
  **before** `BOARD_CLI_LOCK` (`save_copy` holds the edit lock), export/sanitise/raster run
  inside it.
- `pcb_render`: `layer` with a view other than `layer` → `invalid_argument`; markers capped at 100
  (ids 1..n, `markers_omitted`); a `<uuid>.svg.json` download-name sidecar is written (pruned with
  its SVG). `pcb_status.zones` counts copper zones only (rule areas excluded).
- Live view (G8): `LiveViewPublisher.finish()` returns the `set[DocKind]` sent live (was `bool`);
  `HistoryHooks._turn_end` catches up `live_sent − published` from disk (`publish_disk_view(project,
  docs)` with the pcb branch `snapshot.copy_disk_board`); a board that went out live but failed to
  save is caught up from disk, never copied from KiCad. `sdk_tools.py` unchanged: the adapter
  already publishes once after `apply` (`test_board_tool_publishes_once` is a guard).

**Residuals for later phases**
- A live publish with both `sch` and `pcb` pending is skipped as a whole when the schematic has
  unsaved edits (failed partial edit); the board then lags until the turn end (editing halts in
  that turn anyway).
- Render limits and raster cost measured on ecc83 only (P9a: large 4-layer board, D5 memory).
- Comments in `cc/agent/engine.py` still say "live schematic view" (P9b docs pass).
- Review round 1 (deferred): kicad-cli 3D render has only the 120 s wall clock and the lock, no
  rlimits (P9a measures kicad-cli peak RSS per kind, D5); up to three ElementTree parses of a
  ≤ 24 MiB board SVG in the render thread (measured SVGs 50–80 KB; P9a re-measures); a process
  crash mid-render can leave one uuid-named board copy in `renders/tmp/` (no startup sweep).

### As built (P1b)

- **Fixture** `backend/tests/fixtures/projects/sp4_synth/` (`sp4_synth.kicad_pro`,
  `sp4_synth.kicad_sch`, `power.kicad_sch`; no `.kicad_pcb`), written only by
  `scripts/make-sp4-synth.py` (runs `tests/kicad/board/test_sp4_synth_build.py`): KiCad's empty
  project → `ROOT_STEPS` + `POWER_STEPS` (`tests/kicad/board/sp4_synth_steps.py`) through the
  real schematic tools → save → files copied verbatim. **26 parts** (18 on `/`, 8 on `/Power/`),
  **28 nets** (10 of them single-node `unconnected-(…)` nets of no-connect pins), ERC **0 errors,
  0 warnings** (accepted ERC items: none), dry-run netlist import into a new board: 0 errors,
  0 warnings, 26 new footprints. Golden `tests/kicad/board/goldens/sp4_synth_summary.json`
  (parts, nets, sheets, ERC counts; no UUIDs/positions). Generator runtime ≈ 60 s per run
  (two runs: 60 s and 59 s wall, both matching the golden; the files differ only in UUIDs and
  the UUID-driven item order).
- **References follow placement order, not the phase table:** the ESD part
  (`Power_Protection:USBLC6-2SC6`) is **U2**, the buck (`Regulator_Switching:TPS563200`, on
  `/Power/`) is **U3** (`sch_place_symbol` assigns the next free reference project-wide and
  refuses explicit references for single-unit parts). P3a's plan text was updated in this PR
  (`test_classification_matches_circuit`: U3 `switching_regulator`, blocks U1/U3). All other
  references are as in the table (C1–C6, R1–R4, Y1, J1, J2, D1, H1, H2 on `/`; L1, C7–C10, R5,
  R6 on `/Power/`).
- **Extra named nets:** besides `USB_DP`, `USB_DN`, `SWDIO`, `SWCLK`, `OSC_IN`, `OSC_OUT` (root)
  and `SW`, `FB` (`/Power/`), the root sheet labels `CC1`, `CC2`, `NRST`, `BOOT0` (labels keep
  the sheet readable; wires only for PA4 → R4 → D1, VDDA → VDD, R6 → R5 and VBST → C10).
  Local label nets carry the sheet prefix in netlists (`/USB_DP`, `/Power/SW`); rails `+3V3`,
  `GND`, `VBUS` are global. Unnamed nets: `Net-(D1-A)`, `Net-(U1-PA4)`, `Net-(U3-VBST)`.
- **One fixture `synth_session`** (schematic only; `tests/support/kicad_project.py`, registered
  in `tests/conftest.py` like the other session fixtures); no `synth_nopcb_session`. Tests that
  need a board create it (P2's sync, or P3a's `synth_board`).
- `test_sp4_synth_build.py` also exports `summarize(kicad_cli, root_sch, work)`,
  `create_empty_project(root, name)` and `standalone_server(kicad_cli)` (a private api-server
  with the stock library tables seeded via `kicad_cli_env`) — reusable for P3a's `synth_board`.
- `scripts/refresh-fixtures.sh` now calls `make-sp4-synth.py` after `make-blank-fixture.py`; it
  needs `uv` and the backend environment (like the CI kicad job), not only `/opt/venv`.

### As built (P1a)

**Measured / recorded**
- api-server `VmRSS` with the ecc83 board open: ≈ 490–515 MB (`test_api.py`, printed + `record_property`).
- DRC-in-open-commit probe (`test_probe_drc_sees_open_commit`): **`drc_sees_open_commit = False`** — a DRC task
  run inside an open board commit does not see the uncommitted track; the same check after the push does. No
  code depends on it (spec §6.2).
- Reads inside an open board commit return the **pre-commit** board (same root cause): a stage cannot read back its
  own writes inside its own commit. Later phases that need a read-back split the work into two stages (or use
  `commit=False` and push their own commits).
- ecc83 counts pinned in `tests/kicad/board/test_read.py`: 15 parts, 59 tracks, 0 vias, 1 zone, copper layers
  `F.Cu`/`B.Cu`, outline kind `"other"`, net classes `("Default",)`; DRC: 17 markers, 0 unconnected (matches
  `kicad-cli pcb drc` on a `save_copy`).
- `MANIFEST_VERSION = 3` (`absent` list; versions 1–2 read as `[]`; `library_restore` accepts 2 and 3).
- CPU worker: `WORKER_MEMORY_LIMIT_BYTES` = 3 GiB; peak RSS of the P1a selftest job ≈ 26 MiB (`VmHWM`); P5a adds
  the large-board value.
- KiCad rewrites `.kicad_pro` when a board document is created; after a failed creating call the engine removes
  the created `.kicad_pcb` (`remove_created`) but the `.kicad_pro` rewrite stays until the History undo restores it.

**Contract deviations / additions (B-C2–B-C4 updated in place where binding)**
- B-C3 `layer_types`: the pinned API cannot return copper layer types; `load_board_model` parses them from the
  `(layers …)` header of `SaveDocumentToString` (read-only; one extra request per load).
- B-C2 `Zone.filled` holds the outer outlines of the filled polygons only (holes dropped; `filled_area_mm2`
  subtracts them). P3a M11 needs holes → extend the model then.
- Footprint keepouts (`Part.rule_areas`) come from the API footprint-relative and unrotated; the loader transforms
  them (verified at 0°/30°/90°); mirroring on flipped footprints is unverified for asymmetric keepouts.
- `run_drc_task` checks its deadline before every status poll (also the first).
- Busy probes (`refill` after kipy's `refill_zones`, and `session.call`'s wait while the busy slot is set) are
  Pings with a 2 s receive timeout (`PROBE_PING_TIMEOUT_S`, set temporarily on kipy's private pynng socket —
  helper `_short_timeout` in `session.py`; falls back to the normal timeout with a log line if kipy's internals
  change). `refill` never sends a probe that could end after `start + timeout_s + REFILL_BUSY_GRACE_S`, so it
  returns or raises within `timeout_s + 30.5 s < refill_call_timeout`. `session.call` with the busy slot set
  probes within its own budget (−1 s) and otherwise raises `refill_timeout` **without** running the callable or
  restarting (slot kept).
- `KicadSession.commit(..., doc="pcb")` refuses with `DocumentNotOpen` when the project has no `pcb_file`, or a
  running server has no board open (while the server is down the next call's restart decides).
- History: `absent_plan` / `remove_created` accept only **direct children** of the project dir (stricter than
  "inside"; no symlinked parent can redirect an unlink). A board save also records the `.kicad_dru` hash (else
  KiCad's own rules file would read as an external change). `HistoryService.projects_root` (set by
  `install_history`) is used for re-discovery. `ChainedHooks.on_project_changed`, `RunEngine.refresh_project`
  and restore's refresh guard each hook (log and continue); `EditingHooks` / `SessionHooks` follow
  `on_project_changed` too.
- B-C4: `BoardChangeSet.report: dict` (the stages' report, returned as `data`); `CopperSnapshot.raw` (stored
  protobufs, so a re-created via keeps padstack/type); `"full"` compensation also restores **changed**
  tracks/vias, not only missing ones; re-created items keep their KIIDs. Compensation runs as `restore` (one pcb
  commit) + `leftovers` (read after the push); `compensate()` = both. `BoardStageContext.call(fn, *, timeout=None)`.
  A cancel after a non-compensable stage started is reported like `sync_failed` (undo via History).
- B-C2 `BoardModel.texts` (board-level texts and text boxes, kind `"text"` / `"textbox"`) is in `board_hash`;
  both hashes sort the full canonical part rows, so duplicate refs (`REF**`) hash independent of API order.
- Test rig: `board_ctx(session, db, sessions=None)` in `tests/kicad/board/conftest.py` (reload needs the real
  `KicadSessionManager`); fixtures `blank_nopcb_sessions`, `board_db`.

**Residuals for later phases**
- `sdk_tools._inconsistent_error` words leftovers as "local footprint library"; board wording needed (P1c).
- The real-server path where a 2 s probe times out (socket closed, kipy reconnects) is covered by fakes only.
- Removing a group the call created leaves its member footprints (members are compensated separately).

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
    `<dest>` with `actions/cache` (SHA-pinned with a version comment, like the pins in `kicad-base.yml`) keyed on the lock file hash.
12. **`kicad_e2e` marker** registered in `backend/pyproject.toml`; the 8 `kicad` shards run
    `-m "kicad and not kicad_e2e"`; a new `kicad-e2e` CI job runs `-m kicad_e2e` on the pinned
    base image only.
13. **Restore re-discovery**: `restore._reload` calls `get_project(root, project.id)` after the
    file operations and passes the fresh `Project` to `sessions.reopen` and `publish_view`; the
    tool-side equivalent after board creation is `ctx.reload_project() -> tuple[Project,
    KicadSession, int]` (new `ToolContext` callback, P1a; reopening replaces the session; the
    `int` is the new session's `generation` captured at reload time), and the engine assigns
    project and session onto `ctx` for the rest of the call and records the captured value in
    `ctx.reloaded_generation`; `_two_phase_steps` re-baselines its restart check to that value
    (not to the generation after apply), so a restart of the new session in a later stage
    still raises `KicadUnavailable`. `RunEngine.project` and
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

## Provisional — resolved by the owner (2026-11-25)

All three items were answered "as recommended" by the owner on 2026-11-25 (binding; recorded
in P3a's PR):

1. **JLC preset tightening** (call 8): **resolved** — the stricter values stay (annular ring
   0.15 / 0.18 mm, silk text height 1.0 mm, 2-layer min via diameter 0.66 mm); P1a already
   pins them in `presets.py`.
2. **M1 vs. edge connectors**: **resolved** — a part listed in `board.md` → Placement edges may
   overhang **its assigned edge only**; everything else fully inside. P3a implements it as
   `M1_EDGE_OVERHANG = True` (`cc/board/metrics.py`, tested with both settings).
3. **P2b start** (call 2): **resolved** — patch 0005 is carried with the placeholder subject
   right after P2 merges; the owner writes the upstream text later.

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
