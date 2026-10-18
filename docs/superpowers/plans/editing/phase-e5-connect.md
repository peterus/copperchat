# Phase E5 — Connect, labels, power, no-connect, rename, reroute

> Index contracts: E-C4–E-C8. Spikes: 2A (Q3 connectivity rules: T-joints connect
> without junction, wire over a foreign pin end connects it, label anchored on a pin
> end connects; Q5 junction dropped when not on a T), 2D (failure modes 3, 4, 5;
> T-joins; rail misuse guards; multi-pin atomic calls). Reference code:
> `spikes/2d-agent-eval/geo.py` (`route`, `path_problems`, `simplify`),
> `tools.py` (`sch_connect`, `sch_add_label`, `_stub_and_label`, `sch_add_power`,
> `_power_plans`, `sch_no_connect`, `anchors`).

**Goal:** connections by computed orthogonal wires (incl. T-joins onto the target
net), labels and power symbols at pins, no-connects, net rename and route touch-up —
all atomic per call and verified against the live netlist.

**Files:**
- Create: `backend/src/copperchat/kicad_tools/edit/{router,connect,labels,specs_connect}.py`
- Modify: `tools/registry.py` (provider `copperchat.kicad_tools.edit.specs_connect`)
- Tests: `tests/unit/edit/test_router.py`, `tests/kicad/edit/test_connect.py`, `test_labels_power.py`,
  `test_rename_reroute.py`

---

### Task 1: Router

**Interfaces:** Produces `router.route(a: PinEnd, b: PinEnd | WireTarget, scene: Scene,
netlist: Netlist, *, allowed_nets: set[str], max_segments: int = 3) -> Route | None`,
`Route(points: list[tuple[float,float]], junction_at: tuple | None)`, `WireTarget(uuid, a, b, net)`,
`router.route_problems(points, scene, netlist, allowed_nets) -> list[str]`.

- [ ] **Step 1: Failing unit tests** (scenes built from dataclasses, no KiCad):
  `test_straight_and_l_routes`; `test_avoids_symbol_bodies`; `test_never_crosses_foreign_pin_end`
  (Review Focus 1: a pin end on the straight path forces a detour or `None`);
  `test_t_join_onto_target_net_wire` (route ends on the interior of a wire of the target net,
  `junction_at` set); `test_items_of_joined_nets_are_contacts_not_obstacles`;
  `test_perpendicular_stub_when_corridor_blocked`; `test_none_beyond_three_segments`.
- [ ] **Step 2:** FAIL. **Step 3: Implement** (port of `geo.route` with T-join targets and the
  3-segment limit of spec §5.2). **Step 4:** PASS. **Step 5:** commit `feat: orthogonal router with T-joins`.

### Task 2: `sch_connect`

**Interfaces:** tool `sch_connect(from: "REF.PIN", to: "REF.PIN" | <net name>, via: "auto"|"wire"|"label" = "auto", net_name?)`
→ standard result + `{route: {points, junction}, via_used, net}`. Logical 1.

- [ ] **Step 1: Failing kicad tests** (`blank_session` with parts placed through E4 tools in the
  test setup):
  - `test_wire_between_two_pins` (verified merge; `kicad-cli` netlist agrees).
  - `test_t_join_to_existing_net` (`J1.1–U1.VI` wired, then `sch_connect(C1.1, U1.VI)` → joins
    the existing wire, a junction exists after read-back, one net; 2D failure 4).
  - `test_to_net_name_routes_when_close` (`to="VIN"` with a VIN wire within 25.4 mm → wire),
    `test_to_power_net_places_power_symbol` (`to="GND"` → power symbol, no label named GND).
  - `test_auto_falls_back_to_labels` (blocked geometry → `via_used="label"`, both pins on net
    `net_name` or a generated `N_<ref>_<pin>`), `test_wire_only_fails_with_suggestion`
    (`code="no_route"`, suggestion: move part / `via=label`).
  - `test_merging_two_named_nets_refused` (pins already on `SDA` and `SCL` → `invalid_argument`,
    nothing changed).
  - `test_off_grid_or_near_miss_impossible` (every created point on grid; verification
    asserts exact pin ends).
- [ ] **Step 2:** FAIL. **Step 3: Implement** prepare: resolve pins (E2 `resolve_pin`, both on
  the same sheet for wires; different sheets → labels of kind `global` unless the user named a
  hierarchical path), route, else label plan; stages: one `Create` (wires + junction or
  stubs + labels). Junctions only on real T's (KiCad drops others, 2A Q5); verify merge.
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: sch_connect`.

### Task 3: `sch_add_label`, `sch_add_power`, `sch_no_connect` (atomic, multi-pin)

**Interfaces:** tools `sch_add_label(net, at: ["REF.PIN", …] | [x, y], kind: "local"|"global"|"hierarchical" = "local")`,
`sch_add_power(net, at: ["REF.PIN", …], symbol?)` (`net="PWR_FLAG"` → `power:PWR_FLAG`),
`sch_no_connect(pins: ["REF.PIN", …])`. Logical 1 per pin.

- [ ] **Step 1: Failing kicad tests**:
  - `test_label_stub_per_pin_all_on_net` (3 pins → 3 stubs + 3 labels, labels readable outward).
  - `test_multi_pin_is_all_or_nothing` (3rd pin blocked → error naming that pin, **no** item
    created for pins 1–2, no snapshot; 2D failure 3; Review Focus 4).
  - `test_rail_name_label_refused` (`net="GND"` or an existing power net → suggestion
    `sch_add_power`; 2D failure 5).
  - `test_power_symbol_upright_and_numbered` (`+3V3` on two pins → two `power:+3V3` symbols
    pointing up, GND pointing down, refs `#PWR01`, `#PWR02` via `Allocator.next_power`).
  - `test_power_from_free_stub_end_when_pin_wired`.
  - `test_pwr_flag` (`net="PWR_FLAG"` on a rail pin → ERC `power_pin_not_driven` gone in a
    following `run_erc`).
  - `test_no_connect_on_pin_end`; `test_no_connect_on_connected_pin_refused`.
  - `test_label_at_position_must_touch_wire` (position not on a wire/pin end → refused before
    write; Review Focus 1).
- [ ] **Step 2:** FAIL. **Step 3: Implement** plans for all targets first, then one `Create`
  stage; power symbols via `Place` stages (stage 1 place all, stage 2 refs/field positions).
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: labels, power symbols, no-connects`.

### Task 4: `sch_rename_net` and `sch_reroute`

**Interfaces:** tools `sch_rename_net(old, new)` (all labels of the net; logical 1),
`sch_reroute(wires: [uuid…] | {from: "REF.PIN", to: "REF.PIN"}, points: [[x,y],…])` (logical 1).

- [ ] **Step 1: Failing kicad tests**: `test_rename_local_net_all_labels` (2 labels → new
  name, netlist net renamed, members identical); `test_rename_refuses_power_and_conflicts`
  (power net → refused; new name = existing global/hierarchical net → refused, nothing
  changed); `test_reroute_keeps_net_set` (replace an L route by a Z route → partition
  identical, old segments gone, new on grid); `test_reroute_touching_foreign_pin_refused`
  (Review Focus 1; error lists the connection that would change, document unchanged).
- [ ] **Step 2:** FAIL. **Step 3: Implement** (`keep_partition=True` for reroute; rename =
  `Update` of label texts on every sheet instance holding them).
- [ ] **Step 4:** PASS. **Step 5:** commit `feat: sch_rename_net and sch_reroute`.

---

**Done when:** one PR, CI green, review-loop done.
