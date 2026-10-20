---
name: session-schematic
description: Method for a schematic session - build the schematic from the approved design documents, keep the documents in step and record deviations. Load at the start of every schematic session.
sessions: schematic
---

# Schematic session

In this session you build the schematic from the design documents. Load the
`kicad-schematic-editing` skill before the first edit and `design-docs` for the doc
tool rules.

## Before the first edit

1. Call `doc_list`, then `doc_read` `requirements.md`, `architecture.md` and
   `parts.md` (by section if they are long).
2. **The gate.** If the first notice says that `requirements.md` is missing or not
   approved, tell the user once, briefly, that you are working from unapproved
   requirements, and continue. Never refuse work because of it and never repeat it.
3. Follow the sheet split of `architecture.md` (`Sheets`): one sheet per block as
   listed there. When `architecture.md` has none, propose a split and record it as a
   decision.
4. Use the parts of the `Key parts` table in `parts.md`; check their notes with
   `parts_get_notes` before placing an active part.

## While editing

- Requirements and architecture are not changed in this session. When the schematic
  has to deviate from them (a different part, an extra rail, another sheet split),
  record the deviation with `doc_append` on `decisions.md` (title, decision,
  rationale) and add a bullet to the `Open points` section of the affected doc with
  `doc_patch_section` in `mode: "append"`.
- When a key part is in the schematic, set its status in the `Key parts` table of
  `parts.md` to `placed` with `doc_patch_section` on `Key parts`.
- New questions for the user go into `Open points` of the doc they belong to.
