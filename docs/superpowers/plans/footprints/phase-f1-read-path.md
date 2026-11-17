# Phase F1 — Read path: activation, `FootprintDocument`, geometry, `fp_get`, `fp_list_local`

> Index contracts: FP-C1, FP-C2, FP-C3, FP-C9. Spec §5 (read side), §6, §7.1–§7.3,
> §13.1 (`FP_READ`). Spike 3A Q4–Q6. Reference code: `FootprintDoc` and `l201_cycle` in
> `backend/tests/kicad/test_kicad_api_patches.py`, `spikes/3a-footprints/fpdoc.py`.
> Runs in **wave 1, parallel to F3** — stay inside the F1 column of the index's file
> ownership table.

**Goal:** the agent can read any footprint (stock or local) with its full geometry
without a board, and list the repo's local footprints; the memory-heavy library load
happens only when a footprint tool needs it.

**Files:**
- Create: `backend/src/copperchat/library/local.py`,
  `backend/src/copperchat/kicad_session/footprint_doc.py`,
  `backend/src/copperchat/kicad_tools/footprints/{__init__,model,read,specs}.py`,
  `backend/scripts/record-fp-fixtures.py`
- Modify: `config.py` (`footprint_tools`), `kicad_session/session.py`
  (`footprints_active`, `_fp_lock`, `activate_footprints`, `footprint_document`),
  `library/service.py` (FP-C1 additions; `footprints_available` → `footprint_libraries_ready`),
  `kicad_tools/edit/specs_place.py` (caller rename + `footprint_unverified` wording only),
  `library/specs.py` (caller rename), `tools/registry.py` (provider
  `copperchat.kicad_tools.footprints.specs`), `agent/profiles.py` (`FP_READ` part 1,
  `footprint_tools` filter), `.env.example` (commented `COPPERCHAT_FOOTPRINT_TOOLS=on`)
- Tests: `backend/tests/unit/footprints/{__init__,test_local_library,test_model,test_read_tools}.py`,
  `backend/tests/unit/test_config.py`, `backend/tests/unit/agent/test_profiles*.py` (existing file),
  `backend/tests/kicad/footprints/{__init__,conftest,test_activation,test_fp_get,test_fp_list_local}.py`,
  fixtures `backend/tests/fixtures/footprints/*.pb` (recorded API output)

**Not in F1:** `fp_check`/`fp_render` (F2), anything under `history/` or `tools/spec.py`
(F3), any write, journal or `sch_set_fields` local-library logic (F4a/F4b).

---

### Task 1: Setting, local-library derivation, global-nickname probe

**Interfaces:** Produces `Settings.footprint_tools: bool`, FP-C1 `LocalLibrary`,
`base_nickname`, `derive_local_library`, `NAME_RE`.

- [ ] **Step 1: Failing unit tests** (`test_local_library.py`, `tmp_path`):
  - `test_nested_project_uri`: root `/r/OpenDriveHub`, project `/r/OpenDriveHub/hardware/main`
    → `nickname == "OpenDriveHub"`, `dir == root/"footprints/OpenDriveHub.pretty"`,
    `uri == "${KIPRJMOD}/../../footprints/OpenDriveHub.pretty"`.
  - `test_project_at_root_uri` → `"${KIPRJMOD}/footprints/OpenDriveHub.pretty"`.
  - `test_sanitise_and_cut`: `"my repo.ü-x"` → `"my_repo__-x"` (every char outside
    `[A-Za-z0-9_-]` becomes `_`); a 40-char name is cut to its first 32 chars.
  - `test_global_collision_appends_local`: global `{"OpenDriveHub"}` → `"OpenDriveHub_local"`
    and the dir follows (`OpenDriveHub_local.pretty`).
  - `test_same_result_for_two_projects`: `hardware/main` and `hardware/io` → equal
    `nickname` and `dir`, different `uri` only if depth differs.
  - `test_name_re`: accepts `L_Bourns_SRN3012BTA`, `R_0805+x.1`; rejects `""`, `"-x"`,
    `"a/b"`, `".."`, `"a b"`, a 101-char name.
  - `test_config.py::test_footprint_tools_env`: `COPPERCHAT_FOOTPRINT_TOOLS=off` → `False`;
    unset → `True`.
