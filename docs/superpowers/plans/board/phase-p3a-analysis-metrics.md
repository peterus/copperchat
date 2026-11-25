# Phase P3a — Classification, `board.md` parser, bend geometry, metrics M1–M14, `pcb_analyze`, `pcb_metrics`

> Index contracts: B-C5, B-C6, B-C7 (produces); B-C2 (incl. `Part.rule_areas`,
> `Zone.filled`), B-C3, B-C4 `run_cpu`, B-C8 `FabPreset` / `PRESETS` (consumes, from P1a). Spec §7.1 (`pcb_analyze`, `pcb_metrics`), §8.4 (tables), §9.1, §10.1, §12,
> §18.1. Spike 4A Q6 (`spikes/4a-board/{metrics,place}.py`, port, never import). **Wave 3:
> starts when P1b and P1c are merged**; parallel to P2 (shared `board/specs.py` append-only).

**Goal:** a pure, design-agnostic analysis core — part/net classification, the machine-readable
`board.md` tables, the one bend definition (M4/M7) and all fourteen metrics with the owner's
hard thresholds — exposed as two cheap read tools.

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `cc/board/{classify,boardmd,metrics,jobs}.py`, `cc/kicad_tools/board/{analysis,specs_analysis}.py`
  (`cc/board/jobs.py` = the one module of `run_cpu` job functions; later phases append)
- Modify: `cc/board/geom.py` (append B-C5), `cc/kicad_tools/board/specs.py` (append the two specs),
  `backend/tests/kicad/board/conftest.py` (append `synth_board`)
- Tests: `backend/tests/unit/board/{builders_circuit,test_boardmd,test_bends,test_classify,test_metrics_placement,test_metrics_routed,test_metrics_rules}.py`,
  `backend/tests/kicad/board/{test_analysis_synth,test_metrics_tool}.py`,
  `backend/tests/kicad/board/goldens/sp4_synth_classification.json` (CopperChat output)

**Board for the kicad tests.** P3a does not depend on P2 (index wave 3), so the module-scoped
fixture `synth_board` (appended to `tests/kicad/board/conftest.py`) builds the board the way
P1b's `test_netlist_imports_cleanly` does: `synth_session` copy → `kicad-cli sch export netlist
--format kicadsexpr` → `api.create_board_document` → reopen → `Board.import_netlist(...,
dry_run=False)` → `save("pcb")`. API only, no file edits; footprints stay where KiCad puts them.
P2's sync is the product path; this helper is test setup only (index "File ownership" note).

---

### Task 1: `board.md` parser (B-C6 `BoardDoc`)

**Interfaces:** `parse_board_md(text: str) -> BoardDoc` (never raises), `async
read_board_doc(ctx) -> BoardDoc` (`ctx.docs is None` or `board.md` missing → `BoardDoc()` with
`allowed_sides = frozenset({"top"})`; else `await ctx.docs.read(ctx.project, "board.md")`).
Row types (frozen): `EdgeRow(ref, edge: Literal["top","bottom","left","right"] | None,
position: str, reason: str)`, `BlockRow(name, refs: tuple[str, ...], region: str, notes: str)`,
`ClassOverride(ref, part_class: PartClass, reason)`, `NetClassRow(name, width: float | None,
clearance: float | None, via: str, nets: tuple[str, ...])`, `AcceptedWarning(drc_type: str,
items: tuple[str, ...], reason: str, accepted_by: str)`. Parsing (same style as
`agent/context_injection.py::_parts_summary`): sections by `## ` heading (casefolded); a table
is recognised by its header cells (casefolded, exact set of the spec §8.4 headers); separator
rows skipped; HTML comments (`<!-- … -->`, also multi-line) removed **before** parsing, so the
template's table hints never become rows; lists split on `,`/whitespace; numbers accept a
trailing `mm`; `edge` = first word of "Edge/position" if it is one of the four edges (else
`None`, text kept in `position`); `drc_type` normalised (casefold, `drce_` prefix stripped,
spaces → `_`). Summary line `Assembly: <method>; sides: top|top+bottom` (case-insensitive,
index call 9) → `allowed_sides`; absent → `{"top"}`; any other `sides:` value → `{"top"}` +
an error. Input over 256 KiB → only the first 256 KiB parsed + error. Bad rows (cell count
≠ header count, unknown class, unparseable number, empty ref) are skipped with an error
`"<section> row <n>: <why>"`.

