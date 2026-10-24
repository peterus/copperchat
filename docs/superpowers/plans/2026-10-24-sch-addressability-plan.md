# sch_* addressability and ergonomics (v0.5.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the agent find, address and repair every schematic item it can create (sheet pins, labels, wires, rotations), declare intended connections, and work with ERC and the plan budget without needless friction; release v0.5.0.

**Architecture:** Extensions inside the existing edit stack (`backend/src/copperchat/kicad_tools/edit/`: targets, transform, delete, verify, sheets, geometry/fields, specs_*), the plan layer (`plans/machine.py`, `service.py`, `gate.py`, `repo.py`, `editing/state.py`), the ERC layer (`kicad_tools/checks.py`, `changes/erc.py`), one new read tool, and the plan card in the frontend (`frontend/src/components/plan/`, `components/chat/MessageItem.tsx`, `stores/plans.ts`). KiCad changes only as patch files under `docker/kicad-base/patches/` if a cause lies in KiCad.

**Tech Stack:** Python 3.12, KiCad IPC API via kicad-python, SQLite migrations (`backend/src/copperchat/db/migrations/`), pytest (`-m kicad` against the real api-server in the CI `kicad` job; `-m "not kicad"` for unit), ruff, mypy; React + TypeScript + Vitest in `frontend/`.

**Spec:** `docs/superpowers/specs/2026-10-24-sch-addressability-design.md` — read it first; §-references point there.

## Global Constraints

- Never hand-edit KiCad files; all edits through the IPC API (spec §2).
- No `allow_merge` or any switch that disables verification; the budget hard stop stays (spec §2, §7, §9.2).
- Per item: reproduce → locate → failing test → implement → status *done / in KiCad / open* in the PR description (spec §3).
- Real-KiCad tests: `pytestmark = pytest.mark.kicad`, helpers in `backend/tests/support/edit.py`, `backend/tests/kicad/edit/conftest.py`. Run: `cd backend && uv run pytest -m kicad <path> -v` (needs the kicad-base container, as in CI `.github/workflows/ci.yml` `kicad` job).
- KiCad / kicad-python fixes: local fork, patch files in `docker/kicad-base/patches/`, new pinned base image; nothing submitted upstream without the owner (spec §3).
- Exact values: sheet-pin snap tolerance **2.54 mm**, corner clearance **one `PIN_PITCH`**; `run_erc` page **50 / max 200**; ≤ **20** pattern rules per call; refusal list cap stays **`ERRORS_LISTED = 20`**; `sch_list_items` page **100 / max 500**; text constants **`CHAR_W=0.93`, `TEXT_PAD=0.1`, `TEXT_H=1.6`** (spec §5–§11).
- DB migrations: PR B uses `0010_plan_step_resume.sql`, PR C uses `0011_erc_step_justifications.sql`; whichever merges second rebases and checks the runner still applies both in order.
- Every changed/new tool: schema + description consistent (the v0.4.1 description-vs-schema test must pass), budget cost stated in one line, `skills/builtin/kicad-schematic-editing/SKILL.md` updated in the same PR.
- Repo language English; no Copilot; each PR: CI green, review-loop to 0 findings; before merging a sibling's successor: rebase on main + CI re-run.

## Review Focus

1. **`expect_net` on a symbol whose two pins land on the target net** (two different source nets) → refused as a short, rolled back. → Task E3 test `test_expect_net_two_pins_onto_target_refused`.
2. **Server restart / new run between "revise mid-step" and approval** → the resumed step still has its used count and own-placement refund ledger. → Task B2 test `test_step_counters_survive_new_run`.
3. **Deleting a sheet pin of a child file used by two sheet symbols** → the child hier label stays, a note says why. → Task D2 test `test_delete_sheet_pin_shared_child_keeps_label`.
4. **A pattern rule from step 1 must not pass a new error in step 2** → Task C2 test `test_pattern_rule_does_not_carry_to_next_step`.
5. **Rotating a sheet whose pins would land on a foreign wire / sheet edge rule** → refused, nothing written. → Task E1 test `test_rotate_sheet_collision_refused`.

---

## Delivery

