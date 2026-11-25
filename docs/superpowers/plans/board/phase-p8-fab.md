# Phase P8 — JLCPCB fab package and 3D export

> Index contracts: B-C2 (`board_hash`), B-C3 (`require_board`), B-C7 (`compute_metrics` for
> the report), B-C8 (`FabPreset`), B-C10 (`require_board_approved`), B-C14
> (`output_path_unsafe`, `export_failed`, `not_approved`); index call 7 (plain `ToolSpec`s,
> cost 0, no change entry), Global Constraints "Fab". Spec §6.4 (fab outputs not snapshotted),
> §7.2 "Checkpoints and output", §8.3 board checkpoint, §14, §16 G7, spec calls 35, 57, 64, 67.
> Spike 4A Q8 (`spikes/4a-board/q08_fab.py`, port, never import). **Wave 8: starts when P3b and
> P7 are merged.**

**Goal:** after the board approval, one call writes an orderable JLCPCB package (gerbers,
drill, upload zip, BOM, CPL, fab report) into `<project>/fab/jlcpcb/` without touching KiCad
data and without ever leaving a half-written package; a second tool exports the 3D board to
`<project>/fab/3d/` through the same safe path helper.

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `cc/board/{fab,paths}.py`, `cc/kicad_tools/board/{fab,specs_fab}.py`
- Modify: `cc/kicad_tools/board/specs.py` (append two specs), `backend/tests/kicad/board/conftest.py`
  (append a `fab_rig` fixture on P5b's shared `routed_synth`)
- Tests: `backend/tests/unit/board/{test_paths,test_fab_plan}.py`,
  `backend/tests/kicad/board/{test_fab_export,test_export_3d}.py`

**kicad-cli options verified while planning** (`kicad-cli … --help` in
`ghcr.io/peterus/copperchat-kicad-base:52b43fb8…`): `pcb export gerbers` has `--layers`,
`--subtract-soldermask`, `--check-zones`, `--no-protel-ext`, `--precision`; `pcb export drill`
has `--format excellon`, `--excellon-units mm`, `--excellon-separate-th`, `--generate-map`,
`--map-format pdf|gerberx2|…`; `pcb export pos` has `--format csv`, `--units mm`, `--side
both`, `--exclude-dnp`; `pcb export bom` (board BOM) has `--fields`, `--labels`,
`--group-by`, `--exclude-dnp`; `pcb export step|glb` have `--force`, `--no-dnp`,
`--subst-models`, `--board-only`.

---

### Task 1: Safe output path and two-step swap — `cc/board/paths.py`

**Interfaces:**
```python
def safe_output_dir(project_dir: Path, rel: str) -> Path
    # rel = "fab/jlcpcb" | "fab/3d"; lstat each component below project_dir: a symlink → refuse;
    # the resolved target must be inside project_dir.resolve(); creates fab/ (mkdir, no follow)
    # if missing; returns the (possibly absent) target. Refusal: StructuredToolError(
    # code="output_path_unsafe", details={"path": rel, "reason": "symlink"|"outside"|"not_a_dir"})
def staging_dir(target: Path, call_id: str) -> Path       # target.parent / f".{target.name}.tmp-{call_id}", created empty
def swap_in(new: Path, target: Path, call_id: str) -> None
    # 1. target exists → rename target → target.parent / f"{target.name}.old-{call_id}"
    # 2. rename new → target; on failure rename the old one back, re-raise as export_failed
    # 3. rmtree(old); a failing rmtree is logged and reported, the export still succeeds
def replace_file(new: Path, target: Path) -> None          # os.replace inside the same dir (3D)
def sweep_stale(fab_dir: Path, target_name: str) -> list[str]
    # at export start: removes leftovers of crashed earlier exports in fab/ — only entries whose
    # name matches ^\.<target_name>\.tmp-[0-9a-f]{12}$ or ^<target_name>\.old-[0-9a-f]{12}$, only
    # when os.lstat says regular directory (a symlink or file of that name is skipped and logged,
    # never followed); rmtree each; returns the removed names (reported)
```
`call_id` = 12 hex chars from `uuid4`. Only paths under `project_dir/fab/` are ever created,
renamed or removed (asserted before every rename/rmtree). Leftovers can only exist after a
crash or kill mid-export (a normal failure cleans up); the exports run under the edit lock
(Task 3), so a sweep never removes a running export's staging dir.

- [ ] **Step 1: Failing unit tests** (`test_paths.py`, `tmp_path`): `test_creates_fab`;
  `test_symlinked_fab_refused` (`fab` → a dir outside the project → `output_path_unsafe`,
  `reason == "symlink"`, nothing created in the link target); `test_symlinked_target_refused`
  (`fab/jlcpcb` → `fab/other`); `test_outside_refused` (`rel = "../x"` → `"outside"`);
  `test_fab_is_file_refused` (`"not_a_dir"`); `test_swap_fresh` (no old dir);
  `test_swap_replaces_and_removes_old` (old content gone, no `.old-*` left);
  `test_second_rename_failure_restores_old` (patch `os.rename` to fail on the 2nd call → old
  package back at `fab/jlcpcb` byte-identical, `export_failed`); `test_rmtree_old_failure_reported`;
  `test_never_touches_outside_fab` (guard raises for a target outside `fab/`);
  `test_sweep_removes_leftovers` (`fab/.jlcpcb.tmp-0123456789ab/` and
  `fab/jlcpcb.old-0123456789ab/` with files → both removed, returned names);
  `test_sweep_skips_symlink_and_others` (`fab/jlcpcb.old-aaaaaaaaaaaa` → a symlink to a dir
  outside: link skipped and the target's files survive; `fab/jlcpcb.old-notes` and a regular
  file with a matching name untouched).
- [ ] **Step 2: Run** `uv run pytest tests/unit/board/test_paths.py -q` — FAIL. **Step 3:
  Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): safe fab output path with two-step swap`.

### Task 2: Fab planning — `cc/board/fab.py` (pure)

**Interfaces:**
```python
@dataclass(frozen=True) class FabCommand: args: tuple[str, ...]; out: str      # kicad-cli args, relative output
def fab_commands(model: BoardModel, board_file: str) -> tuple[FabCommand, ...]
def bom_rows(kicad_bom_csv: str) -> list[tuple[str, str, str, str]]   # (Comment, Designator, Footprint, LCSC Part #)
def cpl_rows(kicad_pos_csv: str) -> list[tuple[str, str, str, str, str]]   # (Designator, Mid X, Mid Y, Layer, Rotation)
def fab_report(model: BoardModel, *, board_hash: str, date: str, preset: FabPreset | None,
               metrics: MetricsReport, accepted: Sequence[AcceptedWarning],
               custom_rules: Sequence[tuple[str, str]], files: Sequence[tuple[str, int]]) -> str
