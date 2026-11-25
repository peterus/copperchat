---
name: session-board
description: Use in a Board session: drives the step-by-step board flow S0–S9 from the schematic to the fab files, with a step report and the user's ok after every step. Load at the start of every board session and before the first board write.
sessions: board
---

# Board session

You lay out the board from the schematic, one step at a time, with the user. The
schematic is read-only here; you edit the board with the `pcb_*` tools.

## Rules

- After every step, stop the turn and wait for the user's ok (or corrections) before
  the next step.
- Every step ends with a step report: images, a short check summary (what passed, what
  needs attention), the decisions taken, and a question.
- Hard checkpoints: at S4 ask "Approve this placement?", at S8 ask "Approve this board
  for manufacturing?". Never call `pcb_approve` without the user's own words approving
  it in their latest message.
- Never edit the schematic. If the schematic must change, say what and why, and record
  it under `Open points` in `board.md`.
- Record answers and decisions in `board.md` with `doc_patch_section` and in
  `decisions.md` with `doc_append`.

## S0 Intake

What the agent does: reads `requirements.md`, `architecture.md`, `parts.md`, `board.md`;
checks the schematic is ERC-clean and every symbol has a footprint (local footprints
approved); asks the first design questions.

Ends with: questions; answers recorded in `board.md`.

Full content: P9b.

## S1 Board from schematic

What the agent does: `pcb_sync_from_schematic`; explains warnings (pin/pad mismatches).

Ends with: sync report.

Full content: P9b.

## S2 Outline, holes, rules

What the agent does: `pcb_set_outline`, `pcb_set_rules` (layers, preset, stackup, net
classes from the answers).

Ends with: outline render + rules table; ok?

Full content: P9b.

## S3 Blocks

What the agent does: `pcb_analyze`, proposes functional blocks and their rough positions
(a floorplan sketch: block rectangles on the outline).

Ends with: block list + floorplan render; ok?

Full content: P9b.

## S4 Placement

What the agent does: `pcb_define_blocks`, connectors/fixed parts first, then blocks with
`pcb_place_block`, tightening with `pcb_move`, `pcb_legalize`; `pcb_metrics` (placement
part) until M1/M8/M9/M10 pass.

Ends with: placement render with block hulls + metrics; "Approve this placement?" (hard
checkpoint).

Full content: P9b.

## S5 Critical routing

What the agent does: `pcb_route` for the critical set, locked.

Ends with: top/bottom render with these nets highlighted; ok?

Full content: P9b.

## S6 Fill routing

What the agent does: `pcb_route_fill`; rejected connections re-routed with `pcb_route`;
when stuck: small placement changes or questions (more layers, larger board).

Ends with: routed renders + metrics; ok?

Full content: P9b.

## S7 Planes and pours

What the agent does: `pcb_zones` (GND planes, outer pours), `pcb_fanout`, `pcb_stitch`,
refill.

Ends with: per-layer renders; ok?

Full content: P9b.

## S8 Final check

What the agent does: `pcb_tidy_silk`, `pcb_check` full, fix loop, 3D render.

Ends with: full report + all views; "Approve this board for manufacturing?" (hard
checkpoint).

Full content: P9b.

## S9 Fab

What the agent does: `pcb_fab_export` jlcpcb; offers a git commit with the git tools.

Ends with: file list + order notes.

Full content: P9b.
