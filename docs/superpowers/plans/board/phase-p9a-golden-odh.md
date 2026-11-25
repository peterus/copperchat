# Phase P9a — End-to-end golden on `sp4_synth`, ODH dry run, memory measurement

> Index contracts: all tool contracts as built (B-C1 … B-C14); index call 12 (`kicad_e2e`
> marker, own CI job), Global Constraints (hard thresholds D2, Freerouting in CI). Spec §15.3
> D5, §17 phase note (M2 asserted here), §18.2 end-to-end golden, §18.3 OpenDriveHub, §19
> (acceptance items 3, 7), §20 (memory risk). **Wave 9: starts when P6b, P7 and P8 are merged.**

**Goal:** prove the whole board flow on the synthetic fixture — sync to fab package, through
the real tools with scripted user approvals — with every hard gate passing (incl. M2 = 0) in
≤ 3 min, pin its metrics as a golden in a dedicated CI job, and measure the flow and the memory
on the owner's OpenDriveHub board with a manual dry-run script whose report goes into the PR.

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `backend/tests/kicad_e2e/{__init__,conftest,flow,test_synth_e2e}.py`,
  `backend/tests/kicad_e2e/goldens/sp4_synth_metrics.json` (CopperChat JSON output, written
  only with `UPDATE_GOLDENS=1`), `scripts/odh-dry-run.py`
- Modify: `backend/pyproject.toml` (marker), `backend/tests/unit/test_sharding.py`
  (`backend/tests/support/sharding.py` only if the marker expression needs it),
  `.github/workflows/ci.yml` (kicad shards' `-m`, new `kicad-e2e` job), `scripts/update-goldens.sh`
  (one line for the e2e golden)
- Tests: as above

---

### Task 1: `kicad_e2e` marker, shard deselection, CI job

**Interfaces:** `backend/pyproject.toml` markers gain `"kicad_e2e: full board flow on
sp4_synth (needs KiCad + Freerouting); own CI job, run with -m kicad_e2e"`. Every e2e test
module sets `pytestmark = [pytest.mark.kicad, pytest.mark.kicad_e2e]` (so the existing
`kicad` skip/require logic of `tests/conftest.py::pytest_collection_modifyitems` applies).
`ci.yml`: the `kicad` job runs `uv run pytest -m "kicad and not kicad_e2e" --shard ${{
matrix.shard }}/8`; new job `kicad-e2e` — `needs: kicad-commits`, `name: kicad-e2e`,
`container: image: ${{ needs.kicad-commits.outputs.base }}` (the **pinned** base image only,
not the `SUPPORTED_COMMITS` matrix), `options: --user root`, env `COPPERCHAT_REQUIRE_KICAD:
"1"`, `LANG: C`, `timeout-minutes: 30`, steps = checkout, setup-uv, `uv python install 3.13`,
`uv sync --locked`, then exactly P6a's three Freerouting steps — `echo
"COPPERCHAT_FREEROUTING_HOME=$GITHUB_WORKSPACE/.cache/freerouting" >> "$GITHUB_ENV"` (a
container job: never `${{ github.workspace }}`, which is the host path), `actions/cache` with
`path: .cache/freerouting`, the always-running `scripts/fetch-freerouting.sh` — and `uv run
pytest -m kicad_e2e -v`.

- [ ] **Step 1: Failing unit tests** (`test_sharding.py`): `_ci_shards()` also asserts the
  shard command contains `-m "kicad and not kicad_e2e"`;
  `test_ci_shards_cover_the_whole_kicad_selection` compares against `-m "kicad and not
  kicad_e2e"`; new `test_e2e_not_in_shards` (a `pytester` project with one `kicad`+`kicad_e2e`
  test and one `kicad` test, as `test_shard_option_deselects_after_marker_selection` does: the
  shard expression with `--shard i/2` collects only the latter, `-m kicad_e2e` only the
  former); `test_e2e_tests_also_marked_kicad` (collect the real suite with `-m "kicad_e2e and
  not kicad"` → empty); `test_ci_e2e_job` (regex over `ci.yml`: a job
  `kicad-e2e` whose container image is `needs.kicad-commits.outputs.base`, with the
  `$GITHUB_ENV` step writing `$GITHUB_WORKSPACE/.cache/freerouting`, the cache step with
  `path: .cache/freerouting`, the fetch step, and `-m kicad_e2e`; no `${{ github.workspace }}`
  anywhere in the job).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_sharding.py -q` — FAIL. **Step 3:
  Implement** (marker, ci.yml). **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `ci(board): kicad_e2e marker and job; kicad shards deselect it`.

### Task 2: Full flow on `sp4_synth` with scripted approvals

**Interfaces:** `tests/kicad_e2e/flow.py` (importable by the dry-run script):
```python
@dataclass(frozen=True) class Answers:   # the fixed "user" answers of the S0/S2/S3 questions
    outline: dict[str, Any]; rules: dict[str, Any]; blocks: dict[str, dict[str, Any]] | None
    zones: dict[str, Any] | None; stitch: dict[str, Any]