- [ ] **Step 1: Failing unit tests** (`test_boardmd.py`): `test_template_v2_parses_empty`
  (P1c's template file → no rows, no errors, sides `{"top"}`); `test_comment_hints_ignored`;
  `test_edges_table` (`| J1 | left edge, 10 mm from top | cable |` → edge `left`);
  `test_blocks_refs_split` (`U1, C1 C2` → 3 refs); `test_class_override_unknown_class`
  (error, row skipped); `test_net_class_numbers` (`0.25 mm` → 0.25); `test_accepted_warnings`
  (`DRCE_SILK_OVERLAP` → `silk_overlap`); `test_sides_top_bottom`, `test_sides_default`,
  `test_sides_invalid` (`sides: left` → `{"top"}` + error); `test_heading_case_insensitive`;
  `test_oversize_truncated`; `test_never_raises` (seeded `random.Random(11)`, 500 random
  strings built from `|`, `#`, `-`, `<!--`, newlines, unicode; seed printed on failure).
- [ ] **Step 2: Run** `uv run pytest tests/unit/board/test_boardmd.py -q` — FAIL. **Step 3:
  Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): board.md table parser`.

### Task 2: Bend geometry — the one M4/M7 definition (B-C5)

**Interfaces:** B-C5 exactly (`BendReport`, `polylines`, `analyse_bends`, `analyse_tracks`,
`ANGLE_TOL_DEG = 0.5`, `CHAMFER_MIN`, `JOG_MIN`), appended to `cc/board/geom.py`. Algorithm of
`analyse_bends(points, width, *, pad_ends)`: drop zero-length segments (< 1e-6 mm); a segment
is **non-octilinear** when its angle differs from the nearest multiple of 45° by more than
0.5° (midpoint reported); at every interior vertex the direction change δ ∈ [0°, 180°] is
classified **without gaps**: δ ≤ 0.5° collinear (no vertex); |δ − 45°| ≤ 0.5° a 45° bend with
sense = sign of the cross product; **every other vertex fails**: |δ − 90°| ≤ 0.5° →
`ninety`, δ ≥ 134.5° → `acute`, anything else (e.g. 30°, 46°, 60°, 90.8°, 120°) →
`off_angle` (B-C5 field `off_angle: tuple[Point, ...]`, counted in M4 hard like `ninety`);
consecutive 45° bends with
straight piece L between them: same sense and L < `CHAMFER_MIN(w)` → `short_chamfers`
(midpoint), opposite sense and L < `JOG_MIN(w)` → `micro_jogs`. **Pad-entry exemption (the
only one):** when `pad_ends[k]` is true, the first vertex counted from that end is a pad
entry: its 45° bend is not counted in `bends` and does not pair with the next bend for
chamfer/jog checks; a 90°/acute change there is still reported, and every vertex further in
is checked normally. `polylines(model, net, layer)`: joins same-net same-layer segments whose
endpoints coincide (0.002 mm), drops collinear interior vertices, splits at nodes of degree
≠ 2, at vias and at pad centres; an endpoint lying on the interior of another segment of the
same net/layer splits that segment (a T). `analyse_tracks(model)`: all nets/layers;
`pad_ends` true where the end lies inside a pad of the same net on that layer (rotated pad
rectangle); **T-junctions**: a branch node (degree ≥ 3) outside every pad and via where two
incident segments meet at 90° ± 0.5°; arcs (`ArcTrack`) are reported as `non_octilinear`
(owner: straight segments only).

- [ ] **Step 1: Failing unit tests** (`test_bends.py`, plain point lists and `builders`; `report`
  = the returned `BendReport` — its failure fields are tuples of points, compared by `len`;
  `bends` is an int):
  `test_constants` (`CHAMFER_MIN(0.1) == 0.25`, `CHAMFER_MIN(0.4) == 0.4`, `JOG_MIN(0.2) == 0.4`);
  `test_straight_ok`; `test_ninety_fails` (L shape → `len(report.ninety) == 1`); `test_valid_chamfer_passes`
  (w 0.2: two same-sense 45° bends with a 0.25 mm piece → 0 short chamfers, `bends == 2`; w 0.4:
  0.40 passes); `test_short_chamfer_fails` (w 0.2, piece 0.10 mm → `len(report.short_chamfers) == 1`;
  w 0.4, piece 0.39 → 1); `test_micro_jog_fails` (w 0.2, opposite-sense pair 0.30 mm apart →
  `len(report.micro_jogs) == 1`; 0.40 → 0); `test_valid_chamfer_never_micro_jog`;
  `test_acute_135_fails` (δ = 135° → `len(report.acute) == 1`); `test_reversal_is_acute` (180°);
  `test_vertex_90_8_fails` (δ = 90.8° → `len(report.off_angle) == 1`, `len(report.ninety) == 0`);
  `test_vertex_60_fails` (δ = 60° → `len(report.off_angle) == 1`); `test_vertex_46_fails` (δ = 46° →
  `len(report.off_angle) == 1`, `bends == 0`); `test_vertex_45_4_is_bend` (δ = 45.4° → 45° bend, no
  failure);
  `test_off_by_0_6_deg_fails` (segment at 45.6° → `len(report.non_octilinear) == 1`) and
  `test_off_by_0_4_deg_passes` (45.4° → 0); `test_t_junction_90_fails` (a branch ending on the
  interior of a straight same-net track at 90°, no pad there → `len(report.t_junctions) == 1` via
  `analyse_tracks`); `test_t_junction_45_ok`; `test_t_junction_inside_pad_ok`;
  `test_pad_entry_only_exempt_at_pad`: pad → 0.10 mm stub → 45° → 0.10 mm → opposite 45° →
  2 mm: `pad_ends=(True, False)` → `len(report.micro_jogs) == 0`, `bends == 1`; the same points with
  `(False, False)` → `len(report.micro_jogs) == 1`; a 90° at the first vertex with `(True, False)` →
  `len(report.ninety) == 1`; a short chamfer at vertices 2–3 from a pad end → `len(report.short_chamfers) == 1`;
  `test_polylines_join_split` (two collinear segments → one polyline of 2 points; split at a
  via); `test_arc_reported`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): one bend analysis for M4/M7, router and post-processor`.

