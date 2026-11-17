# Phase F3 — History coverage for the shared library

> Index contracts: FP-C6, FP-C7 (owner of both). Spec §9.1 (`library_changed`, re-recording,
> generation check), §9.3 (journal semantics), §10 (all), §16.1 (history, blobs, journal,
> hash re-recording, library lock, generation skip). Index author calls 1, 2, 4, 5, 7, 11, 12.
> Runs in **wave 1, parallel to F1** (and wave 2 next to F2). F3 imports nothing F1
> creates: the library location and the cache/`ReloadLibrary` reactions are injection
> points with no-op defaults, wired by F4a.

**Goal:** snapshots, restore and external-change detection cover the repo-level footprint
library and the project's `fp-lib-table`, without ever undoing another project's work, and
CopperChat's own library writes are never reported as external changes.

**Files:**
- Create: `backend/src/copperchat/db/migrations/0012_footprints.sql`,
  `backend/src/copperchat/db/repos/footprints.py`, `backend/src/copperchat/history/blobs.py`,
  `backend/src/copperchat/history/library_lock.py`, `backend/src/copperchat/history/library_restore.py`
- Modify: `history/store.py` (`tracked_files`, `LibraryLocation`, `TrackedFile`, hashing with
  stat reuse, manifest v2, blob refs, `_prune` GC, `GITIGNORE_LINES` += `history-blobs/`,
  `DocumentHashRepo` stats), `history/restore.py` (v2 + scoped library restore, narrowed
  library lock), `history/service.py` (`locate_library`, `library_listeners`,
  `on_library_changed`, `_on_saved` keeps library rows), `history/hooks.py` (library-only
  external change), `tools/spec.py` (`ToolResult.library_changed`,
  `ToolContext.library_touched`, `ToolContext.on_library_changed`), `agent/sdk_tools.py`
  (re-record in `finally`, generation-check skip), the place that builds the run's
  `ToolContext` (`grep -rn "on_documents_changed=" backend/src`) to wire `on_library_changed`
- Tests: `backend/tests/unit/history/{test_tracked_files,test_blobs,test_manifest_v2,test_library_restore,test_library_external_change}.py`,
  `backend/tests/unit/db/test_footprint_journal.py`, `backend/tests/unit/agent/test_sdk_tools_library.py`,
  existing `tests/unit/history/*` and `tests/kicad/test_history_real.py` stay green

**Not in F3:** writing journal rows from tools (F4a), invalidating `LibraryService` caches or
calling `ReloadLibrary` (F4a registers a listener), any KiCad footprint API call except the
row removal after a restore.

---

### Task 1: Migration `0012_footprints.sql` and journal repo

**Interfaces:** Produces FP-C7 tables and `FootprintWriteRepo` (`insert_pending(project_id,
run_id, lib_id, file_rel, op, pre_sha256, geometry_hash=None) -> int`, `set_post_s2(id, sha)`,
`finalise(id, state, post_sha256)`, `pending(project_id) -> list[Row]`, `since(project_id,
created_at) -> list[Row]`, `latest_for_file(file_rel) -> Row | None`,
`later_foreign(file_rel, project_id, after_id) -> bool`, `any_for_lib(project_id, lib_id) -> bool`).
`ABSENT = "absent"`.

