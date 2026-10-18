---
name: copperchat-tools
description: How to use CopperChat's KiCad inspection tools correctly (units, identifiers, renders, part research). Use whenever you inspect the schematic or board, run ERC/DRC, render a view or need datasheet facts.
---

# Using the CopperChat tools

You see the design only through these tools. They read live data from the
running KiCad API server; there is no file access, shell or web for you.

## Units and identifiers

- All coordinates and sizes are in **mm**. Quote positions as
  `(x, y) mm` and never convert to mils unless the user asks.
- Items are identified by their **full UUID** as returned by the tools. Never
  shorten a UUID; copy it exactly when you refer to an item.
- References (`R12`, `U3`) are for talking to the user; UUIDs are for exact
  identification between tool calls.

## Querying

- Start with `project_info` to learn files, sheets, board size and layers.
- Use the `filter` arguments of `sch_list_symbols` / `pcb_list_footprints`
  instead of listing everything; there is no pagination.
- `sch_get_net` / `pcb_get_net` answer "what is connected to X".

## Seeing the design

- `render_view(doc, mode="agent")` draws a mm grid, references, stable net
  colours and ERC/DRC markers — use it to check what you claim.
- Dense boards: pass a **region** (`around_ref` with a margin, or an
  `x_mm/y_mm/w_mm/h_mm` box) instead of rendering the whole board; small
  parts are unreadable in a full-board render.
- `mode="kicad"` shows KiCad's own look when the user wants to see that.
- Describe what you see in the image; do not infer it from tool data alone.

## Errors

Tool errors come as `{what, why, suggestion}`. Follow the suggestion once;
if it fails again, report the error to the user instead of retrying in a loop.

## Part research

For datasheet questions (pinout, application circuit, component values,
layout rules, footprint) call `research_part`:

- `mpn`: the bare manufacturer part number, e.g. `TPS62133` (required; the
  notes folder and the "Part research" card in the chat are named after it).
- `query`: the concrete question, with the operating point if one matters
  (e.g. "12 V in, 3.3 V / 2 A out: inductor, capacitors, feedback divider").
- `attachment_paths`: paths of PDFs the user attached (they appear as
  `docs/parts/_uploads/...` in the message), so the researcher reads them.

The part-researcher is a separate agent with web access. It stores the
datasheet and notes in `docs/parts/<MPN>/` (fixed template: pinout per
package incl. unused pins, ratings, application circuit with values and
formulas, external parts, layout, footprint with KiCad library mapping,
pitfalls, sources with page numbers) and returns a summary. Later questions
about the same part are answered from those notes, so ask it again rather
than guessing. Research takes minutes: ask one well-formed question per part
instead of many small ones.

## Editing the schematic

To change the schematic (place, connect, label, power, fields, move, delete,
duplicate, sheets) load the `kicad-schematic-editing` skill first: it gives the tool
order, the plan rules and the self-check. In short:

- Library: `lib_search_symbols`, `lib_get_symbol`, `lib_search_footprints`.
- Part knowledge: `parts_get_notes` before an active part; `research_part` when it has
  no notes yet.
- Edits: `sch_place_symbol`, `sch_connect`, `sch_add_power`, `sch_add_label`,
  `sch_no_connect`, `sch_set_fields`, `sch_move`, `sch_delete`, … — every result is
  verified against KiCad's netlist; an error means nothing changed.
- Larger changes: `plan_propose`, then `plan_step_begin` / `plan_step_done` after the
  user approved; `sch_changes` lists what a turn, step or plan changed.

## Git

Use `git_commit` only when the user explicitly asks for a commit.
