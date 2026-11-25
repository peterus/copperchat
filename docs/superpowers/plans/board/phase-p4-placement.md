# Phase P4 — Placer and placement tools

> Index contracts: B-C11 (produces), B-C4 (consumes `BoardChangeSet`, `snapshot="full"`,
> `unrouted_by_call`, `run_cpu`), B-C6, B-C7, B-C10 (`placement_hash` invalidation), B-C14
> (`does_not_fit`, `unknown_block`, `part_routed`, `not_found`). Index calls 3, 6, 9, 10. Spec
> §7.2 "Placement" + "Moves on a routed board", §9.2, §9.3, §15.2 calls 21, 53, 66.
> Spike 4A Q3 (`spikes/4a-board/place.py`, port ideas, never import). **Wave 5: starts when
> P2 and P3b are merged.**

**Goal:** the agent places a board block by block — functional blocks stored as `blk:<name>`
groups, each block clustered around its anchor and packed legally into a region in one commit,
plus exact moves and a legaliser — and a part that already has copper is only moved when the
agent explicitly unroutes it.

`cc/` = `backend/src/copperchat/`. `Rect` = B-C2's `Rect(x, y, w, h)` in `cc/board/model.py`
(P1a).

**Files:**
- Create: `cc/board/placer/{__init__,cluster,layout,pack,legalize,score}.py`,
  `cc/kicad_tools/board/{place,unroute_ops,specs_place}.py`
- Modify: `cc/kicad_tools/board/specs.py` (append four specs), `cc/board/jobs.py` (P3a's
  `run_cpu` job module, append `place_block_job`, `legalize_job`),
  `backend/tests/kicad/board/conftest.py` (append `place_synth`)
- Tests: `backend/tests/unit/board/{test_placer_cluster,test_placer_layout,test_placer_pack,test_placer_props,test_legalize_props,test_score,test_place_schemas,test_unroute_select}.py`,
  `backend/tests/kicad/board/{test_define_blocks,test_place_block,test_move_legalize,test_routed_parts,test_place_synth}.py`

---

### Task 1: Clusters and cluster layout

**Interfaces:** `cluster.py`: `@dataclass(frozen=True) Cluster(anchor: str | None, members:
tuple[str, ...])`; `build_clusters(model, cls, refs: Sequence[str]) -> tuple[Cluster, ...]`
— satellites follow P3a's block rules restricted to `refs` (switcher passives → switcher,
crystal + load caps → its IC, decoupling → its IC, others → anchor sharing most signal nets,
leftovers → anchor sharing most nets, else a cluster of their own). `layout.py`:
`layout_cluster(model, cls, c: Cluster, *, gap: float) -> tuple[Move, ...]` relative to the
anchor at `(0, 0)` rotation 0: decoupling caps first (smallest courtyard first), each at the
side of its served pin, rotated so the supply pad faces the pin, at the minimum courtyard gap
`gap = max(rule courtyard_clearance, 0.0) + 0.05`; then crystal and load caps, switcher
passives (inductor towards the SW pin, in-caps towards VIN/GND), then the rest sorted by shared
nets desc, ref; each satellite starts just outside the anchor courtyard on the side of the
anchor pad it connects to (signal nets first, then non-ground supplies) and takes the nearest
free spot on a 0.25 mm spiral (spike `layout_cluster` idea); 2-pad parts keep their pad axis
radial. `Move` is re-exported from `cc/board/staging.py`.

- [ ] **Step 1: Failing unit tests** (`test_placer_cluster.py`, `test_placer_layout.py`, P3a's
  `builders_circuit`): `test_cluster_switcher_passives`; `test_cluster_decoupling_follows_ic`;
  `test_leftover_own_cluster`; `test_decap_on_served_pin_side` (served pin on the anchor's
  right row → cap x > anchor courtyard right edge, supply pad nearer the pin than the ground
  pad); `test_decaps_first_at_min_gap` (gap between cap and anchor courtyards = 0.05 ± 0.001
  with `courtyard_clearance` 0); `test_layout_no_overlap`; `test_layout_deterministic`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/board/test_placer_cluster.py
  tests/unit/board/test_placer_layout.py -q` — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): placer clusters and cluster layout`.

### Task 2: Block packing, legaliser, score

