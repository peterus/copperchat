# KiCad patch 0005 — board stackup handler: facts for the owner

Plain facts for writing the upstream commit message / issue / MR description yourself. Nothing
here is meant to be pasted upstream; KiCad's tool-generated-content policy applies
(dev-docs.kicad.org/en/rules-guidelines/tool-generated-content). Same conventions as patches
0001–0004 (their facts: `~/copperchat-kicad-mr/kicad-api-patches/FACTS.md`).

## Provenance

- All code, code comments and QA tests in the patch were written by Claude (Claude Code, model
  Opus 5.5) and are tool-generated. The commit carries `Assisted-by: Claude`.
- Commit subject is a placeholder: `TODO(owner): write message (board stackup handler)`, body
  empty except the trailer. Author field: Peter Buchegger (local git config).
- Code comments are short and technical; rewrite them as you see fit.
- **Nothing was posted anywhere**: no GitLab issue, MR or comment, no mailing list, no Discord.

## Base and layout

- File: `docker/kicad-base/patches/0005-TODO-owner-write-message-board-stackup-handler.patch`.
- Base: KiCad master `52b43fb8fc8e11fdab5d683eaefc91a49f9cbcd7` + 0001–0004 (applies with
  `git apply` / `git am` after them). It does not depend on 0001–0004 in code; it only touches
  `qa/tests/api/test_api_handler_pcb.cpp`, which 0001–0004 do not change, so it should also
  apply alone on the base commit (not built that way).
- No proto message changes (only the comment of `UpdateBoardStackup`), so kicad-python needs no
  change: `UpdateBoardStackup` and `BoardStackupResponse` already exist there.

## Problem

- `UpdateBoardStackup { board, stackup }` is declared in `api/proto/board/board_commands.proto`
  ("not yet implemented"); no handler is registered, so a request answers "no handler available
  for request of type kiapi.board.commands.UpdateBoardStackup". `GetBoardStackup` works.
- `BOARD_STACKUP::Deserialize( const kiapi::board::BoardStackup& )` exists but is a stub
  (`// Read-only for now`, `return false`).
- Result: an API client cannot set a board's layer build-up (thicknesses, dielectric materials,
  Er), which fab-specific stackups (e.g. JLCPCB JLC04161H-7628) need.

## Change

1. `pcbnew/board_stackup_manager/board_stackup.cpp`: `BOARD_STACKUP::Deserialize` implemented.
   Builds the item list from the message: per layer the type (copper, dielectric, solder mask,
   solder paste, silkscreen — unknown/undefined types refused), board layer (must match the
   type: copper layer for copper, F/B.Mask for mask, …), enabled flag, colour (stored as hex),
   thickness; dielectrics get core/prepreg from the dielectric type (`BSDT_PREPREG` → prepreg,
   anything else → core), sequential dielectric ids and every sub-layer (thickness, locked
   flag, material, Er, loss tangent, spec frequency — absent = 0 —, dielectric model); solder
   mask thickness/material/Er/loss tangent; silkscreen material; the default type names KiCad
   uses for mask/paste/silk. Board-level: finish (kept when empty), impedance-controlled flag,
   edge connector (kept when `BECT_UNKNOWN`), edge plating. `*this` changes only on success.
   For a dielectric with sub-layers, the sub-layers' thicknesses are used (the layer-level
   thickness only when no sub-layer is given).
2. `pcbnew/api/api_handler_pcb.{h,cpp}`: `handleUpdateBoardStackup`, registered next to
   `SetBoardEnabledLayers`. After `validateDocument`:
   - `Deserialize` fails → `AS_BAD_REQUEST`;
   - copper and dielectric layers must be enabled, alternate, start and end with copper;
   - the copper layers must be exactly the board's enabled copper layers in stack order
     (else `AS_BAD_REQUEST`, "change the layer count with SetBoardEnabledLayers first"), so the
     proto comment's "content on removed layers is deleted" case is never reached;
   - every copper and dielectric (sub-)layer thickness > 0, others ≥ 0;
   - mask/paste/silk layers must be enabled on the board, appear once, front ones above the
     copper, back ones below;
   - then: the design settings' stackup descriptor is replaced, `m_HasStackup = true`, board
     thickness = `BuildBoardThicknessFromStackup()`, `onModified()` (same as
     `handleSetBoardDesignRules`); no undo entry (board settings changes through the API have
     none today either).
   - response: `PackBoardStackup` (the normalised stackup, as `GetBoardStackup` returns it).
