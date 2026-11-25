# Phase P1a — Board model, hashes and the compensated board write path

> Index contracts: B-C1 (`board_tools`), B-C2, B-C3, B-C4, B-C8 (`FabPreset`, `PRESETS`),
> B-C14 (`BOARD_KINDS`); index calls 4, 8, 10, 13. Spec §4 (C1–C6, C11, C12, C16), §6.1–§6.4, §8.3 (hash coverage), §16 (G3, G5),
> §17 P1, §18.1–§18.2. Spike 4A Q1, Q2, Q4, Q7 (`spikes/4a-board/{boards,drc,freerouting}.py`,
> port, never import). **Wave 1, parallel to P1b** — stay inside the P1a row of the index's
> file-ownership table.

**Goal:** a pure, hashable `BoardModel` read from the live board in one API pass, and a board
write engine that commits per stage, verifies by read-back, compensates by KIID diff on any
failure (also cancellation), can create a board and is undoable from History — with no
user-visible tool yet (everything behind `COPPERCHAT_BOARD_TOOLS`, default off).

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `cc/board/{__init__,model,geom,worker,presets,_selftest}.py`,
  `cc/kicad_tools/board/{__init__,read,api,engine,compensate}.py`
- Modify: `cc/config.py` (`board_tools`), `cc/kicad_session/session.py` (per-`DocKind`
  commit, `modified_docs`), `cc/history/{store,restore,service}.py` (`.kicad_dru`, `absent`
  manifest entries, restore re-discovery), `cc/history/hooks.py` (`HistoryHooks.on_project_changed`
  only, see "Index gaps" in the PR), `cc/agent/engine.py` (`RunHooks.on_project_changed`,
  `RunEngine.refresh_project`, `_make_ctx` wires `reload_project`), `cc/tools/spec.py`
  (`ToolContext.reload_project`), `cc/agent/sdk_tools.py` (`_two_phase_steps` generation
  re-baseline only, Task 7), `cc/kicad_tools/edit/engine.py` (refuse `BoardChangeSet`),
  `cc/changes/{repo,diff}.py` (`BOARD_KINDS`), `frontend/src/components/changes/highlight.ts`,
  `.env.example` (commented `COPPERCHAT_BOARD_TOOLS=off`)
- Tests: `backend/tests/unit/board/{__init__,builders,test_model,test_hashes,test_geom,test_presets,test_worker,test_engine_unit}.py`,
  `backend/tests/unit/agent/test_sdk_tools_reload.py`,
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

### Task 1b: JLCPCB fab presets (pure data)

