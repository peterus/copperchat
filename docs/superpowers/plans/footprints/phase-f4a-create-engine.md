# Phase F4a — Footprint write engine and `fp_create`

> Index contracts: FP-C8 (owner), FP-C1–FP-C7 (consumed), FP-C9. Spec §6.3, §7.6, §9
> (all), §12, §16.1 (schema/prepare validation, journal, hash re-recording, library lock,
> generation skip), §16.2 (L201 basics, refusals, compensation, compensation failure,
> server death, turn end, `replace` compensation). Index author calls 7, 8, 11.
> Wave 3: starts only when **F1, F2 and F3 are merged**. Reference implementation for
> S2/S3: `l201_cycle` in `backend/tests/kicad/test_kicad_api_patches.py`.

**Goal:** `fp_create` builds a footprint in the repo's local library from explicit
datasheet geometry with a mandatory source, verified by read-back; every failure is
compensated, every write journalled, and the call is undoable from History.

**Files:**
- Create: `backend/src/copperchat/kicad_tools/footprints/{engine,build,journal,create}.py`
- Modify: `kicad_tools/edit/changeset.py` (`ChangeSet.library_row = None`),
  `kicad_tools/edit/engine.py` (`apply_change_set` refuses `FootprintChangeSet`),
  `errors.py` (`DocumentInconsistent.leftovers`, `footprint_only`), `agent/engine.py`
  (`_on_document_inconsistent` collects `leftovers`/`footprint_only` into the turn state →
  `TurnOutcome`), `agent/sdk_tools.py` (library lock around
  `apply` for footprint sets; `_inconsistent_error` leftovers branch), `history/hooks.py`
  (`_recovery_notice` leftovers branch; `_recover_inconsistent` skips the schematic reopen
  when every inconsistency of the turn is `footprint_only` — the schematic is intact and a
  reopen would drop `footprints_active`; restart the server at turn end when
  `session.restart_pending` is set — F1 flag), `history/service.py` wiring site (`install_history`
  or the app factory: `locate_library` + library listener), `kicad_tools/footprints/{read,specs}.py`
  (`pending` from the journal; `fp_create` spec), `agent/profiles.py`
  (`fp_create` into a new `FP_WRITES`, added to `schematic` and `free` only)
- Tests: `backend/tests/unit/footprints/{test_build,test_create_prepare,test_engine_unit,test_journal_cleanup}.py`,
  `backend/tests/kicad/footprints/{test_fp_create,test_compensation,test_server_death,test_turn_end}.py`

---

### Task 1: Types, schematic-engine guard, F1 ↔ F3 wiring

**Interfaces:** Produces FP-C8 types (`NO_SHEET`, `LibraryRowStage`, `FpStage`,
`FootprintChangeSet`, `FootprintPreImage(items: list[bytes], definition: bytes)`,
`FootprintApplied`), `DocumentInconsistent(…, leftovers: list[str] = [])`,
`make_library_locator(settings: Settings, sessions: KicadSessionManager) -> LibraryLocator`
(per `Project`, also for a project that is not open: `repo_policy(project,
settings.projects_root)` → `None` while transient; global nicknames from the current
session's `LibraryService` when it serves this project, else the stock-dir fallback of
index call 9; then `derive_local_library` → `LibraryLocation`), and one library listener: `own`/`restore` →
`invalidate_footprints(nick)`; `external` → `ReloadLibrary(nick)` + `wait_for_libraries`
**only if** `settings.footprint_tools` and `footprint_libraries_ready()`, then invalidate.

- [ ] **Step 1: Failing tests** (`test_engine_unit.py`): `apply_change_set(ctx, FootprintChangeSet(...))`
  → `TypeError`; `BudgetGate.check_budget` and `PlanGate.check_budget` accept a
  `FootprintChangeSet` with `Logical(5)`; listener with `footprint_tools=False` and
  reason `external` → no `ReloadLibrary` call, caches cleared; locator returns `None` while
  the fake repo policy is transient; `test_locator_for_project_not_open` (no session for it →
  still the derived `LibraryLocation`); `test_tools_off_no_fp_tool_in_any_profile` (all
  registered `fp_*` tools incl. `fp_create`, also in Free).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement**; wire `fp_get`/`fp_list_local`/`fp_check`:
  `pending = journal.pending(project) has lib_id`.
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `feat(footprints): write-path types and wiring`.

