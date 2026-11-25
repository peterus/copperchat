# Phase P2 — Board from schematic, outline, rules

> Index contracts: B-C4 (consumes, incl. `BoardStage.compensable` / `.undo` that P1a
> implements), B-C8 (consumes P1a's `FabPreset`, `PRESETS`), B-C11 (`staging_positions`),
> B-C14; index calls 6, 8, 13. Spec §4 (C1–C4, C8), §6.2 (rules persistence), §6.3 (netlist exception), §6.4,
> §7.2 "Board from schematic" + "Setup", §10.3 (layer type probe), §16 (G2, G3, G10).
> Spike 4A Q1, Q2 (`spikes/4a-board/{q01_netlist,q02_setup,boards}.py`). **Wave 3: starts
> when P1b and P1c are merged**; parallel to P3a (shared `board/specs.py` append-only).

**Goal:** the agent turns the `sp4_synth` schematic into a board with all footprints in a
staging area, gives it a rounded-rectangle outline with mounting holes and keepouts, and sets
layer count, JLCPCB rules, net classes and reasoned custom rules — every call compensated on
failure and undoable from History.

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `cc/kicad_tools/board/{sync,setup,specs_setup}.py`, `cc/board/staging.py`
- Modify: `cc/kicad_tools/board/specs.py` (append the three specs). Consumes P1a's
  `cc/board/presets.py` (unchanged here) and P1a's `BoardStage.compensable` / `.undo`.
- Tests: `backend/tests/unit/board/{test_staging,test_setup_schemas}.py`,
  `backend/tests/kicad/board/{test_sync,test_outline,test_rules}.py`

---

### Task 1: `pcb_sync_from_schematic` (create + reopen, staging, undo)

**Interfaces:** B-C11 `staging_positions(model, refs) -> tuple[Move, ...]` (`Move` is defined
in `cc/board/staging.py` for now with the B-C11 fields; P4's `placer` re-exports it):
column-major grid of the given parts sorted by natural ref order, origin = 5 mm right of
the outline (`outline.x + outline.w + 5`, top `outline.y`) — or, without an outline, 5 mm
right of the bounding box of the board's other footprints — or `(0, 0)` on an empty board;
cell = part courtyard bbox + 1 mm gap; a column ends at the outline height (100 mm without an
outline); rotation and side unchanged. (Comment in `staging.py`: "B-C11, spec §7.2 staging
area".) P1a implements compensable/undo per B-C4: the import stage below sets
`compensable=False`, so a later failure raises `sync_failed` naming the History panel
(spec §6.3, the one documented exception).

Tool `pcb_sync_from_schematic {delete_extra?: bool = true, dry_run?: bool = false}` (two-phase,
cost `Logical(3)`, kind `board_sync_from_schematic`, `creates_board = project.pcb_file is None`):
- **prepare:** flag off → `board_tools_disabled`; schematic modified
  (`session.modified` or `h.schematic().is_document_modified()`) → `schematic_unsaved`;
  `run_kicad_cli(settings, ["sch", "export", "netlist", "--format", "kicadsexpr", "-o",
  <agent_dir>/board/netlist.net, <sch_file>], timeout=120)` (G2: never the API job) →
  non-zero → `sync_failed` with stderr tail. With a board: `Board.import_netlist(dry_run=True,
  delete_extra_footprints=…)` → `error_count > 0` → `sync_failed` listing the report's error
  lines (nothing written). `dry_run: true` → return the dry-run report as a read-only result
  (`changed_docs` empty, cost 0).
- **apply stages:** (0, engine) create board + reopen when boardless; (1) dry run on the new
  board when it was created now (`commit=False`; errors → `sync_failed`, the engine removes the
  created file); (2) real `import_netlist(delete_extra_footprints=delete_extra)` —
  `commit=False`, `compensable=False`; (3) move every footprint that is new since the
  pre-call model to `staging_positions` (`commit=True`).
- **report (step report):** `added`, `removed`, `changed` (refs; changed = value or `lib_id`
  differs), `nets_added`, `nets_removed`, `warnings` (each KiCad warning line, plus
  `"suggestion": "check the footprint of <ref> with fp_check"` when a line names a pad
  without net or a missing pin), `footprints`, `unplaced`, `staging_box {x,y,w,h}`,
  `next: "pcb_set_outline"`.

- [ ] **Step 1: Failing unit tests** (`test_staging.py`, builders): `test_right_of_outline`
  (every move x ≥ outline right + 5); `test_no_outline_right_of_parts`; `test_empty_board_origin`;
  `test_no_overlaps` (courtyards of staged parts pairwise disjoint, seeded `random.Random(7)`
  over 200 random part sets — print the seed on failure); `test_deterministic`;
  `test_wraps_columns` (outline 20 mm high, 30 parts → ≥ 2 columns).
- [ ] **Step 2: Failing kicad tests** (`test_sync.py`, `synth_session` + P1a's `board_ctx`
  helper from `tests/kicad/board/conftest.py`, calls through the real two-phase adapter):
  - `test_sync_create_then_undo` (**first test written**): sync on the boardless fixture →
    `.kicad_pcb` exists, `docs_of == {"sch", "pcb"}`, footprint count == fixture part count;
    History restore of the turn snapshot → `.kicad_pcb` deleted, the session restarts and
    answers `project_info`, `docs_of == {"sch"}`, no "could not open" error posted.
  - `test_sync_all_staged`: on the new empty board every footprint's courtyard lies at
    x ≥ 0 (staging right of `(0, 0)`), no courtyard overlap (DRC task: 0 `courtyards_overlap`), `unplaced == footprints`.
  - `test_resync_keeps_placement`: move U1 and C1 through a board commit; change R4's value
    through a schematic commit + `save("sch")`; re-sync → U1/C1 positions unchanged (0.001 mm),
    R4 value updated, `changed == ["R4"]`, `added == []`.
  - `test_delete_extra`: delete R1 in the schematic (API, save) → re-sync with default
    `delete_extra` removes R1 (`removed == ["R1"]`); with `delete_extra: false` R1 stays.
  - `test_schematic_unsaved`: an unsaved schematic commit → `schematic_unsaved`, board unchanged.
  - `test_pad_warning_suggests_fp_check`: set U3's footprint field to
    `Package_TO_SOT_SMD:SOT-23` (3 pads) through the schematic API + save → sync report has a
    warning naming U3 and the `fp_check` suggestion.
  - `test_dry_run_writes_nothing` (existing board: hashes unchanged, cost 0).
  - `test_failure_after_clean_dry_run`: patch the import stage to raise after the dry run →
    `sync_failed`, suggestion names the History panel, no compensation attempted (a spy on
    `compensate` is not called).
  - `test_failure_on_created_board_dry_run`: patch the stage-1 dry run to report an error →
    `sync_failed`, no `.kicad_pcb` left, `docs_of == {"sch"}`.
  - `test_budget_cost_3`.
- [ ] **Step 3: Run** `uv run pytest tests/unit/board/test_staging.py` and
  `uv run pytest -m kicad tests/kicad/board/test_sync.py` — FAIL. **Step 4: Implement**
  (register the spec in `board/specs.py`). **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): pcb_sync_from_schematic with staging area`.

### Task 2: `pcb_set_outline`

**Interfaces:** schema (plan call 6): `shape: "rect"` (required), `width`, `height` (5 ≤ v ≤
500), `corner_radius` (0 ≤ r ≤ min(w, h)/2, default 0), `origin {x, y}` (|v| ≤ 1000, default
`{x: 100, y: 100}` — the top-left corner), `holes {footprint?: lib_id (maxLength 128,
default "MountingHole:MountingHole_2.2mm_M2"), inset (0.5 ≤ v ≤ 50, default 3.0),
positions?: [{x, y}] ≤ 16}` (no `holes` → no hole is touched), `keepouts?: [{name, polygon
(3–256 points) | rect {x,y,w,h}, layers (≤ 32, enabled layers or "all_copper"), no:
["tracks","vias","copper","footprints"] (1–4)}] ≤ 16`. Cost `Logical(2)`, kind
`board_set_outline`. Stages (each one board commit): (1) remove every Edge.Cuts shape, create
one `BoardRectangle` (stroke 0.05 mm, `corner_radius`); (2) holes, only when `holes` is given:
targets = `positions` or the 4 corners inset by `inset`; existing `MountingHole*` footprints
**with** a `symbol_path` (schematic holes) are moved to the first targets (natural ref
order) and locked; tool-placed holes (`MountingHole*` lib, empty `symbol_path`) are removed;
remaining targets get `place_footprint_from_library(footprint)` after a lazy
`get_library_footprint`, ref `H<n>` (next free), locked, never in a `blk:` group; unknown
footprint → `not_found` with `closest`; (3) keepouts: rule areas (`ZT_RULE_AREA`) with the
given names are replaced, other rule areas kept. Read-back verify: one Edge.Cuts rectangle
with the requested geometry (0.001 mm), hole count == targets, keepout names present.
Report: outline, `holes {moved, placed, removed}`, `keepouts`, `unplaced` (now meaningful),
`placement_hash` delta (changed: true), `next: "pcb_set_rules"`.

- [ ] **Step 1: Failing unit tests** (`test_setup_schemas.py`): bounds rejected
  (`width: 4`, `corner_radius > w/2`, 257-point polygon, unknown `no` value, name `"a/b"`),
  `additionalProperties: false` at every level.
- [ ] **Step 2: Failing kicad tests** (`test_outline.py`, synth after sync):
  `test_rect_with_radius` (read back `corner_radius`, DRC task has no `invalid_outline`);
  `test_schematic_holes_moved_not_duplicated` (H1, H2 of `sp4_synth` moved to two corners,
  2 new tool holes placed, 4 holes total, H1/H2 keep their `symbol_path`);
  `test_rerun_replaces_tool_holes` (second call with `inset: 4` → still 4 holes, tool holes
  replaced, schematic holes moved again); `test_no_holes_arg_keeps_holes`;
  `test_keepout_rule_area` (`keepout_tracks/vias/copper` set, layers read back);
  `test_outline_changes_placement_hash`; `test_unknown_hole_footprint` (`not_found`,
  `closest` non-empty, board unchanged); compensation `test_fail_after_1`, `test_fail_after_2`
  (`fail_after` → both hashes equal pre-call); `test_undo_via_history` (restore → no Edge.Cuts
  rectangle, hashes equal pre-call).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): pcb_set_outline with holes and keepouts`.

### Task 3: `pcb_set_rules` (no stackup) + probes

**Interfaces:** B-C8 `FabPreset`, `PRESETS` from P1a (`cc/board/presets.py`, index call 8
values: 2-layer via diameter 0.66 per call 8, global `hole_to_hole` 0.45). Mapping to
`SetBoardDesignRules` constraints: `min_clearance`←min_space,
`min_track_width`←min_track, `min_via_size`←min_via_diameter, `min_through_drill`←min_drill,
`min_via_annular_width`←min_annular, `copper_edge_clearance`←copper_to_edge,
`hole_to_hole_min`←hole_to_hole, `min_silk_text_height`←silk height,
`min_silk_text_thickness`←silk line. Every stage below carries a P1a `BoardStage.undo`
(compensation runs them in reverse order after the KIID diff).

Tool `pcb_set_rules` (cost `Logical(2)`, kind `board_set_rules`): `layers?: 2|4|6`, `preset?`,
`overrides?: {<FabPreset min field>: number}` (0 < v ≤ 10; a value **below** the preset
minimum → `invalid_argument` "use custom_rules with a reason"), `net_classes?: [{name
(name regex of call 6), track_width, clearance, via_diameter, via_drill (≤ 10),
diff_pair_width?, diff_pair_gap?, patterns: [glob ≤ 256 chars] 1–50}] ≤ 16`, `custom_rules?:
[{name, condition (≤ 512), constraint ∈ {clearance, track_width, via_diameter, hole_size,
hole_to_hole, hole_clearance, edge_clearance, annular_width, courtyard_clearance,
silk_clearance, text_height, diff_pair_gap}, min?, max?, reason (8–300 chars)}] ≤ 32`,
`stackup?: string` → always `stackup_unavailable` in P2 ("needs the patched KiCad base image,
phase P2b"). At least one field required. Stages, each with `undo` restoring the pre-call
value: (1) layers via `set_enabled_layers(n, non_copper)` — **only when `layers` is given**:
`preset` alone never changes the layer count (a `jlcpcb-4l` preset on a 2-layer board sets
the 4-layer rule values and leaves 2 copper layers; the report notes the mismatch), so every
caller that wants 4 layers passes `layers: 4` explicitly; going down refuses with
`invalid_argument` listing up to 20 items (kind, KIID, layer) when any track, arc, zone or
graphic sits on a removed inner layer (plan call: refuse, never delete); (2) design
rules from preset + overrides; (3) net classes via `board_project(h).set_net_classes` +
pattern assignments (classes not named are kept; `Default` only changed when given);
(4) custom rules: `GetCustomDesignRules`, replace same-named rules, append new ones with
`comments = reason`, `SetCustomDesignRules`; status ≠ `CRS_VALID` → `invalid_argument` with
KiCad's error text; a `.kicad_dru` created by this call is removed by the stage's `undo`
(`remove_created`). Read-back verify: layer count, every set rule value (±0.0005 mm), class
names + widths, custom rule names. Report: rules table, classes, custom rules with reasons,
`board_hash` changed, `next: "pcb_analyze"`.

- [ ] **Step 1: Failing unit tests** (`test_setup_schemas.py`; the preset values are pinned
  by P1a's `test_presets.py`): `test_override_below_preset_refused`,
  `test_custom_rule_needs_reason`, bounds and `additionalProperties`.
- [ ] **Step 2: Failing kicad tests** (`test_rules.py`, synth after sync + outline):
  `test_preset_4l_roundtrip` (`{layers: 4, preset: "jlcpcb-4l"}` → layer count 4,
  `get_design_rules` values equal P1a's `PRESETS["jlcpcb-4l"]`);
  `test_preset_without_layers_keeps_layer_count` (fresh 2-layer board, `{preset:
  "jlcpcb-4l"}` only → still 2 copper layers, rule values set, report names the mismatch);
  `test_rules_persist_to_kicad_pro` (**probe, spec §6.2**): after the call and the adapter's
  `save("pcb")`, `.kicad_pro` JSON `board.design_settings.rules.min_clearance == 0.127` and the
  net class + pattern are in `net_settings` — spike Q2 observed this; if the assertion fails at
  the pin, set `saves_project=True` and implement the explicit project save in the engine,
  then record which in "As built"; `test_net_class_applies` (`get_netclass_for_nets` of `+3V3`
  shows the class); `test_custom_rule_with_reason` (`.kicad_dru` contains the rule and the
  reason as its comment); `test_invalid_condition` (`invalid_argument`, board_hash unchanged);
  `test_4_to_2_refused_with_inner_copper` (a track on In1.Cu → refused, item listed, layer count
  still 4); `test_4_to_2_empty_inner_ok`; `test_stackup_unavailable`;
  `test_fail_after_3_restores` (`fail_after=3` → rules, classes, layer count, `board_hash`
  equal pre-call); `test_undo_first_dru_via_history` (no `.kicad_dru` before; call with one
  custom rule; History restore → `.kicad_dru` deleted).
- [ ] **Step 3: Probe** `test_probe_layer_type_power` (spec §10.3): assert that the pinned
  proto offers no settable copper layer type (no field naming signal/power on
  `BoardStackupLayer`, no command in `board_commands_pb2` setting a layer type) and
  `record_property("layer_type_settable", False)`; a KiCad bump that adds one fails this test
  on purpose. Record in "As built (P2)": P6 must use the spec's fallbacks (Freerouting CLI
  options; DSN text only as owner-D4 last resort).
- [ ] **Step 4: Run** — FAIL. **Step 5: Implement.** **Step 6: Run** — PASS.
- [ ] **Step 7: Commit** `feat(board): pcb_set_rules with JLCPCB presets, net classes, custom rules`.

### Task 4: Docs + index as built, checks, PR

- [ ] Index "As built (P2)": `.kicad_pro` persistence result (and `saves_project`), layer-type
  probe result, sync runtimes on `sp4_synth`. CHANGELOG
  `[Unreleased]`: "Board sync, outline and rules tools — behind COPPERCHAT_BOARD_TOOLS."
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, `uv run pytest -m kicad tests/kicad/board` in the kicad-base image (with
  `timeout`); `test_every_board_write_has_a_kind` (P1c) now covers three real tools.
- [ ] PR `board phase P2: sync, outline, rules`; review-loop per the index's Phase Conventions.

## Acceptance (P2)

- [ ] CI `backend` and all 8 `kicad (…, i/8)` shards green.
- [ ] `test_sync_create_then_undo`, `test_resync_keeps_placement`, `test_delete_extra`,
  `test_schematic_holes_moved_not_duplicated`, `test_rules_persist_to_kicad_pro`,
  `test_undo_first_dru_via_history` pass.
- [ ] Every multi-stage tool has a `fail_after` test with hashes equal to the pre-call state
  (sync: documented History exception tested instead).
- [ ] Probe results (rules persistence, layer type) recorded in the index.