### Task 3: Classification (B-C6)

**Interfaces:** B-C6 `classify(model, doc=None) -> Classification`, plus
`primary_ground(cls, model) -> str | None` (the ground net with most pads). Nets use the last
`/`-component of the name for patterns. Rules (spec §9.1, generic, no refs/MPNs):
**ground** `^(GND|AGND|DGND|PGND|VSS)$` or containing `GND` (case-insensitive); **supply**
`^(VCC\w*|VDD\w*|VBUS|VBAT|VSYS|[+-]?\d+V\d*|\+?\d+V\d+)$` (case-insensitive) or ≥ 4 pads on
≥ 3 parts with at least one 2-pad capacitor to ground; **mounting_hole / fiducial /
test_point** by library nickname (`MountingHole*`, `Fiducial*`, `TestPoint*`) or ref prefix
(`H`, `FID`, `TP`); **module_with_antenna**: the part has a footprint-embedded keepout rule
area (B-C2 `Part.rule_areas` non-empty — read by P1a's loader), or a board-level rule area
of `model.zones` with a keepout lies inside the part's courtyard, or a `board.md` override; **connector**: ref `^(J|P|CN|USB)\d` or library `Connector*`; **crystal**: ref
`^[YX]\d` or library `Crystal*`/`Oscillator*`; load caps = 2-pad caps between a crystal net
and ground; `ic` = the anchor sharing most crystal nets; **switching_regulator**: an anchor
pin net whose pads belong to that anchor and exactly one inductor (ref `L\d` or library
`Inductor*` → class `inductor`) plus optionally 2-pad caps whose other net contains only
that anchor's pads (bootstrap; index call 15); `vin_net` = the
anchor's supply net carrying a cap to ground that is not the inductor's other net;
`in_caps`/`out_caps` = caps between `vin_net`/the inductor's other net and ground **on the
switcher's schematic sheet** (when the net also feeds another anchor on the same sheet: only
caps nearer, by `octi_dist`, to the switcher than to that anchor — index call 15);
`fb_parts` = 2-pad resistors on a net that touches only the anchor and resistors;
**decoupling_cap**: 2-pad cap between a supply and a ground net, not a switcher in/out cap,
with an anchor on that supply net; served pin = the anchor pad on that net nearest
(`octi_dist`) to the cap's supply pad, ties by (ref, pin); **anchor**: ≥ 5 pads; precedence
mounting/fiducial/test point > module > connector > crystal > switching regulator > inductor >
decoupling > anchor > other. **Diff pairs**: names equal after stripping the suffix pairs
`_DP/_DM`, `D+/D-`, `+/-`, `P/N` (tried in that order), neither ground nor supply, both
reaching a common anchor (`anchor` = most pads, then ref). **Blocks**: one per anchor (not
mounting/fiducial/test point); satellites in order: switcher passives (incl. bootstrap) → the
switcher, crystal + load caps → its `ic`, decoupling → its `ic`, other non-anchors → the
anchor sharing most signal nets (ties: same sheet, then ref); leftovers → the anchor sharing
most nets of any kind, else block `misc`; name `<sheet>-<anchor ref>` (sheet = last non-empty
path component or `root`, characters outside the index call 6 name regex → `_`).
**Critical nets** (spec §10.1 default): decoupling supply nets, switch nodes and switcher
`vin_net`s, crystal nets, pair nets, every 2-pad net with `octi_dist` ≤ 10 mm between its
pads; ground nets never; sorted. **Overrides**: `ClassOverride` rows replace `part_class`
(unknown ref → recorded in `overrides_applied` as `"<ref>: unknown ref"`); a cap overridden
away from `decoupling_cap` leaves `decoupling`; every applied override listed. Output order is
deterministic (refs natural-sorted, nets sorted).

