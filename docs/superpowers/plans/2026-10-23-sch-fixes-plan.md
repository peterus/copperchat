# sch_* fixes (v0.4.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the eight schematic-tool defects of PM topic #14 so that edits are correct, the tools agree with each other, and no failure path discards successful work; release v0.4.1.

**Architecture:** Bug fixes inside the existing edit stack (`backend/src/copperchat/kicad_tools/edit/`), the turn/recovery path (`agent/engine.py`, `agent/sdk_tools.py`, `history/hooks.py`) and, only if a cause lies in KiCad, patch files applied by `docker/kicad-base/Dockerfile`. Every item follows reproduce → locate → failing test → fix (spec §3).

**Tech Stack:** Python 3.12, KiCad IPC API via kicad-python, pytest (`-m kicad` against the real api-server in the CI `kicad` job), ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-10-23-sch-fixes-design.md` — read it first; §-references point there.

## Global Constraints

- Never hand-edit KiCad files; all edits through the IPC API (spec §2 anti-goals).
- Reproduce before fixing; the feedback's guesses are unchecked (spec §3). Each PR description lists, per item, the repro, the located cause and the status *fixed / not reproducible / in KiCad / open*.
- Regression tests against real KiCad (`pytestmark = pytest.mark.kicad`, helpers in `tests/support/edit.py`, `tests/kicad/edit/conftest.py`) wherever technically possible.
- KiCad / kicad-python fixes: local fork only, carried as patch files under `docker/kicad-base/patches/`; nothing submitted upstream (spec §13, D5). Large heuristic workarounds in CopperChat are not allowed.
- Sheet-edge clearance: **2.54 mm** (spec §8).
- Per-edit save cost threshold for asking the owner: **~0.5 s** per edit on the OpenDriveHub main schematic (spec §5.3).
- Repo language English; no Copilot; each PR: CI green, review-loop to 0 findings.

## Review Focus

1. **A save that fails mid-turn** (disk full, KiCad error): the user must hear it, and the next failing edit must not reload a state older than they were told. → Task A2 test `test_failed_per_edit_save_is_reported`.
2. **External-change detection after a mid-turn save:** the next turn must not report "Files changed outside CopperChat" for CopperChat's own saves. → Task A2 test `test_mid_turn_saves_are_not_external_changes`.
3. **Stop (cancel) during an edit** after earlier successful edits: nothing earlier is lost. → Task A2 test `test_cancel_after_successful_edit_keeps_it`.
4. **A stub chain whose far end is shared by two moved symbols** (both pins of a 2-pin part on one power net): carried once, not twice. → Task C1 test `test_move_two_pins_same_power_symbol_carried_once`.
5. **Parallel `sch_add_sheet_pin` calls on two different sheets** must not serialise against each other forever or deadlock with the edit lock. → Task B2 test `test_parallel_label_spots_distinct_and_other_sheet_unblocked`.

---

## Delivery

Five PRs, each its own child session (worktree, TDD, CI green, review-loop to 0). A, B, D, E start in parallel; C starts after D is merged (both touch routing checks). F (release) after all are merged.

| PR | Items | Tasks | Main files |
|----|-------|-------|------------|
| A | #6, #6b | A1–A3 | `kicad_tools/edit/engine.py`, `agent/engine.py`, `agent/sdk_tools.py`, `agent/sdk_backend.py`, `history/hooks.py` |
| B | left sheet pins, #5 | B1–B2 (+K if KiCad) | `kicad_tools/edit/sheets.py` |
| D | #9 | D1 | `kicad_tools/edit/router.py`, `connect.py`, `specs_core.py` |
| C | #7/#8 | C1 | `kicad_tools/edit/transform.py`, `specs_transform.py` |
| E | #10, #11, #14 | E1–E3 | `specs_place.py`, `labels.py`, `fields.py`, `place.py` |
| F | release | F1 | `CHANGELOG.md`, docs, version |

Task K (KiCad patch) is added to whichever PR finds a KiCad cause.

---

### Task A1: Root cause of the failed `sch_move` compensation (#6a)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/engine.py` (`_compensate`, ~448–564) — or where the repro points
- Test: `backend/tests/kicad/edit/test_engine_real.py`

