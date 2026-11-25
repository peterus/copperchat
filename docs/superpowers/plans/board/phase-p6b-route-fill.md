# Phase P6b — `pcb_route_fill`: Freerouting round trip, post-processing, ladder

> Index contracts: B-C1 (`freerouting` filter at both `profile_specs` call sites), B-C4
> (`snapshot="full"`, `BoardStage.undo`, `fail_after`), B-C10 (`require_placement_approved`),
> B-C12 (`postprocess`, `route`/`build_grid`), B-C13 (`find_runtime`, `run_freerouting`),
> B-C14 (`freerouting_unavailable`, `fill_not_acceptable`); index calls 9a, 11; Review Focus 1
> and 3. Spec §6.3 (SES side effects, cancellation), §7.2 `pcb_route_fill`, §8.3, §10.3 (flow,
> ladder, post-processor, layers during the fill), §10.4, §17 phase note (M2 not asserted
> here), §20 (tool-call duration). **Wave 7: starts when P5b and P6a are merged**; parallel to
> P7 (no P7 import — the locked-fanout test builds its own items).

**Goal:** the agent fills every remaining connection with Freerouting and gets back only
copper that meets the owner's bar: locked work survives, ground copper from the fill is
removed, 90° corners are chamfered, bad connections are ripped and re-routed by the own router,
a fill that needs > 20 % rip-and-reroute is refused, and any failure or turn stop restores the
full pre-fill copper.

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `cc/kicad_tools/board/{fill,specs_fill}.py`
- Modify: `cc/kicad_tools/board/specs.py` (append), `cc/agent/profiles.py` (`freerouting`
  argument + one filter line), `cc/agent/sdk_backend.py` and `cc/agent/fake.py` (pass
  `settings.freerouting` at the two `profile_specs` call sites, B-C1); `cc/agent/sdk_backend.py`
  env line only if Task 4's SDK check requires it