- [ ] **Step 1: Failing unit tests** (`test_classify.py`; `builders_circuit.py` adds
  `ic(ref, pins: dict[str, str], at)`, `cap/res/ind(ref, net_a, net_b, at)`, `xtal(...)`,
  `conn(...)` on top of P1a's `builders`): `test_ground_patterns` (`GND`, `/AGND`, `PGND_1`,
  `VSS`; `GNDA` ground, `SIGNAL` not); `test_primary_ground_most_pads`; `test_supply_patterns`
  (`+3V3`, `3V3`, `VDD_IO`, `+12V`, `VBUS`); `test_supply_structural` (`RAIL` with 4 pads on 3
  parts and a cap to ground); `test_decoupling_and_served_pin` (two VDD pins, cap nearer to
  pin 7 → `pin == "7"`); `test_switcher_switch_node` (anchor + inductor on `SW`, bootstrap cap
  VBST–SW tolerated; in/out caps and FB resistors found; in/out caps not in `decoupling`);
  `test_not_switcher_with_third_part` (a resistor on `SW` → no switcher);
  `test_out_caps_by_sheet` (buck on sheet `power`, MCU on `/` sharing `+3V3`: caps on `power`
  → `out_caps`, caps on `/` → MCU decoupling); `test_crystal_load_caps`;
  `test_diff_pair_suffixes` (parametrised over the four suffix pairs; `USB_DP`/`USB_DN` via
  `P/N`); `test_no_pair_without_common_anchor`; `test_connector_by_prefix_and_lib`;
  `test_module_with_antenna_rule_area` (a part built with `rule_areas=(antenna_poly,)` →
  `module_with_antenna`); `test_module_board_keepout_in_courtyard` (no `rule_areas`, a
  `model.zones` keepout inside the courtyard → `module_with_antenna`);
  `test_no_rule_area_not_module` (same part without either → not `module_with_antenna`);
  `test_holes_fiducials_testpoints`;
  `test_blocks_per_anchor_by_sheet` (two sheets, satellites follow their anchor, name
  `power-U7`); `test_leftover_joins_most_shared`; `test_critical_default` (2-pad net 9.9 mm →
  critical, 10.1 mm → not; GND never); `test_override_cap_to_other`;
  `test_override_unknown_ref`; `test_deterministic` (shuffled part order → equal result).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): generic part and net classification`.

### Task 4: Metrics M1–M14 (B-C7)

**Interfaces:** B-C7 exactly; `connections`, `routed_length` (Dijkstra over the net's
track/via graph, track endpoints joined at 0.002 mm, a pad joins every track end inside it,
vias join layers at 0 cost). Constants (spec §12 verbatim; hard ones never tuned):
`HARD = {"M4_angle_tol_deg": 0.5, "M5_ratio": 2.0, "M5_extra_mm": 5.0, "M8_mm": 5.0}`;
`REVIEW = {"M5_median": 1.15, "M5_any": 1.5, "M5_critical": 1.25, "M6_diag_frac": 0.5,
"M7_bends_per_10mm": 1.0, "M8_mm": 3.0, "M9_in_cap_mm": 3.0, "M9_inductor_mm": 3.0,
"M9_sw_copper_mm": 5.0, "M10_ratio": 3.0, "M11_fill": 0.85, "M12_mismatch_mm": 1.0}`.
`stage = "routed"` when any track exists on a non-ground net. Definitions per metric:
M1 = not placed + courtyard overlaps (same side, `polygons_overlap`) + SMD parts on a side
∉ `doc.allowed_sides` + parts without courtyard (module constant `M1_EDGE_OVERHANG = False`:
when the owner enables it (index "Provisional" 2), a part listed in `board.md` → Placement
edges may overhang its assigned edge only; test both settings); `n/a` when `outline.kind == "none"`. M2 =
`drc.unconnected` (`n/a` without DRC). M3 = error markers (except unconnected) of the API
`DrcResult` + parity markers (CLI, full check only; holes placed by `pcb_set_outline` carry
`not_in_schematic` and produce none) + API warnings not accepted; a warning is accepted when its normalised type equals a row's
`drc_type` and the refs of its KIIDs ⊆ the row's `items` (non-empty); accepted ones →
`review` and listed. M4 = `analyse_tracks` non-octilinear + ninety + acute + off_angle +
T-junctions.
M5 per routed connection of a non-ground, non-supply net with `octi_dist` ≥ 0.5 mm: ratio
and extra; fail when any ratio > 2.0 **and** extra > 5 mm. M6 routed length > 0.5 × board
diagonal → review. M7 hard = micro-jogs + short chamfers; review = average bends per 10 mm
over routed signal nets ≥ 1 mm. M8 per decoupling (cap supply pad → served pin) and per
crystal (crystal pad → `ic` pad, each crystal net): placement stage = `octi_dist`, routed =
`routed_length` (estimate when unrouted); other side → fail; > 5 mm → fail only at stage
`routed`; > 3 mm → review (spec §12 note). M9 per switcher (in-cap VIN pad → IC VIN pin,
inductor → SW pin, switch-node copper = track length on `sw_net` or the estimate) → review.
M10 per block (`blk:*` groups, else `cls.blocks`): convex hull area / Σ courtyard areas
(> 3.0 review), intrusions = other placed parts overlapping the hull on the same side.
M11 per inner layer with a ground zone: fill ratio = area of the zone's `Zone.filled`
polygons (B-C2, `polygon_area` sum; `filled_area_mm2` as fallback when `filled == ()`) /
outline area (< 0.85 review); **void crossings** = signal track segments on an adjacent
layer that cross a void of that plane (a part of the segment inside the outline but in no
filled polygon of the zone), one offender per (net, void). Computed exactly, without point
sampling: the edges of the zone's filled polygons go into a uniform grid of buckets (cell
2 mm, keyed by edge bbox); a segment first takes a bbox prefilter against each polygon, then
a segment–polygon test — a crossing exists when the segment intersects a polygon edge (taken
from the buckets its bbox touches) or an endpoint lies inside the outline and outside every
filled polygon (point-in-polygon only for the two endpoints); `n/a` without plane zones; an unfilled plane zone (`filled == ()` and
`filled_area_mm2 == 0`) → review "zone not filled". M12 width < class `track_width` −
0.001 → fail; pair length mismatch > 1 mm → review. M13 every `EdgeRow` ref with an edge:
courtyard within 0.5 mm of that outline edge or overhanging it, else fail; tracks/vias inside
a module's keepout rule area (`Part.rule_areas` of `module_with_antenna` parts, plus board
keepouts inside their courtyard) → fail. M14 (`preset` given, else `n/a`): board rules below the
preset, track width, via drill/diameter/annular, PTH drill < `min_drill`, NPTH (pad with drill
and no copper layer) < `min_npth`, copper (track/via/pad) to outline < `copper_to_edge`,
**hole to edge**: every via drill and pad hole, measured from the hole edge (centre
distance to the outline − drill/2), < `copper_to_edge` (0.3 mm; no separate preset field),
visible silk text height/line below `silk_min_height`/`silk_min_line`, **silk over pads**:
a visible silk text (bbox from `at`, `size` and character count, rotated) or a silk graphic
(`model.graphics` on `F/B.SilkS`, segment inflated by `width/2`) overlapping the mask
opening of a pad on the same side (pad rectangle of a pad whose `layers` contain
`F.Mask`/`B.Mask`) — pads are bucketed in a uniform grid (cell 2 mm, keyed by mask-opening
bbox) per side; each silk item is tested only against the pads of the buckets its bbox
touches, by exact polygon/segment-vs-rectangle intersection (no sampling), SMD pads without `F.Paste`/`B.Paste`; an offender whose refs all appear in a custom rule condition of
`dru_text` → exception (review, listed). Offenders sorted by value desc then text, capped at
20 + `omitted`. Verdict per metric: fail > review > pass; `n/a` when inputs are missing.
`Zone.filled` polygons are used **only** by M11: when M11 is not requested, the caller strips
them before the job is pickled (`jobs.strip_fills(model)` = zones with `filled=()`, run
in-process before `run_cpu`), so `metrics_job` never ships or reads fill geometry it does
not need.

- [ ] **Step 1: Failing unit tests**, one hand-made geometry with a known answer per metric:
  `test_metrics_placement.py`: `test_m1_outside_by_001` (1), `test_m1_overlap_same_side_only`
  (top/bottom overlap → 0), `test_m1_bottom_smd_not_allowed` / `_allowed`, `test_m1_na_without_outline`;
  `test_m8_estimate_review_at_3_1` (placement, 3.1 mm → review, not fail),
  `test_m8_other_side_fails`, `test_m8_crystal`; `test_m9_hot_loop_review`;
  `test_m10_ratio_and_intrusion` (block hull 30 mm² over 9 mm² courtyards → 3.33 review; a
  foreign part inside → 1 intrusion); `test_stage_detection`.
  `test_metrics_routed.py`: `test_connections_mst` (4 pads on a line → 3 connections);
  `test_routed_length_via_zero`; `test_m4_counts` — each case an isolated polyline on its
  own net, far from the others, pads at both ends: (a) two octilinear segments meeting at
  90° → M4 == 1; (b) one single straight segment 0.6° off the x axis → M4 == 1 (one
  non-octilinear segment, no vertex); (c) a clean octilinear 45° polyline → M4 == 0; all
  three together on one board → M4 == 2 exactly;
  `test_m5_fail_needs_both` (ratio 2.5 with 4 mm extra → not fail; 2.1 with 6 mm → fail);
  `test_m5_median_review`; `test_m6_long`; `test_m7_counts_and_bends_per_10mm`;
  `test_m8_routed_5_1_fails`; `test_m11_fill_and_crossing` (zone with `filled` polygons
  covering 80 % of the outline → review 0.80; a signal track on `F.Cu` crossing a void of the
  `In1.Cu` GND plane → 1 crossing offender; the same track over solid fill → 0);
  `test_m11_unfilled_zone_review`; `test_m4_off_angle_counts` (one 60° vertex → M4 value 1,
  verdict fail); `test_m12_width_and_mismatch`;
  `test_m13_track_in_module_rule_area` (track inside a part's `rule_areas` polygon → fail).
  `test_metrics_rules.py`: `test_m2_m3_from_drc` (synthetic `DrcResult`: 1 error, 2 warnings, 1
  accepted by a `board.md` row → M3 value 2, accepted listed); `test_m3_parity_counts`;
  `test_m13_edge_row` (0.4 mm from edge pass, 0.6 fail, overhang pass);
  `test_m14_each_rule` (parametrised: track 0.12 on `jlcpcb-4l`, via 0.25 drill, annular
  0.14, NPTH 0.4, track 0.25 mm from the edge, an **NPTH** hole (pad with drill 1.0 and no
  copper layer, so no copper-to-edge rule applies) whose hole edge is 0.25 mm from the edge →
  the offender's `what` names `hole_to_edge`, silk 0.8 mm text, silk text over an SMD pad's
  `F.Mask` opening (`silk_over_pad`), silk line crossing a pad, SMD pad without paste) → fail;
  `test_m14_silk_beside_pad_ok` (silk 0.1 mm clear of the mask opening → pass);
  `test_m14_hole_edge_at_0_3_ok` (the same NPTH hole with its hole edge 0.3 mm and 0.35 mm
  from the edge → pass, no offender whose `what` names `hole_to_edge`); `test_m14_custom_rule_exception` (offender ref named in a `dru_text` rule → review);
  `test_m14_na_without_preset`; `test_offender_cap_20` (25 offenders → 20 + `omitted == 5`);
  `test_only_subset`; `test_hard_thresholds_verbatim` (the `HARD` dict equals the literal
  above).
- [ ] **Step 2: Run** `uv run pytest tests/unit/board -q` — FAIL. **Step 3: Implement**
  (pure; no kipy import, asserted like P1a's `test_no_kipy_import`). **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): metrics M1-M14 with spec thresholds`.

