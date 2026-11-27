# Round 2 — Phase R2b: relaxed, logical placement (owner follow-up to R2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Read first: the index (incl. "Round 2", "As built (R2)", "As built (P4)", "As built (P9a)"),
> spec §9 and **§22.3**, `board/round2-r2-floorplan.md`. Index contracts: B-C11 (+ round-2
> block), B-C14. **Starts after R2 (#118) is merged; runs in parallel with R3** (router files
> only there - this phase touches the placer files and the two R2 tools).

**Why (owner, 2026-11-27, on the R2 render of ODH at 80 x 100 mm):**
1. parts **inside** a block are too close - no room for the tracks between them;
2. the blocks are too **far apart** from each other;
3. blocks should be built more relaxed and arranged more logically, not rigidly pushed apart.

R2 builds every cluster with the minimum courtyard gap (+ 0.3 mm for satellites), boxes each
block, bisects the whole board into regions and centres each block in its region: the
board's slack collects **between** the blocks, none of it goes **into** them.

**Goal:** generic rules (no part- or design-specific code) that turn the feedback into
geometry, plus measurable checks that show it:

| Owner point | Rule (R2b) | Check (reported by `pcb_place_blocks`) |
|---|---|---|
| 1 | intra-block gaps sized for routing: pins of the host that do not connect to a satellite must escape between them (half to each side), one track pitch between any two satellites, via room next to fine-pitch pins; decaps and crystals keep their P4/P9a distances (M8) | `congestion_worst` / `congestion_over` - RUDY routing demand / capacity per 2.5 mm cell (intra-block routability) |
| 2 | inter-block gaps sized by the nets the two blocks share (`channel_mm`), never below `keep_channels`; plus at most `SLACK_MAX_MM` of evenly shared slack | `gap_excess_mm` - per connected block pair: actual gap - required channel (worst and weighted mean) |
| 3 | blocks placed one by one in connectivity order (fixed / edge blocks first, then the block with the most weighted shared nets to the placed ones: signal flow from the board's entry points inward), each packed against the **parts** already placed (blocks interlock; no rigid block boxes), pulled toward its connected partners and weakly toward its floorplan region (the global logical topology); the slack goes to the board periphery or evenly into the gaps, never into one hole | `largest_hole_mm2` - the largest empty rectangle inside the placement's bounding box that no part touches (free space collected in one place) |

Owner rules kept: decaps close to their pins (M8 unchanged, `CRYSTAL_STANDOFF_MM` window
untouched), adjacent connected passives aligned (rule b, `align_pairs` runs last as in R2),
connectors on their assigned edges (edge strips of the R2 floorplan).

**Acceptance:**
- golden `sp4_synth` stays green (0 hard); regenerated (idle host) when placement changes,
  every golden change explained in the PR body;
- ODH copy (never PROD, nothing of ODH in the repo) at 80 x 100 mm: visibly relaxed but
  compact, numbers per the table above; 80 x 60 and 80 x 80 tried again and reported honestly
  (`does_not_fit` with `courtyard_fill`, or the score);
- renders `/tmp/sp4-renders/r2b-odh-80x100-placed.png`, `r2b-odh-<best smaller>-placed.png`,
  `r2b-sp4_synth-placed.png`.

**Architecture:** new pure `cc/board/placer/routability.py` (pitch, channel, RUDY congestion,
largest hole, gap report); `cc/board/placer/layout.py` (relaxed satellite gaps, switched on
by a `relax` flag like R2's `escape_room`); `cc/board/placer/pack.py` (relaxed cluster
channels in `block_layout`, per-obstacle gaps in `pack_block`); `cc/board/placer/floorplan.py`
(`place_blocks` = floorplan for the fixed blocks + region targets, then the constructive,
connectivity-ordered packing with an even-slack search); `cc/board/placer/score.py`
(additive `PlacementScore` fields); tool layer `cc/kicad_tools/board/place.py` (report fields,
append). `pcb_place_block` (single block) keeps P4's compact layout - its fixtures
(`place_synth`) feed the P5b/P6b tests, as in R2.

`cc/` = `backend/src/copperchat/`.

## Global Constraints

- Index Global Constraints + "Round-2 phase conventions" (thresholds never relaxed; golden
  regenerated after rebasing, never hand-merged). Determinism: same input → same output,
  stable sort keys, no unseeded randomness. Placement grid 0.5 mm, legaliser snap 0.05 mm.
- `PLACE_BLOCKS_TIMEOUT_S` (180 s) bounds the job; ODH (76 parts, 11 blocks) should stay < 30 s.
- Files: only `cc/board/placer/**`, `cc/kicad_tools/board/place.py` (report fields),
  `cc/kicad_tools/board/specs_place.py` (description text only), tests, the golden, the index
  ("As built (R2b)"), CHANGELOG. **Not** R3's router files, not `flow.py` unless the golden
  flow cannot pass otherwise (then minimal, recorded).

## Definitions (constants in the modules; starting values, tuned on `sp4_synth` + ODH, final values in "As built (R2b)")

- **pitch(net)** = its net class's track width + clearance (`model.net_classes` /
  `net_class_of`, the Default class otherwise, `0.2 + 0.2` mm without classes).
  `signal_pitch(model)` = the Default class's pitch.
