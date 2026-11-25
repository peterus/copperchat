# Phase P1c — Board session, profiles, `pcb_status`, `pcb_render`, live board view

> Index contracts: B-C1 (`profile_specs`), B-C3 (consumes `board_model`, `run_drc_task`),
> B-C9, B-C14; index calls 4, 5, 9. Spec §6.5, §6.6, §7 (intro), §7.1 (`pcb_status`,
> `pcb_render`), §8.1 (step list), §8.4, §13, §16 (G8, G9). Spike 4A Q7
> (`spikes/4a-board/q07_show.py`, `boards.render`). **Wave 2: starts when P1a is merged.**

**Goal:** behind `COPPERCHAT_BOARD_TOOLS`, a user can start a Board session whose agent sees
the board tools of its profile, reads the board state cheaply (`pcb_status`) and looks at it
(`pcb_render`, top/bottom/layer/placement/3D), while KiCanvas follows board changes live.

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `cc/kicad_tools/board/{specs,status,views}.py`, `cc/views/board_render.py`,
  `cc/board/render.py`, `cc/skills/builtin/session-board/SKILL.md`,
  `frontend/src/api/features.ts`
- Modify: `cc/web/routes_conversations.py` (board gate, `features_router`), `cc/web/app.py`
  (include `features_router`, one line),
  `frontend/src/components/sessions/NewSessionDialog.tsx`, `cc/agent/{profiles,sdk_backend,fake}.py`,
  `cc/design_docs/service.py`, `cc/skills/builtin/design-docs/templates/board.md`,
  `cc/tools/registry.py`, `cc/views/{footprint_render,snapshot,_raster_worker}.py`, `cc/history/{live,hooks,service}.py`
  (`service.py`: `publish_disk_view` pcb branch only), `cc/agent/sdk_tools.py` (only if Task 7's
  test shows a second publish path)
- Tests: `backend/tests/unit/web/test_features.py`, `backend/tests/unit/web/test_conversations_board.py`,
  `backend/tests/unit/agent/test_profiles_board.py`, `backend/tests/unit/agent/test_prompt_board.py`,
  `backend/tests/unit/design_docs/test_board_policy.py`, `backend/tests/unit/board/{test_render_wrap,test_svg_limits,test_raster_limits,test_schema_bounds}.py`,
  `backend/tests/unit/history/test_live_board.py`, `frontend/src/components/sessions/NewSessionDialog.test.tsx`,
  `backend/tests/kicad/board/{test_status,test_render,test_live_board}.py`

---

### Task 1: Board session type behind the flag, `/api/features`

**Interfaces:** `create_conversation`: `body.type == "board" and not settings.board_tools`
→ `SessionTypeUnavailable("Board sessions need COPPERCHAT_BOARD_TOOLS")` (400,
`session_type_unavailable`); flag on → created like the other types. `features_router =
APIRouter(prefix="/api")`, `GET /api/features` (authenticated with `CurrentUser`) →
`{"boardTools": bool}`. Frontend: `getFeatures(): Promise<{boardTools: boolean}>` in
`api/features.ts`; `NewSessionDialog` fetches it once; the board card is
`{type: 'board', text: 'Lays out the board from the schematic step by step; asks before routing and before fab output.'}`
when `boardTools`, else the existing disabled card (also while loading or on error).

- [ ] **Step 1: Failing tests:** `test_conversations_board.py::test_board_400_when_flag_off`
  (code `session_type_unavailable`), `::test_board_201_when_flag_on`;
  `test_features.py::test_requires_auth` (401 without a session cookie),
  `::test_reports_flag` (off → `{"boardTools": false}`, on → `true`); vitest
  `NewSessionDialog.test.tsx`: `board card disabled when boardTools false`, `board card enabled
  and creates a board session when boardTools true`, `disabled when the request fails`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/web -q`, `npx vitest run src/components/sessions`
  — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): board session type behind COPPERCHAT_BOARD_TOOLS`.

### Task 2: Profiles