- [ ] **Step 2: Failing kicad probe** (`test_activation.py::test_global_table_before_activation`,
  `blank_nopcb_session`): call `get_library_table(LT_FOOTPRINT, LTS_GLOBAL)` before any
  footprint document was opened; assert either a non-empty row list containing
  `Inductor_SMD`, or an `ApiError`. The test records which (pytest `record_property`)
  and asserts the same branch `LibraryService.global_footprint_nicknames` takes.
- [ ] **Step 3: Run** — FAIL.
- [ ] **Step 4: Implement** `library/local.py` (pure, no KiCad import) and
  `Settings.footprint_tools: bool = Field(True, validation_alias="COPPERCHAT_FOOTPRINT_TOOLS")`
  (accepts `on`/`off` as well as booleans). `global_footprint_nicknames()` (in
  `LibraryService`): the API table if the probe branch says it works pre-activation,
  else the `*.pretty` directory names in the api-server's stock footprint dir (index
  author call 9); cached per session generation.
- [ ] **Step 5: Run** — PASS. Write the probe result into the index "As built".
- [ ] **Step 6: Commit** `feat(footprints): local library derivation and tools switch`.

### Task 2: Activation and `FootprintDocument`

**Interfaces:** Consumes the kipy fork's raw client (`h.kicad._client.send`). Produces
FP-C2 (`activate_footprints`, `footprints_active`, `footprint_document`,
`FootprintDocument`, `SEED_LIB_ID`).

- [ ] **Step 1: Failing unit tests** (`test_read_tools.py`, fake session object):
  `test_activation_refused_when_disabled` — `footprint_tools=False` → `StructuredToolError`
  `code == "footprint_tools_disabled"` and the fake records **no** open-document call.
- [ ] **Step 2: Failing kicad tests** (`test_activation.py`, `blank_nopcb_session`):
  - `test_activate_once`: first `activate_footprints()` returns a float > 0,
    `footprints_active` is True, second call returns `None`; RSS before/after is logged
    (caplog contains `footprint libraries loaded`).
  - `test_flag_resets_on_restart`: `await session.restart()` → `footprints_active` False.
  - `test_document_always_closed`: inside `async with session.footprint_document(SEED_LIB_ID)`
    raise `RuntimeError`; afterwards `get_open_documents(DOCTYPE_FOOTPRINT)` has no entry
    with an `entry_name`, and `h.schematic().get_symbols()` still works.
  - `test_read_items_of_seed`: `items()` contains 2 pads; `footprint()` has the seed's
    description; no write was made (seed `.kicad_mod` mtime unchanged).
  - `test_seed_missing`: monkeypatch `SEED_LIB_ID` to `Inductor_SMD:Nope` → activation raises
    `code == "seed_missing"` naming the lib id.
- [ ] **Step 3: Run** — FAIL.
- [ ] **Step 4: Implement** `kicad_session/footprint_doc.py` (port `FootprintDoc` to async
  methods that each go through `session.call`), and in `session.py`: `_fp_lock`,
  `footprints_active` (stored with the generation it belongs to), `activate_footprints()`
  (open + close the seed under `_fp_lock`, log RSS from `/proc/<pid>/status` of the
  api-server before/after), `footprint_document()` (close in `finally` under
  `asyncio.shield`; a failed close logs and sets the new flag `restart_pending`; F4a wires
  the turn-end restart in `history/hooks.py`, which F3 owns in wave 1). Never call `session.save()` inside the block.
- [ ] **Step 5: Run** — PASS. **Step 6: Commit** `feat(footprints): footprint document and activation`.

### Task 3: `FootprintGeometry`, canonical hash, recorded fixtures

**Interfaces:** Produces FP-C3 (`geometry_from_proto`, `geometry_hash`, `geometry_equal`,
`review_state`, field-name constants).

- [ ] **Step 1: Record fixtures.** `backend/scripts/record-fp-fixtures.py` (runs in the kicad
  image) writes `SerializeToString()` of the library response for
  `Inductor_SMD:L_Bourns-SRN4018`, one THT part (`Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical`),
  one QFN with exposed pad and roundrect pads (`Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm`)
  and one custom-shape pad footprint to `tests/fixtures/footprints/<name>.pb`. These are
  API outputs (protobuf), not KiCad files.