- **net weight** (block adjacency / attraction): signal, diff pair, high current 1.0; supply
  `SUPPLY_WEIGHT` = 0.25 (supply nets reach every block through pours); ground 0.
- **channel_mm(a, b)** between two units (clusters or blocks) =
  `clamp(CHANNEL_BASE_MM + pitch x ceil(shared_nets / 2), keep_channels, CHANNEL_MAX_MM)`
  (+ the slack Δ between blocks): half the shared tracks jog along the gap to line up, the
  rest cross it straight; a via + track (`CHANNEL_BASE_MM`) always fit.
- **escape tracks** of a satellite on a host side: the host's non-ground pads on that side
  inside the satellite's span (± one pitch) that are **not** on one of the satellite's nets;
  gap += pitch x `ceil(n / 2)` (at most `ESCAPE_TRACKS_MAX`), + one via pitch (via diameter +
  clearance) when that side's pin pitch < `FINE_PITCH_MM` (0.65) and n > 0.
- **RUDY congestion** (Rectangular Uniform wire DensitY): every non-ground connection
  (`metrics.connections`) spreads its octilinear length uniformly over its bounding box (grown
  to at least one pitch wide); demand per cell = Σ length share x pitch (mm² of copper);
  capacity per cell = free area (cell minus pad area) x `ROUTING_LAYERS` (2: both outer layers;
  inner planes are not counted); ratio = demand / capacity.
- **largest hole**: the board's usable box rasterised at 0.5 mm, cells under a courtyard (grown
  by half the minimum channel) blocked; the largest all-free axis-aligned rectangle inside the
  courtyards' union bounding box (histogram method, O(cells)).

## Review Focus (this phase)

1. A relaxed gap that pushes a decap or crystal beyond M8 → the golden fails. Expected: decaps
   / crystals keep P4/P9a gaps; `test_relax_keeps_decap_and_crystal_gaps`.
2. A block that never finds a spot in the constructive packing (the earlier blocks boxed it
   out) → the slack Δ steps down, then the whole board, then `does_not_fit` with
   `courtyard_fill` - never an overlap (M1). `test_place_blocks_tight_board_falls_back`.
3. Non-determinism through dict / set order in the connectivity order. `test_place_blocks_deterministic`.
4. A check that divides by zero (no connections, an empty board, a cell fully under pads).
   `test_congestion_empty_and_full_cells`, `test_largest_hole_empty_board`.

---

### Task 1: `routability.py` (pure checks + shared definitions)

**Files:** Create `cc/board/placer/routability.py`; Test:
`backend/tests/unit/board/test_placer_routability.py` (builders from `builders.py`).

**Interfaces:**
```python
def net_pitch(model: BoardModel, net: str) -> float
def signal_pitch(model: BoardModel) -> float
def net_weight(cls: Classification, net: str) -> float
def shared_weight(nets_a: Mapping[str, float] | frozenset[str], nets_b, cls) -> float
def channel_mm(shared: float, pitch: float, keep: float) -> float
@dataclass(frozen=True) class Congestion: worst: float; p90: float; over: int; cells: int
def congestion(model: BoardModel, cls: Classification, *, cell: float = CONG_CELL_MM) -> Congestion
def largest_hole(model: BoardModel, cls: Classification) -> float          # mm²
@dataclass(frozen=True) class GapRow: a: str; b: str; shared: float; gap: float; required: float
def block_gaps(model, cls, blocks: Sequence[BlockProposal], *, keep: float) -> tuple[GapRow, ...]
    # connected block pairs (shared weight > 0) - gap = least courtyard-box gap between their members
```
- [ ] **Step 1: Failing tests:** `test_net_pitch_from_class`, `test_channel_clamped`,
  `test_congestion_dense_beats_sparse` (same nets, parts 0.5 mm vs 3 mm apart → higher worst),
  `test_congestion_empty_and_full_cells`, `test_largest_hole_finds_gap` (two blocks 20 mm apart
  → hole ≥ the gap area), `test_largest_hole_empty_board`, `test_block_gaps_rows`.
- [ ] **Step 2–4:** FAIL → implement → PASS (`uv run pytest tests/unit/board/test_placer_routability.py -q`).
- [ ] **Step 5: Commit** `feat(board): routability, hole and gap checks for placement`.

### Task 2: relaxed cluster layout

