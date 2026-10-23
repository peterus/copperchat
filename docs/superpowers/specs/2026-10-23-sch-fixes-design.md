# sch_* tools: correctness and data loss (patch v0.4.1) — design

Status: draft for owner review (2026-10-23). PM topic #14. Patch release between
v0.4.0 (repository context) and the capability package PM topic #15 (v0.5.0),
both before sub-project 4 (board).

## 1. Purpose

A real CopperChat session on the OpenDriveHub main PCB (prod v0.4.0, 2026-10-23)
hit several wrong behaviours of the schematic tools. One of them silently loses
work: a failed `sch_move` could not be undone exactly, the turn was aborted and
the design was reloaded from its last save. That threw away an earlier,
successful but unsaved `sch_delete` of the same turn. The user saw an
incomprehensible error, asked the agent what happened, and the agent could not
explain it either.

Goal: fix these defects so that the agent's edits are correct, the tools agree
with each other, and **no failure path ever discards successful work**.

## 2. Scope

### Goals

The eight items of the feedback (numbers as in the original feedback), plus the
error-reporting follow-ups the owner added for #6:

| Item | Short |
|------|-------|
| #6 | Data loss after a failed compensation (P0) |
| #6b | Understandable inconsistency notice; no bogus "agent could not start" |
| SP | Left sheet pins: text outside the box |
| #7/#8 | `sch_move` does not carry stubs / power symbols |
| #9 | Sheet-edge rule differs between `sch_connect`, `sch_add_wire`, `sch_reroute` |
| #10 | `sch_set_fields` description vs. schema |
| #11 | `sch_add_power` stubs touch neighbour pins |
| #14 | Value field over body/pins (`Connector_Generic:Conn_01x14`), not caught by overlap check |
| #5 | Hierarchical-label column in a sub-sheet full after ~11 pins; parallel calls collide |

### Non-goals

- Everything in PM topic #15: addressing sheet pins by UUID, rotate/mirror for
  labels/sheets, a `sch_list_items` tool, `expect_net` for `sch_move`, ERC
  sheet filter/pagination, plan budget counting.
- Board / PCB tools (sub-project 4). New UI beyond the changed notice texts.

### Anti-goals

- A failure path that silently drops successful edits — the thing this patch
  exists to kill.
- Large heuristic workarounds in CopperChat for a defect that lives in KiCad
  (owner guideline, PM topic #16). Small, obvious workarounds are fine.
- Hand-editing KiCad files, ever. All edits go through the KiCad IPC API.
- A tool producing geometry another tool rejects.

## 3. Working method (binding)

For **every** item, in this order:

1. **Reproduce** on a small test project against real KiCad in the container.
   Nothing is fixed on the strength of the feedback's guesses; they are unchecked.
2. **Locate** the cause: CopperChat, KiCad IPC API, or kicad-python.
3. **Test first:** a regression test that fails on today's main (against real
   KiCad where technically possible, the existing `backend/tests/kicad/edit/`
   style).
4. **Fix.**
5. **Status** recorded for the final report: *fixed* / *not reproducible* /
   *in KiCad* (with minimal repro and fork/MR state) / *open*.

For the left-sheet-pin item, compare the **`.kicad_sch` text**, not only the
render: the pin line CopperChat writes vs. a pin placed in the KiCad GUI on the
left edge.

## 4. Key decisions (owner, 2026-10-23)