**Interfaces:** B-C8 `FabPreset` + `PRESETS` in `cc/board/presets.py` (stdlib only, frozen;
`STACKUPS` is P2b's). Values exactly as index call 8 (mm):

| id | layers | min_track | min_space | pref_track | min_via_drill | min_via_diameter | min_drill | min_npth | min_annular | copper_to_edge | hole_to_hole | silk h / line | thickness | outer oz |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `jlcpcb-2l` | 2 | 0.127 | 0.127 | 0.15 | 0.3 | 0.66 | 0.3 | 0.5 | 0.18 | 0.3 | 0.45 | 1.0 / 0.15 | 1.6 | 1 |
| `jlcpcb-4l` | 4 | 0.127 | 0.127 | 0.15 | 0.3 | 0.6 | 0.3 | 0.5 | 0.15 | 0.3 | 0.45 | 1.0 / 0.15 | 1.6 | 1 |
| `jlcpcb-6l` | 6 | 0.127 | 0.127 | 0.15 | 0.3 | 0.6 | 0.3 | 0.5 | 0.15 | 0.3 | 0.45 | 1.0 / 0.15 | 1.6 | 1 |

`hole_to_hole` 0.45 is the stricter pad value of call 8, applied globally because KiCad has
one `hole_to_hole_min`; the 0.2 mm via value is then implied. Hole-to-edge (M14, P3a) uses
`copper_to_edge` — no extra field. P2 consumes the presets (`pcb_set_rules {preset}`), P3a's
M14 checks them.

- [ ] **Step 1: Failing unit tests** (`test_presets.py`): `test_preset_ids_and_layers`
  (keys `{"jlcpcb-2l","jlcpcb-4l","jlcpcb-6l"}`, `layers` 2/4/6, `id` equals the key);
  `test_preset_values_call_8` (the table above verbatim);
  `test_via_annular_consistent` (`(min_via_diameter − min_via_drill)/2 ≥ min_annular`
  for every preset; equality for `jlcpcb-2l`); `test_presets_frozen`
  (`FrozenInstanceError` on assignment).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): JLCPCB fab presets`.

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
file this call created. Both `absent_plan` deletion and `remove_created` work on the
**unresolved** path `project_dir / rel`: a rel that (after `os.path.normpath`) leaves the
project dir is refused; `os.lstat` of the unresolved path decides — missing → skip, a
symlink or any non-regular file → skip and `log.warning` (never followed, never deleted),
a regular file → `os.unlink` of the unresolved path (no `resolve()` before the unlink, so a
swapped-in symlink can never redirect the deletion).

- [ ] **Step 1: Failing unit tests** (`test_absent_entries.py`, `tmp_path`, fake session
  manager from `tests/support/history.py`): `test_snapshot_boardless_records_pcb_absent`
  (manifest `absent == ["p.kicad_pcb", "p.kicad_dru"]`, `files` has no pcb);
  `test_snapshot_with_dru_tracks_it` (`p.kicad_dru` in `files`, not in `absent`);
  `test_restore_deletes_exactly_absent` (after snapshot, create `p.kicad_pcb`,
  `p.kicad_dru` and an unrelated `notes.kicad_pcb.bak` → restore deletes the first two only);
  `test_absent_outside_project_refused` (`"../x.kicad_pcb"` → `CorruptSnapshot`, nothing
  deleted); `test_absent_wrong_suffix_refused` (`"p.kicad_sch"`); `test_absent_symlink_not_followed`
  (`p.kicad_pcb` is a symlink to `outside/target.kicad_pcb` at restore time → the target file
  still exists with its bytes, the symlink is skipped with a warning, and the restore still
  succeeds — the other files are copied back and the session reopens);
  `test_remove_created_skips_symlink_and_dir` (symlink → target survives; a directory named
  `p.kicad_dru` → left alone; both logged); `test_remove_created_outside_refused`
  (`"../p.kicad_pcb"` → refused, nothing deleted);
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

**Interfaces:** B-C4 `async def run_cpu(fn, *args, timeout_s: float, **kwargs) -> T`
(keyword arguments are passed through to `fn`, e.g. `solve(model, req, margin=0.1)`). Lazily
created `ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"),
initializer=_limit_worker)`; `_limit_worker` sets `RLIMIT_AS` to `WORKER_MEMORY_LIMIT_BYTES`
(3 GiB; same `_limit` pattern as `cc/views/_raster_worker.py::limit_resources`, never above
the inherited hard limit) — the measured peak RSS of the largest job (P5a's large-board
perf test) is recorded in "As built". `fn` must have `__module__` starting with
`copperchat.board.` (else `TypeError` before submit). Jobs are **serialised** by a
module-level `asyncio.Lock`: a caller waits for the lock first, and its `timeout_s` starts
only when its own job is submitted (time spent queued never counts). On timeout the pool's
process is killed (`os.kill(pid, SIGKILL)` of the executor's own processes only), the
executor is shut down and replaced, and `StructuredToolError(code="board_compute_timeout")`
is raised; a `CancelledError` kills and replaces **only while the caller's own job runs**
(a caller cancelled while still waiting for the lock just leaves the queue, the running job
of another caller is untouched), then re-raises. The lock is released after the
replacement, so the next queued job always gets a live pool. `shutdown_worker()` for app
shutdown.

- [ ] **Step 1: Failing unit tests** (`test_worker.py`; helper functions live in
  `cc/board/_selftest.py`, tiny module-level `square(x)`, `scaled(x, *, factor)`,
  `spin(seconds)`, `rlimit_as()` and `peak_rss()` (the worker's own `VmHWM` in bytes from
  `/proc/self/status`; P5a's large-board test reads it)):
  `test_runs_in_other_process` (returns `os.getpid()` ≠ own); `test_timeout_kills_and_replaces`
  (`spin(30)` with `timeout_s=0.5` → `board_compute_timeout` within 3 s, the old PID is gone
  (`/proc/<pid>` missing), next `square(3) == 9` runs in a new PID);
  `test_cancel_kills`; `test_rejects_foreign_function` (`os.getpid` → `TypeError`);
  `test_exception_propagates` (`ValueError` from the worker re-raised);
  `test_kwargs_passed_through` (`run_cpu(_selftest.scaled, 3, timeout_s=5, factor=2) == 6`);
  `test_concurrent_calls_serialised` (two `asyncio.gather`ed `spin(0.5)` calls with
  `timeout_s=0.8` each → both succeed although together they take > 0.8 s: the second waited
  and its timeout started with its own job); `test_timeout_does_not_break_queued`
  (`spin(30)` with `timeout_s=0.5` and a queued `square(4)` → the first raises
  `board_compute_timeout`, the second returns 16 in the replacement process);
  `test_cancel_while_queued_keeps_running_job` (cancel the queued caller → the running job's
  PID is still alive and its result arrives); `test_worker_rlimit_as` (a worker job returns
  `resource.getrlimit(RLIMIT_AS)[0] == WORKER_MEMORY_LIMIT_BYTES`, or the inherited hard
  limit when lower).
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

Adapter re-baseline (`cc/agent/sdk_tools.py::_two_phase_steps`, the only change there): it
keeps `session_before = ctx.session` next to `generation`; after `_apply`, when
`ctx.session is not session_before` (the call replaced the session through
`reload_project`), it sets `generation = ctx.session.generation` (the new session's value)
before `_save_edit`, so the restart check compares the new session with itself and a
restart **after** the reload is still detected.

`BoardStage.compensable` / `BoardStage.undo` (B-C4) are implemented here, not in P2: once a
stage with `compensable=False` has run, a later failure skips the KIID compensation and
raises `StructuredToolError(code="sync_failed", suggestion="Undo this turn from the History
panel; the board import cannot be reverted item by item.")` (spec §6.3, the one documented
exception); otherwise compensation runs the `undo` of every completed stage in reverse order
after the KIID diff (rules, classes, layer count and stackup are not items).

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
  `test_verify_mismatch` (`verification_failed`, compensated); `test_single_change_log_entry`;
  `test_non_compensable_stage_skips_compensation` (stage 1 `compensable=False`, stage 2
  raises → `sync_failed` naming the History panel, `compensate` spy not called);
  `test_undo_runs_reverse_order` (three stages with `undo` recorders, `fail_after=3` → undo
  calls 3, 2, 1 after `compensate`).
  `test_sdk_tools_reload.py::test_session_replaced_rebaselines` (fake old session
  `generation=2`; the fake `apply` swaps `ctx.session` for a new fake with `generation=1` →
  no `KicadUnavailable`, `_save_edit` saved on the new session);
  `test_restart_after_reload_still_detected` (the new session's generation changes during
  `_save_edit` → `KicadUnavailable`).
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
  `test_read.py`, `MANIFEST_VERSION` 3, the measured peak RSS of the CPU worker (from P1a's
  tests; P5a adds the large-board value) next to `WORKER_MEMORY_LIMIT_BYTES`. No CHANGELOG entry
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
