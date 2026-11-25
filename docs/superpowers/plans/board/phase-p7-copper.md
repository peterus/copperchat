# Phase P7 — Planes, fanout, stitching, silk: `pcb_zones`, `pcb_fanout`, `pcb_stitch`, `pcb_tidy_silk`

> Index contracts: B-C2, B-C4 (multi-stage, `BoardStage.undo`, `fail_after`), B-C6
> (`classify` → ground net), B-C7 (`compute_metrics(only={"M11"})` via
> `run_cpu(metrics_job, …)`), B-C8 (`FabPreset`
> silk values), B-C10 (`require_placement_approved`), B-C14; index calls 6, 8. Spec §7.2
> "Copper", §8.3 (placement checkpoint), §10.3 step 5 (fanout/stitch **locked**), §11, §15.3
> D1, §18.2. Spike 4A Q5 (`spikes/4a-board/fanout.py`, port, never import). **Wave 7: starts
> when P5b is merged**; parallel to P6b.

**Goal:** the agent pours the default GND planes (owner D1) and outer pours, connects every
SMD ground pad by a short locked 45° stub + via (no via-in-pad), stitches the planes with
locked vias, and tidies reference designators — each tool DRC-legalised, compensated on
failure, with M11 plane metrics in its report.

`cc/` = `backend/src/copperchat/`.

**Files:**
- Create: `cc/board/planes.py`, `cc/board/silk.py` (pure reference placement),
  `cc/kicad_tools/board/{zones,fanout,stitch,silk,specs_copper}.py`
- Modify: `cc/kicad_tools/board/specs.py` (append four specs), `cc/board/jobs.py` (append
  `fanout_candidates_job`, `stitch_candidates_job`)
