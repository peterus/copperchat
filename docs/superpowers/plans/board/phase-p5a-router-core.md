# Phase P5a — Pure octilinear router

> Index contracts: B-C12 (produces `RouteRequest`, `RoutePath`, `build_grid`, `route`,
> `route_pair`; adds `solve`, see Task 4), B-C5 (consumes `analyse_bends`, `CHAMFER_MIN`,
> `JOG_MIN`), B-C2 (consumes). Index call 3. Spec §10.2, §12 notes (definitions), §15.2 calls
> 24, 55, §18.1, §21 (router performance budget). Spike 4A Q4c (`spikes/4a-board/router.py`:
> 15/15 short nets in 6.5 s incl. 4.2 s grid build at 0.1 mm and the DRC task; port ideas,
> never import). **Wave 4: starts when P3a is merged**; pure Python, no KiCad, no tool.

**Goal:** a deterministic grid A* router whose output contains only 0° and 45° direction
changes with the owner's straight-piece rules, exact octilinear pad approaches, through vias,
and coupled pairs — proven by property tests that its output has M4 = M7 = 0 and never hits an
obstacle.

`cc/` = `backend/src/copperchat/`. Units mm; directions are the 8 octilinear unit vectors
`DIRS[k]`, k = 0..7 counter-clockwise from +x (board coordinates, y down).

**Files:**
- Create: `cc/board/router/{__init__,grid,astar,simplify,pairs}.py`
- Tests: `backend/tests/unit/board/{router_maps,test_router_grid,test_router_astar,test_router_simplify,test_router_props,test_router_pairs,test_router_perf}.py`
  (`router_maps.py` = seeded random map generator shared by the property and performance tests)

---

### Task 1: Obstacle grid

**Interfaces:** `grid.py`: `class ObstacleGrid` (picklable; fields `pitch`, `x0`, `y0`, `w`,
`h`, `layers: tuple[str, ...]`, `cells: tuple[bytearray, ...]` per layer with 0 = free,
1 = own net, 2 = blocked; `via_blocked: bytearray`); methods `cell(x, y) -> tuple[int, int]`
(nearest), `xy(i, j) -> Point`, `free(li, i, j) -> bool`, `via_ok(i, j) -> bool`. B-C12
`build_grid(model, req, *, pitch=0.05, margin=0.0)`: window = bbox of `req.a`/`req.b`
grown by `WINDOW_MM = 6.0` (the router doubles it on failure, Task 2), clipped to the outline
bbox. Inflation for tracks `r = req.clearance + margin + req.width / 2`. Marked as blocked on
each layer in `req.layers`: pads of other nets (rotated rectangle exactly when the rotation
is a multiple of 90°, else its circumscribed circle; THT pads on every layer, SMD on their
layer), tracks/arcs of other nets (capsule of half width + r), vias of other nets (all
layers), copper-layer graphics (`GraphicItem` on a copper layer, stroked width + r), rule
areas with `tracks` keepout on their layers, everything outside the outline
polygon inset by `max(rule copper_edge_clearance, 0.3) + width / 2`. Own-net copper → 1.
`via_blocked` (when `req.allow_vias`): a cell where a via of `req.via` diameter with clearance
would touch other-net copper on any layer of `req.layers`, a `vias` keepout, or the edge inset.

- [ ] **Step 1: Failing unit tests** (`test_router_grid.py`, P1a `builders`):
  `test_pad_inflation_exact` (0.6 × 1.0 pad of another net, clearance 0.2, width 0.2 → cells
  within 0.4 mm of the pad edge blocked, the first cell outside free); `test_rotated_pad_45_circle`;
  `test_own_net_passable`; `test_track_capsule`; `test_via_blocks_all_layers`;
  `test_keepout_tracks_vs_vias` (a `{"vias"}` keepout blocks vias only); `test_edge_inset`
  (cell 0.39 mm inside the edge blocked for width 0.2, edge 0.3; 0.41 free);
  `test_window_clipped`; `test_grid_picklable`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/board/test_router_grid.py -q` — FAIL.
  **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): router obstacle grid`.

### Task 2: A* with 0°/45° turns, straight-piece rules, vias, pad entry

