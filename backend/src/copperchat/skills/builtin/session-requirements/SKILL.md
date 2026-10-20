---
name: session-requirements
description: Method for a requirements session - elicit what the board must do and write it into docs/design/requirements.md (and architecture.md, parts.md, decisions.md) until the user can approve it. Load at the start of every requirements session.
sessions: requirements
---

# Requirements session

In this session you work out **what** the board must do, with the user, and write it
into the design documents. You cannot change the schematic or the board here; the
next session (Schematic) builds the schematic from what you write. Load the
`design-docs` skill for the doc tool rules.

## Method

1. **Function and interfaces first.** Ask what the board does and what it connects
   to (connectors, buses, signal levels, protocols) before anything else.
2. **Then the rest, in this order:** power (sources, input ranges, rails, current
   budget, protection), environment (temperature, EMC, certifications), mechanical
   (size, mounting, connector positions, height), constraints (cost, layers,
   assembly, vendors) and acceptance criteria (measurable checks).
3. **Ask, don't assume.** Ask a few focused questions at a time. When the user wants
   you to go on without an answer, write the value as an assumption ("Assumption:
   …") and add it to `Open points`.
4. **Research key parts.** For the parts that decide the design (MCU, power
   converter, radio, connectors), call `research_part` with the bare MPN and a
   concrete question; check existing notes with `parts_get_notes` first. Record the
   candidates in the `Key parts` table of `parts.md` with status `candidate` (or
   `chosen` once the user agreed), and the reasons in `decisions.md`.
5. **Keep open points current.** Every unanswered question is a bullet under
   `Open points`; remove the ones that were answered.
6. **Propose approval.** When every section is filled and no open point blocks the
   schematic, tell the user that `requirements.md` looks complete and ask them to
   review and approve it in the Docs panel. You cannot approve it yourself.

## Writing the docs

- `doc_read` the doc first. A missing doc returns its template: fill it in and create
  it with `doc_write` and `base_version: null`.
- Replace a whole doc with `doc_write` only while you are its only author. Once the
  user edited it, patch sections with `doc_patch_section` instead.
- A block structure that is already clear goes into `architecture.md` (blocks, power
  tree, sheet split); the schematic session follows its sheet split.
- Record each decision (part choice, topology, trade-off) with `doc_append` on
  `decisions.md`.
- Do not write `board.md` here.