SYNTH_ANSWERS: Answers   # 60 × 45 mm, r 1, M2 holes inset 3; rules {layers: 4, preset: "jlcpcb-4l"} (layers explicit — a preset alone keeps the layer count, P2); power class 0.4 mm for VBUS/+3V3; blocks = pcb_analyze's proposal with fixed regions
@dataclass class FlowResult: steps: list[StepLog]; metrics: dict[str, Any]; seconds: float
                             renders: list[Path]; fab_files: list[str]; hashes: dict[str, str]
async def run_flow(rig: Rig, answers: Answers, *, on_step: Callable[[StepLog], None] | None = None) -> FlowResult
```
`Rig` = `synth_session` + `board_ctx` + real `HistoryHooks`, with `tool(rig, run_id, name,
**args)` and `user_says(rig, run_id, text)` modelled on
`tests/kicad/footprints/test_fp_approve.py` (human message inserted through `MessageRepo.insert(...,
metadata={"origin": "user"})` and `hooks.on_turn_start` for a new run). Every call goes through
the two-phase adapter (`sdk_tools`' handler), so budget, gates, autosave and snapshots are the
real ones. `run_flow` is the product flow turn by turn, so it does not call P5b's
`routed_synth` test helper, but it uses the same order (placement approved before `pcb_route`,
`layers: 4` explicit). Turns (each a new `run_id`, budget 20 per turn):
1. `pcb_sync_from_schematic`, `pcb_set_outline`, `pcb_set_rules`.
2. `pcb_analyze`, `pcb_define_blocks`, `pcb_place_block` per block, `pcb_legalize`,
   `pcb_check {level: "quick"}`, `pcb_render {view: "placement"}`.
3. `user_says("The placement looks good, approved.")` → `pcb_approve {stage: "placement",
   hash: <placement_hash>, user_quote: "placement looks good"}`.
4. `pcb_route` (critical nets from `pcb_analyze`), `pcb_route_fill`.
5. `pcb_zones {action: "add"}` (default stack), `pcb_fanout {net: "GND"}`, `pcb_stitch {net:
   "GND"}`, `pcb_tidy_silk`, `pcb_check {level: "full"}`, `pcb_render` top + bottom.
6. `user_says("Board approved for manufacturing.")` → `pcb_approve {stage: "board", hash:
   <board_hash>, user_quote: "approved for manufacturing"}` → `pcb_fab_export {preset:
   "jlcpcb"}`.
Any tool error fails the flow with the step, tool and error. A step that needs the ladder
(completion < 100 % after the fill) runs rung (c) `pcb_route` on the open nets in turn 4 —
scripted, logged, never silently.

- [ ] **Step 1: Failing kicad_e2e tests** (`test_synth_e2e.py`, module-scoped rig, test timeout
  `@pytest.mark.timeout(420)`):
  - `test_full_flow_hard_gates` — the final `pcb_check full` has `hard_failures == 0`; per
    metric: M1 pass, **M2 = 0**, M3 0 errors / 0 unaccepted warnings / parity clean, M4 = 0,
    M5 no hard fail, M7 hard = 0, M8 ≤ 5 mm, M12 no width violation, M13 pass, M14 pass;
    `pcb_fab_export` returned the P8 file set; `result.seconds ≤ 180` (spec §18.2).
  - `test_metrics_golden` — `goldens/sp4_synth_metrics.json` = `{metric id: {value,
    verdict}}` plus `{tracks, vias, track_length_mm, fill_seconds}`; tolerances: hard metrics
    and verdicts exact, review values ± 10 % (relative) or ± 0.5 abs. whichever is larger,
    counts ± 10 %, `fill_seconds` not compared (recorded only); `UPDATE_GOLDENS=1` rewrites
    it and the PR explains every change (index Residuals: Freerouting version bump).
  - `test_approvals_bound_to_hashes` — after the flow, `pcb_status` shows both checkpoints
    approved and not stale; one `pcb_move {items: [{ref: <a routed passive>, x: x + 0.5}],
    unroute: true}` (the passive carries copper after the fill — without `unroute` it is
    `part_routed`) succeeds and makes both stale; `pcb_status` shows `stale: true` for both and
    `pcb_fab_export` refuses `not_approved` (Review Focus 2 on the real flow).
  - `test_undo_to_placement` — History restore of the snapshot taken at turn 4's first
    mutation → `placement_hash` equals the approved one, no track/via on the board, the
    session answers `pcb_status` (spec §19 item 7).
- [ ] **Step 2: Run** `uv run pytest -m kicad_e2e` in the kicad-base image with the fetched
  Freerouting (`timeout 900`) — FAIL. **Step 3: Implement** `flow.py`, the rig, then
  `UPDATE_GOLDENS=1` once. A hard gate that fails on `sp4_synth` is a defect of the owning
  phase's tool: fix it in a separate PR against that phase's files before this PR merges
  (record in "As built"); **thresholds are never relaxed** (owner D2). **Step 4: Run** — PASS
  twice in a row (determinism).
- [ ] **Step 5: Commit** `test(board): end-to-end golden on sp4_synth with scripted approvals`.

### Task 3: ODH dry-run script and memory measurement

**Interfaces:** `scripts/odh-dry-run.py --odh <checkout path> --project <relative path of the
transmitter main board .kicad_pro> --answers <json> [--out <dir>]` (manual, not CI; run from
`backend/` with `uv run python ../scripts/odh-dry-run.py …` inside the kicad-base image with
Freerouting fetched). It copies the checkout **without `.git`** into
`tempfile.mkdtemp(prefix="odh-dry-run-")` (keeping the repo-relative footprint library paths,
spike Q1), refuses an `--out` inside this repository (nothing from ODH is ever committed),
starts a session on the copy, and calls `flow.run_flow` with the answers from the JSON
(`Answers` fields; `blocks: null` → the `pcb_analyze` proposal with `pcb_place_block`
regions from the answers or the edge table). Output in `--out` (default
`/tmp/odh-dry-run-<UTC date>`): `report.md` (per step: tool, seconds, cost, result summary;
final metrics table with verdicts; fill completion and ripped counts; open connections; fab
file list; total runtime), the renders, `metrics.json`. Memory: the api-server's peak RSS =
`VmHWM` of `/proc/<session.process.pid>/status` at the end (before and after
`session.activate_footprints()` — FP_WRITES in board sessions, spec §20); the JVM's peak =
`resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` read right after the fill (the JVM is the only
child that has exited by then; the api-server is still running) cross-checked by sampling the
JVM's `VmRSS` every 0.5 s; the CopperChat process peak = `RUSAGE_SELF.ru_maxrss`. The report
sums them as "per active board project" and compares with the D5 figure (≈ 4.5–5 GB).

- [ ] **Step 1: Failing unit tests** (`backend/tests/unit/test_odh_dry_run.py`, imports the
  script with `importlib` from its path): `test_refuses_out_inside_repo`;
  `test_copy_skips_git` (`tmp_path` fake checkout with `.git/` and a `.kicad_pro` → the copy
  has no `.git`); `test_answers_schema` (missing `outline` → usage error naming the field);
  `test_report_has_memory_section` (report builder on a fake `FlowResult` + fake memory numbers
  → headings "Metrics", "Memory", "Runtime" and the D5 comparison line).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS. **Step 5:
  Dry run** on the owner's ODH checkout (path from the coordinator; `timeout 3600`); paste
  `report.md` (and up to 3 renders) into the PR description; if a hard gate fails on ODH,
  report it with the offenders — that is an acceptance finding for the owner, not a reason to
  change thresholds.
- [ ] **Step 6: Commit** `feat(board): ODH dry-run script with runtime and memory report`.

### Task 4: Docs + index as built, checks, PR

- [ ] Index "As built (P9a)": e2e runtime on CI, golden values, any tool fixes made in other
  phases' files (with PR links), the `kicad-e2e` job, ODH dry-run summary (completion, hard-gate result, runtime) and the measured
  memory per active board project (api-server peak with/without footprints, JVM peak,
  CopperChat process) — **this is the D5 number P9b writes into the README**. No CHANGELOG
  entry (tests/CI only).
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, `uv run pytest -m "kicad and not kicad_e2e" tests/kicad/board` and `uv
  run pytest -m kicad_e2e` in the kicad-base image (all with `timeout`).
- [ ] PR `board phase P9a: end-to-end golden and ODH dry run` with the dry-run report;
  review-loop per the index's Phase Conventions.

## Acceptance (P9a)

- [ ] CI jobs `backend`, all 8 `kicad (…, i/8)` shards (now `-m "kicad and not kicad_e2e"`)
  and the new `kicad-e2e` job green; `test_sharding.py` proves the shards exclude the e2e test.
- [ ] `test_full_flow_hard_gates` passes with M2 = 0 and all hard gates, in ≤ 180 s.
- [ ] `test_metrics_golden`, `test_approvals_bound_to_hashes`, `test_undo_to_placement` pass.
- [ ] ODH dry-run report (metrics, renders, runtime, peak RSS of api-server + JVM) in the PR.
- [ ] Memory per active board project recorded in the index for P9b (D5).
