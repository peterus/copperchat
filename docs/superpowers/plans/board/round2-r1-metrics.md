# Round 2 — Phase R1: junction, entry and neck geometry; M15/M16; M3/M4/M11/M12 changes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> Read first: the index (incl. section "Round 2" and every "As built"), spec §12 and **§22**
> (wins over §12). Index contracts: B-C5, B-C7 as amended in "Round-2 contract changes".
> **Wave R-1: no dependency.** Pure Python (`cc/board/`), no KiCad in Tasks 1–4.

**Goal:** one geometry definition of T/X/Y junctions, pad entry zones, escape zones, neck
widths and straight corridors in `geom.py`, used by the metrics now and by the placer (R2),
router (R3) and post-processor (R4) later; the metric changes of spec §22.5 with M15/M16 shipped
as **review** behind `M15_HARD` / `M16_HARD = False`.

**Architecture:** `analyse_tracks` already finds every off-pad, off-via node of degree ≥ 3
(`geom.py:628`, it reports 90° pairs as `t_junctions`); R1 classifies those nodes with
`junction_kind` and fills `BendReport.y_junctions`. M16 walks each connection's routed path
(`routed_path`, the segments of the shortest path that `routed_length` already computes) and
checks corridor, entry zone and loop/jog. M12, M11 and M3 get local exemptions.

**Tech stack:** Python 3.13 stdlib only (index call 3); tests `backend/tests/unit/board/`.

`cc/` = `backend/src/copperchat/`.

## Global Constraints

- Index Global Constraints + "Round-2 phase conventions". Thresholds of §12 unchanged except
  the §22.5 changes; `ANGLE_TOL_DEG = 0.5`, `_JOIN_TOL = 0.002` reused, never duplicated.
- `M15_HARD = False`, `M16_HARD = False` in this phase (R4 switches them). With a switch off,
  the metric's hard offenders get verdict `review`, never `fail`.
- No design-specific names in code or non-fixture tests.

## Review Focus (this phase)

1. Junction headings measured from the node outward (never inward) — a reversed segment must
   give the same kind. → `test_junction_kind_orientation_free`.
2. A node that is both a 90° vertex of degree 2 and touched by a via is still exempt as today
   (vias exempt). → `test_y_at_via_exempt`.
3. Neck pieces that cross the escape-zone boundary (half in, half out) fail M12.
   → `test_m12_neck_crossing_zone_fails`.
4. Two pads of one net on **different** layers (THT vs SMD on B.Cu) never form a corridor.
   → `test_corridor_needs_shared_layer`.
5. The silk-line downgrade must not hide a silk **text** marker that shares a footprint.
   → `test_m3_text_marker_stays_hard`.

---

### Task 1: Junction classification and `BendReport.y_junctions`

**Files:** Modify `cc/board/geom.py` (B-C5 additions: `JunctionKind`, `junction_kind`,
`BendReport.y_junctions`, `analyse_tracks`). Test: `backend/tests/unit/board/test_geom_junctions.py`
(new), `backend/tests/unit/board/test_bends.py` (update).

