# Phase P5b — `pcb_route` and `pcb_unroute`

> Index contracts: B-C12 (consumes `solve`, `solve_pair`, `pair_coupling`, `prune_model`,
> `RouteTooLarge`; produces `route_connections`), B-C4 (stages,
> compensation, `run_cpu`), B-C10 (`require_placement_approved`), B-C6/B-C7 (critical set,
> `connections`, `routed_length`), B-C14 (`placement_not_approved`, `drc_rejected`,
> `not_found`). P4's `unroute_ops`. Spec §7.2 "Routing", §8.3 (placement checkpoint), §10.1,
> §10.2 (verification, locking, pairs), §10.3 ladder step (b), §15.2 calls 23, 24, 67.
> Spike 4A Q4c. **Wave 6: starts when P4 and P5a are merged.**

**Goal:** after the user approved the placement, the agent routes the critical set with the own
router — every connection DRC-verified before it is kept, locked, pairs coupled — and can
remove or unlock copper selectively, with every call compensated on failure.

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `cc/kicad_tools/board/{route,specs_route}.py`
- Modify: `cc/kicad_tools/board/specs.py` (append two specs),
  `backend/tests/kicad/board/conftest.py` (append the shared rig helpers below)
- Tests: `backend/tests/unit/board/{test_route_schemas,test_route_plan}.py`,
  `backend/tests/kicad/board/{test_route,test_unroute}.py`

**Shared rig helpers** (`tests/kicad/board/conftest.py`; **created here, owned by P5b**;
P6b, P7, P8 and P9a reuse them and only append new helpers — never a second copy of this
sequence). All calls go through the real two-phase adapter, in this order:
- `async placed_synth(rig) -> dict` — (1) `pcb_sync_from_schematic`; (2) `pcb_set_outline
  {shape: "rect", width: 60, height: 45, corner_radius: 1, holes: {inset: 3}}`; (3)
  `pcb_set_rules {layers: 4, preset: "jlcpcb-4l", net_classes: [{name: "Power", track_width:
  0.4, clearance: 0.2, via_diameter: 0.6, via_drill: 0.3, patterns: ["*VBUS", "*+3V3",
  "*SW"]}]}` (**`layers: 4` explicit** — a preset alone never changes the layer count, P2);
  (4) P4's `place_synth` (`pcb_define_blocks` + `pcb_place_block` per block); (5)
  `pcb_legalize {scope: "all"}`; (6) turn 1 `pcb_render view=placement` + `pcb_check quick`
  (asserts `m1_pass`); (7) turn 2 `user_says("placement looks good, go ahead")` +
  `pcb_approve {stage: "placement", hash, user_quote: "placement looks good"}` — the
  **placement checkpoint is written before any routing**. Returns `{placement_hash,
  critical_nets}`.
- `async routed_synth(rig) -> dict` — `placed_synth(rig)` + (8) `pcb_route {nets:
  critical_nets}` in chunks of ≤ 50 (`lock: true`). Returns `placed_synth`'s dict +
  `{routed: [...], board_hash}`.
- Module-scoped fixtures `synth_placed` (= `placed_synth` on P3b's `synth_session` rig) and
  `synth_routed` (= `routed_synth`). Tests that change the board run on a function-scoped copy
  where isolation needs it (a fresh server costs ≈ 1.9 GB; reuse the module server and undo
  through History between tests where possible, recorded in "As built").

---

### Task 1: Connection planning (pure)

**Interfaces:** `route.py`: `@dataclass(frozen=True) ConnPlan(net: str, a: Pad, b: Pad,
req: RouteRequest, pair: str | None)`; `plan_connections(model, cls, nets, *, layers:
tuple[str, ...] | None, allow_vias: bool) -> list[ConnPlan]` — per net the unrouted
connections (`metrics.connections` where `routed_length is None`), sorted by `octi_dist`
ascending, nets in request order; widths/clearances/via from the net's class
(`net_class_of` → `NetClassInfo`; `width_from` has the single value `"netclass"`, default);
layers = the request's or all enabled copper layers, ordered: power classes (nets of kind
`supply`/`switch_node`/`high_current`) outer layers first, others in board order (index
B-C12: first layer preferred); a net that is half of a `cls.pairs` pair whose partner is also
requested gets `pair = <partner>` and is planned once for both. `layer_order(model, kind) ->
tuple[str, ...]`.

