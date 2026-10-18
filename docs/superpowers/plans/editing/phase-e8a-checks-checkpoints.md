# Phase E8a — Step checkpoints, ERC delta, change log API, diff

> Index contracts: E-C3, E-C5, E-C10, E-C12. Spec §5.1 (ERC needs saved files,
> baseline, no double runs), §5.4, §8. Spikes: 2A Q8 (save + copy + ERC ≈ 2.9 s),
> 2C (diff JSON, identity = sheet path + UUID, exit code 5, compact add/remove
> properties; side effects). Reference code: `spikes/2c-diff-highlight/harness/`
> (`crosscheck.py`).

**Goal:** step checkpoints in the history store, ERC deltas against a cached
baseline at step end and editing-turn end, the change log and diff available over
REST/WS and to the agent.

**Files:**
- Modify: `history/store.py`, `history/hooks.py` (recovery offers step checkpoint),
  `web/routes_history.py`, `kicad_tools/checks.py` (`run_kicad_check_on_dir`, `run_erc` notes
  the report in the turn state)
- Create: `changes/{erc,diff,checkpoints,specs}.py`, extend `web/routes_editing.py`,
  `editing/hooks.py` (turn-end ERC delta), `agent/protocol.py` (`ChangesUpdatedFrame`),
  `protocol-fixtures/changes-updated.json`, `frontend/src/protocol/frames.ts`
- Tests: `tests/unit/history/test_step_checkpoints.py`, `tests/unit/changes/test_erc_delta.py`,
  `tests/unit/web/test_routes_changes.py`, `tests/kicad/edit/test_checks_real.py`

---

### Task 1: Step checkpoints in the history store

**Interfaces:** Produces `HistoryStore.snapshot(..., kind, plan_step_id)`, `Snapshot.kind/plan_step_id`,
`latest_step_checkpoint`, `take_step_checkpoint` (E-C10); history REST entries gain `kind`, `planStepId`.

- [ ] **Step 1: Failing unit tests**: `test_latest_for_run_ignores_steps`;
  `test_prune_counts_turns_only_and_cascades_steps` (`HISTORY_KEEP` turn snapshots kept; a pruned
  turn's step checkpoints removed, files too); `test_recover_uses_turn_snapshot` (crash after
  a step checkpoint → the restore notice still points at the turn snapshot, plus a second
  notice `{action: "restore", snapshotId: <step>, checkpoint: true}`);
  `test_auto_restore_prefers_last_step_checkpoint` (E2's `auto_restore` path restores
  `latest_step_checkpoint(run_id)` when one exists, else the turn snapshot);
  `test_take_step_checkpoint_saves_first` (fake session: `save("sch")` before the copy; hashes
  updated); `test_history_route_lists_kind`.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS. **Step 5:** commit `feat: step checkpoints`.

### Task 2: ERC baseline and delta

**Interfaces:** Produces `violation_id`, `ErcDelta`, `erc_baseline`, `current_erc`, `erc_delta`;
`EditTurnState.last_erc/last_erc_seq` set by `run_erc`; `erc_results` rows.

- [ ] **Step 1: Failing tests.** Unit (`test_erc_delta.py`, violation dicts):
  `test_new_error_detected`; `test_moved_item_not_new` (same type + UUIDs, other position);
  `test_position_breaks_ties` (two violations of one type on the same UUID set);
  `test_warning_reported_separately`; `test_resolved_listed`;
  `test_last_erc_reused_when_after_last_mutation` (`current_erc` does not call the runner).
  Kicad (`test_checks_real.py`, `blank_session` with a placed R via raw commit):
  `test_baseline_from_snapshot_files_cached` (first call runs `kicad-cli` on a temp copy of
  the snapshot dir incl. `.kicad_pro`/`sym-lib-table`, writes `<snapdir>/erc.json`; second call
  reads the cache); `test_delta_after_edit_reports_unconnected_pin` (new error type
  `pin_not_connected` with the R's UUID); `test_erc_freshness_subsheet_and_new_sheet`
  (spec §12: edit a sub-sheet of `hier_session` + create a new sheet → `run_erc` covers both).
- [ ] **Step 2:** FAIL. **Step 3: Implement**: refactor `run_kicad_check` into
  `run_kicad_check_on_dir(settings, project, doc_path, doc)` used by both; baseline runs on a
  copy of the snapshot (`KIPRJMOD` → project dir as today). **Step 4:** PASS. **Step 5:** commit.

### Task 3: Turn-end ERC delta for editing turns

**Interfaces:** `EditingHooks.on_turn_end` (runs after `HistoryHooks` via `ChainedHooks`);
notice metadata `{action: "erc-delta", ercResultId, errors, warnings}`.

- [ ] **Step 1: Failing unit tests**: `test_editing_turn_posts_erc_badge` (turn with `sch` in
  `changed_docs` and mutations → one `erc_results` row against the turn snapshot, one notice
  with counts); `test_non_editing_turn_no_erc`; `test_kicad_failed_turn_no_erc`;
  `test_turn_with_plan_step_done_skips_duplicate` (if the last step-end delta ran after the last
  mutation, reuse it); `test_erc_failure_posts_warning_not_error_loop`.
- [ ] **Step 2:** FAIL. **Step 3: Implement.** **Step 4:** PASS. **Step 5:** commit.

### Task 4: Change log REST/WS, `sch_changes`, diff

**Interfaces:** REST of E-C12 (`/changes/runs`, `/changes`, `/changes/diff`, `/erc-results/{id}`),
WS `changes-updated` (published via `EditingService.on_changes_recorded`), tool
`sch_changes(scope: "turn"|"step"|"plan" = "turn")`, `diff_against_snapshot`.

- [ ] **Step 1: Failing tests.** Unit routes: shapes/camelCase, 404 unknown snapshot, 400 bad ids,
  401; frames: `changes-updated` fixture valid/invalid in both suites (`frames.test.ts`,
  backend protocol test). Kicad: `test_diff_cross_checks_change_log` (`ecc83_session`: a tool
  sequence (E2 `sch_add_wire` + a raw value update through `apply_change_set`) → every logged
  UUID appears in the diff with the same op; diff-only entries are returned as
  `side_effect: true`); `test_diff_exit_code_5_is_success`; `test_diff_hierarchy_per_instance`
  (`hier_session` shared-sheet edit → one entry per sheet path); `test_sch_changes_tool_lists_turn`.
- [ ] **Step 2:** FAIL. **Step 3: Implement** diff: `save_copy` of the current design into a temp
  dir (full project dir), snapshot dir copy as "before", `kicad-cli sch diff --format json`
  (exit 0/5 ok, else error); compact properties of added/removed items to `{name}` only.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: change log API, diff and sch_changes`.

---

**Done when:** one PR, CI green (backend, kicad, frontend for `frames.ts`), review-loop done.