**Interfaces:** `profiles.py`:
`PCB_READ = frozenset({"pcb_status", "pcb_analyze", "pcb_render", "pcb_metrics", "pcb_check"})`;
`PCB_WRITES = frozenset({"pcb_sync_from_schematic", "pcb_set_outline", "pcb_set_rules",
"pcb_define_blocks", "pcb_place_block", "pcb_move", "pcb_legalize", "pcb_route",
"pcb_route_fill", "pcb_unroute", "pcb_zones", "pcb_fanout", "pcb_stitch", "pcb_tidy_silk",
"pcb_approve", "pcb_fab_export", "pcb_export_3d"})` (the 17 of spec §7.2). `PCB_READ` joins
`requirements`, `schematic`, `board`; `board` += `PCB_WRITES | PLAN_TOOLS | FP_WRITES |
{"doc_patch_section"}`; `SCH_WRITES ∩ board == ∅`; Free gets everything as today.
`profile_specs(conversation_type, specs, *, footprint_tools=True, board_tools=False,
freerouting=True)`: additionally drops `s.name in PCB_WRITES` when `not board_tools`
(`freerouting` is accepted and unused until P6b adds its line). `sdk_backend.py` and
`fake.py` pass `board_tools=settings.board_tools`; `Settings.freerouting` only exists from
P6a, so P6b adds `freerouting=settings.freerouting` at both call sites (index B-C1 note).

- [ ] **Step 1: Failing unit tests** (`test_profiles_board.py`, fake specs named after every
  `PCB_READ`/`PCB_WRITES` tool + `pcb_list_footprints` + `sch_connect`):
  `test_flag_off_removes_writes_everywhere` (each of the 4 types, Free included: no
  `PCB_WRITES` name); `test_flag_off_keeps_reads` (`pcb_status`, `pcb_render`,
  `pcb_list_footprints` in requirements, schematic, board, free);
  `test_flag_on_board_profile` (all 17 writes, `plan_propose`, `fp_create`,
  `doc_patch_section` present; `sch_connect` absent); `test_flag_on_schematic_has_no_pcb_writes`;
  `test_pcb_writes_is_the_spec_list` (17 names, all start with `pcb_`, disjoint from
  `PCB_READ`); `test_every_board_write_has_a_kind`: every name in `PCB_WRITES -
  {"pcb_fab_export", "pcb_export_3d"}` maps to `"board_" + name.removeprefix("pcb_")` ∈
  `BOARD_KINDS`, and every **registered** two-phase spec (`prepare is not None`) whose name
  is in `PCB_WRITES` maps into `BOARD_KINDS` (vacuous until P2 registers writes; it then
  guards every later phase).
- [ ] **Step 2: Run** `uv run pytest tests/unit/agent -q` — FAIL. **Step 3: Implement.**
  **Step 4: Run** — PASS (existing profile tests unchanged).
- [ ] **Step 5: Commit** `feat(board): PCB_READ/PCB_WRITES profiles and board_tools filter`.

### Task 3: System prompt, `board.md` v2, board write policy

**Interfaces:** `system_prompt(project_name, conversation_type="free", *,
footprint_tools=True, board_tools=False)`. Flag off: `_EDITING` and the board paragraph are
byte-identical to today. Flag on: `_EDITING` reads "…The board (PCB) is edited with the pcb_*
tools in a Board session; load the session-board skill before the first board write." and
the board paragraph becomes: "- This is a board session. The schematic is read-only here;
you edit the board with the pcb_* tools. Load the session-board skill first and follow its
steps: stop after each step and wait for the user's ok. Record answers and decisions in
board.md with doc_patch_section and in decisions.md with doc_append." Template
`board.md` = spec §8.4 verbatim (sections Summary, Mechanical, Stack-up and rules,
Placement, Part classes, Routing, Checks, Open points with the HTML comments).
`WritePolicy("board")`: `patch` of any section of `board.md` (incl. `create=true` when
missing), `append` to `board.md` and `decisions.md`; `write` refused; any other doc refused.

