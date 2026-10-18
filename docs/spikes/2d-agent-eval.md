# Spike 2D — agent tool-shape eval

Status: done (2026-10-18). Code, transcripts and renders: `spikes/2d-agent-eval/`. CI does not
run spikes. No hard gate (spec §13); the result informs the final tool list, the default
change budget and the editing skill.

**Tool shape OK, with changes** (see Decision): the high-level shape (place → power/label/connect
by `REF.PIN` → ERC → render) built all three §12 circuits correctly in 13 of 13 runs.
`sch_move`, `sch_delete`, `sch_find_space` and wire T-joins are required. The model must
be told which way a polarised part's pins point after a rotation. The budget must count
logical changes, not KiCad items.

## Question(s)

From the editing spec (`docs/superpowers/specs/2026-10-17-editing-design.md`, §13 row 2D, §12):
with throwaway prototypes of `sch_place_symbol`, `sch_connect` (computed orthogonal route +
label fallback), `sch_add_label`, `sch_add_power`, `sch_set_fields`, `lib_search_symbols`,
`sch_get_pin` and a draft `kicad-schematic-editing` skill, can the current model build the
three §12 scenario circuits from an empty KiCad-created project?

1. LED + series resistor from a 2-pin connector;
2. 3.3 V LDO (AMS1117-3.3) from a 2-pin input connector with 10 µF / 22 µF caps, 0805;
3. ATtiny85 with decoupling, TMP102 I2C sensor connected by labels `I2C_SDA`/`I2C_SCL`,
   4.7 kΩ pull-ups, address pin to GND, unused pins no-connect.

Measured per run: golden connectivity (live `GetSchematicNetlist`), ERC errors, tool calls,
tokens/cost, failure modes, visual quality of the rendered result. Then: is the high-level
shape enough, which tools/parameters are missing or confusing, which default budget fits,
which skill rules matter most.

## Setup

- KiCad `0616634034fa1a8c92c141c293342cbef2f68dff`, kipy `87b945a0` in the pinned image
  `ghcr.io/peterus/copperchat-kicad-base:0616634034…`; headless `kicad-cli api-server`, symbol
  and footprint libraries loaded (`load_all_libraries`, ~8 s). The blank project is created by
  KiCad itself (`CreateDocument` + save). Nothing is hand-edited or text-generated: writes go
  through the IPC API, checks through the live netlist and `kicad-cli` (ERC, PNG).
- Agent: Claude Agent SDK 0.2.168 (bundled CLI), configured like the backend's main agent
  (`sdk_backend.build_options`): built-in tools `["Skill"]` only, an in-process MCP server with
  the prototype tools, a `can_use_tool` guard that allows exactly those, skills passed as a
  local plugin (draft `kicad-schematic-editing` + the built-in `kicad-schematic-style`),
  `setting_sources=[]`, a system prompt adapted from the backend's (editing enabled). One user
  message per run, no follow-up turns. Credentials: the owner's `CLAUDE_CODE_OAUTH_TOKEN`
  (as the live tests use).
- Model: no model is set, as in the backend → the CLI default **`claude-sonnet-5-5`**. One
  comparison run per scenario with `claude-opus-5-5`.
- Tools (all return structured JSON, coordinates in mm on the 1.27 mm grid; mutating ones
  read back the live netlist after the push, compare the pin partition with the intended
  merge, and remove what they created on a mismatch):
  - v1: `lib_search_symbols`, `lib_get_symbol`, `lib_search_footprints`, `sch_list_symbols`,
    `sch_get_pin`, `sch_get_net`, `sch_place_symbol`, `sch_set_fields`, `sch_connect`,
    `sch_add_label`, `sch_add_power`, `sch_no_connect`, `run_erc`, `render_view` (KiCad's own
    PNG plot, cropped, with a mm ruler).
  - v2 = v1 + `sch_find_space`, `sch_delete` (with attached stubs/labels/power symbols),
    `sch_move` (only for parts with no connections yet), `pin_directions_by_rotation` in
    `lib_get_symbol`, misuse guards (no loose `power:` symbols, no labels named like a rail,
    `sch_connect(to=<power net>)` places a power symbol) + skill v2.
  - v2.1 = v2 + geometry fixes made after the v2 runs: one obstacle box per field instead of
    one union box per symbol, the new part's own pin corridors kept free by
    `sch_find_space`, upright power symbols preferred, multi-pin `sch_add_power` all-or-nothing.
