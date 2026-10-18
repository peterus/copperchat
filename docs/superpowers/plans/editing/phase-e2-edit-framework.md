# Phase E2 — Edit framework (geometry, two-phase adapter, verify engine)

> Index contracts: E-C4, E-C5, E-C6, E-C7, E-C8. Spikes: 2A (Q1 transforms/pins,
> Q4 `for_sheet`, Q5 cleanup side effects, Q6 kipy ignores item status /
> compensation with original UUIDs, Q9 boxes), 2D (failure modes 3, 4).
> Reference code: `spikes/2a-sch-write/geom.py`, `harness.py` (`update_symbols`,
> `by_id`, `wire`, `local_label`), `spikes/2d-agent-eval/geo.py` (scene, segment
> tests, `path_problems`), `tools.py` (`verify`, `expected_partition`).

**Goal:** every later tool is "prepare a `ChangeSet`, call `apply_change_set`": the
adapter refuses before any snapshot, the engine writes in commits, verifies by
read-back and compensates. Two tools prove it end to end: `sch_get_pin`,
`sch_add_wire`.

**Files:**
- Create: `backend/src/copperchat/kicad_tools/edit/{__init__,geometry,scene,changeset,engine,verify,annotate,specs_core}.py`
- Create: `backend/src/copperchat/editing/{__init__,state,gate,service,hooks}.py`
- Modify: `errors.py` (code/details, `DocumentInconsistent`), `tools/spec.py`, `tools/__init__.py`,
  `tools/registry.py` (provider `copperchat.kicad_tools.edit.specs_core`; registry check:
  a spec with `prepare` must have `apply`, `mutating`, `changes_documents`),
  `agent/sdk_tools.py`, `agent/engine.py` (`ChainedHooks`, `editing` on ctx,
  `document_inconsistent`), `agent/runtime.py` (`editing` attribute), `history/hooks.py`,
  `web/app.py` (lifespan: `install_editing(app)` after `install_history`)
- Tests: `tests/unit/edit/test_geometry.py`, `test_annotate.py`, `tests/unit/agent/test_two_phase_adapter.py`,
  `tests/unit/history/test_document_inconsistent.py`, `tests/kicad/edit/test_geometry_real.py`,
  `test_engine_real.py`, `test_add_wire.py`, `tests/support/edit.py` (`edit_ctx(session, **kw)`)

---

### Task 1: Geometry and scene

**Interfaces:** Produces E-C7 (`snap`, `Box`, `canonical_transform`, `PinEnd`, `pin_ends`,
`body_box`, `field_boxes`, `Scene`, `read_scene`, `resolve_sheet`, `resolve_pin`, `all_symbols`).

