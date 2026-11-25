# Phase P1a — Board model, hashes and the compensated board write path

> Index contracts: B-C1 (`board_tools`), B-C2, B-C3, B-C4, B-C14 (`BOARD_KINDS`); index calls
> 4, 10, 13. Spec §4 (C1–C6, C11, C12, C16), §6.1–§6.4, §8.3 (hash coverage), §16 (G3, G5),
> §17 P1, §18.1–§18.2. Spike 4A Q1, Q2, Q4, Q7 (`spikes/4a-board/{boards,drc,freerouting}.py`,
> port, never import). **Wave 1, parallel to P1b** — stay inside the P1a row of the index's
> file-ownership table.

**Goal:** a pure, hashable `BoardModel` read from the live board in one API pass, and a board
write engine that commits per stage, verifies by read-back, compensates by KIID diff on any
failure (also cancellation), can create a board and is undoable from History — with no
user-visible tool yet (everything behind `COPPERCHAT_BOARD_TOOLS`, default off).

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `cc/board/{__init__,model,geom,worker,_selftest}.py`,
  `cc/kicad_tools/board/{__init__,read,api,engine,compensate}.py`
- Modify: `cc/config.py` (`board_tools`), `cc/kicad_session/session.py` (per-`DocKind`
  commit, `modified_docs`), `cc/history/{store,restore,service}.py` (`.kicad_dru`, `absent`
  manifest entries, restore re-discovery), `cc/history/hooks.py` (`HistoryHooks.on_project_changed`
  only, see "Index gaps" in the PR), `cc/agent/engine.py` (`RunHooks.on_project_changed`,
  `RunEngine.refresh_project`, `_make_ctx` wires `reload_project`), `cc/tools/spec.py`
  (`ToolContext.reload_project`), `cc/kicad_tools/edit/engine.py` (refuse `BoardChangeSet`),
  `cc/changes/{repo,diff}.py` (`BOARD_KINDS`), `frontend/src/components/changes/highlight.ts`,
  `.env.example` (commented `COPPERCHAT_BOARD_TOOLS=off`)
- Tests: `backend/tests/unit/board/{__init__,builders,test_model,test_hashes,test_geom,test_worker,test_engine_unit}.py`,
  `backend/tests/unit/history/test_absent_entries.py`, `backend/tests/unit/test_config.py`,
  `backend/tests/unit/test_session_doc_commit.py`, `backend/tests/unit/changes/test_board_kinds.py`,
  `frontend/src/components/changes/highlight.test.ts`,
  `backend/tests/kicad/board/{__init__,conftest,test_read,test_api,test_engine,test_history_board,test_session_pcb_commit}.py`

**Not in P1a:** any `pcb_*` ToolSpec, profiles, prompt, render, live view (P1c); netlist
import/sync, rules persistence probe (P2).

---

### Task 1: `BoardModel`, `placement_hash`, `board_hash`, geometry basics

**Interfaces:** B-C2 exactly (`Point`, `Side`, `Rect`, `Pad` (layers incl. paste/mask), `Part` (+ `rule_areas`), `Zone` (+ `filled`, excluded from `board_hash`), `Track`, `ArcTrack`, `Via`,
`Outline`, `NetClassInfo`, `TextItem(kind, text, layer, at, size, thickness,
visible)`, `GraphicItem(layer, kind, points, width)`, `BoardModel.part`, `.placed`,
`placement_hash(m) -> str`, `board_hash(m) -> str`); `cc/board/geom.py`: `octi_dist`,
`polygon_area`, `point_in_polygon`, `polygon_inside`, `polygons_overlap`, `convex_hull`,
`rect_poly(x, y, w, h, r=0.0)` (rounded corners as 8 segments per quarter). Canonical JSON
= `json.dumps(obj, sort_keys=True, separators=(",", ":"))` of rounded tuples; lists sorted
by a KIID-free key (parts by ref; tracks by `(net, layer, start, end, width)`; vias by
`(net, at)`; zones by `(name, net, layers, outline)`), so KIIDs never enter either hash.
`tests/unit/board/builders.py`: `board(**overrides)`, `part(ref, x, y, ...)`,
`track(...)`, `via(...)` — plain-data builders reused by every later phase.

