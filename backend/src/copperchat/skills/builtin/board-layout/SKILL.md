---
name: board-layout
description: Rules for placing and routing a board by part class, the routing and plane rules, and the owner's hard quality thresholds. Load before placing, routing or pouring copper with the pcb_* tools.
sessions: board, free
---

# Board layout rules

These rules are generic: they name part classes, never a particular part. `pcb_analyze`
assigns the classes (`board.md` → Part classes rows override it); `pcb_metrics` and
`pcb_check` measure the checkable part (metric ids M1–M16). The quality bar is strict:
the board must be orderable without rework. DRC 0 is necessary, not sufficient.

## Placement by part class

| Part class | Rule | Checked by |
|---|---|---|
| `decoupling_cap` | same side as its IC; pad-to-served-pin path ≤ 3 mm (review > 3 mm, fail > 5 mm); the smallest value closest to the pin; its ground pad gets a via next to the pad | M8 |
| `switching_regulator` (+ its `inductor`) | minimal hot loop: input cap ≤ 3 mm from the VIN/GND pins, inductor ≤ 3 mm from the SW pin, switch-node copper ≤ 5 mm and not under sensitive parts; feedback divider next to the FB pin, away from the inductor | M9 |
| `crystal` | ≤ 5 mm from the MCU pins, load caps next to it, no other signals between, ground pour under it on the adjacent layer | M8 |
| `connector` | on its assigned edge (body ≤ 0.5 mm from the outline, or overhanging only that edge as the footprint requires); its ESD/filter parts between the connector and the rest | M13 |
| `module_with_antenna` | antenna end at the board edge or beyond, keepout on all layers under it | M13 |
| `anchor` (ICs, modules) | the centre of its block; its satellites (decoupling, crystal, pull-ups) around the pins they serve | M8, M10 |
| `mounting_hole` | inside its corner inset, keepout ring for copper; never in a block | DRC |
| `fiducial`, `test_point` | not in a block; reachable (test points), not under parts; never in the BOM/CPL unless assembled | DRC |
| differential pair (net kind) | routed together, same layer, constant gap, length mismatch reported | M12 |
| power path (supply nets) | high-current nets on outer layers with their class width, short and direct; no vias in series unless doubled | M12 |

Placement practice:

- **Blocks:** one block per function (connector + its protection, regulator + inductor + its
  caps, MCU + crystal + decoupling, …). ESD and filter parts join their connector's block.
- **Order:** edge blocks first (connectors at their edge, `board.md` → Placement rows), then
  the other blocks by connectivity to what is placed — `pcb_place_blocks` does this over the
  whole board; preview it with `pcb_floorplan`.
- **Room for tracks:** `keep_channels` 1.0–1.5 mm by default, 2–3 mm when the board has
  room (courtyard fill well below 30 %), 0.5 mm only when crowded. A part directly in front
  of a power or switch-node pin blocks the wide track's exit; an inductor whose pad faces
  away from its switch pin crosses its own connections; a part that serves two pins should
  be turned so its connections do not cross.
- **Straight links (owner rule):** two adjacent connected pads (an LED and its resistor, a
  pull-up and its pin) are aligned so one straight segment joins them. `pcb_place_blocks`
  aligns such two-pad parts (≤ 1 mm shift); M16 `alignable` lists the pairs still off.
- A part that cannot be placed legally is a `does_not_fit` answer for the user (larger
  board, bottom side, fewer fixed spots) — never an overlap.

## Routing rules