- Prompts: user-style text per scenario, plus "the ERC must have no errors and you must have
  looked at the result with render_view". The prompts name the parts (AMS1117, ATtiny85,
  TMP102) and, for scenario 3, the USI pins (`SDA = PB0`, `SCL = PB2`). In the product,
  `parts_get_notes` / `research_part` would supply these. Research is out of scope here.
- Scoring (`scenarios.py`): golden connectivity **by role** (connector / R / LED / regulator /
  caps / MCU / sensor found by `lib_id`, nets compared by pin), so any reference assignment and
  layout passes. **Success = golden + 0 ERC errors.** `golden` is connectivity only (plus the
  rail/label names `+3V3`, `I2C_SDA`/`I2C_SCL` in s2/s3); the requested **values and 0805
  footprints** are checked separately from the recorded components (`checks.py`,
  `values_0805` in `results/summary.md`: all 13 runs match), and the s1 connector rails
  (pin 1 `+5V`, pin 2 `GND`) were checked in the four s1 renders (all correct; `scenarios.py`
  now scores this as `connector_rails` for future runs). Layout checks: symbol box overlaps,
  off-grid items, wire interiors over pin ends.
- Before any model run, `scripted.py` built all three circuits with fixed tool calls
  (golden + ERC 0), so the prototype was validated before the eval.

## Results

`results/summary.md` (13 runs, all on 2026-10-18). Tokens = input + cache creation + cache
read + output. Cost = SDK `total_cost_usd`, a list-price estimate: the OAuth token is a
subscription, so no per-call billing happened.

| Run | Model | Tools | Success | Golden | ERC err/warn | Tool calls | Tool errors | Renders | KiCad items | Logical changes | Tokens | Output tokens | Cost US$ | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| s1-v1-r1 | sonnet-5-5 | v1 | ✅ | 3/3 | 0/1 | 16 | 1 | 1 | 28 | 10 | 80 k | 3.0 k | 0.09 | 34 |
| s1-v1-r2 | sonnet-5-5 | v1 | ✅ | 3/3 | 0/0 | 13 | 0 | 1 | 27 | 10 | 76 k | 2.2 k | 0.06 | 32 |
| s1-v2-r1 | sonnet-5-5 | v2 | ✅ | 3/3 | 0/0 | 18 | 2 | 1 | 24 | 10 | 99 k | 2.8 k | 0.09 | 32 |
| s1-opus-v2-r1 | opus-5-5 | v2 | ✅ | 3/3 | 0/0 | 14 | 0 | 1 | 22 | 10 | 80 k | 2.2 k | 0.16 | 31 |
| s2-v1-r1 | sonnet-5-5 | v1 | ✅ | 7/7 | 0/0 | 14 | 0 | 1 | 34 | 15 | 64 k | 2.1 k | 0.07 | 23 |
| s2-v1-r2 | sonnet-5-5 | v1 | ✅ | 7/7 | 0/0 | 12 | 0 | 1 | 34 | 15 | 72 k | 2.0 k | 0.07 | 24 |
| s2-v2-r1 | sonnet-5-5 | v2 | ✅ | 7/7 | 0/1 | 19 | 1 | 1 | 38 | 16 | 138 k | 2.9 k | 0.09 | 34 |
| s2-opus-v2-r1 | opus-5-5 | v2 | ✅ | 7/7 | 0/0 | 32 | 3 | 3 | 50 | 23 | 319 k | 6.0 k | 0.32 | 79 |
| s3-v1-r1 | sonnet-5-5 | v1 | ✅ | 11/11 | 0/0 | 38 | 14 | 2 | 58 | 31 | 475 k | 7.1 k | 0.21 | 72 |
| s3-v1-r2 | sonnet-5-5 | v1 | ✅ | 11/11 | 0/1 | 43 | 14 | 3 | 60 | 33 | 578 k | 8.6 k | 0.24 | 93 |
| s3-v2-r1 | sonnet-5-5 | v2 | ✅ | 11/11 | 0/0 | 46 | 7 | 3 | 71 | 39 | 502 k | 7.2 k | 0.23 | 76 |
| s3-v2.1-r1 | sonnet-5-5 | v2.1 | ✅ | 11/11 | 0/0 | 21 | 1 | 2 | 54 | 30 | 132 k | 3.2 k | 0.10 | 36 |
| s3-opus-v2-r1 | opus-5-5 | v2 | ✅ | 11/11 | 0/0 | 43 | 3 | 3 | 92 | 51 | 347 k | 7.0 k | 0.40 | 99 |

