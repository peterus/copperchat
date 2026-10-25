---
name: kicad-schematic-editing
description: How to change a KiCad schematic with CopperChat's editing tools (find parts, place, connect, label, power, fields, move/delete/duplicate, sheets, plans, ERC and the self-check). Use whenever you add, connect, change or remove anything in the schematic.
sessions: schematic, free
---

# Editing a schematic with CopperChat

You change the schematic only through the `sch_*` tools. Every tool writes through
KiCad's API, reads the result back and reports exactly what it changed
(`created` / `modified` / `deleted`, KiCad's own cleanup as `side_effects`). A tool
error means **nothing** was changed. Coordinates are mm on the 1.27 mm grid (the
tools snap). The user sees every change in the Changes panel and can undo a turn or a
plan step.

## At a glance

- Library: `lib_search_symbols`, `lib_get_symbol`, `lib_search_footprints`.
- Part knowledge: `parts_get_notes` before an active part; `research_part` when it has
  no notes yet.
- Edits: `sch_place_symbol`, `sch_connect`, `sch_add_power`, `sch_add_label`,
  `sch_no_connect`, `sch_set_fields`, `sch_move`, `sch_delete`, … — every result is
  verified against KiCad's netlist; an error means nothing changed.
- Larger changes: `plan_propose`, then `plan_step_begin` / `plan_step_done` after the
  user approved; `sch_changes` lists what a turn, step or plan changed.

## Tool order

1. **Think the sheet through first** (no tool calls): parts, nets, rough positions.
   Signal and power flow left → right, supplies up, GND down. Leave 15–25 mm between
   blocks and ≥ 10 mm between parts. The layout recipes are in `kicad-schematic-style`.
2. **Research active parts.** Before placing an IC or another active part
   (regulator, MCU, op-amp, interface chip, sensor, transistor in a non-trivial role)
   call `parts_get_notes(mpn)`; when it says `researched: false`, call `research_part`
   first. Follow the notes' typical application circuit, required external parts and
   unused-pin rules; their "KiCad symbol/footprint mapping" gives `lib_id` and
   footprint. Passives, connectors, LEDs and standard diodes need no research.
3. **Find the part**: `lib_search_symbols` → a `lib_id` from the results (project
   libraries come first). For anything with more than 2 pins call `lib_get_symbol`:
   pin numbers, names, which side each pin points to, body size. Confirm footprint names
   with `lib_search_footprints` when unsure (not available in projects without a board).
4. **Find room**: parts that belong to a pin (decoupling cap, pull-up, series
   resistor) get their position from `sch_find_space(lib_id, near="U1.VCC")`; it keeps
   the pin's corridor and the new part's own pin corridors free. Use it instead of
   guessing coordinates.
5. **Place** with `sch_place_symbol` — value and footprint in the same call. The result
   lists every pin's absolute position and direction; use them. Orient parts so the
   pins you will connect **face each other** (a connector with `mirror="y"` has its pins
   on the right; a vertical R/C has pins up/down, `rotation=90` makes it horizontal).
   Before rotating a polarised part (LED, diode, transistor) read
   `pin_directions_by_rotation` from `lib_get_symbol` (LED anode up = the rotation where
   `A` points up).
6. **Connect** (see "Rails and labels"), mark unused pins with `sch_no_connect`.
7. **Annotate**: new parts get references automatically (per-sheet blocks such as
   `R101` on page 1, or flat `R1, R2…` — the design's existing scheme wins). Use
   `sch_annotate` only for unannotated (`R?`) or duplicate references; never renumber
   existing references unless the user asks (`mode="renumber"`).
8. **Check**: `run_erc`, then `render_view(doc="sch", region=…around the changed refs…)`
   and the self-check below.

Other tools: `sch_set_fields` (value, footprint, MPN, DNP, custom fields of one
reference), `sch_move` (move / rotate / mirror; wires are dragged), `sch_delete`
(with its stubs, labels and power symbols), `sch_duplicate` (copy a block, e.g. a
channel ×3 with `net_suffix="_CH{n}"`), `sch_add_wire` (an explicit wire by
coordinates; prefer `sch_connect`), `sch_reroute` (tidy an existing route, the nets
stay identical), `sch_rename_net`, `sch_add_sheet` + `sch_add_sheet_pin`
(hierarchical sheets; connect a sheet pin as `'<sheet name>.<pin name>'`),
`sch_get_pin`, `sch_get_net`, `sch_list_symbols`, `sch_list_items` (look before you
act, and after an error), `sch_changes` (what this turn / step / plan changed).

## Addressing pins

`REF.PIN` with the pin number (`U1.3`) or a unique pin name (`U1.VIN`; `U1.PB0` also
matches `AREF/PB0`). If a call fails with `ambiguous_pin`, use the number. A sheet pin
on the parent sheet is `'<sheet name>.<pin name>'` or its UUID (from `sch_add_sheet_pin`,
`sch_list_items` or `sch_get_pin`); both work as `sch_connect` endpoints and as
`sch_move` / `sch_delete` items. Quote references to the user, never UUIDs; pass full
UUIDs between tool calls only for wires, labels, junctions, no-connects and sheet pins.

## Finding items

- `sch_list_items` lists the items of one sheet (default root; `sheet="/Power/"`) with
  their UUID, type, position (`start_mm` / `end_mm` for wires), text and net (`null` =
  connected to nothing). Filter with `types` (`label`, `global_label`, `hier_label`,
  `wire`, `junction`, `no_connect`, `sheet_pin`, `power`, `symbol`, `sheet`), `bbox`
  ([x1, y1, x2, y2] mm), `net` and `text` (substring or glob like `SPI*`). It pages
  (`limit` default 100, at most 500; `offset`, `total`). Use it to find what you placed
  wrongly — a floating label, a sheet pin on the wrong edge, a stray wire — and repair
  it with `sch_move` / `sch_delete` by UUID.
- `sch_get_pin` lists the items exactly at the pin (`connected`, sheet pins included) and,
  when the pin has its own stub, what hangs at the stub's far end (`stub_end`: labels,
  power symbols, no-connects with UUIDs) and the stub's wires (`stub_wires`).

## Rails and labels

- **Rails and GND only via `sch_add_power(net, at=[REF.PIN, …])`** — one call for all
  pins of a rail. Never place a `power:` symbol with `sch_place_symbol`, never name a
  label like a rail (a `GND` or `+3V3` label does not join the power net), never draw
  long supply wires between distant pins.
- **`PWR_FLAG` once on every externally supplied rail, GND included**
  (`sch_add_power(net="PWR_FLAG", at=[a pin of that net])`), else ERC reports "input
  power pin not driven". A regulator output drives its rail; no flag there.
- **Short local connections** (series parts, a cap next to its pin):
  `sch_connect(from, to)`; it computes the route and may T-join onto a wire of the
  target net. `to` may also be a net name (`to="+3V3"` places a power symbol).
- **Signals between blocks** (I2C, SPI, UART, MCU ↔ peripheral): labels with bus-style
  names (`I2C_SDA`, `SPI1_SCK`) via `sch_add_label(net, at=[pins])` or
  `sch_connect(..., via="label", net_name=...)`. Pins on different sheets get global
  labels.
- **Hierarchical sheets**: `sch_add_sheet` (its own new file), then one
  `sch_add_sheet_pin` per signal; it also puts a hierarchical label of that name at the
  left edge inside the sub-sheet. On the parent connect the sheet pin
  (`sch_connect(from="<sheet name>.<pin name>", to=…)`). Inside the sub-sheet (pass its
  path, e.g. `sheet="/Sensor/"`) place the parts near those labels and connect with
  `sch_connect(from="U2.SDA", to="<pin name>")`: within 25.4 mm it wires onto the label;
  farther away first `sch_move` the label (its UUID is in the `sch_add_sheet_pin` result)
  next to the pin. An unwired hierarchical label is an ERC error.
- **Repairing a sheet pin**: `sch_move(items=["<sheet>.<pin>"], to=[x, y])` moves it
  along its edge (its own stub travels with it) or onto another edge (`to` within 2.54 mm
  of that edge; the side and text orientation follow, its wires are dragged; if they
cannot follow without crossing the sheet, it is refused: `sch_delete` the wire, move the
pin, then `sch_connect` again). Pins stay
  2.54 mm apart and one pitch clear of the corners; rotation / mirror is refused for sheet
  pins. `sch_delete(items=["<sheet>.<pin>"])` removes the pin and its own stub; the
  sub-sheet's hierarchical label of that name goes too if it is connected to nothing and
  no other sheet symbol of a shared file still has that pin (a note says when it stays).
  A sheet cannot have two pins of one name.
- Leave ≥ 7.62 mm free in front of every pin that still needs a power symbol or label;
  a pin boxed in by another part cannot be connected cleanly.
- Inputs must not float: enable/address pins go to a rail or GND as the notes or the
  user say. Unused pins: `sch_no_connect(pins=[…])` (only pins the notes call unused).

## Plans

- The tools enforce the change budget (default 20 logical changes per turn outside a
  plan, at most 3 deletions of parts that existed before the turn). One logical change =
  a placed / moved / edited / deleted part, a connection, one pin given a label / power
  symbol / no-connect. Correcting a part you placed yourself in the same turn is free.
  Each tool description ends with its `Budget:` line; the two that count by item:

  | Tool | Cost |
  |------|------|
  | `sch_delete` | 1 per symbol (all units), 1 per sheet, 1 per sheet pin (its stub and child label included); all wires, junctions, labels and no-connects you list that are on one net: 1 together per net |
  | `sch_move` | 1 per moved symbol, label, sheet or sheet pin; carried stubs and their end items are free |
- When a request needs more than the budget (a new circuit block, a whole sub-sheet),
  or a tool answers `plan_required`, call `plan_propose` **before** editing: steps of
  5–10 logical changes each, every step with a clear intent, `expected_changes` and how
  you will verify it. Then stop and wait: the user approves or rejects the plan card.
- While a plan is open every edit outside a begun step is refused (`plan_open`,
  `plan_step_required`). After approval: `plan_step_begin(k)` → edits → `run_erc` +
  `render_view` → `plan_step_done(k, summary, erc_justifications)`. Steps run in order and
  begin and finish in the same turn.
- **Never exceed the plan.** A step may use at most its `expected_changes` + 50 %
  (`budget` in each edit result shows `used` / `limit`). If you see mid-step that the
  step needs more changes than expected, call `plan_revise` *before* exceeding the budget
  and explain why: the running step is paused (its edits and its used count stay), the
  user approves the revision, and the step continues as the first step of the new
  revision with its used count against the new `expected_changes`. Exceeding the limit
  still fails the step and pauses the plan (`plan_step_exceeded`).

## ERC policy

- Every new ERC **error** must be fixed, or justified to the user one by one (e.g. "pin
  open, the connector follows in step 4"). In a plan step `plan_step_done` refuses open
  errors without a justification in `erc_justifications {violation_id: reason}`; outside
  a plan it is your duty and the turn's ERC badge shows open errors in red.
- `run_erc` lists every violation with the same `id` that `plan_step_done` takes, so you
  can justify before the step ends. It is paged (`limit` 50, max 200, `offset`) and takes
  `sheet` (name or path); `total` and `counts` (by type / sheet) show the shape first.
- Many alike errors: `plan_step_done(erc_rules=[{match: {type?, sheet?}, reason}])`
  (≤ 20 rules) justifies every new error a rule matches, **in that step only**. Ids and
  rules are kept per step, so after a refusal send only what is new. A refusal lists 20
  errors with `total`/`counts`; page the rest with `run_erc(sheet=…, offset=…)`. Do not
  use a rule to wave through errors you have not looked at.
- New **warnings** are only reported (name them in the summary).
- Run `run_erc` after your last change of a step or turn, not after every call.

## Results and errors

- Read `connectivity_verified`, `nets_touched` and `notes` in every result.
- `notes` with `footprint_unverified` (project without a board: footprint existence is
  not checked) → tell the user the footprint is **unverified**. `preference_deviation`
  (e.g. a passive not in 0805) → justify it to the user or fix it with `sch_set_fields`.
  `active_part_without_notes` → you skipped the research; do it.
- An `overlap` error carries the nearest free position: use it, or `sch_find_space`.
- `no_route`: the parts usually face away from each other; prefer `sch_move` (rotate /
  mirror) over long detours, or connect by label.
- Repair before you recreate: find the wrong item (`sch_list_items`, `sch_get_pin`) and
  `sch_move` it; delete and add again only what cannot be moved.
- If the same call fails twice, change approach: `sch_move` the part, or `sch_delete` it
  (stubs, labels and power symbols go with it) and place it again via `sch_find_space`.
  Never leave stray symbols, loose wires or labels behind as workarounds.
- `document_inconsistent`: stop editing and tell the user what failed; the turn end
  reloads the last save, the state just before that edit (every successful edit is saved
  at once, so earlier edits of the turn are kept).
- `save_failed`: the edit is in KiCad but could not be saved; stop editing and tell the
  user (the turn ends and CopperChat retries the save).

## Missing parts

Never invent a `lib_id`, a pin name or a footprint. If no library symbol fits, tell the
user, offer the closest matches from `lib_search_symbols` (lib_id + why it is close) and
ask how to proceed; creating library symbols or footprints is not something you can do.

## Self-check

At the end of every plan step and of every editing turn, render the changed region with
`render_view(doc="sch", region=…)` and check it against this list:

- Values and references readable, no text overlaps, nothing overlapping a part body.
- Supplies point up, GND down; decoupling caps next to the pin they decouple.
- Signal flow left → right; connected pins face each other; no long detours.
- No wire runs over a pin or through a part body.
- Every intended connection is in a tool result with `connectivity_verified: true`.
- ERC: 0 new errors (or each one justified), warnings listed.

## Reporting

Finish with a short summary for the user: parts placed (reference, value, footprint),
nets created, ERC result (errors / warnings), notes the user must know (unverified
footprints, preference deviations), and anything you could not do or did differently
than asked.
