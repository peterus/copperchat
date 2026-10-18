# Phase E4 — Place, fields, free space, annotate

> Index contracts: E-C4–E-C9. Spikes: 2A (Q1 placement/refs, Q2 fields incl.
> multi-unit and the definition hang, Q7 shared sheets, Q9 boxes), 2D (tool table,
> failure modes 1, 2, 6; field placement rule). Reference code:
> `spikes/2d-agent-eval/tools.py` (`sch_place_symbol`, `_place_fields`,
> `_free_spot`, `sch_set_fields`, `_set_user_fields`), `tools_v2.py::sch_find_space`.

**Goal:** the agent finds room, places library parts with value/footprint/fields and
a correct reference, edits fields, and (on request) annotates — each verified and
budgeted.

**Files:**
- Create: `backend/src/copperchat/kicad_tools/edit/{place,fields,space,specs_place}.py`
- Modify: `tools/registry.py` (provider `copperchat.kicad_tools.edit.specs_place`)
- Tests: `tests/kicad/edit/test_place.py`, `test_fields.py`, `test_find_space.py`,
  `test_annotate_tool.py`; unit `tests/unit/edit/test_field_layout.py`

---

### Task 1: `sch_find_space`

**Interfaces:** Consumes `LibraryService.get_symbol` (E3), `read_scene` (E2). Produces
`space.find_space(scene, size_box, near, rotation, keep_free: list[Box]) -> list[tuple[float,float]]`
and tool `sch_find_space(lib_id, near: "REF.PIN" | "REF" | [x, y], rotation?, sheet?)` →
`{"candidates": [{"at_mm", "box_mm", "distance_mm"}], "size_mm"}` (≤ 3, nearest first).

- [ ] **Step 1: Failing kicad tests** (`ecc83_session`): `test_candidates_do_not_overlap`
  (each candidate's box, placed by a following `sch_place_symbol`, is accepted);
  `test_near_pin_keeps_pin_corridor_free` (near `U1.1`: no candidate box intersects the
  5.08 × 2.54 mm corridor in front of that pin, nor its own pins' corridors, 2D v2.1);
  `test_on_grid`; `test_unknown_near_ref` → `not_found`.
- [ ] **Step 2:** FAIL. **Step 3: Implement** spiral search on the grid around the anchor
  (pin end + outward direction offset), obstacles = bodies + per-field boxes + labels
  + corridors of every unconnected pin. **Step 4:** PASS. **Step 5:** commit.

### Task 2: `sch_place_symbol`

**Interfaces:** tool `sch_place_symbol(lib_id, at: [x,y], sheet?, rotation? (0|90|180|270),
mirror? ("x"|"y"), unit?, reference?, value?, footprint?, fields?: {name: value})` →
standard result + `placed: {ref, uuid, lib_id, value, footprint, at_mm, rotation, mirror,
box_mm, pins: [PinEnd JSON]}` and `notes` (preference deviation, active part without notes,
footprint `unverified`). Logical 1.

- [ ] **Step 1: Failing kicad tests**:
  - `test_place_resistor_with_auto_ref_value_footprint` (`blank_session`): ref `R1`, value
    `10k`, footprint `Resistor_SMD:R_0805_2012Metric`, on grid; `kicad-cli` netlist of a saved
    copy lists `R1` with that footprint.
  - `test_auto_ref_per_sheet_block` (`hier_session`, sheet `ampli_ht` instance on page 2) → `R2xx`.
  - `test_place_mirrored_rotated_canonical` (`Device:D`, 180 + mirror x) → read-back transform is
    the canonical form, pins at the computed ends (2A Q1 bug).
  - `test_overlap_refused_with_nearest_free_position` → `code="overlap"`, suggestion holds a
    position that `sch_find_space` also accepts; document unchanged, no snapshot.
  - `test_power_lib_id_refused` (`power:GND` → suggestion `sch_add_power`).
  - `test_unknown_footprint_refused` (closest matches) and `test_footprint_unverified_without_board` (note `unverified`, preference check from the name
    still runs; owner decision 3).
  - `test_preference_deviation_noted` (`Device:R` + `R_0603_1608Metric` → note, still placed).
  - `test_active_part_without_notes_noted` (`Amplifier_Operational:LM358`, no MPN).
  - `test_multi_unit_sets_fields_on_all_units` (LM358 placed unit 1, then unit 2 with the same
    `reference` → value identical on both; no server hang — every update definition-less, 2A Q2).
  - `test_fields_readable` (unit test of `_place_fields` port: Reference/Value horizontal, on a
    pin-free side, not overlapping the body box; rotated parts centred, 2D follow-up).
  - `test_shared_sheet_gets_unique_refs` (`hier_session`: place on one `ampli_ht` instance →
    after the tool, both instances have distinct refs `R2xx`/`R3xx`; 2A Q7 save + revert +
    `for_sheet(other)` workaround).
  - `test_new_pins_do_not_change_existing_nets` (verify only: existing partition unchanged).
- [ ] **Step 2:** FAIL. **Step 3: Implement** prepare: library detail, dry-run probe at the
  target for the real box, overlap check, reference via `Allocator`, footprint check
  (`library.footprint`), notes; stages: (1) `Place(...)` with the canonical orientation,
  (2) `Update` of the placed symbol: mirror flags, value, footprint, datasheet, custom fields,
  field positions (definition cleared); for a shared sheet a third stage after `save` +
  `revert` sets the other instances' references. Intent: `may_change` = the new pins only.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: sch_place_symbol`.

### Task 3: `sch_set_fields`

**Interfaces:** tool `sch_set_fields(ref, fields: {value?, footprint?, datasheet?, mpn?,
manufacturer?, <custom>?}, dnp?, exclude_from_bom?, exclude_from_board?, remove_fields?: [name])`.
Logical 1 per call. `mpn` → field `MPN`, `manufacturer` → `Manufacturer`.

- [ ] **Step 1: Failing kicad tests**: `test_value_footprint_dnp_round_trip` (persist after save +
  reopen; `kicad-cli` netlist shows `dnp`); `test_all_units_updated` (LM358 two units);
  `test_custom_field_added_hidden_near_symbol`; `test_existing_field_position_kept`;
  `test_deviation_note`; `test_unknown_ref_not_found`; `test_netlist_unchanged`
  (`keep_partition`).
- [ ] **Step 2:** FAIL. **Step 3: Implement** (one `Update` stage with every unit of the
  reference, on each unit's sheet instance). **Step 4:** PASS. **Step 5:** commit.

### Task 4: `sch_annotate`

**Interfaces:** tool `sch_annotate(scope: "new"|"sheet"|"all", mode: "keep"|"renumber", sheet?)`
→ `{renamed: [{uuid, old, new, sheet}]}`; logical = number of renamed references.

- [ ] **Step 1: Failing kicad tests**: `test_keep_assigns_only_unannotated` (`blank_session`:
  three symbols placed through a raw API commit without reference (`R?`) → `R1..R3`; existing
  refs untouched); `test_renumber_hierarchy_per_sheet` (`hier_session`, `scope=all,
  mode=renumber` on a copy where two refs were swapped → each ref in its page block, multi-unit
  parts keep one shared ref); `test_renumber_counts_against_budget` (budget 5 → `plan_required`);
  `test_power_symbols_numbered` (`#PWR` duplicates from headless placement → unique).
- [ ] **Step 2:** FAIL. **Step 3: Implement** with `Allocator`; renumber orders by sheet page,
  then position (top-left first). **Step 4:** PASS. **Step 5:** commit `feat: sch_annotate`.

---

**Done when:** one PR, CI green, review-loop done.
