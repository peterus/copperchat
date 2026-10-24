---
name: kicad-review-discipline
description: Electrical review checklist and validation discipline for KiCad designs. Use for any design review, any question about whether a circuit or board is correct, and before stating that something is fine or fixed.
sessions: schematic, board, free
---

# Review discipline

## Prime directive: verify before you claim

Never state that a schematic or board is correct, clean or fixed without
evidence from this turn:

1. `run_erc` on the schematic — read every violation (it is paged: check `total`, and
   use `sheet` / `offset` for the rest; each violation has a stable `id`). Zero violations, or
   each one explained (what it means, why it is acceptable or how to fix it).
2. `run_drc` on the board, if a board exists — same standard.
3. `render_view` of the area you are talking about — look at the image and
   describe what you actually see. "It looks correct" without a render in
   this turn is not acceptable.

Warnings are not decoration: either explain why one is harmless or report it
as a finding. If a tool fails, say so; do not guess the result.

## Checklist

- **Decoupling:** 100 nF ceramic per IC supply pin, placed at the pin, plus
  a bulk capacitor (1–10 µF) per IC or per rail segment. On the board, the
  capacitor sits next to the pin with the shortest loop to ground.
- **Regulators:** input and output capacitors per datasheet values and
  types (some LDOs need specific ESR); feedback dividers checked against the
  datasheet formula.
- **Pull-ups / pull-downs:** I2C needs one pull-up pair per bus (typically
  4.7 kΩ to the bus voltage), not one per device. Open-drain outputs, reset
  and enable pins, boot-strap pins need a defined level.
- **Floating inputs:** unused logic inputs tied high or low; unused op-amp
  sections configured as followers to a defined voltage.
- **Power flags:** every externally driven supply net has a `PWR_FLAG` at its
  source, otherwise ERC reports "input power pin not driven".
- **Crystals:** load capacitors computed from the crystal's load capacitance
  and stray capacitance.
- **Connectors:** pin 1 orientation, current rating, ESD protection on
  externally exposed lines.
- **Footprints:** every symbol has a footprint; IC pad count and pin mapping
  checked against the datasheet, not only the symbol.

## Library policy

1. KiCad's bundled libraries first.
2. Manufacturer-provided KiCad libraries next.
3. Third-party libraries (SnapEDA, Ultra Librarian) into a project-local
   library, never into the global tables.
4. Hand-made symbols/footprints only as the last resort, checked against the
   datasheet dimension by dimension.

Project-local libraries live next to the `.kicad_pro` and are registered in
the project's own library tables so the project stays self-contained.

## Unknown parts

When a part's wiring, values or layout rules matter and you do not know them
for certain, call `research_part` (with `mpn` = the bare manufacturer part
number) instead of answering from memory.