Five PRs, each its own child session (worktree, TDD, CI green, review-loop to 0). Wave 1: A, B, C in parallel. Wave 2: D after A merged. Wave 3: E after D merged. Then R.

| PR | Items | Tasks | Main files |
|----|-------|-------|------------|
| A | #19 | A1 | `kicad_tools/edit/geometry.py`, `fields.py` |
| B | #13 | B1–B3 | `delete.py`, `transform.py`, `plans/machine.py`, `service.py`, `gate.py`, `repo.py`, `editing/state.py`, frontend plan card |
| C | #12 | C1–C2 | `kicad_tools/checks.py`, `kicad_tools/specs.py`, `changes/erc.py`, `plans/service.py`, `plans/specs.py` |
| D | #3, #1 | D1–D3 | new `kicad_tools/edit/list_items.py`, `specs_core.py`, `scene.py`, `targets.py`, `labels.py`, `transform.py`, `delete.py`, `sheets.py` |
| E | #2, #20, #4 | E1–E4 | `transform.py`, `verify.py`, `changeset.py`, `engine.py`, `specs_transform.py` |
| R | release | R1 | `CHANGELOG.md`, docs, version |

Task K (KiCad patch, as v0.4.1 plan Task K) is added to whichever PR finds a KiCad cause.

---

### Task A1: One set of text-extent constants (#19, spec §11)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/fields.py:21-26` (export constants), `geometry.py:274` (`_field_box`)
- Test: `backend/tests/unit/edit/test_geometry.py`, whole `backend/tests/kicad/` suite

**Interfaces:**
- Produces: `fields.text_width(text: str, size: float) -> float` is the single width formula; `geometry._field_box` uses it and `TEXT_H * size` for height.

- [ ] **Step 1: Failing test** `test_field_box_matches_layout_extent`: for a horizontal field "10k" size 1.27, `_field_box` width == `fields.text_width("10k", 1.27)` and height == `1.6 * 1.27`; rotated 90° swaps them; alignment handling unchanged (existing tests stay).
- [ ] **Step 2: Run** `uv run pytest tests/unit/edit/test_geometry.py -v` → FAIL (0.6 em).
- [ ] **Step 3: Implement** — `_field_box` imports from `fields.py`; no second constant set remains (`grep -rn "0\.6" kicad_tools/edit/geometry.py` shows none for text).
- [ ] **Step 4: Run** unit tests → PASS. **Run the full KiCad suite** `uv run pytest -m kicad tests/kicad -v`; for each newly failing test decide: new refusal/placement correct → update expectation/golden; wrong → fix. List every changed expectation with one-line reason in the PR description.
- [ ] **Step 5: Commit** `fix(geometry): field boxes use measured text extents (#19)`.

### Task B1: Budget counts logical operations (#13, spec §9.1)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/delete.py` (`_logical` ~441), `transform.py` (logical count ~900-944)
- Modify: tool descriptions in `specs_transform.py`, `specs_core.py`, `specs_connect.py`, `specs_place.py`, `specs_sheets.py` (one "Budget: …" line each)
- Test: `backend/tests/unit/edit/test_delete_logical.py` (new) or existing delete unit tests; `backend/tests/kicad/edit/test_delete.py`

**Interfaces:**
- Produces: `_logical(world, targets, placed_this_turn) -> tuple[int, list[str]]` — symbols 1 per part, sheets 1, sheet pins 1 (once D adds them), and **wires + junctions + labels + no-connects: 1 per distinct net together** (an unconnected item counts as its own net).

- [ ] **Step 1: Failing tests:** `test_delete_wire_run_with_junctions_and_label_costs_one` (3 wire segments, 2 junctions, 1 label, one net → 1); `test_delete_items_on_two_nets_costs_two`; `test_delete_symbol_and_its_net_items` (symbol 1 + net items 1 = 2); `test_move_carried_stubs_are_free` (move 1 symbol carrying 2 stub chains → 1).
- [ ] **Step 2: Run** → FAIL (labels/junctions count 1 each).
- [ ] **Step 3: Implement** net-grouped counting in `_logical`; `sch_move` counts primary targets only.
- [ ] **Step 4: Run** unit + `uv run pytest -m kicad tests/kicad/edit/test_delete.py tests/kicad/edit/test_move.py tests/kicad/edit/test_plan_flow.py` → PASS.
- [ ] **Step 5: Commit** `feat(budget): count logical operations per net (#13)`.