**Interfaces:**
- Produces: compensation of any `sch_move` restores the document exactly (UUIDs or a documented remap in `run.replaced`).

- [ ] **Step 1: Reproduce.** Build a test project: wire with two PWR_FLAGs (#FLG01, #FLG02) on it, a symbol pin on the same wire. In one edit context: `sch_delete #FLG01`, then `sch_move #FLG02` from (60.96, 88.9) to (71.12, 91.44) with a forced verify mismatch (`_verify` seam) so compensation runs. Record which wire UUID goes missing and why (re-created with a new UUID? merged by KiCad? not in `run.deleted`/`run.side`?). Write the finding into the PR description.
- [ ] **Step 2: Write the failing test** `test_move_compensation_restores_split_wire`: after the forced mismatch, `apply_change_set` raises the original mismatch error (not `DocumentInconsistent`), and every wire UUID present before the move is present after it with identical endpoints; netlist partition equal to before.
- [ ] **Step 3: Run** `cd backend && uv run pytest -m kicad tests/kicad/edit/test_engine_real.py::test_move_compensation_restores_split_wire -v` → FAIL with `DocumentInconsistent … missing [...]`.
- [ ] **Step 4: Fix at the cause.** If KiCad cannot re-create an item with its UUID, that is Task K; the allowed small CopperChat fallback is matching the re-created wire by endpoints + net and remapping its UUID in `run.replaced` (as for re-placed symbols).
- [ ] **Step 5: Run** the test → PASS; run `uv run pytest -m kicad tests/kicad/edit/test_engine_real.py tests/kicad/edit/test_move.py tests/kicad/edit/test_undo.py` → PASS.
- [ ] **Step 6: Commit** `fix(edit): sch_move compensation restores wires exactly (#6)`.

### Task A2: Save after every successful edit (#6b, D1b)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/engine.py` (end of `apply_change_set`, ~811–821) or the two-phase wrapper in `backend/src/copperchat/agent/sdk_tools.py` — one place that all `apply_change_set` callers pass through after success (callers: `specs_core.py:417`, `specs_connect.py:93`, `specs_transform.py:125`, `specs_sheets.py:104/157`, `specs_place.py:563/702`, `duplicate.py:990`, `place.py:465`)
- Modify: `backend/src/copperchat/history/hooks.py` (`_recover_inconsistent` ~159, `_auto_restore` ~188, `on_turn_end` ~113)
- Test: `backend/tests/kicad/edit/test_engine_real.py`, `backend/tests/unit/agent/test_engine_document_inconsistent.py`, `backend/tests/unit/history/` (existing hook tests)

**Interfaces:**
- Consumes: `KicadSession.save(doc: DocKind) -> None` (`kicad_session/session.py:567`); save listeners re-record hashes (`history/service.py:_on_saved`).
- Produces: `async def save_after_edit(ctx: ToolContext, docs: frozenset[DocKind]) -> None` in `kicad_tools/edit/engine.py`, called once per successful mutating tool call with the docs it changed; raises `KicadUnavailable` / a save error that the wrapper reports like today's KiCad failures.

- [ ] **Step 1: Write the failing tests:**
  - `test_failed_compensation_keeps_earlier_unsaved_edit` (kicad): edit 1 `sch_delete` succeeds; edit 2 forced into `DocumentInconsistent` via `_before_compensate`; after the turn-end recovery the symbol deleted by edit 1 is still absent and the recovery notice does not say anything was lost.
  - `test_auto_restore_uses_last_save` (unit, hooks): with `auto_restore=True`, recovery reopens from the last save; plan-step checkpoint and turn snapshot are **not** restored automatically, but are still offered in the notice.
  - `test_failed_per_edit_save_is_reported` (unit): save raises → the tool result is an error with code `save_failed` and the turn is interrupted like `kicad_failed`.
  - `test_mid_turn_saves_are_not_external_changes` (unit, hooks): after mid-turn saves, `on_turn_start` of the next turn posts no "Files changed outside CopperChat".
  - `test_cancel_after_successful_edit_keeps_it` (kicad): cancel during edit 2's apply; edit 1's change is on disk.
- [ ] **Step 2: Run** them → FAIL.
- [ ] **Step 3: Implement** `save_after_edit` and call it after `record_applied`; change `_recover_inconsistent` so every inconsistency path reopens from the last save (schematic and board) and drops the automatic step-checkpoint / snapshot restore; keep the turn-end autosave (no-op when nothing is modified). Check each save consumer (hash recording, `publish_view`, history snapshot per run, ERC baseline in `editing/hooks.py`) still behaves with mid-turn saves.
- [ ] **Step 4: Measure** save time per edit on a copy of the OpenDriveHub main schematic (10 edits, median and max); put the numbers in the PR. If the median is above 0.5 s, stop and report to the coordinator before merging.
- [ ] **Step 5: Run** `uv run pytest -m "not kicad"` and `uv run pytest -m kicad tests/kicad/edit` → PASS.
- [ ] **Step 6: Commit** `fix(edit): save after every successful edit so failures never drop earlier work (#6)`.

### Task A3: Understandable notice, no bogus start failure (#6b, D2)

**Files:**
- Modify: `backend/src/copperchat/agent/sdk_tools.py` (~136–149), `backend/src/copperchat/history/hooks.py` (`_recovery_notice` ~222), `backend/src/copperchat/agent/engine.py` (~927–960, `_fail_burst` ~714), `backend/src/copperchat/agent/sdk_backend.py` (`_finished` ~435)
- Test: `backend/tests/unit/agent/test_engine_document_inconsistent.py`, `backend/tests/unit/agent/test_two_phase_adapter.py`

**Interfaces:**
- Consumes: Task A2 (last save = state before the failing edit).

- [ ] **Step 1: Write the failing tests:**
  - `test_inconsistent_tool_result_names_failed_edit`: the tool result text contains the failed tool and its target (e.g. `sch_move #FLG02`), says earlier edits of this turn are kept, and tells the agent to explain this to the user; no "compensating"/UUID jargon in the user-facing part (UUIDs may stay in a `detail` field).
  - `test_recovery_notice_says_nothing_lost`: notice after a normal inconsistency reads that the design is at the state before the failed edit and nothing else was lost, and recommends no restore; snapshot/checkpoint actions still listed as optional undo.
  - `test_interrupt_after_inconsistency_is_not_start_failure`: SDK error result with `stop_reason=tool_use` after a CopperChat-requested interrupt → `TurnFinished(reason="interrupted")`; no "The agent could not start" row.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the texts and mark the post-inconsistency `backend.interrupt()` as requested so `_finished` maps the result to "interrupted".
- [ ] **Step 4: Run** `uv run pytest tests/unit/agent tests/unit/history` → PASS.
- [ ] **Step 5: Commit** `fix(agent): plain-language inconsistency notice, no false start failure (#6)`.

### Task B1: Left sheet pins (SP)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/sheets.py` (`build_sheet_pin` ~204, `plan_add_sheet_pin` ~413)
- Test: `backend/tests/kicad/edit/test_sheets.py`

- [ ] **Step 1: Reproduce.** `sch_add_sheet`, then `sch_add_sheet_pin` left and right; save; plot with `kicad-cli sch export svg`; extract the two `(pin …)` s-expressions. In the KiCad GUI (or `kicad-cli`-loaded reference file committed as a fixture under `backend/tests/fixtures/`), place a left pin and extract its line. Diff `(at x y angle)` and `(effects … (justify …))`. Record the diff in the PR.
- [ ] **Step 2: Write the failing test** `test_left_sheet_pin_matches_gui`: the saved left pin's `at` angle and `justify` equal the GUI fixture's (expected per KiCad convention: left pin angle 180, `justify left`; right pin angle 0, `justify right` — confirm against the fixture), and the text bbox lies inside the sheet box.
- [ ] **Step 3: Run** → FAIL.
- [ ] **Step 4: Fix** — set what `build_sheet_pin` omits; if the IPC API ignores it, Task K.
- [ ] **Step 5: Run** `uv run pytest -m kicad tests/kicad/edit/test_sheets.py tests/kicad/edit/test_sheet_pin_connect.py` → PASS.
- [ ] **Step 6: Commit** `fix(sheets): left sheet pins orient text into the box`.

### Task B2: Hierarchical-label spots in the sub-sheet (#5)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/sheets.py` (`_label_spot` ~395, `_blockers` ~232)
- Test: `backend/tests/kicad/edit/test_sheets.py`

**Interfaces:**
- Produces: per-sheet `asyncio.Lock` registry `label_spot_lock(project_id: str, sheet_path: str) -> asyncio.Lock` held from spot search through apply of `sch_add_sheet_pin`.

- [ ] **Step 1: Reproduce** adding 20 sheet pins to one sheet; record after which pin "No room for the hierarchical label" appears and which blocker causes it.
- [ ] **Step 2: Write the failing tests:**
  - `test_twenty_sheet_pins_fit`: 20 sequential `sch_add_sheet_pin` on one sheet all succeed; the hierarchical labels do not overlap each other (bbox check).
  - `test_label_column_overflow_opens_second_column`: after the first column is full, the next label sits in a second column right of the first, not failing.
  - `test_parallel_label_spots_distinct_and_other_sheet_unblocked`: 5 concurrent calls on sheet A get 5 distinct spots; a call on sheet B completes while A's lock is held.
- [ ] **Step 3: Run** → FAIL.
- [ ] **Step 4: Fix** the cause found in Step 1 (tighter pitch on the 2.54 grid without label overlap, second column) and add the lock.
- [ ] **Step 5: Run** `uv run pytest -m kicad tests/kicad/edit/test_sheets.py` → PASS.
- [ ] **Step 6: Commit** `fix(sheets): compact hierarchical-label spots, serialised per sheet (#5)`.

### Task D1: One sheet-edge rule (#9)

**Files:**
- Create: `backend/src/copperchat/kicad_tools/edit/sheet_edges.py`
- Modify: `backend/src/copperchat/kicad_tools/edit/router.py` (`Obstacles` ~138–271, `route` ~391), `connect.py` (`plan_reroute` ~637), `specs_core.py` (`_check_path` ~214)
- Test: `backend/tests/kicad/edit/test_sheet_edges.py`

**Interfaces:**
- Produces: `SHEET_EDGE_CLEARANCE_MM = 2.54`; `def sheet_edge_problems(segment: tuple[Point, Point], sheets: Sequence[SheetBox], *, sheet_pins: Sequence[Point]) -> list[str]` — empty when the segment keeps ≥ 2.54 mm from every sheet edge, or touches an edge only at a sheet pin perpendicular to it. Used by `router.Obstacles.problems`, `_check_path`, `plan_reroute` and (Task C1) move rubber-banding.

- [ ] **Step 1: Write the failing table test** `test_sheet_edge_rule_same_for_all_tools`, parametrised over geometries {on edge, parallel at 1.27 mm, parallel at 2.54 mm, perpendicular into a sheet pin, perpendicular onto the edge without a pin} × tools {`sch_add_wire`, `sch_reroute`}: accepted exactly for "parallel at 2.54" and "perpendicular into a sheet pin"; plus `test_connect_keeps_sheet_edge_clearance`: `sch_connect` between a pin and a sheet pin next to the sheet produces no segment within 2.54 mm of an edge except the final perpendicular one, and its result passes `sch_reroute`'s check unchanged. Feedback cases: VBUS 1.27 mm above a sheet top edge; wires along x = 152.4 at an MCU-sheet edge.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `sheet_edge_problems` and use it in all three places; `router.route` treats the clearance band as an obstacle. Existing violating wires: tools warn (a `warnings` entry in the result), do not refuse.
- [ ] **Step 4: Run** `uv run pytest -m kicad tests/kicad/edit/test_sheet_edges.py tests/kicad/edit/test_connect.py tests/kicad/edit/test_connect_sheets.py tests/kicad/edit/test_rename_reroute.py tests/kicad/edit/test_add_wire.py` → PASS.
- [ ] **Step 5:** Update the descriptions of `sch_connect`, `sch_add_wire`, `sch_reroute` to state the rule. **Commit** `fix(edit): one sheet-edge clearance rule for connect, add_wire, reroute (#9)`.

### Task C1: `sch_move` carries owned stub chains (#7/#8)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/transform.py` (`plan_move` ~249, `along_ids` ~401–426, rubber-band ~473–590), `specs_transform.py` (description ~179)
- Test: `backend/tests/kicad/edit/test_move.py`

**Interfaces:**
- Consumes: `sheet_edge_problems` (Task D1) for rubber-banded wires.
- Produces: `def owned_stub_chains(scene: Scene, pin_points: Sequence[Point]) -> list[StubChain]` in `transform.py`; `StubChain(wires: list[str], end_items: list[str])` — wires reachable from a pin with no junction, no ≥3-way point, no other pin; `end_items` only power symbols, labels, no-connects; a chain ending at anything else (other pin, sheet pin, further wire) is not owned.

- [ ] **Step 1: Reproduce** C401 (Device:C vertical, +3V3 above, GND below, move +7.62 mm) and SW301; record today's result (L-shapes, power symbols left behind; "pin would land on a wire" on a follow-up move).
- [ ] **Step 2: Write the failing tests:**
  - `test_move_cap_carries_power_stubs`: after moving C401 by (7.62, 0), both stub wires and both power symbols are offset by exactly (7.62, 0), wire count unchanged, no new bends; netlist unchanged.
  - `test_move_symbol_stub_to_label_and_noconnect_carried`: same for a label end and a no-connect end.
  - `test_move_stub_to_other_pin_rubber_bands`: a stub ending at another symbol's pin is not carried; behaviour as before.
  - `test_move_stub_ending_at_sheet_pin_not_carried`.
  - `test_move_power_symbol_alone_rubber_bands`: moving only +3V3 leaves C401 in place; the wire is rubber-banded; no "pin would land on a wire" refusal.
  - `test_move_two_pins_same_power_symbol_carried_once`: a part with both pins on one GND symbol via a shared stub → GND carried once.
- [ ] **Step 3: Run** → FAIL.
- [ ] **Step 4: Implement** `owned_stub_chains`; add chains + end items to the moved set with the same offset; keep rubber-banding for the rest; relax the own-stub clash for a lone label/power move (still refuse unwanted connections).
- [ ] **Step 5: Run** `uv run pytest -m kicad tests/kicad/edit/test_move.py tests/kicad/edit/test_undo.py tests/kicad/edit/test_scenarios.py` → PASS.
- [ ] **Step 6:** Update the `sch_move` description. **Commit** `fix(move): carry stub chains and their power symbols, labels, no-connects (#7/#8)`.

### Task E1: `sch_set_fields` description vs. schema (#10)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/specs_place.py` (~811–835)
- Test: `backend/tests/unit/test_tools_registry.py`

- [ ] **Step 1: Write the failing test** `test_descriptions_name_only_schema_params`: for every spec in `tools.registry.all_specs()`, every backticked or `name=` parameter-like token in `description` that looks like a parameter (`` `foo` `` followed by `=` or listed as a parameter) exists in `input_schema["properties"]` (recursively). Keep an explicit allowlist for tokens that are values, not parameters; start it empty.
- [ ] **Step 2: Run** → FAIL on `sch_set_fields` (`value`, `mpn`, `manufacturer`).
- [ ] **Step 3: Fix** the description to `fields={"Value": …, "MPN": …}`; fix any other tool the test flags.
- [ ] **Step 4: Run** `uv run pytest tests/unit/test_tools_registry.py` → PASS. **Commit** `fix(tools): sch_set_fields description matches its schema (#10)`.

### Task E2: `sch_add_power` never touches neighbour pins (#11)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/labels.py` (`_power_candidates` ~758, `plan_power` ~784)
- Test: `backend/tests/kicad/edit/test_labels_power.py`

- [ ] **Step 1: Reproduce** with a 2.54 mm-pitch connector (microSD-like: VDD next to CMD, VSS next to DAT0): `sch_add_power` on VDD and VSS; record the stub geometry and why the existing checks miss the neighbour.
- [ ] **Step 2: Write the failing tests:** `test_power_stub_avoids_neighbour_pin_stub` — after `sch_add_power` on VDD and on CMD's neighbour, no stub end point or segment touches a foreign pin, stub or wire, netlist has no new merges; then `sch_add_label` on CMD succeeds. `test_power_no_free_variant_fails_clearly` — fully boxed-in pin → error naming the pin, no change applied.
- [ ] **Step 3: Run** → FAIL. **Step 4: Fix** the candidate check. **Step 5: Run** `uv run pytest -m kicad tests/kicad/edit/test_labels_power.py` → PASS.
- [ ] **Step 6: Commit** `fix(power): stubs never touch neighbour pins (#11)`.

### Task E3: Value field and overlap check (#14)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/fields.py` (`field_layout` ~57), `place.py` (`lay_out_fields` ~181, `clashes` ~219)
- Test: `backend/tests/kicad/edit/test_place.py`

- [ ] **Step 1: Reproduce** `sch_place_symbol` with `Connector_Generic:Conn_01x14`; record field positions vs. body and pins.
- [ ] **Step 2: Write the failing tests:** `test_conn_01x14_value_field_clear_of_body_and_pins` — Value and Reference bboxes intersect neither the body box nor any pin line; `test_overlap_check_reports_field_over_own_pins` — a forced layout with Value over pins is reported by `clashes`.
- [ ] **Step 3: Run** → FAIL. **Step 4: Fix** layout (a side without pins, else above/below the body outside pin extents) and extend `clashes` to field-vs-own-body/pins. **Step 5: Run** `uv run pytest -m kicad tests/kicad/edit/test_place.py tests/kicad/edit/test_fields.py` → PASS.
- [ ] **Step 6: Commit** `fix(place): value field clear of body and pins, checked (#14)`.

### Task K (conditional): KiCad / kicad-python patch

Only if a task finds the cause in KiCad.

**Files:**
- Create: `docker/kicad-base/patches/kicad/NNNN-<topic>.patch` and/or `docker/kicad-base/patches/kicad-python/NNNN-<topic>.patch`
- Modify: `docker/kicad-base/Dockerfile` (after the checkout at ~44 and ~137: `COPY patches/… ` + `git apply`), `.github/workflows/kicad-base.yml` if the image tag must change
- Create: `docs/kicad-patches.md` (per patch: defect, minimal repro, upstream MR text draft — not submitted)

- [ ] **Step 1:** Local fork clone outside the repo (`~/kicad-fork`, branch `copperchat` at the pinned `KICAD_COMMIT`); commit the fix with a minimal repro; `git format-patch` into `docker/kicad-base/patches/`.
- [ ] **Step 2:** Dockerfile applies patches in order and fails the build if one does not apply.
- [ ] **Step 3:** Build the base image locally, run the owning task's test against it → PASS; then the CI base-image workflow and the `kicad` job on the new image.
- [ ] **Step 4: Commit** `build(kicad-base): carry <topic> fix as patch`. Report to the coordinator: upstream MR draft ready for owner review.

### Task F1: Release v0.4.1

**Files:** `CHANGELOG.md`, version files (as in PR #51), `docs/user/*` editing/recovery docs, README pins.

- [ ] **Step 1:** CHANGELOG v0.4.1 with the per-item status list; user docs: per-edit save, the new notice, move behaviour, sheet-edge rule.
- [ ] **Step 2: Run** `scripts/check-docs.sh` → no errors.
- [ ] **Step 3:** Release PR; after review-loop and CI, verify `gh pr view <n> --json state` = `MERGED` before tagging `v0.4.1`; release workflow green.
- [ ] **Step 4:** Prod upgrade only after owner OK (backup `data/` with the app stopped).