- [ ] **Step 1: Failing unit tests** (`test_route_plan.py`, builders): `test_skips_routed`
  (a connection with an existing track path is not planned); `test_class_widths`;
  `test_power_outer_first` (4 layers → `F.Cu`, `B.Cu`, `In1.Cu`, `In2.Cu` for a supply net);
  `test_pair_planned_once`; `test_shortest_first`. `test_route_schemas.py`: `nets` 1..50,
  `maxLength 256`, `layers` ≤ 32, `width_from` enum `["netclass"]`, `pcb_unroute` needs
  `nets` or `region`, `region` w/h > 0, `additionalProperties: false`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/board/test_route_plan.py
  tests/unit/board/test_route_schemas.py -q` — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): pcb_route connection planning`.

### Task 2: `pcb_route`

**Interfaces:** `prepare_route(ctx, args) -> BoardChangeSet`, registered with
`edit_spec("pcb_route", …)`. The per-connection loop below is the reusable routine
`async def route_connections(sctx: BoardStageContext, plans: Sequence[ConnPlan], *, lock: bool)
-> RouteOutcome(routed: tuple[str, ...], failed: tuple[tuple[str, str], ...])` — full
signature `route_connections(sctx, plans, *, lock: bool, deadline: float | None = None)`
(B-C12; `deadline` is a `time.monotonic()` value: a connection not started before it is
reported failed with reason `timeout` and not attempted) — in
`cc/kicad_tools/board/route.py` (connection key `"{net}:{refA}.{padA}-{refB}.{padB}"`, failure
reason); `pcb_route`'s stages call it per net and P6b's rip-and-reroute and ladder rung (c)
call it too (index B-C12). Schema `{nets: [1..50], layers?: [..32], width_from?:
"netclass", lock?: bool = true, allow_vias?: bool = true}`. Prepare: `require_board`;
`board_model`; **`require_placement_approved(ctx, model)`** first (spec §8.3); unknown nets
→ `not_found` + `details.closest`; ground nets → `invalid_argument` ("ground connects through
planes and pcb_fanout", spec §10.1); unknown/disabled layers → `not_found`; nets with no
unrouted connection are reported as `already_routed`. **Total deadline:** the call sets
`deadline = monotonic() + ROUTE_DEADLINE_S` (300 s, module constant) at the start of apply
and passes it to every `route_connections` call; nets whose connections were not started by
then are reported with status `failed`, reason `timeout` (the routed ones stay — the call
succeeds with a partial report). `route_connections` checks the deadline **before every
margin attempt** (not only per connection) and bounds each step by `remaining = deadline −
monotonic()`: `run_cpu(..., timeout_s=min(30, remaining), wait_s=remaining)` and
`sctx.drc(timeout_s=min(60, remaining))`; `remaining ≤ 0` before an attempt → the connection
is failed `timeout`. **Worst case:** the call ends no later than deadline + one DRC (≤ 60 s)
— the one DRC/reject commit already in flight when the deadline passes. Cost `Logical(ceil(n/5))`, kind
`board_route`, `snapshot="ids"` (the call only adds and removes its own items). One
`BoardStage` per requested net (`commit=False`; the stage opens its own commits), stage run:
for each `ConnPlan` of the net (pairs: `solve_pair(pruned, p, n, gap, margin=m)`, then
`pair_coupling(pp, pn)` reported as `{length_mismatch, gap_min, gap_max}`):
1. `path = await run_cpu(solve, prune_model(model, req), req, margin=m, timeout_s=min(30,
   remaining), wait_s=remaining)` (`timeout_s=30`, `wait_s=None` when `deadline is None`)
   (B-C12 pruned model; keyword passed through per B-C4) with `m` = 0.0, then 0.05, then
   0.10 (`MARGINS`); `None` → next margin, after the last → failed `no_path`;
   `RouteTooLarge` → failed `too_large` (no retry); `board_compute_timeout` → failed
   `timeout`;
2. one commit (`ctx.session.commit("copperchat: pcb_route", doc="pcb")`) creating the tracks
   (`req.width`, layer, net) and vias (`req.via`), `locked = args.lock`; their KIIDs go into
   `sctx.created`;
3. after the push, `sctx.drc(timeout_s=min(60, remaining))` (60 without a deadline); markers (except `unconnected_items`) whose KIIDs intersect
   the new items → reject: remove the new items in one commit, retry with the next margin;
   after the last margin → failed `drc_rejected: <first marker text>`;
4. accepted → the in-memory model gets the new copper (re-read with `sctx.model()` once per
   net) so later connections see it.
Never relies on DRC seeing an open commit (P1a probe). B-C4 compensates after the first stage
**started** (also `commit=False` stages that push their own commits), so a failure in a later
net removes the earlier nets' pushed copper; `test_route_fail_after_1` pins it. Verify
(read-back): every accepted
item exists with its `locked` flag; no item of this call is left from a rejected attempt.
Step report: per net `{net, status: routed|partial|failed|already_routed, connections,
routed, failed: [{a: "U1.3", b: "C1.1", reason}], length_mm, vias, locked, coupling?}`,
totals, `analyse_bends` over the new items (`M4`, `M7` counts — expected 0), DRC errors
after the call, `next` (`pcb_render` with `highlight_nets`, then `pcb_route_fill`).

- [ ] **Step 0: Failing unit tests** (`test_route_plan.py`, fake `BoardStageContext` and a
  fake `run_cpu`): `test_route_connections_deadline` (monkeypatched `monotonic`: the clock
  passes the deadline after the first connection → the other connections are failed
  `timeout`, never solved); `test_route_deadline_none_routes_all`; `test_too_large_reported`
  (fake `run_cpu` raises `RouteTooLarge` → failed `too_large`, no further margin tried);
  `test_solve_gets_pruned_model` (the fake `run_cpu` receives a model without the far-away
  parts); `test_pcb_route_total_deadline` (`ROUTE_DEADLINE_S` monkeypatched to 0.0 → every
  net `failed`/`timeout`, nothing created, the call succeeds);
  `test_deadline_checked_per_margin` (fake `run_cpu` returns `None` for margin 0.0 and the
  clock passes the deadline during it → margins 0.05/0.10 never tried, failed `timeout`);
  `test_steps_bounded_by_remaining` (deadline 10 s ahead on a fake clock advanced by each
  fake call: every `run_cpu` got `timeout_s ≤ min(30, remaining)` and `wait_s == remaining`,
  every `drc` got `timeout_s ≤ min(60, remaining)`); `test_overrun_bounded` (real clock,
  `ROUTE_DEADLINE_S = 1.0`, fake `run_cpu` / `drc` sleep their full `timeout_s` → the call
  returns within 1.0 s + 0.5 s slack, i.e. far below deadline + 60 s).
- [ ] **Step 1: Failing kicad tests** (`test_route.py`, `synth_placed`):
  `test_route_critical_set` — `pcb_analyze.critical_nets` (ground excluded by P3a) routed in
  chunks of ≤ 50 → every planned connection `routed`; DRC task: 0 errors other than
  unconnected items; `pcb_metrics {only: ["M4","M7"]}` → both value 0 and `pass`; every
  track/via created by the calls has `locked == True`; `placement_hash` unchanged (the
  approval stays valid); `record_property` per-net seconds and totals;
  `test_pair_routed_coupled` (the `USB_DP`/`USB_DN` pair: same layer, `coupling` reported
  with keys `length_mismatch`, `gap_min`, `gap_max`, M12 mismatch ≤ 1 mm or listed); `test_lock_false`; `test_no_vias_when_disallowed`
  (`allow_vias: false` → no via created);
  `test_placement_not_approved_refusal` (fresh `synth_setup` + `place_synth`, no approval →
  `placement_not_approved`; after an approval, a 0.5 mm board commit move → refused with
  `details.stale`); `test_unknown_net_closest`; `test_ground_net_refused`;
  `test_already_routed_reported`; `test_budget_cost` (6 nets → 2);
  compensation: `test_route_fail_after_1` (2 nets, `fail_after=1` → `board_hash` equals the
  pre-call value, no KIID of the call left), `test_route_cancel_compensates` (cancel while
  the second net's `run_cpu` waits → hashes unchanged, `CancelledError` propagates),
  `test_drc_reject_then_retry` (`route.run_cpu` is wrapped in the test so its first answer is
  a hand-made octilinear path crossing a pad of another net, later calls go to the real
  worker — CopperChat code is patched, KiCad is not → the first attempt is rejected by the
  real DRC task, its items removed, the retry with margin 0.05 is accepted, no leftover item).
- [ ] **Step 2: Run** `uv run pytest -m kicad tests/kicad/board/test_route.py` (kicad-base
  image) — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): pcb_route with per-connection DRC verification and locking`.

### Task 3: `pcb_unroute` (incl. `unlock_only`)

**Interfaces:** `prepare_unroute(ctx, args) -> BoardChangeSet`, `edit_spec("pcb_unroute",
…)`. Schema `{nets?: [..50], region?: {x,y,w,h}, include_locked?: bool = false,
unlock_only?: bool = false}` (one of `nets`/`region` required; unknown nets → `not_found`).
Cost `Logical(1)`, kind `board_unroute`. Plane nets = nets of kind `ground` (P3a) plus every
net carrying a copper zone. Selection = P4's `unroute_ops.select_copper(model, nets, region,
include_locked)`.
- **delete** (default): one stage = `unroute_stage(selection)` (`snapshot="full"` when
  `include_locked`, so compensation recreates locked copper); `cs.unrouted_by_call` = the
  selected nets; report `{removed_tracks, removed_vias, nets, skipped_locked}`.
- **`unlock_only: true`** (spec §10.3 ladder step (b), call 67): selection with
  `include_locked=True`, then keep only locked items; items of plane nets are dropped unless
  that net is named in `nets` (so a `region`-only call never unlocks fanout stubs or stitching
  vias); one stage (one commit) clearing `locked` via `update_items` in `route.py`
  (`unlock_stage(kiids) -> BoardStage`; `unroute_ops` stays P4's); verify: those items now
  unlocked, nothing deleted (KIID set unchanged); report `{unlocked, unlocked_nets: [...],
  skipped_plane_items}` — `unlocked_nets` is what the agent passes to `pcb_route_fill
  {relock_nets}` (P6b).

- [ ] **Step 1: Failing kicad tests** (`test_unroute.py`, `synth_placed` after routing two
  critical nets with `lock: false` for one and `lock: true` for the other, plus a **locked**
  GND stub + via created through `apply_board_change_set` stages in the test):
  `test_unroute_net_deletes_unlocked` (the unlocked net's copper gone, the locked one kept and
  reported as `skipped_locked`); `test_unroute_include_locked`; `test_unroute_region_whole_connection`
  (a region touching one segment removes the whole connection; DRC task has no
  `track_dangling` marker); `test_unlock_only_returns_nets` (locked signal net → `locked`
  false, KIIDs unchanged, `unlocked_nets == [<net>]`, `board_hash` changed only by the lock
  flags); `test_unlock_only_skips_plane_net_in_region` (region covering the signal route and
  the GND stub/via → signal unlocked, GND items still locked, `skipped_plane_items == 2`);
  `test_unlock_only_plane_net_when_named` (`nets: ["GND"]` → GND items unlocked);
  `test_unroute_fail_after_1_restores_locked` (`include_locked: true`, `fail_after=1` →
  `board_hash` equals pre-call, locked flags restored); `test_unroute_budget_cost_1`;
  `test_unroute_needs_selection` (`{}` → `invalid_argument`).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): pcb_unroute with unlock_only`.

### Task 4: Docs + index as built, checks, PR

- [ ] Index "As built (P5b)": retry margins, layer order per kind, `ROUTE_DEADLINE_S`, the
  shared `placed_synth` / `routed_synth` rig helpers, the per-net stage layout
  (stages open their own commits), measured routing time of the synth critical set (per net,
  total), connections routed / failed on synth, how the kicad tests share the server.
  CHANGELOG `[Unreleased]`: "Own octilinear router (`pcb_route`, `pcb_unroute`) — behind
  COPPERCHAT_BOARD_TOOLS."
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, `uv run pytest -m kicad tests/kicad/board` in the kicad-base image (with
  `timeout`); `test_every_board_write_has_a_kind` (P1c) covers both new tools.
- [ ] PR `board phase P5b: pcb_route and pcb_unroute`; review-loop per the index's Phase
  Conventions.

## Acceptance (P5b)

- [ ] CI `backend` and all 8 `kicad (…, i/8)` shards green.
- [ ] `test_route_critical_set`: critical set of `sp4_synth` routed DRC-clean, M4 = M7 = 0,
  every new item locked, placement approval still valid.
- [ ] `test_placement_not_approved_refusal` passes (no approval and stale approval).
- [ ] `test_route_fail_after_1` and `test_route_cancel_compensates` leave the board unchanged.
- [ ] `test_unlock_only_skips_plane_net_in_region` and `test_unlock_only_returns_nets` pass.
- [ ] Routing times recorded in the index.
