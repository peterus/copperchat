# Phase E7 — Hierarchical sheets and sheet pins

> Index contracts: E-C4–E-C8. Spike: 2A Q7 (sheet creation, page numbers, relative
> `Sheetfile` bug + workaround, sheet pins ↔ hierarchical labels, shared sheets),
> Q8 (new sheet only on disk after `save()`). Spec §10 (file name validation).
> Reference code: `spikes/2a-sch-write/q07_sheets.py`.

**Goal:** the agent creates sub-sheets (new files) and sheet pins with matching
hierarchical labels, and can place parts on them with the existing tools.

**Files:**
- Create: `backend/src/copperchat/kicad_tools/edit/{sheets,specs_sheets}.py`
- Modify: `tools/registry.py` (provider `copperchat.kicad_tools.edit.specs_sheets`)
- Tests: `tests/unit/edit/test_sheet_names.py`, `tests/kicad/edit/test_sheets.py`

---

### Task 1: `sch_add_sheet`

**Interfaces:** Produces `sheets.validate_sheet_file(project, file, *, existing: set[str]) -> str`
(relative posix path) and tool `sch_add_sheet(name, file, at: [x,y], size?: [w,h] = [50.8, 38.1],
parent?: sheet)` → standard result + `sheet: {name, file, path, page, uuid}`. Logical 1.

- [ ] **Step 1: Failing tests.** Unit: `test_valid_names` (`power.kicad_sch`, `sub/power.kicad_sch`);
  `test_rejected_names` (`../x.kicad_sch`, `/abs.kicad_sch`, `a/b/c.kicad_sch` (two dirs),
  `x.sch`, `x.kicad_sch` that exists on disk but is not in the hierarchy, symlinked
  directory); `test_existing_hierarchy_file_allowed` (re-use = second instance).
  Kicad (`blank_session`, `hier_session`):
  - `test_new_sheet_file_written_and_relative` → after the tool: `get_hierarchy()` has the sheet
    with the next page number (`"2"` / `"4"`), the `Sheetfile` field is the relative name, and
    after `session.save("sch")` the file exists and `kicad-cli sch export netlist` succeeds.
  - `test_place_on_new_sheet` (an E2 `ChangeSet` with a `Place` stage on the new `SheetRef`
    and a reference from `Allocator.next_ref("R", "2")` → `R201` read back on that sheet —
    E4's tool is not a dependency of this phase; Review Focus 3).
  - `test_save_copy_contains_new_sheet` (E1 `save_copy` → file in the copy; `run_erc` works).
  - `test_overlap_refused`; `test_hashes_recorded_after_save` (no external-change notice at the
    next turn start).
- [ ] **Step 2:** FAIL. **Step 3: Implement** stages: (1) `Create` `SheetSymbol` with the
  **absolute** file path, name, position, size; (2) `Update` its `Sheetfile` field to the
  relative path (2A Q7 bug); then `session.save("sch")` so the file exists (hash listener runs).
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: sch_add_sheet`.

### Task 2: `sch_add_sheet_pin`

**Interfaces:** tool `sch_add_sheet_pin(sheet, name, direction: "input"|"output"|"bidirectional"|"tri_state"|"passive", side?: "left"|"right")`
→ standard result + `pin: {sheet, name, at_mm}`, `label: {uuid, at_mm, sheet}`. Logical 1.

- [ ] **Step 1: Failing kicad tests**: `test_pin_and_label_connected` (sheet pin on the sheet
  edge, hierarchical label of the same name inside via `for_sheet`, live netlist and
  `kicad-cli` netlist connect a wire on the pin with a pin inside the sheet);
  `test_duplicate_pin_name_refused`; `test_pins_stack_without_overlap` (three pins on one side,
  2.54 mm apart, on grid); `test_shared_sheet_label_once` (sheet file with two instances →
  one label in the file, a pin on the chosen sheet symbol only).
- [ ] **Step 2:** FAIL. **Step 3: Implement** one stage: `Update` of the `SheetSymbol.pins` +
  `Create` of the hierarchical label on the sheet (2A Q7). **Step 4:** PASS. **Step 5:** commit.

---

**Done when:** one PR, CI green, review-loop done.