- [ ] **Step 1: Failing unit tests** (`test_hashes.py`, builders only):
  `placement_hash` **changes** on: `test_ph_move_001` (x + 0.01 mm), `test_ph_rotate`
  (rot + 90), `test_ph_flip_side`, `test_ph_lock_flag`, `test_ph_outline_change` (w + 0.01),
  `test_ph_rule_area_change` (keepout polygon moved 0.01 mm; keepout set `{"tracks"}` →
  `{"tracks","vias"}`); **unchanged** on `test_ph_track_add`, `test_ph_move_below_rounding`
  (x + 0.004 mm), `test_ph_rot_360_equiv` (0 vs 360). `board_hash` **changes** on:
  `test_bh_silk_text_move` (ref text at + 0.01), `test_bh_track_add`, `test_bh_via_add`,
  `test_bh_zone_settings` (clearance 0.2 → 0.3), `test_bh_rule_change`
  (`min_clearance` 0.127 → 0.15), `test_bh_net_class_change` (track width), 
  `test_bh_net_class_assignment`, `test_bh_dru_text` (one added rule line);
  **unchanged** on `test_bh_zone_fill_only` (`filled_area_mm2` differs),
  `test_bh_dru_newlines` (`\r\n` vs `\n`). `test_hashes_kiid_independent`: the same board
  with every KIID replaced → both hashes equal. `test_hash_is_sha256_hex` (64 hex chars).
- [ ] **Step 2: Failing unit tests** (`test_model.py`, `test_geom.py`): `part("X")` raises
  `KeyError`; `placed` False for courtyard crossing the outline by 0.01 mm, True fully
  inside, False for `outline.kind == "none"`; `octi_dist((0,0),(3,4)) == 4 + 3*(√2−1)`;
  `polygon_area(rect_poly(0,0,10,5)) == 50`; `rect_poly(..., r=1)` area
  `== 50 − (4 − π)` within 0.05; `point_in_polygon` on edge/inside/outside;
  `polygon_inside` and `polygons_overlap` on convex and on an L-shaped (concave) pair;
  `convex_hull` of a square plus its centre returns the 4 corners.
- [ ] **Step 3: Run** `uv run pytest tests/unit/board -q` — FAIL. **Step 4: Implement**
  (stdlib only, frozen dataclasses, no kipy import — `test_model.py::test_no_kipy_import`
  asserts `"kipy" not in sys.modules` after importing `copperchat.board.model`).
  **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): BoardModel, placement/board hashes, geometry basics`.

### Task 2: Flag and change-log kinds

**Interfaces:** `Settings.board_tools` (B-C1). `cc/changes/repo.py`:
`BOARD_KINDS = frozenset({"board_sync_from_schematic", "board_set_outline", "board_set_rules",
"board_define_blocks", "board_place_block", "board_move", "board_legalize", "board_route",
"board_route_fill", "board_unroute", "board_zones", "board_fanout", "board_stitch",
"board_tidy_silk", "board_approve"})` (index call 4/10: `board_` + name without `pcb_` for the
14 document-changing `PCB_WRITES` tools, plus `board_approve`). `cc/changes/diff.py` skips
`BOARD_KINDS` exactly where it skips `FOOTPRINT_KINDS`. `highlight.ts`: exported
`BOARD_KINDS` (same 15 strings) and `isSchematicEntry` excludes them.

- [ ] **Step 1: Failing tests:** `test_config.py::test_board_tools_env` (unset → `False`,
  `COPPERCHAT_BOARD_TOOLS=on` → `True`); `test_board_kinds.py::test_board_entries_not_in_sch_diff`
  (a `board_move` entry with `sheet_path=""` is not counted by `diff.py`);
  `test_board_kinds_disjoint_from_footprint_kinds`; vitest `highlight.test.ts`:
  `isSchematicEntry({kind: 'board_move', sheetPath: '/'})` is `false`, and the TS set equals
  the backend list (the test reads a 15-item literal; a comment names `cc/changes/repo.py`).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_config.py tests/unit/changes -q` and
  `npx vitest run src/components/changes` — FAIL. **Step 3: Implement**; add the commented
  line to `.env.example` next to `COPPERCHAT_FOOTPRINT_TOOLS`. **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): COPPERCHAT_BOARD_TOOLS flag and BOARD_KINDS`.

### Task 3: Per-document commit and modified state

**Interfaces:** `KicadSession.commit(label, *, doc: DocKind = "sch", dry_run=False)` begins
`h.schematic()` or `h.board()` `.begin_commit()`, pushes/drops on the same document;
`self.modified_docs: set[DocKind]` (a pushed commit adds `doc`; `_save_locked(doc)` removes
`doc`; start/restart clears it); `modified` becomes a read-only property
`"sch" in self.modified_docs` (existing readers `history/hooks.py` keep working);
`jobs_stale` is set only for `doc == "sch"`. A commit on `"pcb"` with no board open raises
`DocumentNotOpen` before taking the commit lock.

- [ ] **Step 1: Failing unit tests** (`test_session_doc_commit.py`, `tests/support/fake_kicad.py`):
  `test_pcb_commit_marks_pcb_only` (`modified_docs == {"pcb"}`, `modified is False`,
  `jobs_stale` unchanged); `test_save_pcb_clears_pcb`; `test_sch_commit_unchanged`
  (old behaviour: `modified` True, `jobs_stale` True); `test_pcb_commit_without_board`
  → `DocumentNotOpen`; `test_drop_on_error_pcb` (exception inside → board `drop_commit`
  called, `modified_docs` unchanged).
- [ ] **Step 2: Failing kicad test** (`test_session_pcb_commit.py`, `ecc83_session`): inside
  `commit("t", doc="pcb")` move one footprint by 1 mm through `update_items`; after the scope
  `modified_docs == {"pcb"}`; `save("pcb")` → `modified_docs == set()` and the saved file
  holds the new position (checked after `ecc83_session`'s manager reopens the project:
  `board().get_footprints()` position of that reference).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(session): per-document commits and modified state`.