- [ ] **Step 2: Failing unit tests** (`test_model.py`):
  - `test_seed_geometry`: 2 SMD rect pads, layers `["F.Cu", "F.Mask", "F.Paste"]`,
    `mounting == "smd"`, `layer_summary()["F.CrtYd"]["closed"] is True`.
  - `test_bbox_excludes_texts`: a Text far outside does not change `bbox()`.
  - `test_summary_has_no_shapes`, `test_full_has_shapes`.
  - `test_hash_stable`: same input twice → same hex; reordering pads → same hash.
  - `test_hash_changes_on_manufacturing_relevant` — parametrised over: pad position
    +1 nm·10⁶ (1 µm), paste margin, mounting, `exclude_from_pos`, model path, a silk
    segment, Reference text position → hash differs.
  - `test_hash_ignores_metadata` — description, keywords, value of `Footprint source`,
    `needs_user_review`, `User review` → hash equal.
  - `test_geometry_equal_tolerance`: 0.5 µm off → `[]`; 2 µm off → one diff naming the pad.
  - `test_review_state`: fields `{Footprint source, needs_user_review: yes}` →
    `"needs_user_review"`; `no` → `"approved"`; source missing → `"unreviewed"`; review
    field missing → `"unreviewed"`; `pending=True` with both fields → `"unreviewed"`.
- [ ] **Step 3: Run** — FAIL.
- [ ] **Step 4: Implement** `model.py`: canonical JSON with coordinates rounded to 1 nm
  (`round(nm)` ints), lists sorted (pads by number then position, shapes by layer/kind/points),
  `json.dumps(sort_keys=True, separators=(",", ":"))`, sha256. Include everything spec §9.4
  lists; exclude description, keywords and field **values** (field names of custom fields
  are excluded too, Reference/Value **text** geometry is included).
- [ ] **Step 5: Run** — PASS. **Step 6: Commit** `feat(footprints): geometry model and hash`.

### Task 4: `LibraryService` footprint access and cache invalidation

**Interfaces:** Consumes Tasks 1–3. Produces FP-C1 `LibraryService` additions.

- [ ] **Step 1: Failing unit tests** (`tests/unit/library/test_service.py`, existing fakes):
  - `test_invalidate_clears_all`: fill `_fp_scopes`, `_fp_libs["X"]`, `_fp_failed["X"]` and the
    geometry LRU → `invalidate_footprints("X")` clears `X` entries and `_fp_scopes`;
    `invalidate_footprints(None)` clears everything.
  - `test_ready_board_or_active`: no board + inactive → False; `footprints_active` → True.
  - `test_local_names_on_disk`: dir with `A.kicad_mod`, `B.kicad_mod`, `c.txt`, a symlink
    `D.kicad_mod` → `["A", "B"]`; missing dir → `[]`; file content never opened (patch
    `Path.read_bytes`/`open` to raise).
  - `test_local_library_transient_returns_none` and
    `test_local_library_for_write_transient_raises` (`code == "repo_root_unknown"`).
  - `test_row_conflict`: project rows `{OpenDriveHub: other uri}` → `row_conflict` names both
    URIs; a row with another nickname pointing at the local dir → `row_conflict` asks to
    rename/remove it.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** the additions; `footprint_geometry()` uses batched
  `GetItemsFromLibrary` (existing `_get_items`) and an LRU (`functools`-free dict, 64
  entries); on a boardless server it calls `activate_footprints()` first and returns the
  seconds through a `notes` list the caller passes in. Rename every
  `footprints_available()` call (`specs_place.footprint_notes`,
  `library/specs.py`, `_require_footprints`) to `footprint_libraries_ready()`; change the
  `footprint_unverified` text to "footprint libraries are not loaded (no board, no
  footprint tool used)". Update the existing tests that assert the old text.
- [ ] **Step 4: Run** — PASS (unit + `uv run pytest -m kicad tests/kicad/edit/test_library_real.py`).
- [ ] **Step 5: Commit** `feat(footprints): library service footprint access`.

### Task 5: `fp_get` tool, provider, `FP_READ` part 1

**Interfaces:** Produces tool `fp_get` (`{lib_id, detail?}` → `FootprintGeometry.to_dict` +
`source`, `review`, `notes`), `TOOL_SPECS` in `kicad_tools/footprints/specs.py`,
`profiles.FP_READ = {"fp_get", "fp_list_local"}`, `profile_specs(..., footprint_tools: bool = True)`.

