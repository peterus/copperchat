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

**Round 2 (R1–R7, 2026-11-26) runs after P9a and before P9b** — section "Round 2" at the end of
this index (spec §22).

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
| R-1 … R-4 | R1 … R7 | round 2 (section "Round 2"), between P9a and P9b |
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
  T-junction → fail (**superseded 2026-11-26, spec §22**: T-junctions are allowed, Y-junctions
  fail M15, M16 added); M5 a signal connection with detour > 2.0 **and** > 5 mm extra → fail; M7
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
  | `pcb_tidy_silk` | 480 s | P7, no deadline: ≤ 4 DRCs × `drc_call_timeout(60)` 105 + 2 plans × `run_cpu` 30 (sp4_synth: < 2 s) |
  | `pcb_check` (full) | 495 s | R6: API DRC `drc_call_timeout(60)` 105 + heavy gate / idle worker / `BOARD_CLI_LOCK` `HEAVY_WAIT_S` 120 + parity `BOARD_PARITY_TIMEOUT_S` 120 + metrics `run_cpu(timeout_s=30, wait_s=HEAVY_WAIT_S)` 150 |

  If `L` is lower than a worst case + 60 s, set `MCP_TOOL_TIMEOUT` (≥ 1,065,000 ms = 1005 s
  + 60 s); if that does not lift it, scale down: every deadline is capped so its tool's
  worst case ≤ `L − 60` (`timeout_s` max = `L − 465`, `ROUTE_DEADLINE_S` = `L − 120`,
  `FANOUT_DEADLINE_S` / `STITCH_DEADLINE_S` = `L − 165`, each `min` with its nominal value);
  if the capped `timeout_s` max falls below its schema minimum 30 (`L` < 495 s),
  `pcb_route_fill` is disabled — removed from the profiles exactly like
  `COPPERCHAT_FREEROUTING=off`, with one startup log line — and reported to the coordinator as
  an owner decision. `L` and every cap are recorded in "As built (P6b)".
  **Measured (P6b):** L ≥ 1070 s (CLI default 1e8 ms, no idle timeout for `sdk` servers) → no
  caps, `pcb_route_fill` enabled.
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
                                         item_kiids: tuple[tuple[str, str], ...] = ()     # P7: footprint item KIID -> role ("pad:<n>", "reference", "value", "field:<name>", "text", "textbox", "shape"); not hashed
                                         silk: tuple[GraphicItem, ...] = ()               # P7: footprint F/B.SilkS graphics (board coords); not hashed
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