### Task 4: Board loader and raw-protobuf API (G3, G5) + DRC-in-open-commit probe

**Interfaces:** B-C3 exactly. `load_board_model` reads in one `session.call`: footprints
(ref, KIID, `definition.id`, value, position, orientation, layer → side, locked,
`symbol_sheet_name`, `symbol_path` joined with `/`, courtyard from the footprint's
`F/B.CrtYd` shapes transformed to board coordinates — the polygon hull of those shapes,
`()` if none, texts, footprint rule areas → `rule_areas`), pads (net, size, shape, layers incl.
`F/B.Paste` / `F/B.Mask`, drill), zone filled polygons → `Zone.filled`, tracks/arcs/vias, zones (rule
areas incl. keepout flags; `filled_area_mm2` from filled polygons), Edge.Cuts graphics →
`Outline` (`"rect"` when exactly one `BoardRectangle`, `"other"` for any other closed
shape set, `"none"` if no Edge.Cuts item), enabled copper layers, design rules (name →
mm), net classes + effective class per net (`get_netclass_for_nets`, project via
`board_project`), groups (name → member refs), `dru_text` from
`<project>.kicad_dru` (UTF-8, `""` if absent), stackup `(layer, thickness/material)` pairs.
`run_drc_task`: `StartDrcTask(report_all_track_errors=True)` → poll `GetTaskStatus` every
0.05 s until not `TS_RUNNING` or `timeout_s` → `GetDrcTaskResult`; `unconnected` = number of
markers whose type name contains `UNCONNECTED`; a non-`TS_COMPLETED` state raises
`StructuredToolError(code="drc_rejected")` with the task's error text.
`board_project` appends the trailing `/` (G3). `require_board` raises `no_board` with
suggestion "run pcb_sync_from_schematic".

- [ ] **Step 1: Failing kicad tests** (`test_read.py`, one module-scoped `ecc83` server):
  `test_load_ecc83` — part count equals `len(board.get_footprints())` and the number of
  `(footprint` blocks of a `save_document_to_string` copy, `tracks` non-empty (the demo
  board is hand-routed), `outline.kind != "none"`; every part has a KIID, a `lib_id` and a non-empty
  `symbol_path`; the counts are pinned as constants after the first green run; `test_hashes_stable_across_reads` (two loads → equal
  hashes); `test_move_changes_placement_hash` (move one footprint 0.01 mm inside a commit,
  reload); `test_dru_text_read` (write a custom rule through `SetCustomDesignRules`, save →
  `dru_text` contains the rule name).