**Interfaces:** B-C11 exactly (incl. the `target` keyword):
`place_block(model, cls, block_refs, region: Rect, *, side: Side, rotation: int | None,
keep_channels: float = 0.5, fixed: Sequence[Move] = (), allowed_sides: frozenset[Side],
edge: str | None = None, target: Point | None = None) -> PlaceResult | DoesNotFit`;
`legalize(model, moves, *, movable: frozenset[str], max_shift: float | None, keepouts:
Sequence[tuple[Point, ...]], edge_margin: float) -> tuple[Move, ...] | DoesNotFit`;
`score_placement(model, cls, doc) -> PlacementScore`, `@dataclass(frozen=True)
PlacementScore(m1_violations: int, m8_worst_mm: float | None, m9_reviews: int,
m10_worst_ratio: float | None, airwire_mm: float, max_cell_pins: int, routability_ok: bool)`.
Packing (`pack.py`): the block = `layout_cluster` of each cluster, clusters shelf-packed
(largest first) at `keep_channels`; candidates = every 0.5 mm grid position × 4 block
rotations (only `rotation` when given) with the block bbox inside `region ∩ (outline − edge
margin)`; legal = no courtyard overlap with placed parts / locked parts / `fixed` inflated by
`keep_channels`, not in a footprint keepout rule area; cost = Σ over signal-net pads of
`octi_dist` to the nearest placed pad of that net (airwire to placed parts) + 0.5 × n ×
`octi_dist(block centre, target or region centre)` + 3.0 × distance of each connector's
courtyard to its `board.md` edge (else the nearest outline edge); weights are module constants.
Lowest cost wins, ties by (y, x, rotation). Then `legalize` over the block's unlocked parts.
`side == "bottom"` flips the members (rotation mirrored); `place_block` raises `ValueError`
when an SMD member would land on a side ∉ `allowed_sides` (the tools check this first and
refuse with `invalid_argument`, spec §9.2 side policy).
No legal candidate → `DoesNotFit(needed_mm2 = Σ courtyard bbox areas inflated by
keep_channels, available_mm2 = region area − placed/keepout area inside it, refs)`.
`legalize`: iterative pairwise push-apart of overlapping courtyards along the smaller
penetration axis (movable parts only; locked and non-movable fixed), clamp into the outline
minus `edge_margin` and out of keepouts, snap to 0.05 mm, ≤ 200 rounds; any part shifted >
`max_shift` or overlaps left → `DoesNotFit`. Edge margin = rule `copper_edge_clearance`
(0.3 when unset). Routability: pins (pads) per 5 × 5 mm cell over the board; threshold
`MAX_CELL_PINS` starts at 30 and is tuned in Task 5 (spec call 21; recorded).
Determinism: no randomness, natural-ref sort keys everywhere.

- [ ] **Step 1: Failing unit tests**: `test_placer_pack.py`: `test_pack_into_region`
  (all parts inside the region, no overlaps); `test_rotation_fixed` (`rotation=90` → anchor
  rot 90); `test_connector_pulled_to_edge` (edge row `left` → connector courtyard ≤ 0.5 mm
  from the left outline edge); `test_keep_channels_gap` (≥ 0.5 mm between the block and an
  already placed part); `test_does_not_fit_reports_area` (region 5 × 5 for 40 mm² of parts →
  `needed_mm2 ≥ 40`, `available_mm2 ≤ 25`); `test_fixed_part_respected`;
  `test_deterministic` (two runs → identical moves). `test_score.py`:
  `test_airwire_octilinear_mst`, `test_max_cell_pins`, `test_score_uses_p3a_metrics`.
- [ ] **Step 2: Failing property tests** (`test_placer_props.py`, `test_legalize_props.py`,
  seeded `random.Random(seed)` for seeds 0..249, seed printed on failure): random outline
  20–80 mm, 2–30 parts with courtyards 0.5–8 mm and random pads/nets, 0–3 keepouts, 0–3
  locked parts already placed legally; `place_block` into the whole outline →
  `test_no_overlaps`, `test_inside_outline_minus_margin_and_keepouts`,
  `test_locks_never_move`, `test_grid_snap_005`; `test_never_does_not_fit_when_sparse`
  (Σ courtyards ≤ 25 % of the outline → never `DoesNotFit`); `legalize` of random overlapping
  placements → no overlaps, inside, locks unchanged, every shift ≤ `max_shift` when given
  (else `DoesNotFit`). Runtime of the property files < 60 s locally (recorded).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): block packing, legaliser and placement score`.

### Task 3: `pcb_define_blocks`

**Interfaces:** `place.py`: `prepare_define_blocks`, `specs_place.py` registers
`edit_spec("pcb_define_blocks", …)`. Schema: `blocks: [{name (index call 6 regex), refs:
[1..200], anchor?: ref}] 1..32`, `replace?: bool = false`. Prepare: unknown refs →
`not_found` with `details.closest`; a ref in two blocks, an `anchor` not in its `refs`, a
mounting hole / fiducial (P3a class) in a block → `invalid_argument`. One stage (one commit):
`replace` → remove every `blk:*` group, else only same-named ones; members leave any other
`blk:*` group first (KiCad: one group per item); create groups via `create_items(Group)` +
`api.set_group_name`. Verify: read-back groups == request. Cost `Logical(1)`, kind
`board_define_blocks`. Report: blocks with counts, refs not in any block, `next:
"pcb_place_block"`.

- [ ] **Step 1: Failing kicad tests** (`test_define_blocks.py`, P3b's `synth_setup`):
  `test_define_from_analyze` (`pcb_analyze` proposals → groups `blk:<name>` read back);
  `test_replace_false_keeps_others`; `test_replace_true`; `test_ref_moves_between_blocks`;
  `test_unknown_ref_closest`; `test_hole_refused`; `test_fail_after_1` (`board_hash`
  unchanged); `test_budget_cost_1`. Unit `test_place_schemas.py`: bounds and
  `additionalProperties: false` for all four tools.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): pcb_define_blocks`.

