# Phase E1 — Session core for editing

> Index: `../2026-10-18-editing-plan.md` (contracts E-C1, E-C2, E-C3). Spikes:
> 2A (Q3 export jobs, Q6 commits, Q8 save/copy), 2B (library loading).
> Reference code: `spikes/2a-sch-write/harness.py` (`commit`, `nets_by_pin`,
> `load_libraries`), `q06_commit.py`, `q08_save_latency.py`, `make_blank.py`.

**Goal:** the session can hold a KiCad commit safely, every CopperChat save records
hashes, export jobs and `save_copy` see unsaved edits, symbol libraries are loaded,
connectivity comes from the live netlist, and the SP2 tables exist.

**Files:**
- Modify: `backend/src/copperchat/kicad_session/session.py`, `kicad_session/__init__.py`
- Modify: `backend/src/copperchat/kicad_tools/access.py`, `kicad_tools/schematic.py`
- Modify: `backend/src/copperchat/views/kicad_render.py` (refresh jobs before sch PNG export)
- Modify: `backend/src/copperchat/history/service.py`, `history/hooks.py`
- Create: `backend/src/copperchat/db/migrations/0006_editing.sql`, `db/repos/editing_settings.py`,
  `changes/__init__.py`, `changes/repo.py`, `web/routes_editing.py`; Modify `web/app.py`
- Create: `scripts/make-blank-fixture.py`, `backend/tests/fixtures/projects/blank/`; Modify: `scripts/refresh-fixtures.sh`
- Tests: `backend/tests/kicad/edit/test_session_edit.py`, `tests/kicad/edit/conftest.py`,
  `tests/unit/test_editing_db.py`, `tests/unit/web/test_routes_editing.py`

---

### Task 1: `KicadSession.commit()` with shielded drop

**Interfaces:** Produces `KicadSession.commit(label, *, dry_run=False)`, `CommitScope`,
`jobs_stale`, `modified` (E-C1).

- [ ] **Step 1: Failing tests** (`tests/kicad/edit/test_session_edit.py`, real api-server, `ecc83_session`):
  - `test_commit_pushes_and_marks_stale`: inside `async with s.commit("t")` create a wire
    via `s.call(lambda h: h.schematic().create_items([...]))`; after exit the wire is
    returned by `get_items_by_id`; `s.jobs_stale is True`.
  - `test_commit_drops_on_exception`: raise `RuntimeError` inside the scope → full item
    dump (UUID set) equals the one before; `jobs_stale is False`.
  - `test_cancel_inside_commit_leaves_no_stale_commit`: a task that opens a commit,
    creates an item and then awaits forever is cancelled → afterwards a new
    `commit()` + create + push applies, the cancelled item never appears (2A Q6: a
    stale commit would capture it).
  - `test_dry_run_never_changes_document`: `commit(dry_run=True)` with a
    `place_symbol_from_library` → document unchanged, `is_document_modified()` False.
  - `test_commit_spans_for_sheet_writes` (`hier_session`): create a label through
    `for_sheet(<ampli_ht instance>)` inside one commit → label present on that instance
    after push.
  - `test_save_waits_for_open_commit`: `save("sch")` started while a commit is open
    completes only after the scope exits (assert ordering with an `asyncio.Event`).
- [ ] **Step 2:** run `uv run pytest tests/kicad/edit/test_session_edit.py -m kicad` → FAIL.
- [ ] **Step 3: Implement** in `session.py`: `@asynccontextmanager commit(...)` — `begin_commit`
  through `self.call`, keep the kipy `Commit`; on exit `push_commit(commit, label)` (or
  `drop_commit` when `dry_run`); on `BaseException` `await asyncio.shield(self._drop(scope))`
  and re-raise. `_drop` failing (incl. `KicadUnavailable`) → `await self.restart()` and raise
  `KicadUnavailable("could not drop an open KiCad commit; the server was restarted")`.
  `self._commit_lock = asyncio.Lock()` held for the whole scope; `save`, `save_copy`,
  `refresh_jobs` acquire it. Restart resets `jobs_stale`/`modified` to False.
- [ ] **Step 4:** tests PASS. **Step 5:** commit `feat: KicadSession.commit with shielded drop`.

### Task 2: Saves record hashes; fresh `save_copy` and export jobs

**Interfaces:** Produces `KicadSessionManager.add_save_listener`, `KicadSession.last_saved_at`,
`refresh_jobs()`; `save_copy("sch")` saves first when modified (E-C1).