- [ ] **Step 2: Failing kicad tests** (`test_api.py`, `ecc83_session`):
  `test_drc_task_on_ecc83` (`seconds < 5`, markers carry KIIDs, `unconnected` equals
  `kicad-cli pcb drc --format json` unconnected count of a `save_copy`);
  `test_save_document_to_string` (starts with `(kicad_pcb`); `test_specctra_export_live`
  (`RunBoardJobExportSpecctraDsn` → file starts with `(pcb`);
  `test_import_specctra_session_missing_file` (a missing `.ses` path → `ApiError` carrying
  KiCad's message, board unchanged; the real DSN/SES round trip needs Freerouting and is
  P6b's); `test_board_project_path_fix` (`get_net_classes()` works through
  `board_project`, fails without the fix); `test_group_name` (create a `Group` with
  `set_group_name(g.proto, "blk:power")`, read back the name);
  `test_create_board_document` (`blank_nopcb_session`: `create_board_document(h, path)` →
  file exists and starts with `(kicad_pcb`).
- [ ] **Step 3: Probe** (`test_api.py::test_probe_drc_sees_open_commit`): inside an open
  board commit create a track that crosses a pad of another net, run `run_drc_task` before
  the push; `record_property("drc_sees_open_commit", bool)`; the test asserts only that the
  DRC call succeeds. Record the value in "As built (P1a)" — **no code may depend on it**
  (spec §6.2).
- [ ] **Step 4: Run** `uv run pytest -m kicad tests/kicad/board -q` (kicad-base image) — FAIL.
  **Step 5: Implement.** **Step 6: Run** — PASS.
- [ ] **Step 7: Commit** `feat(board): board model loader and raw protobuf API wrappers`.

### Task 5: History — `.kicad_dru`, absent entries, restore re-discovery

**Interfaces:** `document_files(project)` appends `<stem>.kicad_dru` when it is a regular
file. `HistoryStore.snapshot` writes manifest key `absent: list[str]` = the project-relative
POSIX names of `pro_file.with_suffix(".kicad_pcb")` and `pro_file.with_suffix(".kicad_dru")`
that do not exist (or are symlinks) at snapshot time; `MANIFEST_VERSION = 3`; readers treat
a missing `absent` (versions 1–2) as `[]`. `restore.absent_plan(manifest, project_dir) ->
list[Path]`: only the two allowed suffixes, only entries that resolve inside `project_dir`
(`_inside`), never a symlink; anything else → `CorruptSnapshot`. `restore_snapshot` deletes
them after `_copy_back` (`unlink(missing_ok=True)`), then `_reload` re-discovers:
`fresh = get_project(settings.projects_root, project.id)` and passes `fresh` to
`sessions.reopen`, `publish_view` and `docs_of`, and calls
`svc.runtime.get(project.id).refresh_project(fresh)` when an engine exists.
`RunHooks.on_project_changed(project) -> None` (default no-op);
`HistoryHooks.on_project_changed` sets `self.project`; `RunEngine.refresh_project(project)`
sets `self.project` and calls the hook. A shared helper `remove_created(project_dir,
rels) -> None` (same checks as `absent_plan`) is what both restore and the engine's
board-creation compensation (Task 7) call — the only non-restore file deletion, scoped to a
file this call created.