- Tests: `backend/tests/unit/agent/test_profiles_freerouting.py`,
  `backend/tests/unit/board/{test_fill_schema,test_fill_plan}.py`,
  `backend/tests/kicad/board/{test_fill_roundtrip,test_fill_compensation,test_fill_postprocess}.py`
  (+ append a module-scoped `fill_rig` fixture to `tests/kicad/board/conftest.py` that calls
  P5b's shared `routed_synth` — no own copy of the setup sequence)

**Test board (`fill_rig`, built once per module through the real tools via the adapter):**
`synth_session` + `board_ctx` → P5b's shared **`routed_synth(rig)`** (`tests/kicad/board/conftest.py`),
i.e. in this order: `pcb_sync_from_schematic` → `pcb_set_outline {shape: "rect", width: 60,
height: 45, corner_radius: 1, holes: {inset: 3}}` → `pcb_set_rules {layers: 4, preset:
"jlcpcb-4l", net_classes: [Power]}` (4 copper layers explicit — a preset alone keeps the layer
count, P2) → `pcb_define_blocks` + `pcb_place_block` per block → `pcb_legalize {scope: "all"}`
→ `pcb_render view=placement` + `pcb_check quick` → **placement approved through the real
`pcb_approve` flow** (turn 2, `user_says`) → `pcb_route` on `pcb_analyze`'s critical nets.
The placement checkpoint exists **before** `pcb_route` (which requires it). Board size and
order are pinned in P5b's helper; a change there is explained in that PR. Freerouting via the
`freerouting_rt` fixture (P6a: fails under `CI=true`, skips locally only). The ini default
per-test limit is 180 s (`backend/pyproject.toml`): fill tests call with `timeout_s=120` and
carry `@pytest.mark.timeout(480)`; the rig is module-scoped so the server and placement are
built once per module.

---

### Task 1: Tool surface, profile filter, refusals

**Interfaces:** `profile_specs(conversation_type, specs, *, footprint_tools=True,
board_tools=False, freerouting=True)` drops `pcb_route_fill` when `not freerouting`; both
callers pass `settings.freerouting`. Tool `pcb_route_fill` (two-phase `edit_spec`, cost
`Logical(5)`, kind `board_route_fill`), schema (`_obj`, `additionalProperties: false`):
`timeout_s` integer 30–600 (default 300), `passes` integer 1–100 (default 30, the spike's
value range 20–40), `relock_nets` array ≤ 50 of strings `maxLength 256`, `plane_layers`
array ≤ 4 of copper layer names (default: layers of existing ground-net zones that cover
≥ 80 % of the outline, else the owner-D1 default — 4 layers `["In1.Cu"]`, 6 layers
`["In1.Cu", "In4.Cu"]` (plan call), 2 layers `[]`). **prepare:** flag off →
`board_tools_disabled`; `require_board`; `find_runtime(settings) is None` →
`freerouting_unavailable` ("the app image ships it; set COPPERCHAT_FREEROUTING_HOME");
`require_placement_approved(ctx, model)`; unknown `relock_nets` / `plane_layers` → `not_found`
with `details.closest`; a ground net in `relock_nets` → `invalid_argument`; no unrouted
non-ground connection → `invalid_argument` ("nothing to fill"). It returns the
`BoardChangeSet(kind="board_route_fill", snapshot="full", board_stages=[…Task 2…])`.

- [ ] **Step 1: Failing unit tests:** `test_profiles_freerouting.py`:
  `test_freerouting_off_drops_only_fill` (board profile with `board_tools=True,
  freerouting=False` lacks `pcb_route_fill`, keeps `pcb_route`), `test_both_call_sites_pass_flag`
  (sdk_backend and fake build their tool lists with `freerouting=False` → no `pcb_route_fill`);
  `test_fill_schema.py`: `timeout_s: 601`, `passes: 0`, `passes: 101`, 51 `relock_nets`, an
  extra key → rejected; defaults 300 / 30.
- [ ] **Step 2: Failing kicad tests** (`test_fill_roundtrip.py`, `fill_rig`):
  `test_runtime_missing_refused` (settings with `freerouting_home=tmp_path` →
  `freerouting_unavailable`, nothing written, budget not charged);
  `test_no_placement_approval_refused` (fresh P3b `synth_setup` + P4 `place_synth`, no
  approval → `placement_not_approved`) and `test_stale_approval_refused` (on the routed rig,
  `pcb_move {items: [{ref: <a part with critical copper>, x: <x + 0.5>}], unroute: true}` — a
  routed part needs `unroute: true`, else `part_routed` — succeeds, then `pcb_route_fill` →
  `placement_not_approved` with `details.stale == True`; History undo restores the rig); `test_unknown_relock_net` (`not_found`,
  `closest` non-empty). All refusals happen in `prepare`, so no stage runs.
- [ ] **Step 3: Run** `uv run pytest tests/unit/agent/test_profiles_freerouting.py
  tests/unit/board/test_fill_schema.py -q` and `uv run pytest -m kicad
  tests/kicad/board/test_fill_roundtrip.py -k "refused or unknown"` — FAIL. **Step 4:
  Implement** (spec, schema and `prepare`; the spec is registered in Task 2's commit, when its
  stages exist — no placeholder stage is ever committed). **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): pcb_route_fill surface, COPPERCHAT_FREEROUTING filter`.

### Task 2: Round trip — fills, DSN, Freerouting, SES, ground deletion (with probes)

**Interfaces:** `cc/kicad_tools/board/fill.py`: `fill_stages(args, model, cls, rt) ->
list[BoardStage]` and the pure helpers `plan_fill(model, cls, plane_layers) -> FillPlan`
(ground nets, non-ground unrouted connections, plane zones) and `new_ground_items(snap,
model, ground_nets) -> list[str]` (KIIDs of tracks/arcs/vias on ground nets **not** in the
pre-import snapshot). Stages (the engine took the full snapshot first, B-C4):
1. `clear_fills` (`commit=True`, `undo` restores): the plane zones' fills are cleared, or —
   if Step 2's probe shows the API cannot clear a fill — the plane zones are removed and their
   full data kept for stage 4 and for `undo` (recreate with the stored outline, net, layers,
   priority and settings).
The scratch dir `workdir = <agent_dir>/board/fill-<uuid>/` is **owned by the change set**:
`prepare` registers it in `cs.temp_dirs` (B-C4) and stage 2 creates it as its first action
(before the export); stage 3 removes it after a successful import; on every failure or
cancel path the engine's `finally` removes it (shielded, B-C4/P1a step 6) — no stage-local
`finally` that a cancel could skip.
2. `freerouting` (`commit=False`): `api.export_specctra_dsn(h, <workdir>/board.dsn)`;
   plane layers kept out of the fill by the first mechanism that Step 2 proves (index call 9a,
   P2 recorded the layer type as not settable): (a) a Freerouting 2.5.0 option passed through
   `run_freerouting(..., extra_args=…)`, (b) **last resort, owner D4** — rewrite the plane
   layers' `(type signal)` to `(type power)` in the exported DSN text, nothing else;
   `run_freerouting(rt, dsn, ses, timeout_s=args.timeout_s, passes=…, workdir=…)` (Freerouting
   alone gets the user's `timeout_s`); `timed_out` or a missing
   `.ses` → `fill_not_acceptable` with `details.reason ∈ {"timeout", "freerouting_error"}` and
   `log_tail`.
3. `import` (`commit=False`; Step 2 records whether `ImportSpecctraSession` makes its own
   undo entry): `api.import_specctra_session(h, ses)`; on success `rmtree(workdir)`.
4. `cleanup` (`commit=True`): remove `new_ground_items` (spec §10.3 step 5); if
   `placement_hash(model) ≠` the pre-call hash → restore positions from the snapshot
   (`update_items`) and report `positions_restored`; re-assert the hash (still different →
   `verification_failed`); refill the plane zones (or recreate them, stage 1's mechanism).
Freerouting routes all connections including ground (C7); locked items go into the DSN as
protected wiring (KiCad's exporter, asserted below).

- [ ] **Step 1: Failing unit tests** (`test_fill_plan.py`, builders): `test_new_ground_items_only_new`
  (a locked GND via in the snapshot and a new GND track → only the new KIID);
  `test_plane_layers_default` (4 layers, no zones → `("In1.Cu",)`; 2 layers → `()`; a GND zone
  covering 90 % of the outline on In2.Cu → `("In2.Cu",)`).
- [ ] **Step 2: Probes + failing kicad tests** (`test_fill_roundtrip.py`):
  `test_probe_ses_import_side_effects` (before import: one unlocked non-ground track, one locked
  track; after import: `record_property` whether the unlocked one was deleted or re-created with
  a new KIID, whether footprints moved, whether an undo entry was created; asserts only that the
  locked track kept its KIID); `test_probe_zone_fill_clearable` (`record_property("zone_fill_clearable",
  …)`, decides stage 1's mechanism); `test_probe_plane_layer_mechanism` (records which of (a)/(b)
  keeps In1.Cu free); `test_dsn_layer_type_rewrite` (on a DSN exported live from the
  rig — never stored: the rewrite helper changes exactly one line, the plane layer's `type`
  token); `test_dsn_marks_locked_as_protected` (a locked track's net appears in the
  exported DSN's `wiring` with `(type protect)`); `test_fill_routes_synth` (after the call
  non-ground unrouted connections < before, `report.freerouting.seconds` recorded);
  `test_no_fill_copper_on_plane_layers` (no track with a new KIID on `In1.Cu`);
  `test_ground_copper_deleted` (no track/arc/via on `GND` with a KIID outside the pre-call
  snapshot); `test_report_fields` (`completion`, `fill_routed`, `ground_items_deleted`,
  `positions_restored`, `plane_layer_mechanism`, `freerouting {seconds, passes, timed_out}`,
  `open` ≤ 50 entries with positions, `next`); `test_budget_cost_5` (the call charges 5).
- [ ] **Step 3: Run** `uv run pytest -m kicad tests/kicad/board/test_fill_roundtrip.py` — FAIL.
  **Step 4: Implement.** **Step 5: Run** — PASS; record probe values.
- [ ] **Step 6: Commit** `feat(board): pcb_route_fill DSN/SES round trip with scoped ground deletion`.

### Task 3: Full-copper compensation, locked work, placement hash

**Interfaces:** consumes B-C4 `snapshot="full"` (`compensate` recreates missing tracks/vias
from the stored data) and stage 1's `undo`. Cancellation: `run_freerouting` kills the JVM on
`CancelledError` (P6a); the engine runs `compensate` shielded and re-raises.

Helpers in the test module: `locked_gnd_fanout(rig, n=4)` — a one-stage test
`BoardChangeSet` applied with `apply_board_change_set` that creates, for `n` SMD pads of
`GND`, a 0.6 mm track stub at 45° from the pad centre and a via (0.6/0.3 mm) at its end, both
`locked=True` (this is what P7's `pcb_fanout` will create; no P7 import), plus one GND zone on
`In1.Cu` over the outline (priority 10) refilled; `copper(model)` = sorted KIID-free tuples of
every track `(net, layer, start, end, width, locked)` and via `(net, at, diameter, drill,
locked)` rounded to 0.001 mm.

- [ ] **Step 1: Failing kicad tests** (`test_fill_compensation.py`, `fill_rig`):
  - `test_refill_keeps_locked_vias` — `locked_gnd_fanout`, record the KIIDs and positions of
    its tracks/vias, run `pcb_route_fill` **twice**; after each run every recorded KIID exists
    with the same position (0.001 mm) and `locked`, and the In1.Cu zone exists with
    `filled_area_mm2 > 0` (same KIID if fills were cleared, recorded otherwise).
  - `test_fill_keeps_placement_hash` — `placement_hash` after == before; `report.positions_restored`
    recorded (0 expected if the probe found no moves).
  - `test_fill_failure_restores_full_copper` — add one **unlocked** non-ground track first
    (test stage), take `copper(model)` and `board_hash`; run with `fail_after=3` (after the
    SES import; set on the prepared change set by wrapping the spec's `prepare`, as P2's tests
    do) → the tool error is returned, `copper(model)` equals the pre-call value (the unlocked
    track included, even if the import deleted it), zones and `board_hash` equal; parametrised
    also over `fail_after=4` and `fail_after=1`.
  - `test_no_fill_dir_left` — no `<agent_dir>/board/fill-*` dir exists after a successful
    fill, after `fail_after=1` and `fail_after=3`, and after the cancel of
    `test_fill_cancel_compensates`.
  - `test_fill_cancel_compensates` — start the tool as a task, wait until
    `<workdir>/freerouting.log` exists (the JVM runs), cancel → `CancelledError` propagates,
    the JVM PID (read from the run's log line or the process table filtered to children of
    the test process) has no `/proc` entry, `copper(model)` and `board_hash` equal pre-call,
    `session.modified_docs` holds no stale state (a following `pcb_status` works).
  - `test_rejected_fill_restores` — a post-processor patched (test-only) to rip every
    connection → `fill_not_acceptable` with `details.reason == "rip_ratio"`, copper equal
    pre-call (covers Task 4's rule path).
- [ ] **Step 2: Run** `uv run pytest -m kicad tests/kicad/board/test_fill_compensation.py` —
  FAIL. **Step 3: Implement** (whatever the probes of Task 2 require: zone undo, position
  restore). **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `test(board): fill keeps locked work and restores full copper on failure`.

### Task 4: Post-processing, rip-and-reroute, 20 % rule, ladder, SDK limit

**Interfaces:** stages 5–7 appended to `fill_stages`. **Separate post-processing deadline:**
when stage 5 starts, `post_deadline = monotonic() + POST_DEADLINE_S` (120 s, module constant,
independent of `timeout_s`); stage 5's `run_cpu(postprocess, …)` gets `timeout_s =
min(60, remaining)` and `wait_s = remaining`, its DRC task `min(60, remaining)`; stage 6
passes `deadline=post_deadline` to `route_connections` (B-C12: deadline checked before every
margin attempt, each `run_cpu` `min(30, remaining)` with `wait_s = remaining`, each DRC
`min(60, remaining)`), so ripped connections not started by then are reported in `rejected`
with reason `timeout` (their fill copper is already removed; they stay open and the ladder's
`next` names them); stage 7 always runs (cheap). The post-processing phase thus ends no later
than `post_deadline` + one DRC (≤ 60 s); the call's worst case is `timeout_s` (≤ 600 s) +
DSN export/SES import (≤ 60 s together — the budget the executor records as built on
`sp4_synth`) + `POST_DEADLINE_S` (120 s) + one DRC (≤ 60 s) ≈ **840 s** (`FILL_WORST_CASE_S
= 840`, module constant; Step 2's threshold).
5. `postprocess` (`commit=True`): `run_cpu(postprocess, jobs.strip_fills(model), frozenset(new
   non-ground KIIDs), timeout_s=min(60, remaining), wait_s=remaining)` (the zone fill
   polygons are never pickled to the worker, as for every board `run_cpu` job). **Rejection rule (spec §10.4):**
   fill connections = non-ground connections unrouted before and routed after the import;
   `len(distinct rip keys ∩ fill connections) / len(fill connections) > 0.20` →
   `fill_not_acceptable` with `details = {reason: "rip_ratio", fill_connections, rip, ratio,
   reasons: {reason: n}, worst_areas: [≤ 5 {x, y, w, h, count}]}` (10 × 10 mm cells with the most rip defects; the suggestion names `pcb_render
   {region, markers: true}` after improving channels or the critical routing) → the engine
   restores the pre-fill board. Else apply `replace` (remove old KIIDs, create new tracks),
   run the DRC task (`sctx.drc(timeout_s=min(60, remaining))`); a replacement whose new
   KIIDs appear in a non-`unconnected` marker is reverted and its connection added to the rip
   list. A `DrcTimeout` (B-C14 `drc_timeout`, P1a) here does **not** abort the call: every
   replacement of this stage is kept unverified-by-DRC, reported in `drc_skipped: true`, and
   the stage-7/final `metrics` still run (the user's next `pcb_check` re-verifies); stage 6's
   own DRC timeouts are handled per connection by `route_connections` (P5b: that connection
   fails with reason `timeout`, the others are kept).
6. `reroute` (`commit=False`; P5b's `route_connections(sctx, plans, lock=False,
   deadline=post_deadline)` commits per
   connection as `pcb_route` does): remove the fill's tracks/vias of every ripped connection, re-route each
   with the own router (vias allowed, net-class width, DRC-verified, `lock=False`); failures
   are reported as `rejected` with the reason.
7. `relock` (`commit=True`): every `relock_nets` net whose tracks pass M4/M7
   (`analyse_tracks` restricted to the net) is set `locked=True`; the others are listed in
   `relock_failed`.
Report adds `chamfered`, `merged`, `ripped`, `rerouted`, `rejected` (per connection with
reason and position), `relocked`, `relock_failed`, `completion` (non-ground routed / all
non-ground connections), `metrics` = `run_cpu(metrics_job, jobs.strip_fills(model), doc,
None, None, only=frozenset({"M4","M5","M7","M12"}), timeout_s=min(30, remaining))` summary
(never a direct `compute_metrics`; index jobs.py ownership row), and `next` per ladder rung (spec §10.3: (a) more passes, (b) `pcb_unroute
{nets: […critical signal nets next to open connections], unlock_only: true}` then
`relock_nets`, (c) `pcb_route`, (d) ask the user — never "done" with open connections).

- [ ] **Step 1: Failing kicad tests** (`test_fill_postprocess.py`, `fill_rig`):
  `test_fill_synth_m4_m7_zero` — after the fill, `pcb_metrics {only: ["M4","M7"]}` → M4 = 0
  and M7 hard = 0 (no non-octilinear, 90°, acute, off-angle, off-pad T, micro-jog, short
  chamfer);
  `test_fill_synth_complete_non_ground` — `report.completion == 1.0` and every non-ground
  connection has a `routed_length` (M2 is **not** asserted: the fill deletes its ground copper,
  spec §17; P9a asserts M2 after P7's planes); `test_chamfers_drc_clean` (DRC task: no
  clearance/track markers on KIIDs created by stage 5); `test_rip_reroute_path` (post-processor
  patched to rip 2 named connections → both re-routed by the own router, `rerouted == 2`,
  their tracks octilinear); `test_ladder_unlock_fill_relock` — route one critical net with
  `pcb_route` (locked), `pcb_unroute {nets: [n], unlock_only: true}` → its tracks unlocked
  with the same KIIDs, `pcb_route_fill {relock_nets: [n]}` → `n` in `relocked`, its tracks
  `locked` again and M4/M7 = 0 on it; `test_plane_net_never_relocked` (GND in `relock_nets` →
  `invalid_argument`); `test_post_deadline_reports_remaining_rips` (post-processor patched to
  rip 4 connections, `POST_DEADLINE_S` monkeypatched so the deadline passes after the first
  re-route (patched `monotonic`) → `rerouted == 1`, the other 3 in `rejected` with reason
  `timeout`, the call succeeds, M4/M7 = 0 on what was kept); `test_post_overrun_bounded`
  (`POST_DEADLINE_S` monkeypatched to 2.0, the post-processor and every re-route `run_cpu`
  patched to sleep their full `timeout_s` → stages 5–7 together finish within 2.0 s + 60 s,
  and every spied `run_cpu` / `drc` got `timeout_s ≤ remaining`); `test_freerouting_gets_timeout_s`
  (spy on `run_freerouting`: `timeout_s` equals the argument, not the post deadline);
  `test_postprocess_job_gets_stripped_model` (spy on `run_cpu`: the `postprocess` job's model
  argument has no zone fill polygons — `jobs.strip_fills` applied);
  `test_postprocess_drc_timeout_keeps_call` (stage 5's DRC patched to raise `DrcTimeout` →
  the call succeeds, `drc_skipped: true`, replacements kept, no `drc_timeout` error);
  `test_reroute_drc_timeout_per_connection` (stage 6's DRC patched to raise `DrcTimeout` for
  the second of 2 ripped connections → `rerouted == 1`, the second in `rejected` with reason
  `timeout`, the first's tracks kept); `test_fill_metrics_via_run_cpu` (spy: the report's
  metrics come from one `run_cpu(metrics_job, …, only=…)` call).
- [ ] **Step 2: SDK tool-call limit check** (spec §20): claude-agent-sdk 0.2.168's Python side
  has no per-tool-call timeout (only hook-matcher timeouts and the initialize timeout read from
  `CLAUDE_CODE_STREAM_CLOSE_TIMEOUT`; verified while planning in the package's `client.py` /
  `types.py`); in-process SDK MCP tools are called by the bundled CLI, which may apply its own
  MCP tool timeout (`MCP_TOOL_TIMEOUT`). The executor reads the bundled CLI's handling for
  `type: "sdk"` servers and its default, and measures it with a `live_sdk` probe
  (`tests/live/test_tool_duration_live.py`: a test-only tool that sleeps 700 s is called once;
  the result arrives or the CLI's error text is recorded; the probe sleeps 900 s when the
  700 s call succeeds, so a limit between 700 and 900 s is still seen). Outcome → "As built":
  the threshold is the call's worst case `FILL_WORST_CASE_S` = 840 s (above). If the limit
  `L` is < 840 s, set `env["MCP_TOOL_TIMEOUT"] = "900000"` in `build_options`
  (`cc/agent/sdk_backend.py`, unit test `test_mcp_tool_timeout_env`); if that does not lift it,
  cap `timeout_s`'s schema maximum at `L − 240` s (240 = export/import 60 + post 120 + DRC
  60), record `L` and the cap in "As built (P6b)" and update the index Global Constraints
  with the owner-visible reason.
- [ ] **Step 3: Run** `uv run pytest -m kicad tests/kicad/board/test_fill_postprocess.py` —
  FAIL. **Step 4: Implement.** **Step 5: Run** — PASS; record fill seconds, ripped/rerouted
  counts and the completion on `sp4_synth`.
- [ ] **Step 6: Commit** `feat(board): fill post-processing, 20 % rejection rule, relock ladder`.

### Task 5: Docs + index as built, checks, PR

- [ ] Index "As built (P6b)": probe results (SES import deletes/re-creates unlocked items? moves
  footprints? own undo entry?), zone-fill mechanism (clear vs remove/recreate), plane-layer
  mechanism ((a) option name or (b) DSN rewrite — D4 last resort, say why (a) failed),
  6-layer plane default, the SDK limit result and what was changed, fill runtime and counts on
  `sp4_synth`, `POST_DEADLINE_S`, the P5b routine name used for re-routing. CHANGELOG `[Unreleased]`: "`pcb_route_fill` (Freerouting fill +
  post-processing) — behind COPPERCHAT_BOARD_TOOLS; `COPPERCHAT_FREEROUTING=off` removes it."
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, `uv run pytest -m kicad tests/kicad/board` in the kicad-base image with
  the fetched runtime (all with `timeout`).
- [ ] PR `board phase P6b: pcb_route_fill`; review-loop per the index's Phase Conventions.

## Acceptance (P6b)

- [ ] CI `backend` and all 8 `kicad (…, i/8)` shards green; no fill test skipped in CI.
- [ ] `test_refill_keeps_locked_vias`, `test_fill_keeps_placement_hash`,
  `test_fill_failure_restores_full_copper`, `test_fill_cancel_compensates` pass (Review Focus 1).
- [ ] `test_fill_synth_m4_m7_zero`, `test_fill_synth_complete_non_ground`,
  `test_ladder_unlock_fill_relock`, `test_rejected_fill_restores` pass (Review Focus 3, §10.4).
- [ ] `COPPERCHAT_FREEROUTING=off` removes only `pcb_route_fill`; a missing runtime refuses
  `freerouting_unavailable`.
- [ ] Probe results, plane-layer mechanism and the SDK tool-call limit recorded in the index.