- [ ] **Step 1: Failing tests**
  - unit (`tests/unit/test_kicad_session.py`, `FakeServer`): `test_save_calls_listeners_after_save`
    (listener sees the saved state; `last_saved_at` set); `test_save_listener_error_is_logged_not_raised`.
  - kicad: `test_save_copy_contains_unsaved_subsheet_edit` (`hier_session`: change R201's value
    via `for_sheet` inside `commit()`, then `save_copy("sch", tmp/…)` → the copied
    `ampli_ht.kicad_sch`, read with `kicad-cli sch export netlist`, has the new value).
  - kicad: `test_render_after_edit_shows_edit` — after a pushed commit that adds a wire,
    `render_view(session, "sch", "kicad")` differs from the render before
    (`assert_image_close(..., expect_equal=False)` or byte inequality) and
    `session.jobs_stale is False` afterwards.
  - kicad: `test_run_erc_sees_unsaved_edit` — add a dangling wire in a commit; the
    existing `checks.check_violations(session, "sch")` reports a new
    `unconnected_wire_endpoint`.
  - unit (`tests/unit/history/…`): `test_history_registers_hash_listener` — after
    `install_history`, a `session.save` updates `document_hashes`.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3: Implement**: manager keeps `_save_listeners`; sessions get a reference;
  `save()` sets `modified=False`, `last_saved_at=utcnow_iso()`, then awaits listeners
  (exceptions logged). `save_copy("sch")`: under `_commit_lock`, if
  `sch.is_document_modified()` → `save("sch")` first (spike 2A Q8: otherwise stale
  sub-sheets and a crash on new sheets). `refresh_jobs()`: if `jobs_stale` and not
  read-only → `save("sch")`, `kicad.close_document(sch.document)`,
  `open_document(sch_file, DOCTYPE_SCHEMATIC)`, `jobs_stale=False` (2A Q3,
  `q03_connect.py:150`). `views/kicad_render.export_kicad_png` calls
  `session.refresh_jobs()` before a schematic export. `HistoryService.__init__`
  registers `self._on_saved(project)` → `hashes.replace(...)`.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: save listeners, fresh save_copy and export jobs`.

### Task 3: Live netlist

**Interfaces:** Produces `PinKey`, `Netlist`, `live_netlist(h)` (E-C2).

- [ ] **Step 1: Failing tests** (kicad):
  - `test_live_netlist_matches_cli_on_hierarchy` (`hier_session`): for every pin,
    `live_netlist` net equals the net of `kicad-cli sch export netlist` on a saved copy
    (map by (sheet path, ref, pin)); ≥ 160 pins compared, 0 differences (2A Q8).
  - `test_live_netlist_sees_unsaved_wire` (`ecc83_session`): after a pushed commit that
    wires two free pins, both `PinKey`s share one net — without saving.
  - existing `tests/kicad/test_sch_tools.py` stays green after the switch (same output shape).
- [ ] **Step 2:** FAIL. **Step 3: Implement** `live_netlist` (port `harness.nets_by_pin`:
  `sch.get_netlist()`, resolve member KIIDs through each placed symbol's
  `definition.pins[].id` per sheet `path`), switch `schematic._netlist` /
  `_net_members` to it (pin name/type from the definition pins), fix the outdated
  docstring of `netlist_nodes` and drop the export-job netlist if unused.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: live schematic netlist`.

### Task 4: Symbol libraries loaded eagerly

**Interfaces:** Produces `KicadSession.ensure_symbol_libraries(timeout=120.0)`.

- [ ] **Step 1: Failing tests**: unit `test_symbol_libraries_kicked_off_after_start`
  (FakeServer records `load_all_libraries` with the symbol type only — never footprints);
  kicad `test_place_from_stock_after_ensure` (`blank_session`: `ensure_symbol_libraries()`
  then a dry-run `place_symbol_from_library("Device:R", …)` succeeds) and
  `test_ensure_is_idempotent` (second call returns immediately, < 50 ms).
- [ ] **Step 2:** FAIL. **Step 3: Implement**: after `open_document` in `_start`, fire
  `load_all_libraries(<symbol library type>)` (kipy enum as in
  `spikes/2b-libraries/q01_load.py`); `ensure_symbol_libraries` awaits
  `wait_for_libraries(<symbol type>, timeout_s=timeout)` once per server start
  (an `asyncio.Event`/future reset by restart). Footprints are **never** load-all'ed
  (owner decision 1).
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: eager symbol libraries`.

### Task 5: `blank` fixture, migration 0006, settings and change repos

**Interfaces:** Produces fixture `blank` + pytest fixture `blank_session`, helper
`tests.support.kicad_project.copy_fixture("blank", …)`; tables of E-C3;
`EditingSettingsRepo`, `EditingSettings`; `ChangeRepo`, `ChangeEntry`;
`GET|PUT /api/projects/{id}/settings/editing`.

- [ ] **Step 1: Fixture.** `scripts/make-blank-fixture.py` (runs in the kicad-base image):
  copies KiCad's template `/usr/share/kicad/template/kicad.kicad_pro` verbatim as
  `blank.kicad_pro`, creates `blank.kicad_sch` and `blank.kicad_pcb` with the API
  (`create_document` + `save`, as `spikes/2a-sch-write/make_blank.py`), and copies the
  ecc83 project library (`ecc83-pp.kicad_sym`, `sym-lib-table`) verbatim. `scripts/refresh-fixtures.sh` (which
  `rm -rf`s the fixture root) calls it at its end so a refresh keeps `blank`. Run it in the
  base image, commit the output under `backend/tests/fixtures/projects/blank/`. Test `tests/kicad/test_fixtures_open.py::test_blank_opens`
  (opens, 0 symbols, project library `ecc83-pp` listed by `get_library_statuses(PROJECT)`).
- [ ] **Step 2: Failing tests**: `tests/unit/test_editing_db.py` — migration applies on a DB
  at version 5 and on a fresh DB; `history_snapshots.kind` defaults to `turn`;
  `ux_plans_open` rejects a second open plan for one project but allows a `done` one;
  `EditingSettingsRepo.get` returns defaults `("large", 20, 100, True)`; `put` validates
  (`plan_policy` in the 3 values, `change_budget` 1–500, `annotation_block` in {100, 1000});
  `ChangeRepo.add_many` / `for_run` / `runs` round-trip. `tests/unit/web/test_routes_editing.py`
  — GET defaults, PUT partial update, 400 on invalid values, 401 without login, 404 unknown
  project.
- [ ] **Step 3:** FAIL. **Step 4: Implement** the SQL (E-C3, comments per table), repos and
  router (`routes_editing.py`, included in `create_app` before `mount_frontend`).
- [ ] **Step 5:** PASS, full backend suite + ruff + mypy. **Step 6:** commit
  `feat: editing tables, settings API and blank fixture`.

---

**Done when:** all tasks merged in one PR, CI `backend` and `kicad` jobs green, the
index's E-C1–E-C3 match the code (add "as built" bullets for any deviation).
