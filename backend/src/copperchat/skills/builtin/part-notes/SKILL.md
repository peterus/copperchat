---
name: part-notes
description: Method and fixed template for docs/parts/<MPN>/notes.md written by the part-researcher. The notes must let the design agent draw the schematic and lay out the PCB without opening the datasheet again.
agent: part-researcher
---

# Part notes: method and template

Your notes are the **only** thing the design agent will read about this part.
It cannot open the PDF and has no web access. Every fact it needs to place the
symbol, wire every pin, choose and dimension every external component, and
route the board must be in `notes.md` — with a datasheet page reference so a
human can verify it. Write for an engineer who will build the board from your
notes alone.

## Method

1. **Pin down the exact part.** Decode the orderable part number (suffix →
   package, temperature grade, fixed/adjustable output, option codes). If the
   question names no variant, cover the most common package in full and list
   the other packages (with pin-number differences) in "Packages and pinout".
2. **Get the primary source.** Prefer the manufacturer's current datasheet
   (download it with `download_datasheet`). Check the revision history and the
   manufacturer's errata sheet / application notes for the part when they
   exist; cite them as D2, D3 …. Distributor pages and forums are not sources
   for electrical values.
3. **Read in this order:** pin configuration and functions → absolute maximum
   ratings → recommended operating conditions → electrical characteristics
   needed for design (thresholds, switching frequency, reference voltage,
   quiescent current, logic levels) → application information / typical
   application → layout guidelines and layout example → mechanical drawing and
   recommended land pattern → errata / revision history.
4. **Write the notes in the template below.** Keep every heading and every
   table column exactly as given — a validator checks them. Replace every
   `<placeholder>`; delete example rows you do not need. Add rows, bullets
   and `###` package subsections as the part requires.
5. **Values:** always with unit and condition (`2.5 MHz (MODE = low)`,
   `0.8 V ±1 % (TJ = 25 °C)`). Component values must be **concrete**
   (`2.2 µH, Isat ≥ 3.5 A`, `22 µF X7R 10 V 0805`) — never "see datasheet".
   Note dielectric and voltage rating for capacitors (DC-bias derating matters
   for MLCCs), current/saturation rating for inductors, power for resistors.
6. **Formulas:** whenever the datasheet gives a formula (feedback divider,
   inductor ripple, soft-start time, current limit, timing capacitor, RC
   filter, crystal load), give the formula with every symbol defined **and**
   a worked example for the operating point of the question — or the
   datasheet's typical application — on a line starting with `Example:`,
   ending with the chosen standard (E24/E96) value.
7. **Every pin gets an "If unused" entry:** what to do when its function is
   not used (tie to GND, tie to VIN through 10 kΩ, leave open, must be
   connected). Supply and ground pins: `n/a — always connected`. Exposed pads
   are a row of their own (pin `EP`). Note internal pull-ups/pull-downs and
   whether a pin may float.
8. **Cite pages** as `[D1 p.<n>]` (D1 = the datasheet, D2 … = other sources).
   At least the pinout, the ratings, the application circuit and the layout
   section need citations; cite every number you copy.
9. **Never invent values.** If the datasheet does not specify something,
   write `not specified [D1]` and, if a value is still needed, what you would
   assume and why (marked `assumption:`).
10. **KiCad mapping:** name the KiCad library symbol and footprint in
    `Library:Name` form (e.g. `Regulator_Switching:TPS62133` and
    `Package_DFN_QFN:VQFN-16-1EP_3x3mm_P0.5mm_EP1.68x1.68mm`) when the standard
    KiCad libraries contain a match. Compare pad count, pitch, body and
    exposed-pad size with the datasheet land pattern. Otherwise write
    `no exact match — closest: <Library:Name>, differences: <…>`.
11. **Validate:** after writing, call `check_notes` with the MPN and fix every
    issue it reports, then call it again until it reports none.

## Template

~~~markdown
---
mpn: <MPN>
manufacturer: <manufacturer>
datasheet: datasheet.pdf (<document number>, rev <revision>, <date>)
user_edited: false
---

# <MPN> — <one-line function, e.g. 3–17 V input, 3 A synchronous buck converter>

## Summary

<3–6 sentences: what it is, key ratings, which variant and package these notes
cover, the operating point used in the examples, anything unusual.>

## Packages and pinout

### <package name> (<pins>, <body size>)

| Pin | Name | Type | Function | If unused |
|---|---|---|---|---|
| <1> | <name> | <power / input / output / I/O / analog / ground / NC> | <what it does, internal pull-up/down, logic thresholds, max voltage if lower than VIN> [D1 p.<n>] | <tie to GND / leave open / must be connected / n/a — always connected> |

## Absolute maximum ratings

| Parameter | Min | Max | Unit | Source |
|---|---|---|---|---|
| <VIN to GND> | <-0.3> | <20> | <V> | [D1 p.<n>] |

## Recommended operating conditions

| Parameter | Min | Typ | Max | Unit | Source |
|---|---|---|---|---|---|
| <Input voltage> | <3> | | <17> | <V> | [D1 p.<n>] |

## Typical application circuit

<Topology in words, then every connection: which pin goes to which part or
net. Mark which parts are optional and what they do. State the operating
point the values are for.>

| Ref | Part | Value | Rating | Purpose | Source |
|---|---|---|---|---|---|
| <C1> | <capacitor, X7R> | <10 µF> | <25 V, 0805> | <input decoupling, at PVIN> | [D1 p.<n>] |

## Design equations

- <Name>: <formula with every symbol defined> [D1 p.<n>]
- Example: <the formula evaluated for the operating point, with result and the chosen standard value>

## Required external components

- <Decoupling: values, dielectric, voltage rating, placement distance>
- <Pull-ups / pull-downs: value, to which rail>
- <Protection: TVS, series resistors, reverse polarity, as applicable>

## Layout guidelines

- <Critical loop or trace and how to keep it small> [D1 p.<n>]
- <Thermal pad: vias (count, drill, pitch), copper area>
- <Ground: plane, star point, sensitive analog ground>
- <Clearances, keep-outs, sensitive nodes to route away from>

## Package and footprint

| Package | Body (mm) | Pitch (mm) | Thermal pad | KiCad symbol | KiCad footprint |
|---|---|---|---|---|---|
| <VQFN-16> | <3 × 3> | <0.5> | <1.68 × 1.68 mm, connect to GND> | <Lib:Symbol> | <Lib:Footprint> |

<Land-pattern notes: paste reduction on the thermal pad, solder mask, pin-1
mark, differences between the KiCad footprint and the datasheet land pattern.>

## Errata and pitfalls

- <Known errata, start-up quirks, stability conditions, common wiring mistakes>

## Sources

- [D1] <manufacturer> <MPN> datasheet, <document number> rev <revision> (<date>), <URL>, accessed <YYYY-MM-DD>
- [D2] <application note / errata sheet, if used>
~~~

## Existing notes

- `notes.md` exists and `user_edited: false`: update it in place (keep correct
  content, fix and extend the rest), then validate again.
- `user_edited: true`: the user owns the file. Do **not** modify it (writes
  are refused). Answer from it and put new findings only into your answer.

## Answer to the design agent

After writing the notes, answer in at most 250 words: the key facts the
question asked for, with values and units, anything you could not verify,
and the path of `notes.md`.
