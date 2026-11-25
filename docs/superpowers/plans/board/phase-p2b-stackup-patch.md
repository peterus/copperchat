# Phase P2b — KiCad patch 0005 (`UpdateBoardStackup`, gap 4A-G1) and stackup presets

> Index contracts: B-C8 (`STACKUPS`), B-C14 (`stackup_unavailable`); index call 2 and
> "Provisional" 3. Spec §4 (C3, C14), §7.2 `pcb_set_rules.stackup`, §15.3 D1, §16 (G1).
> Spike 4A Q2 (`spikes/4a-board/q02_setup.py`: `update_stackup_no_handler`).
> **Wave 4: starts when P2 is merged and the owner gave the go** (index "Provisional" 3).

**Goal:** a carried KiCad patch gives the IPC API a handler for `UpdateBoardStackup`, the
kicad-base recipe builds it, and `pcb_set_rules {stackup: "jlc-4l-1.6-7628"}` writes JLC's
real stackup into the board — refusing cleanly with `stackup_unavailable` on an unpatched
server.

`cc/` = `backend/src/copperchat/`.

**Constraints (C14, Global Constraints):** the patch is a `git format-patch` commit on top of
`KICAD_COMMIT` (after 0001–0004) with the placeholder subject `TODO(owner): write message
(board stackup handler)` and the trailer `Assisted-by: Claude` (no `Co-Authored-By`); code
comments in the patch are minimal and technical (the owner rewrites every human-readable
upstream text); nothing is posted on KiCad's GitLab, mailing list or Discord; no kipy change
(the proto messages exist).

**Files:**
- Create: `docker/kicad-base/patches/0005-TODO-owner-write-message-board-stackup-handler.patch`,
  `docs/kicad-patches/0005-board-stackup-FACTS.md` (owner notes),
  `backend/tests/kicad/board/test_stackup_patch.py`, `backend/tests/unit/board/test_stackups.py`
- Modify: `docker/kicad-base/patches/README.md` (table row "4A-G1"), `cc/board/presets.py`
  (`StackupPreset`, `STACKUPS`), `cc/kicad_tools/board/setup.py` (stackup branch of
  `pcb_set_rules`), `cc/kicad_tools/board/specs_setup.py` (schema enum), `CHANGELOG.md`

---

### Task 1: Read the pinned sources; write the handler and its QA test

**Interfaces (KiCad side, at `52b43fb8fc8e11fdab5d683eaefc91a49f9cbcd7`):** facts verified in
a local partial checkout at the pin: `API_HANDLER_BOARD::handleGetStackup`
(`pcbnew/api/api_handler_board.cpp:1024`) validates the document and fills the response with
`board::PackBoardStackup` (`pcbnew/api/api_pcb_utils.cpp:183`:
`GetStackupOrDefault().Serialize(...)` + user layer names); write handlers live in
`API_HANDLER_PCB` (`pcbnew/api/api_handler_pcb.cpp`: `handleSetBoardEnabledLayers` :629,
`handleSetBoardDesignRules` :914, registered at :138/:140); `UpdateBoardStackup { board,
stackup }` is declared in `api/proto/board/board_commands.proto:53` ("not yet implemented",
response `BoardStackupResponse`). **Not available locally** (read them in Step 1):
`pcbnew/board_stackup_manager/board_stackup.{h,cpp}` (`BOARD_STACKUP::Serialize` and whether a
`Deserialize` exists), `qa/tests/api/` (the e2e harness 0001/0003 extend in
`test_api_e2e_libraries.cpp`), and the commit/undo pattern a board-settings change needs.