- [ ] **Step 1: Failing unit tests:** `test_prompt_board.py::test_flag_off_unchanged`
  (equals the pre-P1c strings, kept as literals in the test), `::test_flag_on_mentions_skill`
  (contains `session-board`, no "read-only: if asked to change it"); `test_board_policy.py`:
  `test_patch_any_board_section`, `test_patch_creates_missing_board_md`,
  `test_append_board_and_decisions`, `test_write_refused`, `test_patch_other_doc_refused`
  (e.g. `requirements.md`); `test_template_v2_sections` (the 8 `##` headings in order).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): board prompt, board.md v2 and write policy`.

### Task 4: Provider and `pcb_status`

**Interfaces:** `cc/kicad_tools/board/specs.py`: `TOOL_SPECS: list[ToolSpec]` (later phases
append), provider `"copperchat.kicad_tools.board.specs"` appended to `_PROVIDERS`.
`pcb_status({})` (read tool, never takes the edit lock, schema `_obj({})`):
no board → `{"exists": false, "suggestion": "run pcb_sync_from_schematic"}` (no error);
else `{"exists": true, "outline": {kind, x, y, w, h, corner_radius} | null,
"copper_layers": n, "footprints": n, "placed": n, "unplaced": n, "unplaced_refs": [≤ 50] +
"unplaced_omitted", "connections": n, "routed": n, "unrouted": n, "zones": n,
"placement_hash": …, "board_hash": …}`. `connections` = Σ over nets with ≥ 2 pads of
(pads − 1); `unrouted` = `run_drc_task().unconnected`; `routed = connections − unrouted`
(floored at 0). Checkpoint and last-check fields are added by P3b.

- [ ] **Step 1: Failing kicad tests** (`test_status.py`): `test_status_boardless`
  (`blank_nopcb_session` → `exists: false`, no exception); `test_status_ecc83`
  (footprints = model part count, hashes 64 hex, `unrouted` equals `run_drc_task`'s count,
  outline not null); `test_status_hash_changes_after_move` (one footprint moved through a
  board commit → `placement_hash` differs); unit `test_status_registered` (registry lists
  `pcb_status` and it is in every profile with the flag off).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): pcb_status`.

### Task 5: SVG limits, board wrapper and `pcb_render`

**Interfaces:** `footprint_render.py`: `@dataclass(frozen=True) class SvgLimits:
max_bytes, max_elements, max_coords, max_element_coords, max_span` (B-C9);
`FOOTPRINT_SVG_LIMITS` = today's constants; `load_kicad_svg(data, *,
limits=FOOTPRINT_SVG_LIMITS)`; `rasterise(svg, extent_mm, *, max_side_px=MAX_SIDE_PX,
min_px_per_mm=MIN_PX_PER_MM, timeout_s=RASTER_TIMEOUT_S, memory_bytes=None)`. `rasterise`
also passes the worker's **CPU and memory limits** as two extra argv elements
(`<width_px> <height_px> <cpu_s> <memory_bytes>`, integers, validated like the first two):
CPU = `ceil(timeout_s)` (hard limit + 2 s, as today), memory = `memory_bytes` or the
worker's `MEMORY_LIMIT_BYTES`. `cc/views/_raster_worker.py::main` accepts 2 or 4 argv items
(2 → today's constants, so the footprint path is byte-identical) and `limit_resources(cpu_s,
memory_bytes)` never raises a limit above today's file-size cap or the inherited hard limit.
The board path calls `rasterise(max_side_px=1600, timeout_s=BOARD_RASTER_TIMEOUT_S,
memory_bytes=BOARD_RASTER_MEMORY_BYTES)`; the board memory cap is set in Step 4 from the
measured peak RSS of the worker (≥ 2× the peak, ≤ 3 GiB). `cc/board/render.py` (pure): `simplify_paths(root, tol_mm=0.02)`
(Ramer–Douglas–Peucker on `M`/`L`-only path data with > 200 points; curves untouched) and B-C9
`wrap_svg(...)`: scale bar (1/2/5/10 mm step chosen so it spans 10–25 % of the width),
origin cross, `"<w> × <h> mm"` label, block hulls (convex hull of the member courtyards of
each `blk:<name>` group, dashed, name label), markers (numbered circles, id = report index),
highlight overlay (the nets' tracks/pads from the model as thick strokes); every injected
string through `xml.sax.saxutils.escape`. `cc/views/board_render.py`: B-C9 `render_board`:
`session.save_copy("pcb", agent_dir/"renders"/"tmp"/<uuid>.kicad_pcb)` → `kicad-cli pcb export
svg --mode-single --page-size-mode 2 --exclude-drawing-sheet --layers <list>` (bottom:
`--mirror`) → `load_kicad_svg(limits=BOARD_SVG_LIMITS)` → `simplify_paths` → `wrap_svg` →
`rasterise(max_side_px=1600)`; `3d`: `kicad-cli pcb render --width 1600 --height 1200
--quality basic` with `--side top|bottom` or `--rotate -45,0,45 --perspective` (`iso`).
View layers (spec §13): top = `F.Cu,F.Mask,F.SilkS,Edge.Cuts`; bottom =
`B.Cu,B.Mask,B.SilkS,Edge.Cuts` mirrored; layer = `<layer>,Edge.Cuts`; placement =
`F.CrtYd,B.CrtYd,F.Fab,F.SilkS,Edge.Cuts` + hulls. Files land in `agent_dir(project)/
"renders"` with a uuid name; the tmp board copy is deleted. `pcb_render` schema: `view`
enum, `layer` (maxLength 32, must be an enabled copper layer → `not_found` with
`details.closest`), `region {x,y,w,h}` (|x|,|y| ≤ 1000, 0 < w,h ≤ 1000), `highlight_nets`
(≤ 20, maxLength 256, unknown → `not_found` + closest), `markers: bool` (true → run the DRC
task and mark its markers), `angle` enum; result: 1 PNG image, `data = {view, svg_url,
png_url, placement_hash, board_hash, markers: [...]}`; failures → `render_failed`.
`BOARD_SVG_LIMITS` per B-C9, then tuned in Step 3.

- [ ] **Step 1: Failing unit tests:** `test_svg_limits.py`: `test_footprint_limits_unchanged`
  (`FOOTPRINT_SVG_LIMITS` equals the old constants; the existing footprint render tests pass
  unchanged), `test_board_limits_accept_large` (synthetic 60 k-element SVG passes board limits,
  fails footprint limits); `test_render_wrap.py`: `test_escapes_injected_text` (net
  `<script>&"` appears only escaped), `test_scale_bar_step`, `test_hull_per_group`
  (`blk:power` with 3 parts → one hull polygon labelled `power`), `test_simplify_keeps_shape`
  (a 1000-point circle → < 200 points, max deviation ≤ 0.02 mm), `test_region_viewbox`;
  `test_raster_limits.py`: `test_rasterise_passes_cpu_and_memory` (spy on
  `create_subprocess_exec`: `timeout_s=7.2, memory_bytes=2**30` → argv ends with `"8"`,
  `str(2**30)`), `test_worker_two_args_uses_defaults` and `test_worker_four_args_sets_limits`
  (call `_raster_worker.limit_resources` in a `spawn` subprocess, read
  `resource.getrlimit(RLIMIT_CPU/RLIMIT_AS)` back), `test_worker_rejects_bad_limits`
  (non-integer or ≤ 0 → non-zero exit).