### Task 2: Target geometry and `fp_create.prepare`

**Interfaces:** Produces `build_target(args, lib: LocalLibrary) -> FootprintGeometry` in
`build.py` and `prepare_create(ctx, args) -> FootprintChangeSet` in `create.py`.

Build rules (spec §7.1, §7.6; author calls pinned here):
- Pad layers by type when omitted: smd `F.Cu F.Mask F.Paste`; tht `*.Cu *.Mask`; npth
  `*.Cu *.Mask`; connector `F.Cu F.Mask`.
- Courtyard: `rect`(+`center`) as given; `margin` (default 0.25) around the union bbox of
  copper and body, rounded outward to 0.01 mm; F.CrtYd, width 0.05.
- Body: F.Fab rect, width 0.10; the corner nearest pad 1 chamfered by
  `min(1.0, 0.25 × min(w, h))` when `pin1_marker` is `auto`.
- Silk `auto`: the body outline, each segment clipped so that its edge keeps ≥ 0.2 mm from
  copper; width 0.12. Pin-1 silk marker (`auto`): a segment along pad 1's outer side
  (the side facing away from the footprint centre), its edge 0.12 mm clear of copper (above
  the 0.1 mm `silk_on_pad` threshold and inside a 0.25 mm courtyard margin), length = half
  the pad's extent on that side.
- Texts: Reference on F.SilkS 1.0 mm above the courtyard top; Value on F.Fab 1.0 mm below
  the courtyard bottom; user text `${REFERENCE}` on F.Fab at the body centre, size
  `clamp(min(body)/3, 0.25, 1.0)`; other texts 1.0 × 1.0, thickness 0.15.
- Fields: `Reference = "REF**"`, `Value = name`, `Datasheet` = `fields.Datasheet` or the
  source URL, `Footprint source = "<document>, <location>[, MPN <mpn>]"`,
  `needs_user_review = "yes"`, plus up to 16 extra fields (name ≤ 64, value ≤ 1024);
  extra names in `RESERVED_FIELDS` (casefolded) → `invalid_argument`, and the tool-set
  fields are applied last anyway.
- Input length limits (schema): `description`/`keywords` ≤ 1024, `source.document` ≤ 400
  (repo path) / ≤ 2048 (URL), `source.location` 1…200, `source.mpn` ≤ 64, `model.path` ≤ 400,
  `model.offset/rotation/scale` exactly 3 numbers each.
- Model: the link if given, else none (seed model removed).

`prepare_create` order (spec §7.6, nothing written): schema → `NAME_RE` →
`activate_footprints()` (also on board servers: S2/S3 open documents anyway; adds the
`footprint_libraries_loaded` note) + seed existence check (`seed_missing`, also on board
servers) → `local_library(for_write=True)`
(`repo_root_unknown`, `library_conflict`) → name free (row present: `GetLibraryItems`;
no row: `local_names_on_disk()`; a copy that `classify_pending` would clean counts as
free; `replace=true` requires an existing local footprint and reads the API pre-image) →
`source` (relative repo path through `repo_context.paths.resolve_file` + regular file,
or `http(s)` URL; every failure → the **same** `invalid_argument` message; `location`
non-empty) → model path (`${KIPRJMOD}/` or `${KICAD<n>_3DMODEL_DIR}/` with n in 6…11,
no `..`, suffix `.step/.stp/.wrl/.wrz`; missing file → note `model_missing`) →
`build_target` → `run_checks` (any error → `check_failed` with `details.findings`) →
`Logical(changes=5)`; S1 planned when no row.