- [x] **Step 1: Failing tests.** Unit: `test_snap` (`snap(10.16) == 10.16`, `snap(10.2) == 10.16`);
  `test_canonical_transform_8_forms` (`(180, "x") -> (0, False, True)`; all 8 distinct, 12
  requests map onto them, cf. 2A Q1). Kicad (`blank_session`, place via dry-run-free commits):
  `test_pin_ends_match_kicad_for_all_transforms` — place `Device:D`, `Device:Q_NPN_BCE`
  and `Amplifier_Operational:LM358` unit 2 in the 8 canonical transforms, put a local label
  exactly on each computed `PinEnd.at` → every pin is on its label's net in `live_netlist`
  (port of 2A `canonical_8_transforms_round_trip`); `test_pin_direction_outward` (R at 0°:
  pin 1 `up`, pin 2 `down`); `test_body_box_contains_symbol` (R at (50.8, 50.8): box contains
  both pin ends, width ≤ 6 mm without margin); `test_resolve_pin` (`"U1.3"`, `"U1.+"`-style unique
  name; unknown pin → `code="not_found"` with candidate list; duplicated name →
  `ambiguous_pin`); `test_read_scene_subsheet` (`hier_session`: scene of the `ampli_ht`
  instance lists R201… with that instance's refs).
- [x] **Step 2:** FAIL. **Step 3: Implement** by porting `geom.py` (incremental
  `SetOrientation` composition; pins filtered to the instance's unit and body style,
  deduplicated by number) and `geo.py` (scene, one obstacle box per symbol body plus one
  per visible field — never a union box, 2D failure mode 2). All sub-sheet reads use
  `sch.for_sheet(instance)`. Field text boxes via `get_text_extents` when available,
  else 0.6 × size × chars.
- [x] **Step 4:** PASS. **Step 5:** commit `feat: schematic geometry and scene`.

### Task 2: Two-phase adapter, editing state, budget gate

**Interfaces:** Consumes E1 `EditingSettingsRepo`. Produces E-C4 adapter order,
`edit_spec`, `ToolContext.editing`, `.on_document_inconsistent`, E-C5 `EditTurnState`,
`EditGate`, `BudgetGate`, `EditingService`, `install_editing`, `ChainedHooks`, and the
E-C6 budget rule (in `BudgetGate.check_budget` / `record(ctx, cs)`, reading `cs.applied`).

- [x] **Step 1: Failing unit tests** (`test_two_phase_adapter.py`, fake specs whose
  `prepare`/`apply` record calls, fake `on_mutation`):
  - `test_read_only_refused_before_prepare_and_snapshot` → error `code="read_only"`,
    `prepare`/`on_mutation`/`apply` never called.
  - `test_budget_refusal_takes_no_snapshot` (settings budget 20, `logical.changes=21`) →
    `code="plan_required"`, suggestion names `plan_propose`; `on_mutation` not called.
  - `test_order_prepare_budget_snapshot_apply` (call log equals
    `["before_prepare","prepare","check_budget","on_mutation","apply","record"]`).
  - `test_budget_accumulates_over_calls` (8 + 8 ok, + 5 refused at 21 > 20).
  - `test_deleting_own_placement_is_free` (place 1 → `placed_this_turn={u}`; delete `u` →
    `logical_used` back to 0, `deleted_preexisting` 0); `test_fourth_preexisting_delete_refused`; `test_power_symbol_deletes_not_limited`.
  - `test_policy_never_has_no_budget`; `test_policy_always_requires_step` (`plan_step_required`).
  - `test_note_call_counts_every_tool` (`call_seq` increments for read-only tools too;
    `render_view` with `doc="sch"` sets `last_render_seq`).
  - `test_chained_hooks_call_in_order_and_propagate_first_error`.
  - registry: `test_two_phase_spec_requires_apply_and_flags`.
- [x] **Step 2:** FAIL. **Step 3: Implement** per E-C4/E-C5. Turn state lives in
  `EditingService._turns[(project_id, run_id)]`, dropped at turn end by `EditingHooks`.
  `install_editing(app)` sets `runtime.editing = svc` (the engine passes it into every
  `ToolContext`) and wraps `runtime.hooks_factory` → `ChainedHooks([history, EditingHooks])`.
- [x] **Step 4:** PASS. **Step 5:** commit `feat: two-phase mutating tool adapter and budget gate`.

### Task 3: Apply / verify / compensate engine and change recording

**Interfaces:** Produces `apply_change_set`, `Applied`, `edit_result`,
`verify.check(before: Netlist, after: Netlist, intent: NetIntent) -> list[str]`,
`ChangeEntry` rows via `EditingService.changes` (+ `on_changes_recorded` callbacks).

- [x] **Step 1: Failing kicad tests** (`test_engine_real.py`, `ecc83_session` unless noted):
  - `test_create_verified_and_logged`: a `ChangeSet` creating one wire between two free pin
    ends with `merge=[{a, b}]` → `Applied.created` has the wire; one `changes` row
    `op=add kind=wire` with the sheet path.
  - `test_mismatch_compensates_create` (intent demands a merge the wire cannot make) →
    `StructuredToolError(code="verification_failed")` with `details.expected/found`; UUID set
    and `live_netlist` identical to before; no `changes` rows.
  - `test_mismatch_compensates_update_and_delete`: update a label's text and delete another
    label + a no-connect in one stage, force mismatch by an injected verifier
    (`apply_change_set(..., _verify=lambda *a: ["forced"])`) → original UUIDs back, texts
    restored, netlist identical (2A Q6).
  - `test_compensated_symbol_delete_is_replaced_from_library` (`blank_session` copy **without**
    `.kicad_pcb`): delete one of two `Device:R` instances, forced mismatch → a symbol with the same ref, value, footprint, unit and
    transform exists again (new UUID, named in `details`), the saved file has no `:R_1`
    `lib_id`, netlist identical after remapping the old UUID to the new one, and the error code
    is `verification_failed` (not `document_inconsistent`).
  - `test_compensated_symbol_delete_with_board_requests_restore` (`ecc83_session`, has a board):
    same forced mismatch → no re-place; `DocumentInconsistent` with `auto_restore=True`
    (owner decision 4).
  - `test_one_stage_writes_two_sheet_instances` (`hier_session`: one `Update` of a symbol on
    the root plus one `Create` of a label with `sheet=<ampli_ht instance>` in the same stage →
    both read back on their own sheets; per-op `sheet`, index E-C4).
  - `test_staging_error_drops_commit`: a stage whose `build` raises → nothing changed, error.
  - `test_cleanup_side_effects_recorded`: delete the T-branch wire of a T with junction
    (create the T first) → `Applied.side_effects` lists the removed junction and the merged
    segment (`side_effect=1` rows), and the result's `side_effects` names them (2A Q5).
  - `test_two_stage_change_set`: stage 1 places `Device:R` with a reference, stage 2 (built
    from stage 1's placed item, definition cleared) sets its value → value read back.
  - `test_subsheet_stage_uses_for_sheet` (`hier_session`): update R201's value on the
    `ampli_ht` instance → re-read shows it (the unbound schematic would lose it, 2A Q4).
  - `test_compensation_failure_raises_document_inconsistent` (injected compensation error)
    → `DocumentInconsistent`.
- [x] **Step 2:** FAIL. **Step 3: Implement** `engine.py`: each `Stage` = one
  `session.commit(f"{cs.tool}: {stage.label}")`, ops executed with `for_sheet(op.sheet or cs.sheet)`; read-back, cleanup detection and
  compensation run per touched sheet
  (`Update` always with `definition` cleared — 2A Q2 server hang); after the last push read
  back created/modified ids **and** the neighbourhood (wires/junctions within 0.01 mm of any
  touched end) and compare with the pre-images to detect KiCad's cleanup; never trust kipy's
  per-item return (2A Q6) — check presence by `get_items_by_id`. `verify.check`: pins outside
  `may_change` keep their partition; each `merge` group on one net; `names` match; with
  `keep_partition` the partition is identical. Compensation in reverse stage order; non-symbol pre-images are re-created with
  `create_items` (original UUIDs); a deleted symbol (never `create_items` of a symbol pre-image,
  which is lossy, 2A Q6): without a `.kicad_pcb` re-placed from the library with its
  fields/unit/transform/body style copied (2A Q10 method A, new UUID); with a board →
  remaining compensation done, then `DocumentInconsistent(auto_restore=True)` (owner decision 4). `apply_change_set` stores the result in
  `cs.applied`.
- [x] **Step 4:** PASS. **Step 5:** commit `feat: verified apply engine with compensation and change log`.

### Task 4: `sch_get_pin` and `sch_add_wire`

**Interfaces:** Produces test helper `tests.support.edit.place_raw(session, lib_id, at, ref,
*, sheet=None, rotation=0) -> str` (UUID; one `apply_change_set` with `Place` + `Update`
stages) used by E5/E7/E8a tests that must not depend on E4, and provider
`copperchat.kicad_tools.edit.specs_core` with
`sch_get_pin(pin)` (read-only: `{ref, pin, name, type, at_mm, direction, sheet, net, free,
connected: [{kind, uuid}]}`) and `sch_add_wire(points: [[x,y],…], sheet?)` (two-phase;
logical 1).

- [x] **Step 1: Failing kicad tests** (`test_add_wire.py`, `ecc83_session`; Review Focus 1):
  - `test_add_wire_connects_two_pins` (points from `sch_get_pin` of two free pins) → result
    `connectivity_verified`, nets merged, `logical_changes == 1`, all points on grid.
  - `test_points_are_snapped` (input 0.3 mm off grid → stored on grid, note in result).
  - `test_end_off_pin_by_0_01mm_refused` → `code="verification_failed"` or a pre-write
    refusal; document unchanged (UUID set + netlist).
  - `test_interior_over_foreign_pin_refused_before_write` → `code="overlap"`, `why` names the
    pin (`R5.2`-style), nothing written, no snapshot (fake `on_mutation` counter).
  - `test_diagonal_segment_refused` (`invalid_argument`).
  - `test_get_pin_reports_net_and_free` (free pin → `free: true`, `net: null`-or-unconnected
    name; wired pin → connected wire UUID).
- [x] **Step 2:** FAIL. **Step 3: Implement** prepare: snap, orthogonality, foreign pin ends
  on segment interiors (`scene.foreign_pin_ends`), segments crossing symbol bodies refused;
  intent = merge of the pins at the two ends (if any) else `may_change` empty.
- [x] **Step 4:** PASS. **Step 5:** commit `feat: sch_get_pin and sch_add_wire`.

### Task 5: `DocumentInconsistent` turn handling

**Interfaces:** Produces `TurnOutcome.document_inconsistent`, engine callback
`_on_document_inconsistent`, HistoryHooks behaviour; error row
`metadata.code = "document_inconsistent"`.

- [x] **Step 1: Failing unit tests** (FakeAgent + fake session):
  `test_document_inconsistent_ends_turn_with_one_error_row`;
  `test_no_autosave_and_session_reopened` (`sessions.reopen` called once, `save` never);
  `test_recovery_notice_names_last_save_time` (notice text contains
  `session.last_saved_at`, metadata `{action: "restore", snapshotId: <turn snapshot>}`);
  `test_kicad_failed_editing_turn_uses_same_wording` (a turn that mutated the schematic);
  `test_auto_restore_restores_turn_snapshot` (`auto_restore=True` → at turn end the turn
  snapshot is restored without the busy check of the REST path — the turn is ending — and a
  notice says so; E8a prefers the run's last step checkpoint).
- [x] **Step 2:** FAIL. **Step 3: Implement** like `kicad_failed` (X18: the engine posts the
  error row, history never does); `_recover` text for mutated turns: "The design is at the
  state of <last save>; changes after that are lost." (spec §5.4).
- [x] **Step 4:** PASS. **Step 5:** commit `feat: document_inconsistent recovery`.

### Task 6: Annotation allocator

**Interfaces:** Produces E-C8 `Allocator`.

- [x] **Step 1: Failing tests**: kicad `test_per_sheet_blocks_on_hierarchy` (`hier_session`:
  scheme `per_sheet`; `next_ref("R", page of ampli_ht instance 2)` = first free `R2xx`;
  two calls give two different refs); `test_single_sheet_numbers_from_1` (`blank_session` → `R1`,
  then `R2`); unit (fake symbol lists) `test_flat_scheme_detected` (refs `R1..R40` across 3
  pages → `flat`, next `R41`), `test_block_full_error` (`R101..R199` used → error
  suggesting block 1000 + renumber), `test_block_1000`, `test_power_refs_unique`
  (`#PWR01`, `#PWR02` even when KiCad placed duplicates `#PWR01`), `test_multi_unit_shares_ref`
  (a ref already used by unit A is not "free" for a new part).
- [x] **Step 2:** FAIL. **Step 3: Implement** with page numbers from `get_hierarchy()` /
  sheet instance paths (2A Q7). **Step 4:** PASS. **Step 5:** commit `feat: per-sheet annotation allocator`.

---

**Done when:** one PR, CI green (`backend`, `kicad`), review-loop done, E-C4–E-C8 as built.

**As built (E2):**
- `sch_add_wire`: an end that snaps onto a pin end without being on it (e.g. 0.01 mm off)
  is refused before writing (`invalid_argument`, `why` names the pin) instead of being
  silently snapped onto the pin; other points are snapped with a note. Ends on an
  existing wire's interior get a junction (T-joint); a segment over a foreign wire end,
  label or junction, or running along a foreign wire, is refused (`overlap`), like a
  foreign pin end or a symbol body. Intent: the terminal pins plus one pin of every
  net touched at an end must end up on one net.
- `sch_get_pin` also returns `unit`, `symbol_uuid`, `page`; `net` is `null` for
  KiCad's `unconnected-…` nets.
- Test helpers live in `tests/support/edit.py`: `edit_ctx(session, db=None, **kw)` (with
  `db`: an `EditingService` and run id), `place_raw`, `place_direct` (plain commits),
  `call_tool` (through the real adapter), `two_resistors`, `free_area`, `get_item`,
  `kiids`; fixtures `blank_nopcb_session` (blank without `.kicad_pcb`) and `edit_db`
  (`tests/kicad/edit/conftest.py`). `tests.kicad.edit.helpers.edit_ctx` delegates.
- The engine-level `document_inconsistent` test is
  `tests/unit/agent/test_engine_document_inconsistent.py` (needs the agent fixtures).