Handler `API_HANDLER_PCB::handleUpdateBoardStackup(const HANDLER_CONTEXT<UpdateBoardStackup>&)
-> HANDLER_RESULT<BoardStackupResponse>`:
1. `validateDocument`; the request's copper layer count must equal the board's enabled copper
   count (else `AS_BAD_REQUEST`, "change the layer count with SetBoardEnabledLayers first" —
   the proto's "content on removed layers is deleted" case is never reached);
2. build a `BOARD_STACKUP` from the message (`Deserialize` if the pin has it, else per layer:
   type, layer id, thickness, material, εr, loss tangent, dielectric sub-layers, colours;
   finish, impedance-controlled flag, edge settings); dielectric thickness > 0, copper
   thickness > 0, every copper `layer` enabled (else `AS_BAD_REQUEST`);
3. store it in `board()->GetDesignSettings()` (stackup descriptor, `m_HasStackup = true`,
   board thickness from `BuildBoardThicknessFromStackup()`), mark the board modified the way
   `handleSetBoardDesignRules` does;
4. respond with `PackBoardStackup` (normalised form, so set + get round-trips).

- [ ] **Step 1: Read the pinned sources** in a throwaway container from the kicad-base
  `deps` stage: `docker build --target deps -t cc-kicad-deps docker/kicad-base`; inside, clone
  `KICAD_REPO` at `KICAD_COMMIT` (shallow, as the Dockerfile does), `git am` patches 0001–0004
  (they are format-patch files), then read `board_stackup.{h,cpp}`, `api_handler_pcb.cpp`
  around :914, and `qa/tests/api/`. Write the findings (Deserialize present?, modify/commit
  pattern, QA harness entry points) into the FACTS file (Task 3) before coding.
- [ ] **Step 2: Failing QA test** in `qa/tests/api/` (a new case next to the existing board/API
  e2e tests, or `test_api_e2e_board_stackup.cpp` registered in `qa/tests/api/CMakeLists.txt`
  if no board e2e file exists): open a 4-layer board, `UpdateBoardStackup` with the
  JLC04161H-7628 thicknesses → `GetBoardStackup` returns them (±1 nm); a 2-copper stackup on
  the 4-layer board → `AS_BAD_REQUEST`; a dielectric thickness 0 → `AS_BAD_REQUEST`.
- [ ] **Step 3: Build and run** in the same container with `-DKICAD_BUILD_QA_TESTS=ON` (only
  the API QA target, ccache mounted, `timeout 21600`) — FAIL (no handler). **Step 4:
  Implement** the handler + registration. **Step 5: Run** — PASS; record the exact command,
  runtime and result in the FACTS file.
- [ ] **Step 6: Commit in the KiCad tree** with subject `TODO(owner): write message (board
  stackup handler)`, body empty except the trailer `Assisted-by: Claude`, author = the git user;
  `git format-patch -1 --start-number 5 -o docker/kicad-base/patches/` → file name
  `0005-TODO-owner-write-message-board-stackup-handler.patch` (rename only if `format-patch`
  derived a different slug, and say so in "As built"). Verify `git apply --check` of 0001–0005
  in name order on a clean checkout of `KICAD_COMMIT` (the Dockerfile applies with `git apply`).
- [ ] **Step 7: Commit (CopperChat)** `build(kicad-base): patch 0005 UpdateBoardStackup handler (4A-G1)`
  with the patch, the README row: `| 0005 | 4A-G1 board stackup | Handler for
  UpdateBoardStackup (same message as GetBoardStackup; copper layer count must match the
  enabled layers; responds with the normalised stackup). |` and the README's test line
  extended by the QA file name.

### Task 2: Base image and the integration test

**Interfaces:** the recipe hash (`scripts/kicad-base-image.sh recipe`) changes with the new
patch, so `kicad-base.yml` builds `<KICAD_COMMIT>-r<recipe>` on this PR and CI's kicad jobs
resolve to it (existing logic; no workflow change). Test helper in the test module:
`update_stackup(h, stackup) -> BoardStackup` = raw `client.send(UpdateBoardStackup(...),
BoardStackupResponse)` (kipy has the classes, no wrapper).

- [ ] **Step 1: Failing kicad tests** (`test_stackup_patch.py`, style of
  `tests/kicad/test_kicad_api_patches.py`, `ecc83_session` switched to 4 layers through
  `set_enabled_layers`): `test_set_get_roundtrip` (JLC04161H-7628 values set → `get_stackup()`
  equal within 1 nm, copper 0.035 / 0.0152 / 0.0152 / 0.035, dielectrics 0.2104 / 1.065 /
  0.2104); `test_saved_board_has_stackup` (`save("pcb")` → file contains `(stackup` and
  `0.2104`); `test_copper_count_mismatch_rejected` (2-copper message → `ApiError` with
  `AS_BAD_REQUEST`, stackup unchanged); `test_zero_dielectric_rejected`.
- [ ] **Step 2: Run** locally against the recipe-pinned image built from this branch
  (`docker build` of `docker/kicad-base` with `--build-arg KICAD_COMMIT=…`, `timeout`) — FAIL
  before Task 1's patch is in the build context, PASS after.
- [ ] **Step 3: Commit** `test(kicad-base): UpdateBoardStackup round trip`.

### Task 3: Stackup presets and `pcb_set_rules.stackup`

**Interfaces:** `cc/board/presets.py`:
```python
@dataclass(frozen=True) class StackupLayer: kind: Literal["copper","prepreg","core"]; thickness: float; material: str
@dataclass(frozen=True) class StackupPreset: id: str; copper_layers: int; jlc_name: str | None
                                             layers: tuple[StackupLayer, ...]   # top to bottom
STACKUPS: Mapping[str, StackupPreset]
```
Values (mm, top → bottom):
- `jlc-2l-1.6`: Cu 0.035 / FR-4 core 1.53 / Cu 0.035 (`jlc_name None`; verify against
  jlcpcb.com's 2-layer 1.6 mm build-up in Step 1 and record the source line in "As built").
- `jlc-4l-1.6-7628` (`JLC04161H-7628`, index call 8): Cu 0.035 / 7628 prepreg 0.2104 /
  Cu 0.0152 / core 1.065 / Cu 0.0152 / 7628 prepreg 0.2104 / Cu 0.035.
- `jlc-6l-1.6` (`JLC06161H-3313`): Cu 0.035 / 3313 prepreg 0.0994 / Cu 0.0152 / core 0.55 /
  Cu 0.0152 / 2116 prepreg 0.1088 / Cu 0.0152 / core 0.55 / Cu 0.0152 / 3313 prepreg 0.0994 /
  Cu 0.035.
εr / loss tangent per material from jlcpcb.com/impedance, recorded with the access date; if
the page gives none for a material, KiCad's default for FR-4 is kept and the report says so.

`pcb_set_rules`: `stackup` enum = `STACKUPS` keys; its copper count must equal the resulting
layer count (`layers` arg or current) → else `invalid_argument`; new stage after "layers":
`GetBoardStackup` (kept for `undo`) → build the request from the current stackup with the
preset's thicknesses/materials → `UpdateBoardStackup`; an `ApiError` whose message contains
`no handler available for request of type kiapi.board.commands.UpdateBoardStackup` →
`StructuredToolError(code="stackup_unavailable", suggestion="the default stackup stays; the
KiCad base image with patch 0005 is needed")`; the engine's compensation then runs the
`undo` of the completed "layers" stage, so nothing of the call stays applied (`prepare` never
writes, so it does not probe). Read-back verify: thicknesses ±0.0005 mm. Report adds `stackup {id,
jlc_name, total_mm}`.

- [ ] **Step 1: Failing unit tests** (`test_stackups.py`): values above verbatim;
  `test_copper_counts` (2/4/6); `test_total_thickness` (each preset's sum within
  1.6 ± 0.1 mm, the mask excluded — record the sums); `test_stackup_enum_in_schema`.
- [ ] **Step 2: Failing kicad tests** (`test_stackup_patch.py`, `synth_session` after sync):
  `test_set_rules_stackup_4l` (`layers: 4, preset: "jlcpcb-4l", stackup: "jlc-4l-1.6-7628"`
  → `get_stackup` matches, `board_hash` changed); `test_stackup_layer_mismatch`
  (`stackup: "jlc-6l-1.6"` on 4 layers → `invalid_argument`, board unchanged);
  `test_fail_after_stackup_restores` (`fail_after` on the stage after stackup → stackup and
  `board_hash` equal pre-call); unit `test_unpatched_detected` (fake handle raising the
  "no handler" `ApiError` → `stackup_unavailable`, the layers stage's `undo` ran).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): JLC stackup presets via patched UpdateBoardStackup`.

### Task 4: Owner notes, docs + index as built, checks, PR

- [ ] `docs/kicad-patches/0005-board-stackup-FACTS.md` (owner notes, not upstream text):
  what the handler does and refuses; files touched; the QA test and the CopperChat
  integration tests with the commands and results of Task 1/2; what the owner must do
  upstream under C14 (open an issue / feature request assigned to the owner before an MR;
  write the commit message and MR text); that nothing was posted anywhere.
- [ ] Index "As built (P2b)": final patch file name, recipe tag built by the PR, measured
  stackup sums, εr source, the 2-layer verification. Remove "Provisional" 3. CHANGELOG
  `[Unreleased]`: "JLC stackup presets for board rules — behind COPPERCHAT_BOARD_TOOLS.
  **Release note:** PROD needs the new kicad-base tag; `kicad-base.yml` on `main` must have
  published this recipe before a release is cut."
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, `uv run pytest -m kicad tests/kicad/board tests/kicad/test_kicad_api_patches.py`
  in the recipe-pinned image (with `timeout`).
- [ ] PR `board phase P2b: stackup patch 0005`; review-loop per the index's Phase Conventions
  (the patch is reviewed like code; its subject/trailer are not "fixed" by reviewers).

## Acceptance (P2b)

- [ ] `kicad-base.yml` built the recipe-pinned tag on the PR; CI `backend` and all 8
  `kicad (…, i/8)` shards green on it.
- [ ] Patch 0005 applies after 0001–0004 with `git apply`; subject placeholder, trailer
  `Assisted-by: Claude`; README row "4A-G1".
- [ ] `test_set_get_roundtrip`, `test_saved_board_has_stackup`, `test_set_rules_stackup_4l`,
  `test_unpatched_detected` pass; QA test result recorded in the FACTS file.
- [ ] CHANGELOG release note about the base tag present; nothing posted upstream.
