# Round 2 — Phase R3: own router — neck-down, T-junction branching, straight-first, pad entry

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Read first: the index (incl. "Round 2", "As built (P5a)", "As built (P5b)", "As built (P9a)"
> → router fixes), spec §10.2 and **§22.1 / §22.4**, phase files P5a/P5b. Index contracts:
> B-C12 (+ round-2 block), B-C5 round-2 additions (R1). **Wave R-2: starts when R1 is merged.**

**Goal:** the own router (a) narrows a wide class track to the pad-limited width inside a
fine-pitch part's escape zone, so 0.5 mm power tracks reach 0.25 mm QFN pads (ODH: 13 / 2
`no_path`); (b) branches as T-junctions — perpendicular onto a straight own-net track — and
never as Y (reverses P9a's 90° branch ban); (c) takes the straight segment for aligned adjacent
pads; (d) leaves pads toward the other end, without jogs or loops. Router output: M4 = M7 =
M15 = 0 and M16 hard = 0 on the property tests and on `sp4_synth`'s critical set.

**Architecture:** `RouteRequest.neck` gives the search a second inflation width inside the
escape-zone polygons; segments carry their width (B-C12 round 2). `junction_masks` inverts its
rule (block 45°/135° against own copper at off-pad nodes, allow 90°); `junctions_ok` /
`valid_path` follow with `geom.junction_kind`. `RouteRequest.tree` adds every grid node on own
copper connected to pad b (collinear run, not at a bend vertex) as a goal, reachable only by a
perpendicular final move. `route_connections` tries `geom.straight_corridor` first. Pad-entry
cost: a bend inside `geom.entry_zone` costs `ENTRY_TURN_COST`; the first move away from the
goal direction by > 90° inside the zone is forbidden.

`cc/` = `backend/src/copperchat/`.

## Global Constraints

- Index Global Constraints (owner D2 + §22: only 45° bends within a path; T branches allowed,
  Y forbidden). Router grid 0.05 mm, `MAX_CELLS_PER_LAYER`, `ROUTE_DEADLINE_S` 300 s and the
  deadline rules unchanged; `pcb_route` worst case 360 s unchanged.
- Neck width = `geom.neck_width(pad, class_w, preset.min_track)`; never outside
  `geom.escape_zone(part)`; never below the preset's `min_track`.
- Every routed connection is still DRC-verified and locked (P5b); P9a's "no same-net
  X-crossing" stays (round-2 plan call 3).

## Review Focus (this phase)

1. A neck that continues past the zone because the path re-enters it → width switches at every
   boundary crossing. → `test_neck_only_in_zone`.
2. A tree goal on a through-track's **bend vertex** or chamfer → not a goal (would be Y).
   → `test_tree_goal_not_on_bend`.
3. Straight corridor free in geometry but DRC-rejected (clearance to a zone) → falls back to
   the search, no error. → `test_straight_first_falls_back`.
4. Coupled pairs (`pairs.py`) with per-segment widths → unchanged widths, coupling report
   unchanged. → existing `test_router_pairs.py` stays green.

---

### Task 1: Per-segment widths in `RoutePath` (contract change)

**Files:** Modify `cc/board/router/{astar,simplify,pairs}.py` (`RoutePath.segments` 4-tuple),
`cc/board/router/grid.py` (`path_clear`, `grid.py:354`, unpacks the segments),
`cc/kicad_tools/board/route.py` (`_create` uses the segment width); Test: update
`backend/tests/unit/board/test_router_{astar,simplify,pairs,props,grid}.py` and the shared
helper `backend/tests/unit/board/router_maps.py`,
`backend/tests/kicad/board/test_route.py` for the tuple shape.

- [ ] **Step 1:** change the tests' expectations to `(layer, start, end, width)` — FAIL.
  **Step 2:** implement (all widths = `req.width` here). **Step 3:** `uv run pytest tests/unit/board -q` — PASS.
- [ ] **Step 4: Commit** `refactor(board): router segments carry their width`.

### Task 2: Neck-down in escape zones

**Files:** Modify `cc/board/router/grid.py` (second inflation map for the neck width, built
**only over the bounding boxes of the `req.neck` zones** — a few mm² each — and used for nodes
inside a zone; the cell cap `MAX_CELLS_PER_LAYER` applies to the sum of both maps), `cc/board/router/astar.py` (node validity by zone),
`cc/board/router/simplify.py` (split runs at zone boundaries, width per piece),
`cc/kicad_tools/board/route.py` (`plan_connections` fills `req.neck` for each end pad whose
`neck_width < class width`); Test: `backend/tests/unit/board/test_router_neck.py` (new; builder:
QFN-like part, 0.5 mm pitch, 0.25 × 0.8 pads, courtyard), `backend/tests/unit/board/test_route_plan.py`.

- [ ] **Step 1: Failing tests:** `test_neck_reaches_fine_pitch_pad` (class 0.5 → path found, the
  pieces inside the zone 0.25, outside 0.5), `test_neck_only_in_zone` (no piece < 0.5 outside
  the zone), `test_neck_never_below_min_track`, `test_no_neck_when_pad_wide_enough` (0805 pad),
  `test_plan_sets_neck_for_fine_pitch_only`; metric cross-check: R1's M12 on the result → 0
  offenders.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS; `test_router_perf.py`
  budgets unchanged (if a budget grows, record and justify in As built);
  `test_neck_map_bounded` (the neck map's cells ≤ Σ zone-bbox cells).
- [ ] **Step 5: Commit** `feat(board): router neck-down at fine-pitch pads`.

### Task 3: T-junction branching, Y forbidden, tree goals

**Files:** Modify `cc/board/router/astar.py` (`junction_masks`, `junctions_ok`, `valid_path`,
goal set for `req.tree` with a **multi-source heuristic** = min octilinear distance to the
goal set's bounding boxes, the goal set capped at `TREE_GOALS_MAX = 2_000` nodes nearest to
pad a; `FIRST_TRY_EXPANSIONS` / deadlines unchanged), `cc/board/router/grid.py` (own-copper
goal nodes), `cc/kicad_tools/board/route.py` `_bends` (`route.py:1065`: count `y_junctions`,
no longer `t_junctions` — the post-route M4 check must accept the T branches this task creates),
`cc/kicad_tools/board/route.py` (`plan_connections(…, tree: bool = False)`: with `tree=True`,
every connection whose net already has copper connected to pad b gets `req.tree`, except
pairs; **`pcb_route` passes `tree=True`, the fill's re-route keeps the default `False`** until R4
switches `fill._bends_of` / relock to `y_junctions` and turns it on). Tree goal nodes are only
**interior points of straight own-net runs** — at least `width` away from every run end, never a
node of degree ≠ 2 (existing junctions, free track ends, bend vertices, pads, vias); Test:
`backend/tests/unit/board/test_router_astar.py` (rewrite the P9a junction tests; add
`test_tree_goal_not_on_existing_junction`, `test_tree_goal_not_on_track_end`),
`backend/tests/unit/board/test_router_props.py`.

**Interfaces:** after this task the docstring and tests of `junction_masks` state: "at a node
outside own pads/vias, a move at 45° or 135° to existing own-net copper is blocked; a move
perpendicular to it is allowed (T, owner rule a)". `valid_path` rejects a path whose junction
nodes (path vertices or own track ends on the path) have `geom.junction_kind == "y"`.

- [ ] **Step 1: Failing tests** (replace, not delete, the P9a ones — names kept where the
  meaning survives): `test_junctions_ok_accepts_ninety_branch_off_own_copper` (was
  `…rejects_ninety…`), `test_junctions_ok_rejects_45_branch`, `test_search_branches_as_t_off_a_shared_stub`
  (was `…as_y…`), `test_tree_goal_perpendicular`, `test_tree_goal_not_on_bend`,
  `test_junction_masks_direction_convention` (updated bits), property test: seeded ≥ 200 random
  maps with pre-existing own copper → every result has `analyse_tracks(model+path)` with
  `y_junctions == ()`, `ninety == ()`, `acute == ()`, `off_angle == ()`, M7 hard = 0.
- [ ] **Step 2–4:** FAIL → implement → PASS (incl. `test_junction_masks_cost_bounded_and_deadline`,
  `test_junction_masks_fast_with_long_diagonal_own_tracks`).
- [ ] **Step 5: Commit** `feat(board): router T-junction branching; Y forbidden (owner rule a)`.

### Task 4: Straight-first and pad-entry costs

**Files:** Modify `cc/kicad_tools/board/route.py` (`route_connections`: **pass 0** — before any search
in the call, every plan with a `geom.straight_corridor(a, b, w)` gets its one segment created and
DRC-checked (kept on pass); the corridors of the plans whose segment failed only because of
copper this call created are retried once at the end of pass 0; then the normal search for the
rest. So a later search can never block an earlier-planned corridor), `cc/board/router/astar.py` (`ENTRY_TURN_COST = 4.0` per bend inside
`geom.entry_zone`, the away-move ban), `cc/board/router/simplify.py` if snapping adds a jog;
Test: `backend/tests/unit/board/test_router_entry.py` (new), `backend/tests/kicad/board/test_route.py`.

- [ ] **Step 1: Failing tests:** unit — `test_entry_no_loop` (pad whose long axis points away
  from the goal: result has no `analyse_entry` loop), `test_entry_no_jog_when_straight_possible`,
  property: router output `analyse_entry` loops = 0 on ≥ 200 seeded maps; kicad on the P5b
  `placed_synth` rig:
  `test_straight_first_aligned_pair` (an aligned 2-pad pair → exactly one segment),
  `test_straight_first_falls_back`; `pcb_route` critical → M4 = M7 = M15 = 0, M16 hard (dogleg,
  pad_loop) = 0.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(board): straight-first and pad-entry costs (owner rules b, c)`.

### Task 5: Golden, ODH critical-routing probe, index

- [ ] **Step 1:** `uv run pytest -m kicad_e2e -v` — hard gates pass; golden regenerated after
  rebasing; the PR body lists the M15/M16 count changes (expected: the own router's Y /
  doglegs / loops gone; Freerouting's remaining Y-junctions are R4's).
- [ ] **Step 2: ODH probe** (copy outside the repo): 80 × 100 mm, P9a's answers with the
  power class 0.5 mm: `pcb_route` critical → record seconds, `no_path` count (target: none from
  pad width), M4/M7/M15/M16 on the critical copper, render `/tmp/sp4-renders/r3-*`.
- [ ] **Step 3:** "As built (R3)" (contract deviations, perf, ODH numbers), CHANGELOG; lint/type;
  PR `board round 2 R3: router neck-down, T-junctions, straight pads`; CI green; review-loop;
  done report.
