# Round 2 — Phase R2: whole-board floorplanning, pair alignment, `pcb_floorplan` / `pcb_place_blocks`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Read first: the index (incl. "Round 2", "As built (P4)", "As built (P9a)"), spec §9 and
> **§22.3**, `docs/superpowers/plans/board/phase-p4-placement.md`. Index contracts: B-C11 (+
> round-2 block), B-C14. **Wave R-2: starts when R1 is merged** (uses `geom.straight_corridor`).

**Goal:** placement uses the whole board: a global floorplan assigns every block a region
(edge blocks first, then min-cut bisection of the rest over the usable area with routing
channels sized by the nets crossing each cut) before the existing per-block packing fills each
region; adjacent connected 2-pad parts are aligned so a straight connection exists (rule b).
On ODH: fits 80 × 60 mm if physically feasible (courtyard fill reported), else 80 × 100 mm with
`spread ≥ 0.75`.

**Architecture:** new pure `cc/board/placer/floorplan.py` (`floorplan`, `place_blocks`) on top
of `pack.block_layout` / `pack.block_extent` / `pack.place_block` / `legalize.legalize`; a
post-pass `align_pairs` in `layout.py` after `layout_cluster`. Tool layer: `place.py` adds the
handlers, `specs_place.py` the two specs, `jobs.py` the two `run_cpu` jobs.

`cc/` = `backend/src/copperchat/`.

## Global Constraints

- Index Global Constraints + "Round-2 phase conventions". Determinism (B-C11): same input →
  same output, stable sort keys, no unseeded randomness. Placement grid 0.5 mm, legaliser snap
  0.05 mm unchanged. `PLACE_TIMEOUT_S` (60 s) bounds each job.
- `pcb_place_blocks` is a `PCB_WRITES` tool: hidden with `COPPERCHAT_BOARD_TOOLS=off`, budget 1,
  refuses `part_routed` on routed parts unless `unroute: true` (P4 rule), records
  `board_place_blocks`. `pcb_floorplan` is `PCB_READ` (no edit lock, cost 0).
- Locked parts and parts of blocks not named in `blocks` stay where they are (obstacles).

## Review Focus (this phase)

1. An edge block longer than its edge, or two blocks on one edge that overlap → `does_not_fit`
   with `courtyard_fill`, never overlap. → `test_floorplan_edge_conflict`.
2. A single free block → one region = the whole free area (no division by zero in the cut).
   → `test_floorplan_single_block`.
3. A block whose minimal box does not fit its region in any rotation → the cut ratio is
   re-balanced once, then `does_not_fit`. → `test_floorplan_rebalance_then_fail`.
4. Alignment pushing a part into a neighbour or off the board → shift skipped (legal slack
   only). → `test_align_pairs_never_overlaps`.

---

### Task 1: `floorplan()` (pure)

**Files:** Create `cc/board/placer/floorplan.py`; Modify `cc/board/placer/__init__.py`
(exports), `cc/board/placer/legalize.py` (`DoesNotFit.courtyard_fill: float | None = None`);
Test: `backend/tests/unit/board/test_placer_floorplan.py` (new; builders from
`backend/tests/unit/board/builders.py`).

**Interfaces:** B-C11 round-2 block. Algorithm (decided here, constants in the module):
1. `BlockBox` per block = `block_layout` box (+ `keep_channels`), best rotation for its region.
2. Usable area = outline bbox inset by the legaliser's edge margin, minus mounting-hole and
   keepout bboxes (as obstacles). `courtyard_fill` = Σ member courtyard areas / usable area.
   Σ block box areas > `PACK_MAX` × usable → `DoesNotFit(courtyard_fill=…)`.
3. Fixed blocks: a block with a `board.md` Region row → that region; a block whose anchor has a
   Placement edges row → a strip along that edge (depth = box depth + channel, centred on the
   row's position or the edge middle); removed from the free area (largest-rectangle split of
   the remainder into ≤ 4 axis-aligned free rects, biggest first).