- [ ] **Step 1: Failing unit tests** (`test_absent_entries.py`, `tmp_path`, fake session
  manager from `tests/support/history.py`): `test_snapshot_boardless_records_pcb_absent`
  (manifest `absent == ["p.kicad_pcb", "p.kicad_dru"]`, `files` has no pcb);
  `test_snapshot_with_dru_tracks_it` (`p.kicad_dru` in `files`, not in `absent`);
  `test_restore_deletes_exactly_absent` (after snapshot, create `p.kicad_pcb`,
  `p.kicad_dru` and an unrelated `notes.kicad_pcb.bak` → restore deletes the first two only);
  `test_absent_outside_project_refused` (`"../x.kicad_pcb"` → `CorruptSnapshot`, nothing
  deleted); `test_absent_wrong_suffix_refused` (`"p.kicad_sch"`); `test_absent_symlink_not_followed`;
  `test_v2_manifest_without_absent` (restores, deletes nothing);
  `test_restore_rediscovers_before_reopen`: undo of a created board (snapshot boardless,
  then the board file appears and the engine's project has `pcb_file` set) → the fake
  `reopen` receives a `Project` with `pcb_file is None`, `docs_of(fresh) == {"sch"}`, and
  the fake engine's `refresh_project` got the same `fresh`.
- [ ] **Step 2: Failing kicad test** (`test_history_board.py`, `blank_nopcb_session` + real
  `HistoryService` as in `tests/kicad/edit/test_checks_real.py::history`): snapshot →
  `create_board_document` + `SetCustomDesignRules` (one rule) + `save("pcb")` → restore →
  no `.kicad_pcb`, no `.kicad_dru`, the reopened session's `docs_of == {"sch"}` and it
  answers `project_info`.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(history): .kicad_dru, absent manifest entries, restore re-discovery`.

### Task 6: CPU worker

**Interfaces:** B-C4 `run_cpu(fn, *args, timeout_s)`. Lazily created
`ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"))`; `fn` must have
`__module__` starting with `copperchat.board.` (else `TypeError` before submit); on timeout
the pool's process is killed (`os.kill(pid, SIGKILL)` of the executor's own processes
only), the executor is shut down and replaced, and
`StructuredToolError(code="board_compute_timeout")` is raised; a `CancelledError` while
waiting kills and replaces too, then re-raises. `shutdown_worker()` for app shutdown.

- [ ] **Step 1: Failing unit tests** (`test_worker.py`; helper functions live in
  `cc/board/_selftest.py`, a tiny module-level `square(x)` and `spin(seconds)`):
  `test_runs_in_other_process` (returns `os.getpid()` ≠ own); `test_timeout_kills_and_replaces`
  (`spin(30)` with `timeout_s=0.5` → `board_compute_timeout` within 3 s, the old PID is gone
  (`/proc/<pid>` missing), next `square(3) == 9` runs in a new PID);
  `test_cancel_kills`; `test_rejects_foreign_function` (`os.getpid` → `TypeError`);
  `test_exception_propagates` (`ValueError` from the worker re-raised).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): one-process CPU worker with timeout kill`.

### Task 7: `BoardChangeSet` engine, compensation, board creation

**Interfaces:** B-C4 (`BOARD_SHEET`, `BoardStage`, `BoardStageContext`, `BoardChangeSet`,
`apply_board_change_set`) and `compensate.py` (`CopperSnapshot`, `take_snapshot`,
`compensate`). `ToolContext.reload_project: Callable[[], Awaitable[tuple[Project,
KicadSession]]] | None` — wired in `RunEngine._make_ctx`: re-discover
(`get_project`), `sessions.reopen(fresh)`, `refresh_project(fresh)`, return both; the engine
assigns `ctx.project, ctx.session` in place (the adapter's later `_save_edit` then saves on
the new session). `apply_change_set` raises `TypeError` for a `BoardChangeSet`.

Algorithm of `apply_board_change_set`:
1. `creates_board` and `ctx.project.pcb_file is None`: `create_board_document` (inside
   `session.call`), then `reload_project`; failure of any later step removes the created
   file with `remove_created` (Task 5) and reloads again (`docs_of == {"sch"}` afterwards).
2. `async with ctx.session.applying()`: `take_snapshot(cs.snapshot)`; for each stage:
   `commit=True` → `async with session.commit(f"copperchat: {cs.tool}", doc="pcb")`, else
   plain; after stage *n* raise `RuntimeError("fail_after")` when `cs.fail_after == n`.
3. After the last stage: `verify(model, report)`; non-empty → compensate, raise
   `StructuredToolError(code="verification_failed")` listing the mismatches.
4. Any exception after the first stage **started** (incl. `CancelledError`; also `commit=False`
   stages that push their own commits, e.g. `pcb_route`): run
   `compensate` under `_uncancellable`; leftovers non-empty or compensation raised →
   `DocumentInconsistent(leftovers=...)`; else re-raise the original.
5. Success: `ToolResult(data=cs.report | {"step_report": ...}, changed_docs={"pcb"})`, one
   change-log entry (`kind=cs.kind`, `sheet_path=""`, `after` = counts) via `record_applied`.

`compensate`: removes tracks/arcs/vias/zones/groups/graphics/footprints whose KIID is not in
the snapshot (`remove_items` in one board commit), restores `(x, y, rot, side, locked)` of
moved footprints (`update_items`, `flip_items` for a side change), and in `"full"` mode
recreates missing tracks/vias from the stored data; returns what still differs.

- [ ] **Step 1: Failing unit tests** (`test_engine_unit.py`, fake session):
  `test_apply_change_set_refuses_board_cs` (`TypeError`); `test_fail_after_compensates`
  (fake stages, compensation called once, original error re-raised);
  `test_compensation_failure_is_document_inconsistent`; `test_cancel_compensates_shielded`
  (cancel during stage 2 → compensation completes, `CancelledError` propagates);
  `test_verify_mismatch` (`verification_failed`, compensated); `test_single_change_log_entry`.
- [ ] **Step 2: Failing kicad tests** (`test_engine.py`, `ecc83_session`, a 3-stage test
  change set built in the test: stage 1 adds a track, stage 2 adds a via and moves a footprint
  2 mm, stage 3 adds a zone): `test_fail_after_2_restores_hashes` (`fail_after=2` →
  `placement_hash` and `board_hash` equal the pre-call values, KIID sets equal);
  `test_fail_after_3`; `test_cancel_during_stage_2` (stage 2 awaits an event; the task is
  cancelled → hashes unchanged); `test_full_snapshot_recreates_deleted_track` (stage 1 deletes an
  existing track, stage 2 fails, `snapshot="full"` → `board_hash` equal);
  `test_success_changed_docs` (`{"pcb"}`, `modified_docs == {"pcb"}`); on
  `blank_nopcb_session` with `board_ctx(session, db)` — a new helper in
  `tests/kicad/board/conftest.py` that later phases reuse: `tests/support/edit.edit_ctx` plus
  a real `HistoryService`, `on_mutation` taking the turn snapshot and `reload_project` wired to
  `get_project` + `KicadSessionManager.reopen` as `RunEngine._make_ctx` does:
  `test_creates_board_then_reopens`
  (`creates_board=True`, one empty stage → `.kicad_pcb` exists, `ctx.project.pcb_file` set,
  `ctx.session` is the new open session with `"pcb"` open) and
  `test_creates_board_failure_removes_it` (`fail_after=1` → no `.kicad_pcb`,
  `docs_of(ctx.project) == {"sch"}`; record whether KiCad rewrote `.kicad_pro` bytes during
  the creation — "As built"; a changed `.kicad_pro` is restored by History, not by the engine).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): BoardChangeSet engine with stage commits and KIID compensation`.