- Tests: `backend/tests/unit/board/{test_planes,test_silk_plan,test_copper_schemas}.py`,
  `backend/tests/kicad/board/{test_zones,test_fanout,test_stitch,test_tidy_silk}.py`
  (+ append a module-scoped `copper_rig` fixture to `tests/kicad/board/conftest.py` that calls
  P5b's shared `routed_synth` — no own copy of the setup sequence)

**Long KiCad calls** (index Global Constraints / B-C4 — `session.call`'s 30 s default
restarts the api-server on expiry): every `refill_zones` in this phase (`pcb_zones` stage 2,
fanout `round_i_refill`, stitch stages 2 and 4, every `undo` that refills) runs as
`session.call(lambda h: h.board().refill_zones(block=True, max_poll_seconds=REFILL_TIMEOUT_S),
timeout=REFILL_TIMEOUT_S + 15)` with `REFILL_TIMEOUT_S = 60` (module constant in
`zones.py`, shared by `fanout.py` / `stitch.py`); every DRC goes through `sctx.drc(...)`
(which passes `drc_call_timeout`, P1a) — never a bare `session.call(run_drc_task)`.
Test (`test_zones.py`): `test_refill_passes_explicit_timeout` (spy on `session.call`: each
refill call has `timeout == REFILL_TIMEOUT_S + 15 == 75`, each DRC call `timeout ==
drc_call_timeout(t)`, none `None`).

**Test board (`copper_rig`):** `synth_session` + `board_ctx` → P5b's shared
**`routed_synth(rig)`**: sync → `pcb_set_outline` (60 × 45 mm, r = 1, 4 holes) →
`pcb_set_rules {layers: 4, preset: "jlcpcb-4l", net_classes: [Power]}` (4 copper layers
explicit; In1.Cu must exist for the default plane stack) → blocks + `pcb_place_block` per
block → `pcb_legalize` → render placement + `pcb_check quick` → placement approved through the
real `pcb_approve` flow → `pcb_route` on the critical nets (the checkpoint exists before
`pcb_route`). Same rig as P6b's `fill_rig` without the fill. pytest-timeout counts
fixture setup against the first test (ini limit 180 s): modules using the rig mark their first
test `@pytest.mark.timeout(480)` if the measured build time needs it (recorded).

---

### Task 1: Pure candidates — `cc/board/planes.py`

**Interfaces:**
```python
@dataclass(frozen=True) class ViaCandidate: at: Point; stub: tuple[Point, Point] | None
                                            pad: tuple[str, str] | None   # (ref, number)
                                            rank: int                     # 0 = preferred
@dataclass(frozen=True) class ZoneSpec: name: str; net: str; layers: tuple[str, ...]; priority: int
def default_planes(model: BoardModel, cls: Classification) -> tuple[ZoneSpec, ...]
def fanout_candidates(model, net, *, via: tuple[float, float], width: float, clearance: float,
                      max_stub: float = 1.0) -> dict[tuple[str, str], tuple[ViaCandidate, ...]]
def stitch_candidates(model, net, *, via: tuple[float, float], clearance: float,
                      pitch: float = 5.0, edge_ring: bool = False) -> tuple[ViaCandidate, ...]
```
- `default_planes` (owner D1, spec §11; ground net = the classification's ground net with the
  most pads): 4 layers → `GND_In1` (In1.Cu, priority 10, solid plane), `GND_F` (F.Cu, 1),
  `GND_B` (B.Cu, 1) — outer pours below the plane; 2 layers → `GND_B` (B.Cu, 10), `GND_F`
  (F.Cu, 1); 6 layers → `GND_In1`, `GND_In4` (10) + the two outer pours (plan call, same as
  P6b's plane-layer default).
- `fanout_candidates`: SMD pads of `net` only (THT pads reach the planes through their hole);
  8 directions (0°, 45°, …, 315°) × 2 distances `d1 = pad half-extent along the direction +
  clearance + via_d/2`, `d2 = d1 + 0.5` (spike); the stub runs pad centre → via centre and is
  exactly octilinear; candidates whose stub part outside the pad exceeds `max_stub` (spec:
  ≤ 1 mm) are dropped; also dropped: via circle overlapping **any** pad (no via-in-pad, spec
  call 30), other-net copper inflated by `clearance`, a via/track keepout, or the outline
  inset by `copper_to_edge + via_d/2`. Order: rank by distance, then direction away from the
  footprint centre first.
- `stitch_candidates`: grid at `pitch` (1–50 mm) aligned to the outline origin + `pitch/2`,
  inside the outline inset by `copper_to_edge + via_d/2 + 0.25`; `edge_ring` adds points every
  `pitch` along the outline inset by `copper_to_edge + via_d/2 + 0.5`; same filters plus not
  inside any courtyard; a point counts only where ≥ 2 zones of `net` on different layers
  cover it (else it would dangle).
Deterministic (sorted keys, no randomness), no kipy.

- [ ] **Step 1: Failing unit tests** (`test_planes.py`, builders): `test_default_planes_4l`
  (three specs exactly as above), `test_default_planes_2l`, `test_default_planes_6l`;
  `test_fanout_16_candidates_free_pad` (isolated 1 × 0.6 mm pad → 16 candidates, every stub at
  a multiple of 45° within 1e-9, outside-pad stub length ≤ 1.0); `test_fanout_no_via_in_pad`
  (neighbouring pads of a 0.65 mm-pitch row → no candidate circle overlaps a pad);
  `test_fanout_max_stub` (`max_stub=0.3` drops `d2`); `test_fanout_skips_tht`;
  `test_stitch_grid_count` (50 × 40 mm, pitch 5, two full GND zones → 10 × 8 points minus
  filtered); `test_stitch_edge_ring` (ring points lie on the inset rectangle ± 1e-6);
  `test_stitch_needs_two_zone_layers` (one zone layer → no candidates); `test_deterministic`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/board/test_planes.py -q` — FAIL. **Step 3:
  Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): plane, fanout and stitching candidate generation`.

### Task 2: `pcb_zones`

**Interfaces:** schema: `action` ∈ `add|update|delete` (required); `zones` ≤ 16 items
`{name (call-6 regex), net (maxLength 256), layers (1–32, enabled copper layers), outline:
"board" | {polygon: [{x, y}] 3–256}, priority (0–100, default 0), clearance (0 < v ≤ 10,
default: the net's class clearance), thermal {gap, spoke_width} (0 < v ≤ 5)}`; `update` needs
`name` + any field, `delete` needs `name` only. `action: "add"` with `zones` omitted →
`default_planes`. Cost `Logical(len(zones))` (1 per zone), kind `board_zones`; requires the
placement approval (spec §8.3). Refusals: unknown net/layer/zone → `not_found` +
`closest`; `add` of an existing name → `invalid_argument` ("use update"); `outline: "board"`
with `outline.kind != "rect"` → `invalid_argument`. Stages: (1) create/update/delete in one
commit, `undo` restores the updated/deleted zones from their stored pre-call data; (2) refill
(`refill_zones`, blocking, `commit=False`); a refill after a compensation is done by stage 1's
`undo`. Report: per zone `{name, kiid, layers, priority, filled_area_mm2}`, M11 (`run_cpu(metrics_job,
model, doc, None, None, only=frozenset({"M11"}), timeout_s=30)` — the model keeps its fills,
M11 needs them; never a direct `compute_metrics`: fill ratio per plane layer, crossings), `next: "pcb_fanout"`.

- [ ] **Step 1: Failing unit tests** (`test_copper_schemas.py`): bounds and `additionalProperties`
  at every level for all four tools (zones: 257-point polygon, priority 101, `thermal.gap: 0`;
  fanout `max_stub: 5`; stitch `pitch: 0.5`, `pitch: 51`).
- [ ] **Step 2: Failing kicad tests** (`test_zones.py`, `copper_rig`): `test_default_stack_4l`
  (three zones named as Task 1, In1.Cu filled area > 0, report M11 fill ratio for In1.Cu
  ≥ 0.85 recorded); `test_add_update_delete` (polygon zone added, its clearance updated 0.2 →
  0.3 and read back, deleted); `test_add_existing_name_refused`; `test_unknown_net`
  (`not_found`, `closest`); `test_zones_m11_via_run_cpu` (spy: one `run_cpu(metrics_job,
  …, only=frozenset({"M11"}))`, model with fills); `test_requires_placement_approval` (`pcb_move {items: [{ref:
  <a part with critical copper>, x: <x + 0.5>}], unroute: true}` → the approval is stale →
  `pcb_zones` refuses `placement_not_approved`; History undo restores the rig); `test_cost_per_zone` (3 zones → 3); `test_fail_after_1_restores`
  (an `update` + `delete` call with `fail_after=1` → zone set, settings and `board_hash` equal
  pre-call); `test_metrics_budget_plane_filled` (after `test_default_stack_4l`'s zones are
  filled on the routed rig: `pcb_metrics {}` — all of M1–M14, incl. M11 on the filled
  `In1.Cu` plane and M14 silk-over-pads — returns within **5 s** wall time on CI; the
  measured seconds and the `seconds` field are recorded with `record_property` and in "As
  built (P7)"; this pins P3a's bucketed M11/M14 geometry).
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): pcb_zones with the default GND plane stack`.

### Task 3: `pcb_fanout`

**Interfaces:** schema `{net (required), via?: {diameter (0 < v ≤ 2), drill (0 < v ≤ 1)},
max_stub? (0.1–3, default 1.0)}`; via default = the net's class via, stub width = the net's
class track width. Cost `Logical(2)`, kind `board_fanout`; placement approval required.
Candidates are generated once, before the first stage, off the event loop:
`run_cpu(fanout_candidates_job, jobs.strip_fills(model), net, via=…, width=…, clearance=…,
max_stub=…, timeout_s=30)` (`fanout_candidates_job` appended to `cc/board/jobs.py`; it calls
`planes.fanout_candidates`). Algorithm (spike Q5, DRC-legalised rounds), three stages per
round i = 1…16 (a round with no open pad is a no-op): `round_i_add` (commit: for every open
pad create its next candidate's stub + via, **both `locked=True`**), `round_i_refill`
(`commit=True`, its own **pushed** commit: `refill_zones`, blocking), `round_i_check`
(commit: `sctx.drc()` → remove the items of every candidate whose KIIDs appear in a marker
other than unconnected-items; a via in a `DANGLING_VIA` marker is rejected too; open pads
advance to their next candidate). The DRC therefore only ever sees pushed state — nothing
relies on DRC seeing an open commit (P1a probe).
Pads without any candidate: when a pour of `net` on the pad's layer exists and, after the last
refill, no unconnected marker names the pad → `via_pour`; else `unconnected` with reason
(`no_candidate` = fine-pitch/QFN, or `all_rejected`). Report: `smd_pads`, `fanned_out`,
`via_pour`, `unconnected` [{ref, pad, reason}], `rounds`, `drc_seconds`, M11, `next:
"pcb_stitch"`.

- [ ] **Step 1: Failing kicad tests** (`test_fanout.py`, `copper_rig` after `pcb_zones` default
  stack): `test_fanout_gnd` — every GND SMD pad is in exactly one of `fanned_out` / `via_pour`
  / `unconnected`; `fanned_out ≥ 1`; every created track and via is `locked`; stubs pass M4
  (`analyse_bends` on each stub, pad entry exempt) and their outside-pad length ≤ 1.0 mm; DRC
  task: no marker other than unconnected-items references a created KIID;
  `test_no_via_in_pad` (no created via overlaps any pad polygon); `test_fanout_fail_after_mid`
  (`fail_after=4`, i.e. after round 2's add — stages are add, refill, check per round → KIID
  sets and `board_hash` equal pre-call); `test_fanout_fail_after_refill` (`fail_after=2`,
  after round 1's pushed refill → same equality); `test_fanout_refill_pushed_before_drc`
  (spy on `session.commit` / `run_drc_task`: every DRC call happens after the refill commit
  of its round was pushed, never inside an open commit); `test_fanout_candidates_via_run_cpu`
  (spy: `fanout_candidates_job` runs through `run_cpu` once, with a fill-stripped model);
  `test_fanout_requires_approval`; `test_fanout_cost_2`; `test_fanout_unknown_net`
  (`not_found`, `closest`); `test_m11_in_report`.
- [ ] **Step 2: Run** `uv run pytest -m kicad tests/kicad/board/test_fanout.py` — FAIL.
  **Step 3: Implement.** **Step 4: Run** — PASS; record pads/rounds/seconds on `sp4_synth`.
- [ ] **Step 5: Commit** `feat(board): pcb_fanout with locked DRC-legalised stubs and vias`.

### Task 4: `pcb_stitch`

**Interfaces:** schema `{net (required), pitch? (1–50, default 5.0), edge_ring? (default false),
via?}`. Cost `Logical(2)`, kind `board_stitch`; placement approval required. Candidates:
`run_cpu(stitch_candidates_job, jobs.strip_fills(model), net, via=…, clearance=…, pitch=…,
edge_ring=…, timeout_s=30)` (`stitch_candidates_job` appended to `cc/board/jobs.py`, calls
`planes.stitch_candidates`; the zone outlines it needs are not fill polygons). Module constant
`MAX_STITCH_VIAS = 2000`: more candidates → `invalid_argument` with `details = {candidates:
<would-be count>, max: 2000}` and a suggestion (larger `pitch`, no `edge_ring`), before any
stage runs (board untouched). Stages: (1) add every candidate via `locked=True` (commit);
(2) `refill` (`commit=True`, its own **pushed** commit: `refill_zones`); (3) DRC → remove
vias in any marker other than unconnected-items, and vias in `DANGLING_VIA` markers (spike:
grid vias where no pour exists dangle) (commit) — the DRC sees only pushed state; (4)
refill (commit). Report: `candidates`, `kept`,
`rejected_drc`, `removed_dangling`, `edge_ring` count, M11, `next: "pcb_tidy_silk"`.

- [ ] **Step 1: Failing kicad tests** (`test_stitch.py`, `copper_rig` + default zones +
  fanout): `test_stitch_grid` (`kept > 0`, all vias `locked`, after the call the DRC task has
  no `DANGLING_VIA` and no clearance marker on a created KIID); `test_edge_ring` (with
  `edge_ring: true` more vias than without, ring vias within 0.5 mm of the inset outline);
  `test_stitch_without_two_zone_layers` (only In1.Cu zone → `kept == 0`, report explains);
  `test_stitch_fail_after_1`, `test_stitch_fail_after_2` (after the pushed refill),
  `test_stitch_fail_after_3` (after DRC + remove) (KIID sets, `board_hash` equal pre-call);
  `test_stitch_refill_pushed_before_drc` (spy as for fanout); `test_stitch_cost_2`;
  `test_stitch_too_many_vias` (`pitch: 1` on the 60 × 45 mm rig with `edge_ring: true` →
  candidate count > 2000 → `invalid_argument`, `details.candidates` equals the
  `stitch_candidates` count, `details.max == 2000`, `board_hash` unchanged; or
  `MAX_STITCH_VIAS` monkeypatched to 5 if the rig yields fewer);
  `test_stitch_candidates_via_run_cpu` (spy: one `run_cpu(stitch_candidates_job, …)`).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat(board): pcb_stitch with locked vias and dangling-via removal`.

### Task 5: `pcb_tidy_silk`

**Interfaces:** `cc/board/silk.py`: `@dataclass(frozen=True) class RefPlacement: ref: str; at:
Point; rot: float; layer: str`, `plan_refs(model, *, height: float, line: float, skip:
frozenset[str] = frozenset()) -> tuple[tuple[RefPlacement, ...], tuple[str, ...]]` (placements,
unresolved). Text box estimate `w = len(text) × 0.9 × height + line`, `h = height + line`;
candidates (in order) above, below, left, right of the courtyard bbox at 0.2 mm gap, then the
4 corners; rotation 0° (90° for parts at 90°/270°); layer `F.SilkS` (top parts) or `B.SilkS`
mirrored (bottom); a candidate is free when its box overlaps no pad (any part, +0.1 mm), no
footprint silk graphic, no already placed reference and stays inside the outline; parts in
natural ref order. Tool `pcb_tidy_silk {}`: height = `FabPreset.silk_min_height` (1.0 mm,
index call 8 — not the spec's 0.8), line = `silk_min_line` (0.15). Cost `Logical(1)`, kind
`board_tidy_silk`; no approval needed. Stages, each with an `undo` restoring the stored pre-call
text attributes (position, angle, size, thickness, layer, visible — text attributes are not
items, so the KIID diff cannot restore them): (1) apply the plan + uniform size (commit);
(2) DRC task → references in silk markers (`silk_overlap`, silk over copper/pads, silk to edge)
are re-planned with those candidates excluded (commit); (3) still failing → hidden (`visible
= false`) with a report line each (commit). Report: `moved`, `resized`, `hidden` [{ref,
reason}], `silk_markers_before`, `silk_markers_after`, `board_hash` changed.

- [ ] **Step 1: Failing unit tests** (`test_silk_plan.py`): `test_ref_outside_pads`
  (box overlaps no pad); `test_refs_do_not_overlap` (seeded `random.Random(11)` over 200
  random placements — seed printed on failure); `test_bottom_part_on_b_silk`;
  `test_crowded_part_unresolved` (all candidates blocked → in `unresolved`);
  `test_uniform_size` (all placements use `height`, `line`); `test_deterministic`.
- [ ] **Step 2: Failing kicad tests** (`test_tidy_silk.py`, `copper_rig` after
  zones/fanout/stitch): `test_tidy_silk_synth` (every visible reference has size 1.0 and
  thickness 0.15 and lies on its part's side; DRC silk markers naming a visible reference = 0;
  each hidden ref has a report line); `test_tidy_changes_board_hash`;
  `test_tidy_silk_fail_after_1` and `_2` (`board_hash` equal pre-call — texts restored by the
  `undo`s); `test_tidy_cost_1`.
- [ ] **Step 3: Run** — FAIL. **Step 4: Implement.** **Step 5: Run** — PASS.
- [ ] **Step 6: Commit** `feat(board): pcb_tidy_silk`.

### Task 6: Docs + index as built, checks, PR

- [ ] Index "As built (P7)": the 6-layer
  plane default, `pcb_metrics` seconds on the plane-filled rig (budget 5 s), fanout and stitch counts/seconds on `sp4_synth`, the DRC marker types used
  (exact `DrcErrorType` names for dangling via and silk), whether `refill_zones` inside a
  stage needed its own commit, In1.Cu fill ratio. CHANGELOG `[Unreleased]`: "Board copper
  tools `pcb_zones`, `pcb_fanout`, `pcb_stitch`, `pcb_tidy_silk` — behind
  COPPERCHAT_BOARD_TOOLS."
- [ ] Run `uv run pytest -m "not kicad"`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy src`, `uv run pytest -m kicad tests/kicad/board` in the kicad-base image (with
  `timeout`); `test_every_board_write_has_a_kind` (P1c) covers the four new tools.
- [ ] PR `board phase P7: zones, fanout, stitching, silk`; review-loop per the index's Phase
  Conventions.

## Acceptance (P7)

- [ ] CI `backend` and all 8 `kicad (…, i/8)` shards green.
- [ ] `test_default_stack_4l`, `test_fanout_gnd`, `test_no_via_in_pad`, `test_stitch_grid`,
  `test_tidy_silk_synth` pass on `sp4_synth`.
- [ ] Every multi-stage tool has `fail_after` tests with KIID sets and `board_hash` equal to
  the pre-call state (`test_fanout_fail_after_mid`, `test_fail_after_1_restores`,
  `test_fanout_fail_after_refill`, `test_stitch_fail_after_1/2/3`, `test_tidy_silk_fail_after_1/2`).
- [ ] All fanout/stitching copper is created `locked` (P6b's re-fill keeps it).
- [ ] M11 values and fanout/stitch counts recorded in the index.