**Interfaces:** B-C12 `route(grid, req, *, max_expansions=2_000_000) -> RoutePath | None`.
State `(i, j, layer, dir, last_sense, run)`: `last_sense` ∈ {−1, 0, +1} (sense of the last 45°
bend, 0 = none since the start/a via), `run` = straight length since that bend in grid steps,
capped at the largest requirement. Moves: continue straight (cost = step length, `pitch` or
`pitch·√2`); turn ±45° (`dir ± 1`) only when `last_sense == 0`, or the same sense and `run`
covers `CHAMFER_MIN(width)`, or the opposite sense and `run` covers `JOG_MIN(width)`
(lengths compared in mm, so a 90° is two 45° bends with ≥ `max(1 × w, 0.25)` between them and
an S-offset needs ≥ `2 × w`); turns of 90° or more at one vertex do not exist as moves; turn
cost `TURN_COST = 0.3` mm-equivalent. Via (only when `req.allow_vias` and `via_ok`): change
to any other layer at the same cell, cost `VIA_COST = 2.0`, resets `last_sense` and `run`.
Layer preference: the first entry of `req.layers` costs ×1.0, every other ×1.2 (the caller
orders layers per net class, P5b). **Pad entry** (spec §10.2): the start/goal cell is the grid
cell nearest the pad centre inside the pad; the first move from a pad uses the pad's long
axis (both ways) or ±45° to it — 6 of 8 directions; square pads allow all 8; a pad rotated by
a non-multiple of 45° uses its axis rounded to the nearest 45°. Heuristic = octilinear
distance (admissible); ties broken by an insertion counter (determinism). Exhausting
`max_expansions` → `None`; no path in the window → retry once with the window doubled (new
grid via `build_grid`), then `None`.

- [ ] **Step 1: Failing unit tests** (`test_router_astar.py`): `test_straight_line`;
  `test_diagonal`; `test_ninety_needs_chamfer` (goal forces a 90° turn: the path has two
  same-sense 45° bends with a piece ≥ 0.25 mm for w 0.2 and ≥ 0.4 mm for w 0.4);
  `test_s_offset_respects_jog_min` (opposite-sense bends ≥ 2 × w apart);
  `test_no_turn_over_45_at_vertex` (inspect every vertex); `test_detours_obstacle`;
  `test_uses_via_when_blocked` (wall across the top layer, free bottom → one via);
  `test_no_via_when_disallowed` (→ `None`); `test_pad_exit_long_axis` (first segment parallel
  to the pad's long axis or at 45° to it); `test_prefers_first_layer`;
  `test_max_expansions_none` (`max_expansions=100` on a 20 mm route → `None`);
  `test_window_doubles_once`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): octilinear A* with straight-piece rules and vias`.

### Task 3: Simplify and exact pad approach

**Interfaces:** `simplify.py`: `merge_collinear(points) -> tuple[Point, ...]`;
`snap_ends(points, a: Point, b: Point, width) -> tuple[Point, ...]` — replaces the grid end
cells by the **real** (off-grid) pad centres keeping every segment octilinear: the end
segment keeps its direction and the previous vertex moves along the previous segment's line
to the intersection (spike `fix_ends` idea); when the intersection does not exist or would
shorten a piece below `CHAMFER_MIN`/`JOG_MIN`, a short octilinear stub (one axis-parallel
piece plus one 45° piece, both inside the pad) from the pad centre to the grid end is inserted
instead; `to_path(states, grid, req) -> RoutePath` (per-layer segments, vias at layer
changes, `length` = Σ segment lengths). Output points are rounded to 1 nm (1e-6 mm).

- [ ] **Step 1: Failing unit tests** (`test_router_simplify.py`): `test_merge_collinear`;
  `test_snap_moves_last_bend` (pad centre 0.013 mm off-grid → final segment exactly at its
  octilinear angle, `analyse_bends(..., pad_ends=(True, True))` all zero);
  `test_snap_stub_when_parallel` (stub inserted, stays inside the pad rectangle);
  `test_snap_never_creates_short_chamfer`; `test_vias_at_layer_changes`;
  `test_length_sum`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): router simplify and exact pad approach`.

### Task 4: Coupled pairs and the worker entry point

**Interfaces:** B-C12 `route_pair(model, p, n, gap) -> tuple[RoutePath, RoutePath] | None`:
coupled when `p.a`/`n.a` and `p.b`/`n.b` are each within `2 × (w + gap) + max pad size` of
each other (straight-ish pairs, spec call 24): route the centreline between the two pad-pair
midpoints with width `2w + gap` and the larger clearance on one layer (no vias), offset it by
`±(gap + w) / 2` (mitred at 45° bends, so the offsets stay octilinear), and join each offset
end to its pad with `route` on a small window; any failure → fallback: route `p`, then `n`
with a gap cost (cells at distance `gap + w ± pitch` from `p`'s segments cost ×0.8, cells
nearer than `gap + w` blocked); both failing → `None`. `pair_coupling(pp, pn, gap) -> float`
= fraction of `pp`'s length whose parallel partner segment lies at `gap + w ± 0.01` mm.
`__init__.py` exports B-C12 names plus **`solve(model: BoardModel, req: RouteRequest, *,
margin: float = 0.0, max_expansions: int = 2_000_000) -> RoutePath | None`** and
`solve_pair(model, p, n, gap) -> tuple[RoutePath, RoutePath] | None` — module-level,
picklable arguments, for `run_cpu` (B-C4; index B-C12 addition, reported).