### Task 8: Docs + index as built, checks, PR

- [ ] Index "As built (P1a)": api-server RSS with the ecc83 board open (spec §20: measured in
  P1 and P9; read `VmRSS` of the api-server PID in a kicad test and record it), DRC-in-open-commit probe result, ecc83 counts pinned in
  `test_read.py`, `MANIFEST_VERSION` 3, the `reload_project` return type `(Project,
  KicadSession)` (B-C4/index call 13 deviation, update the contract text in place),
  `remove_created` as the one file deletion outside restore (update Global Constraints line
  "File operations on KiCad data"), `verification_failed` added to B-C14. No CHANGELOG entry
  (nothing user-visible; flag off).
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`; `npm run lint`, `npx vitest run`, `npm run typecheck`; `uv run pytest -m
  kicad tests/kicad/board` in the kicad-base image (all with `timeout`).
- [ ] PR `board phase P1a: board model and compensated write path`; review-loop per the
  index's Phase Conventions.

## Acceptance (P1a)

- [ ] CI jobs `backend`, `frontend` and all 8 `kicad (…, i/8)` shards green.
- [ ] `test_hashes.py` pins every coverage case of Task 1 (incl. KIID independence).
- [ ] `test_fail_after_2_restores_hashes`, `test_cancel_during_stage_2`,
  `test_creates_board_failure_removes_it` pass on the real server.
- [ ] `test_restore_rediscovers_before_reopen` and `test_history_board.py` show
  `docs_of == {"sch"}` after undoing a created board.
- [ ] `test_timeout_kills_and_replaces` passes; no `pcb_*` tool is registered yet.
- [ ] DRC-in-open-commit probe result recorded in the index.
