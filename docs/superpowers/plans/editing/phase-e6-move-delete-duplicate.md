# Phase E6 — Move, delete, duplicate

> Index contracts: E-C4–E-C8, E5 `router`. Spikes: 2A (Q4 move/rotate/mirror do not
> drag fields or wires — client-side drag in one commit; Q5 delete leaves dangling
> wires, push cleanup; Q6 symbol re-create limits; Q10 duplicate method A), 2D
> (`sch_move` must work on connected parts, `sch_delete` with attached items and
> `#PWR` refs). Reference code: `spikes/2d-agent-eval/tools_v2.py` (`_attached`,
> `sch_delete`, `sch_move`), `spikes/2a-sch-write/q04_transform.py`, `q10_duplicate.py`.

**Goal:** the agent corrects placements (move/rotate/mirror keeping connections),
removes parts cleanly, and copies blocks — verified, budgeted, undoable.

**Files:**
- Create: `backend/src/copperchat/kicad_tools/edit/{transform,delete,duplicate,specs_transform}.py`
- Modify: `tools/registry.py` (provider `copperchat.kicad_tools.edit.specs_transform`)
- Tests: `tests/kicad/edit/test_move.py`, `test_delete.py`, `test_duplicate.py`

---

### Task 1: `sch_move`

**Interfaces:** tool `sch_move(items: ["REF" | label/sheet uuid …], to?: [x,y], by?: [dx,dy],
rotation?, mirror?)` (exactly one of `to`/`by` unless only rotating; `to` only with one item).
Logical 1 per moved item.

- [ ] **Step 1: Failing kicad tests** (`blank_session`, small circuit built with E4/E5 tools):
  `test_move_connected_resistor_keeps_nets` (partition identical; wire ends at the new pin
  ends; fields moved by the same delta); `test_rotate_drags_wires_orthogonally` (diagonal
  results replaced by an L via the router; all segments orthogonal); `test_mirror_keeps_nets`;
  `test_label_on_pin_moves_with_symbol`; `test_move_into_overlap_refused` (nearest free
  position suggested; nothing changed); `test_drag_that_would_short_refused` (dragged wire
  crossing a foreign pin end → refused before write); `test_move_on_subsheet` (`hier_session`).
- [ ] **Step 2:** FAIL. **Step 3: Implement** one `Update` stage: symbol position/transform
  (canonical, definition cleared), each field shifted (rotated with the symbol), attached wire
  endpoints moved to the new pin ends; where a moved end makes a segment diagonal, replace
  that segment by a router L (`Create` + `Delete` in the same stage). Intent `keep_partition`.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: sch_move with wire dragging`.

### Task 2: `sch_delete`

**Interfaces:** tool `sch_delete(items: ["REF" | "#PWR03" | uuid | "sheet:<name>" …],
with_attached: bool = true)` → standard result + `nets_changed: [{net, before, after}]`.
Logical 1 per deleted symbol/label/sheet; wires/junctions/no-connects deleted directly count 1
per connection.

- [ ] **Step 1: Failing kicad tests**: `test_delete_with_attached_stubs_power_labels`
  (deleting `R1` removes its stubs, power symbols and labels attached only to it; wires shared
  with other pins stay); `test_delete_power_symbol_by_ref`; `test_nets_changed_reported`;
  `test_cleanup_side_effects_reported` (T-branch removal → merged segment and removed junction
  in `side_effects`); `test_delete_sheet_symbol_keeps_file` (`hier_session` copy: sheet gone
  from `get_hierarchy`, file still on disk, result says so);
  `test_compensation_replaces_symbol_from_library` (forced verification mismatch on a
  `74xx:74LS00` body-style-2 unit delete → re-placed, body style/unit/fields identical, new
  UUID reported, netlist back; index author call);
  `test_budget_counts_preexisting_only` (delete 4 pre-existing symbols → 4th refused;
  own placements free; deleting R1 with its attached `+5V`/`GND` power symbols counts as 1
  toward the ≤ 3 limit, E-C6).
- [ ] **Step 2:** FAIL. **Step 3: Implement** attached-item discovery (port `_attached`), pre-images
  of everything deleted plus neighbours, one `Delete` stage; `Logical.deleted_symbols` lists
  the UUIDs of deleted non-power symbols (attached power symbols excluded). Intent: `may_change` = pins of the deleted symbols' former nets.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: sch_delete`.

### Task 3: `sch_duplicate`

**Interfaces:** tool `sch_duplicate(items: ["REF" | uuid …], offset?: [dx,dy], at?: [x,y],
sheet?, count: int = 1, net_suffix: str = "_CH{n}")` (`n` = 2 for the first copy).
Logical = sum over copied items (E-C6).

- [ ] **Step 1: Failing kicad tests** (`ecc83_session` and a channel built in `blank_session`):
  `test_duplicate_channel_three_times_isomorphic` (count 3 → three netlist-isomorphic copies,
  local nets suffixed `_CH2.._CH4`, `GND`/global nets shared, spec §12);
  `test_symbols_copied_via_library_method_a` (fields, unit, mirror, body style identical; library
  link kept — no `:R_1` cache names in the saved file's `lib_id`s, 2A Q10);
  `test_copies_annotated_per_sheet_block`; `test_target_overlap_refused`;
  `test_duplicate_to_other_sheet` (`sheet=`); `test_saved_copy_renders_in_kicad_cli`
  (`kicad-cli sch export svg` exit 0).
- [ ] **Step 2:** FAIL. **Step 3: Implement**: non-symbol items = read protos, fresh UUIDs,
  offset, `Create`; symbols = `Place` + `Update` (method A, never `create_items` of a read
  symbol); stages: (1) place symbols + create wires/labels/junctions/no-connects, (2) symbol
  updates. Intent: merges mirror the source's partition among copied pins.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: sch_duplicate`.

---

**Done when:** one PR, CI green, review-loop done.