- [ ] **Step 1: Failing unit tests**:
  - `test_profiles`: `FP_READ` ⊆ `requirements`, `schematic`, `board`, `free`; with
    `footprint_tools=False` no profile (incl. Free) contains `fp_get`; Free stays a superset
    of the registry when enabled.
  - `test_fp_get_schema`: `additionalProperties: false`, `lib_id.maxLength == 256`,
    `detail.enum == ["summary", "full"]`; `lib_id` without `:` → `invalid_argument`.
- [ ] **Step 2: Failing kicad tests** (`test_fp_get.py`):
  - `test_fp_get_boardless_activates` (`blank_nopcb_session`): `fp_get("Inductor_SMD:L_Bourns-SRN4018")`
    → 2 pads, `scope == "global"`, `notes[0]["kind"] == "footprint_libraries_loaded"`;
    a second call has no such note.
  - `test_fp_get_with_board_is_lazy` (`blank_session`): result OK and `footprints_active` stays
    False (board servers read per library, spec §6.1).
  - `test_fp_get_not_found_closest`: `Inductor_SMD:L_Bourns-SRN4019` → `not_found`,
    `details.closest` contains the SRN4018.
  - `test_coexistence_with_open_commit`: open a schematic commit, call `fp_get`, drop the
    commit; schematic UUID set unchanged; no footprint doc open afterwards.
- [ ] **Step 3: Run** — FAIL.
- [ ] **Step 4: Implement** `read.py` (`async def fp_get(ctx, args) -> ToolResult`) and the spec
  in `specs.py` (`_obj` schemas as in `library/specs.py`); register the provider; find the
  `profile_specs` call site(s) (`grep -rn profile_specs backend/src`) and pass
  `ctx.settings.footprint_tools`.
- [ ] **Step 5: Run** — PASS. **Step 6: Commit** `feat(footprints): fp_get`.

### Task 6: `fp_list_local`, `repo_session` fixture, read paths write nothing

**Interfaces:** Produces tool `fp_list_local` (spec §7.3 output) and fixture `repo_session`
(git repo `<tmp>/OpenDriveHub` with `hardware/main` and `hardware/io`, each a copy of the
KiCad-generated blank project without board; yields `(main_session_factory, repo_root)` —
one server at a time, C14).

- [ ] **Step 1: Failing kicad tests** (`test_fp_list_local.py`, `repo_session`):
  - `test_no_library_yet`: `library is None`, a note explains it, and
    `session.footprints_active` stays False.
  - `test_lists_seed_copy_as_unreviewed`: in the test, through the raw API only, add the
    project row with `create_library=True`, `SaveCopyOfDocument` the seed to
    `<nick>:Seedcopy` (an API-made fixture standing in for a crash after S2) →
    `footprints[0]` has `name == "Seedcopy"`, `needs_user_review == "unreviewed"`,
    `source is None`, `pad_count == 2`.
  - `test_used_by`: assign `<nick>:Seedcopy` to a placed `Device:L` through the existing
    `sch_set_fields` path with the row present → `used_by == ["L1"]`.
  - `test_read_paths_write_nothing`: sha256 of every file under the repo root before and
    after `fp_get`, `fp_list_local` and activation → identical (journal is F4a; this pins
    files).
  - `test_limit_and_query`: `limit=1` → `truncated is True`; `query` filters by name/description.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** in `read.py`: `GetLibraryItems` on the local nickname when the row
  exists; `review` via `review_state(g, pending=False)` (F4a wires `pending` from the journal);
  `used_by` from the open project's symbol Footprint fields (the reader used by
  `sch_list_symbols`). No row → `library: null`, and **no** activation. Add
  `fp_list_local` to `FP_READ`.
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `feat(footprints): fp_list_local`.

---

**Acceptance (F1 done when):** all tests above green in CI (unit + kicad shards);
`fp_get` works on boardless and board projects; nothing in `history/` changed;
`ruff`/`mypy` clean; index "As built" has the Task-1 probe result.

**Risks:** the activation probe might show that `GetLibraryTable` needs the kiface
(fallback in Task 1); activation memory in CI (one module-scoped server per test module);
kipy fork raw client API changes (pin `copperchat-e0c9c8e`).

**Do NOT:** write any KiCad file or library row outside a test's explicit raw-API
fixture setup; touch `history/`, `tools/spec.py`, `agent/sdk_tools.py` (F3 owns them);
act on KiCad GitLab; request Copilot.