```
- Gerbers: `pcb export gerbers --layers <copper layers>,F.Mask,B.Mask,F.SilkS,B.SilkS,F.Paste,B.Paste,Edge.Cuts
  --subtract-soldermask --check-zones -o gerbers/` (writes the `.gbrjob`; layer names checked
  against `--help`/a real run in Task 3).
- Drill: `pcb export drill --format excellon --excellon-units mm --excellon-separate-th
  --generate-map --map-format gerberx2 -o drill/`.
- BOM: `pcb export bom --fields "Value,Reference,Footprint,LCSC,MPN" --labels
  "Value,Reference,Footprint,LCSC,MPN" --group-by "Value,Footprint,LCSC,MPN" --exclude-dnp` →
  `bom_rows`: Comment = Value, Designator = refs, Footprint = the footprint name without the
  library prefix, 4th column = LCSC if set else MPN else empty; header `Comment,Designator,Footprint,LCSC Part #`
  (JLC template header, plan call).
- CPL: `pcb export pos --format csv --units mm --side both --exclude-dnp` → `cpl_rows`:
  `Designator,Mid X,Mid Y,Layer,Rotation` with `Top`/`Bottom`, numbers as `"%.4fmm"`, rotation
  `"%.1f"` — CSVs stay clean for upload; "not validated for JLC assembly" (spec §2) is stated
  in `fab-report.md` and in the tool result.
- `fab_report` (Markdown): board file + `board_hash`, date (UTC ISO), layer count, stackup
  (`model.stackup` or "KiCad default stackup"), outline size, preset id (the `jlcpcb-<n>l`
  preset whose layer count matches; `None` → "custom rules"), metrics table (id, value,
  verdict), accepted warnings (`board.md` → Checks rows), custom rules with reasons, order
  notes: thickness 1.6 mm, 1 oz outer, surface finish "lead-free HASL; ENIG if a pad pitch
  < 0.5 mm" (computed from the model), stencil "top only" when every SMD part is on top else
  "top + bottom", the file list with sizes, and the line "BOM/CPL not validated for JLC
  assembly (rotation offsets, LCSC numbers)". Every injected string Markdown-escaped
  (`|`, backticks, newlines).