- [ ] **Step 2: Failing kicad tests** (`test_render.py`, module-scoped `ecc83` server):
  `test_render_each_view` (top, bottom, layer `B.Cu`, placement, 3d iso: PNG long side ≤ 1600,
  hashes equal `pcb_status`'s); `test_render_unknown_layer` (`In5.Cu` → `not_found` with
  `closest`); `test_render_highlight_and_markers`; `test_render_boardless` (`no_board`);
  `test_render_zone_filled` (add a GND zone over the whole outline on F.Cu and B.Cu through the
  API, `refill_zones`, render top → passes `BOARD_SVG_LIMITS`; record bytes, elements,
  coords, max element coords, span, raster seconds and the raster worker's peak RSS —
  `ru_maxrss` of `resource.getrusage(RUSAGE_CHILDREN)` delta — with `record_property`).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement**; set `BOARD_SVG_LIMITS` to ≥ 4× the
  largest measured value of each field (ecc83 and the zone-filled board) and the board raster
  timeout to ≥ 3× the measured seconds, capped at 60 s, and `BOARD_RASTER_MEMORY_BYTES` to
  ≥ 2× the measured peak RSS (≤ 3 GiB); record all three in "As built (P1c)".
  **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): pcb_render with board SVG limits`.

### Task 5b: Every board schema is bounded

**Interfaces:** none new — a guard test over the registry (plan call 6, Global Constraints).

- [ ] **Step 1: Failing unit test** (`test_schema_bounds.py::test_every_board_schema_bounded`):
  iterate every registered `ToolSpec` whose name starts with `pcb_` (from
  `cc/tools/registry.py`, the real providers — so specs registered by later phases are
  covered automatically) and walk its `input_schema` recursively (incl. `items`,
  `properties`, `anyOf`/`oneOf`): every `array` has `maxItems`; every `string` has
  `maxLength` or `enum` or `pattern`; every `number`/`integer` has `minimum` (or
  `exclusiveMinimum`) and `maximum` (or `exclusiveMaximum`); every `object` has
  `additionalProperties: false`. A failure names the tool and the JSON path. In P1c it
  covers `pcb_status` and `pcb_render`.
- [ ] **Step 2: Run** — FAIL (until both schemas comply). **Step 3: Fix the schemas.**
  **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `test(board): every pcb_* schema is bounded`.

### Task 6: `session-board` skill skeleton

**Interfaces:** `cc/skills/builtin/session-board/SKILL.md` with frontmatter (`name:
session-board`, description "Use in a Board session: drives the step-by-step board flow
S0–S9…"), a "Rules" list (stop the turn and wait for the user's ok after every step;
**hard checkpoints** S4 "Approve this placement?" and S8 "Approve this board for
manufacturing?" — never call `pcb_approve` without the user's own words; never edit the
schematic; every step ends with images, a short check summary, decisions, a question),
and one `##` heading per step S0 Intake … S9 Fab with the "What the agent does" and "Ends
with" lines of spec §8.1; body text "Full content: P9b." per step.

