# KiCad API patches

`git format-patch` series on top of `../KICAD_COMMIT`, applied in name order by the
Dockerfile's build stage (`git apply`). They close the KiCad IPC API gaps found in spike 3A
(`docs/spikes/3a-footprints.md`) so that footprints can be created, edited and deleted
through the API only, and gap G1 of spike 4A (`docs/spikes/4a-board.md`: no stackup
setter). Each patch is meant to go upstream as its own merge request; the commit messages
are placeholders until the owner writes them (KiCad's policy on tool-generated content).
Drop a patch once KiCad has the change and `KICAD_COMMIT` is bumped past it; re-generate the
series on every KiCad bump (`git am` onto the new commit).

| Patch | Gap | API |
|---|---|---|
| 0001 | G1 library table rows | Handlers for `GetLibraryTable`, `AddLibraryTableEntry`, `UpdateLibraryTableEntry`, `DeleteLibraryTableEntry` (any library type, global or project table; KiCad saves the table and reloads it). New `AddLibraryTableEntry.create_library`: creates an empty library at the row's URI if nothing exists there. |
| 0002 | G2 footprint properties | `UpdateItems(FootprintInstance)` on the footprint open in the footprint editor updates the footprint's own properties in place (description, keywords, attributes, overrides, net ties, private layers, jumpers, height, 3D models); its children stay as they are. |
| 0003 | G3 delete a library item | New `DeleteLibraryItem { type, item }` → `LibraryCommandStatus` for symbols, footprints and design blocks. |
| 0004 | api-server bookkeeping | Opening a footprint while another is open replaces the open one (closing it no longer fails with "Requested document does not match"). |
| 0005 | 4A-G1 board stackup | Handler for `UpdateBoardStackup` (same message as `GetBoardStackup`; copper layer count must match the enabled layers; responds with the normalised stackup). `SetBoardEnabledLayers` keeps an explicit stackup in sync with the new layer count. |

The proto changes of 0001 and 0003 are also carried by the kipy fork
(`proto_patches/` on branch `copperchat` of github.com/peterus/kicad-python), so the Python
classes match; `KIPY_COMMIT` pins that commit. Integration tests:
`backend/tests/kicad/test_kicad_api_patches.py`, `backend/tests/kicad/board/test_stackup_patch.py`
(0005); KiCad QA tests: `qa/tests/api/test_api_e2e_libraries.cpp` (added by the patches) and the
`UpdateBoardStackup*` / `SetBoardEnabledLayersKeepsStackupInSync` cases in
`qa/tests/api/test_api_handler_pcb.cpp` (0005). 0005 changes no proto message (only the
comment of `UpdateBoardStackup`), so the kipy fork needs no change for it.