- **Critical set first, with the own router** (`pcb_route`, locked): decoupling
  connections, switch-node / hot-loop nets, crystal nets, differential pairs, every 2-pin
  net with an airwire ≤ 10 mm, and (from `pcb_analyze`'s `critical_reasons`) nets with a
  straight corridor between adjacent pads and nets that stay inside one block. Then
  **Freerouting fills the rest** (`pcb_route_fill`), post-processed to the same rules.
- **Ground is connected by planes, not tracks**: never route a ground net with
  `pcb_route`; `pcb_fanout` and the pours connect ground pads.
- **Angles:** segments only at 0°/45°/90°/135°…; a direction change within a track is 45°
  (or straight) — never 90°, never acute. **Pad entries are not bends.**
- **T, not Y (owner rule):** where a track branches off another track of the same net, the
  branch meets the straight through-track perpendicularly (a T-junction). A 45° branch (a
  Y-junction) fails M15. Four tracks on one 90° lattice (an X) are allowed.
- **No doglegs, pad loops or pad jogs (owner rule):** adjacent connected pads with a free
  straight corridor get one straight segment (M16 dogleg otherwise); a pad is left toward
  its other end — no U-turn (signed turn ≥ 135°, pad loop) and no S-offset (opposite-sense
  bend pair, pad jog) inside the pad's entry zone `max(1.0 mm, 3 × width)`.
- **Neck-down:** a track wider than a fine-pitch pad narrows to the pad width only inside
  that part's escape zone (courtyard + 0.5 mm), never below the preset's minimum track;
  outside the zone it has the class width (M12).
- **Chamfer / jog definitions** (the only ones; metric, router and post-processor share
  them): a *chamfer* is a pair of same-sense 45° bends (net 90° change) with a straight
  piece ≥ `max(1.0 × width, 0.25 mm)` between them; a *too-short chamfer* is a same-sense
  pair whose straight piece is shorter than `max(1.0 × width, 0.25 mm)`; a *micro-jog* is a
  pair of opposite-sense bends closer than `2 × width`. A valid chamfer is never a micro-jog.
- **Widths** come from the net class (`pcb_set_rules`); a class only as wide as its current
  needs (≈ 0.3 mm per ampere on 1 oz outer copper as a start), so wide tracks still fit
  between pins and parts.

## Planes, fanout, stitching

- **Default stack (D1):** 4 layers = Signal / GND / Signal + power / Signal (solid GND plane
  on In1.Cu, GND pours on F.Cu and B.Cu below it); 2 layers = GND pour on B.Cu (+ F.Cu pour);
  6 layers add a GND plane on In4.Cu. `pcb_zones {action: "add"}` alone pours it.
- **Fanout:** a ground SMD pad gets a stub ≤ 1 mm at 0°/45°/90° and a via next to the pad;
  **no via-in-pad** — fine-pitch pads connect through the outer pour (`via_pour`) or a tie
  stub to a larger own-net pad. Exposed ground pads need room for a via beside the part.
- **Stitching:** default pitch 5 mm (`pcb_stitch`); every ground pour island needs a via.
- **Refill after every zone change** (the zone tools do it); check M11 (plane fill, signals
  crossing a plane void) and M2 (islands) afterwards.

## Owner hard thresholds (D2)

Hard thresholds change only with the owner's OK — never relax them, never weaken a global
rule to pass; a footprint-specific exception is a custom rule with a reason and the user's
ok. A hard failure means the board is not done:

- M4 any segment off 0/45/90/135/… by more than ±0.5°, any 90° or acute direction change
  → fail (a perpendicular T-junction branch is allowed: owner 2026-11-26).
- M5 a signal connection with detour > 2.0 **and** > 5 mm extra → fail.
- M7 any micro-jog or too-short chamfer → fail.
- M8 decoupling path > 5 mm or other side → fail.
- M15 any Y-junction → fail.
- M16 any dogleg between adjacent pads, pad-entry loop or pad-entry jog → fail.
- M1 any placement violation (courtyard outside the outline, overlap, SMD on a side the
  assembly does not allow), M2 > 0 (unrouted), M3 errors or unaccepted warnings > 0, M12
  width violation, M13 violation (edge / antenna keepout), M14 JLCPCB preset violation
  without a recorded custom-rule exception → fail.

Review thresholds (show them, fix when you can, explain the rest): M5 median > 1.15, any
> 1.5 (critical > 1.25); M6 a connection longer than half the board diagonal; M7 > 1.0 bends
per 10 mm on average; M8 > 3 mm; M9 input cap > 3 mm, inductor > 3 mm, switch-node copper
> 5 mm; M10 block hull / courtyards > 3.0 or foreign parts in a block hull; M11 plane fill
< 85 % or void crossings; M12 pair length mismatch > 1 mm; M16 pairs alignable by ≤ 1 mm.
Footprint silk lines below the silk minimum are a warning only (owner 2026-11-26).