**Total: 13 runs, 13 successful, US$2.13** (target ≤ 25). Runs per scenario: 3 with the default
model (2 × v1 + 1 × v2), 1 Opus comparison, and for scenario 3 one extra diagnostic v2.1 run
after the geometry fix. In every run the agent loaded the editing skill as its first call and
rendered the result. No run had symbol overlaps, off-grid items or a wire over a pin. No run
had a wrong pin address (0 "no such pin" / "ambiguous" errors in 13 runs).

### Visual quality (final renders, `results/runs/*/final.png`)

- **s1**: v1 runs are correct but awkward. Both placed the LED at 270° (cathode up) and had
  no rotate/delete to fix it, so GND ended up above the LED and the anode wire looped
  around. Both runs said so in their summary. v2 runs used `pin_directions_by_rotation` and
  placed it anode-up (90°): a clean left-to-right loop.
- **s2**: clean in every run: regulator in the middle, input left, output right, GND below.
  Sonnet attaches a power symbol to every pin instead of drawing wires (readable, KiCad-typical).
  Opus wired the output cap to `VO` with a junction.
- **s3**: correct and readable. Labels sit at the MCU and the sensor, pull-ups sit between
  them, unused pins carry no-connects. The decoupling caps are often **not next to the
  pin** they decouple: Sonnet puts them on the rail near the IC, while Opus wires them to
  `VCC`. Field text sometimes touches a body edge (the prototype estimates text width).

### Failure modes (all recovered; no run ended with wrong connectivity)

1. **No way to correct a placement (v1).** Both v1 s1 runs reported "there is no delete or
   rotate tool" and left the upside-down LED. In s3 v1 the agent worked around it with
   misuse: loose `power:GND` symbols placed with `sch_place_symbol` and wired, a local
   label named `GND`, a duplicate GND symbol left on a pin. v2 runs used `sch_delete` (up to 5
   symbols per run; Opus deleted and re-placed U1, U2, C1, C2 in s3 to get rid of a duplicate
   GND symbol left by a partly failed `sch_add_power`, see 3), `sch_move` and `sch_find_space`.
2. **Boxed-in pins → retry loops** (s3 v1: 14 errors per run; s3 v2: 7). The agent placed a
   decoupling cap close to the IC, so the cap's GND pin had no room for a power symbol, then
   tried labels, wires and loose symbols. Root cause was half the agent's placement and half the
   prototype: one union box per symbol (body + field text) blocked free space beside the IC's
   fields. `replay.py` reproduced it; with per-field boxes and pin corridors in
   `sch_find_space` (v2.1) the same scenario took **21 calls / 1 error / US$0.10** instead of 38–46 /
   7–14 / ~0.23.
3. **Partially applied multi-pin calls.** v1/v2 `sch_add_power` committed pin by pin. When a
   later pin failed, the earlier symbols stayed, and the agent's retry duplicated them (s3-v1-r1:
   two GND symbols on one pin). Opus noticed this and switched to one pin per call. This confirms
   spec §5.1: one call = one commit, all checks before `BeginCommit`.
4. **No T-join onto an existing wire.** `sch_connect(C1.1, U1.VI)` after `J1.1–U1.VI` was wired
   failed with "no clean wire route": the router refuses to touch any existing wire, also of the
   target net. The agent fell back to power symbols, or to a second label name on the same net
   (`VIN_5V` on `+5V` → ERC warning `multiple_net_names`). "No clean wire route" occurred in
   6 of 13 runs (s1-v2, s2-v2, s2-opus, s3-v1-r1, s3-v1-r2, s3-v2); only s2-v2 ended with the
   `multiple_net_names` warning.