### Task 5: `pcb_analyze` and `pcb_metrics` (read tools)

**Interfaces:** `analysis.py`: `async pcb_analyze(ctx, args) -> ToolResult`, `async
pcb_metrics(ctx, args) -> ToolResult`, `preset_for(model, doc) -> FabPreset | None` (B-C6:
`PRESETS[doc.preset]` when it names a known preset, else
`PRESETS.get(f"jlcpcb-{len(model.copper_layers)}l")`; P1a's `cc/board/presets.py`). The
worker functions `analyse_job(model, doc)` / `metrics_job(model, doc, drc, preset, only,
parity=None)` live in the new module **`cc/board/jobs.py`** (`metrics_job` builds the
`Classification` itself — `classify(model, doc)` in the worker — and passes `parity`
through to `compute_metrics`; no caller runs `compute_metrics` directly in `run_cpu`; plus
the in-process helper `strip_fills(model)`) (module-level, so `run_cpu`'s
`copperchat.board.` check and spawn pickling hold; P4 appends `place_block_job` /
`legalize_job`, P5b/P6b may append) and run through `run_cpu(..., timeout_s=30)`. Specs in `specs_analysis.py`, appended to
`board/specs.py`'s `TOOL_SPECS` (read tools: `mutating=False`, never take the edit lock, cost
0). `pcb_analyze` schema `_obj({"scope": {"enum": ["parts","nets","blocks","all"]}})`, default
`all`; result: `parts` (ref → class), `nets` (net → kind), `decoupling`, `switchers`,
`crystals`, `pairs`, `blocks`, `critical_nets`, `overrides_applied`, `board_md_errors`; each
list ≤ 500 entries + `omitted`. `pcb_metrics` schema `{"only": {"type": "array", "items":
{"enum": ["M1",…,"M14"]}, "maxItems": 14, "uniqueItems": true}}`; it runs `run_drc_task`
(`session.call(..., timeout=drc_call_timeout(60))`, B-C4) unless `only` excludes M2 and M3; parity is `None` (full check only, P3b);
preset from `preset_for`; result: `stage`, `metrics` (id, value, verdict, hard, review,
offenders, omitted), `hard_failures`, `reviews`, `placement_hash`, `board_hash`, `seconds`.
Both: `require_board` → `no_board`.