- **D1 (#6):** Two layers. (a) Fix the root cause of the failed compensation.
  (b) **Save the real project files after every successful mutating edit**, so
  that an unrecoverable failure falls back to the state just before the failing
  edit (the last save), never further back. The owner accepted that project
  files on disk (and so `git status`) change during a turn, not only at its end.
- **D2 (#6b):** The notice says in plain words which edits were lost (if any) and
  which restore action is the right one; the follow-on "The agent could not
  start" error after the interrupt disappears.
- **D3 (#7/#8):** Moving a symbol carries everything that hangs only on its pins.
  Moving just a label / power symbol leaves the symbol in place.
- **D4 (#9):** One rule for all tools: wires keep clear of sheet edges. New
  geometry strict; existing violations only warned.
- **D5 (KiCad):** If a cause lies in KiCad / kicad-python, fix it in a **local**
  fork; no GitLab fork or MR is created now (the owner's GitLab account is not
  set up). The MR is prepared later from the same commits, and shown to the owner
  before anything is submitted.

## 5. #6 — no silent data loss

### 5.1 What happened (prod, 2026-10-23, run `be26737b`)

1. Successful edits 15:12–15:13 (wire deletes, 6 labels, rewiring); the design
   was saved at 15:13:35.
2. 15:14:15 `sch_delete #FLG01` succeeded (unsaved).
3. `sch_move #FLG02` failed; `_compensate`
   (`backend/src/copperchat/kicad_tools/edit/engine.py`) undid it, but the
   re-check found wire `8b946cae-…` missing → `DocumentInconsistent`.
4. The turn was interrupted, no autosave, session reopened from disk at the
   15:13:35 state → the `#FLG01` delete was lost.
5. The interrupt landed mid tool-use; the SDK reported
   `[ede_diagnostic] result_type=user … stop_reason=tool_use` and CopperChat
   showed it as "The agent could not start".

Hypothesis (unverified, to be checked in step 2 of §3): a wire deleted or merged
by the move is re-created during compensation via `create_items` and gets a new
UUID, so the UUID check reports it missing.

### 5.2 Root cause fix (D1a)

Reproduce the exact sequence on a test project (power flag on a wire, delete one
flag, move the other so its pin lands where the move must split/merge a wire).
Find why the wire does not come back with its UUID and fix it so that
compensating a `sch_move` restores the document exactly. If KiCad cannot
re-create an item with a given UUID, that is a KiCad finding (D5); a CopperChat
workaround is acceptable only if it is small (e.g. compare the restored wire by
geometry + net and remap its UUID in the change log, the same way re-placed
symbols are already remapped).

### 5.3 Save after every successful edit (D1b)

Why not a separate checkpoint copy: the IPC API writes only the root sheet;
sub-sheet files are current on disk only after a real save
(`KicadSession.save_copy` already saves the project first for that reason, see
`backend/src/copperchat/kicad_session/session.py`). A checkpoint that leaves the
project files untouched is therefore not possible without KiCad changes.

- After **every successful mutating edit** (a two-phase tool whose change was
  applied and verified), CopperChat saves the modified documents (schematic and,
  if modified, board) to the project files.
- When a later edit ends in `DocumentInconsistent`, reopening from disk (today's
  behaviour) now yields the state right **before the failing edit**. Every
  successful edit survives; only the failing edit is gone.
- The `auto_restore` path (deleted symbols that cannot be re-placed losslessly,
  deleted sheet symbols, shared sheet files; `history/hooks.py`) today restores
  the plan-step checkpoint and then the turn snapshot, both older than the
  turn's earlier edits. With per-edit saves it restores **the last save** too,
  for schematic and board together. Plan-step checkpoints and the turn snapshot
  stay available as explicit undo actions for the user, not as automatic
  failure fallback.
- Whatever reacts to saves must keep working with saves in the middle of a turn
  (e.g. external-change detection via document hashes, view updates, history
  snapshots, ERC baseline, the end-of-turn autosave becoming a no-op when
  nothing changed). The builder checks each consumer of a save.
- A save that fails is reported like today's KiCad failures; it must not be
  swallowed.
- **Measure the cost** (time per save on the OpenDriveHub main schematic) and
  record it in the PR. If it is clearly above ~0.5 s per edit, bring the number
  to the owner before shipping; do not silently weaken the guarantee.

Acceptance test: force a compensation failure (the existing `_before_compensate`
seam) after one successful edit in the same turn; afterwards that edit is still
in the document and nothing is reported as lost. Same for an `auto_restore`
failure.

### 5.4 Notice and agent error (D2)

- The `document_inconsistent` tool result and the user notice name the lost edits
  in plain language (tool + reference, e.g. "the delete of #FLG01"), or say that
  nothing was lost (the normal case once every edit is saved, §5.3).
- When there are several restore options, the notice says which one to use and
  why (normally: none needed; otherwise the most recent state that keeps the
  finished work).
- The agent-facing text gives the agent enough to explain it to the user
  afterwards (what failed, what was restored, what was lost).
- The interrupt after an inconsistency must not surface as "The agent could not
  start". Treat the SDK's error result for a CopperChat-initiated interrupt as an
  expected end of turn (log it, don't show it as a start failure).

## 6. Left sheet pins (SP)

Symptom: `sch_add_sheet_pin(..., side="left")` puts the pin text outside the box
to the left; right pins are inside. Wires then run through the text.

Repro: `sch_add_sheet`, one pin left, one right, `kicad-cli` plot, and diff the
`.kicad_sch` pin lines against a left pin placed in the KiCad GUI (angle,
`justify`). Fix where the difference comes from: CopperChat's arguments (likely
small fix) or the IPC API (D5). Acceptance: the file content of a left pin
matches the GUI one; the text lies inside the box.

## 7. #7/#8 — `sch_move` carries what belongs to the symbol (D3)

Definitions:

- A **stub chain** of a moved pin: the wires reachable from that pin's end point
  with no junction, no branch (a point joining three or more wire ends), and no
  other pin along the way. Length does not matter.
- At the chain's far end there may be **only** power symbols, labels (any kind)
  or no-connect flags — or nothing. If anything else is attached there (another
  symbol's pin, a sheet pin, a further wire), the chain is not owned and today's
  behaviour applies (the wire is rubber-banded).

Behaviour:

- **Moving a symbol:** all owned stub chains and the items at their ends move by
  the same offset as the symbol; their shape is preserved (no L-shapes).
- **Moving only a label or power symbol:** the symbol it hangs on stays; the wire
  is rubber-banded to the new position. Such a move is no longer rejected with
  "pin would land on a wire" just because its own stub is there; it is rejected
  only if it would create an unwanted connection.
- The documentation and tool description state this behaviour.

Repro cases from the feedback: C401 (Device:C vertical, +3V3 above, GND below,
offset +7.62 mm) and SW301.

## 8. #9 — one sheet-edge rule (D4)

- **Rule:** no wire segment may lie on a sheet symbol's edge or run parallel to it
  within **2.54 mm**. Exception: a wire arriving at a sheet pin may touch the edge
  at that pin, perpendicular to the edge; it may not run along it.
- One shared check used by `sch_connect`, `sch_add_wire`, `sch_reroute` (and any
  other tool that writes wires, e.g. the move rubber-banding).
- `sch_connect` plans its routes with this clearance, so it never produces what
  the others reject.
- **Existing** wires that violate the rule: tools working near them warn, but do
  not refuse; only newly written geometry is checked strictly.

## 9. #10 — `sch_set_fields` description vs. schema

The description mentions `value` / `mpn`, the schema accepts only
`fields={"Value": …, "MPN": …}`. Align the description to the schema (no new
parameters). Add a test over **all** tools that every parameter name the
description promises exists in the input schema.

## 10. #11 — `sch_add_power` next to neighbour pins

Repro: microSD footprint, 2.54 mm pin pitch (VDD/CMD, VSS/DAT0). Before writing a
stub, check that its end point and segment touch no foreign pin, stub or wire. If
they do, try another direction / length; if no variant is free, fail with a clear
error. Never create an unintended connection. Afterwards `sch_add_label` must find
room next to the neighbour pin.

## 11. #14 — value field placement and overlap check

Repro: `sch_place_symbol` with `Connector_Generic:Conn_01x14`. The value field is
placed beside the body, not over body or pins, and the overlap check covers
field-vs-body and field-vs-pins so this case is reported if it happens anyway.

## 12. #5 — hierarchical labels in a sub-sheet

Today the column at the left margin (x = 25.4 mm, pitch 15.24 mm) is full after
~11 pins ("No room for the hierarchical label"), and parallel calls get the same
spot.

- Place labels on a 2.54 mm-grid pitch tight enough for normal label heights
  (exact pitch is the builder's call; labels must not overlap), and open a second
  column when the first is full.
- Serialise the free-spot search per sheet (a per-sheet lock), so parallel calls
  never get the same place.

## 13. KiCad fixes (D5)

- If an item's cause lies in KiCad or kicad-python: make a **local** fork clone
  (outside the CopperChat repo), branch `copperchat`, commit the fix there with a
  minimal repro.
- The base image must still build in CI, which cannot reach a local clone.
  Builder's call (author's suggestion): carry the fork commits as patch files in
  `docker/kicad-base/patches/` applied after checkout of the pinned
  `KICAD_COMMIT` / `KIPY_REV`, then build and pin a new base image the usual way
  (`.github/workflows/kicad-base.yml`).
- Prepare MR text + diff in the fork for later; **nothing is submitted
  upstream** without the owner seeing it first.
- If only a trivial CopperChat workaround is needed instead, prefer that and note
  the KiCad defect for later.

## 14. Testing

- One regression test per item (§3), against real KiCad where possible.
- #6: forced compensation failure after a successful unsaved edit (§5.3).
- #9: one table-driven test feeding the same geometries to all three tools.
- #10: description-vs-schema test across all tools.
- #5: concurrency test with parallel label placements on one sheet.
- CI green.

## 15. Documentation

- Tool descriptions (#7/#8, #9, #10) and the user docs on editing / recovery
  (#6 notice, what is restored).
- CHANGELOG entry for v0.4.1.
- Run `scripts/check-docs.sh` before the release PR.

## 16. Release

- Patch release **v0.4.1**. Tag only after the release PR is verified MERGED.
- Prod upgrade only with owner OK; back up `data/` with the app stopped.
- Final report to the PM (`pm_report`): status list for all items.

## 17. Calls made by the author (not owner decisions)

- Threshold 0.5 s per save for "ask the owner".
- Stub-chain definition details (junction/branch/other-pin stop conditions;
  sheet pins at the end make a chain not owned).
- 2.54 mm as the sheet-edge clearance (owner confirmed the number).
- Patch files in `docker/kicad-base/patches/` as the transport for local fork
  commits.
- #10 fixed by aligning the description, not by widening the schema.
- #5: per-sheet lock for the free-spot search; second column when full.