3. `handleSetBoardEnabledLayers`: when the board has an explicit stackup (`m_HasStackup`), it
   is re-synchronised with the new layer set (`BOARD_STACKUP::SynchronizeWithBoard`, as the
   board setup dialog does) and the board thickness recomputed. Without this, after a layer
   count change `GetBoardStackup` (and the saved file) kept describing the old copper layers.
   This is a latent upstream issue of its own (any loaded board with a `(stackup …)` section);
   it could be a separate small MR.
4. `api/proto/board/board_commands.proto`: comment of `UpdateBoardStackup` updated (no longer
   "not yet implemented"; states the copper-layer rule).

## How it was tested

- KiCad QA (`qa/tests/api/test_api_handler_pcb.cpp`, suite `ApiHandlerPcb`, in-process handler
  tests on the four-copper `issue5830` board — there is no board e2e test file, so the cases
  went next to the existing handler tests, no CMake change):
  `UpdateBoardStackupRoundTrip` (JLC04161H-7628 thicknesses set → response and
  `GetBoardStackup` equal to the nm; material/Er read back; `m_HasStackup`; board thickness =
  stackup sum), `UpdateBoardStackupCopperCountMismatchRejected` (2-copper message →
  `AS_BAD_REQUEST`, stackup unchanged), `UpdateBoardStackupInvalidThicknessRejected` (zero
  dielectric, zero copper), `UpdateBoardStackupLayerOrderRejected` (two coppers in a row; a
  copper layer the board lacks), `SetBoardEnabledLayersKeepsStackupInSync` (4 → 6 layers after a
  stackup update: 6 copper / 5 dielectric, F.Cu thickness kept).
  - Before the handler (test only): 7 failures (status 5 = `AS_UNHANDLED`, no handler).
  - With the patch: all `ApiHandlerPcb` cases pass ("No errors detected", 2026-11-25).
  - Full `qa_api` in this partial build: the `ApiHandlerPcb` suite passes; 20 failures in
    `ApiE2E` / `ApiJobs` (api-server e2e: schematic open fails, `nng_recv` timeouts) because
    only `qa_api`'s own dependencies were built (no eeschema kiface). Not caused by the patch,
    but not re-run on a full build — do that before an MR.
  - Command (Debian trixie, the kicad-base `deps` stage + `libboost-test-dev`,
    `-DKICAD_BUILD_QA_TESTS=ON`, target `qa_api` only, `-j5`):
    `qa/tests/api/qa_api --run_test=ApiHandlerPcb`. Cold build ≈ 2 h 10 min on the 6-core host
    (shared with other builds); incremental rebuild after the patch 92 s.
- CopperChat integration tests (`backend/tests/kicad/board/test_stackup_patch.py`, Python/kipy
  over the IPC API against `kicad-cli api-server`, image
  `copperchat-kicad-base:52b43fb8…-r0f2b8cc78711f52b` built by the PR): set/get round trip,
  saved `.kicad_pcb` contains the stackup, copper-count mismatch and zero dielectric refused,
  `pcb_set_rules {stackup}` end to end incl. compensation after a later failure. All 7 pass on
  the patched image; on the unpatched published image 5 fail (no handler) as expected.

## What the owner has to do upstream (C14)

- Open an issue / feature request on gitlab.com/kicad/code/kicad first (implement
  `UpdateBoardStackup`), assigned to yourself; a feature needs a core developer's OK before an
  MR. The `SetBoardEnabledLayers` stackup sync (change 3) is a bug of its own → its own issue
  and MR if you want to send it.
- Write the commit message(s) and the MR text yourself; rebase on current master (re-run
  `qa_api --run_test=ApiHandlerPcb`).
- Possible upstream questions: whether `UpdateBoardStackup` should also change the copper layer
  count (the proto comment originally implied it; this patch refuses it), whether an undo/commit
  entry is wanted, and whether standard colour names should survive a round trip (the patch
  stores colours as hex).
