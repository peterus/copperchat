# Spike 3A — footprint library via the KiCad IPC API

## Question(s)
The footprint feature (`fp_create` / `fp_get` / `fp_check` / `fp_render` / `fp_delete` / `fp_list_local`)
must create, read and assign footprints **only through the KiCad API**: no hand-written `.kicad_mod`,
no hand-written `fp-lib-table`; `kicad-cli` only to validate and render. Library location (owner
decision "B"): one library at the repo root, shared by several projects in sub-folders, referenced
from each project's `fp-lib-table` through a `${KIPRJMOD}`-relative URI.

1. Create a NEW footprint purely via the API; is the file a valid current-format `.kicad_mod`?
2. Create a new, empty `.pretty` library via the API; what non-API step remains?
3. Library location B: does a `${KIPRJMOD}/../..` row resolve, load and save for two projects?
4. Footprint document next to the open schematic (and board) in one api-server: conflicts?
5. Read any footprint (stock + local) with full pad geometry, courtyard, fab, 3D model without a `.kicad_pcb`; cost?
6. Which kipy wrappers exist, which calls need raw protobuf?
7. After save: does the schematic side (Footprint field, ERC `footprint_link_issues`) and a board see the new footprint?
8. Is `kicad-cli fp export svg` usable for a dimension check?

Acceptance case: L201 = Bourns SRN3012BTA-2R2M, footprint `L_Bourns_SRN3012BTA`: two SMD rect pads
1.3 × 3.5 mm at x = ±1.1 mm (pad 1 left), F.CrtYd 4.0 × 4.0 mm, F.Fab 3.0 × 3.0 mm, silk marker
outside the pads, Reference/Value, description, a "Footprint source" property; geometry verified by
reading it back.

## Setup
- KiCad master `52b43fb8fc8e11fdab5d683eaefc91a49f9cbcd7` (image
  `ghcr.io/peterus/copperchat-kicad-base:52b43fb8…-rf96c7c8335ad9c6f`, built locally; it still ships
  upstream kipy `76b47d25`, so `run.sh` overlays a copy with the fork's 3-line #147 fix = kipy
  `ab07e98`; with the published image the overlay is skipped). Headless `kicad-cli api-server`, one
  per project, `LANG=C`, spike-local `KICAD_CONFIG_HOME` seeded with KiCad's template tables
  (156 stock footprint libraries).
- Synthetic repo per probe (`spikes/3a-footprints/repo.py`): `repo/hardware/main/main.kicad_{pro,sch,pcb}`
  and `repo/hardware/io/io.kicad_{pro,sch}`, all created by KiCad (`CreateDocument` + save), library at
  `repo/footprints/copperchat.pretty`, row URI `${KIPRJMOD}/../../footprints/copperchat.pretty`.
  **The `fp-lib-table` rows are a spike-only stand-in** (`standin_fp_lib_table`) because the API has
  no handler to write them (Q2); it is the only file a probe writes itself.
- Source read at the pin: `kicad/cli/command_api_server.cpp` (document bookkeeping),
  `pcbnew/pcbnew.cpp` (`handleOpenFootprint`), `pcbnew/api/api_handler_footprint.cpp`,
  `headless_footprint_context.cpp`, `api_handler_board.cpp` (`handleCreateUpdateItemsInternal`),
  `common/api/api_handler_libraries.cpp`, `api/proto/common/commands/library_commands.proto`.
- Harness: `spikes/3a-footprints/` (`./run_all.sh`, docker only). All 8 probes, **82 checks pass**
  (checks that document a refusal pass when KiCad refuses); a full run takes ~2 min.

## Results