- [ ] **Step 1: Failing unit tests** (`test_fab_plan.py`, builders + CSV strings produced by
  `kicad-cli` in Task 3 are **not** stored; unit tests use CSV text built in the test from
  rows): `test_gerber_layers_4l` (In1.Cu/In2.Cu present, paste both sides, `--check-zones`,
  `--subtract-soldermask`); `test_drill_separate_mm_map`; `test_bom_lcsc_then_mpn`;
  `test_bom_header`; `test_cpl_header_and_layer_names`; `test_report_sections` (hash, date,
  size, preset, metrics, order notes, "not validated for JLC assembly");
  `test_report_escapes` (a custom-rule reason with `|` and a newline stays one table cell);
  `test_enig_for_fine_pitch`; `test_stencil_sides`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): JLCPCB fab plan (commands, BOM/CPL, report)`.

### Task 3: `pcb_fab_export`

**Interfaces:** plain `ToolSpec("pcb_fab_export", …, mutating=True, changes_documents=False)`
(index call 7: writes no KiCad data, cost 0, no change-log entry), schema `{preset:
"jlcpcb"}` (`enum ["jlcpcb"]`, required). Handler: flag off → `board_tools_disabled`;
`require_board`; then the **whole export runs under `ctx.editing.edit_lock(ctx.project.id)`**
(when `ctx.editing` is set — the same lock every two-phase write holds from prepare to the
budget record, `cc/agent/sdk_tools.py::_run_two_phase_locked`), so no board write can land
between the approval check and the copy: `model = await board_model(ctx)`; `await
require_board_approved(ctx, model)` (refused `not_approved` unless the latest `board`
checkpoint's hash equals `board_hash(model)` and the last `pcb_check full` row for that hash
has `hard_failures == 0`); `swept = sweep_stale(fab_dir, "jlcpcb")`; `target =
safe_output_dir(project.dir, "fab/jlcpcb")`; `tmp = staging_dir(target, id)`;
`session.save_copy("pcb", tmp/"src"/<board>.kicad_pcb)` (the copy only — never the live
file; `save_modified=False`); **re-check**: the `board_hash` of the model read right after
`save_copy` returned (the copied state — still under the lock) must equal the approved hash,
else `rmtree(tmp)` and `not_approved` (`details.reason == "changed_during_export"`), nothing
swapped; run each `FabCommand` with `run_kicad_cli(settings, args,
timeout=120, cwd=tmp)` (non-zero → `export_failed` with the stderr tail); zip `gerbers/` +
`drill/` into `<board>-jlcpcb.zip` (`zipfile`, deflated, sorted names, fixed timestamps);
write `bom-jlcpcb.csv`, `cpl-jlcpcb.csv`, `fab-report.md` (metrics from `compute_metrics` on
the model with a fresh `run_drc_task`; a "Swept leftovers" line lists `swept` when non-empty);
remove `src/`; `swap_in(tmp, target, id)`; any error
→ `rmtree(tmp)` and re-raise (old package untouched). Result `data = {dir: "fab/jlcpcb",
files: [{path, bytes}], board_hash, swept, notes: ["BOM/CPL not validated for JLC assembly"],
next: "offer a git commit of fab/ (spec §7.2: fab/ is a normal repo directory)"}`.

- [ ] **Step 1: Failing kicad tests** (`test_fab_export.py`, module-scoped `fab_rig` = P5b's
  shared `routed_synth` — sync → outline → `pcb_set_rules {layers: 4, preset: "jlcpcb-4l", …}`
  → place + legalize → placement approved → `pcb_route` critical; the **board** approval is
  then set up by writing a `board` checkpoint with `BoardCheckpointRepo.approve` and a
  full-check row with `BoardReviewRepo.record_check(hard_failures=0)` for the current
  `board_hash` — DB setup only, P3b tests the guards): `test_file_set` (gerbers: one file per
  copper layer (4), F/B mask, F/B silk, F/B paste, Edge.Cuts, one `.gbrjob`; drill: separate
  PTH and NPTH Excellon files in mm (`METRIC` in the header), a map; the zip holds exactly
  the gerber + drill files; `bom-jlcpcb.csv` and `cpl-jlcpcb.csv` headers as Task 2, one CPL
  row per non-DNP footprint; `fab-report.md` contains the `board_hash`);
  `test_kicad_data_untouched` (`.kicad_pcb`, `.kicad_pro`, `.kicad_dru` sha256 and
  `session.modified_docs` unchanged; no change-log entry; budget not charged);
  `test_reexport_keeps_old_on_failure` (first export OK; second export with the zip step patched
  to raise → `export_failed`, the first package byte-identical, no `.jlcpcb.tmp-*` /
  `jlcpcb.old-*` left); `test_leftover_swept` (a `fab/.jlcpcb.tmp-0123456789ab/` and a
  `fab/jlcpcb.old-0123456789ab/` from a "crashed" export → removed, listed in `data.swept`
  and in `fab-report.md`); `test_reexport_replaces` (second clean export → new
  `fab-report.md` date, no leftovers); `test_symlinked_fab_refused` (`fab` → a dir in
  `tmp_path` outside the project → `output_path_unsafe`, link target empty);
  `test_symlinked_target_refused`; `test_not_approved_refused` (no checkpoint → `not_approved`);
  `test_stale_approval_refused` (`pcb_move {items: [{ref, x: x + 1}], unroute: true}` of one
  part after approving — the part may carry critical copper — succeeds, then `not_approved`);
  `test_concurrent_move_blocks_or_refuses` (export started as a task with the first
  `run_kicad_cli` call patched to wait on an event; a concurrent `pcb_move` through the
  adapter is still pending after 1 s (it waits for the edit lock); release → the export
  succeeds and `fab-report.md` carries the approved hash; the move then completes; a second
  case patches `save_copy` to move one part through a direct board commit before returning (a
  write that bypassed the lock) → `not_approved` with `reason == "changed_during_export"`, the
  old package untouched, no staging dir left);
  `test_hard_failure_check_refused` (latest full check row with `hard_failures=2` for the
  current hash → `not_approved`).
- [ ] **Step 2: Run** `uv run pytest -m kicad tests/kicad/board/test_fab_export.py` — FAIL.
  **Step 3: Implement.** **Step 4: Run** — PASS; record export seconds and package size.
- [ ] **Step 5: Commit** `feat(board): pcb_fab_export (JLCPCB package, board checkpoint, atomic swap)`.

### Task 4: `pcb_export_3d`

**Interfaces:** plain `ToolSpec("pcb_export_3d", …, mutating=True, changes_documents=False)`,
schema `{format: "step"|"glb"}` (required), cost 0, no approval needed. Handler:
under `ctx.editing.edit_lock(ctx.project.id)` (when set) like Task 3, `sweep_stale(fab_dir,
"3d")`, `safe_output_dir(project.dir, "fab/3d")` (mkdir), `save_copy("pcb", …)` into a staging dir
next to it, `kicad-cli pcb export step|glb --force -o <staging>/<board>.<ext> <copy>`
(timeout 180), `replace_file` into `fab/3d/<board>.<ext>`, staging removed. 3D needs no
approval, so the post-copy re-check compares the copy's `board_hash` with the hash read at
the start of the call (mismatch → `export_failed`, nothing replaced); the result's
`board_hash` is the copy's. Result `data =
{path, bytes, board_hash, models: "none"}` and the note "bare board: this image has no KiCad 3D
part models (gap G7; optional phase P10 adds them)" — the text is a constant
`BARE_BOARD_NOTE` that P10 replaces conditionally.

- [ ] **Step 1: Failing kicad tests** (`test_export_3d.py`): `test_step` (file starts with
  `ISO-10303-21`, note present); `test_glb` (first 4 bytes `glTF`); `test_symlinked_fab_refused`
  (`output_path_unsafe`); `test_failure_leaves_old_file` (an existing `fab/3d/<board>.step`,
  `kicad-cli` patched to fail → `export_failed`, old file byte-identical, no staging left);
  `test_kicad_data_untouched`; `test_export_3d_holds_edit_lock` (a concurrent `pcb_move` waits
  until the export returned).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): pcb_export_3d through the shared safe path helper`.

