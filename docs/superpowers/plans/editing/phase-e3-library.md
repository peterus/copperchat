# Phase E3 — Library index, `lib_*` tools, part preferences, part notes

> Index contracts: E-C9 (and E-C4 for the error codes). Spike: 2B (all), 2D
> (`pin_directions_by_rotation`, body size). Owner decision 1 (symbols eager,
> footprints lazy per library). Reference code: `spikes/2b-libraries/libdata.py`
> (`symbol_from_proto`, `fetch_symbols` 500-id batches, `footprint_from_proto`,
> `package_of`), `index.py` (inverted index with prefix matching),
> `spikes/2d-agent-eval/tools_v2.py` (`pin_directions`), `tools.py::_probe`.

**Goal:** fast, API-only library search and inspection; part preferences the tools
can read; notes of researched parts for the main agent.

**Files:**
- Create: `backend/src/copperchat/library/{__init__,index,service,packages,prefs,specs}.py`
- Create: `backend/src/copperchat/skills/builtin/part-preferences/SKILL.md`
- Modify: `tools/registry.py` (provider `copperchat.library.specs`), `web/routes_projects.py`
  (start warm-up after open), `skills/store.py` (seed of the user layer, Task 4)
- Tests: `tests/unit/library/test_index.py`, `test_packages.py`, `test_prefs.py`,
  `tests/kicad/edit/test_library_real.py`, `tests/unit/library/test_parts_notes_tool.py`

---

### Task 1: Index and warm-up

**Interfaces:** Consumes `KicadSession.ensure_symbol_libraries` (E1). Produces
`library_for(session)`, `LibraryService.start/state/search_symbols`.

- [ ] **Step 1: Failing tests.** Unit (`test_index.py`, records built in code):
  `test_prefix_and_keyword_match` ("resist" → `Device:R` first when its keywords contain
  "resistor"); `test_project_libraries_rank_first` (same score → `scope=project` first);
  `test_name_fallback_when_description_empty`; `test_limit_and_library_filter`.
  Kicad (`blank_session`): `test_warm_up_and_search` — `start()`, wait for `ready` (≤ 120 s),
  `search_symbols("resistor")` top-5 contains `Device:R`, `"LM358"` finds
  `Amplifier_Operational:LM358`, `"ECC83"` finds the project library `ecc83-pp:ECC83`, each
  query < 300 ms; `test_search_while_loading_errors` → `StructuredToolError(code="libraries_loading")`.
- [ ] **Step 2:** FAIL. **Step 3: Implement**: warm-up task = `ensure_symbol_libraries` →
  `get_library_symbols()` names → batched `GetItemsFromLibrary` (500 ids/request) →
  `Index`. Rows: `lib_id, description, keywords, unit_count, pin_count (None for derived
  symbols until inspected), footprint, footprint_filters, scope`. Footprints are **not**
  indexed eagerly. Started from the open route (background, errors logged → `failed`) and
  lazily by any `lib_*` call; a restarted session gets a new service.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: symbol library index`.

### Task 2: Symbol detail incl. derived-symbol pins

**Interfaces:** Produces `get_symbol(lib_id) -> SymbolDetail` (E-C9).

- [ ] **Step 1: Failing kicad tests**: `test_derived_symbol_pins_via_probe`
  (`MCU_ST_STM32F1:STM32F103C8Tx` → 48 pins; `pin_count` in later search rows filled);
  `test_probe_leaves_document_unchanged` (UUID set equal, `is_document_modified()` False);
  `test_pin_directions_by_rotation_for_small_parts` (`Device:LED` → 4 rotations, at 0° pin 1
  (K) `left`, pin 2 (A) `right`; `None` for a 14-pin part); `test_multi_unit_and_body_styles`
  (`74xx:74LS00` → `unit_count 5`, `body_styles 2`, pins deduplicated per unit/number);
  `test_unknown_symbol_not_found` (closest matches in `suggestion`).
- [ ] **Step 2:** FAIL. **Step 3: Implement**: detail from the cached proto; if it has no
  pins (derived), probe with `session.commit("probe", dry_run=True)` +
  `place_symbol_from_library` at (0, 0) and read the flattened `definition` (2B finding 3);
  cache per `lib_id`. `body_size_mm` from the definition's graphics bounding boxes plus pin ends, untransformed
  (computed here — E3 does not depend on E2's geometry).
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: lib symbol detail with derived pins`.