`TextItem(kind, text, layer, at, size, thickness, visible, angle=0.0)` (P7: `angle` in degrees,
board frame; in `board_hash` for footprint fields only) and `GraphicItem(layer, kind, points,
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
# inside a stage; on any error after the first stage STARTED → compensate() (+ stage undo; P7: restore, then
# the stage undos, then the leftover read-back); verify after the last stage; result changed_docs={"pcb"}, data =
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
                target: Point | None = None, doc: BoardDoc | None = None) -> PlaceResult | DoesNotFit   # As built (P4): doc kwarg; moves = fixed first, then members
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
               window_mm: float | None = None, extra=()) -> ObstacleGrid   # None → WINDOW_MM (6.0); solve/solve_pair retry once with 2 × WINDOW_MM (the retry from pad b, As built P5a); lattice aligned to req.a
def route(grid: ObstacleGrid, req: RouteRequest, *, max_expansions: int = 2_000_000) -> RoutePath | None
def route_pair(model, p: RouteRequest, n: RouteRequest, gap: float) -> tuple[RoutePath, RoutePath] | None
def solve(model: BoardModel, req: RouteRequest, *, margin: float = 0.0, max_expansions: int = 2_000_000) -> RoutePath | None   # run_cpu entry point; owns the one window-doubling retry (route never retries); first try ≤ FIRST_TRY_EXPANSIONS, retry from pad b (As built P5a)
def solve_pair(model, p, n, gap, *, margin: float = 0.0) -> tuple[RoutePath, RoutePath] | None                                 # run_cpu entry point
def pair_coupling(a: RoutePath, b: RoutePath) -> dict[str, float]   # keys length_mismatch, gap_min, gap_max (centre-to-centre over the coupled stretch; M12 reporting)
def prune_model(model: BoardModel, req: RouteRequest | tuple[RouteRequest, RouteRequest]) -> BoardModel   # only items intersecting the retry window; callers pass it to solve/solve_pair
# cc/kicad_tools/board/route.py (P5b), reused by P6b rip-and-reroute and ladder rung (c):
async def route_connections(sctx: BoardStageContext, plans: Sequence[ConnPlan], *, lock: bool,
                            deadline: float | None = None) -> RouteOutcome   # deadline = time.monotonic() value; checked before every margin attempt; connections not started by then → failed "timeout"; each run_cpu gets timeout_s=min(30, remaining), wait_s=remaining, each DRC min(60, remaining); DrcTimeout → that connection failed "timeout" (its items removed), others kept; worst case deadline + one DRC (≤ 60 s)
#   As built (P5b): a coupled pair that fails is routed net by net; a blocked connection may take out one
#   connection THIS call routed (≤ RIP_MAX = 6, nearest first), route, re-route the other (else put it back
#   exactly); one more such round for what stayed blocked. Never touches copper the call did not create.
#   RouteOutcome(routed: tuple[str, ...], failed: tuple[tuple[str, str], ...], results: tuple[ConnResult, ...] = (),
#                drc: DrcResult | None = None); connection key "{net}:{refA}.{padA}-{refB}.{padB}" (pads in natural order,
#                = chamfer's keys; a repeated key — duplicate pad numbers — gets "#2", "#3" in the outcome); ConnResult(key, net, a, b, status, reason, length_mm, vias, kiids, coupling, seconds)
#   failure reasons: no_path, too_large, timeout, drc_rejected: <text> (a DRC rejection of an earlier margin stays the
#   reason when later margins find no path)
#   ConnPlan(net, a, b, req, pair): a matched pair connection = two ADJACENT plans (P, then N) with pair set
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
`stale`, `check_failed`, `changed_during_export`}; P8: `check_failed` also when the
export-time metrics have a hard failure), `approval_not_allowed`
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

### As built (R2b)

Owner follow-up to R2 (2026-11-27: parts inside a block too close, blocks too far apart,
arrange more relaxed and logically). Phase file `board/round2-r2b-relaxed-placement.md`.
Only `pcb_place_blocks` changes; `pcb_place_block` and `pcb_floorplan` keep their layouts
(`relax` defaults off).

**Routing room inside a block** (`layout.py`, `layout_cluster(relax=)`, `block_layout(relax=)`):
a satellite in front of host pins keeps one track pitch (Default class width + clearance,
`routability.signal_pitch`) per two pins it does not connect to (ground excluded),
at most `ESCAPE_TRACKS_MAX` = 4, plus one via pitch (via diameter + clearance) when that
host side's pin pitch is < `FINE_PITCH_MM` = 0.65 mm and a pin there (ground included)
must leave; every satellite keeps at least one pitch to its neighbours. **Deviations from the
phase file (measured):** decaps get the via room only (in front of a fine-pitch pin other
than the one they serve; a full track pitch made ODH's M8 3.6 → 4.0 mm, and without any
room the flush decap C2 boxed in `sp4_synth`'s U1.15 GND pin → one `unconnected` in the
full check after the pour); a switcher's inductor / input / output caps are never relaxed
(relaxed they took ODH from 3 to 6 M9 reviews); crystals unchanged. Clusters of one block
are shelf-packed `channel_mm` apart (widest need of any cluster to the rest).

**Block placement** (`floorplan.place_blocks`): the floorplan (relaxed boxes, else compact,
else the fixed blocks alone - a box-shape or box-area refusal no longer stops the run) only
places the fixed blocks (edge strips / Region rows; relaxed, else compact layout in the
strip, else the whole board = `fallback`) and gives every free block a weak target; the free
blocks are then packed one by one, the most weighted-connected to the placed ones first
(net weight: signal 1, supply `SUPPLY_WEIGHT` = 0.25, ground 0), on the whole usable box
against the **parts** already placed (blocks interlock), each placed part keeping
`channel_mm` = clamp(1.0 + pitch x ceil(shared / 2), keep_channels, 4.0) + slack.
`pack_block(gaps=)` grows an obstacle part's box by its extra gap; a block that finds no spot
moves to the front and the run restarts (≤ `RETRY_MAX` = 8). **Deviation (not in the phase
file):** the greedy order is sensitive to the target pull (ODH 80 x 65: 1127 vs 1511 mm
airwire), so every slack (`SLACK_STEPS_MM` = 1.0 / 0.5 / 0.0) x pull (`TARGET_PULL_WEIGHTS`
= 0.02 / 0.1 / 0.2 per member and mm) is tried per layout and the least airwire x
(1 - `SLACK_CREDIT` 0.05 x slack) wins; relaxed layout first, P4's compact layout only when
nothing relaxed fits; R2's region packing remains the last resort. The `grow` (bounding-box
growth) term from the first draft was dropped: it only raised airwire. `Floorplan` gains
`gaps`, `slack_mm`, `order`, `relaxed` (the free blocks' layout), `compact_fixed` (fixed blocks
whose strip only held the compact layout - on ODH both edge blocks) (additive). Review round 1:
the constructive packing spends at most `PACK_BUDGET` = 600 `pack_block` calls per layout pass
(deterministic; review round 2: per pass, so a failing relaxed pass never starves the compact
one; ODH 80 x 100 uses 117, 80 x 65 357, 80 x 60 fails after 753 in 36 s); the fixed-blocks-only
floorplan runs on the parked model (a free block's stale position is no obstacle) and gives
`usable_mm2`.

**Checks** (`routability.py`, reported, not metrics): `PlacementScore.congestion_worst` /
`congestion_over` (RUDY per 2.5 mm cell: connection length x pitch spread over its bbox vs
cell area minus pads x 2 outer layers), `largest_hole_mm2` (largest empty rectangle inside
the courtyards' bbox, 0.5 mm raster); `pcb_place_blocks` adds `order`, `relaxed`,
`slack_mm`, `congestion_*`, `largest_hole_mm2`, `gap_excess_mm` {worst over pairs sharing a
signal net, shared-weighted mean} and `gaps` (≤ 32 rows, largest excess first). `spread`
stays reported but is no longer a target (compactness is) - `well_spread` is often false now.

**Measured, ODH copy** (`2e17ac5`, 4 layers `jlcpcb-4l`, Power 0.3 mm, J201 left, J302
bottom, keep_channels 1.5; R2 in brackets):

| Board | Fits | Airwire mm | Gap excess mean / worst (signal) mm | Largest hole mm² | Congestion worst | M8 / M9 rev | Time |
|---|---|---|---|---|---|---|---|
| 80 x 100 | yes, relaxed, slack 0.5 | 1161 (1822) | 5.0 / 20.3 (16.0 / 52.9) | 627 (548) | 0.91 | 3.6 / 3 | 12 s |
| 80 x 80 | yes, relaxed, slack 1.0 | 1205 | 6.1 / 30.4 | 462 | 1.00 | 3.6 / 3 | 9 s |
| 80 x 70 | yes, relaxed, slack 0 | 1171 | 4.5 / 25.9 | 325 | 1.09 | 3.6 / 3 | 10 s |
| **80 x 65** | **yes, relaxed, slack 0** | 1253 | 6.1 / 26.7 | 206 | 0.81 | 3.6 / 3 | 20 s |
| 80 x 60 | `does_not_fit` (courtyard fill 0.429) | - | - | - | - | - | 36 s |

M1 0 everywhere; M13 J201 0.75 mm from its edge = R2's value (pre-existing, R7). At 80 x 100
the placement uses the lower ~65 mm and leaves the top free (= the board could be 80 x 65).
80 x 60: compact block boxes need 92 % of the usable area; the constructive packing does fit
it without the fine-pitch via room (one draft) but not with it. Renders:
`/tmp/sp4-renders/r2b-odh-80x100-placed.png`, `r2b-odh-80x65-placed.png`,
`r2b-sp4_synth-placed.png`, `-routed-top.png`, `-routed-bottom.png`.

**Golden `sp4_synth`** (regenerated, confirmed by a second run; 0 hard): M5 1.052 → 1.092,
M7 review → pass, M10 1.722 pass → 1.774 review (the interlocking blocks' hulls intrude
each other - review only), M11 0.9582 → 0.9581, M15 3 → 2, M16 18 → 19 (review-only, router
output), tracks 184 → 177, vias 124 → 123, length 282 → 224 mm, fill 14.1 → 12.0 s.
`tests/kicad/board/test_place_blocks.py`: one alignable pair left (C1.1-U1.5), R3.1-J1.B5
got its corridor.

**Residuals**
- R4 / R7: M16 doglegs (19 on the golden) are router output; M16 pad jogs / S-offsets are
  hard per the owner (brief 2026-11-27) - R4 switches them on.
- R7 / owner: J201's 0.75 mm edge distance (M13) as in R2; J402's microSD body overhangs
  the right edge in the 80 x 100 run (no courtyard overlap, M1 0).
- Placement at 80 x 100 is compact at one end of the board, not centred; a centring /
  even-slack pass would need the edge blocks to move with it (they stay on their edges).
- Congestion is a reported estimate (2 outer layers); not calibrated against routed boards.
- The relaxed routing room is computed for the spot a satellite wants (`t_star`); a satellite
  that slides along its row keeps that gap (review round 2, documented, not recomputed).
- Review round 3 (valid, deferred, PM inbox #60): `_relaxed` sorts host pads to a side by the
  nearest courtyard edge, so a SOIC/TSSOP row's end pins near a short edge are missed. The
  pad-axis fix (as `_side_of`) shifts `sp4_synth`'s block arrangement and the router / pour then
  isolates U1.15 (fine-pitch GND pad, P7's `via_pour` fallback) → golden M2 hard fail. Fix both
  together (robust fine-pitch GND fanout first) in R7.

### As built (R2)

**Floorplan** (`cc/board/placer/floorplan.py`, B-C11 round 2, pure, deterministic):
- `BlockBox` = the block's `block_layout` box + `keep_channels` + one 0.5 mm grid step
  (P4's edge-strip slack). Additive fields: `BlockBox.offset` (mm along the edge from the
  outline's top / left end, like `pcb_place_block`'s offset: only `<edge>, <n> mm` in the
  Edge/position cell, e.g. `left, 15 mm`, optionally `from top` / `from left` and a trailing comment; another reference, a negative number or "USB 2.0" → the middle) and
  `edge_ref` (the member with the row); `Floorplan.fixed` (blocks with a Region row or an
  edge strip) and `Floorplan.fallback` (blocks `place_blocks` had to pack on the whole board).
- Edge strips: the orientation that turns the connector's mouth toward the edge first
  (`_facing_dims`, P4's `_mouth`), the other as the fallback; a strip without a position that
  clashes slides along its edge; an explicit position that clashes → `does_not_fit`.
  Region rows: `parse_region` = four numbers (x, y, w, h in mm, board coordinates) or
  `left/right/top/bottom` (half) / `top-left` ... (quarter); free text ("near J1") is ignored.
- **Deviation (not in the plan): an edge channel.** The free area is the usable box inset
  by `max(keep_channels, CHANNEL_MIN_MM)` (routing room between a block and the board edge;
  on `sp4_synth` the power block hugged the top edge and boxed in R6), and the guillotine
  pieces keep a channel between each other (`_minus(gap=)`). Every channel is at least
  `keep_channels` (else two neighbours could end closer than `pack_block` allows).
- Free blocks over the free rectangles (`_assign`): the biggest takes all except blocks with
  nets to a fixed block that a smaller rectangle touches - only while a trial bisection of
  that rectangle succeeds (`_tiles`; ODH 80 x 100: three blocks fitted a 30 x 17 mm side
  piece by area but not by shape).
- Bisection: both cut directions (longer side first); partitions ranked (infeasible,
  unbalanced, shared nets across the cut + terminal-propagation pull, |area share - 0.5|,
  names); the rebalance-once rule as planned. **Addition: bounded backtracking** - when a
  deeper cut fails, the next of the best `BACKTRACK_TRIES` = 6 partitions of that level is
  tried, within `BISECT_BUDGET` = 400 tried partitions per run (ODH 80 x 100 with two edge
  rows failed without it at 54 % box fill). Floorplan of ODH's 11 blocks: ≈ 0.3 s.
- `place_blocks` packs each block with **`REGION_PULL_WEIGHT` = 2.0** per member and mm
  toward its region centre (`pack_block(pull_weight=)`; P4's 0.1 kept every block hugging
  its neighbours: `sp4_synth` spread 0.58 → 0.78), fixed blocks first, then by courtyard
  area; a block that does not fit its region gets the whole board once (`fallback`).
  `pack.pack_block` (new) = `place_block` without the score (scored once at the end).
- `PlacementScore.spread` (appended, default 0.0): parts on the board, mounting holes and
  fiducials not counted; usable = outline box minus the edge margin; 0.0 without an outline.

**Pair alignment (rule b, `align_pairs`)** over R1's M16 placement pairs - **R1's
`metrics.py` touched additively**: `alignment_need(a, b, width, layers)` (`_alignable`'s
geometry, 0.0 = corridor exists) and `alignment_pairs(model, cls)` (M16's MST pairs with
the neck width), so the placer targets exactly what M16 counts. Two passes in natural ref
order, only a movable non-anchor 2-pad part moves: (1) align - centre-aligned when ≤ 1 mm,
else the least overlap shift, once per part; (2) **tighten (addition)** - a pair with a
corridor whose pads are > `TIGHT_GAP_MM` = 0.3 mm apart is pulled together along the
corridor in 0.05 mm steps as far as the courtyards allow (on `sp4_synth` the +3V3 track took
the 1.75 mm gap between R6.1 and R5.2 and boxed in /Power/FB); never a pair with a crystal
(review round 2: it would undo `CRYSTAL_STANDOFF_MM`). Every move keeps the pair's
corridor and every corridor the part already had, stays inside the outline margin and off
courtyards and keepouts. Crystals / decaps are not excluded (M8 unchanged at 4.97 mm).

**Placer escape room (P4 files, plan Task 4 "per-pin escape room"; switched on by
`place_blocks` only - `layout_cluster(escape_room=)`, `pack_block(occlude_weight=,
escape_room=)`; `pcb_place_block` keeps P4's layout, else the P5b/P6b fixtures built with it
(`place_synth`) moved and their fill/route tests failed in CI)**:
- `layout.ESCAPE_PER_NET_MM` = 0.4 per signal net a > 2-pad satellite (header, ESD array)
  shares with its anchor, at most `ESCAPE_MAX_MM` = 2.0, on top of `SATELLITE_EXTRA_MM`.
- `pack.OCCLUDE_WEIGHT` = 10 per pad (not ground) whose straight line to the centroid of its
  net's outside pads crosses another member's courtyard, per block rotation (evaluated with
  the block centred on the target). On `sp4_synth` the MCU block's 90° rotation put the SWD
  header J2 between U1's USB pins and the USB connector: the pair routed through J2's pin
  field and boxed in J2.2 (/SWDIO `no_path`); with the term the header faces away.

**Tools** (`place.py`, `specs_place.py`, `jobs.py` append): `pcb_floorplan {blocks?,
keep_channels?}` → `regions, channels, fixed, fallback, courtyard_fill, courtyard_mm2,
usable_mm2, keep_channels, board_hash, next, png_url, svg_url` + one PNG (regions as named
hulls: `render_board(hulls=)`, drawn instead of the block hulls); `pcb_place_blocks {blocks?,
keep_channels?, unroute?}` → `plan_moves` report + the floorplan fields + `spread`,
`well_spread`. `does_not_fit.details.courtyard_fill` (+ the why text names the percentage).
Without an outline or blocks, or more than `MAX_BLOCKS` (32) blocks by default:
`invalid_argument`. Both run as one CPU job with `PLACE_BLOCKS_TIMEOUT_S` = 180 s (P4's
single-block 60 s; 32 blocks / 288 parts measured ≈ 17 s). `does_not_fit` from a floorplan
carries `details.reason` "area" (Σ block boxes > `PACK_MAX` x usable; `available_mm2` is then
that 85 % share) or "shape" (the outlines + channels do not tile); review rounds 1-2.
`place_blocks` packs on a model with all of this run's members parked off the board (review
round 1: re-running from a placed state gave a different placement); parts that stay
(locked, other blocks; not fixtures) and keepouts come out of the free rectangles. **Deviation:** `pcb_floorplan` is a plain
`PCB_READ` tool, visible with `COPPERCHAT_BOARD_TOOLS` off (P1c's invariant "PCB_READ in
every profile, flag or not"); Task 3's "flag off → both tools absent" holds for
`pcb_place_blocks` only. `PCB_WRITES` has 18 names; `BOARD_KINDS` += `board_place_blocks`.

**Golden flow** (`flow.py`): turn 2 = `pcb_analyze` → `pcb_define_blocks` → the user's edges
as a board.md `Placement` table (`doc_read` + `doc_patch_section`; `Answers.blocks[ref].edge`
/ `edge_position`, any part ref) → `pcb_place_blocks` (no `pcb_legalize`); blocks with a
scripted `region` are placed afterwards by `pcb_place_block` + `pcb_legalize`.
`SYNTH_ANSWERS.blocks = {"J1": {"edge": "left", "absorb": ["U2"]}}`.

**Measured** (`sp4_synth`, kicad-base `52b43fb8…-rdc93f47200b08f97`): courtyard fill 0.133,
spread 0.78, `pcb_place_blocks` < 1 s, critical `pcb_route` ≈ 28 s (was ≈ 90 s), fill 12 s
completion 1.0, full flow ≈ 90 s; placement-stage alignable pairs 2 (C1.1-U1.5: U1 / Y1 in
the way; J1.B5-R3.1: the ESD array U2 in the way - `align_pairs` never moves ICs). Golden
diff (regenerated, confirmed by a second run): M5 1.112 → 1.052, M9 3 → 1, M10 1.858 →
1.722, M7 pass → review (bend density /CC2, OSC), M12 pass → review (USB pair mismatch
2.27 mm: the pair now crosses the board), M15 2 → 3, M16 15 → 18 (review-only; doglegs
incl. D1.2-R4.2 are router output), tracks 174 → 184, length 239 → 282 mm.

**ODH placement probe** (copy of the PROD checkout `OpenDriveHub@2e17ac5` + its working
tree, read-only mount; 4 layers `jlcpcb-4l`, Power class 0.3 mm, blocks = `pcb_analyze`'s 11,
edge rows J201 left, J302 bottom; answers outside the repo):
- **80 x 60: does not fit.** Courtyard fill **0.429**. keep_channels 1.5: refused before any
  cut, block boxes 4,248 mm² = 92 % of the usable 4,620 mm² (> `PACK_MAX` 85 %).
  keep_channels 0.5: boxes 83 % (passes `PACK_MAX`) but **the shapes do not tile**: the MCU
  block's box alone is 45.8 x 31.1 mm and the USB block's strip (connector facing the edge)
  is 17 mm deep. Reason = the rigid block boxes (cluster layouts use ≈ 2x their courtyard
  area), not the courtyard area. 80 x 70 and 80 x 80 do not tile either.
- **80 x 100: fits.** Courtyard fill 0.254, `pcb_place_blocks` 1.5 s (flow step 3.8 s), M1
  pass, **spread 0.94**, M8 3.6 mm (review), M9 3 reviews, M10 1.58, max 16 pads per 5 x 5 mm
  cell, airwire 1,821 mm; 4 alignable pairs left (L201.1-U202.4 1.00, C207.1-U202.8 0.70,
  R206.1-U201.14 0.62, U401.2-Y401.1 0.03 mm - each blocked by an IC or a neighbour).
- Renders: `/tmp/sp4-renders/r2-odh-80x100-placed.png`, `r2-odh-80x100-floorplan.png`,
  `r2-sp4_synth-placed.png`, `r2-sp4_synth-routed-top.png` / `-bottom.png`.

**Outside R2's files (minimal):** `cc/views/board_render.py` (R5's) besides `render_board(hulls=)`:
`BOARD_CLI_LOCK` is now a `LoopLocalSemaphore` (a `Semaphore(1)` per running event loop, same
interface) - adding R2's KiCad tests shifted the round-robin CI shards and
`test_render.py::test_board_cli_serialised` then failed with "bound to a different event loop"
(a module-level semaphore stays bound to the first loop that waited on it). The app runs one
loop, so production behaviour is unchanged. `tests/unit/test_board_cli_lock.py` covers it.

**Residuals for later phases**
- R3 / R4: M16 doglegs / pad loops on the golden are router output (D1.2-R4.2 still a
  dogleg although the pads are aligned); corridor nets first (R4) would also have avoided
  the /Power/FB box-in that the tighten pass fixes on the placement side.
- R7 / owner: ODH 80 x 60 needs denser block layouts (cluster layout compaction or a
  non-guillotine packing of block outlines, not boxes) or the bottom side; reported as
  `does_not_fit` with the fill, as the spec asks.
- The floorplan render draws adjacent region names on top of each other (cosmetic).
- P9b skill: show `pcb_floorplan` before `pcb_place_blocks`; write the user's edges as
  Placement rows (any part ref, number = mm along the edge); prefer keep_channels 0.5-1.5.

### As built (R3)

**Router: neck-down, T-junctions, straight first, pad entry** (`cc/board/router/{grid,astar,
simplify,pairs}.py`, `cc/kicad_tools/board/route.py`; B-C12 round 2). Pure Python + the
existing KiCad write path.

- **Contract (B-C12 round 2) as specified:** `RoutePath.segments` are `(layer, start, end,
  width)`; `RouteRequest.neck` / `.tree` (defaults keep every old call). Additions:
  `ObstacleGrid.width` (the width the needs were built for), `.neck_cells`, `.tree_segments`;
  `segment_clear(..., shrink=)` (a narrower piece needs that much less; the outline inset stays
  the class width's, conservative). `plan_connections(..., tree=False, min_track=None)`
  (`min_track` default: the JLC preset of the layer count, else `jlcpcb-4l`, like M12 / M16);
  `pcb_route` passes `tree=True` and `preset_for(model, doc).min_track`; the fill's re-route
  keeps `tree=False` (R4). `route_connections(..., reserve=())`. `valid_path(..., loops_ok=)`,
  `route(..., loops_ok=)`.
- **Neck-down:** per neck zone a node map over the zone's node box only, copied into `cells`
  for the nodes inside the zone (narrowest zone last) - the search itself is unchanged; the
  cell cap counts window + Σ boxes (`test_neck_map_bounded`). Zones = `escape_zone(part)` of
  each end pad with `neck_width < class width`, **inset by 2 µm** (`NECK_ZONE_INSET_MM`) so a
  split point rounded to 1 nm stays inside the zone M12 checks; `simplify.neck_pieces` splits
  at every boundary crossing and makes no sliver < 1 µm. Pairs keep their widths (no neck).
- **T-junctions (owner rule a):** `junction_masks` blocks a step at 45° / 135° to own copper
  it touches (both ways), a perpendicular step that LEAVES own copper sideways or crosses it
  (X; round-2 plan call 3), and allows a perpendicular ARRIVAL (T). `junctions_ok` judges the
  distinct headings of path + own copper at each meeting node: ≥ 3 → `junction_kind` must be
  `t`; 2 → straight or a 45° bend. Own-pad joint spots are now the exact rotated rectangle
  (a 45° pad's circumscribed disc exempted a Y outside the pad - found by the property test).
  **Tree goals:** nodes within half a pitch of the interior of an own straight track
  connected to pad b (union-find over the net's tracks / vias / pads when the grid is built;
  arcs not followed), ≥ `req.width` from its ends, bend vertices, junctions, crossings, not in
  own pads / vias / pad b, entered perpendicularly; the path is extended straight onto the
  track. Heuristic: min(octile to pad b, Euclidean to the goal pieces); coarse field seeded at
  every goal. **Deviation:** when the tree search finds nothing, a plain pad-b search follows
  (tree alone routed 199 / 224 tree maps vs 215 plain at 100 k expansions; with the fallback
  219). `req.tree` only means "T allowed": the grid finds the copper at solve time, so
  `plan_connections(tree=True)` sets it on every non-pair plan.
- **`_bends`:** M4 without T; new key `M15` (Y-junctions of the call's copper).
- **Straight first (owner rule b):** pass 0 as planned (one push, one DRC; sibling-only
  rejections retried alone). Additions: candidates are pre-filtered (`_corridor_free`: no
  other-net pad / via / track / keepout in the way - USB-C A6–B6 straddle A7 and were DRC
  shorts); **a pass-0 segment is never ripped** by rip-and-retry (`_Kept.straight`; re-routing
  it through the search made the dogleg M16 flags); a matched pair routed straight on both
  nets records its coupling. `straight_path` runs along the corridor's centre line from pad
  a's centre coordinate to pad b's (both ends inside the pads), width = min(class, neck
  widths) as M16 measures, split at the neck zones. Reservations: `corridor_reservations`
  (every free straight corridor of an unrouted non-ground connection) passed by `pcb_route`;
  other nets' searches see them as that net's copper.
- **Pad entry (owner rule c) - deviation:** `ENTRY_TURN_COST = 4.0` only for a bend in an end
  pad's entry zone (outside the pad) that follows another bend within the straight-piece
  minimums (search state `s ≠ 0`: a jog or a loop forming). Charging every zone bend (the plan)
  broke the golden: decoupling C1.1–U1.5 detoured via B.Cu to 7.35 mm (M8 hard), one open
  connection, USB-C A6–B6 unroutable. No hard "away-move ban" (spec §22.4 asks for costs and
  an M16 pass; a ban makes a QFN pad whose goal lies behind the IC unroutable); instead
  `valid_path` refuses an M16 pad loop **and pad jog** (owner, 2026-11-27: a jog / S-offset
  in the entry zone is an error like doglegs and loops). **`solve` order:** default window
  clean → doubled window clean (≤ `RETRY_STRICT_EXPANSIONS` = 1 M) → default window with a
  loop / jog allowed → doubled window likewise (`entry_ok`): a placement-forced one is
  reported by M16 instead of costing 20 s per try and a rip-up cascade (sp4_synth USB-C
  A6–B6: 6 × 20 s). Same-part links (USB-C A6–B6 around A7) are solved `entry_ok` from the
  start: the clean search sent them on via detours that walled in the ESD's VBUS pin on R2's
  placement (`VBUS:U2.5-U3.5` no_path).
- **Review round 1:** a half of a matched pair keeps its straight pass-0 segment even when
  its partner has none (tried "pairs only as a whole": J1.B7–U2.3 then doglegged and USB-C
  A7–B7 became an M5 detour failure - owner rule b outranks coupling, a review); pass-0 retries are capped (`STRAIGHT_RETRY_MAX = 4`); the stored DRC
  drops markers of copper removed again; reservations use the widest piece's width and are
  pre-filtered cheaply (MST pairs with a corridor before `routed_length`); a routed path with
  a pad-entry loop / jog is reported (`ConnResult.pad_entry`, net entry
  `pad_entry_defects`); `fill._bends_of` counts the Y-shaped T the chamfer rips. Measured:
  `ENTRY_TURN_COST` hardly ever decides a path (the second-bend condition rarely holds where
  a cheaper alternative exists) - the guarantee is `valid_path`'s rejection; the cost stays
  as the plan's steering term.
- **After the rebase on R2b (`4d81119`)** the golden failed three ways, each fixed: (1) a
  90-degree meeting of two paths at a via (M4 counts a degree-2 90-degree vertex at a via):
  `junctions_ok` now exempts only pad nodes from that check (`grid.own_pad_at`); (2) M8
  links are flagged `early` and routed before pass 0, crystals first, in plan order, never
  as a T (`tree=False`): the shorter /OSC_OUT link and other nets' straight segments had
  pushed Y1.1–U1.2 to 5.47 mm; an M8 link over the limit also tries a solve that allows a
  pad-entry jog/loop and takes it only when that meets M8 (both owner-hard; the jog is
  then reported); (3) rip-and-retry ripped a connection another path ended on as a T,
  leaving it dangling (DRC `dangling_track`, M3): `_dependants` are now taken out with
  their anchor, re-routed after it, and restored with it.
- **M8 connections first:** `plan_connections` puts the connections M8 measures (decap
  supply pad ↔ its IC pin, crystal ↔ IC) first, shortest first (`_m8_first`): with jogs
  refused, the crystal nets (routed earlier) walled C1.1–U1.5 off to 9.4 mm (M8 hard).
- **`PAD_AXIS_RATIO = 1.5`:** a pad whose long / short side is below 1.5 has no long axis (all
  8 exit directions). An 0603 pad (0.8 × 0.95) could not be left straight towards its
  neighbour - the D1–R4 dogleg the owner marked.
- **Same-part connections** (both pads on one part) plan last in their net when unmatched, so
  they can land on the net's other copper as a T; two crossing same-part connections (USB-C
  A6–B6 / A7–B7 interleave) are not pair-matched; parallel ones (ESD pass-throughs) still are.
- **Pairs:** the coupled centreline's grid carries no own-copper junction data (the centre is
  no copper; the real P / N paths are still checked with the T / Y / X rules).
- **Files of other phases (minimal, round-2 rule a needs them):** `cc/board/router/chamfer.py`
  (R4): `_REASON` keeps `t_junctions` but `_fails` rips only the Y-shaped ones (a T is legal;
  a Y with a 90° pair was ripped before and still is; pure Ys stay R4's). Without this the
  fill ripped every fill connection touching a net with a router T (`ninety_unfixable`).
  `cc/kicad_tools/board/fill.py` (R4) `_bends_of` without T (as M4). Tests:
  `tests/kicad/board/test_fill_postprocess.py` (M4 helper without T),
  `tests/unit/board/test_chamfer.py` (T allowed, Y-shaped T not).

- **Neck layers:** a neck zone applies only on the layers of the end pad inside it
  (`grid.neck_layers`): on ODH a via inside the escape zone took the neck to B.Cu, which M12
  fails (the QFN pad is on F.Cu).
- **sp4_synth critical routing (`test_route_critical_set`, main `972766a` → R3 before
  rebase):** M15 2 → **0**; M16 hard 14 (10 doglegs D1.2–R4.2, R4.1–U1.10, U1.16–C2.1,
  C8/C9–L1, C4–Y1, U3.2–L1, J2–U1 SWCLK, J1.B7–U2.3, C10–U3 VBST; 4 pad loops J1 A6/B6/A7/B7)
  → **3** (0 doglegs; the forced loops J1.A7 / J1.B7 and U3.2 /Power/SW C10.2–U3.2); pad jogs
  5 → 2; M4 = M7 = 0; 37 / 37 routed; `pcb_route` 55 s → 89 s.
- **Golden (`sp4_synth_metrics.json`, regenerated on R2b's main `4d81119` + R3, idle host,
  verified by a second run):** vs R2b's golden M15 2 → **0** (pass), M16 19 → **7**
  (review: 1 dogleg /Power/FB R5.2–R6.1, pad loops J1.A6/B6/A7/B7 of the USB-C links, U3.2
  SW and Y1.3), M8 4.97 → **4.20 mm**, M12 review → pass, M5 1.09 → 1.0, M7 pass → review
  (value 0: bend density only); tracks 177 → 137, vias 123 → 120, 210.8 mm.
- **ODH probe** (copy of the PROD checkout, 80 × 100 mm, P9a's answers with Power class
  0.5 mm, R2's `pcb_place_blocks` flow, critical routing only; renders
  `/tmp/sp4-renders/r3-odh-*`): 25 critical nets, `pcb_route` **50 s** (P9a: 105 s), M4 = M7
  = **M12 = M15 = 0** (every neck-down accepted), M16 hard 3 (doglegs +3V3 C208.1–U202.1,
  /Power/VSYS U202.5–C206.1; pad loop U203.8 /VBUS). **4 `no_path`** (P9a: 13, all from the
  0.25 mm QFN pads): /Power/VSYS C203.1–U201.10 and U201.5–U201.10 (the neck gets into the
  QFN pads; the 0.5 mm piece outside the escape zone does not fit between the placed
  parts), /VBUS J302.A4–A9 (USB-C pins; the VBUS pads are wide enough, so M12 allows no neck,
  but 0.5 mm does not pass the pin row), Net-(U202-L1) L201.1–U202.4 (Default 0.2 mm,
  placement). The first three route at 0.2 mm: class width vs placement / connector pitch,
  not the end pad's width. (Before the rebase, on P9a's per-block placement: 26 nets, 61 s,
  1 `no_path`.)
- **Residuals for R4 / R7:** (0) ODH's 0.5 mm Power class does not pass between R2's
  placed parts next to U201 / through the USB-C pin row (above) - R7 / owner: a narrower
  class, more room, or a neck rule for a part's pin row (M12 would have to accept it). (1) M16's dogleg corridor is measured at the narrower NECK width
  along its whole length, but a straight link may only be that narrow inside the escape zone
  (M12) - the ODH doglegs C208.1–U202.1 / U202.5–C206.1 have no legal straight segment at
  class width; R4 (or an R1-file fix) should test the corridor at class width outside the
  zones. (2) Placement-forced pad loops (USB-C A7–B7 around A6/B6, U3.2 SW) become hard with
  `M16_HARD` - R2's alignment / R4's rip-and-re-route or an owner call; with jogs hard too
  (owner 2026-11-27) the golden's J1.B5 /CC2 jog (USB-C pin row) joins them. (3) The fill
  (Freerouting) leaves sp4_synth's `VBUS:C7.1-U2.5` open next to the ESD's straight
  pass-throughs (the flow's ladder routes it); fill fixtures moved `VBUS` to `OWN_ROUTED`
  and `/CC2` out of `FILL_NETS` (P6b files). (4) Pure Y-junctions from Freerouting are still
  not ripped (R4); `fill`'s re-route keeps `tree=False` (R4 turns it on). (5) `pcb_route`
  is ~35 s slower on sp4_synth (strict + fallback searches); `test_router_perf` budgets
  unchanged and green.

### As built (R5)

**Presets (owner decision 2, spec §22.2):** `min_drill` = 0.2 mm in `jlcpcb-2l/4l/6l`
(`min_via_drill` stays 0.3 mm, plan call 5). `pcb_set_rules` maps it to KiCad's
`min_through_drill` unchanged, so KiCad's own DRC also allows 0.2 mm via holes - the agent's vias
still use 0.3 mm and M14 `via_drill` still checks `min_via_drill`. Tests that pinned 0.3 moved
(`test_metrics_rules` `pth` case now a 0.15 mm hole, `test_preset_maps_every_rule`).

**BOM/CPL (spec §22.6, P8 residual):** `cc/board/fab.py` `fab_excluded(model, cls, only=None)`
- **deviation from B-C9:** a third argument `only: "bom" | "cpl" | None`, because the two
attributes act per file: `"bom"` = mounting holes ∪ exclude-from-BOM, `"cpl"` = mounting holes ∪
exclude-from-position-files, `None` = the union (what the report lists). `bom_rows(csv,
exclude=)` drops excluded designators from grouped rows (a row left empty disappears),
`cpl_rows(csv, exclude=)` drops rows; `small_holes(model)` (plated pad drills and via drills
< `SMALL_HOLE_MM` = 0.3, one row per ref with its smallest drill, sorted by drill then ref);
`fab_report(..., excluded=)` adds "Excluded from BOM/CPL" and "Small holes (possible JLC
surcharge)" only when non-empty. `pcb_fab_export` classifies once through the CPU worker
(`run_cpu(fab_exclusions, model, doc, timeout_s=FAB_METRICS_TIMEOUT_S)` - review round 1; returns
the `bom` / `cpl` / `all` tuples) and passes them to the CSV writer and the report. **`Pad`
gained `npth: bool = False`** (not hashed, from `pad_type == PT_NPTH`): KiCad lists `*.Cu` on
NPTH pads too, so `small_holes` cannot tell plated from layers alone. **Outside R5's files
(review round 2, one line):** `metrics.py` M14 now splits `pth_drill` / `npth_drill` on
`_copper(q) and not q.npth` - before, an NPTH pad with `*.Cu` was checked against `min_drill`
(now 0.2) instead of `min_npth` (0.5); R1 owns `metrics.py` (keep both sides on rebase).
**`Part` gained `exclude_from_bom` / `exclude_from_pos`** (default `False`, not hashed - an
attribute edit is no layout change), filled in `read.py` from `fp.attributes`
(`test_read_exclude_attributes`). P8's `test_file_set` expectation changed: `sp4_synth`'s
schematic holes (H1, H2, no exclude attribute) are now absent from the CPL and BOM.

**Render extent (spec §22.6):** `cc/board/render.py` `default_extent(model, markers=())` =
outline bbox + every courtyard (a part without a courtyard counts with its origin and its pads'
circumscribed extents - not in the contract, so such a part is never cut off; tracks, vias and
zones off the outline are not included, they are DRC markers) + markers; `None` when all are empty; a side below
`MIN_EXTENT_MM` = 1.0 is grown around its centre (a lone marker gives no zero-size view).
`cc/views/board_render.py` `view_extent(model, region, markers)` (region wins) feeds `_Overlay.region`
for every 2D view; 3D unchanged. `test_render_extent_ignores_far_drawing` adds a board text
150 mm right of the outline through a `BoardChangeSet` on `synth_placed` (one more api-server
for `test_render.py`'s shard) and checks the wrapper's viewBox; it fails with the old code.

**Probe (ODH copy, sync + 80 × 100 outline + `_place` with P9a's answers):** KiCad's board area
is `6.96 0.52 179.52 200.48` on every layer set. The item is **J402**,
`Connector_Card:microSD_HC_Hirose_DM3AT-SF-PEJM5` from KiCad's library: its 5 F.Cu keepout
rule areas lie ≈ 130 mm from the footprint origin (library defect: zone coordinates not
footprint-relative), so its KiCad bounding box (and our `Part.rule_areas`) reaches
(6.96, 0.52)-(19.7, 13) mm. Nothing in `BoardModel` outside the rule areas lies outside the
outline after placement (KiCad's text bboxes of H1-H4, F.Fab values included, reach 6.5 mm past it; a
view without them may clip such text at the 3 % padding). With R5 the
default view is the outline (`Rect(100, 100, 80, 100)`).

**Golden:** `sp4_synth_metrics.json` unchanged (`kicad_e2e` 4 passed; no 0.2-0.3 mm holes, the
render extent is not in the golden).

**Residuals for later phases (R7 / P9b):**
- J402's misplaced library keepouts also make `classify` mark it `module_with_antenna`
  (`_pre_class` checks `rule_areas` before the connector rule) and add keepout obstacles at
  (7-20, 0.5-13) mm, outside the board. Fix-forward candidate for R7 (e.g. ignore a part's rule
  areas that do not intersect its courtyard's bbox, or report them as a footprint defect).
- CPL rotation offsets for JLC assembly stay open (P8 residual, `ASSEMBLY_NOTE`).

### As built (R6)

**Heavy-process gate** (`cc/board/heavy.py`, as planned): `heavy_slot(kind, *, wait_s)` = one
`asyncio.Semaphore(1)` per running loop (`_GATES`, a `WeakKeyDictionary` like
`freerouting._SLOTS`) plus the holder kind; kinds `freerouting` / `parity`. `parity` then waits
for an idle CPU worker through the new **`worker.wait_idle(timeout_s)`** (takes the worker lock
and drops it at once — a cleaner seam than reaching into `worker._get_lock()`); a failed wait
releases the gate. Timeouts raise `board_compute_timeout` with `details.waiting_for` ∈
{`freerouting`, `parity`, `cpu_job`, **`kicad_cli`**} — the fourth value (deviation, additive)
names the bounded `BOARD_CLI_LOCK` wait in `cli_parity`, done by the helper `heavy.bounded(sem,
wait_s=…, waiting_for=…)` with the rest of the `HEAVY_WAIT_S` budget.
- Users: `run_freerouting` takes the gate **before** its slot (`MAX_CONCURRENT_RUNS = 2` kept,
  effectively 1), wait `min(HEAVY_WAIT_S, remaining timeout)`, counted against `timeout_s`; a
  gate timeout → `FreeroutingRun(returncode=-1, timed_out=True, log_tail="timed out waiting
  for the heavy-process gate (<kind> still running)")`, so the fill reports its usual
  `fill_not_acceptable` / `timeout` and `FILL_WORST_CASE_S` stays 1005 s. `cli_parity`
  enters the gate only after `_copy_project`. The legacy `run_kicad_check_on_dir` gates only
  `doc == "pcb" and parity` (ERC / DRC without parity ungated); it takes no `BOARD_CLI_LOCK`
  (unchanged). `run_drc` returns `board_compute_timeout`; the board render's markers
  (`views/render.py` `_agent_board`, **one small edit outside R6's file list**) degrade to
  none on exactly that code (logged), any other check error still fails the render as before;
  the marker DRC waits only `checks.MARKER_GATE_WAIT_S` (5 s, `heavy_wait_s` threaded through
  `check_violations` / `run_kicad_check` / `run_kicad_check_on_dir`) so the live view's HTTP
  request does not stall ≈ 120 s behind another project's fill or full check (review round 1;
  not cached, the next render retries). Residual (pre-R6, review round 2): a render of the
  project whose own fill is running still waits for it earlier, in `save_copy` on the apply
  lock (only on a marker-cache miss). `FreeroutingRun` gains `started: bool = True` (False for a gate / slot timeout);
  `fill.py` (R4's file, minimal edit) words that refusal "Freerouting did not start: another
  heavy board process … was still running" with `details.started: false` and suggestion
  "Retry the same call once it has finished" instead of "raise timeout_s" (reason stays
  `timeout`).
  `pcb_check`'s metrics `run_cpu` now has `wait_s=HEAVY_WAIT_S` (was unbounded).
- Worst case: row `pcb_check (full)` 495 s added to the Global Constraints table; `run_drc`
  gains ≤ `HEAVY_WAIT_S`; a render gains ≤ `HEAVY_WAIT_S` when it has to run the marker DRC.
  The gate is process-wide (all projects): cross-project serialisation of parity DRCs and
  Freerouting runs is intended (memory is a host budget).
- Tests: `tests/unit/board/test_heavy.py`, `tests/unit/test_checks_parity_gate.py`,
  `tests/unit/test_render_markers_gate.py`, additions in `test_freerouting_unit.py`
  (`test_freerouting_holds_slot`, `test_freerouting_gate_timeout_is_timed_out_run`; the old
  slot test now patches the gate out), `test_check_parse.py`
  (`test_parity_gate_not_held_during_copy`, `test_cli_parity_bounded_waits`,
  `test_quick_check_runs_during_parity`).

**CPU-worker recycling** (`worker.py`): `WORKER_RECYCLE_MB = 512`; after a finished job (also
one whose function raised — deviation: the worker is alive and may be big either way; not
after a `BrokenProcessPool`, which already replaces it) the worker's `VmRSS` from
`/proc/<pid>/status` is read and above the threshold the pool is killed and replaced **before
the lock is released**; one `log.info` line per recycle. The next job pays the spawn start
(≈ 1 s) inside its own `timeout_s`.

**Concurrent peak in `odh-dry-run.py`**: `PeakSampler.samples` (per 0.5 s sample, Σ VmRSS by
kind over every classified process), `mark(label)` from the flow's `on_step` and from each
after-failure call, `step_at` / `labelled()`, pure `concurrent_peak(samples)`; `Memory` gains
`concurrent_mb / _step / _parts`; the report prints "Concurrent peak (lower bound …)" with step
and shares and "Sum of peaks (upper bound)" (replaces "Sum per active board project"), the D5
line compares both.

**ODH re-measurement** (2026-11-27, copy of the PROD checkout `OpenDriveHub@2e17ac5` mounted
read-only, kicad-base `52b43fb8…` + fetched Freerouting 2.5.0, 80 × 100 mm, 4 layers
`jlcpcb-4l`, Power class 0.3 mm, blocks = `pcb_analyze`'s proposal; answers outside the repo):
flow failed in turn 4 as expected before R2–R4 (fill 39 % refused, retry routed 136/138, the
final `pcb_route` left `no_path` on `/MCU/USB_DP_C` J302.A6–B6 and `/Power/VSYS` at U201);
the after-failure calls (full check, renders, STEP, GLB) all ran. 546 s total.
- **Concurrent peak (lower bound): 6,052 MB ≈ 5.91 GB**, in the after-failure `pcb_check
  full`: api-server 3,150 MB + **parity DRC 2,599 MB** + the idle CPU worker 303 MB (no JVM,
  no other kicad-cli at that moment — the gate worked; the flow is sequential, so no gate
  waits were observed in this run).
- **Sum of peaks (upper bound): 7,048 MB ≈ 6.88 GB** (api-server 3,154, JVM 624, CPU worker
  VmHWM 569 — one worker, it never ended a job above 512 MB, so no recycle fired; P9a's
  third run had 1,017 MB — raster 103, kicad-cli peak 2,599 = parity DRC; others: netlist
  585, 3D render 338, svg 223, dsn 117, step 114, glb 112).
- **D5 (≈ 5 GB): concurrent peak ABOVE by ≈ 0.9 GB.** Serialising cannot go lower: the
  api-server (≈ 3.15 GB with footprints) and the parity DRC (≈ 2.6 GB) are both needed at the
  same time. Without the idle worker it would still be ≈ 5.75 GB. → **owner decision**
  (round-2 plan call 9): accept ≈ 6 GB per active board project, or a later own parity check
  on the `kicad-cli` netlist (≈ 0.6 GB → ≈ 4 GB concurrent). Not implemented here.

Residuals: R7 re-measures on the final round-2 flow; the idle worker (≈ 300 MB) could be
shut down after an idle period (not planned); `MAX_CONCURRENT_RUNS` could go to 1 when
someone touches `freerouting.py` again.

### As built (R1)

**Junction / entry / neck / corridor geometry** (`cc/board/geom.py`, B-C5 round 2) and the
§22.5 metric changes (`cc/board/metrics.py`, B-C7 round 2). Pure Python; no KiCad change.

- **`junction_kind` / `BendReport.y_junctions`** as contracted. `y_junctions` is a new field
  with default `()` (appended last, so every existing `BendReport(...)` keeps working).
  `t_junctions` keeps its P3a semantics; its consumers (`chamfer._REASON`, `route._bends`,
  `fill._bends_of`) are untouched (R3/R4 switch them). Junctions are built from straight
  tracks only (arcs are not in `analyse_tracks`' graph, as before; arcs stay M4
  non-octilinear). A Y at a via or inside a pad is exempt, like the old T. Note: a
  **degree-2** 90° vertex at a via is still reported by M4 `ninety` (today's behaviour; the
  phase file's Review Focus 2 wording "exempt as today" refers to junctions only).
- **Contract extensions (keyword-only, defaults keep the contracted call):**
  `straight_corridor(a, b, width, *, copper_layers=("F.Cu", "B.Cu"))` — the pads carry no
  board layer list (`*.Cu`, plated THT), so the caller passes `model.copper_layers` (M16 does);
  `analyse_entry(points, width, zone, *, in_pad=None)` — the pad test for "bends inside the pad
  itself are ignored" (M16 passes `_in_pad` of the end's pad). `escape_zone` falls back to the
  pads' bbox when a part has no courtyard (`()` without pads either).
- **Corridor rule:** the two pads' axis-aligned extents (rotated pad rectangles) must be
  apart by `0 < gap ≤ ADJ_GAP_MM` along one axis and overlap by `≥ width` across it;
  overlapping pads → `None`. Start lies on `a`'s edge, end on `b`'s.
- **Entry analysis:** vertices are walked from `points[0]` until the first vertex outside the
  zone; loop = |signed cumulative turn| ≥ `LOOP_TURN_DEG − ANGLE_TOL_DEG` (reported once, at
  that vertex; an acute vertex alone is a loop); jog = two consecutive 45° bends of opposite
  sense (reported at the second; a 90°/off-angle vertex in between resets the pair).
- **`routed_path`**: `_NetGraph._search` (one Dijkstra with predecessors) backs both
  `length` (unchanged values) and `path`; `_NetGraph` now records each edge's track width
  and arc mid; an arc on the path appears as its two chords.
- **M3:** a `text_thickness` marker whose KIIDs **all** map to footprint items of role `shape`
  → offender `silk_line_warning: …` (verdict at most `review`, not counted). A marker that also
  names a text item, or the footprint's own KIID, stays hard.
- **M4:** the `t_junction` offender is gone (threshold text without "T").
- **M11:** pieces of a signal track inside the antipad disc (same reach as the antipad test:
  via radius / pad half-diagonal + `ANTIPAD_MARGIN_MM`) of its **own** net's via or plated
  pad are cut out before the void test.
- **M12:** a narrow piece passes when all its points (track start/end; arc start/mid/end) lie
  in the escape zone of a part with a copper pad of the same net whose `neck_width` (class
  width, preset `min_track`; without a preset `jlcpcb-4l`) is below the class width, and the
  piece is ≥ that neck width (− `M12_WIDTH_TOL`).
- **M15** offenders `y_junction` per node. **M16** (every non-ground net, its MST
  connections): corridor width = the narrower of the two pads' neck widths; corridor free =
  no other-net copper pad on that layer and no other-net via within `w/2 + clearance` (net
  class clearance, else the board's `min_clearance`), no track/copper keepout zone on that
  layer and no footprint rule area (every layer, like the router) touching it; dogleg = a free
  corridor layer exists and the routed path, after dropping pieces wholly inside either pad
  and merging collinear same-layer touching pieces, is not one segment on a free corridor
  layer. Entry loops/jogs on the first and last same-layer run of each routed path when it
  starts inside the end's pad. Placement stage: `alignable` when at least one of the two parts
  has exactly two pads (covers "2-pad part next to the IC pin it serves", §22.3), they share a
  copper layer, are adjacent along an axis and the missing overlap is `0 < need ≤ 1.0 mm`
  (offender value = the shift). M16 `value` = hard offenders (doglegs + loops).
- **Switches off → no hard text:** while `M15_HARD` / `M16_HARD` are `False`, `Metric.hard` is
  `None` and the hard text moves into `review` ("… (hard from round 2 R4)"), so the agent is not
  told a hard threshold that does not fail yet. R4 flipping the switches restores the hard text
  (no other change needed). Unit-tested (`test_switches_default_review`).
- **Golden (`sp4_synth_metrics.json`, regenerated on `b28ad7f` + R1):** M11 verdict
  `review → pass` (value 0.9588 unchanged: the "void crossing" offenders were the P7/P9a
  own-via artefact); **M15 = 2** and **M16 = 15** (`review`). The owner-marked defects appear (offender list of a
  local run with M15 = 3 / M16 = 16): doglegs D1.2–R4.2, R4.1–U1.10, R5–R6/U3 FB, C8/C9–L1, U1.16–C2, C10–U3 VBST, U3.2–L1 SW,
  C4–Y1, J2–U1 SWCLK, J1–U2 USB_DN; pad loops at J1 A6/B6/A7 (USB-C connector pads of one
  net); pad jogs at C5.1, U1.16, U1.2, U2.5 (review only); Y-junctions on +3V3, /Power/FB,
  /USB_DP. **R3/R4 must bring M15 and M16's hard count to 0.** Tracks/vias/fill unchanged.
- **Golden test change (minimal, P9a/R7 file `tests/kicad_e2e/test_synth_e2e.py`):** the
  fill's run-to-run variance moves M15 between 2 and 3 and M16 between 15 and 16 on the same
  code (observed in 3 local runs on this host). While a switch is off, those values are
  **recorded only** (`_recorded_only()`; verdict still exact). With the switch on, R4's golden
  compares them exactly again (as every metric with a hard threshold).
- **Environment note:** under heavy host load (parallel children) the unmodified `main` flow
  took the "nothing to fill" path once (172 tracks / 126 vias, `fill_seconds` null): the own
  router's deadlines depend on CPU time. Golden regenerated on an idle host (fill ran,
  174 / 124 as on `main`).
- Docstring-only "M1-M14" → "M1-M16" in `kicad_tools/board/analysis.py`, `board/jobs.py`;
  `check.py` / `kicad_tools/board/fab.py` (R6 / R5 files) left alone. Tests that pin 14
  metrics updated (`test_metrics_rules`, `test_analysis_unit`, `test_metrics_routed`,
  `tests/kicad/board/test_metrics_tool.py`, `test_zones.py`).
- **Residuals for later phases:** R3 — the router's `junction_masks` should use
  `junction_kind`; corridor-first should call `straight_corridor(..., copper_layers=…)`. R4 —
  switch `M15_HARD` / `M16_HARD` (and `_recorded_only()` then pins M15/M16 automatically);
  `chamfer._REASON` still rips `t_junctions`. R2 — `alignable` counts are the placer's target.
  Timing: M16 adds one Dijkstra per MST connection of every non-ground net (memoised with
  M5/M6's); the 2 400-net timing test stays inside its budget. A single supply net with
  1 000–2 000 pads costs ≈ 1.3–3.2 s (O(P²) MST + per-pair search; review round 2) — fine
  for real boards (ODH: 76 parts), revisit (one search per source pad) if a board needs it.

### As built (P9a)

**End-to-end golden on `sp4_synth`** (`backend/tests/kicad_e2e/`, marker `kicad_e2e`, CI job `kicad-e2e`)
- `flow.run_flow(rig, answers)` drives the product flow turn by turn through the real adapter
  (`agent.sdk_tools`): the `Rig` is the run engine's wiring without a model - `HistoryService` +
  `HistoryHooks` (turn start, first-mutation snapshot, turn-end autosave), `EditingService` with
  the plan gate, `DesignDocs`, a **Board** conversation (`conversation_type="board"`, the doc write
  scope), `reload_project` after board creation. Turns: 1 sync / outline / rules; 2 `pcb_analyze`
  → `pcb_define_blocks` → `pcb_place_block` per block → `pcb_legalize` → `pcb_check quick` +
  `pcb_render placement`; 3 the user's message + `pcb_approve placement`; 4 `pcb_route` (critical
  nets, locked) + `pcb_route_fill` with the scripted ladder; 5 zones / fanout / stitch / tidy silk /
  `pcb_check full` (+ the user's accepted warnings, then a second full check) / renders top +
  bottom; 6 the user's message + `pcb_approve board` + `pcb_fab_export` - **without P8's
  `metrics_pass` seam** (the real export-time gate: 0 hard failures). A call that would pass the
  turn budget (20) starts a new turn. Any tool error → `FlowError(step, tool, error)` carrying the
  partial `FlowResult`.
- **Scripted ladder (spec §10.3)**: a fill refused by the 20 % rule → rung (c) `pcb_route` on the
  nets it would have ripped, then the fill again (≤ 3 fills); "nothing to fill" ends the rung; a
  Freerouting timeout → the tool's suggestion, once: `timeout_s 600, passes 10`; connections open
  after the last rung → `FlowError` (rung (d) = the user). On `sp4_synth` the first fill is refused
  (`/Power/FB` detour, 1 of 2), rung (c) routes it, the second fill fills the rest.
- **Accepted warnings (`Answers.accept_warnings`)**: KiCad 11 applies the board's
  `min_silk_text_thickness` (JLC preset 0.15 mm) to footprint silk **lines** too - every KiCad
  library footprint draws 0.12 mm silk (KLC), so `text_thickness` warnings make M3 fail on any
  stock-footprint board. The scripted user accepts that type: the flow writes a `board.md` →
  Checks row (`doc_patch_section`, `create: true`; Board sessions may not `doc_write`) naming every
  part, then checks again. No threshold changed (M3 counts accepted warnings by design, spec §12).
  **Owner decision (open):** keep it as a per-board acceptance (P9b skill: the agent asks), or map
  only the text stroke (spec: "silk text ≥ … / 0.15 mm line") and not the KiCad constraint that
  also covers silk lines.
- `sp4_synth` answers (`SYNTH_ANSWERS`): 60 × 45 mm, r 1, holes inset 3; 4 layers `jlcpcb-4l` +
  Power class 0.4 mm (`*VBUS`, `*+3V3`, `*SW`); blocks keyed by anchor: J1 `edge: left` (absorbs
  the ESD part U2's block), U1 right half, U3 left half; channels 1.5 mm; default zones; fanout
  and stitching on GND.
- **Measured:** the flow ≈ 110–120 s locally (`pcb_route` critical 27–56 s, the fill ≈ 11–15 s,
  sync ≈ 9 s, full check ≈ 5.7 s each), the module ≈ 120 s, CI job `kicad-e2e` 3 min 12 s;
  `test_full_flow_hard_gates` asserts ≤ 180 s. Two consecutive local runs identical (golden).
- **Golden** `goldens/sp4_synth_metrics.json` (CopperChat output, `UPDATE_GOLDENS=1`): hard
  metrics M1 0, M2 0, M4 0, M7 0, M12 0, M13 0, M14 0 (pass); M3 0 (review: accepted warnings);
  M5 1.11 (review: USB pair fan-in, `/Power/FB`), M8 **4.97 mm** (review; crystal Y1.1 → U1.2 -
  0.03 mm under the hard limit), M9 3, M11 0.959 (review, see residuals); 174 tracks, 124 vias,
  239.3 mm; `fill_seconds` 12.3 (recorded only).
- `test_approvals_bound_to_hashes`: both checkpoints current after the flow; `pcb_move` of a
  routed passive (+0.5 mm, `unroute: true`) makes both stale and `pcb_fab_export` refuses
  `not_approved`. `test_undo_to_placement`: History restore of turn 4's first-mutation snapshot →
  the approved `placement_hash`, no track/via, `pcb_status` answers.

**Tool fixes made here (owning phases' files; every hard gate that failed on `sp4_synth` was a
tool defect, thresholds untouched)**
- **P4 placer (`placer/layout.py`):** a satellite's pads are measured to the host pads **of their
  own net** (the crystal sat crossed: its OSC_IN pad at the OSC_OUT pin → M5 3.40 / M8 8.6 mm);
  crystals keep `CRYSTAL_STANDOFF_MM` **0.3** more than the courtyard gap (flush, the crystal
  boxed in the reset pin → M2). The value sits in a **narrow window** on `sp4_synth`: 0.2 mm left
  the supply pin's decap path 5.40 mm, 0.4 mm made the far crystal pad's path 5.07 mm (M8 hard
  5 mm). Tried and rejected: decaps before the crystal (crystal path 6.1 mm), load caps right
  after the crystal (decap 6.05 mm), supply nets routed before crystal nets (no change).
- **P5a/P5b router (`router/astar.py`, `router/grid.py`):** two connections of one TSSOP pin shared
  the exit stub and turned NW / SW 0.16 mm outside the pad - an off-pad 90° T (M4 hard fail).
  `ObstacleGrid.own_segments` / `own_spots` (own-net tracks per layer, own pads + vias); the search
  skips every move perpendicular to existing own-net copper at a node outside own pads / vias
  (`junction_masks`, a per-layer node → direction bitmask), and `valid_path` checks the snapped
  path the same way (`junctions_ok`: path vertices, own track ends on the path, proper
  crossings; M4's 2 µm join tolerance; own pads of the layer and vias exempt). A
  same-net 90° X-crossing is refused too - stricter than M4, which does not split crossings.
  Masks are cached per grid, each segment is clipped to the window and checks only the spots
  near it, with deadline checks. A goal-only check made `pcb_route` take 196 s and fail (A*
  kept reaching the goal through the same T prefix). The move masks exempt only existing own
  pads / vias: a new via dropped on an own track with a 90° leg is pruned during the search
  (stricter than M4 and than `valid_path`'s final check, which exempts the path's own vias) -
  never wrong copper, at most a detour.
- **P3a M12 (`metrics.py`):** a net whose class KiCad names "Effective for net: <net>" now takes
  the classes whose patterns match the net + Default (widest), like `route.class_params`; such
  nets were skipped (closes the P2/P5b/P6b residual).
- **P3b `pcb_check` rows (`check.py`):** warning/error rows map marker KIIDs through the
  footprints' `item_kiids` too (pads, fields, texts, shapes), so their `refs` / `accepted` agree
  with M3 (closes the P3b residual).
- **P2 test (`test_sync.py`):** asserts the API DRC name `overlapping_footprints` (closes the P4
  residual).

**ODH dry run** (`scripts/odh-dry-run.py`, copy of `OpenDriveHub@2e17ac5` + the uncommitted PROD
working tree, transmitter main board, 76 parts; answers outside the repo; local kicad-base image)
- **80 × 60 mm** (brief size): placement fails - `pcb_place_block` `does_not_fit` for the 4th/6th
  block although > 3,000 mm² are free: block-by-block packing fragments the board (render handed
  to the coordinator). With per-block regions as answers (MCU right half first, RTC, charger,
  buck-boost left) it still left no contiguous spot for the SD-card block.
- **80 × 100 mm** (spec size; Power class 0.5 mm): sync 7–9 s, 12 blocks placed in 14 s, M1 pass,
  M8 worst 3.65 mm (placement), max 19 pins per 5 × 5 mm cell, placement approved. `pcb_route`
  critical (≈ 40 nets) **105 s, 13 connections `no_path`** - all on power nets at the 0.5 mm-pitch
  QFN charger / buck-boost (adjacent same-net pins, VSYS / VBAT / VBUS / switch node): a 0.5 mm
  class track cannot enter 0.25 mm pads and the own router has **no neck-down at pads**. Fill:
  30 passes did not finish in 300 s (≈ 24 s per pass, stalled at ≈ 31 unrouted / 24 violations);
  10 passes / 600 s twice refused by the 20 % rule (15/60 then 11/35: `ninety_unfixable` 11 / 6,
  `drc_rejected` 4, detour 3, micro-jog 1, acute 1); rung (c) routed the refused signal nets each
  time; flow stopped after ≈ 1,000 s with the ladder exhausted. Full check of the partial board:
  M2 118 open, **M3 347 (incl. `drill_out_of_range` errors: 0.2 mm holes in a footprint vs the
  JLC preset's 0.3 mm), M14 90 `pth_drill` U202 0.2 < 0.3**, M8 5.47 mm (C201 → U301 routed).
- **80 × 100 mm, Power class 0.3 mm** (third run): `pcb_route` critical 223 s, **2 `no_path`**
  left; fill 30 passes refused at once (25/58 = 43 %: `ninety_unfixable` 20, micro-jog 3, detour
  2), after rung (c) a 300 s timeout, then 10 passes / 600 s refused again (5/18: `ninety_unfixable`
  4); ladder exhausted after ≈ 1,020 s; partial board: M2 93 open, M5 hard fail (`detour_fail`
  ratio 4.0, +8.6 mm), M3 / M14 the 0.2 mm holes, M8 4.07 mm (review). **The ODH flow does not
  reach a routed board** - the acceptance finding for the owner: Freerouting's output on the
  dense ODH placement is mostly unchamferable (90° corners in tight spots), so the owner's 45°
  rule plus the 20 % rejection rule stop the fill.
- **Memory (D5) — the number P9b writes into the README**, per active board project, five parts
  (80 × 100 run): (1) api-server RSS (peak VmHWM) **3,118 MB** (unchanged by
  `activate_footprints()`), (2) Freerouting JVM RSS **649 MB** (683 MB in the first run),
  (3) CPU worker VmHWM **613 MB**, (4) raster worker peak **106 MB**, (5) kicad-cli peak
  **2,616 MB = the parity DRC of `pcb_check full`** (others: netlist 590, 3D render 339, glb 129,
  step 123, dsn 73, svg export 70 MB; gerbers / drill / pos not reached). **Sum 7,102 MB ≈ 6.9 GB
  - above the D5 figure of 4.5–5 GB** (without the parity DRC: 4.98 GB). Owner decision needed
  for P9b's README / PROD sizing. Third run (0.3 mm class): api-server 3,157, JVM 596, CPU
  worker **1,017**, raster 106, kicad-cli (parity DRC) 2,531 → **7,406 MB ≈ 7.2 GB**.
- Render limits on a large 4-layer board (P1c residual): placement view SVG 786 KB / 14,465
  elements, top view 475 KB / 5,680 elements, raster ≤ 106 MB RSS, `pcb_render` 2–5 s - far below
  `BOARD_SVG_LIMITS`. **Render extent finding:** the ODH views include an item at (7, 0.5) mm far
  outside the outline (the board occupies a third of the image) - P1c `render_board` extent / P9b.

**Residuals for P9b / later**
- M8 on the golden is 0.03 mm under the hard limit; a Freerouting / router change can flip it -
  the golden then fails loudly (good), but the placer's crystal window is narrow. A placer that
  reserves escape room per pin (or routes crystal + decap together) would be sturdier.
- Own router: neck-down into fine-pitch pads for wide classes (ODH power nets), or the skill tells
  the agent to give power classes ≤ the pad width at QFN pitch. Freerouting on ODH: ≈ 24 s per
  pass, many unchamferable corners - more room between blocks (P9b skill: channels ≥ 2–3 mm when
  the board has room) before the fill.
- `pcb_place_block` packing fragments small boards (ODH 80 × 60): place the biggest blocks first
  and/or a whole-board packing pass; P9b skill: order blocks by area, give regions.
- `MAX_CELL_PINS` stays 30: ODH's dense placement measured 19 pins per cell, `routability_ok`, yet
  routing failed for the width reason above - no evidence for a tighter value.
- M11 "void crossing … outer" offenders at the critical nets' own signal vias stay `review` on the
  golden (P7 residual confirmed on `sp4_synth`; ODH never reached zones) - exempt a track's piece
  inside its own via's antipad (P3a file) in a later phase.
- Freerouting's "unrouted" reading of locked pad-to-via links (P6b residual): not isolated on ODH
  (the fill never completed there).
- JLC preset vs real footprints: 0.2 mm holes (QFN thermal vias / module) fail M3/M14 against the
  conservative 0.3 mm minimum (JLC allows 0.15–0.25 mm): owner decision (custom rule exception per
  board, or preset value).
- P8 residual (holes in BOM/CPL) unchanged - P9b skill.

### As built (P6b)

**Probes** (`tests/kicad/board/test_fill_roundtrip.py`, `record_property`; local kicad-base `52b43fb8…` recipe
`dc93f472…`, Freerouting 2.5.0)
- **SES import:** locked copper keeps its KIIDs; **unlocked copper is deleted and not re-created** (Freerouting's
  re-route of it comes back with new KIIDs); no footprint moved (`positions_restored` = 0 on every run, the
  position restore stays as a safety net). `ImportSpecctraSession` **is staged by an open board commit** (a
  dropped commit leaves no trace) → stage 3 `import` runs with `commit=True` (plan: `commit=False`); "own undo
  entry" is therefore moot.
- **Zone fills are clearable** (`update_items` with `filled = False` and no fill polygons → `filled_area_mm2` 0):
  stage 1 clears, its `undo` and a last stage 8 `refill` (`sctx.refill(REFILL_TIMEOUT_S)`) refill; no zone is
  ever removed. **Deviation:** the refill moved from stage 4 to the end (review round 2), so the zones see
  the post-processed and re-routed copper; still one refill, the worst case is unchanged.
- **The API DSN job is stale (new gap, like G2):** `RunBoardJobExportSpecctraDsn` returns the board of its
  *first* run on every later export, also after a save (`test_probe_api_dsn_job_stale`). The fill therefore
  writes a board copy into its scratch dir (`save_as(include_project=False)` under the session lock -
  `KicadSession.save_copy` takes the apply lock and cannot run inside a stage), byte-copies `.kicad_pro` +
  lib tables (P3b's `pcb_check full` precedent) and runs `kicad-cli pcb export specctra` on it (under
  `BOARD_CLI_LOCK`). Copy + kicad-cli + SES import together share `IO_BUDGET_S` (measured ≈ 1-2 s on `sp4_synth`;
  kicad-cli's own timeout = the remaining budget). `api.export_specctra_dsn` stays (unused by the fill).
- **Locked copper is exported as `(type fix)`**, not `(type protect)` (plan); Freerouting keeps both
  (`test_dsn_marks_locked_as_fixed`). KiCad's DSN job/CLI exports **no zones and no rule areas**.
- **Plane-layer mechanism = (b') DSN `autoroute_settings` layer rule** (`PLANE_LAYER_MECHANISM =
  "dsn_autoroute_settings"`, owner-D4 last resort): (a) Freerouting 2.5.0 has no CLI option for one layer -
  `router.layers` is an array and indexed CLI paths (`--router.layers.1.routable=false`) are rejected
  ("Unknown settings property"), a `-de` JSON is read as a board; (b) the plan's `(type signal)` → `(type
  power)` rewrite has **no effect** in 2.5.0 (signals still routed on a "power" layer). `disable_layers` inserts
  exactly one line `(autoroute_settings (layer_rule In1.Cu (active off)))` before the structure's `boundary`;
  nothing else changes (`test_dsn_layer_rewrite`, `test_probe_plane_layer_mechanism`: 0 wires on In1.Cu).
  **Plane nets** (review rounds 4/5 + verify) = ground nets + nets whose copper zones cover ≥ 80 % of the
  outline on one **inner** layer (summed per net and layer, so a split plane counts; a small local pour and
  outer pours - P7's default stack pours GND on F.Cu/B.Cu too - do not):
  their connections are not open for the fill and Freerouting's copper on them is deleted. **Plane layers**
  default = every layer of such a plane (ground or supply), plus the owner-D1 default (4 L `In1.Cu`, 6 L
  `In1.Cu` + `In4.Cu`, 2 L none) while no ground plane exists yet; outer layers refused as plane layers.

**SDK tool-call limit (Task 4 Step 2)**
- Bundled CLI (claude-agent-sdk 0.2.168): an MCP tool call's timeout = per-server `timeout` ?? `MCP_TOOL_TIMEOUT`
  ?? **1e8 ms** (≈ 27.8 h); `sdk` servers have **no idle timeout**; a `tools/call` control request to the Python
  SDK carries no control timeout (other MCP messages 70 s). Measured: a **1070 s** in-process tool call returned
  normally (mock Messages API driving the real bundled CLI - no model credentials in the build environment;
  `tests/live/test_tool_duration_live.py`, opt-in `COPPERCHAT_TOOL_LIMIT_PROBE=1`, also a real-model variant).
- **L ≥ 1070 s ≥ 1005 s + 60 s → no `MCP_TOOL_TIMEOUT`, no `limits.py`, no capped deadlines; `pcb_route_fill`
  stays enabled.** `FILL_WORST_CASE_S` 1005 / `FILL_FIXED_S` 405 as planned.

**Measured on `sp4_synth`** (routed rig + 6 unrouted signal nets + `/Power/FB` own-routed): one call ≈ 10-13 s
(Freerouting 8-11 s, 30 passes), completion 1.0, 6 fill connections, 0 ripped, ~70-110 new ground items and
11-15 redundant items deleted, M4 = M7 = M12 = 0. Render: `/tmp/sp4-renders/sp4_synth_p6b_filled_{top,bottom}.png`.
`POST_DEADLINE_S` 120, `IO_BUDGET_S` 60, re-routing through P5b's `route_connections(…, lock=False,
deadline=post_deadline)` with `plan_connections` on the ripped nets (plane layers excluded).

**Contract deviations / additions**
- B-C1: `profiles.FILL_TOOL`; both call sites pass `freerouting=settings.freerouting` (fake: `True` without
  settings).
- **Redundant copper:** Freerouting reads pad-to-via links of locked work as open (its log: "N unrouted";
  19-20 "pre-existing clearance violations" of fixed P5b copper) and routes them again. Stage 4 also deletes the
  new copper of non-ground nets whose connections the old copper alone still connects (`redundant_items_deleted`).
  Side effect: Freerouting's optimizer never runs on such boards ("only on completely routed boards").
- **Width fix:** Freerouting necks traces down at pins (0.15 mm on a 0.2 mm class; `--router.automatic_neckdown
  =false` is accepted but did not change it). Stage 5 widens every fill/replacement track below its net's class
  width (`route.class_params`) as part of the replacements (DRC-checked; rejected → originals back + ripped);
  report `widened`.
- Rip keys come from `postprocess`; a DRC-rejected replacement reverts **every replacement of its net** (chamfer
  pairs share legs) and rips that net's connections through the originals (`_conns_using`, chamfer's `_Paths`).
  The fill copper of a ripped connection = the unlocked new tracks on its shortest path + new vias left without
  a track end. The 20 % rule counts only fill connections (open before, routed after); rips of other nets
  (relocked / re-created copper) are re-routed but not counted. `rip_ratio` details add `connections` (≤ 50);
  the success report adds `ripped_connections`, `rejected` [{connection, reason, x, y}], `connections {routed,
  total}`, `open_before`, `fill_connections_before`, `io_seconds`, `fills_cleared`.
- A post-processor `run_cpu` timeout (`board_compute_timeout`) fails the call (fill restored), since unchecked
  Freerouting copper must not stay. `test_fill_metrics_via_run_cpu` passes the post deadline right after the
  post-processor (fake clock) instead of `POST_DEADLINE_S = 0` (which would time out the post-processor itself).
- `relock_nets` with no open connection is a valid call (ladder rung (b)); "nothing to fill" only when nothing is
  open and `relock_nets` is empty.
- A `session.call` timeout of the copy/import restarts the api-server (B-C4) → `KicadUnavailable` (engine), not
  `io_timeout`; only the measured budget and kicad-cli's timeout map to `io_timeout`.
- **P6a residual decided (stricter):** the pad-entry exemption of M7 / the post-processor now applies only when
  the end bend lies inside the pad (`geom.pad_entry_ends`, used by `analyse_tracks` and `chamfer.postprocess`;
  the own router's `valid_path` already did this). Since `polylines` already end at nodes inside pads, a 45° pair
  right outside a pad is now judged like any other pair. `analyse_bends(…, count_ends=…)` keeps the bend-density
  count as before (else the review verdict changed). Normal pad entries are unaffected: P5b's critical set still
  M4 = M7 = 0, verdict pass. **Minimal touch of P3a's `geom.py` and P6a's `chamfer.py`.**

**Residuals for later phases**
- **M5:** the remaining hard M5 failure on the filled `sp4_synth` is P5b's own critical routing
  (`/OSC_IN Y1.1-U1.2` ratio 3.40, +6.07 mm) - the compact placement (P5b residual); P9a/P9b skill: channels.
- On the compact synth placement Freerouting produces detours / unchamferable corners for `/NRST C6.1-U1.4`,
  `/Power/FB R5.2-R6.1`, `/CC1 J1.A5-R2.1`, `/OSC_IN`: a small fill with one of them is refused by the 20 % rule
  (by design; ladder rung (c) `pcb_route` handles it). The kicad tests use 6 nets Freerouting fills cleanly
  (`fill_support.FILL_NETS`) and route `/Power/FB` with the own router first.
- **M12** still skips KiCad's "Effective for net: …" class names (P5b residual, P3a file) - not fixed here; the
  fill's own width fix uses `route.class_params`.
- The DSN sees `.kicad_pro` as saved on disk (the adapter saves after every tool call, so same-turn rule changes
  are included); the API DSN job staleness could be fixed upstream (KiCad patch, owner) - then the copy path could
  go.
- Freerouting's "unrouted" misreading of locked pad-to-via links (and the skipped optimizer) deserves a look in
  P9a on OpenDriveHub (pad/via geometry or the DSN resolution).

### As built (P8)

**Measured / recorded** (`tests/kicad/board/test_{fab_export,export_3d}.py`, local kicad-base
image `52b43fb8…`, `record_property`; `fab_rig` = `copper_rig` (P5b `routed_synth`) + the
default GND stack, 60 × 45 mm, `jlcpcb-4l`)
- **`pcb_fab_export` on `sp4_synth`: 2.7 s wall**, package 404,430 B, upload zip 70,398 B.
  STEP 52,288 B in 1.0 s, GLB 62,092 B.
- **Files** (`fab/jlcpcb/`): `gerbers/sp4_synth-{F_Cu.gtl, In1_Cu.g1, In2_Cu.g2, B_Cu.gbl,
  F_Mask.gts, B_Mask.gbs, F_Silkscreen.gto, B_Silkscreen.gbo, F_Paste.gtp, B_Paste.gbp,
  Edge_Cuts.gm1, job.gbrjob}`, `drill/sp4_synth-{PTH,NPTH}.drl` (Excellon, `METRIC`) +
  `-{PTH,NPTH}-drl_map.gbr`, `sp4_synth-jlcpcb.zip` (exactly the gerber + drill files, flat
  names, sorted, fixed 1980 timestamps), `bom-jlcpcb.csv`, `cpl-jlcpcb.csv`, `fab-report.md`.
  `fab/3d/sp4_synth.{step,glb}`.
- `kicad-cli` accepts the KiCad layer names as given (`F.Cu,In1.Cu,In2.Cu,B.Cu,F.Mask,
  B.Mask,F.SilkS,B.SilkS,F.Paste,B.Paste,Edge.Cuts`); file names use KiCad's Protel
  extensions. **`--check-zones` refilled nothing** on the copy (no refill message: P7's
  stages leave the fills current).
- **Paste (stencil, owner: hot-plate reflow):** F.Paste has exactly one flash per board pad
  with `F.Paste` (every top SMD pad, paste-only sub-pads included), B.Paste is empty
  (`test_paste_layer_matches_smd_pads`; added, not in the plan).
- BOM: header `Comment,Designator,Footprint,LCSC Part #` (call 17), grouped by
  `Value,Footprint,LCSC,MPN`, designators `R1,R2` (`--ref-delimiter , --ref-range-delimiter ""`
  — kicad-cli's default `-` would write `C1-C3`, which JLC cannot read), footprint without the
  library prefix, 4th column LCSC → MPN → empty. CPL: `Designator,Mid X,Mid Y,Layer,Rotation`,
  `%.4fmm` / `%.1f` from parsed floats (kicad-cli's pos file: y negated as KiCad writes it,
  `Top`/`Bottom`); a non-number or unknown side fails the export (`export_failed`).

**Contract deviations / additions**
- **Export-time metrics gate (closes the P3b residual "board.md edits do not stale the
  approval"):** after `require_board_approved` the export runs `run_cpu(metrics_job, model,
  doc, drc, preset_for(model, doc), only=None, timeout_s=30)` with a fresh `run_drc_task`
  (`drc_call_timeout(60)`) and the **current** `board.md` (no parity: the full check's parity
  evidence is covered by `board_written_since(any_kind)`); any hard failure → `not_approved`
  with `details {reason: "check_failed", hard_failures, failing: [metric ids]}`, before
  anything is written. The same report fills `fab-report.md` (one `run_cpu`, as planned).
- **`sp4_synth` cannot pass that gate before P6b/P9a** (only the critical nets are routed:
  M2 etc. fail), so the happy-path kicad tests use a `metrics_pass` seam: the real
  `run_cpu(metrics_job, …)` runs, its report comes back with `hard_failures = 0`;
  `test_export_metrics_hard_failures_refused` checks the real gate on the rig. **P9a** should
  export the fully routed golden without the seam.
- `swap_in(...) -> str | None` (the warning when removing the old package failed; the result's
  `notes` carry it) instead of `None`; `cc/board/paths.py` also exports `new_call_id()` and
  `export_failed(why)`. `safe_output_dir` accepts only `fab/<name>` (anything else
  `output_path_unsafe` reason `outside`), creates `fab/` only; `pcb_export_3d` creates
  `fab/3d` itself and re-validates it. `staging_dir` refuses an existing dir. Every rename /
  rmtree is preceded by a guard: the path lies in a real (lstat, non-symlink) `fab/` dir,
  directly or one level below.
- `fab_report(..., board_file, swept=())`: two keyword arguments more than planned (the report
  names the board file; the "Swept leftovers" line). Custom rules come from
  `custom_rules(model.dru_text)`: probed — KiCad writes a `pcb_set_rules` reason as a `#`
  comment line **inside** the rule, below its header (a block right before a rule is read
  too). Surface finish: ENIG when the finest SMD pad pitch of one part (stacked pads < 0.05 mm
  apart excluded) is below 0.5 mm, else lead-free HASL; stencil `top only` / `bottom only` /
  `top + bottom` / `none` from the pads with `F.Paste` / `B.Paste`; paste-only aperture pads never count as a pitch.
- Tool module: the whole export runs under `ctx.editing.edit_lock(project)` (plain `ToolSpec`
  handler; the adapter still takes the turn snapshot because `mutating=True`); model + DRC
  under `session.applying()` like `pcb_check`; `save_copy` into `fab/.jlcpcb.tmp-<id>/src/`,
  then the copy's `board_hash` re-read (`changed_during_export`); file work off the loop in
  shielded threads; kicad-cli outputs read back as regular files ≤ 8 MiB (`O_NOFOLLOW`). The
  3D export compares the copy's hash with the start hash (`export_failed`), checks the
  output's magic bytes (`ISO-10303-21` / `glTF`) and replaces `fab/3d/<board>.<ext>`.
- Result extras: `pcb_fab_export` adds `preset`, `hard_failures`, `reviews`, `seconds`;
  `pcb_export_3d` adds `swept`, `seconds`.
- Tests: `fab_rig` (session scope) reuses P7's `copper_rig` + `ensure_default_zones` instead
  of a module-scoped `routed_synth` (one server per shard); `approve_board(placed, db,
  hard_failures=)` writes the checkpoint + full-check rows (`conftest.py`, appended). The
  stale-approval and concurrent-move tests move a **mounting hole** (no copper; moved back
  exactly) instead of `pcb_move … unroute: true` of a routed part, which would strip the shared
  rig's routing; the "write that bypassed the lock" moves it through a raw `update_items`
  inside the patched `save_copy`.

**Residuals for later phases**
- `sp4_synth`'s schematic mounting holes **H1/H2** have no "exclude from position files"
  attribute, so they are in the CPL (the export honours KiCad's attributes; only the
  tool-placed holes are excluded). P9b's skill: tell the agent to check BOM/CPL for
  hole/fiducial/test-point rows; JLC assembly itself stays unvalidated (spec §2).
- The CPL is KiCad's pos data as is (no JLC rotation offsets, no origin shift); the BOM's 4th
  column needs an `LCSC` (or `MPN`) symbol field — the fab report says so.
- The export-time metrics run without parity: a schematic change between the full check and
  the export is caught only through `board_written_since(any_kind)` (P3b), not by re-running
  parity (≈ 9 s more per export).

### As built (P7)

**Measured / recorded** (`tests/kicad/board/test_{zones,fanout,stitch,tidy_silk}.py`, local
kicad-base image `52b43fb8…` recipe `dc93f472…`, `record_property`; `sp4_synth` after
`routed_synth`: 60 × 45 mm, `jlcpcb-4l`, 16 critical nets routed and locked)
- **Default stack** (`pcb_zones {action: add}`): `GND_In1` (In1.Cu, 10) + `GND_F` / `GND_B`
  (1), net GND, solid fill. **In1.Cu fill ratio 0.958** (filled area / rounded-outline
  area; bound 0.85); M11 value 0.958, verdict `review` (see residuals). 6 layers:
  `GND_In1` + `GND_In4` (10) + the two outer pours; 2 layers: `GND_B` (10) + `GND_F` (1).
- **`pcb_metrics {}` on the plane-filled routed rig: 0.39 s wall** (`seconds` 0.376; budget
  5 s) - P3a's bucketed M11/M14 hold.
- **`pcb_fanout {net: GND}`: 23 SMD pads, 19 fanned out, 4 `via_pour` (J1.B1, U1.15, Y1.2,
  Y1.4: no legal via spot, connected by the F.Cu pour), 0 unconnected, 1 round, 1.3 s**
  (DRC 0.09 s). Every stub/via locked, stubs exactly octilinear, ≤ 1 mm outside the pad, no
  DRC marker on new items.
- **`pcb_stitch {net: GND}` (pitch 5): 87 candidates, 87 kept, 0 DRC-rejected, 0 dangling,
  1.3 s.**
- **`pcb_tidy_silk {}`: 27 references moved, 1 hidden (U3: no free spot in the dense power
  block), silk markers 94 → 66** (the rest name footprint silk / fab texts, none a
  visible reference); M14 pass afterwards.
- **Refills:** a refill needs **no commit** - KiCad applies `RefillZones` at once (read-back
  and the next DRC see the fills), so every refill stage is `commit=False` and the DRC runs
  after it outside any open commit. No `RefillTimeout` on `sp4_synth`.
- **DRC types used (API names):** `dangling_via` (stitching: counted as
  `removed_dangling`), silk: `silk_clearance` (kicad-cli `silk_overlap`),
  `silk_edge_clearance`, `silk_mask_clearance`, `text_height`, `text_thickness`,
  `mirrored_text_on_front_layer`, `nonmirrored_text_on_back_layer`; everything except
  `unconnected_items` (and exclusions) rejects a new fanout/stitching item.
- Render of the finished synth (top + In1.Cu after zones/fanout/stitch/tidy) handed to the
  owner (P7 done report; `P7_RENDER_DIR` keeps them on local runs).

**Contract deviations / additions** (B-C2 / B-C4 updated in place)
- **P1a files (minimal):** `TextItem.angle` (degrees; read from the text attributes) - in
  `board_hash` **for footprint fields only**: KiCad reports a footprint's free text
  (`${REFERENCE}` on F.Fab) at the footprint rotation on the live board but at 0° after a
  reload (found by `test_outline.py::test_undo_via_history`). `Part.item_kiids` (pad /
  field / text / shape KIID → role) and `Part.silk` (footprint silk graphics), both not
  hashed. Engine: the stage `undo`s now run **between** `restore` and the leftover read-back
  (a `pcb_zones` undo re-creates a deleted zone with its KIID; the old order reported it
  missing → `DocumentInconsistent`).
- **P3a file (minimal):** M3 maps marker KIIDs through `item_kiids` too - **closes the P3b
  residual** (pad / field warnings such as `silk_edge_clearance` can be accepted in
  `board.md`); M14 swaps the text box of 90°/270° texts (**closes P3a's rotation residual**).
- `copper_rig` is **session-scoped** (one `routed_synth` server per CI shard for all four P7
  modules, ≈ 75 s + 55 s once instead of four times; the plan said module-scoped). Each
  module brings the board into its state with the idempotent `ensure_default_zones` /
  `ensure_fanout` / `ensure_stitch` (`conftest.py`); tests restore what they change. P8 /
  P9a may reuse it.
- `planes.py`: repeated pad numbers of one part are keyed `(ref, "<n>#2")` (as P5b's
  connection keys); `fanout_pads` skips pads that already have own-net copper (a track end
  or a via in the pad: `already_connected`); obstacles bucketed (2 mm cells); pads as their
  rotated rectangle, arcs as two chords; `edge_ring_points`, `vias_spaced`, `pad_key`
  public for the tools.
- `pcb_zones`: update/delete store the zones' pre-call protobufs and the undo writes them
  back (re-created zones keep their KIIDs - probe) and refills; a name that matches several
  zones is refused; `outline: "board"` = the rounded-rect polygon; `thermal` switches the
  zone to thermal reliefs (else KiCad's "inherited"); default clearance = the net class
  clearance. Report `zones [{name, action, kiid, net, layers, priority,
  filled_area_mm2}]`, `m11`, `next`. Shared helpers for the other copper tools live in
  `zones.py` (`invalid`, `keys`, `number`, `check_net`, `m11_report`).
- `pcb_fanout`: a pad's candidate that clashes with a via kept or picked in the same round
  (spacing `max(via_d + clearance, drill + hole_to_hole)`) is skipped; reason `no_candidate`
  also when every candidate clashed before reaching the DRC (`all_rejected` = the DRC
  rejected at least one). The 16 rounds × 3 stages are static stages (no-op once no pad is
  open; `fail_after` numbering as planned); the report's `step_report` lists only the
  rounds that ran. Nothing to fan out → no write, cost 0.
- `pcb_stitch`: stage 4 refills only when stage 3 (or a timeout) removed vias, always with
  the full `REFILL_TIMEOUT_S`; report adds `edge_ring_kept`, `drc_seconds`, `seconds`;
  nothing to place → no write, cost 0 and a `note`.
- `pcb_tidy_silk`: text box `len × height + line` wide (M14's estimate, a superset of the
  plan's `len × 0.9 × height + line`); among free spots one off other parts' courtyards is
  preferred (a reference under a part body is unreadable); unresolved refs are hidden in
  stage 1 already; `RefPlacement.candidate` + `plan_refs(exclude=…)` for the stage-2
  re-plan; one `undo` (stage 1) writes back every touched reference field (stored
  protobufs). A read-only DRC in `prepare` gives `silk_markers_before`.
- Review round 1: `pcb_fanout` reports `via_pour` only on a DRC of the final copper
  (`drc_fresh`; one more DRC after a removal or the final refill, skipped when the
  deadline stopped the call → those pads stay `unconnected`); `_pick` and `fanout_pads`
  bucketed; the reference planner bucketed (`_Index`) and run as `run_cpu(silk_plan_job,
  …, timeout_s=30)`; `pcb_tidy_silk`'s verify checks the reference angle; `pcb_stitch
  {edge_ring}` on a non-rectangular outline reports `edge_ring_note`.
- `test_requires_placement_approval` moves a mounting hole 0.5 mm and back (as P5b) instead
  of `pcb_move … unroute: true` + History undo; `test_fanout_requires_approval` uses a
  fresh database.

**Residuals for later phases**
- M11 on the stacked synth stays `review`: "void crossing … over In1.Cu outer" offenders sit
  at the critical nets' own signal vias (the track piece inside the via's antipad is
  located outside every fill polygon) - the plane itself is solid (render). Looks like a
  P3a metric artefact; P9a should confirm on ODH and, if so, exempt a track's piece inside
  its own via's antipad.
- U3's reference is hidden on `sp4_synth` (dense power block, P4 channels 1.5 mm); P9b's
  skill: wider channels or a manual `pcb_move` before `pcb_tidy_silk`.
- `pcb_tidy_silk` moves references only (values / user silk texts stay); the angle of a
  footprint's free silk text is unreliable (see above), so M14's box for such texts may be
  rotated wrongly.
- Fanout candidates are the spike's 8 × 2: fine-pitch / QFN ground pads usually end as
  `via_pour` (needs a pour on the pad's layer) or `unconnected` - no via-in-pad by owner
  rule (JLC cost).

### As built (P5b)

**Measured / recorded** (`tests/kicad/board/test_route.py::test_route_critical_set`, local kicad-base
image `52b43fb8…` recipe `dc93f472…`, `record_property` per net and total)
- Critical set of `sp4_synth` after `placed_synth` (P4 placement, 60 × 45 mm, `jlcpcb-4l`, Power class
  0.4 mm): **16 nets / 37 connections, 37 routed, 0 failed**, 223.8 mm, 18 vias, DRC 0 errors (other than
  unconnected items), M4 = M7 = 0 (`pcb_metrics` and the call's own `bends`), every new item locked,
  `placement_hash` unchanged (approval still valid). **One `pcb_route` call 55.5 s.** Solve seconds per
  net: USB_DN 32.4 / USB_DP 27.3 (the pair's coupled attempt hits the router's 25 s limit, then net by
  net; a pair's solve time counts for both nets), NRST 5.0, +3V3 3.7 (9 connections), VBUS 2.8, OSC_IN
  1.9, all others < 0.5. `placed_synth` setup ≈ 75 s (sync, outline, rules, placement, legalize, render,
  check, approval). Render of the routed critical set handed to the owner (P5b done report).
- Not all-routed before the fixes below: 31–36 of 37 (crossing crystal/MCU pins, the long USB pair, a
  dense MCU block) — the order, the pair fallback, the rip-up retry and the via-pair fix each mattered.

**Contract deviations / additions** (B-C12 updated in place)
- **One stage** `"route"` (`commit=False`, one commit per attempt and per rejection) instead of one per
  net: a blocked connection may take out a connection of *another* net routed earlier in the same call.
  Compensation (B-C4) is unchanged; `test_route_fail_after_1` raises after that stage.
- **Routing order:** `routing_order` routes the requested nets by kind — diff pairs, crystal, switch
  node, supply (short decoupling paths, M8), then signals — each kind shortest unrouted connection
  first; connections of a net shortest first. The report keeps the request order.
- **Default layers exclude the plane layers** (owner D1, plan call 16): no `layers` → every copper
  layer but `In1.Cu` (4 layers) / `In1.Cu` + `In4.Cu` (6 layers). `layer_order(model, kind)`: power
  kinds (supply / switch node / high current) `F.Cu, B.Cu`, then inner layers; others board order.
- **Net class lookup** (P2 residual): KiCad names a pattern-assigned net's class "Effective for net:
  <net>" (not the constituents) — `class_params` maps known constituent names, else the classes whose
  patterns match the net (`fnmatchcase`) + Default; a composite takes the largest width / clearance /
  via. Never below the board's `min_track_width` / `min_clearance`. Before this, Power nets were routed
  at 0.2 mm. (**P3a's M12 has the same lookup gap** — it skips "Effective for net: …" names; P6b/P9a.)
- **Stacked pads** (USB-C VBUS A9/B4 share one position): a connection whose pads overlap on a shared
  copper layer is connected without copper and never planned.
- **Pairs:** a requested pair (`cls.pairs`, both nets requested) is matched connection by connection
  (ends within `PAIR_MATCH_MM` 3 mm); gap = the class's diff-pair gap, else the larger clearance; width
  = diff-pair width, else track width. A pair that fails coupled is routed net by net (spec §10.2). The
  report has `pairs: [{p, n, coupled_connections, uncoupled_connections, length_mismatch, gap_min,
  gap_max, warning?}]` — `warning` when a connection is uncoupled or the mismatch > 1 mm
  (`PAIR_MISMATCH_MM`). On `sp4_synth`: 3 coupled + 2 uncoupled connections, mismatch 3.19 mm (USB FS:
  harmless; listed with a warning). M12 (P3a) does not report pair mismatch.
- **Rip-up retry** inside `route_connections` (B-C12 above): bounded (`RIP_MAX` 6 nearest kept
  connections of other nets within `RIP_HALO_MM` 2 mm of the blocked pads' box, one at a time) and
  exact restore; a second round for what stayed blocked.
- **Router obstacles added in `route.py` (model transform, no P5a file):** footprint-embedded keepouts
  (`Part.rule_areas`, e.g. an antenna) become track + via keepout zones on every copper layer
  (conservative: the model has no keepout type / layer for them), except an area holding one of the
  request's pads; every existing hole (vias and drilled pads, any net incl. the routed one) gets a
  via-keepout octagon of the board's `hole_to_hole_min`, so a new via keeps the hole rule (KiCad
  rejected new vias on top of own-net ones).
- **P5a file touched (minimal):** `router/astar.py::valid_path` rejects a path whose own vias are
  closer than via diameter + clearance — the search used a via, a 0.05 mm hop on B.Cu and a via to fake
  a 135° turn (owner rule!) next to a TSSOP pin; KiCad's DRC rejected the overlapping holes.
- Report extras: per net `seconds` (solve time), call `seconds`, `pairs`; `drc_errors` = errors of the
  last DRC whose copper stayed, other than unconnected items (null when nothing was kept). A taken-out
  connection is restored without a DRC gate (same board state as its accepted DRC); copper is never
  pushed once the deadline has passed. A call with nothing
  to route (all `already_routed`) writes nothing and costs 0.
- `pcb_unroute`: `skipped_locked` (components) plus `skipped_locked_nets`; nothing selected → nothing
  written, cost 0; `unlock_only` with nothing locked → cost 0. Plane nets = classify's ground nets +
  nets carrying a copper zone. `unlock_stage(kiids)` = one commit of `update_items` (`locked = False`).
  `"full"` snapshot only with `include_locked` (delete mode).
- Not-approved test: a fresh database (no checkpoint → `stale: false`) and a 0.5 mm move of a mounting
  hole on the routed module board (→ `stale: true`; moved back → approval valid) instead of a second
  `synth_setup` server (saves ≈ 1.9 GB per shard).

**Shared rig helpers** (`tests/kicad/board/conftest.py`, owned by P5b; later phases append only)
- `placed_synth(rig, db, run) -> {placement_hash, critical_nets}` (sync → outline `SETUP_OUTLINE` →
  rules `PLACED_RULES` (4 layers, `jlcpcb-4l`, Power class 0.4 mm `*VBUS`/`*+3V3`/`*SW`) → `place_synth`
  → `pcb_legalize` → turn 1 render placement + `pcb_check quick` (M1 pass) → turn 2 `user_says` +
  `pcb_approve`) and `routed_synth(...)` (+ `pcb_route` of the critical nets in chunks of ≤ 50, `lock:
  true`; adds `{routed: [chunk data], board_hash}`). The critical set comes from `pcb_analyze scope=nets`
  **after** placement (16 nets on the placed board; P3a's golden lists 8 for KiCad's import positions).
- Module fixtures `synth_placed` / `synth_routed` → `PlacedSynth(rig, db, info)` with `turn(run_id)`;
  each owns one fresh server (`_synth_rig`). The kicad tests share their module server: a test that
  needs a net unrouted removes it with `pcb_unroute {include_locked: true}` (placement and approval
  stay), so no History undo is needed.

**Residuals for later phases**
- The P4 placement of `sp4_synth` is very compact (all blocks in the middle of a mostly empty 60 × 45
  board; the crystal's pins cross the MCU's OSC pins) — routing succeeds only through vias and the
  rip-up retry. P9b's skill / P9a: wider channels, crystal orientation.
- `solve_pair` reaches the 25 s limit on the 23 mm USB pair (fan-in from 1.9 mm pad pitch); the nets are
  then routed alone. A faster coupled search would help P6b/P9a timing.
- Router grid limits remain (P5a): same-path via spacing is now checked at the goal only (a rejected
  goal state costs search time).
- `pair_coupling` reports `gap_max` up to 2.95 mm on fan-in stretches (it samples every parallel
  segment) — P6b/P9a may want "coupled stretch only" figures for M12.
- Footprint keepouts block tracks and vias on all copper layers; a footprint rule area that only
  forbids e.g. footprints or a single layer is over-restrictive until `BoardModel` carries keepout type
  and layers (P1a file).

### As built (P2b)

**Patch 0005** (`docker/kicad-base/patches/0005-TODO-owner-write-message-board-stackup-handler.patch`,
name as planned; owner facts `docs/kicad-patches/0005-board-stackup-FACTS.md`)
- `BOARD_STACKUP::Deserialize` existed at the pin as a stub (`return false`); the patch
  implements it and adds `API_HANDLER_PCB::handleUpdateBoardStackup` (copper layers must be the
  enabled ones in order, alternating with dielectrics; thickness > 0, ≤ 10 mm per (sub-)layer,
  ≤ 100 mm in total, ≤ 32 sub-layers; finite material values; known enum values; colour names kept when the RGBA value is unchanged; normalised response).
- **Addition:** `SetBoardEnabledLayers` re-synchronises an explicit stackup (`m_HasStackup`)
  with the new layer set (KiCad's `SynchronizeWithBoard`) — otherwise a layer count change left
  `GetBoardStackup` and the saved file describing the old copper layers (also true for any
  loaded board with a `(stackup …)` section, unpatched).
- QA tests went into `qa/tests/api/test_api_handler_pcb.cpp` (in-process handler suite
  `ApiHandlerPcb` on the 4-copper `issue5830` board; no board e2e file exists, no CMake change),
  not a new `test_api_e2e_board_stackup.cpp`. 7 failures before the handler, all pass after
  (FACTS file: command, runtimes).
- Recipe tag built by the PR: `52b43fb8fc8e11fdab5d683eaefc91a49f9cbcd7-rdc93f47200b08f97`
  (kicad-base.yml on the PR, ≈ 8 min with CI's ccache); `kicad-base.yml` on `main` publishes it after merge — **a release
  needs that tag first** (CHANGELOG release note). No kipy change (no proto message change).

**CopperChat**
- `StackupLayer` gained `epsilon_r` (dielectrics; `None` for copper). Values (mm, εr) from
  jlcpcb.com/impedance read 2026-11-25: prepreg 7628 4.4, 3313 4.1, 2116 4.16, core 4.6; JLC
  publishes **no loss tangent** (KiCad's value stays) and **no 2-layer build-up**: `jlc-2l-1.6`
  = 1 oz / 1.53 mm FR-4 core / 1 oz with Er 4.5 ("FR-4 Dielectric Constants 4.5 (2-Layer PCB)",
  jlcpcb.com/capabilities/pcb-capabilities). Core material string `FR4`, prepregs `7628` /
  `3313` / `2116`. Sums (mask excluded): 2l 1.6000, 4l 1.5862, 6l 1.5384 mm.
- `pcb_set_rules`: `plan_stackup(req, copper)` runs after the board read (needs the current
  count); unknown id / copper mismatch → `invalid_argument`. Stage `stackup` right after
  `layer count`: `GetBoardStackup` → `stackup_request` (current stackup with the preset's
  thicknesses, dielectric type/material/εr as one sub-layer; mask, finish, edge kept) →
  `UpdateBoardStackup` (raw `client.send`, `setup.update_stackup`). The KiCad "no handler"
  `ApiError` → `stackup_unavailable` (confirmed against the unpatched image end to end); any
  other refusal → `invalid_argument`. Read-back in `verify` from `BoardModel.stackup`
  (±0.0005 mm). Report `stackup {id, jlc_name, total_mm, board_mm}` (`total_mm` = the preset's
  copper + dielectric; `board_mm` = KiCad's board thickness, solder mask included). A busy /
  internal `ApiError` is re-raised (undo armed), only `AS_BAD_REQUEST` is `invalid_argument`. A
  board stackup the API could not write back (disabled or zero-thickness copper/dielectric, or a
  stale stackup for another copper count — KiCad accepts those from files) is refused before the
  write by both the stackup stage and the layer-count stage (its undo writes the pre-call
  stackup back); `_unrestorable` mirrors every check of the patch (copper ids = the enabled
  ones in stack order, alternation, enabled / positive copper and dielectrics, technical layers
  enabled + unique + on their side, 0–10 mm per layer, 100 mm total, 32 sub-layers, finite
  non-negative material values). Only when such a refusal is due,
  `has_stackup_handler` probes for patch 0005 with an empty stackup (refused, never written): an
  unpatched server never writes a stackup back, so it gets `stackup_unavailable` for `stackup`
  and no refusal for a plain layer change. A layer count change without
  `stackup` adds a note (an existing fab stackup keeps its matching layers, KiCad adds defaults).
- **Compensation:** the stackup stage's undo restores the stackup it found (layer count already
  applied); the `layer count` stage now also snapshots the pre-call stackup and its undo writes
  it back after restoring the count when it differs (P2 file, minimal change). Equal stackups are
  never written, so an unpatched server is never asked; a "no handler" there is ignored.
- P2's `test_stackup_unavailable` (kicad) and `test_stackup_unavailable_in_p2` (unit) removed:
  superseded by `tests/kicad/board/test_stackup_patch.py` / `tests/unit/board/test_stackups.py`.

**Residuals for later phases**
- Restoring a stackup writes it explicitly: a board that had no `(stackup …)` section before a
  failed/undone call keeps one afterwards (same values; `board_hash` equal). History undo
  restores the file and is unaffected.
- `UpdateBoardStackup` has no KiCad undo entry; CopperChat's History is the undo.
- A colour changed through the API is stored as `#RRGGBBAA`, not as a KiCad colour name.
- Rollback after a layer shrink (4 → 2 → undo): the dielectrics KiCad dropped come back with
  their RGBA colour, so a named colour KiCad cannot parse ("FR4 natural") becomes explicit black
  on those dielectrics (cosmetic: 3D view / board characteristics; History undo restores the
  file exactly).

### As built (P4)

**Placer design (`cc/board/placer/`, pure)**
- Satellites go to a *host* (decoupling cap → its IC pin; load cap → its crystal; otherwise
  the placed part sharing the most signal, then supply, then ground nets; ties fewer pads,
  placed earlier), on the host side the connected pin points to (an elongated pin pad only
  allows the two sides along its long axis; the neighbouring sides cost `SIDE_PENALTY_MM`
  1.5), **flush** at the courtyard gap with the connected pad aligned to the pin; the spot
  is found exactly by interval search over rings (`ALIGN_WEIGHT` 0.5 per mm slid, physical
  pad-to-pin distance as the cost). 2-pad parts radial, connected pad facing the host.
- Order inside a cluster (deviation from Task 1's "decoupling caps first"): crystals, the
  first decoupling cap of every supply pin, load caps, switcher passives, the rest by shared
  nets, bulk decoupling caps last (each takes the IC supply pin of its net with the most
  room). Caps of one IC + net are spread round-robin over its pins, smallest value first.
- Gaps: decoupling caps and crystals keep `gap` (courtyard clearance + 0.05); every other
  satellite `gap + 0.3` (routing room between neighbours).
- `build_clusters`: a smaller anchor sharing signal nets with a bigger anchor of the same
  block becomes its satellite (ESD array next to its connector; P3a residual).
- Packing: `PULL_WEIGHT` 0.1 (plan 0.5: the region pull dominated connectivity on
  `sp4_synth`), coarse 2 mm grid + region ends, refine 0.5 mm, full 0.5 mm scan per rotation
  with no coarse hit. Connector edge term only for connectors with a `board.md` edge, the
  `edge` argument, or anchoring their own cluster (deviation: a debug header in an MCU block
  dragged the whole block into a corner); a connector's mouth (courtyard side away from its
  pads) must face its edge (`FACING_MM` 5).
- `place_block(..., doc=None)` keyword added (board.md edges + allowed sides for the score).
- `PlaceResult.moves` = `fixed` moves first, then the members.
- Legaliser: after the push rounds a part still in conflict moves to the nearest free spot
  around its start (0.25 mm rings within `max_shift`, else 20 mm) — the pure push-apart got
  stuck on ~1/4 of sparse random boards. A legal part keeps its exact (possibly off-grid)
  position; only moved parts are snapped (`max_shift` allows the snap, + 0.035 mm). Both
  phases have deterministic work budgets (push rounds scale with the part count, 200k
  fallback spots), so a huge infeasible board answers `does_not_fit`, not a timeout.
- Packing search (review round 2): per-net nearest index for the airwire cost; the coarse
  grid widens beyond 4,000 spots; a rotation without a coarse hit gets an exact sweep (every
  0.5 mm spot where the block box touches a region end, an obstacle, a keepout or an
  outline vertex), not a full scan; `does_not_fit` areas use the union of the obstacles.
  Round 3: the coarse grid also keeps coarse spots x block pads within 400k (≥ 400 spots), a
  wide coarse grid is refined on 2 mm before 0.5 mm, the nearest index sizes its cells per net.
  Measured: a 100-pad block on a 600 x 400 mm board with 1,000 outside parts ≈ 10 s. A connector keeps its own cluster (never absorbed
  as a satellite). An assigned edge (board.md row or the `edge` argument) is nearly hard:
  a connector farther than 0.5 mm beyond the edge margin costs `EDGE_MISS` 1000.

**Measured / recorded**
- `sp4_synth` placed by `place_synth` (`tests/kicad/board/test_place_synth.py`, local
  kicad-base image; outline 60 x 45, `jlcpcb-4l`, channels 1.5 mm): M1 pass, 0 DRC
  `overlapping_footprints`, 0 unplaced, airwire 176.9 mm, M8 worst 4.23 mm (review: the
  3225 crystal's far diagonal pad; every decoupling cap within 3 mm), M9 0 reviews, M10
  worst 1.86, max 12 pads per 5 x 5 mm cell, three `pcb_place_block` calls 2.6 s total;
  a second placement from the same unplaced state gives identical positions.
- `place_block` on a synthetic 252-part / 12-block board: 0.15-3 s per block (pure).
- Property tests (`test_placer_props.py`, `test_legalize_props.py`, seeds 0..249): ~21-26 s.
- **`MAX_CELL_PINS` stays 30 (untuned, deviation from Task 5 Step 3):** on `sp4_synth` one
  footprint dominates (the USB-C receptacle alone puts 12 pads in one cell; the placed board
  max is 12) and every denser test placement we could build (legaliser-packed into 28 x 28
  mm, blocks with `keep_channels: 0`) also measured 12, while all synth blocks with
  `keep_channels: 0` do not fit 20-25 mm squares at all. The metric cannot separate good
  from crammed on this fixture; **P9a** re-measures on OpenDriveHub (~160 parts) and tunes.

**Tools (`cc/kicad_tools/board/{place,unroute_ops,specs_place}.py`)**
- `pcb_define_blocks` may use two stages: stage 1 (one commit) takes members out of other
  `blk:*` groups, reuses a same-named group (keeps its KIID) and creates new ones, with an
  `undo` that restores every touched pre-existing group (KiCad moves a member silently);
  a last stage "remove replaced blocks" deletes groups (`replace`, duplicates, emptied
  `blk:*` groups) — compensation cannot re-create a deleted group. `anchor` is validated and
  reported, not stored (a KiCad group has no field for it).
- Probes: flip + position/orientation in one commit works (`flip_items` refreshes the
  items, one `update_items` follows); group membership moves silently; empty groups stay.
- API DRC type names are `overlapping_footprints`, `dangling_track`, `dangling_via`
  (`courtyards_overlap` / `track_dangling` / `via_dangling` are kicad-cli names); P4 tests use
  the API names. **Residual:** `tests/kicad/board/test_sync.py:91` asserts on
  `courtyards_overlap` and therefore checks nothing (P2 file).
- `unroute_ops` (for **P5b**): `CopperSelection(track_kiids (tracks + arcs), via_kiids, nets,
  locked)` + `.empty`; `locked` counts skipped *components*. Components join at shared ends,
  track end on a same-layer track (T) and track end in a via — **never through pads** (else
  moving one decoupling cap would unroute all of GND); copper continuing through another
  part's pad stays. `attached`: a track/arc end inside a copper pad on its layer, a via
  centre in a copper pad. Region seeds use track width / via radius; zones never selected.
  `unroute_stage(sel)` = one commit, `remove_items_by_id`.
- Errors: `not_found` for refs has `details {refs, closest: {ref: [..]}}`; `unknown_block`
  `details.closest: [..]`. `score.after` is computed after the apply (`run_cpu(score_job)`;
  a timeout gives `null`, the tool still succeeds). A call that changes nothing writes
  nothing and costs 0. `pcb_move` may move a locked part (explicit), `pcb_legalize` never;
  the side policy is checked for `pcb_move` only on a side change. The `region` schema is one
  object with all six fields optional (no `oneOf`); the code requires `{x,y,w,h}` or
  `{edge, offset?}`. `cc/board/jobs.py` gains `place_block_job` (returns `(result, region
  used)`; edge strip depth capped at the outline), `legalize_job`, `score_job`, `edge_strip`.
- `place_synth(rig, db, run)` (`tests/kicad/board/conftest.py`): blocks from `pcb_analyze`,
  the ESD part's block merged into the USB connector's, order connector block (`edge: left`)
  → MCU block (right half) → power block (left half), `keep_channels` 1.5; plus
  `analyzed_blocks(ctx)`. P5b's `placed_synth` builds on it.

**Residuals for later phases**
- The board-layout skill (P9b) should tell the agent: protection parts join their
  connector's block; place edge blocks first, then blocks by connectivity to what is placed;
  wider `keep_channels` when the board has room.
- Silk reference texts overlap after placement (P7 `pcb_tidy_silk`).

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
  only, `O_NOFOLLOW`, ≤ 16 MiB); the JSON report is read only as a regular file ≤ 8 MiB;
  timeout, no report, a bad report or a copy failure → `drc_rejected`. Without a schematic →
  `drc_rejected`.
- Review round 3: `0013_board.sql` adds a fourth index `board_reviews_conversation
  (project_id, board_file, conversation_id, id)` (B-C10 SQL otherwise verbatim). The board
  stage also treats a **schematic (or any non-approval) edit** after the evidence as a write:
  guard 4 for `stage = board` and `require_board_approved` (after the newest full check →
  `reason = stale`, counted from the older of the newest full check and the approval, so a
  new passing check never revives an older approval) use `board_written_since(...,
  any_kind=True)`; a full check records nothing when any change was logged while it ran — the parity evidence
  depends on the schematic, which `board_hash` does not cover. `pcb_check` reads the model
  and runs the API DRC under `session.applying()` (a write undone by compensation logs no
  change row); the full-check copy dir refuses a symlinked `.kicad-agent`/`board`/`check`
  (`output_path_unsafe`) and sweeps copy dirs older than 1 h.
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
- A History restore / plan rollback rewrites the schematic without a change-log row: the
  board-stage evidence and `require_board_approved` do not see such a schematic restore at
  an unchanged `board_hash` (only edits are seen). A marker row from `restore_snapshot` would
  close it (history code, not P3b's); later phases / P9b.
- `board.md` edits (preset, Checks rows, allowed sides) log no change row, so they do not make
  board evidence or the board approval stale although a full check's `hard_failures` depend on
  them. The board itself is unchanged; **P8** should re-run (or compare a stored `board.md`
  hash with) the full check at export time.
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
### As built (P5a)

**Measured / recorded** (local, pure Python 3.13, no other load than a parallel phase child;
CI records the same `record_property` values)
- `test_15_short_nets_budget` (30 × 30 mm, 2 layers, 20 parts, seed 4242): **1.9 s** for 15/15
  nets (bound 10 s; slowest net 0.6 s). `test_large_board_budget` (100 × 100 mm, 4 layers,
  80 parts, 40 nets of 5–40 mm, each `run_cpu(solve, prune_model(m, req), req,
  timeout_s=60)`): **38.6 s, 40/40 routed**, worker peak RSS (`VmHWM`) **385 MB**
  (limit 3 GiB); slowest single connection ≈ 7 s.
- Property tests (`test_router_props.py`, 250 seeded maps, 493 requests, one search each
  with `max_expansions=100_000`): ≈ 95 % routed, every returned path M4 = M7 = 0 and exact
  clearance; runtime ≈ 40–56 s for the routing fixture + ≈ 14 s determinism (every tenth
  map re-routed) + 3 s open maps. Whole router test set ≈ 2 min (CI `backend` job; still
  below the kicad shards' critical path).
- Per expansion ≈ 4–10 µs (pure Python); search effort, not grid building, dominates.

**Cost constants** (`astar.py`): `TURN_COST = 1.0` (plan: 0.3; 1.0 gives ≈ 10–15 % fewer
bends at equal speed on both perf boards), `VIA_COST = 2.0`, `LAYER_FACTOR = 1.2`,
`PAIR_BAND_FACTOR = 0.8`, `HEURISTIC_WEIGHT = 1.5`, `MAX_GOAL_REJECTS = 64`,
`FIRST_TRY_EXPANSIONS = 500_000`, `COARSE_MM = 0.5`; `grid.py`: `WINDOW_MM = 6.0`,
`MAX_CELLS_PER_LAYER = 4_000_000`, `EDGE_MIN_MM = 0.3`, `PRUNE_HALO_MM = 3.0`.

**`ObstacleGrid`** (picklable): `pitch, x0, y0, w, h` (node counts), `layers`, `cells`
(per layer, 0 free / 1 own net / 2 blocked), `via_blocked`, plus `shapes` (per layer the
exact obstacle cores, each with its required centre-line distance), `outline`, `inset`
(exact check `segment_clear` / `path_clear`) and `cheap` (pair-fallback band).

**Contract deviations / additions (B-C12 updated where binding)**
- The lattice is aligned to `req.a`'s centre (exact start); only the `b` end is snapped.
  Node count = `floor(extent / pitch) + 1` per axis (the plan's `ceil(w / pitch)` ± 1);
  `RouteTooLarge` is raised before any allocation.
- A node is blocked when its distance to an obstacle core is ≤ need + `chord_slack` (the
  sagitta of one diagonal step, ≈ 0.002–0.004 mm), so straight runs between free nodes keep
  the exact need. The plan's `test_pad_inflation_exact` text said 0.4 mm for clearance 0.2 /
  width 0.2; the B-C12 formula gives 0.3 mm, which the test asserts.
- Vias are checked against other-net copper on **every** copper layer (a through via
  touches all), not only `req.layers`; no via in an own SMD pad (reflow); a `vias`
  keepout needs the via disc outside it. Copper zones (non rule areas) are not obstacles
  (routing runs before pours; `prune_model` drops fills). Footprint-embedded keepouts
  (`Part.rule_areas`) are not obstacles (no layer/keepout type in the model) — residual.
- Copper graphics: segments/arcs as strokes, circles/rects/polygons as filled, beziers as
  their control-point hull.
- Search: weighted A* (`HEURISTIC_WEIGHT` 1.5, no re-opening) on an obstacle-aware coarse
  distance field (0.5 mm cells linked where a fine move crosses their boundary) instead of
  the plain octilinear heuristic — a 4 mm detour went from 2.3 s to 0.3 s. Before the
  search a union-find over free runs (`reachable`) rejects disconnected requests at once.
  A 45° turn advances straight for the shortest legal piece in one move (no bend can follow
  sooner), which halves the state space; the goal may be reached mid-move.
- A goal state is accepted only when the snapped path passes `analyse_bends` (pad ends at
  pads, not at vias) on every layer run and the exact clearance check; otherwise the
  search continues (≤ `MAX_GOAL_REJECTS`). `snap_ends(points, a, b, width)` takes `None`
  for an end that stays (vias) and raises `SnapError` when no ≤ 45° approach exists.
- `solve`: first search on the default window with at most `FIRST_TRY_EXPANSIONS`; the one
  retry (doubled window, `max_expansions`) runs **from pad `b`** (lattice aligned to `b`,
  path reversed) — on the large board a connection that the `a`-aligned lattice could not
  fit (2 M expansions, 26 s) routed in 1.5 s that way. Still exactly two `build_grid`
  calls (`[None, 12.0]`).
- `build_grid(..., extra=())`: extra `(layer, shape)` obstacles (the pair partner).
- Pairs: the coupled centreline gets straight leads (`JOG_MIN(2w + pc)`) at right angles
  out of each pad pair (pad pairs must sit on an octilinear axis ± 1°), so the offsets meet
  the pads; the pair keeps `pc = max(gap, clearance_p, clearance_n)` between its two nets
  and `margin` does not apply between them (`test_solve_pair_margin`). The fallback's
  partner obstacle uses `pc`; its preferred band is `gap + w ± pitch` (cost × 0.8). Each
  fallback net gets `solve`'s retry (default window, then doubled from pad `b`).
- `pair_coupling` counts a sample only where the nearest partner segment is parallel and
  the sample projects onto it.
- `prune_model` keeps items intersecting the retry window grown by `PRUNE_HALO_MM`;
  `solve(prune_model(m, req), req) == solve(m, req)` on the seeded maps and the 100 mm board.

**Review round 1 (PR #105)**
- Pad exit: the first move out of pad `a` follows `pad_dirs` all the way out of the pad
  outline (one macro move); a goal is accepted only if the segment that enters pad `b`
  does too (`pad_exits_ok`). `valid_path` also requires the first run on a layer of pad
  `a` and the last on one of pad `b`; `path_clear` checks vias exactly (`via_clear`).
- `reachable`: two-pointer sweeps over sorted run lists (row joins and via joins) instead
  of all-pairs loops — a 4-layer board with 204 parallel tracks went from 90 s to ≈ 1 s.
- `build_grid`: refuses empty/duplicate/unknown layers (`ValueError`); a `vias` keepout on
  any copper layer blocks vias; the outline is rasterised once; `extra_vias` (via
  obstacles) next to `extra`; `ObstacleGrid.via_shapes` / `via_inset` for the exact via check.
- Pairs: the coupled centreline runs on the first layer that all four pads are on (else no
  coupled attempt); a reversing centreline is rejected before offsetting; an offset end that
  does not meet its pad (pad pitch ≠ gap + w, e.g. 0.5 / 0.65 mm) gets a 45° fan-in jog;
  the fallback treats the partner's vias as obstacles (tracks and vias of the second net).
  Budgets: centreline 250 k, per net 250 k + 750 k on the retry (`PAIR_*_EXPANSIONS`; worst
  case 2.25 M expansions per `solve_pair`).
- Two nodes at one lattice point (start = goal) give the direct snapped connection or None.
- Tests: an independent bend checker (`router_maps.strict_bends_ok`, angle tolerance
  1 nm / length for the 1 nm rounding) on all property-test output; 30 seeded pair maps
  (exact clearance incl. vias, pair spacing, bends). Router test set ≈ 2.5 min.
- Not changed: the chord slack (≈ 0.002–0.004 mm) also applies along flat pad sides, so a
  gap exactly equal to the need (to the µm) is refused — conservative by design.

**Review round 2 (PR #105)**
- `valid_path` exempts a run's first / last bend only when that bend lies **inside** its
  pad (the board metric exempts every pad-end bend; the router is stricter), so fan-in
  jogs, coupled leads and snaps can no longer hide a micro-jog or short chamfer near a pad;
  coupled results also pass `pad_exits_ok`. The test checker `strict_bends_ok` takes the
  end pads and applies the same rule.
- `snap_ends`: a centre on the last segment's line moves the end point (a goal node just
  past the centre no longer yields a 4-bend bump or loop).
- Time: `solve`, `route_pair` and `solve_pair` stop after `SOLVE_TIME_LIMIT_S = 25 s`
  (`route(..., deadline=)`, checked every 4,096 expansions) → `None`, below `run_cpu`'s 30 s.
- `reachable`'s via joins: one sorted sweep over all layers' via-capable intervals per row.
- `build_grid` refuses non-finite numbers, width ≤ 0, clearance / margin < 0 and via
  diameter ≤ drill (`ValueError`).
- Not changed: outline rasterisation is O(rows × edges) — v1 outlines are rounded
  rectangles (≈ 40 edges, spec §2 non-goal: polygon outlines); revisit with polygon outlines.

**Review round 3 (PR #105)**
- Coupled check: the partner net's **existing** tracks, arcs and vias are obstacles at the
  pair clearance (the centreline merges both nets, so they were invisible before).
- `SOLVE_TIME_LIMIT_S` also covers grid building and the reachability pre-check
  (`build_grid(..., deadline=)`, `reachable(..., deadline=)` raise the internal
  `DeadlineExpired`; every entry point returns None) and no retry starts after it.
- The exact-check shape index clips each obstacle's bbox to the window; `build_grid`
  refuses width / clearance / margin / via diameter above `MAX_SIZE_MM = 100`.

**Review rounds 4–5 (PR #105)**
- Every grid build of the pair fallback, the own-net / via marking (`_Marker` checks the
  deadline every 64 rows) and each goal validation honour the time limit.
- `snap_ends`: before a two-piece stub, the end point backs off along the last segment so
  one 45° piece reaches the centre; a stub that would form a 90° with a chamfer shorter
  than `CHAMFER_MIN` is refused (`SnapError`, the search goes on).
- `route_pair` / `solve_pair` refuse a non-finite gap or one outside 0..100 mm; stacked pads
  on two layers at one node fall through to the search (a via joins them).
- Tests: `random_map` ends every other request on the bottom layer (vias in the property
  tests); the pair property test checks each path against the partner's pads.

**Verification pass (PR #105)**
- Every snapped tail (single 45° piece or stub) keeps the straight-piece rules for both
  senses **without** the pad exemption and has no segment shorter than 0.1 µm
  (`simplify._tail_ok`); otherwise `SnapError` and the search tries another goal state.
- Deadline checks in the pad / own-net / via loops of `build_grid`, every polygon row and
  every 256 polygon edges in `_Marker.shape`; a coupled centreline wider than 100 mm is
  not attempted (the fallback routes the nets alone).

**Residuals for later phases**
- P5b: `route` returns `None` for a connection whose pad is boxed in by earlier copper
  along its axis; such a failure can take up to the full expansion budget (≈ 10–25 s per
  search) — the `run_cpu` timeout (≤ 30 s) bounds it; reporting it `no_path` / `timeout`
  is P5b's call. Order connections so short/critical ones route first.
- Footprint keepouts (`Part.rule_areas`, antenna) are not router obstacles yet (needs their
  layer/keepout type in `BoardModel`).
- The router never produces vias inside SMD pads and never two vias at one node; hole-to-hole
  spacing between vias of the same path is not checked separately (VIA_COST makes close
  via pairs unlikely; DRC in P5b is the oracle).

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
   right after P2 merges; the owner writes the upstream text later. Done in P2b (#106).

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

---

## Round 2 (2026-11-26) — OpenDriveHub improvement round

**Why.** P9a's ODH dry run never reached a routed board and the owner marked Y-junctions, a
dogleg and pad-entry jogs on the golden render (spec §22). **Goal of round 2:** the agent can
lay out the OpenDriveHub transmitter main board (copy, 76 parts) to the owner's bar, and the
`sp4_synth` golden stays green through every round-2 PR. **Spec:** §22 "Amendment 2026-11-26
(round 2)" (owner rules a–c, owner decisions 1–4, M15/M16, changes to M3/M4/M11/M12/M14) — it
wins over every earlier section and over every contract above that it contradicts.
**P9b (skills, flag on) and P10 run after round 2**; P9b's phase file stays valid, plus the
round-2 tools (`pcb_floorplan`, `pcb_place_blocks`) in its skill and tool list.

### Round-2 phase overview

| # | Phase file | Goal (one line) | Depends on | Wave |
|---|---|---|---|---|
| R1 | `board/round2-r1-metrics.md` | junction / entry-zone / neck / corridor geometry in `geom.py`; M4 without the T clause, **M15** Y-junction, **M16** dogleg + pad-entry loop/jog (both behind `M15_HARD` / `M16_HARD = False`), M12 neck-down exemption, M11 own-antipad fix, M3 silk-line warning-only | — | R-1 |
| R5 | `board/round2-r5-owner-fixes.md` | JLC presets `min_drill` 0.2 + fab-report surcharge note, BOM/CPL without mounting holes / excluded parts, `pcb_render` default extent from outline ∪ courtyards | — | R-1 |
| R6 | `board/round2-r6-memory.md` | heavy-process gate (parity DRC after Freerouting / CPU jobs / other board `kicad-cli`), CPU-worker recycling, concurrent-peak measurement in `odh-dry-run.py`, re-measure | — | R-1 |
| R2 | `board/round2-r2-floorplan.md` | global block floorplanning (edge blocks, min-cut bisection over the whole board, channel reservation, courtyard fill), pair alignment (rule b), `pcb_floorplan` + `pcb_place_blocks`, golden flow on the new tool | R1 | R-2 |
| R3 | `board/round2-r3-router.md` | own router: neck-down in escape zones, T-junction (tree) branching instead of the 90° ban, 45° branches forbidden, straight-first for aligned pads, pad-entry costs; router output M15 = M16 = 0 | R1 | R-2 |
| R4 | `board/round2-r4-fill.md` | Freerouting tuning (probed settings, passes/timeouts), intra-block nets in the critical set, post-processor Y → T (else rip `y_junction`), T no longer ripped, `_widen` keeps neck-downs; **switch `M15_HARD` / `M16_HARD` on**; ODH fill passes the 20 % rule | R2, R3 | R-3 |
| R7 | `board/round2-r7-odh-acceptance.md` | repeat the ODH dry run (80 × 60 first, else 80 × 100 well spread), honest report, memory re-measured, fix-forward of tool defects found, index "As built (R7)" | R1–R6 | R-4 |

Waves (a phase starts once every phase it depends on is merged): **R-1** R1, R5, R6 (disjoint
files) · **R-2** R2, R3 · **R-3** R4 · **R-4** R7. Then P9b, then the release (owner OK).

### Round-2 file ownership

`cc/` = `backend/src/copperchat/`. Same rules as "File ownership while phases run in parallel"
above (owner edits freely; "append" = add lines only, keep both sides on rebase).

| Area | Owner (wave) | Others |
|---|---|---|
| `cc/board/geom.py` (junction / entry / neck / corridor additions), `cc/board/metrics.py`, `cc/kicad_tools/board/specs_analysis.py` (metric description text) | R1 (R-1) | R4 flips `M15_HARD` / `M16_HARD` in `metrics.py` (two lines) |
| `cc/board/presets.py`, `cc/board/fab.py`, `cc/kicad_tools/board/fab.py`, `cc/board/render.py` (`default_extent`), `cc/views/board_render.py`, `cc/kicad_tools/board/views.py`; `cc/board/model.py` + `cc/kicad_tools/board/read.py` only to append the unhashed `Part.exclude_from_bom` / `exclude_from_pos` fields if missing | R5 (R-1) | — |
| new `cc/board/heavy.py`, `cc/board/worker.py` (recycling), `cc/board/freerouting.py` (gate around the run only), `cc/kicad_tools/board/check.py` (`cli_parity` gate only), `scripts/odh-dry-run.py` (memory sampler), `backend/tests/unit/test_odh_dry_run.py` | R6 (R-1) | R2 (answer loading only, if needed) and R7 edit both afterwards |
| `cc/board/placer/**` (new `floorplan.py` incl. the `align_pairs` pass), `cc/kicad_tools/board/{place,specs_place}.py`, `cc/agent/profiles.py` (`PCB_READ` / `PCB_WRITES` names), `cc/changes/repo.py` + `frontend/src/components/changes/highlight.ts` (`board_place_blocks` kind), `cc/views/board_render.py` (`render_board(hulls=…)` only, after R5), `backend/tests/kicad_e2e/flow.py` | R2 (R-2) | `cc/board/jobs.py` append (`floorplan_job`, `place_blocks_job`) |
| `cc/board/router/{grid,astar,simplify,pairs}.py`, `cc/kicad_tools/board/route.py` | R3 (R-2) | — |
| `cc/kicad_tools/board/{fill,specs_fill}.py`, `cc/board/router/chamfer.py`, `cc/board/freerouting.py` (settings arguments; R6's gate stays), new `cc/board/critical.py`, `cc/kicad_tools/board/specs_analysis.py` (description, after R1), `backend/tests/kicad_e2e/flow.py` (corridor nets first, after R2) | R4 (R-3) | `cc/board/jobs.py` append-only edit inside `analyse_job` (critical-set extension); `cc/kicad_tools/board/place.py` (import `defined_blocks` / `BLOCK_PREFIX` from `critical.py` only) |
| `scripts/odh-dry-run.py`, `backend/tests/kicad_e2e/**` (flow answers), any owning file of a defect found on ODH (minimal fix, recorded in "As built (R7)") | R7 (R-4) | — |
| **Shared, append-only (all round-2 phases):** `CHANGELOG.md` `[Unreleased]`, this index ("As built (R#)" sections, contract edits in place), `backend/tests/{unit,kicad}/board/conftest.py`. **`backend/tests/kicad_e2e/goldens/sp4_synth_metrics.json`**: every phase whose change moves a golden value regenerates it with `UPDATE_GOLDENS=1` **after rebasing on the latest `main`** and explains each change in the PR body; never hand-merged. | every phase | — |

### Round-2 contract changes

Binding like B-C1…B-C14; a phase that deviates updates this block and its "As built (R#)".

**B-C5 (R1, append to `cc/board/geom.py`)**
```python
JunctionKind = Literal["t", "x", "y"]
def junction_kind(headings: Sequence[float]) -> JunctionKind   # ≥ 3 headings (deg) of track ends at one node:
    # any pair d with ANGLE_TOL_DEG < (d mod 90) < 90 − ANGLE_TOL_DEG → "y"; else all on one 90° lattice → "t" (3) / "x" (≥ 4)
# BendReport gains  y_junctions: tuple[Point, ...]   (analyse_tracks: off-pad, off-via nodes of degree ≥ 3, kind "y")
# t_junctions keeps EXACTLY today's semantics (off-pad nodes with a 90° pair, Y-shaped ones included); M4 ignores it
#   from R1 on; its other consumers (chamfer._REASON, route._bends, fill._bends_of) are switched to y_junctions in R3/R4
ADJ_GAP_MM = 3.0; ESCAPE_HALO_MM = 0.5; LOOP_TURN_DEG = 135.0
def entry_zone_width(width: float) -> float                         # max(1.0, 3.0 * width)  (a def, not a lambda: ruff E731)
def entry_zone(pad: Pad, width: float) -> tuple[Point, ...]          # pad rectangle (rotated) grown by entry_zone_width(width)
def escape_zone(part: Part) -> tuple[Point, ...]                    # courtyard's axis-aligned bbox grown by ESCAPE_HALO_MM (convex)
def neck_width(pad: Pad, class_width: float, min_track: float) -> float   # min(class_width, min(pad.w, pad.h)), never < min_track
def straight_corridor(a: Pad, b: Pad, width: float) -> tuple[tuple[str, ...], Point, Point] | None
    # (shared copper layers, start, end) when a and b share ≥ 1 copper layer (THT pairs: all shared layers), their edge gap ≤ ADJ_GAP_MM and their extents overlap
    # across x or y by ≥ width: the axis-parallel segment through the overlap band's centre line, pad edge to pad edge
@dataclass(frozen=True) class EntryReport: loops: tuple[Point, ...]; jogs: tuple[Point, ...]
def analyse_entry(points: Sequence[Point], width: float, zone: Sequence[Point]) -> EntryReport
    # the path's piece from its pad end until it first leaves `zone`: |signed cumulative turn| ≥ LOOP_TURN_DEG → loop; an
    # opposite-sense 45° pair inside the zone → jog (pad-entry bends inside the pad itself excluded, geom.pad_entry_ends)
```

**B-C7 (R1, `cc/board/metrics.py`)**: `MetricId` = `"M1"…"M16"` (`METRIC_IDS` follows, so
`pcb_metrics.only`'s enum grows by itself); `M15_HARD = False`, `M16_HARD = False` (R4 sets both
`True`); M15 offenders `"y_junction"`; M16 offenders `"dogleg"`, `"pad_loop"`, `"pad_jog"`,
`"alignable"` (placement stage); M16 covers every non-ground net; `routed_path(model, a, b) ->
tuple[tuple[str, Point, Point], ...] | None` (new: `routed_length`'s Dijkstra (`metrics.py:549`)
factored to keep predecessors, so both share one search; `routed_length` returns the same values); M3 label `"silk_line_warning"`;
`REVIEW["M16_align_mm"] = 1.0`.

**B-C11 (R2, `cc/board/placer/floorplan.py`)**
```python
@dataclass(frozen=True) class BlockBox: name: str; refs: tuple[str, ...]; w: float; h: float; edge: Edge | None; region: Rect | None
@dataclass(frozen=True) class Floorplan: regions: Mapping[str, Rect]; channels: tuple[tuple[Point, Point, float], ...]  # cut line + width
                                         courtyard_fill: float; usable_mm2: float; courtyard_mm2: float
def floorplan(model, cls, doc, blocks: Sequence[BlockProposal], *, keep_channels: float,
              allowed_sides: frozenset[Side]) -> Floorplan | DoesNotFit     # DoesNotFit gains courtyard_fill: float | None = None
def place_blocks(model, cls, doc, blocks, *, keep_channels: float, allowed_sides) -> tuple[PlaceResult, Floorplan] | DoesNotFit
CHANNEL_MIN_MM = 1.0; CHANNEL_MAX_MM = 4.0; CHANNEL_PER_NET_MM = 0.3; PACK_MAX = 0.85; SPREAD_SPAN_MIN = 0.75
# As built (R2), additive: BlockBox.offset / edge_ref, Floorplan.fixed / fallback; REGION_PULL_WEIGHT = 2.0,
# BACKTRACK_TRIES = 6, BISECT_BUDGET = 400, TIGHT_GAP_MM = 0.3; pack.pack_block(..., pull_weight=) (place_block
# without the score); align_pairs uses metrics.alignment_pairs / alignment_need (R1's file, additive)
# R2b (additive): floorplan(..., relax=False); Floorplan.gaps / slack_mm / order / relaxed; place_blocks
# = constructive connectivity-ordered packing (TARGET_PULL_WEIGHTS, SLACK_STEPS_MM, SLACK_CREDIT, RETRY_MAX);
# pack_block(..., relax=, gaps=); block_layout / layout_cluster(..., relax=); new routability.py
# (net_pitch, channel_mm, congestion, largest_hole, block_gaps, gap_summary); PlacementScore +=
# congestion_worst, congestion_over, largest_hole_mm2
```
Tools: `pcb_floorplan {blocks?, keep_channels?}` (`PCB_READ`, cost 0, render with region
rectangles) and `pcb_place_blocks {blocks?, keep_channels?, unroute?}` (`PCB_WRITES`, cost 1,
change-log kind `board_place_blocks`). `PlacementScore` gains `spread: float` (union bbox of the
courtyards / usable bbox, min over x and y).

**B-C12 (R3, router)**: `RouteRequest` gains `neck: tuple[tuple[tuple[Point, ...], float], ...] = ()`
(escape-zone polygon, neck width) and `tree: bool = False` (the goal is pad b **or** any
own-net track already connected to b, entered perpendicularly). `ObstacleGrid` keeps one
inflation per width. `junction_masks` inverts: moves at 45°/135° to existing own copper at an
off-pad node are blocked, perpendicular moves are allowed (T). `route_connections` tries
`straight_corridor` first for every plan whose pads have one. `RoutePath.segments` may carry
per-segment widths: `segments: tuple[tuple[str, Point, Point, float], ...]` (layer, start, end,
width) — every consumer (P5b `_create`, P6b re-route, pairs) is updated in R3.
*As built (R3):* `RouteRequest.neck` entries are `(zone, neck width, layers)` - the
copper layers of the pad the zone belongs to; the neck applies only there (review: a via
or another part's pad inside the zone took it to B.Cu, M12). `tree` = "a T is allowed" (the grid finds the copper connected to b when the
request is solved; a tree search that finds nothing is followed by a plain pad-b search);
the pad-entry cost applies to a second bend in an entry zone only; `valid_path` refuses an
M16 pad loop or jog and `solve` allows one only after both windows found no clean path;
`route_connections(..., reserve=)` takes `corridor_reservations(model, cls)`.

**B-C12 post-processor (R4, `chamfer.py`)**: `t_junctions` are no longer ripped; each
`y_junctions` node is re-landed perpendicular (the branch's last segment replaced by the
perpendicular from its previous vertex onto the through-track, when that vertex's turn stays
0°/45° and DRC passes), else the branch's connection is ripped with reason `y_junction`;
connections with an M16 hard offender are ripped with reason `dogleg` / `pad_loop` and
re-routed by the own router (straight-first). `run_freerouting` refuses `extra_args` that set
the pass count or a thread/processor count.

**B-C13 (R4)**: `run_freerouting(..., extra_args=…)` receives `FREEROUTING_SETTINGS` (a tuple of
CLI options proven accepted by 2.5.0 in R4's probe; empty when none helps); DSN
`autoroute_settings` additions only under owner D4 (last resort) and recorded.

**B-C1/B-C8/B-C9 (R5, R6)**: presets `min_drill = 0.2` (2/4/6 layers); `cc/board/fab.py`
`fab_excluded(model, cls) -> tuple[str, ...]` (refs never in BOM/CPL), `bom_rows(csv, *,
exclude: frozenset[str] = frozenset())` / `cpl_rows(csv, *, exclude=…)` drop those designators,
`small_holes(model) -> tuple[tuple[str, float], ...]` (ref or `via`, plated drill < 0.3 mm),
`fab_report(..., excluded: Sequence[str] = ())` adds the "Excluded from BOM/CPL" and "Small
holes (possible JLC surcharge)" sections;
`cc/board/render.py` `default_extent(model, markers=()) -> Rect | None` (outline bbox ∪ courtyards
∪ markers; `None` when all are empty);
`cc/board/heavy.py` `heavy_slot(kind: str, *, wait_s: float) -> AsyncContextManager[None]`
(one `asyncio.Semaphore(1)` per running event loop, like `freerouting._slots`; kinds
`freerouting`, `parity` — `parity` also waits for an idle CPU worker but never holds its lock;
lock order heavy → (worker, momentarily) → `BOARD_CLI_LOCK`), `HEAVY_WAIT_S = 120`;
`worker.WORKER_RECYCLE_MB = 512`.

**Error codes (B-C14 additions):** `does_not_fit.details.courtyard_fill`; `fill_not_acceptable`
reason list unchanged; post-processor reason `y_junction`; `board_compute_timeout` also when the
heavy gate is not free within `HEAVY_WAIT_S`.

### Round-2 Review Focus

1. **A golden that turns red between phases** — R1 adds M15/M16 while the router still makes
   Y-junctions. Expected: both stay `review` until R4 switches them; every PR regenerates the
   golden after rebasing. Tests: R1 Task 4 (`test_switches_default_review`), R4 Task 5
   (`test_golden_m15_m16_hard`).
2. **A neck-down that leaks** — a narrow piece outside the escape zone, or a neck below the
   preset minimum. Expected: M12 fails it; the router never emits it; `_widen` widens it back.
   Tests: R1 Task 3 (`test_m12_neck_outside_zone_fails`), R3 Task 2 (`test_neck_only_in_zone`),
   R4 Task 3 (`test_widen_keeps_neck`).
3. **A T that hides a Y or a 90° bend** — a degree-3 node with headings {0°, 90°, 225°}, a T
   at a bend vertex, a branch landing exactly on a through-track's 45° chamfer. Expected: M15
   flags it, M4 still flags a degree-2 90° vertex. Tests: R1 Task 1 (`test_junction_kind_table`,
   `test_t_at_bend_vertex_is_y`, `test_branch_on_chamfer_is_y`), R1 Task 4
   (`test_m4_degree2_ninety_still_fails`).
4. **A floorplan that silently drops or overlaps blocks** — more blocks than cuts, an edge
   block wider than its edge, a board that cannot hold the blocks. Expected: every block gets a
   region or the call fails `does_not_fit` with `courtyard_fill`; never overlaps (M1). Tests:
   R2 Task 1 (`test_floorplan_property_legal`, `test_floorplan_does_not_fit_reports_fill`).
5. **A parity DRC that never runs or deadlocks** — a fill holding the heavy gate for 600 s, a
   cancelled turn holding it. Expected: the gate is released on cancel/error; `pcb_check full`
   fails `board_compute_timeout` after 120 s instead of hanging. Tests: R6 Task 1
   (`test_gate_released_on_cancel`, `test_parity_waits_then_times_out`).

### Round-2 plan calls (made by the plan author; the coordinator relays them)

1. **M16 hard/review split** (spec §22.5): dogleg (a) and pad loop (b, total turn ≥ 135° in the
   entry zone) hard; pad jog (c) and placement-stage "alignable" review. Numbers: adjacent =
   edge gap ≤ 3 mm, overlap ≥ track width, entry zone `max(1.0 mm, 3 × width)`, align fix ≤ 1 mm.
2. **M15/M16 ship as review first** (`M15_HARD` / `M16_HARD = False`) and become hard in R4,
   so `main` and the golden stay green at every merge.
3. **X-junctions** (four headings on one 90° lattice) pass M15; the own router still never
   creates one (P9a behaviour kept).
4. **Escape zone = courtyard + 0.5 mm**; neck width = `min(class width, pad minor side)`,
   never below the preset minimum track.
5. **Via drill minimum stays 0.3 mm** (owner decision 2 lowers plated holes to 0.2 mm; the
   agent's own vias keep 0.3/0.6 mm — no surcharge from our vias).
6. **New tools `pcb_floorplan` (read) + `pcb_place_blocks` (write, cost 1)** rather than more
   `pcb_place_block` arguments: one global decision needs one call; `pcb_place_block` stays for
   single-block edits.
7. **"Well spread"** = `PlacementScore.spread ≥ 0.75` (the courtyards' union bbox spans ≥ 75 %
   of the usable width and height), reported, not a hard metric.
8. **Intra-block and corridor nets join the critical set** (R4, `pcb_analyze`, not `classify`):
   a non-ground net whose pads all lie in one defined block (≤ 8 pads), and every net with a
   straight-corridor pair, is routed by the own router before the fill, so Freerouting only
   routes the rest through the reserved channels.
9. **Parity DRC is gated, not replaced** (owner decision 3): it *starts* only after the running
   Freerouting run and CPU job finish; a CPU job started during it is not blocked (else routing
   starves) — the measured concurrent peak shows whether that matters. P9a's numbers suggest api-server
   (≈ 3.1 GB) + parity `kicad-cli` (≈ 2.6 GB) ≈ 5.7 GB concurrent even when serialised; R6
   measures it. If it stays > 5 GB, replacing the parity DRC by an own parity check on the
   `kicad-cli` netlist (≈ 0.6 GB) is an **owner decision**, not part of round 2.

### Round-2 phase conventions

As "Phase Conventions" above, with: PR title `board round 2 <R#>: <short goal>`;
`kontor-recipes:review-loop --max-rounds 5 --dimensions bugs+tests,security+performance`
(style nits deferred after round 2; cap hit with code changes → one `--review-only` verify
pass); each phase adds `### As built (R#)` under "As built"; **thresholds are never relaxed**
(owner D2 + §22): a hard gate that fails on `sp4_synth` or ODH is a tool defect. ODH runs use
`scripts/odh-dry-run.py` on a copy outside the repo (answers JSON outside the repo, never
committed; no OpenDriveHub name in tools, metrics or non-fixture tests).