**Interfaces:** Produces `junction_kind(headings) -> "t" | "x" | "y"` and
`BendReport.y_junctions` (B-C5). `t_junctions` keeps nodes with a 90° pair that are not "y"
(informational; P6a's `chamfer.postprocess` still rips them until R4 — do not touch chamfer).

- [ ] **Step 1: Failing tests** — `test_junction_kind_table` (parametrised, headings in deg):
  `(0, 180, 90)` → `t`; `(0, 90, 180, 270)` → `x`; `(0, 180, 45)` → `y`; `(0, 180, 135)` → `y`;
  `(0, 90, 225)` → `y`; `(0, 45, 180)` → `y`; `(0, 180, 90.4)` → `t` (inside tolerance);
  `(0, 180, 45.6)` → `y`. `test_junction_kind_orientation_free`: a board with a branch drawn
  once as (node→out) and once as (out→node) gives the same `analyse_tracks` result.
  `test_y_junction_reported` (builder: through-track 0°, branch at 45° meeting off-pad) →
  `y_junctions == (node,)`, `t_junctions == ()`; `test_t_junction_not_y` (branch at 90°) →
  `t_junctions == (node,)`, `y_junctions == ()`; `test_y_at_via_exempt`, `test_y_inside_pad_exempt`.
  In `test_bends.py`: `test_t_junction_45_ok` becomes `test_t_junction_45_is_y`
  (asserts `y_junctions`); `test_t_junction_90_fails` becomes `test_t_junction_90_reported_not_m4`
  (still in `t_junctions`; the M4 part moves to Task 4).
- [ ] **Step 2: Run** `uv run pytest tests/unit/board/test_geom_junctions.py tests/unit/board/test_bends.py -q` — FAIL.
- [ ] **Step 3: Implement.** Headings = the direction from the node to the other end of each
  incident segment (arcs: the tangent at the node). "y" if any pair's difference mod 180 is
  45° ± tol (covers 45° and 135°); else "t" for 3, "x" for ≥ 4. Nodes keyed with `_JOIN_TOL`
  exactly as the existing T detection.
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `feat(board): junction kinds and y_junctions`.

### Task 2: Entry zones, escape zones, neck width, straight corridor

**Files:** Modify `cc/board/geom.py`; Test: `test_geom_junctions.py` (append) or new
`test_geom_zones.py`.

**Interfaces:** Produces `ADJ_GAP_MM`, `ESCAPE_HALO_MM`, `ENTRY_ZONE`, `LOOP_TURN_DEG`,
`entry_zone`, `escape_zone`, `neck_width`, `straight_corridor`, `EntryReport`, `analyse_entry`
exactly as B-C5 (round 2). R2 uses `straight_corridor`, R3 uses all of them, R4 uses
`escape_zone` / `neck_width`.

- [ ] **Step 1: Failing tests:**
  - `test_neck_width`: pad 0.25 × 0.8, class 0.5, min 0.127 → 0.25; class 0.2 → 0.2; pad
    0.1 × 0.5, class 0.5, min 0.127 → 0.127.
  - `test_escape_zone_is_courtyard_plus_halo`: square courtyard 4 × 4 → zone bbox 5 × 5.
  - `test_corridor_aligned_pads`: two 0805 pads (1.0 × 1.3) at the same y, gap 2.0 → segment
    on the shared layer from pad edge to pad edge along y = centre; offset 0.5 in y → still a
    corridor (overlap 0.8 ≥ width 0.25), centre line = overlap band centre; offset 1.2 → None;
    gap 3.5 → None.
  - `test_corridor_needs_shared_layer` (F.Cu SMD vs B.Cu SMD) → None.
  - `test_entry_loop`: path leaving a pad east, then 45°, 45°, 45° (total 135°) inside the
    zone → `loops == (vertex,)`; `test_entry_jog`: opposite-sense 45° pair inside the zone →
    `jogs`; `test_entry_plain_45_ok`: one 45° bend in the zone → empty; bends inside the pad
    itself (as `pad_entry_ends` defines) → ignored.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (pure polygon helpers already in `geom.py`;
  rotated rectangle for pads via `pad.rot`). **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): entry/escape zones, neck width, straight corridor`.

### Task 3: M12 neck exemption, M11 own-antipad fix, M3 silk-line warning

**Files:** Modify `cc/board/metrics.py` (`_m12`, `_PlaneVoids` / `_m11`, `_m3`); Test:
`backend/tests/unit/board/test_metrics_round2.py` (new).

**Interfaces:** Consumes `escape_zone`, `neck_width` (Task 2), the preset's `min_track`
(`compute_metrics(..., preset=…)`; without a preset use `presets.PRESETS["jlcpcb-4l"].min_track`).

- [ ] **Step 1: Failing tests:**
  - `test_m12_neck_inside_zone_passes`: class 0.5, QFN pad 0.25 wide, a 0.25 track wholly inside
    the part's escape zone → no M12 offender.
  - `test_m12_neck_outside_zone_fails`: the same 0.25 track 2 mm outside the zone → offender.
  - `test_m12_neck_crossing_zone_fails`: one segment half in, half out → offender.
  - `test_m12_neck_below_min_fails`: 0.1 track in the zone (min 0.127) → offender.
  - `test_m11_own_via_antipad_not_crossing`: signal via with its own track on F.Cu, GND plane
    on In1 with the via's antipad → no "void crossing"; a **foreign** net's track over the same
    antipad hole → still a crossing.
  - `test_m3_silk_line_warning_only`: DRC marker `text_thickness`, items = footprint item
    KIIDs whose role (`Part.item_kiids`) is `shape` → M3 verdict not fail, offender label
    `silk_line_warning`, not counted as unaccepted; `test_m3_text_marker_stays_hard`: same
    marker type on a `reference` item → counts as before.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** M12: before reporting a narrow segment, accept it when both ends
  lie in `escape_zone(part)` of one part that has a pad of this net with `neck_width(pad,
  class_w, min_track) < class_w` and `width ≥ that neck width − M12_WIDTH_TOL`. M11: a crossing
  test skips the piece of a track inside a disc (radius as `_PlaneVoids` uses for antipads)
  around a via or plated pad **of the track's own net**. M3: a `text_thickness` marker whose
  mapped items all have role `shape` → review offender `silk_line_warning` (owner decision 1,
  spec §22.2).
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `fix(board): M12 neck-down, M11 own antipad, M3 silk-line warning`.

### Task 4: M4 without the T clause, M15, M16, switches

**Files:** Modify `cc/board/metrics.py` (`MetricId` → `"M16"`, `_m4`, new `_m15`, `_m16`,
`routed_path`, `M15_HARD`, `M16_HARD`, `REVIEW["M16_align_mm"] = 1.0`, `__all__`),
`cc/kicad_tools/board/specs_analysis.py` (description text: M4 without the T clause, M15, M16);
Test: `test_metrics_round2.py`, `backend/tests/unit/board/test_metrics_routed.py` (update),
`backend/tests/unit/board/test_specs.py` if it pins the metric count.

**Interfaces:** Produces B-C7 (round 2). M15: one offender per `y_junctions` point,
`what="y_junction"`, net. M16 per connection (`connections(model, net)` MST pairs): (a) `dogleg`
when `straight_corridor(a, b, w)` exists, its rectangle (segment ± w/2 + clearance) is free of
other-net pads, vias and keepouts, and `routed_path(model, a, b)` is not exactly one straight
segment on the corridor's layer outside the pads; (b) `pad_loop` / (c) `pad_jog` from
`analyse_entry` on each end's entry zone; placement stage (no tracks): `alignable` (review) for
adjacent 2-pad pairs (edge gap ≤ `ADJ_GAP_MM`) without a corridor whose perpendicular offset
could be removed by ≤ `REVIEW["M16_align_mm"]`. Verdict: `fail` when hard offenders exist and
the switch is on, `review` when any offender exists otherwise.

- [ ] **Step 1: Failing tests:** `test_m4_t_junction_not_hard` (the old T case → M4 pass);
  `test_m4_degree2_ninety_still_fails`; `test_m15_y_review_when_switch_off`,
  `test_m15_y_fails_when_switch_on` (monkeypatch `metrics.M15_HARD = True`);
  `test_m16_dogleg` (aligned 0805 pair routed with two 45° bends → `dogleg`),
  `test_m16_straight_ok`, `test_m16_corridor_blocked_by_foreign_pad_not_dogleg`,
  `test_m16_loop`, `test_m16_jog_review_only` (jog never `fail`, also with the switch on),
  `test_m16_alignable_placement_stage`; `test_switches_default_review` (both module constants
  `False`, golden-style board with a Y → verdict `review`, `hard_failures` unchanged);
  `test_metric_ids_m1_to_m16`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `uv run pytest -m "not kicad" tests/unit/board -q` — PASS.
- [ ] **Step 5: Commit** `feat(board): M15 Y-junction and M16 pad dogleg/entry metrics (review until R4)`.

### Task 5: Golden, index, CI, review

- [ ] **Step 1:** In the kicad-base image (`timeout 900`): `uv run pytest -m kicad_e2e -v`.
  Expected: hard gates still pass (M15/M16 review). Regenerate with `UPDATE_GOLDENS=1` after
  rebasing on `main`; the PR body lists each changed value (M3 verdict, new M15/M16 counts — the
  owner-marked Y-junctions and doglegs appear as review offenders; record the counts: R3/R4
  must bring them to 0).
- [ ] **Step 2:** `uv run pytest -m kicad tests/kicad/board/test_metrics_tool.py tests/kicad/board/test_check.py`
  (metric count / labels) — PASS.
- [ ] **Step 3:** Index: "As built (R1)" (golden counts, any contract deviation), CHANGELOG
  `[Unreleased]` line. Lint/type: `uv run ruff check . && uv run ruff format --check . && uv run mypy src`.
- [ ] **Step 4:** PR `board round 2 R1: junction geometry, M15/M16, metric fixes`, CI green,
  review-loop (index "Round-2 phase conventions"), done report with the golden's M15/M16 counts.