### Task 4: `pcb_place_block`, `pcb_move`, `pcb_legalize`, routed parts

**Interfaces:** `unroute_ops.py` (shared with P5b's `pcb_unroute`):
`@dataclass(frozen=True) CopperSelection(track_kiids: frozenset[str], via_kiids:
frozenset[str], nets: tuple[str, ...], locked: int)`; `attached(model, refs) ->
dict[str, tuple[str, ...]]` (ref → nets with a track end inside one of its pads or a via in
it); `select_copper(model, *, nets: Sequence[str] = (), region: Rect | None = None,
pads_of: Sequence[str] = (), include_locked: bool = False) -> CopperSelection` — seeds
(tracks/vias of `nets`, items intersecting `region`, items touching pads of `pads_of`)
extended to their whole **connection component** (same-net graph of tracks, arcs and vias,
joined at shared endpoints, via positions and pads; zones excluded), so nothing dangling
remains; with `include_locked=False` a component containing a locked item is skipped
entirely (`locked` counts them); `unroute_stage(sel: CopperSelection) -> BoardStage` (one
commit, `remove_items`). Tools (all two-phase, read `read_board_doc` for `allowed_sides`):
- `pcb_place_block {block, region: {x,y,w,h} | {edge: top|bottom|left|right, offset?: 0..500},
  side?: top|bottom = top, rotation?: 0|90|180|270, keep_channels?: 0..5 = 0.5, fixed?:
  [{ref,x,y,rot,side}] ≤ 200, unroute?: bool = false}`; block = group `blk:<block>` (unknown →
  `unknown_block` with `closest`); edge form = a strip along that outline edge, depth = the
  block's largest layout extent + 2 × `keep_channels`, `target` = the point `offset` mm along
  the edge from its top/left end (default centre); computed in `run_cpu(place_block_job, …,
  timeout_s=60)` (`place_block_job` / `legalize_job` are module-level functions appended to
  `cc/board/jobs.py`, P3a's job module; `pcb_move {legalize: true}` and `pcb_legalize` use
  `legalize_job`); `DoesNotFit` → `does_not_fit` with `details {needed_mm2, available_mm2,
  refs}`, nothing written; cost `Logical(1)`, kind `board_place_block`.
- `pcb_move {items: [{ref, x?, y?, rot?, side?, locked?}] 1..200, legalize?: bool = false,
  unroute?: bool = false}`; each item needs a field besides `ref`; `legalize: true` runs
  `legalize(movable = moved refs)` on the result; cost `Logical(ceil(n/10))`, kind `board_move`.
- `pcb_legalize {scope: all|block, block?, max_shift?: 0 < v ≤ 50, unroute?: bool = false}`;
  movable = unlocked parts in scope; cost `Logical(1)`, kind `board_legalize`.

Routed-part rule (spec §7.2, call 66): a part to be moved (rotation/side/position differ by
more than the rounding of `placement_hash`) with `attached` copper → `part_routed`
(`details {refs, nets}`) unless `unroute: true`; then stage 1 = `unroute_stage(select_copper(
pads_of=moved refs, include_locked=True))`, `cs.unrouted_by_call` = its nets,
`cs.snapshot = "full"` (compensation recreates the deleted locked copper); stage 2 = the moves
(`update_items`, `flip_items` for side changes) in one commit. Verify: read-back positions
equal the moves (0.001 mm, 0.01°). Step report: moved refs (≤ 50 + omitted), unrouted nets,
`placement_hash` changed, `PlacementScore` before → after (M1 violations, airwire, M8 worst,
max cell pins), `next` (`pcb_place_block` for unplaced blocks, else `pcb_metrics` + render).

- [ ] **Step 1: Failing unit tests** (`test_unroute_select.py`, builders): component
  extension across a via; `include_locked=False` skips a component with one locked track;
  region seed extends to the whole connection; zone never selected.
- [ ] **Step 2: Failing kicad tests**: `test_place_block.py` (`synth_setup`, blocks defined):
  `test_place_block_one_commit` (all members inside the region, DRC task 0
  `courtyards_overlap`, one change-log entry); `test_edge_form` (J1's block with `edge: left`
  → J1 courtyard ≤ 0.5 mm from the left edge); `test_does_not_fit_writes_nothing` (region 3 × 3
  → `does_not_fit`, `board_hash` unchanged); `test_bottom_refused_by_board_md`;
  `test_unknown_block_closest`; `test_place_block_fail_after_1`. `test_move_legalize.py`:
  `test_move_exact` (x/y/rot read back), `test_move_flip_side`, `test_move_with_legalize`
  (overlap resolved), `test_move_cost_ceil` (11 items → 2), `test_legalize_locks_fixed`,
  `test_legalize_max_shift_does_not_fit`, `test_move_invalidates_placement_approval`
  (approve via P3b's flow, `pcb_move` 0.5 mm → `checkpoint_state` stale,
  `require_placement_approved` raises). `test_routed_parts.py`: setup creates, through
  `apply_board_change_set` stages in the test, a **locked** two-segment octilinear track
  between a pad of U1 and a pad of C1 on their shared net and a **locked** fanout-like 0.6 mm
  stub + via on U1's GND pad; `test_move_routed_refused` (`part_routed`, `details.nets`
  lists both nets); `test_move_unroute_removes_locked` (`unroute: true` → both components'
  KIIDs gone, report lists the nets as unrouted, DRC task has no `track_dangling` /
  `via_dangling` marker); `test_unroute_move_fail_after_2` (failure after the unroute stage
  → `board_hash` equals the pre-call value: locked copper recreated with `locked` set);
  same rule for `test_place_block_routed_refused` and `test_legalize_routed_refused`.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): pcb_place_block, pcb_move, pcb_legalize with routed-part rule`.

### Task 5: Synth placed block by block

**Interfaces:** none new; a helper `place_synth(rig)` (appended to
`tests/kicad/board/conftest.py`; P5b's shared `placed_synth` / `routed_synth` rig builds on
it): `pcb_analyze` → `pcb_define_blocks` from
its proposals → for each block in descending part count, `pcb_place_block` with a fixed
floorplan in the test (connector blocks on `edge: left`, the power block in the left half,
the MCU block in the right half, the rest `region` = whole outline).

- [ ] **Step 1: Failing kicad test** (`test_place_synth.py`): `test_synth_placed_m1_pass`
  — after `place_synth`, `pcb_metrics {only: ["M1"]}` passes, DRC task: 0
  `courtyards_overlap`, `pcb_status.unplaced == 0`; `record_property` for airwire, M8 worst,
  M10 worst ratio, max cell pins and the total placement seconds; `test_place_synth_deterministic`
  (a second fresh run → identical positions).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement/tune**: set `MAX_CELL_PINS` so the synth
  placement passes and a test-only placement with `keep_channels: 0` and all parts in one
  20 × 20 mm region exceeds it; record both values. **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `test(board): synth placed block by block`.

### Task 6: Docs + index as built, checks, PR

- [ ] Index "As built (P4)": `unroute_ops` API (for P5b), the jobs appended to
  `cc/board/jobs.py`, `MAX_CELL_PINS` and the tuning
  measurements, synth placement metrics and runtime, property-test runtime. CHANGELOG
  `[Unreleased]`: "Board placement tools — behind COPPERCHAT_BOARD_TOOLS."
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, `uv run pytest -m kicad tests/kicad/board` in the kicad-base image (with
  `timeout`).
- [ ] PR `board phase P4: placement`; review-loop per the index's Phase Conventions.

## Acceptance (P4)

- [ ] CI `backend` and all 8 `kicad (…, i/8)` shards green.
- [ ] Property tests (250 seeds each): no overlaps, inside outline minus margin and keepouts,
  locks never move.
- [ ] `test_synth_placed_m1_pass`: M1 pass, 0 courtyard overlaps by DRC.
- [ ] `test_move_unroute_removes_locked` and `test_unroute_move_fail_after_2` pass (locked
  route + fanout-like via removed, restored on failure, nothing dangling).
- [ ] `test_move_invalidates_placement_approval` passes; `does_not_fit` writes nothing.