- [ ] **Step 1: Failing unit tests** (`test_build.py`, `test_create_prepare.py`):
  - `test_l201_target`: owner numbers → pads at ±1.1, CrtYd bbox `[-2.0, -2.0, 2.0, 2.0]`
    with `margin` default, Fab 3×3, body-outline silk ≥ 0.2 mm (edge) from copper, pin-1
    marker left of pad 1 and inside the courtyard, `run_checks(...)` passed with **0 warnings**.
  - `test_name_rules` (regex cases from F1), `test_replace_on_non_local` → `not_local`.
  - `test_source_rules_same_error`: absolute path, `../x`, `.env`, `.git/config`,
    `.kicad-agent/x`, missing file, empty `location` → identical `what`/`why` text and
    `code == "invalid_argument"`; `ftp://x` refused; `https://x` accepted.
  - `test_model_rules`: `/abs.step`, `${HOME}/x.step`, `${KIPRJMOD}/../x.step`, `.obj` refused;
    `${KICAD10_3DMODEL_DIR}/x.step` accepted + `model_missing` note.
  - `test_name_taken_on_disk_without_row`: no row, `X.kicad_mod` on disk → `already_exists`.
  - `test_check_error_refuses`: a pad without mask → `check_failed`, no snapshot callback.
  - `test_limits`: 513 pads / 257 silk segments / 17 fields / each string limit above + 1 →
    `invalid_argument` from the schema.
  - `test_reserved_field_names_refused`: extra fields `needs_user_review`, `User Review`,
    `footprint source`, `Reference`, `VALUE` → `invalid_argument` each; nothing is built.
  - `test_seed_missing_refused_in_prepare` (fake library without the seed, board server) →
    `seed_missing` before any write.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(footprints): fp_create prepare and target geometry`.

### Task 3: Engine S1–S3, read-back, compensation, journal

**Interfaces:** Produces `apply_footprint_change_set`, `run_library_row` (lock-free,
`assert_library_locked`), the adapter change: for a `FootprintChangeSet`,
`_two_phase_steps` runs `on_mutation` (turn snapshot, which takes the library lock itself)
**first**, then `async with library_lock(cs.library.dir): result = await spec.apply(...)`.

Stage order inside `session.applying()`: cleanup (Task 4) → S1 `ensure_library`
(`AddLibraryTableEntry(create_library=True)`; `LCS_ALREADY_EXISTS` with the same URI is OK;
read back `GetLibraryTable` + `GetLibraryStatuses == LLS_LOADED`) → **API name re-check**
(`GetLibraryItems`; taken → `already_exists`, compensate S1) → S2 `copy_seed`
(`footprint_document(SEED)` → `save_copy(lib:name)`; store `post_s2_sha256`) → S3 `rebuild`
(open the copy; one commit: delete all children except the immutable fields, create pads,
shapes, texts and fields, `update_footprint` with description/keywords/attributes/model or
no model; `push`; `save`) → read-back (`invalidate_footprints` → `footprint_geometry`;
`geometry_equal(target, readback) == []` and fields equal, else `verification_failed`).
Journal: `insert_pending` before S1 (`row_add`, `lib_id = nickname`, `file_rel` = the
project's table, repo-relative) and before S2 (`create`/`replace`, `pre_sha256` = current or
`absent`); `finalise(done, post_sha256)` after each stage's read-back. Every write appends
its file to `ctx.library_touched`. The read-back runs **after** the footprint document is closed (no `_fp_lock` held).
`KicadUnavailable` or a generation change during any stage **skips compensation**: rows
stay `pending` (with `post_s2_sha256` if S2 saved) and the error is re-raised (spec §9.3
server-death case; F4a Task 4 cleans up later). Otherwise compensation runs in reverse, shielded
(`asyncio.shield`): S3 before save → `drop`; S2 → `DeleteLibraryItem`; S1 →
`DeleteLibraryTableEntry` + `rmdir` of the `.pretty` dir if empty **and** created by this
call. Journal rows become `compensated` (with the current sha or `absent`). A failing
compensation → rows `leftover`, `DocumentInconsistent(leftovers=[lib ids / "row <nick>"])`.
Result (spec §7.6): `{created: [lib_id], file: <repo-relative path>, library_created,
`check` (fp_check on the read-back), `geometry` (summary), `needs_user_review: true`,
`logical_changes`, `budget`, `notes`}`; the suggestion text tells the agent to call
`fp_render` and present render, check and source for approval.
Test hook: `cs.fail_after ∈ {"ensure_library", "copy_seed", "rebuild_saved", "verify"}`
raises `InjectedFailure` (a `StructuredToolError` with `code="injected"`, only set by tests);
`cs.kill_after` (same stage names) kills the api-server PID after that stage;
`cs.fail_compensation = True` makes `DeleteLibraryItem` raise.

- [ ] **Step 1: Failing kicad tests** (`test_fp_create.py`, `test_compensation.py`, `repo_session`):
  - `test_l201_create_basic`: file at `<repo>/footprints/<nick>.pretty/L_Bourns_SRN3012BTA.kicad_mod`,
    project row URI `${KIPRJMOD}/../../footprints/<nick>.pretty`, result `library_created`,
    `needs_user_review: true`, `check.passed`, journal rows `row_add` + `create` are `done`.
  - `test_refusals_write_nothing`: existing name, missing source, check error → sha256 of
    the table and every library file identical before/after; no journal row.
  - `test_api_recheck_before_s2_refuses_and_rolls_back_row`: between prepare and apply,
    create the same name through the raw API (simulating the GUI) → `already_exists`, the
    row this call added is gone, the empty dir it created is gone, the foreign footprint
    is untouched.
  - `test_compensation_after_copy_seed` and `test_compensation_after_rebuild_saved`:
    footprint gone, row gone when this call created it, empty dir gone, but a dir that
    existed before is kept; error `code == "injected"` (never success).
  - `test_verification_failure_compensates` (`fail_after="verify"` → `verification_failed`).
  - `test_compensation_failure_reports_leftover`: `fail_after="rebuild_saved"` +
    `fail_compensation` → `document_inconsistent`; the error text and the turn-end
    `_recovery_notice` name the lib id; journal row `leftover`.
- [ ] **Step 2: Failing unit tests** (`test_engine_unit.py`):
  `test_kicad_unavailable_skips_compensation` (fake session raises `KicadUnavailable` in S3 →
  no `DeleteLibraryItem`, row `pending`, error re-raised);
  `test_footprint_only_inconsistency_skips_reopen` (turn outcome with `footprint_only` →
  `_recover_inconsistent` does not reopen, the notice names the leftovers);
  `test_library_lock_taken_once`: a fake apply with S1 completes (no deadlock) and
  `assert_library_locked` holds inside S1.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement** `engine.py`, `journal.py` (thin helpers
  over `FootprintWriteRepo`), the adapter change, the `_inconsistent_error` /
  `_recovery_notice` leftovers branches.
- [ ] **Step 5: Run** — PASS. **Step 6: Commit** `feat(footprints): staged write engine with compensation`.

### Task 4: Pending cleanup after a server death

**Interfaces:** Produces `classify_pending(rows, current_sha) -> list[PendingOutcome]`
(pure; `create` with `current == post_s2_sha256` → `delete`; everything else → `leftover`,
incl. `replace`/`approve`/`delete` rows, a changed file and a missing hash) and
`cleanup_pending(ctx, lib)` (runs at the start of every writing footprint `apply`, under
the library lock; deletes with `DeleteLibraryItem`, finalises rows, appends touched
files, returns notes `pending_cleaned` / `pending_leftover`).

- [ ] **Step 1: Failing unit tests** (`test_journal_cleanup.py`): the classification table
  above, row by row; `test_generation_change_irrelevant` (a row written under generation 1,
  classified under generation 3 → same outcome, only hashes count);
  `test_prepare_counts_cleanable_copy_as_free` (`fp_create`) and as `not_found` for
  `replace` (index FP-C8).
- [ ] **Step 2: Failing kicad test** (`test_server_death.py`): `kill_after="copy_seed"`
  (kills the api-server PID, pattern of `tests/kicad/test_server_kill.py`) → call fails
  with `KicadUnavailable`; journal row `pending` with `post_s2_sha256`; then `fp_get` and
  `fp_list_local` write nothing (hashes of repo files and journal rows unchanged) and show
  `unreviewed`; then `fp_create` of another name deletes the seed copy and reports
  `pending_cleaned`; then a retry with the first name succeeds.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(footprints): clean up pending writes`.