- [ ] **Step 1: Failing unit test** (existing builtin-skill tests pick it up; add
  `test_session_board_skeleton`: 10 step headings in order, both hard checkpoint sentences
  present, the stop-and-wait rule present).
- [ ] **Step 2: Run** — FAIL. **Step 3: Write.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): session-board skill skeleton`.

### Task 7: Live board view (G8)

**Interfaces:** `LiveViewPublisher.schedule(project, docs: set[DocKind])` (pending docs
accumulate in `_State.docs`); `_publish` publishes `pending & docs_of(project)` in one
`publish_view(..., live=True)` (pcb via `write_view` → `save_copy("pcb")`, which waits for
`applying()`, so a half-applied board is never copied). `HistoryHooks.on_documents_changed`:
`self.svc.live.schedule(self.project, docs & {"sch", "pcb"})`. The only caller is the
adapter after `apply` (once per tool call, `sdk_tools.py` success path and the
`save_failed` path); the board engine never calls it per stage (asserted below).
`snapshot.copy_disk_board(project, dest)` (byte copy of the board file on disk);
`publish_disk_view` gets a pcb branch (catch-up when a live pcb frame went out).

- [ ] **Step 1: Failing unit tests** (`test_live_board.py`, fake clock/session as the existing
  live tests): `test_pcb_change_schedules_pcb`; `test_sch_and_pcb_one_frame` (both pending →
  one `view-updated` with `docs ["pcb","sch"]`); `test_boardless_pcb_ignored`;
  `test_disk_catch_up_includes_pcb`; `test_board_tool_publishes_once`: a 3-stage
  `BoardChangeSet` (fake stages) run through `sdk_tools`' two-phase handler →
  `on_documents_changed` called exactly once, with `{"pcb"}`, after `apply` returned.
- [ ] **Step 2: Failing kicad test** (`test_live_board.py`, `ecc83_session` + real
  `HistoryService`): a board commit + `on_documents_changed({"pcb"})` → after the debounce one
  live frame whose pcb view file differs from the previous one.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): live board view`.

### Task 8: Docs + index as built, checks, PR

- [ ] Index "As built (P1c)": measured board SVG limits, raster timeout and raster memory cap
  (peak RSS), `freerouting=` passed from P6b (B-C1 note). CHANGELOG `[Unreleased]`: "Board sessions with
  `pcb_status` / `pcb_render` and a live board view — behind COPPERCHAT_BOARD_TOOLS."
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`; `npm run lint`, `npx vitest run`, `npm run typecheck`; `uv run pytest -m
  kicad tests/kicad/board` in the kicad-base image (with `timeout`).
- [ ] PR `board phase P1c: board session, status, render, live view`; review-loop per the
  index's Phase Conventions.

## Acceptance (P1c)

- [ ] CI `backend`, `frontend`, all 8 `kicad (…, i/8)` shards green.
- [ ] Flag off: `POST type=board` → 400, `/api/features` → `boardTools: false`, no
  `PCB_WRITES` name in any profile, `pcb_status`/`pcb_render` available; prompt unchanged.
- [ ] Flag on: board card enabled; board profile = reads + 17 writes + plan + `FP_WRITES` +
  `doc_patch_section`, no `SCH_WRITES`.
- [ ] `pcb_render` produces all five views on ecc83 with PNG ≤ 1600 px; the zone-filled board
  passes `BOARD_SVG_LIMITS`; measurements recorded.
- [ ] `test_board_tool_publishes_once` and the kicad live-frame test pass.
- [ ] `test_every_board_schema_bounded` passes (and keeps guarding every later phase).