- [ ] **Step 1: Failing unit test** `test_specs_registered` (both in the registry, in
  `PCB_READ`, schemas `additionalProperties: false`, `only` rejects `"M15"`);
  `test_preset_for_doc_then_layers` (`doc.preset == "jlcpcb-6l"` on a 4-layer model → 6l;
  no doc preset → 4l; unknown id → by layer count); `test_jobs_module_level`
  (`analyse_job.__module__ == "copperchat.board.jobs"`); `test_metrics_job_parity_passthrough`
  (called in-process with one parity marker → M3 counts it; with `parity=None` → not);
  `test_strip_fills` (zones keep everything but `filled == ()`; M11-free metrics equal on the
  stripped and the original model).
- [ ] **Step 2: Failing kicad tests** (`test_analysis_synth.py`, `synth_board`):
  `test_classification_matches_circuit`: U2 `switching_regulator` with inductor L1, `sw_net`
  ending in `SW`, `in_caps == ("C7",)`, `out_caps == ("C8","C9")`, `fb_parts == ("R5","R6")`;
  Y1 crystal with `ic == "U1"` and load caps C4, C5; one pair whose nets end in `USB_DP` /
  `USB_DN`; C1, C2, C3 decoupling to U1 on `+3V3`-ending nets; J1 `connector`; H1, H2
  `mounting_hole`; GND `ground`, VBUS and +3V3 `supply`; blocks for U1 and U2 on sheets
  `root` / `power`; `test_classification_golden` (full `pcb_analyze` result minus hashes ==
  `goldens/sp4_synth_classification.json`, `UPDATE_GOLDENS=1` writes it);
  `test_analyze_boardless` (`no_board`). `test_metrics_tool.py`: `test_metrics_on_synth`
  (stage `placement`, M1 `n/a` without outline, M2 = DRC unconnected count, M4 value 0);
  `test_metrics_budget`: one warm-up call, then 5 calls; median wall time < 2.0 s (CI-generous;
  spec target 0.1–0.5 s), `record_property("pcb_metrics_s", …)`; `test_metrics_only_m4`
  (no DRC task run: spy on `run_drc_task`).