| # | Question | Gate | Result |
|---|---|---|---|
| 1 | New footprint via API | **partial** | Path: open a stock seed (`OpenDocument(DOCTYPE_FOOTPRINT, "Inductor_SMD:L_Bourns-SRN4018")`) → `SaveCopyOfDocument(path="copperchat:L_Bourns_SRN3012BTA")` → close seed → open the copy → one commit: `DeleteItems` (19 seed items; mandatory fields refuse with `IDS_IMMUTABLE`), `CreateItems` (2 pads, 2 rectangles, 3 silk segments, 1 fab text, 1 custom field), `UpdateItems` (Reference/Value/Description fields) → `SaveDocument`. Read-back after close/reopen is exact (pads, layers F.Cu/F.Mask/F.Paste, CrtYd 4.0 × 4.0, Fab 3.0 × 3.0, silk clear of copper, fields incl. "Footprint source"). File: `(kicad_footprint_lib (version 20261031) (generator "kicad_footprint_editor") …`; `kicad-cli fp upgrade --force` output is **byte-identical**. Wall time 3.1 s, of which 2.9 s is the first footprint open (see Q5); every other call ~10 ms. **Gap:** footprint-level data is not writable in the footprint document: `UpdateItems(FootprintInstance)` → *"Tried to create a footprint in Footprint, which is not a board"*; `CreateItems(Footprint3DModel)` → *"Could not decode a valid type"*. So `descr`, `tags`, `attr` (mounting type, BOM/pos exclusions), 3D models, clearance overrides and net ties **stay as in the seed** (measured: the L201 file carries SRN4018's `descr`, `tags` and `model`). No stock seed without `descr` and `model` exists (only `Symbol_ESD-Logo-Text_CopperTop`). |
| 2 | New empty library | **no-go via API** (table) / partial (dir) | `GetLibraryTable`, `AddLibraryTableEntry`, `UpdateLibraryTableEntry`, `DeleteLibraryTableEntry` → *"no handler available"* (protos exist since `4437bf38`, handlers were never added; `API_HANDLER_LIBRARIES` registers only `GetLibraryItems`, `GetLibraryStatuses`, `ReloadLibrary`, `GetItemsFromLibrary`, `LoadAllLibraries`; no upstream MR). No `kicad-cli` command writes a table. Without a row, `SaveCopyOfDocument` → *"failed to save footprint copy"*. With a row: missing dir → status `LLS_INVALID`, save fails and **does not create the dir**; `mkdir` alone is not enough while the server holds the failed load (`LLS_ERROR … not found`) → `ReloadLibrary` → `LLS_LOADED` → save works, listed without further reload. Remaining non-API steps: **the table row (a file) and `mkdir`**. Also: `SaveCopyOfDocument` onto an existing name **overwrites silently**; there is **no command to delete a library item**. |
| 3 | Location B, two projects | **go** (given a row) | `${KIPRJMOD}/../../footprints/copperchat.pretty` resolves in both projects (`LLS_LOADED`), the file lands at the repo root, no copies in the project dirs, tables untouched by KiCad. `main` creates, `io` (second api-server, running at the same time) reads the same geometry, edits and saves; `main` sees the edit **only after `ReloadLibrary`** (stale cache before). No locking between servers: last writer wins. |
| 4 | Coexistence sch + pcb + fp | **go** with rules | One api-server, schematic + board open: a schematic commit stays open across footprint create + save, and `drop_commit` is still exact; a pushed schematic commit next to an open (then dropped) footprint commit; schematic usable after the footprint document closes; board untouched. **Bookkeeping bug** in `command_api_server.cpp`: opening a 2nd footprint without closing the 1st leaves a stale entry → closing either fails (*"Requested document does not match the open document"*); the server keeps working (next open replaces the footprint). Rule: one footprint document at a time, always close before opening the next. Saving an edited stock footprint is refused (read-only system library). Closing the *last* open document unloads the project (keep the schematic open). |
| 5 | Read without board | **go**, memory caveat | With only the schematic open, footprint library requests → *"no handler"* (as in 2B). The first `OpenDocument(DOCTYPE_FOOTPRINT)` registers the footprint-library handler, but it `AsyncLoad` + `BlockUntilLoaded`s **all 156 footprint libraries**: 3.0 s, RSS 221 → 1 987 MB (grew to 4.1 GB by the end of the probe). After that, without a board: `GetItemsFromLibrary` returns the full definition — pads (number, type, shape, position, size, drill, layer set), CrtYd/Fab shapes, `Footprint3DModel` items, description; batched **401 footprints in 0.2 s**, single id 61 ms median / 152 ms max. Footprint-document route (open + `GetItems` + `GetItemsById(footprint)`): 51 ms per footprint, includes attributes. Stock + local identical. A board-only server (Q7) places after a lazy per-library `GetItemsFromLibrary` at **121 MB**. |
| 6 | kipy wrappers | see below | Works: `KiCad.open_document/close_document/get_open_documents`, `get_library_footprint(s)`, `get_library_statuses`, `reload_library`, `wait_for_libraries`; `Board(fp_doc)` for pads/shapes (`get_items([pad, shape])`, `create_items`, `update_items`, `remove_items`, `begin/drop_commit`, `save`, `get_items_by_id(footprint)`). Breaks: `Board(fp_doc).get_items()` / fields → `AssertionError` (fields are `kiapi.board.types.Field`, not a `BoardItem`); `save_as("lib:Name")` resolves the path to `/spike/copperchat` → wrong library; `is_document_modified()` → always False (`GetDocumentModifiedState` answers `DMS_UNKNOWN`); `open_library_item` is GUI-only. Missing: library-table wrappers (and handlers), batched `GetItemsFromLibrary`, `SaveCopyOfDocument` with a lib id. Raw protobuf needed for: SaveCopy, field create/update, any `GetItems` incl. fields, batch library reads. Prototype: `spikes/3a-footprints/fpdoc.py`. |
| 7 | Schematic + board see it | **go** | Symbol `Footprint` field set through `update_items` (definition-less, spike 2A). ERC (`kicad-cli sch erc`, fresh process): no `footprint_link_issues` for L201; negative control L202 → *"Footprint 'Does_Not_Exist' not found in library 'copperchat'"*. The board in the same server places the new footprint immediately (shared library adapter, no reload); a fresh board-only server needs the library loaded first (`GetItemsFromLibrary`, as for stock footprints), then places it (2 pads). |
| 8 | SVG render | **go** | `kicad-cli fp export svg --fp <name> --layers …`: units are mm (`viewBox` = size in mm), origin = footprint anchor, y down, one colour per layer, path coordinates are stroke centrelines, text as `<g class="stroked-text">`. Measured extents: CrtYd 4.0 × 4.0, Fab 3.0 × 3.0 (text excluded), Cu 3.5 × 3.5 — exact. Page = bounding box of all content (17.2 × 7.3 mm here, the Value text dominates). Pitfall: the output dir must exist; with a missing dir it prints "Error creating svg file" and **still exits 0**. |

## Findings
1. **Everything below the footprint is API-only today** (pads, graphics, texts, fields incl. custom
   ones), in one commit, exact on read-back, and KiCad writes a current-format file that
   `fp upgrade` leaves byte-identical.
2. **Two things are not reachable through the API at this pin** and both block "everything through
   the API":
   - **library table rows** (no handlers for the table commands): without a row nothing can be
     saved into a new library;
   - **footprint-level attributes** (description, keywords, SMD/THT + exclusion flags, 3D models,
     overrides): a footprint made from a seed keeps the seed's values.
3. Small things that shape the tool: `mkdir` + `ReloadLibrary` for a new library; silent overwrite on
   `SaveCopyOfDocument` (check existence first); no delete-item command; one footprint document per
   server (close before open); `GetDocumentModifiedState` is useless for footprints; other servers
   need `ReloadLibrary` after a write (`ReloadLibrary` is asynchronous — `wait_for_libraries` after it).
4. **Memory:** the first footprint document loads every footprint library (~+1.8 GB, 3 s), which
   undoes the 2026-10-18 owner decision "footprint libraries lazily" for any server that touches a
   footprint. `handleOpenFootprint` calls `adapter->AsyncLoad(); adapter->BlockUntilLoaded()`.

## Decision
**3A partial — footprint content via the API is a go; two KiCad API gaps need an owner decision
before the implementation plan:**

| Gap | Options | Recommendation |
|---|---|---|
| G1 `fp-lib-table` row (Q2/Q3) | **A** CopperChat writes only the table row (KiCad's own one-line format, `(version 7)`), verified by KiCad (`ReloadLibrary` + `GetLibraryStatuses` = `LLS_LOADED`); **B** implement the `Get/Add/Update/DeleteLibraryTableEntry` handlers (upstream MR, carried in a KiCad fork for kicad-base until merged); **C** the user adds the library once per project in the KiCad GUI | **B**, with A as a stop-gap only if the implementation should start before the fork exists |
| G2 footprint attributes (Q1) | **B** small patch: let `UpdateItems(FootprintInstance)` in the footprint document apply `attributes`/`definition` (description, keywords, attrs, 3D models, overrides) to the open footprint; **D** accept seed values (not acceptable: wrong description and 3D model) | **B** (same fork/MR as G1) |
| G3 undo / `fp_delete` | **B** add a `DeleteLibraryItem`-style command to the patch; **E** CopperChat deletes the `.kicad_mod` it created itself (file removal, no content written) | B if the fork exists, else E |

Also worth one upstream patch line each (same MR or separate): the stale footprint entry in
`command_api_server.cpp`, `GetDocumentModifiedState` for footprints, and not loading every footprint
library in `handleOpenFootprint` (memory).

## Recommended tool shape (for the design doc)
- `FootprintDocument` helper (raw protobuf, `fpdoc.py` as the starting point) with a per-server lock:
  open → commit → save → close, never two footprint documents.
- `fp_create(name, pads[], graphics[], fields{}, attributes{}, source)` → seed by mounting type (SMD:
  any 2-pad SMD stock footprint; THT: a THT one) → `SaveCopyOfDocument` → rebuild in **one** commit →
  save → read back + `fp_check` → `ReloadLibrary` in every other running server of the repo. Refuse
  if the name exists (silent overwrite). Mark `needs_user_review` and store the "Footprint source"
  field.
- `fp_get(lib_id)` → batched `GetItemsFromLibrary` (cache by lib id); geometry summary like
  `l201.geometry()`; needs the footprint-library handler (board open or one footprint document).
- `fp_check` → courtyard encloses copper, silk clear of copper (≥ 0.1 mm), pin 1 position, pad sizes
  vs spec; pure geometry on the read-back.
- `fp_render` → `kicad-cli fp export svg --fp` into an existing dir, per-layer exports for
  measurement; check the file exists (exit code is 0 on failure).
- `fp_list_local` → `GetLibraryItems` on the project-scope rows.
- `sch_set_fields` Footprint check for local libraries → ERC `footprint_link_issues` (works today).

## Risks
- A KiCad fork for kicad-base (G1/G2) means rebasing the patches at every KiCad bump and a ~2 h cold
  base build; upstream acceptance is not guaranteed.
- Memory (Q5): ~2 GB per api-server once a footprint is opened (4 GB observed during heavy reads).
- Shared library across concurrently running project servers: no lock, stale caches until
  `ReloadLibrary`.
- The seed approach depends on stock footprint names (`Inductor_SMD:L_Bourns-SRN4018` here); pin a
  small seed list and check it at startup.

## For later
- **History snapshots** cover only files *below the project dir* (`history/store.document_files`:
  `.kicad_pro`, `.kicad_sch`, board) — the shared library at the repo root is outside it, so neither
  snapshots nor the external-change check see `.kicad_mod` changes; `fp-lib-table` is not covered
  either. The design must add the library dir(s) referenced by the project table (outside the project
  dir) and the table itself.
- Undo of `fp_create` in the plan/undo engine: no API delete (G3); "restore" of an edited footprint =
  write back the snapshot bytes KiCad wrote (as for schematics today).
- KiCad-11 files start with `(kicad_footprint_lib (version 20261031) …)`; KiCad 10 cannot read them
  (`kicad-cli fp downgrade` exists if a user needs that).
- Not tested: 3D-model paths with `${KIPRJMOD}`, THT/custom-shape pads, footprints with many pads
  (time per `CreateItems` batch), concurrent writes to the same footprint from two servers.