5. **Rail names as labels.** A local label `+5V` next to a `+5V` power symbol triggered
   `same_local_global_label` (s1-v1-r1); a `GND` label as a workaround appeared in s3-v1-r2. The
   v2 guard refuses this and points to `sch_add_power`.
6. **Blind coordinates.** About 1–2 overlap refusals per run. The agent always followed
   the "nearest free position" suggestion, and `sch_find_space` (v2) replaced the guessing.
7. **ERC discipline worked**: `PWR_FLAG` on externally supplied rails (skill rule) in every
   run, 0 ERC errors in every final state. A second `run_erc` happened in 3 runs after a fix.

### Budget data (§7.1)

- Both counts below are **lower bounds**: the prototype logs a change only after a call's
  verification succeeds, so items left by a partly failed multi-pin call (v1/v2) are not
  counted, and items the agent created and deleted again in the same turn net out. The most
  affected are s3-v1/s3-v2. This only strengthens the conclusion below.
- **KiCad item changes per scenario: s1 22–28, s2 34–50, s3 54–92.** One `sch_add_power` call
  on 6 pins alone is 12 items (symbol + 1–2 stub segments each). With the 12-item budget the
  turn would have been refused at call 7–12, i.e. **already in the 3-part LED circuit**.
- **Logical changes** (one per placed / moved / edited / deleted part, one per connection,
  one per pin given a label, power symbol or no-connect): **s1 10, s2 15–23, s3 30–51.**
- Self-correction deletes parts the agent placed itself in the same turn: up to 4–5 symbol
  deletions per turn (s3-v2, s3-opus).

## Decision

**Tool shape OK — needs changes:** add `sch_move` (drag wires), `sch_delete` (with attached
stubs), `sch_find_space(lib_id, near=REF.PIN)` and wire T-joins in `sch_connect` to the
core set; return pin directions per rotation from `lib_get_symbol`; make every multi-target
call atomic; budget in logical changes (12) with deletions of own same-turn parts free.

Recommended final tool list / parameters for the plan:

| Tool | Keep / change |
|---|---|
| `lib_search_symbols(query, library?, limit?)` | keep (2B index). Results with description + default footprint were enough to pick the part in one search |
| `lib_get_symbol(lib_id)` | keep; **add `pin_directions_by_rotation`** for ≤ 4-pin symbols (LED/diode/transistor orientation) and the body size |
| `lib_search_footprints(query)` | keep (used in 6 of 13 runs to confirm 0805 / header names) |
| `sch_find_space(lib_id, near=REF.PIN|REF|[x,y], rotation?)` | **promote to core** (spec §6.1 has it with `size_mm`; take `lib_id` + rotation instead, the tool knows the size). Must keep the anchor pin's corridor and the new part's own pin corridors free |
| `sch_place_symbol(lib_id, at, rotation, mirror, value, footprint, fields, reference?)` | keep; return pins with absolute position + direction (the agent used them in every run). Refuse `power:` lib_ids (use `sch_add_power`). Obstacles = body box + one box per visible field + label text |
| `sch_add_power(net, at=[REF.PIN…])` | keep, **multi-pin**; the most used mutating tool (3–13 calls per run). Atomic. `net="PWR_FLAG"` for flags. Place from the free end of an existing stub when the pin is already wired |
| `sch_add_label(net, at=[REF.PIN…], kind)` | keep, multi-pin; refuse rail names |
| `sch_connect(from, to, via=auto|wire|label, net_name?)` | keep; **add T-joins**: a route may end on (or touch) a wire of the *target* net, with an automatic junction, and "to = net name" should route a wire to that net when it is close, not only place a label/power symbol |
| `sch_no_connect(pins[])` | keep |
| `sch_set_fields(ref, fields, dnp?)` | keep (rarely needed: the agent set value/footprint at placement) |
| `sch_move(ref, to?, rotation?, mirror?)` | **core**, with 2A's wire/field dragging. The prototype's "unconnected only" limit forced delete + re-place in s3-v2 |
| `sch_delete(refs, with_attached=true)` | **core**; must also accept power-symbol references (`#PWR…`) and report nets that changed |
| `sch_get_pin`, `sch_get_net`, `sch_list_symbols` | keep (1–2 calls per run, used after errors) |
| `run_erc`, `render_view(around?, region?)` | keep. The mm ruler on the render was enough to relate the picture to coordinates |