- [ ] **Step 1: Failing tests** (`test_footprint_journal.py`, in-memory DB as in
  `tests/unit/test_editing_db.py`): migration applies on a DB at `0011`; existing
  `document_hashes` rows keep their values with `size/mtime_ns/inode IS NULL`; `op`/`state`
  CHECKs reject `"edit"`/`"done2"`; `finalise` sets `finalised_at`; `later_foreign` true only
  for a newer row of another project on the same `file_rel`; `ABSENT` round-trips.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(history): footprint tables and write journal repo`.

### Task 2: `tracked_files` and the library lock

**Interfaces:** Produces FP-C6 `LibraryLocation`, `TrackedFile`, `tracked_files`,
`library_lock`, `assert_library_locked`.

- [ ] **Step 1: Failing tests** (`test_tracked_files.py`, `tmp_path` repo with
  `hardware/main`, `hardware/io`, `footprints/X.pretty/`):
  - `test_project_files_unchanged`: `library=None` → same paths and order as
    `document_files(project)` (all `library=False`).
  - `test_table_and_library`: with a `fp-lib-table` and `A.kicad_mod`, `B.kicad_mod` → table rel
    `fp-lib-table`, library rels `../../footprints/X.pretty/A.kicad_mod` (`library=True`).
  - `test_library_dir_missing`: dir absent → no library entries, no error.
  - `test_symlinks_skipped`: a symlinked `C.kicad_mod` and a symlinked `.pretty` dir → not tracked.
  - `test_never_outside_root`: `LibraryLocation.dir` outside `repo_root` → `ValueError`.
  - `test_project_at_root`: library rel `footprints/X.pretty/A.kicad_mod` (no `../`), still
    `library=True`.
  - `test_library_lock_identity`: same resolved dir → same lock object; `assert_library_locked`
    raises `AssertionError` when the lock is free.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (`lstat`-based regular-file check; only
  `*.kicad_mod` directly in `dir`).
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `feat(history): tracked files incl. local library`.

### Task 3: Hashing with stat reuse (index call 1)

**Interfaces:** Produces `HashRow(sha256, size, mtime_ns, inode)`,
`compute_tracked_hashes(files: list[TrackedFile], previous: dict[str, HashRow]) -> dict[str, HashRow]`
(blocking; a library file whose `(size, mtime_ns, inode)` equals `previous` reuses its sha;
stat before and after hashing, stat dropped if they differ), `DocumentHashRepo.get_rows`,
`DocumentHashRepo.replace(project_id, rows: dict[str, HashRow])`,
`HistoryService.current_hashes(project) -> dict[str, HashRow]` (calls `locate_library`).
Existing `compute_hashes(project)` keeps its signature for project files only and is used
by callers that do not know the library.

- [ ] **Step 1: Failing tests**: `test_unchanged_stat_reuses` (patch `sha256_file` to count
  calls → 0 on the second run); `test_same_size_new_mtime_rehashes` (rewrite with equal size,
  `os.utime` +1 s → fresh sha); `test_inode_change_rehashes` (replace via rename);
  `test_stat_changes_during_hash_not_stored` (patch hashing to touch the file);
  `test_null_stats_rehash` (rows from before 0012); project files are always hashed (no reuse).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** Replace `compute_hashes_async(project)`
  calls in `history/hooks.py`, `history/service.py`, `history/restore.py` with
  `current_hashes` where the stored hashes are written.
- [ ] **Step 4: Run** — PASS (incl. all existing `tests/unit/history`). **Step 5: Commit**
  `feat(history): stat-keyed hash reuse for library files`.

### Task 4: Blob store, manifest v2, GC (index call 2)

**Interfaces:** Produces `BlobStore(agent_dir)` in `history/blobs.py`: `put(src: Path,
known_sha: str | None) -> tuple[str, int]` (reuse if `known_sha` blob exists; else copy to
`.tmp-<uuid>` while hashing, `fsync`, rename to `<sha>`), `path(sha) -> Path` (validates
`^[0-9a-f]{64}$`, else `CorruptSnapshot`), `gc(referenced: set[str]) -> int`
(removes unreferenced blobs and every `.tmp-*`), `blob_lock(project_id) -> asyncio.Lock`.
`HistoryStore.snapshot` writes manifest v2 (index FP-C6) and inserts `history_blob_refs`
in the same transaction as the snapshot row; `_prune` deletes refs with their snapshot
and runs `gc` only if it removed ≥ 1 snapshot or temp files exist.

- [ ] **Step 1: Failing tests** (`test_blobs.py`, `test_manifest_v2.py`):
  - `test_dedup_across_snapshots`: two snapshots with an unchanged library file → one blob,
    two ref rows.
  - `test_blob_written_before_manifest`: make the manifest write fail → the blob exists, no
    snapshot row; the next `_prune` GC removes it.
  - `test_blobs_survive_prune_and_sweep`: blobs outside `history/` are untouched by
    `_sweep_orphans`; pruning the only referencing snapshot removes the blob.
  - `test_gc_does_not_parse_manifests`: patch `json.loads` to raise inside GC → GC still works.
  - `test_tmp_files_removed`: a stale `.tmp-x` is removed by GC.
  - `test_forged_sha_refused`: manifest sha `../../x` → `CorruptSnapshot`.
  - `test_v2_shape`: `version 2`, `repoRootRel`, `library.dir/existed/files`, `table.existed`;
    `library` is `null` when the locator returns `None` (transient).
  - `test_gitignore_has_blobs`: `history-blobs/` in `GITIGNORE_LINES`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** The snapshot takes
  `library_lock(dir)` only around the library part (hash + blob put) and `blob_lock` inside it.
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `feat(history): content-addressed library blobs`.

### Task 5: Scoped library restore plan (pure)

**Interfaces:** Produces in `history/library_restore.py`:
`plan_library_restore(manifest: dict, location: LibraryLocation, project_dir: Path,
journal: list[Row], foreign_later: Callable[[str, int], bool], current: Callable[[Path], str])
-> LibraryRestorePlan(copy: list[tuple[str, Path]], delete: list[Path],
rmdir: Path | None, remove_rows: list[str], remove_table: bool, skipped: list[str], refused: str | None)`
and `validate_delete(path: Path, location: LibraryLocation, project_dir: Path, *, table: bool) -> None`.

Rules (spec §10, binding): only files named by this project's journal rows created after
the snapshot are considered; per file the **latest** such row decides: `done` →
`current == post_sha256`; `leftover`/`compensated` → `current == post_sha256` (value stored
at finalisation); `pending` → any current state; a row without any recorded value →
skipped and reported; any later row of another project on the same `file_rel` → skipped
("changed outside this project, not restored"). File in `library.files` → copy blob back;
not in it → delete. `library.dir` ≠ derived dir, or `repoRootRel` ≠ today's → `refused`
(project files still restored). v1 manifest → empty plan.

- [ ] **Step 1: Failing tests** (`test_library_restore.py`):
  - `test_v1_restore_deletes_nothing` (v1 manifest, existing `fp-lib-table` and footprints).
  - `test_deletes_own_created_after_snapshot`.
  - `test_copies_back_own_changed_and_deleted` (delete row with `post_sha256 = absent`).
  - `test_skips_file_changed_elsewhere` (current ≠ `post_sha256`).
  - `test_skips_when_foreign_row_later` (another project approved it after this create).
  - `test_pending_accepts_any_state` and `test_pending_replace_is_restored_from_blob`.
  - `test_leftover_restored_unless_changed_by_other_project` (index call 7).
  - `test_row_without_value_skipped_and_reported`.
  - `test_forged_library_dir_refused`, `test_moved_repo_refused`.
  - `test_symlink_component_never_deleted`, `test_only_kicad_mod_in_lib_dir_or_own_table`.
  - `test_never_touches_untouched_files` (a footprint with no journal row stays as is).
  - `test_remove_table_only_if_all_rows_own` (snapshot `table.existed=false`; rows supplied
    by the caller from the API) and `test_otherwise_remove_rows`.
  - `test_rmdir_only_if_created_after_snapshot_by_this_project`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (pure; `current` is injected).
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `feat(history): scoped library restore plan`.

### Task 6: Restore integration, narrowed lock (index call 4)

**Interfaces:** Consumes Tasks 2–5. `restore_snapshot` order: `svc.lock(project)` →
read the project's library rows through the API (if the session is open) → close session
→ **`library_lock`**: library plan + copy/delete/`rmdir` → **release** → copy project files
→ re-record hashes → reopen → `DeleteLibraryTableEntry` for `remove_rows` (history lock
only) → re-record the table hash → `library_listeners(reason="restore")` → notice.

- [ ] **Step 1: Failing tests** (`tests/unit/history/test_history_restore.py`, fakes for the
  session API):
  - `test_restore_notice_lists_skipped_and_unremoved_rows` (a row whose API removal fails is
    named, never silent).
  - `test_library_lock_not_held_during_reopen` (the fake `reopen` asserts the lock is free).
  - `test_library_part_skipped_when_locator_none` (transient) — project files restored.
  - `test_listeners_called_with_restore`.
  - `test_v1_snapshot_restore_unchanged_behaviour` (existing tests pass unchanged).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS
  (+ `uv run pytest -m kicad tests/kicad/test_history_real.py`).
- [ ] **Step 5: Commit** `feat(history): restore the local library scoped to the project`.

### Task 7: `library_changed`, re-recording, generation-check skip

**Interfaces:** Produces FP-C6 `ToolResult.library_changed`, `ToolContext.library_touched`,
`ToolContext.on_library_changed`, `HistoryService.on_library_changed(project, files)`
(re-records only `files`, merged into the stored rows like `_on_saved`; calls listeners with
`"own"`); `_on_saved` keeps library and table rows.

- [ ] **Step 1: Failing tests** (`test_sdk_tools_library.py`, `test_library_external_change.py`):
  - `test_success_rerecords_only_written_files`: a fake two-phase tool appends `A.kicad_mod`
    to `ctx.library_touched`; meanwhile "another project" changed `B.kicad_mod` → after the
    call the stored hash of A is current, B's is old → the next `on_turn_start` reports B only.
  - `test_failed_call_rerecords_in_finally`: the fake apply appends then raises
    `StructuredToolError` → `on_library_changed` still called (also on `CancelledError`, shielded).
  - `test_generation_skip_for_library_only`: fake session generation changes inside apply;
    result `library_changed={"X"}`, no `changed_docs` → success; with `changed_docs={"sch"}` →
    `KicadUnavailable` as today.
  - `test_saved_listener_keeps_library_rows`: `_on_saved(project, "sch")` leaves library rows.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** and wire `on_library_changed` where the
  run's `ToolContext` is built.
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `feat(history): re-record own library writes`.

### Task 8: Library-only external change

**Interfaces:** `HistoryHooks.on_turn_start`: changed rels split into project files
(incl. `fp-lib-table`) and library files. Project files changed → today's reaction
(reload, notice, `on_external_change` → plan pause). **Only** library files changed → no
reload, no `on_external_change`, a notice "Footprints changed outside this project: A, B"
(stems), `library_listeners(reason="external")`, hashes re-recorded.

- [ ] **Step 1: Failing tests** (`test_library_external_change.py`): library-only → session
  `reopen` not called, plan not paused, notice names the stems, listener got `"external"`;
  mixed → today's path; table-only → today's path; no change → nothing.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(history): library-only external changes`.

---

**Acceptance (F3 done when):** all unit tests green; existing history unit and kicad tests
unchanged; with the default locator (`None`) behaviour is identical to today except
manifest `version: 2`; CI green. The index "As built" names anything F4a must wire.

**Risks:** stat reuse hiding a change (mitigated: inode + mtime_ns + size, stat before and
after); restore partially applied when the copy-back fails midway (same as today for
project files; library deletes run after copies, so a failure leaves extra files, never
missing ones); concurrent snapshots of two projects (library lock, blob lock per project).

**Do NOT:** parse or write KiCad file content (blobs are byte copies of KiCad-written
files); delete anything from a v1 manifest; delete outside the derived library dir or the
own `fp-lib-table`; follow symlinks; hold the library lock across a KiCad restart; act on
KiCad GitLab; request Copilot.
