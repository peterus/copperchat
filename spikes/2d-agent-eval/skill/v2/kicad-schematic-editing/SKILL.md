---
name: kicad-schematic-editing
description: How to edit a KiCad schematic with CopperChat's editing tools (place, connect, label, power, fields, ERC, self-check). Use whenever you add, connect or change parts in the schematic.
---

# Editing a schematic with CopperChat (DRAFT v2, spike 2D)

You change the schematic only through the `sch_*` tools. Every tool writes through
KiCad's API, reads the result back and reports what it changed; a tool error means
**nothing** was changed. Coordinates are mm on the 1.27 mm grid (the tools snap).

## Order of work

1. **Plan the sheet first** (in your head, not in tool calls): which parts, which
   nets, rough positions. Signal / power flow left → right, supplies up, GND down.
   Leave room: 15–25 mm between functional blocks, ≥ 10 mm between parts.
2. **Find parts**: `lib_search_symbols` → pick a stock `lib_id`; for anything
   with more than 2 pins call `lib_get_symbol` to see pin numbers, names, which
   side each pin points to, and the body size. Never invent a `lib_id` or a
   footprint; if nothing fits, tell the user and offer the closest parts.
3. **Place** with `sch_place_symbol` (value + footprint in the same call). The
   result lists every pin's absolute position and direction — use it.
   Orient parts so the pins you will connect **face each other**
   (connectors: `mirror="y"` puts the pins on the right; a vertical R/C has pins
   up/down, `rotation=90` makes it horizontal). For polarised 2-pin parts read
   `pin_directions_by_rotation` from `lib_get_symbol` before choosing the rotation
   (e.g. LED: anode up = the rotation where `A` points up).
   - Parts that belong to a pin (decoupling cap, pull-up, series resistor): get the
     position from `sch_find_space(lib_id, near="U1.VCC")` instead of guessing
     coordinates; it keeps clearance for the power symbols and labels.
   - Leave ≥ 7.62 mm free in front of every pin that still needs a power symbol or
     label — a pin boxed in by another part cannot be connected cleanly.
4. **Connect**:
   - Power rails and GND: `sch_add_power(net, at=[REF.PIN, …])` — one call for all
     pins of a rail. Never draw wires between distant supply pins, never place a
     `power:` symbol with `sch_place_symbol`, never name a label like a rail
     (`GND`, `+3V3` labels do not join the power net).
   - Short local connections (series parts, a cap next to its pin):
     `sch_connect(from, to)` — CopperChat computes the route.
   - Buses and signals between blocks (I2C, SPI, UART, MCU ↔ peripheral):
     `sch_add_label(net, at=[REF.PIN, …])` with one name per signal (e.g.
     `I2C_SDA`), or `sch_connect(..., via="label", net_name=...)`.
   - Unused IC pins: `sch_no_connect(pins=[…])`.
5. **Check**: `run_erc`, then `render_view` and look at the picture (see
   checklist). Fix ERC **errors** (or justify each one to the user); report warnings.

## Addressing pins

`REF.PIN` with the pin number (`U1.3`) or a unique pin name (`U1.VIN`, `U1.PB0`
also matches `AREF/PB0`). If a call fails with "ambiguous", use the number.

## Results and errors

- Results are JSON; quote references, not UUIDs, to the user.
- An overlap error carries the nearest free position — use it or move elsewhere.
- `sch_connect` with `via="auto"` falls back to labels when no clean wire route
  exists (it then needs `net_name`); a blocked route usually means the parts face
  away from each other — prefer re-placing over long detours.
- Errors: follow the suggestion once; if the same call fails twice, change the
  approach instead of retrying: `sch_move` a still-unconnected part, or
  `sch_delete` the part (removes its stubs, labels and power symbols too) and
  place it again with `sch_find_space`. Do not leave stray symbols or labels
  behind as workarounds.

## Power and ERC rules

- Every externally supplied rail (connector input, a rail with no regulator on this
  sheet) needs **one** `PWR_FLAG` on that net (incl. GND):
  `sch_add_power(net="PWR_FLAG", at=[pin on that net])`. Without it ERC reports
  "Input Power pin not driven".
- A regulator output (power_output pin) drives its rail; no flag there.
- Inputs must not float: address/enable pins go to a rail or GND as the user or the
  datasheet says.

## Part preferences

- Passives (R, C, L, ferrite) default to **0805** (`Resistor_SMD:R_0805_2012Metric`,
  `Capacitor_SMD:C_0805_2012Metric`). A `preference_deviation` note in a result must be
  justified to the user.
- Keep the library's default footprint for ICs unless the user asks otherwise.

## Self-check after editing (look at the render)

- Labels and values readable, nothing overlapping.
- Supplies point up, GND down; decoupling caps next to the pin they decouple.
- No wire runs over a pin or through a part body; no long detours around parts.
- Every intended connection is in the tool results (`connectivity_verified: true`).

## Reporting

Finish with a short summary: parts placed (ref, value, footprint), nets created,
ERC result, anything you could not do or decided differently than asked.