### Task 5: Turn integration — hashes, generation skip, budget, change log

**Interfaces:** `fp_create.apply` returns `ToolResult(data=…, library_changed={nick},
changed_docs=set())`; change-log entries through `editing.record_changes` with `kind` in
`footprint_created`, `footprint_replaced`, `fp_lib_table_row_added` (op `add`/`modify`),
`ref = lib_id`, `after = {"geometry_hash": …}`. `sch_changes` and the diff code treat these
kinds as non-schematic (listed, never diffed).

- [ ] **Step 1: Failing kicad tests** (`test_turn_end.py`, run engine rig of
  `tests/kicad/edit/test_plan_flow.py`):
  - `test_turn_end_after_fp_create`: turn-end autosave succeeds; the next `on_turn_start`
    reports **no** external change and pauses no plan.
  - `test_failed_create_no_external_change`: a compensated `fp_create` (S1 ran) → next turn
    start reports nothing.
  - `test_restart_after_apply_is_success`: restart the server right after `apply` returns →
    the call reports success (generation skip), the file is on disk.
  - `test_budget_weight`: four `fp_create` fit a turn; the fifth → budget refusal before
    any write; change log has four `footprint_created` rows.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(footprints): fp_create turn integration`.

### Task 6: `replace`

**Interfaces:** `replace=true`: prepare reads `FootprintPreImage` (raw protobuf bytes of
items + `FootprintInstance`) through a footprint document; S2 copies the **seed** over
the name (`save_copy` overwrites; the name was checked to be this local footprint);
the result drops `User review` and sets `needs_user_review = yes`. Compensation rebuilds the
pre-image through the S3 code path and compares `geometry_hash` + fields; any difference →
`document_inconsistent` naming the footprint (history is the backstop).

- [ ] **Step 1: Failing kicad tests** (`test_fp_create.py`): `test_replace_resets_review`
  (approve via direct API field update in the test setup, then `replace` → `needs_user_review
  == "yes"`, no `User review`, new hash); `test_replace_compensation_rebuilds_preimage`
  (`fail_after="rebuild_saved"` → same hash as before, or `document_inconsistent`);
  `test_replace_journal_pending_never_deleted` (kill after S2 of a replace → next writing
  call marks it `leftover`, file stays).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(footprints): fp_create replace`.

---

**Acceptance (F4a done when):** `fp_create` works end to end in a boardless repo project;
every refusal leaves table, library and journal unchanged; every injected failure is
compensated or reported as a named leftover; History undo of the turn removes the new
footprint (spot check with `test_history_real` style; the full matrix is F5); CI green.

**Risks:** KiCad normalising geometry on save (read-back diffs → compare within 1 µm and
fix the target builder, never relax the check); `SaveCopyOfDocument` silently overwriting
(API re-check right before S2); deadlocks (lock order from the index; the library lock is
taken once by the adapter, the snapshot takes it before); 1.7 GB per activation in CI
(module-scoped servers).

**Do NOT:** write `.kicad_mod`/`fp-lib-table` content, call `ParseAndCreateItemsFromString`,
or `rm` a library file directly (deletes go through `DeleteLibraryItem`; only `rmdir` of an
empty dir this call created); restore a snapshot automatically on failure; close the
schematic; take the library lock inside a stage; act on KiCad GitLab; request Copilot.