4. Free blocks: recursive bisection of (rect, blocks): cut across the longer side; partition the
   blocks into two groups minimising Σ shared-net weight across the cut (weight = number of
   non-ground nets with pads in both blocks, + pull toward fixed blocks on that side), area
   balance within 35–65 %; exhaustive for ≤ 12 blocks, greedy Kernighan–Lin above; ties by
   block name. Cut position ∝ group areas (the slack spreads). Channel on the cut =
   `clamp(CHANNEL_PER_NET_MM × crossing nets, CHANNEL_MIN_MM, CHANNEL_MAX_MM)`, half per side.
5. A block box that does not fit its leaf rect in any rotation → retry that level with the
   ratio moved toward the block's need once; else `DoesNotFit`.

- [ ] **Step 1: Failing tests:** `test_floorplan_uses_whole_board` (6 equal blocks on 80 × 60 →
  regions tile the usable area, union bbox spans ≥ 90 %), `test_floorplan_edge_block_strip`,
  `test_floorplan_region_row_wins`, `test_floorplan_connected_blocks_adjacent` (two blocks with
  10 shared nets end in sibling leaves), `test_floorplan_channel_width_follows_crossings`,
  `test_floorplan_does_not_fit_reports_fill`, `test_floorplan_edge_conflict`,
  `test_floorplan_single_block`, `test_floorplan_rebalance_then_fail`,
  `test_floorplan_deterministic` (twice → identical), `test_floorplan_property_legal` (seeded
  `random.Random`, ≥ 200 cases: regions pairwise disjoint, inside usable area, every block
  assigned or `DoesNotFit`).