### Task B2: `plan_revise` from a running step (#13, spec §9.2)

**Files:**
- Modify: `backend/src/copperchat/plans/machine.py` (`_ALLOWED["revise"]` add `"running"`; hint text; reject of a revision → back to previous revision), `plans/service.py` (`revise` ~332, `reject`, resume), `plans/gate.py`, `plans/repo.py`, `plans/model.py`, `editing/state.py`
- Create: `backend/src/copperchat/db/migrations/0010_plan_step_resume.sql` (persist `step_used`, `step_placed` (JSON) on the step row; keep the previous revision's steps for reject-fallback)
- Modify: `plans/specs.py` (`plan_revise` description: allowed mid-step, call before exceeding)
- Test: `backend/tests/unit/plans/test_machine.py`, `test_service.py`, `test_gate.py`

**Interfaces:**
- Produces: `PlanService.revise(ctx, plan_id, ...)` accepted in `running`: marks the active step `paused` (not `failed`), stores its counters, plan → `proposed`, revision + 1, reason stored on the plan (`revise_reason: str`). On approve + `step_begin`/resume of the first unfinished step, counters are restored into `EditTurnState.step_used` / `step_placed`. `PlanService.reject` on a revision with a paused predecessor restores the previous revision, status `paused`.

- [ ] **Step 1: Failing tests:**
  - `test_revise_allowed_while_running` (machine): running → proposed, revision 2, interrupted step status `paused`.
  - `test_revised_step_inherits_used_count` (service): step expected 4, used 3, revise to expected 8 → after approval and resume, `step_used == 3`, limit `step_limit(8)`.
  - `test_step_counters_survive_new_run` (service, fresh `turn()` state after approval) → counters and own-placement ledger restored (Review Focus 2).
  - `test_reject_mid_step_revision_falls_back_paused` → previous revision, status `paused`, no steps lost.
  - `test_exceeding_still_hard_stops` (gate) → `plan_step_exceeded`, step failed (unchanged).
- [ ] **Step 2: Run** `uv run pytest tests/unit/plans -v` → new tests FAIL.
- [ ] **Step 3: Implement** machine, service, repo, migration as in Interfaces.
- [ ] **Step 4: Run** unit plans tests + `uv run pytest -m kicad tests/kicad/edit/test_plan_flow.py` → PASS.
- [ ] **Step 5: Commit** `feat(plans): revise from a running step, counters persisted (#13)`.

### Task B3: Re-posted plan card for approval (#13, spec §9.4)

**Files:**
- Modify: `frontend/src/components/chat/MessageItem.tsx`, `frontend/src/components/plan/PlanCard.tsx`, `frontend/src/stores/plans.ts`; backend plan DTO / event if the builder chooses a backend event (spec §9.4 leaves the mechanism open)
- Test: `frontend/src/components/plan/PlanCard.test.tsx`, a chat-list test

**Interfaces:**
- Consumes: plan DTO with `revision`, `status`, `revise_reason`, per-step `status` (`paused`).
- Produces: a chat item "plan revision N" positioned at the revision time; older cards of the same plan render collapsed `Superseded by revision N` (expandable, read-only).

- [ ] **Step 1: Failing tests:** `renders revised plan at the bottom with Approve`; `older card collapses to "Superseded by revision 2"`; `paused step shows "Paused – revision awaiting approval" and the reason`; `survives reload` (store rebuilt from API returns same layout).
- [ ] **Step 2: Run** `cd frontend && npx vitest run src/components/plan` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** frontend tests + typecheck/lint as in CI → PASS.
- [ ] **Step 5: Commit** `feat(ui): re-post revised plan for approval (#13)`; docs: user docs on plans.

### Task C1: `run_erc` sheet filter, ids, pagination (#12, spec §8.1)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/checks.py` (~237, `_entry` ~34-62), `kicad_tools/specs.py` (run_erc schema), `changes/erc.py` (reuse `violation_id`)
- Test: `backend/tests/kicad/test_checks.py`, `backend/tests/unit/` ERC tests

**Interfaces:**
- Produces: `run_erc(sheet: str | None = None, limit: int = 50, offset: int = 0)` → `{violations: [{id, type, severity, description, position_mm, items, sheet}], total, counts: {by_type: {..}, by_sheet: {..}}, offset, limit}`; `limit` > 200 → `invalid_argument`. `state.last_erc` keeps the unfiltered full result.

- [ ] **Step 1: Failing tests:** `test_run_erc_ids_match_step_done_ids` (same id as `violation_id`); `test_run_erc_sheet_filter`; `test_run_erc_pagination_total_and_counts`; `test_run_erc_limit_max_200`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Update `kicad-review-discipline/SKILL.md` (run_erc filter / ids).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(erc): run_erc sheet filter, ids, pagination (#12)`.

### Task C2: Pattern justifications persisted per step (#12, spec §8.2–8.4)

**Files:**
- Modify: `backend/src/copperchat/plans/service.py` (`_justifications` ~439, `step_done` ~462, `_refuse_erc` ~558, `_brief` ~136), `plans/specs.py` (~173), `plans/repo.py`, `changes/erc.py` (`ErcResults`)
- Create: `backend/src/copperchat/db/migrations/0011_erc_step_justifications.sql`
- Modify: `frontend/src/components/plan/PlanStepRow.tsx` (show justifications)
- Test: `backend/tests/unit/plans/test_service.py`, `backend/tests/kicad/edit/test_plan_flow.py`

**Interfaces:**
- Produces: `erc_justifications` accepts `{"<id>": "reason", ...}` (unchanged) **and** `erc_rules: [{match: {type?: str, sheet?: str}, reason: str}]` (≤ 20, at least one key in `match`). Stored per step on every call (also refused); retries merge (same id / same match replaces). Step result reports `rules: [{match, covered: int}]`. Refusal adds `total`, `counts.by_type`, `counts.by_sheet`, hint to `run_erc(sheet=…, offset=…)` and `erc_rules`; listed ids stay ≤ 20.

- [ ] **Step 1: Failing tests:** `test_pattern_rule_justifies_matching_new_errors`; `test_justifications_persist_across_retry` (refused call with 2 ids, retry with none for those → accepted); `test_pattern_rule_does_not_carry_to_next_step` (Review Focus 4); `test_rule_needs_type_or_sheet`; `test_more_than_20_rules_refused`; `test_refusal_has_total_and_counts`; `test_step_result_delta_bounded`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Plan card step row lists justifications (ids collapsed to count, rules with `covered`).
- [ ] **Step 4: Run** unit + kicad plan-flow + frontend tests → PASS.
- [ ] **Step 5: Commit** `feat(erc): pattern justifications persisted per step (#12)`.

### Task D1: `sch_list_items` and `sch_get_pin.stub_end` (#3, spec §10)

**Files:**
- Create: `backend/src/copperchat/kicad_tools/edit/list_items.py`
- Modify: `kicad_tools/edit/specs_core.py` (register tool; `_get_pin` ~72-101), `kicad_tools/edit/scene.py` (`items_at` ~157 includes sheet pins), `agent/profiles.py:25-59` (read allowlists)
- Test: `backend/tests/kicad/edit/test_list_items.py` (new), `backend/tests/kicad/test_sch_tools.py`

**Interfaces:**
- Consumes: `Scene`, `owned_stub_chains(scene, pin_points, moving)` (`transform.py:138`).
- Produces: tool `sch_list_items(sheet?, types?, bbox?, net?, text?, limit=100, offset=0)` → `{items: [{uuid, type, sheet, position_mm | start_mm+end_mm, text?, net, ...type fields}], total, offset, limit}`; types `label, global_label, hier_label, wire, junction, no_connect, sheet_pin, power, symbol, sheet`. `sch_get_pin` result gains `stub_end: [{uuid, type, text?, position_mm}]` and `stub_wires: [uuid]`; `connected` includes sheet pins.

- [ ] **Step 1: Failing tests:** `test_list_items_types_filter`; `test_list_items_bbox_and_net`; `test_list_items_text_glob`; `test_list_items_unconnected_label_net_null`; `test_list_items_pagination_limit_500`; `test_get_pin_stub_end_shows_label_at_far_end`; `test_get_pin_connected_includes_sheet_pin`.
- [ ] **Step 2: Run** `uv run pytest -m kicad tests/kicad/edit/test_list_items.py -v` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS; description-vs-schema test PASS.
- [ ] **Step 5: Commit** `feat(tools): sch_list_items and sch_get_pin.stub_end (#3)`.

### Task D2: Sheet pins as targets — move and delete (#1, spec §5)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/targets.py` (`Kind` l.36, `_SUPPORTED`, `_by_uuid` l.183), `labels.py` (`World.sheet_pin`/`endpoint` ~308-360: UUID form), `transform.py` (`MOVE_KINDS` l.91; sheet-pin move with edge snap / side change), `delete.py` (sheet-pin delete + child label rule), `sheets.py` (shared edge helpers with `_free_pin_y`, `PIN_PITCH`; `sch_add_sheet_pin` result returns pin + label UUIDs), `specs_transform.py`, `specs_sheets.py`
- Test: `backend/tests/kicad/edit/test_sheet_pin_edit.py` (new)

**Interfaces:**
- Produces: target kind `"sheet_pin"`; `snap_to_sheet_edge(sheet_box, point, pins_on_edges) -> tuple[side, point]` raising `invalid_argument` with edge extents if > 2.54 mm off any edge; orientation derived from side (v0.4.1 §6 left-pin fix reused).

- [ ] **Step 1: Failing tests:** `test_move_sheet_pin_by_uuid_along_edge`; `test_move_sheet_pin_by_name_to_other_edge_changes_side` (text into the box); `test_move_sheet_pin_far_from_edge_refused`; `test_move_sheet_pin_onto_other_pin_refused`; `test_rotate_sheet_pin_refused`; `test_delete_sheet_pin_removes_unconnected_child_label`; `test_delete_sheet_pin_keeps_connected_child_label_with_note`; `test_delete_sheet_pin_shared_child_keeps_label` (Review Focus 3); `test_duplicate_pin_name_still_refused`; `test_delete_sheet_pin_costs_one`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Compare the written `.kicad_sch` for a side change with a GUI-placed pin on that side.
- [ ] **Step 4: Run** new tests + `tests/kicad/edit/test_sheets.py tests/kicad/edit/test_move.py tests/kicad/edit/test_delete.py` → PASS.
- [ ] **Step 5: Commit** `feat(sheets): sheet pins addressable, movable, deletable (#1)`.

### Task D3: Docs for PR D

- [ ] Update `skills/builtin/kicad-schematic-editing/SKILL.md` (sheet-pin addressing ~l.67/74/96-100, `sch_list_items`, `stub_end`); run `uv run pytest tests/unit -k description` → PASS; commit `docs(skill): finding items, sheet-pin repair`.

### Task E1: Rotate / mirror labels and sheet symbols (#2, spec §6.1, §6.2, §6.4)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/transform.py` (`plan_move` ~359, refusal l.383-388), `specs_transform.py`
- Test: `backend/tests/kicad/edit/test_move.py`

**Interfaces:**
- Produces: `rotate`/`mirror` accepted for kinds symbol, label kinds, sheet; refusal text "rotate / mirror applies to symbols, labels and sheet symbols". Sheet transform = rectangle about centre + pin side/position remap via IPC (`SheetSymbol` has no orientation field).

- [ ] **Step 1: Failing tests:** `test_rotate_label_keeps_anchor_and_reads_correctly` (spin style matches GUI); `test_mirror_label`; `test_rotate_sheet_matches_gui_file` (compare `.kicad_sch` sheet block with a GUI-rotated reference fixture); `test_mirror_sheet_swaps_pin_sides`; `test_rotate_junction_refused`; `test_rotate_sheet_collision_refused` (Review Focus 5).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** If the API rejects pin side changes → Task K.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(move): rotate/mirror labels and sheets (#2)`.

### Task E2: Rotate / mirror carries owned stub chains (#20, spec §6.3)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/transform.py` (`owned_stub_chains` l.138-212 usage at ~549; rigid-only path), obstacle checks (`overlap_error`, `sheet_edge_problems`, `_short`)
- Test: `backend/tests/kicad/edit/test_move.py`, `backend/tests/unit/edit/test_stub_chains.py`

**Interfaces:**
- Produces: on rotate/mirror of a symbol or sheet, each owned chain (wire points + end items) gets the same rigid transform about the rotation centre; power symbols rotate to keep pointing away from the pin; labels' spin style follows stub direction; no-connects move. Any collision of carried items with foreign items → `invalid_argument`/`overlap` error naming pin, carried item and obstacle (UUID + position); nothing written.

- [ ] **Step 1: Failing tests:** `test_rotate_symbol_carries_power_and_label_stubs` (C401 / SW301 fixtures from v0.4.1); `test_mirror_symbol_carries_stubs`; `test_rotate_carried_stub_collides_refused_nothing_written`; `test_not_owned_chain_rubber_banded_on_rotate`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS + `tests/unit/edit/test_stub_chains.py`.
- [ ] **Step 5: Commit** `feat(move): rotate/mirror carries owned stubs (#20)`.

### Task E3: `expect_net` (#4, spec §7)

**Files:**
- Modify: `backend/src/copperchat/kicad_tools/edit/changeset.py` (`NetIntent` ~89: `expect_merge_with: frozenset[...] | None` resolved item-level group), `verify.py` (`check` ~104), `transform.py` (`_short` ~1028 allows exactly the declared merge; intent ~943), `engine.py` (error text), `specs_transform.py` (`expect_net` param)
- Test: `backend/tests/unit/edit/test_verify.py`, `backend/tests/kicad/edit/test_move.py`

**Interfaces:**
- Produces: `sch_move(..., expect_net: str | None)`. Before the move, `expect_net` resolves to its item-level group (pins, labels, sheet pins, wires); unknown → `invalid_argument`. After: exactly one moved group ∪ target group, all else identical; otherwise `verification_failed` + compensation, message naming the actual result.

- [ ] **Step 1: Failing tests:** `test_expect_net_floating_label_onto_wire_passes`; `test_expect_net_wrong_net_refused_and_rolled_back` (partition + UUIDs identical after); `test_expect_net_no_connection_refused`; `test_expect_net_two_pins_onto_target_refused` (Review Focus 1); `test_expect_net_unknown_name_invalid_argument`; `test_move_without_expect_net_unchanged`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS + `tests/kicad/edit/test_engine_real.py`.
- [ ] **Step 5: Commit** `feat(move): expect_net declares an intended connection (#4)`.

### Task E4: Acceptance scenario + docs (spec §12, §15)

**Files:**
- Modify: `backend/tests/kicad/edit/test_scenarios.py`; `skills/builtin/kicad-schematic-editing/SKILL.md`
- [ ] **Step 1: Test** `test_repair_wrong_sheet_pin_and_unknown_label`: fixture with a left sheet pin that belongs right and an off-wire label; via tools only: `sch_list_items` finds both → `sch_move` pin to right edge → `sch_move` label onto wire with correct `expect_net` passes; wrong `expect_net` refused and rolled back; no `sch_delete`/`sch_add_*` in the sequence; ERC clean or rule-justified.
- [ ] **Step 2: Run** → PASS (all pieces exist after D, E1–E3).
- [ ] **Step 3: Docs:** SKILL.md rotate/mirror scope, carried stubs, `expect_net`, budget table, revise mid-step, ERC rules. Commit `test(scenario): repair without delete-and-recreate`.

### Task R1: Release v0.5.0 (coordinator, after A–E merged)

- [ ] CHANGELOG v0.5.0 with per-item status (#1, #2, #3, #4, #12, #13, #19, #20); version bump; `scripts/check-docs.sh` → OK.
- [ ] Release PR → CI green → merge → verify `gh pr view --json state` == `MERGED` → tag `v0.5.0` → release workflow green.
- [ ] Prod upgrade only after owner OK (backup `data/` with app stopped).
- [ ] Delete own `review/<PR>/*` memory keys; `pm_report done` with status list.