**Files:** Modify `cc/board/placer/layout.py` (`layout_cluster(..., relax: bool = False)`:
`_gap_for` per side with the escape tracks / via room / one pitch between satellites),
`cc/board/placer/pack.py` (`block_layout(..., relax=False)`: the clusters of a block shelf-packed
`channel_mm` apart instead of `keep_channels`); Test: `test_placer_layout.py`, `test_placer_pack.py`.

- [ ] **Step 1: Failing tests:** `test_relax_gap_counts_escaping_pins` (QFP host, a 2-pad
  satellite in front of 6 unconnected pins → gap ≥ base + 3 pitches),
  `test_relax_fine_pitch_via_room`, `test_relax_keeps_decap_and_crystal_gaps`,
  `test_relax_off_is_p4_layout` (flag off → identical moves), `test_block_layout_relaxed_channels`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(board): relaxed intra-block spacing sized for routing`.

### Task 3: constructive, connectivity-ordered `place_blocks`

**Files:** Modify `cc/board/placer/pack.py` (`pack_block(..., gaps: Mapping[str, float] | None
= None)`: an obstacle part's box grown by `gaps[ref] - keep_channels` when larger),
`cc/board/placer/floorplan.py` (`place_blocks`); Test: `test_placer_floorplan.py`.

**Algorithm:**
1. `floorplan` as R2 (relaxed block boxes) → the fixed blocks' regions (edge strips, Region
   rows) and every free block's region centre (its target).
2. Order: fixed blocks first (as R2), then repeatedly the unplaced block with the largest
   weighted shared nets to the placed blocks (ties: larger courtyard area, name); a block
   without any connection to the placed ones comes after those that have one.
3. Each block: `pack_block` with region = its fixed region (fixed blocks) or the whole usable
   box (free blocks), `target` = its floorplan region centre, `pull_weight` =
   `TARGET_PULL_WEIGHT` (weak: the airwire to the placed partners dominates, so connected blocks
   touch), `gaps` = per placed part `channel_mm(shared(block, its block)) + Δ`, relaxed
   layout.
4. Slack: Δ from `SLACK_STEPS_MM` (descending, e.g. 2.0, 1.0, 0.5, 0.0): the first Δ with every
   block placed and no fallback wins; else R2's region packing as the fallback for the blocks
   that did not fit, then `DoesNotFit(courtyard_fill)`.
5. One global `legalize`, `align_pairs` (rule b), score - as R2.

- [ ] **Step 1: Failing tests:** `test_place_blocks_connected_blocks_touch` (two blocks with 8
  shared nets end ≤ their channel + Δ apart; an unconnected third farther),
  `test_place_blocks_order_follows_connectivity`, `test_place_blocks_gaps_by_shared_nets`,
  `test_place_blocks_tight_board_falls_back`, existing `test_place_blocks_*` /
  `test_align_pairs_*` stay green (spread assertions adapted: `spread` is reported, compactness
  is the goal now - recorded in As built).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(board): compact, connectivity-ordered block placement`.

### Task 4: score + tool report

**Files:** Modify `cc/board/placer/score.py` (`PlacementScore` gains `congestion_worst: float =
0.0`, `congestion_over: int = 0`, `largest_hole_mm2: float = 0.0` - additive, defaults keep
old callers), `cc/board/placer/floorplan.py` (`place_blocks` also returns the gap rows in
`Floorplan.gaps`, additive), `cc/kicad_tools/board/place.py` (`pcb_place_blocks` report:
`congestion_worst`, `congestion_over`, `largest_hole_mm2`, `gap_excess_mm` {worst, mean},
`gaps` (≤ 32 rows)); Test: `test_score.py` / `test_placer_floorplan.py`,
`backend/tests/kicad/board/test_place_blocks.py` (fields present).

- [ ] Steps: failing tests → implement → PASS → commit `feat(board): placement routability report`.

### Task 5: golden, ODH probe, As built

- [ ] `uv run pytest -m kicad_e2e -v` (kicad-base + Freerouting, idle host, `timeout 900`): 0
  hard; regenerate `sp4_synth_metrics.json` after rebasing on `main` when placement moved.
  A hard gate that newly fails is a placer defect (fix here, never a threshold).
- [ ] ODH probe on a copy (answers outside the repo; J201 left, J302 bottom, 4 layers
  `jlcpcb-4l`, Power class 0.3 mm, keep_channels 1.5): 80 x 100, 80 x 80, 80 x 60 - fit or
  `does_not_fit` (+ reason), M1/M8/M10, spread, congestion, gap excess, largest hole, runtime;
  renders to `/tmp/sp4-renders/r2b-*`.
- [ ] "As built (R2b)" in the index (final constants, deviations, numbers, residuals),
  CHANGELOG; lint / types; PR `feat(board): R2b relaxed logical placement`; CI green;
  review-loop; done report.