- [ ] **Step 2: Run** `uv run pytest tests/unit/board/test_placer_floorplan.py -q` — FAIL.
- [ ] **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): global block floorplan`.

### Task 2: `place_blocks()`, `spread`, pair alignment

**Files:** Modify `cc/board/placer/floorplan.py` (`place_blocks`), `cc/board/placer/layout.py`
(`align_pairs`), `cc/board/placer/score.py` (`PlacementScore.spread`); Test:
`test_placer_floorplan.py`, `backend/tests/unit/board/test_placer_layout.py`,
`backend/tests/unit/board/test_score.py`.

**Interfaces:** `place_blocks` = `floorplan` → `place_block(…, region=leaf, target=leaf
centre, keep_channels=…)` per block, largest first, earlier moves as `fixed` → one global
`legalize(max_shift=None)` → `(PlaceResult, Floorplan)`; any `DoesNotFit` is returned with
`courtyard_fill`. `align_pairs(model, cls, moves) -> tuple[Move, ...]`: for each pair (2-pad
part ↔ 2-pad part on a shared non-ground net, or 2-pad satellite ↔ its anchor pin) with edge gap
≤ `geom.ADJ_GAP_MM` and no `straight_corridor`, shift the satellite perpendicular by the offset
(≤ `REVIEW["M16_align_mm"]`, 1.0 mm) when the shifted courtyard stays legal; called at the end
of `layout_cluster`. `spread` = min(union-bbox width / usable width, height / usable height).

- [ ] **Step 1: Failing tests:** `test_place_blocks_legal_and_spread` (builder board with 8
  blocks → M1 pass, `spread ≥ 0.75`), `test_place_blocks_deterministic`,
  `test_align_pairs_led_resistor` (LED + resistor offset 0.4 → corridor exists after),
  `test_align_pairs_never_overlaps`, `test_spread_score`.
- [ ] **Step 2–4:** FAIL → implement → PASS. Existing `test_placer_*` stay green (P9a's crystal
  window: `CRYSTAL_STANDOFF_MM` untouched; if alignment moves a crystal or decap, exclude
  crystal / decoupling satellites from `align_pairs` and record).
- [ ] **Step 5: Commit** `feat(board): place_blocks over the floorplan; align adjacent pairs`.

### Task 3: Tools `pcb_floorplan` and `pcb_place_blocks`

**Files:** Modify `cc/kicad_tools/board/place.py`, `cc/kicad_tools/board/specs_place.py`,
`cc/board/jobs.py` (append `floorplan_job`, `place_blocks_job`), `cc/agent/profiles.py`
(`PCB_READ` += `pcb_floorplan`, `PCB_WRITES` += `pcb_place_blocks`), `cc/changes/repo.py` +
`frontend/src/components/changes/highlight.ts` (`board_place_blocks`); Test:
`backend/tests/unit/board/test_place_schemas.py`, `backend/tests/kicad/board/test_place_blocks.py`
(new), the P1c unit test that maps every `PCB_WRITES` tool to a kind.

**Interfaces:** schemas `_obj`, `additionalProperties: false`: `blocks` (array ≤ `MAX_BLOCKS`
32 of block names, default all defined blocks), `keep_channels` (0–5, default
`DEFAULT_KEEP_CHANNELS_MM`), `unroute` (write only). Result data: `regions [{block, x, y, w,
h}]`, `channels [{x1, y1, x2, y2, width}]`, `courtyard_fill`, `usable_mm2`, `score` (incl.
`spread`), `moved`, `placement_hash` before/after (write). `pcb_floorplan` returns one
placement-view render with region rectangles as markers (B-C9 `markers`). Errors: `no_board`,
`unknown_block`, `does_not_fit` (`details.courtyard_fill`, `needed_mm2`, `available_mm2`),
`part_routed`, `board_compute_timeout`.

- [ ] **Step 1: Failing tests:** schema bounds; kicad on `sp4_synth` (sync → outline 60 × 45 →
  rules → `pcb_define_blocks` from `pcb_analyze` → `pcb_place_blocks`): M1 pass, `spread ≥ 0.75`,
  one change-log entry `board_place_blocks`, History undo restores the staging positions;
  `pcb_floorplan` writes nothing (`board_hash` unchanged) and returns regions;
  flag off → both tools absent.
- [ ] **Step 2–4:** FAIL → implement → PASS (`uv run pytest -m kicad tests/kicad/board/test_place_blocks.py`, kicad-base, `timeout 900`).
- [ ] **Step 5: Commit** `feat(board): pcb_floorplan and pcb_place_blocks tools`.

### Task 4: Golden flow on the new tool; ODH placement probe

**Files:** Modify `backend/tests/kicad_e2e/flow.py` (turn 2: `pcb_define_blocks` →
`pcb_place_blocks` (it legalises itself) → check/render; `Answers.blocks` keeps `absorb` /
`edge`; per-block `region` / `order` stay supported for scripted overrides only), golden;
`scripts/odh-dry-run.py` only if its answer loading needs changes (it imports `flow`; else untouched).

- [ ] **Step 1:** `uv run pytest -m kicad_e2e -v` (kicad-base + Freerouting, `timeout 900`) —
  all hard gates pass, ≤ 180 s. A hard gate that newly fails (e.g. M8 crystal 4.97 → > 5 mm) is
  a placer defect: fix in this PR (per-pin escape room, or decap/crystal order), never a
  threshold. Regenerate the golden after rebasing; PR body explains each change.
- [ ] **Step 2: ODH probe** (copy outside the repo, not CI): run the dry run up to placement at
  80 × 60 mm and at 80 × 100 mm; record `courtyard_fill`, `does_not_fit` or the score (M1, M8,
  M10, `spread`, max pins per cell) and the placement renders (`/tmp/sp4-renders/r2-*`) in "As
  built (R2)". If 80 × 60 does not fit, say whether the fill (> `PACK_MAX`) or the floorplan
  shape is the reason.
- [ ] **Step 3:** "As built (R2)", CHANGELOG; lint/type/frontend lint+typecheck; PR `board round
  2 R2: whole-board floorplanning`; CI green; review-loop; done report with the ODH numbers and
  the render paths.