- [ ] **Step 3: Run** `uv run pytest -m kicad tests/kicad/board/test_analysis_synth.py
  tests/kicad/board/test_metrics_tool.py` (kicad-base image) — FAIL. **Step 4: Implement**;
  generate the golden once with `UPDATE_GOLDENS=1` and review it by hand against the P1b
  circuit table. **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): pcb_analyze and pcb_metrics`.

### Task 6: Docs + index as built, checks, PR

- [ ] Index "As built (P3a)": measured `pcb_metrics` time on synth (median, warm), the
  switcher bootstrap refinement, the DRC type normalisation (API enum name vs CLI JSON type),
  `preset_for`, `cc/board/jobs.py`, the `synth_board` helper, `primary_ground`, any review threshold tuned (none
  planned). CHANGELOG `[Unreleased]`: "Board analysis and quality metrics (`pcb_analyze`,
  `pcb_metrics`) — behind COPPERCHAT_BOARD_TOOLS."
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, and `uv run pytest -m kicad tests/kicad/board` in the kicad-base image
  (all with `timeout`).
- [ ] PR `board phase P3a: analysis and metrics`; review-loop per the index's Phase Conventions.

## Acceptance (P3a)

- [ ] CI `backend` and all 8 `kicad (…, i/8)` shards green.
- [ ] `test_t_junction_90_fails`, `test_short_chamfer_fails`, `test_pad_entry_only_exempt_at_pad`,
  `test_valid_chamfer_passes`, `test_micro_jog_fails`, `test_acute_135_fails`,
  `test_vertex_90_8_fails`, `test_vertex_60_fails`, `test_vertex_46_fails`,
  `test_off_by_0_6_deg_fails` / `test_off_by_0_4_deg_passes` pass.
- [ ] Every metric M1–M14 has at least one known-answer unit test; `HARD` equals the spec values.
- [ ] `test_classification_matches_circuit` and the classification golden pass on `sp4_synth`.
- [ ] `pcb_metrics` timing on synth recorded in the index.