Not needed in this eval: `sch_reroute`, `sch_add_wire` (no run asked for coordinates of a
wire), `sch_rename_net`, `sch_duplicate`, sheets (out of the scenarios' scope).

**Budget:** keep the owner's **12**, but count **logical changes**: a placed / moved /
edited symbol = 1, a connection = 1 (all its wire segments and junctions), each pin that gets
a label / power symbol / no-connect = 1, a deleted symbol = 1. Then s1 (10) runs without a
plan, and s2 (15–23) and s3 (30–51) need one. That fits "a new circuit block = plan, a
small edit = direct". Count symbol deletions (≤ 3) only for symbols that existed **before the
turn**: deleting what the agent itself placed in the same turn is correction, not
destruction. Per-step `expected_changes` must use the same unit.

**Top skill rules** (in order of observed impact; v2 draft in `skill/v2/`):

1. Get positions for parts that belong to a pin (decaps, pull-ups) from `sch_find_space(near=PIN)`
   and leave free room in front of every pin that still needs a power symbol / label.
2. Rails only via `sch_add_power` (multi-pin): never loose `power:` symbols, never labels named
   like a rail, never long supply wires.
3. Before rotating a polarised part, read `pin_directions_by_rotation`. Orient parts so that
   the connected pins face each other (connector `mirror="y"`).
4. If the same call fails twice, change approach: `sch_move` / `sch_delete` + re-place. Never
   leave stray symbols or labels as workarounds.
5. `PWR_FLAG` once on every externally supplied rail incl. GND. Fix ERC errors before
   finishing, and report warnings.
6. Signals between blocks (I2C, SPI, …) by labels with bus-style names; short local
   connections by `sch_connect`.
7. End with `run_erc` + `render_view` and check the render against the checklist (decaps next
   to their pin, supplies up / GND down, no text overlaps).

## Follow-ups for the plan

- **Router (`connect.py`)**: T-joins onto wires of the target net (+ junction). Treat items of
  the two nets being joined as allowed contacts, not obstacles. Try perpendicular stubs when a
  pin's outward corridor is blocked.
- **Obstacles (`geometry.py`)**: body/pin box + one box per visible field + label text boxes
  (not a union box). The text width is estimated in this spike; the implementation can use
  `get_text_extents` (2A Q9).
- **Field placement**: headless placement does no autoplace (2A). The spike's rule (stack
  Reference/Value on a pin-free side, horizontal) gave readable results. Field angles are
  relative to the symbol (90 for 90/270°-rotated symbols), and justification turns with the
  symbol, so centre the text for rotated parts.
- **Power symbol references**: headless placement annotates every power symbol `#PWR01`
  (duplicates). The annotator (§5.5) must number `#PWR`/`#FLG` too.
- **Atomic multi-target tools** (`sch_add_power`, `sch_add_label`, `sch_no_connect`):
  prepare all targets first, then one commit (spec §5.1). The spike's v2.1 compensates instead.
- **Live agent eval (§12)**: reuse `eval.py` + `scenarios.py` (role-based golden checkers,
  ERC, layout checks, cost) as the opt-in live suite. A run costs US$0.06–0.40 and 25–100 s.
- **Model choice**: Sonnet 5.5 (default) succeeded everywhere at roughly half the cost of
  Opus 5.5. Opus produced the tidier s3 layout (decaps wired to `VCC`); its rebuild of
  four parts was a recovery from a partly failed call, not a layout choice. The model setting can stay at the default.
- **Part research**: the scenario prompts gave the USI pin mapping. In the product that
  comes from `parts_get_notes` / `research_part` (§9), which this spike did not exercise.