### Task 5: Docs + index as built, checks, PR

- [ ] Index "As built (P8)": exact gerber layer names accepted by `kicad-cli` at the pin, the
  produced file names, BOM header and LCSC/MPN fallback (plan call), CPL number format,
  export seconds and package size on `sp4_synth`, whether `--check-zones` refilled anything on
  the copy. CHANGELOG `[Unreleased]`: "`pcb_fab_export` (JLCPCB gerbers, drill, zip, BOM, CPL,
  fab report) and `pcb_export_3d` — behind COPPERCHAT_BOARD_TOOLS."
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, `uv run pytest -m kicad tests/kicad/board` in the kicad-base image (with
  `timeout`).
- [ ] PR `board phase P8: fab export`; review-loop per the index's Phase Conventions.

## Acceptance (P8)

- [ ] CI `backend` and all 8 `kicad (…, i/8)` shards green.
- [ ] `test_file_set`, `test_reexport_keeps_old_on_failure`, `test_symlinked_fab_refused`,
  `test_not_approved_refused`, `test_stale_approval_refused`, `test_hard_failure_check_refused`
  pass on the real server.
- [ ] `test_second_rename_failure_restores_old` passes (two-step swap rollback).
- [ ] `test_concurrent_move_blocks_or_refuses`, `test_leftover_swept` and
  `test_sweep_skips_symlink_and_others` pass.
- [ ] `pcb_export_3d` STEP and GLB tests pass; both tools leave KiCad files and
  `modified_docs` untouched.