### Task 3: Footprints lazily per library

**Interfaces:** Produces `search_footprints`, `footprint(lib_id)`, `footprints_available`,
`package_of`.

- [ ] **Step 1: Failing tests.** Unit (`test_packages.py`): `package_of("R_0805_2012Metric") ==
  "0805"`, `package_of("C_01005_0402Metric") == "01005"`, inconsistent pairs → `None`,
  `D_2114_3652Metric` → `None`. Kicad (`ecc83_session`, has a board):
  `test_footprint_lazy_load` (`footprint("Resistor_SMD:R_0805_2012Metric")` →
  `pad_count 2, smd_pads 2, package "0805"`; only that library is loaded — check
  `get_library_statuses` of one other footprint library is not loaded);
  `test_search_footprints_in_library` (`library="Resistor_SMD", query="0805"` → hit);
  `test_search_without_library_suggests_libraries` (query `"0805 resistor"` → results from
  libraries whose nickname matches tokens, e.g. `Resistor_SMD`, plus `libraries` hint).
  Kicad (`blank` copy **without** its `.kicad_pcb`): `test_no_board_footprints_unavailable`
  → `code="footprints_unavailable"`.
- [ ] **Step 2:** FAIL. **Step 3: Implement**: nicknames from `get_library_statuses(FOOTPRINT)`
  (no load); a request for one library calls `GetLibraryItems(nickname)` /
  `GetItemsFromLibrary` for that library only (2B Q4), caches names + details; search without
  `library` ranks nicknames by token match and searches the top 3 libraries (loading them)
  plus every already-loaded library. Never `load_all_libraries` for footprints.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: lazy footprint libraries`.

### Task 4: Part preferences and `lib_*` / `parts_get_notes` tools

**Interfaces:** Produces `PartPreferences`, `effective_preferences`, `preference_notes`,
`active_part_note` (E-C9); tools `lib_search_symbols(query, library?, limit?)`,
`lib_get_symbol(lib_id)`, `lib_search_footprints(query, library?, limit?)`,
`parts_get_notes(mpn)`.

- [ ] **Step 1: Skill document.** `skills/builtin/part-preferences/SKILL.md`: front matter
  `name: part-preferences`, description "The user's part preferences (packages, libraries,
  sourcing). Follow them when choosing parts; justify any deviation."; body = short
  explanation + a fenced block ```` ```preferences ```` with keys `passives_package:`,
  `preferred_libraries:`, `preferred_manufacturers:`, `assembly:` (empty values).
- [ ] **Step 2: Failing tests** (`test_prefs.py`): `test_header_parsed` (`passives_package: 0805`,
  comma lists); `test_most_specific_layer_wins_per_key` (project overrides only
  `passives_package`, user `preferred_libraries` stays); `test_user_layer_seeded_with_0805_once`
  (first start writes the user layer with `passives_package: 0805` via the skills store; a later
  user edit is never overwritten); `test_deviation_note_for_passive` (`Device:R` +
  `R_0603_1608Metric` → one `preference_deviation` note naming 0805);
  `test_no_note_for_unknown_package_or_non_passive`; `test_active_part_note` (`U3` without MPN →
  note; with MPN and `docs/parts/<MPN>/notes.md` → none). `test_parts_notes_tool.py`:
  `parts_get_notes("TMP102AIDRLR")` returns the text; unknown → `{"researched": false}`;
  invalid MPN (`../x`) → `invalid_argument` (Phase 8 MPN rule, `parts.mpn_dirname`).
  Kicad: `test_lib_tools_shapes` (each tool's JSON keys as in spec §6.1 + 2D table).
- [ ] **Step 3:** FAIL. **Step 4: Implement** `prefs.py` (reads the effective
  `part-preferences` skill per layer via `skills.store`, parses the block), the seed at app
  startup (`seed_user_part_preferences(settings)`), and `library/specs.py`
  (`requires_kicad=False` only for `parts_get_notes`).
- [ ] **Step 5:** PASS. **Step 6:** commit `feat: lib tools, part preferences, parts_get_notes`.

---

**Done when:** one PR, CI green, review-loop done, E-C9 as built.
