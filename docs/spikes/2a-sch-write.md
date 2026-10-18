# Spike 2A — headless schematic write round-trip

## Question(s)

From the editing spec (`docs/superpowers/specs/2026-10-17-editing-design.md`, §13,
row 2A), answered against the headless `kicad-cli api-server`:

1. Place from the stock and a project library (`load_all_libraries` first):
   references, units, rotation, mirror; pin connection points from API data.
2. Field updates (value, footprint, datasheet, custom fields, DNP / exclude flags)
   incl. field positions; multi-unit parts.
3. Wires, labels, junctions, no-connects: do they connect (pin ends, T-joints with
   and without junction, wire splitting, a wire over a foreign pin end)? Does the
   netlist (`GetSchematicNetlist`, the netlist export job) reflect new connectivity
   without a frame, or is save/reload needed?
4. Move / rotate / mirror with attached wires.
5. Delete; what the push does to neighbouring items.
6. Commit semantics: staged items before push, `drop_commit`, cancellation between
   begin and push, compensating commit that re-creates deleted items **with their
   original UUIDs** (spec §5.1 step 8, §5.3).
7. New sheet → file on save, sheet pins ↔ hierarchical labels, page numbers (§5.5).
8. Mid-turn `save()` + `save_copy` + ERC latency on a ~100-symbol hierarchical
   schematic; does the copy hold just-edited sub-sheets and new sheets?
9. Symbol bounding boxes from API data vs. KiCad's own plot.
10. Duplication via `GetItems` → new UUIDs → `create_items`, and via
    `place_symbol_from_library` + field copy (fields, unit, mirror, body style?).
11. Saved files load in `kicad-cli`; 12. … and in KiCanvas.

