# Coordinator brief — 2026-10-17 (sub-project 2: schematic editing)

Handoff from the sub-project 2 brainstorming/spec session to the next
CopperChat coordinator session. Read this, then the spec.

## Where we are

- Foundation (sub-project 1) is complete and released (`v0.1.0`); all
  foundation PRs merged. As-built facts: plan index
  `docs/superpowers/plans/2026-10-15-foundation-plan.md` (C7a, X-items,
  "Decided by owner").
- Spec for sub-project 2: `docs/superpowers/specs/2026-10-17-editing-design.md`
  — schematic editing, plan-before-edit, visual control. **Only proceed once
  the owner has approved the spec PR.** The owner answered the ten open
  design questions on 2026-10-17 (spec §15 "Decided by owner": per-sheet
  annotation blocks, passives 0805 part preferences, no edits outside an open
  plan, ERC errors fixed or justified per step, route touch-up by
  coordinates, block duplication). The remaining author calls are listed
  under §15 for review.
- The owner's first real test of the foundation went well (no blocking
  findings, 2026-10-17).

## Next steps (in this order)

1. **Spikes 2A–2D** (spec §13), throwaway, results in `docs/spikes/2x-*.md`
   with a gate line each. 2A (headless write round-trip) and 2B (library
   access) in parallel; 2C (diff/highlight) in parallel; 2D (agent tool-shape
   eval) after 2A. A **no-go in 2A** (a core edit needs a text edit) stops
   everything → escalate to the owner.
2. **Implementation plan** (superpowers `writing-plans`) written by a child
   from the spec + spike results, as a PR; same index/phase-file structure as
   the foundation plan (contracts, as-built sections per phase).
3. Implementation, one child per phase/PR.

## Suggested phase shape (for the plan-writing child)

1. `KicadSession.commit()`, mid-turn save, geometry (grid, bounding boxes,
   pin ends), verification helpers, change log + migration `0006_editing.sql`.
2. Library index + `lib_*` tools, `parts_get_notes`, `part-preferences`.
3. Two-phase adapter (`prepare`/`apply`), place / fields / move / delete /
   per-sheet annotate / duplicate tools.
4. Connect / reroute / labels / power / no-connect / rename net (router +
   verification).
5. Sheets + sheet pins.
6. Plans: model, budget enforcement, plan tools, REST/WS, step checkpoints.
7. Frontend: plan card, changes view, viewer highlight, history nesting,
   Settings › Editing.
8. Skills (`kicad-schematic-editing`, style recipes), scenario tests,
   live eval suite, docs.

Phases 2 and 3 can start after 1; 7 after 6's contracts are fixed.

## How the owner wants to work (unchanged)

- Delegate to Kontor child sessions (`kontor-manager-cycle`); coordinator
  stays lean, never works in the main checkout, merges via `gh` CLI (GitHub
  MCP auth is broken).
- **No Copilot.** Children self-review with `review-loop` (max 2 rounds).
- Owner comms German, repo English.
- Never hand-edit KiCad files — API / `kicad-cli` only; that now includes
  `ParseAndCreateItemsFromString` (spec §2 anti-goals).
- Before spawning: `git pull --ff-only` in the main checkout.

## Environment facts (verified 2026-10-17)

- Pinned KiCad `0616634034fa1a8c92c141c293342cbef2f68dff`; kipy
  `87b945a0d56b26a78bf547db9771ff9bf3a057ea`. `~/build/kicad` has the pinned
  commit fetched (`git show 0616634034:<path>`; its checkout is at
  `69cafb907e`).
- Schematic write API at the pin: see spec §13 "Facts already checked"
  (create/update/delete for wires, labels, junctions, no-connects, symbols,
  sheets; `PlaceSymbolFromLibrary`; library listing via `GetLibraryItems` /
  `GetItemsFromLibrary` / `LoadAllLibraries`; no annotate, no schematic
  `GetBoundingBox`, no undo of a pushed commit; staged creates/deletes are
  invisible until push; headless has no editor frame → no auto-annotate,
  no field autoplace, no connectivity recalc after updates).
- Existing `save_copy`/`run_erc` byte-copy sub-sheets from disk — phase 1
  must make them save first once documents can be modified (spec §4, §5.1).
- `kicad-cli sch diff` exists (JSON/text/PNG/SVG) — spike 2C.
- Kontor lessons from the foundation (coordinator memory `coordinator/brief`):
  a child whose follow-up depends on a later signal must not have its PR
  merged first (worktree auto-retired); token costs per child are high —
  keep phase files tight.