- [ ] **Step 1: Failing unit tests** (`test_router_pairs.py`): `test_coupled_straight`
  (10 mm pair, gap 0.2, w 0.2 → `pair_coupling ≥ 0.8`, both paths octilinear, M4 = M7 = 0 by
  `analyse_bends`); `test_coupled_one_bend` (offset kept through a 45° bend);
  `test_fallback_when_far_apart` (pads 6 mm apart → two routes, `pair_coupling < 0.8`, gap ≥
  `gap` everywhere); `test_pair_none_when_blocked`; `test_solve_picklable` (`pickle.dumps` of
  a `BoardModel` + `RouteRequest` round-trips; `solve` equals `route(build_grid(...))`).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): coupled pair routing and worker entry points`.

### Task 5: Property, determinism and performance tests

**Interfaces:** `router_maps.py`: `random_map(rng: random.Random) -> tuple[BoardModel,
list[RouteRequest]]` — outline 10–30 mm, 1–2 copper layers, 2–20 obstacles of other nets
(SMD pad rows, tracks at random octilinear and non-octilinear angles, vias, one keepout with
probability 0.3), 1–3 two-pad requests with widths 0.15–0.5 mm and clearances 0.1–0.3 mm;
`exact_clearance_ok(model, path, req) -> bool` — an independent geometric check (segment to
pad rectangle / segment / circle distances, no grid) that every segment keeps `clearance +
width / 2` from other-net copper (tolerance 1e-6) and stays inside the edge inset.

- [ ] **Step 1: Failing property tests** (`test_router_props.py`, seeds 0..249, seed printed
  on failure): for every request with a returned path: `analyse_bends` per layer polyline
  (pad ends `True` at the pads, `False` at vias) has `ninety == acute == micro_jogs ==
  short_chamfers == len(non_octilinear) == 0` (`test_output_m4_m7_zero`);
  `exact_clearance_ok` (`test_no_obstacle_hit`); the path starts and ends at the exact pad
  centres (`test_exact_pad_centres`); `test_open_maps_always_route` (maps without obstacles →
  a path for every request); `test_deterministic` (each map routed twice → identical
  `RoutePath`).
- [ ] **Step 2: Failing performance test** (`test_router_perf.py`): a 30 × 30 mm 2-layer
  board with 20 SMD-like parts from `router_maps` (seed 4242) and 15 short 2-pin nets (3–10 mm),
  routed sequentially with each result added to the model as copper (as P5b will):
  `test_15_short_nets_budget` — all 15 routed and total wall time < 10 s (spike: 6.5 s for 15
  nets incl. the DRC task and 4.2 s grid build at 0.1 mm; budget generous for CI),
  `record_property("router_15_nets_s", …)` and per-net expansions; the measured value goes
  into "As built".
- [ ] **Step 3: Run** `uv run pytest tests/unit/board -k router -q` — FAIL. **Step 4:
  Implement/tune** (`TURN_COST`, `VIA_COST`, `WINDOW_MM` within the rules above; hard
  geometry rules are not tunable). **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `test(board): router property and performance tests`.

### Task 6: Docs + index as built, checks, PR

- [ ] Index "As built (P5a)": `solve`/`solve_pair` (add to B-C12 in place), `ObstacleGrid`
  fields, cost constants, measured performance (15 nets: seconds, expansions), property-test
  runtime. No CHANGELOG entry (no user-visible tool).
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src` (all with `timeout`).
- [ ] PR `board phase P5a: octilinear router core`; review-loop per the index's Phase
  Conventions (focus: performance of the state space, correctness of the straight-piece rules).

## Acceptance (P5a)

- [ ] CI `backend` green (P5a adds no kicad test).
- [ ] `test_output_m4_m7_zero` and `test_no_obstacle_hit` pass for 250 seeded maps.
- [ ] `test_15_short_nets_budget` < 10 s; measured time recorded in the index.
- [ ] `test_deterministic`, `test_max_expansions_none` pass; no kipy import in `cc/board/router/`.