The coordinator added three observations from spike 2C (PR #23): sub-sheet
updates through the unbound `Schematic` are lost, push-time side effects
(dangling junction dropped, segments merged), symbol moves not moving fields.
They are covered in Q4/Q5.

## Setup

- Runtime: local image
  `ghcr.io/peterus/copperchat-kicad-base:0616634034fa1a8c92c141c293342cbef2f68dff-r41da5e29cdd40d32`
  — KiCad `0616634034fa1a8c92c141c293342cbef2f68dff` (`kicad-cli version` 10.99.0),
  kipy `87b945a0` in `/opt/venv`, stock libraries (224 symbol libraries), demos;
  runs as uid 1000. Every command through `spikes/2a-sch-write/run.sh` (mounts the
  spike at `/spike`). `LANG=C`, `LC_ALL=C`, `KICAD_CONFIG_HOME=spikes/2a-sch-write/.kicad/config`
  seeded with the stock `sym-lib-table`/`fp-lib-table` (byte copies of KiCad's
  templates, as `launcher.seed_library_tables` does).
- Server: `kicad-cli api-server --socket S PROJECT.kicad_pro`, then
  `open_document(root.kicad_sch)` (spike 1C), then `load_all_libraries()` +
  `wait_for_libraries()` before any placement: **8.2–9.2 s** with all stock
  libraries, the process then holds **~5 GB RSS** (input for spike 2B).
- Fixtures (`make_fixtures.sh`, never hand-edited): `ecc83-pp` and
  `complex_hierarchy` copied from the image's demos and upgraded with
  `kicad-cli sch upgrade --force`; `blank` = an empty project + schematic created
  by KiCad itself through the API (`create_document` + `save`), plus a byte copy of
  the ecc83 project symbol library and its `${KIPRJMOD}` `sym-lib-table` as the
  project library. Everything else is created by the scripts through the API.
- Host check for KiCanvas: Playwright 1.64.1 / Chromium on the vendored
  `frontend/public/vendor/kicanvas/kicanvas.js` (peterus/kicanvas fork, as shipped).
- Oracle: every "is it connected" question is answered by KiCad, never by the
  spike's geometry: `GetSchematicNetlist` (live), the netlist export job, and
  `kicad-cli sch export netlist` / `sch erc` on the saved files.

## Results

`./run.sh python run_all.py` + `npx playwright test` on 2026-10-18
(`spikes/2a-sch-write/results/summary.md`):

| Question | Result | Failed checks (findings, see below) |
|---|---|---|
| q01_place | 14 pass / 1 fail | rotate_then_mirror_matrix_as_requested |
| q02_fields | 14 pass / 2 fail | multi_unit_full_definition_update_responds, multi_unit_fields_synced_by_kicad |
| q03_connect | 36 pass / 4 fail | netlist_job_reflects_unsaved_edits, netlist_job_reflects_edits_after_save, png_job_reflects_edits_after_save, revert_refreshes_export_jobs |
| q04_transform | 10 pass / 3 fail | move_symbol_moves_fields, move_symbol_drags_attached_wires, subsheet_update_via_unbound_schematic_applies |
| q05_delete | 12 pass / 4 fail | dangling_junction_kept_after_push, delete_wire_keeps_junction, delete_wire_keeps_neighbour_segments, collinear_pair_kept_separate_on_create |
| q06_commit | 24 pass / 3 fail | update_of_missing_item_raises, create_with_existing_uuid_raises, recreate_with_sibling_keeps_library_link |
| q07_sheets | 20 pass / 2 fail | relative_sheetfile_new_sheet_save, workaround_field_fix_inside_create_commit |
| q08_save_latency | 10 pass / 4 fail | new_symbol_on_shared_sheet_visible_in_other_instance, copy_without_save_finds_all_hierarchy_files, copy_without_save_has_subsheet_edits, shared_sheet_second_instance_unique_ref_after_save |
| q09_bbox | 4 pass / 1 fail | api_bbox_matches_plot_within_0_6mm |
| q10_duplicate | 9 pass / 2 fail | definition_keeps_all_body_style_pins, dup_symbols_create_items_keep_library_link |
| q11_files_load | 9 pass / 0 fail | |
| q12_kicanvas | 7 pass / 1 fail | kicanvas_loads_q10_duplicate_B |

Each FAIL is a documented KiCad behaviour; where it matters, a sibling check
verifies the API-only workaround (PASS). None of them needs a file edit.

### Place (Q1)

- `place_symbol_from_library` before `load_all_libraries` fails with
  `symbol 'Device:R' not found`; after it, stock (`Device:R`, `power:GND`,
  `Amplifier_Operational:LM358`) and project (`ecc83-pp:ECC83`) symbols place.
  Unknown symbol / out-of-range unit are clean `ApiError`s.
- References: with `reference` the value is applied as given (`R101`; multi-unit
  units read back as `U1` with `unit` 1/2/3 and are **one** component in the
  netlist). Without it, normal symbols stay **unannotated** (`R?`) — the headless
  server has no auto-annotation preference; power symbols are "always annotate"
  and get `#PWR01`. CopperChat's own annotator (§5.5) is therefore required, as
  the spec assumed.
- Rotation via the `orientation` argument; mirror only through `update_items`
  (`transform.mirror_x/mirror_y`). **KiCad bug**: rotation 180° + `mirror_x`
  collapses to the identity (`SCH_SYMBOL::SetMirrorX` ORs a mirror bit into a
  non-bit-flag orientation enum); 90/270 + `mirror_y` are stored as the
  equivalent 270/90 + `mirror_x`. Workaround verified
  (`canonical_8_transforms_round_trip`): send every one of the 8 distinct
  transforms as orientation 0/90/180/270, 0+mx, 90+mx, 270+mx, 0+my (180+mx is
  sent as 0+my).
- **Pin positions**: the placed symbol's `definition.pins` are library-local
  (y-down), untransformed, and include the pins of **all** units. Absolute
  connection point = `position + T·pin.position` with KiCad's incremental
  `SetOrientation` composition (each step applied to the already transformed
  coordinates; `geom.py`). Verified for all 12 orientation/mirror requests and the
  8 canonical ones by putting a label exactly on each computed end: every pin is on
  its label's net in the live netlist and in `kicad-cli` after save. A label
  anchored exactly on a pin end connects without a wire.

### Fields (Q2)

- Value, footprint, datasheet, description, custom fields (`user_fields`: add,
  position, visibility, remove), DNP and exclude-from-BOM/board all round-trip
  through `update_items` (~13 ms), persist after save + reopen and appear in the
  `kicad-cli` netlist (`dnp`, `exclude_from_bom` properties).
- Field positions are absolute and kept as sent. Headless placement does **no
  field auto-placement** (`fields_autoplaced=false`, library default offsets:
  value text on the body centre) — CopperChat places fields itself.
- **Multi-unit fields are per unit**: setting value/footprint on unit A leaves
  unit B unchanged (KiCad's GUI syncs them, the API does not). CopperChat must
  update all units of a reference (`multi_unit_fields_synced_by_client` PASS).
- **KiCad bug (server hang)**: `update_items` of a multi-unit symbol **with its
  `definition`** (as `GetItems` returns it) hangs the server once a second
  instance of the same multi-unit library symbol is updated (also two in one
  call; LM358, ECC83; not for single-unit parts). The server stops answering
  everything, even `ping`; main thread idles in `wxMicroSleep`, no CPU, no log.
  Workaround verified: **clear `definition` before every symbol update**
  (KiCad's "definition-less update" keeps library link and pins;
  `harness.update_symbols`). Every symbol update in this spike uses it.

### Connect (Q3)

- Wires, local/global labels, junctions, no-connects and power symbols created
  via `create_items` / `place_symbol_from_library` connect as KiCad does in the
  GUI: wire end on pin end; **T-joint connects with and without a junction**
  (KiCad does not split the host wire; `wires_not_split_or_merged_on_push`); a
  label on a wire interior names the net; crossing wires without a junction stay
  separate; a 0.05 mm near miss does not connect (ERC flags it).
- **A wire passing over a foreign pin end connects it** (`OVER` net got R5.2):
  the "silent short" of the spec's anti-goals is real — the router must keep
  foreign pin ends off wire interiors (§5.2 already says so).
- The live **`GetSchematicNetlist` reflects every edit immediately** (no save).
  Its members are bare KIIDs, but they resolve: pin KIIDs = the placed symbols'
  `definition.pins[].id`, scoped by the netlist's per-sheet `path`
  (`harness.nets_by_pin`). Verified equal to `kicad-cli`'s netlist on the edited
  blank project and on the unedited hierarchical demo (164 pins, 0 differences;
  `live_netlist_matches_cli_on_hierarchical_demo`). This contradicts the comment
  in `kicad_tools/access.py` (`netlist_nodes`) that the KIIDs cannot be resolved.
- **All API export jobs are stale after edits** (netlist, PNG; same code path as
  SVG/PDF/BOM): the headless job handler loads its own copy of the schematic
  **from disk at the first job** and caches it (`EESCHEMA_JOBS_HANDLER::getSchematic`,
  `m_cliSchematic`). Later jobs ignore unsaved edits, **ignore `save()`** and
  **ignore `revert()`**. Only `close_document` + `open_document` clears the cache
  (`IFACE::closeSchematic`, ~50 ms; libraries stay loaded) — verified
  (`workaround_save_close_open_refreshes_jobs`). The foundation is not affected:
  it never edits and restarts the server on external changes (`reopen`).

### Move / rotate / mirror (Q4)

- `update_items` with a new `position` moves only the symbol: **fields stay at
  their absolute positions** and **attached wires are not dragged** (connection
  silently broken). Same for rotate/mirror (fields are not rotated either).
- Workaround verified: in **one commit** update the symbol (position/transform),
  shift its fields by the same delta, and move the attached wire ends / labels
  to the new pin ends (computed from Q1's transform) — connectivity intact for
  move, rotate and mirror.
- **Sub-sheet updates must go through `sch.for_sheet(instance)`**: an update via the
  unbound `kicad.get_schematic()` returns without error and changes nothing
  (spike 2C's finding confirmed). An edit on a shared sheet file
  (`ampli_ht.kicad_sch`, two instances) is visible in both instances.

### Delete (Q5)

- `remove_items` / `remove_items_by_id` delete symbols, wires, labels, junctions,
  no-connects; the netlist updates live. Deleting a symbol leaves its wires and
  no-connects behind (dangling). Deleting an unknown id raises nothing.
- **The push runs KiCad's local connectivity cleanup** (`SCH_COMMIT::pushSchEdit`
  → `RecalculateConnections(LOCAL_CLEANUP)`), with side effects on items the
  tool did not touch: a junction not on a T (dangling or on a corner) is dropped;
  after deleting a T branch the junction is removed and the two collinear
  segments are **merged into one** (one UUID survives, its end moves); two
  collinear end-to-end segments created together are merged at push (a returned
  UUID no longer exists). Tools must read back the neighbourhood after the push,
  record these as changes, and include neighbours in the pre-images.

### Commit semantics (Q6)

- Inside `begin_commit` … `push_commit`: staged **creates are invisible**
  (`GetItemsById`, `GetItems`, live netlist) and **staged deletes still
  visible** until push — as the spec states. **Staged updates are visible
  immediately** (KiCad swaps the item data in place). `drop_commit` restores the
  document exactly (creates, update and delete in one commit; full item dump
  equal).
- Cancellation: a client that disappears between begin and push leaves its
  commit open on the server; another client name keeps working and never sees
  the staged items. **The same client name** (CopperChat uses a fixed
  `"copperchat"`, `session.py`) after reconnecting: `begin_commit` fails
  (`already has a commit in progress`) and plain `create_items` are **staged
  silently into the stale commit**; `drop_commit` with the old commit id cleans
  up and later writes apply. So: keep the `Commit` id and always drop it on
  cancellation (shielded), or restart the session (§5.3 is right).
- **kipy ignores per-item statuses**: `update_items` of a missing item returns an
  item and raises nothing; `create_items` with an existing UUID likewise returns an
  item and creates nothing (`ISC_EXISTING` dropped). Tools must
  verify by read-back (or send the raw command and check `ItemStatus`).
- **Compensating commit with original UUIDs works**: deleting a mirrored
  multi-unit symbol unit (custom field, DNP), a plain symbol, wires, a local and a
  global label and a no-connect, then `create_items` with the pre-image objects
  (incl. the symbol `definition`) restores the same UUIDs, identical content
  (except the symbols' pin UUIDs, which KiCad renews on every create),
  identical netlist, persisted after save + reopen (35 ms). Restoring an update
  = `update_items` with the pre-image.
- **Limit for symbols**: the definition that the API returns and accepts is not
  identical to the library's `LIB_SYMBOL`. When another instance of the same
  library symbol exists, KiCad keeps a second cached copy and one of the two
  instances gets a private `lib_name` (`Device:R` → `:R_1`; netlist libsource
  `:R_1`). For a De Morgan symbol with body style 2, the returned definition lacks
  the other body style's pins (14 of 26), and a symbol created from it produces a
  cache entry KiCanvas cannot render (Q10/Q12). Consequence: re-create **non-symbol
  items** with original UUIDs; for a **deleted symbol** use the spec's fallback
  (restore the step checkpoint / turn snapshot) or re-place it from the library
  (new UUID) — decision for the plan.

### Sheets and page numbers (Q7)

- A `SheetSymbol` created through the API appears in the hierarchy with the next
  free page number (`"2"` in blank, `"4"` in complex_hierarchy); symbols,
  hierarchical labels are created on it via `for_sheet`; sheet pins are part of
  the `SheetSymbol` (`pins`, via `update_items`). Sheet pin ↔ hierarchical label
  connect (live netlist, `kicad-cli` netlist, ERC clean for the pin); the file is
  written by `save()`; reopen, a relocated copy and KiCanvas load it.
- **KiCad bug**: with the usual relative `Sheetfile` (`power.kicad_sch`) the new
  screen's path is resolved against the parent *file* name instead of its
  directory, and `save()` fails (`failed to save schematic`, root written, sub
  file not). Workaround verified: create with the **absolute** path, push, then
  set the `Sheetfile` field back to the relative name in a second commit (the
  field fix inside the create commit is silently ignored: the staged sheet is not
  resolvable yet).
- Page numbers for per-sheet annotation: `get_hierarchy()` gives
  `page_number` per sheet instance and `get_sheet_symbols()` per sheet symbol;
  every symbol instance's `path` maps to one (complex_hierarchy: refs 1xx on page
  1, 2xx on page 2, 3xx on page 3 — the demo already uses the §5.5 scheme).
- **Shared sub-sheets**: a symbol placed on one instance of a sheet used twice is
  invisible in the other instance until reload, and on save KiCad gives the second
  instance the **same reference** (duplicate `R299`). Workaround verified:
  `save()` + `revert()` (reload), then set the other instance's reference via
  `for_sheet(other)` → `R299` / `R399`.

### Save, copy, ERC latency (Q8)

- complex_hierarchy: 119 symbol unit instances, 3 sheet instances; edits on the
  shared sub-sheet + a new sheet with a part. Medians of 3 rounds: `save()` 53 ms,
  `save_copy` (root `save_as` + byte copy of the hierarchy files) 37 ms,
  `kicad-cli sch erc` **2.8 s** (process start + library load), total **2.9 s**.
- After `save()` the copy holds the edited sub-sheet value, the new sub-sheet
  symbol with its label net, and the new sheet file + its part. Without `save()`
  the foundation procedure copies stale sub-sheets and **crashes on a new sheet**
  (its file does not exist yet): the spec's "save before every ERC/diff/step
  checkpoint" is required, not optional. `save()` rewrites all sheet files.

### Bounding boxes (Q9)

- Box from API data (library graphics `bounding_box()` + pin end/length +
  transform, `geom.symbol_bbox`) vs. `kicad-cli sch export png` (600 dpi, fields
  hidden, no drawing sheet) for 13 placements (R, C_Polarized 90°, D mirrored,
  BC547 mirrored, LM358 units 1/2/3, AMS1117, Conn_01x04 180°, ATmega328-P,
  74LS00 body style 2, GND, ECC83 90°): **≤ 0.34 mm** for body + pins (the
  plotted stroke), up to **1.9 mm** where pin name/number text sticks out
  (LM358 power unit: only two pins with names; connector; De Morgan). Use the API
  box + **2 mm margin** (verified to contain every plot), or add text extents
  (`get_text_extents`) later. No `GetBoundingBox` needed.
- Page size can be set through `set_page_settings` (A3 here); body style is set
  via `update_items` (`body_style = 2`).

### Duplicate (Q10)

- Non-symbol items: `GetItemsById` → copy proto → new UUID → offset → `create_items`
  works for wires, junction (on a T, so it survives cleanup), local labels (with
  `_CH2` suffix), global label, no-connect; all present after push.
- Symbols, method A (`place_symbol_from_library(lib_id, pos, orientation, unit,
  reference)` + copy of value/footprint/datasheet/description/custom fields with
  offset positions, attributes, transform incl. mirror, body style, pin
  name/number flags; definition-less update): **identical** to the source for R,
  C (90° + mirror), LM358 unit 2 (mirrored), 74LS00 unit 3 body style 2; library
  link kept; connectivity of the copy mirrors the source, local nets suffixed,
  global net shared. **Use method A.**
- Method B (`create_items` of the read symbol with a new UUID + reference): fields,
  unit, mirror and body style are kept, but every copy gets a private library
  cache entry (`:R_1`, `:LM358_1`, …) and the 74LS00 copy breaks KiCanvas
  (`No pin numbered 1 on library symbol 74LS00_1`, blank canvas).

### Files load (Q11, Q12)

- All 9 projects saved by the spike (incl. the edited hierarchical demo and both
  new-sheet projects) load in `kicad-cli`: netlist, SVG of every sheet, ERC.
- KiCanvas (vendored bundle) renders 7 of 8 without page/console errors; the one
  failure is the method-B duplicate above.

## Decision

The headless IPC API can do every core editing operation — place, field edits,
move/rotate/mirror, delete, wires/labels/junctions/no-connects, new sheets with
pins, duplication, commit/drop, compensation — **without any file edit or
S-expression generation**. Several operations need API-level workarounds, all
verified in the harness:

2A partial: definition-less symbol updates (multi-unit update hang); canonical orientation/mirror forms (180°+mirror_x bug); client-side drag of fields + wire ends in the same commit on move/rotate/mirror; `for_sheet` for every sub-sheet write; read-back of the neighbourhood after each push (cleanup merges segments, drops junctions); live `GetSchematicNetlist` (path-aware pin KIIDs) for verification, export jobs only after save + close_document/open_document; new sheets created with an absolute file path and the `Sheetfile` field reset to relative in a second commit; save + revert before setting per-instance references on shared sheets; symbols duplicated via place_symbol_from_library + field copy; deleted symbols compensated by checkpoint/turn-snapshot restore (or re-place with a new UUID) — non-symbol items keep their original UUIDs.

No core operation needs a text edit → **not a no-go**; no owner escalation
needed. Pin stays at KiCad `0616634034` / kipy `87b945a0`.

## Follow-ups

- **Upstream candidates (owner OK required before any upstream report/PR):**
  multi-unit full-definition update hang; `SetMirrorX` on 180°; relative
  `Sheetfile` resolved against the parent file; job-handler schematic cache not
  cleared by save/revert; lossy definition round-trip (private `X_1` cache
  entries, body-style pins); kipy ignoring per-item status.
- **Plan / phase `kicad_session`:**
  - `commit(label)`: keep the `Commit` id; shielded `drop_commit` on any
    exception/cancellation; if that fails, restart — a stale commit captures
    every later write of the fixed client name silently.
  - `save()` → record hashes → **`close_document` + `open_document`** whenever
    export jobs (agent view PNG, `kicad-cli`-free netlist/BOM) must see the edits;
    or render through `kicad-cli` on the `save_copy`. `publish_view` after an
    editing turn must do this, else the agent view shows the pre-edit state.
  - `save_copy`: `save()` first (always, once modified); new sheet files only
    exist after it.
  - `load_all_libraries` + `wait_for_libraries` after open (~8–9 s, ~5 GB RSS
    with all stock libraries; spike 2B decides how to bound that).
- **`kicad_tools/edit` (geometry.py):** pin ends from `definition.pins` +
  `geom.transform` (filter the instance's unit / body style); bbox from library
  graphics + pins + 2 mm margin; canonical orientation/mirror encoding; the
  router must never let a wire interior cross a foreign pin end (connects).
- **place.py / fields.py / transform.py:** always definition-less symbol
  updates; place fields explicitly (no auto-place headless); update all units of
  a multi-unit reference; move/rotate/mirror = symbol + fields + attached wire
  ends + labels in one commit; all sub-sheet writes via `for_sheet`.
- **verify.py:** read-back by id after push (created UUIDs may vanish by
  merge; check every per-item result); connectivity from the live
  `GetSchematicNetlist` mapped through (sheet path, pin KIID) — no save needed;
  include KiCad's cleanup changes (merged segments, removed junctions) in the
  change log (§8.1) and in the pre-images used for compensation.
- **Compensation (§5.1 step 8):** non-symbol items: re-create from pre-images
  (original UUIDs verified); updates: `update_items` with the pre-image;
  deleted symbols: checkpoint/turn-snapshot restore (spec fallback) or re-place
  from the library — the plan must pick one and state the UUID consequence.
- **annotate.py (§5.5):** page numbers from `get_hierarchy()`; place with an
  explicit reference (headless never annotates normal symbols); for a sheet
  file with several instances, save + revert, then set each instance's reference
  through `for_sheet(instance)`.
- **sheets.py:** create with absolute `Sheetfile`, push, reset to relative; sheet
  pins via `SheetSymbol.pins` update; hierarchical label via `for_sheet(new)`.
- **duplicate.py:** non-symbol items as API objects with new UUIDs; symbols via
  `place_symbol_from_library` + field/attribute/transform/body-style copy
  (never `create_items` of a read symbol).
- **ERC delta (§8.3):** ~2.9 s per save + copy + ERC on a 100-symbol hierarchy;
  fine at step/turn end, too slow per call (spec already excludes per-call ERC).
- **Foundation code:** `kicad_tools/access.py` `netlist_nodes` uses the export
  job — correct for the read-only foundation, but stale as soon as SP2 edits;
  switch read-back to the live netlist (its docstring's "cannot be resolved"
  claim is outdated, see Q3).
