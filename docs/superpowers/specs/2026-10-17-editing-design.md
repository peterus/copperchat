# CopperChat — Schematic editing (sub-project 2) design

Date: 2026-10-17
Status: draft, awaiting owner review

## 1. Purpose

Sub-project 1 (Foundation, `2026-10-15-foundation-design.md`) gave the agent
eyes: it reads schematic and board through KiCad's IPC API, runs ERC/DRC,
renders an agent view it looks at, researches parts, and every turn is
snapshotted for undo. It cannot change anything.

Sub-project 2 lets the agent **edit the schematic**: place parts from the
libraries, connect them, label nets, change values and footprints, move and
delete things, and work with hierarchical sheets. It does that in a way the
user can trust:

- every edit goes through KiCad's own API and is **read back and verified**
  before the tool reports success;
- larger changes are **planned first**, approved by the user, then executed
  step by step with a checkpoint after each step;
- the user **sees what changed** (before/after, change list, highlight in the
  viewer), and the agent checks its own work with ERC and the agent view;
- everything stays undoable through the Phase 7 history.

### Why this is the next step

The foundation spec's §2 and §13 place schematic editing second: the board
(sub-projects 3/4) is derived from the schematic, so editing the schematic is
the first thing that makes CopperChat a design tool rather than a reviewer.
The owner's first real test of the foundation (2026-10-17) went well with no
blocking findings, so no foundation rework is scheduled ahead of this.

### Lessons carried over

The foundation's original motivation (spec §1) applies with full force to
editing: the MCP tools it replaced wrote files ad hoc, misreported state and
reported false failures and false successes. The rules in §3 below
(API-only writes, read-back verification, structured errors) exist so that
CopperChat does not repeat that.

## 2. Scope

### In scope

1. **Library access** — search symbols and footprints in the configured
   libraries (global + project tables), inspect a symbol's pins, units and
   default fields before placing it.
2. **Place** symbols (incl. power symbols, multi-unit parts) with reference,
   value, footprint and extra fields (MPN, manufacturer, datasheet).
3. **Connect** pins: by wire (short, orthogonal, computed by CopperChat) or by
   net label at each pin (stub + label), plus no-connect markers and
   junctions.
4. **Labels**: local, global, hierarchical, power symbols; rename a net.
5. **Edit** fields (value, footprint, MPN, DNP, exclude-from-BOM/board),
   **move / rotate / mirror**, **delete** (symbols, wires, labels, sheets).
6. **Annotation** of new or all symbols, with stable re-numbering rules.
7. **Hierarchical sheets**: create a sheet (new file), sheet pins and the
   matching hierarchical labels, place parts on a given sheet.
8. **Plan before edit** for larger changes (§7).
9. **Visual control**: per-turn change list, before/after view, highlight
   of changed items, ERC delta after every edit (§8).
10. **Skills**: schematic style by circuit type grows into an editing skill;
    use of `notes.md` from part research when placing a researched part (§9).

### Non-goals (sub-project 2)

- Any `.kicad_pcb` change, including "update PCB from schematic" — that is
  sub-project 3. After schematic edits, DRC schematic-parity findings are
  expected and reported as such, not fixed.
- Creating or editing **library** symbols or footprints. A missing part is
  reported to the user with the closest library alternatives (e.g.
  `Device:R`, a generic connector) and an explicit question; symbol/footprint
  creation is a candidate for a later sub-project (§14).
- Buses and bus entries (read-only stays as is), simulation directives,
  variants, rule areas, tables, graphics/text boxes beyond plain text notes.
- Automatic whole-sheet "beautify"/re-layout of existing user schematics.
- Editing while the KiCad GUI holds the project (foundation: read-only with
  "close it there"); no live co-editing.

### Anti-goals (would make this a failure even if it "works")

- The agent changes things the user did not ask for, or more than it said
  it would.
- An edit that **looks** connected but is not (a wire ending near but not on
  a pin, a label not touching a wire), or a wire that silently shorts by
  passing over a pin end — and the tool reports success.
- Any write that bypasses the KiCad API: no file text edits, no S-expression
  generation, and also **no** `ParseAndCreateItemsFromString` (it is text
  generation by another name).
- A change that cannot be undone from the History panel.
- A half-applied plan that leaves no clear record of what was done.

## 3. Key decisions

| Topic | Decision |
|---|---|
| Write path | KiCad IPC API only (`kicad-cli api-server` + kipy), `BeginCommit`/`EndCommit` per tool call; `kicad-cli` only for checks/exports/diff. Unchanged rule from the foundation |
| Atomicity | One tool call = one KiCad commit. Everything that can be checked **before** writing (targets, geometry, collisions, intended connectivity) is checked before `BeginCommit`; a failure while staging drops the commit. A mismatch found **after** the push is undone by a compensating commit built from the pre-images the tool read before writing (§5.1) |
| Verification | Every mutating tool reads its result back from the API (items + netlist) **after** the commit is pushed (staged creates/deletes are not visible before, see §13) and only then reports success; mismatch = compensating commit + structured error |
| Tool level | High-level, intent-shaped tools (`sch_connect`, `sch_place_symbol` …) plus one low-level escape hatch (`sch_add_wire`). Coordinates in mm on the 1.27 mm (50 mil) grid, snapped by the tool |
| Connection geometry | Computed by CopperChat (pin positions from the API, orthogonal routing around symbol bounding boxes, collision check against foreign pin ends); the model never has to hit a pin by hand |
| Annotation | CopperChat assigns references itself (next free number per prefix, all sheets) and writes them through the API; it does not depend on headless auto-annotation (spike 2A) |
| Plan before edit | Server-enforced **change budget** per turn; beyond it mutating tools refuse until an approved plan exists (§7) |
| Checkpoints | Turn snapshot (Phase 7, unchanged) + one checkpoint per executed plan step (save + snapshot), stored with `kind = step` so crash recovery and pruning keep treating the turn snapshot as the turn's undo point (§5.4) |
| Saving mid-turn | Allowed and explicit: before every ERC/diff run and every step checkpoint the schematic is saved through the API (`save()`), hashes are recorded right after, so on-disk sub-sheets are never stale (§5.1) |
| Diff | Semantic change list from API data recorded by the tools themselves (no file parser); `kicad-cli sch diff` for the visual before/after if spike 2C confirms it |
| Libraries | KiCad's library API (`GetLibraryItems`, `GetItemsFromLibrary`, `LoadAllLibraries`; kipy `get_library_symbols`, `get_library_symbol`, `get_library_footprints`, `load_all_libraries`) is the source for listing and pin data; CopperChat keeps an in-memory search index built from those results — no library file parsing (spike 2B measures cost) |
| Part knowledge | New read-only tool `parts_get_notes(mpn)` exposes `docs/parts/<MPN>/notes.md` to the main agent without a research run |
| Scope of agent tools | Main agent still has **no** file/shell/web tools; all new tools are CopperChat MCP tools behind the existing guard + `PreToolUse` hook |

## 4. Architecture

No new services or containers. Changes by backend module (names from the
foundation, spec §4.1 and plan C7a):

| Module | Change |
|---|---|
| `kicad_session` | `KicadSession.commit(label)` async context manager around kipy `begin_commit` / `push_commit` / `drop_commit` whose cleanup is shielded from cancellation (§5.3); `load_all_libraries` after open; mid-turn `save(doc)`; `save_copy("sch")` saves first when the document is modified (`GetDocumentModifiedState`), because the foundation procedure byte-copies sub-sheets from disk (spike 1C) — this also fixes the existing `run_erc`/views for edited documents. Every CopperChat save records hashes right after (spike 1C: `save()` rewrites files) |
| `agent` (`sdk_tools` adapter) | Before `on_mutation`: refuse mutating tools on a read-only session and run the budget pre-check, so no snapshot is taken for a call that will be refused |
| `kicad_tools` | New `edit/` package: `library.py` (search/inspect), `place.py`, `connect.py` (router), `fields.py`, `transform.py`, `delete.py`, `annotate.py`, `sheets.py`, `verify.py` (read-back + ERC delta), `geometry.py` (grid, bounding boxes, pin ends, collision). Plain functions, no Claude dependency, like the foundation tools |
| `tools` | New providers `copperchat.kicad_tools.edit.specs`, `copperchat.plans.specs`, `copperchat.parts.specs`; mutating specs set `mutating=True`, `changes_documents=True` (existing `ToolSpec` fields) |
| `plans` (new) | Plan model, storage, state machine, change budget, step execution bookkeeping (§7) |
| `changes` (new) | Per-turn change log (what each tool changed, with UUIDs and refs), change list for the UI, link to snapshots/checkpoints (§8) |
| `history` | Checkpoint snapshots for plan steps: same store, new columns `kind` (`turn` default / `step`) and `plan_step_id` in `0006_editing.sql`; `latest_for_run` and `_recover` select `kind = turn` only; `HISTORY_KEEP` applies to turn snapshots, step checkpoints are pruned with their turn; restore to a checkpoint |
| `views` | Before/after image for a turn or step; highlight set for the viewer; optional `kicad-cli sch diff` render |
| `skills` | Built-in `kicad-schematic-editing` skill; `kicad-schematic-style` extended (§9) |
| `web` | REST for plans and changes; WS frames `plan-updated`, `changes-updated` (§6.3) |
| `db` | Tables `plans`, `plan_steps`, `changes` (migration `0006_editing.sql`) |

Frontend:

- **Plan card** in the chat (§7.3).
- **Changes** view: per turn/step change list; tapping an entry selects the
  item in the viewer; "before / after" toggle (history preview vs. current).
- **Viewer highlight** of changed refs after a turn (KiCanvas
  `viewer.select(ref)` per spike 1B; multi-select tested in spike 2C).
- **History panel**: steps shown nested under their turn; restore to any
  checkpoint.
- **Settings › Editing** per project: plan policy (`always` / `large changes`
  (default) / `never`), change budget value, "show agent view after each step"
  (default on).

## 5. Edit model and data flow

### 5.1 One mutating tool call

1. Guard + hook allow the call (unchanged mechanism).
2. Adapter pre-checks (before `on_mutation`): read-only session → refuse;
   plan policy `always` without an active step → refuse (§7.1). Nothing is
   snapshotted for a refused call.
3. **Plan the change without writing**: resolve targets (refs → UUIDs +
   sheet path; pins → absolute positions), compute geometry (snap, route,
   collision check), compute the intended connectivity, and **read
   pre-images** of every item that will be updated or deleted.
4. Budget check (§7.1) on the now-known change count. Refused → structured
   error, nothing changed.
5. First mutating call of the turn → Phase 7 `on_first_mutation` snapshot
   (unchanged; a failed snapshot fails the call).
6. `async with session.commit(label)`: `create_items` / `update_items` /
   `remove_items` / `place_symbol_from_library`. A staging error drops the
   commit (document unchanged); otherwise the commit is pushed.
7. **Read back after the push** (at the pinned commit created/deleted items
   are only applied to the screen when the commit is pushed — staged changes
   are invisible to `GetItems` and the netlist): affected items by id, plus the
   netlist for any connectivity change (netlist export job, as `sch_get_net`
   does today). Compare with intent (e.g. "U1.3 and R5.1 are on the same net
   and no other net merged into it").
8. Mismatch → **compensating commit** from the pre-images (remove what was
   created, restore updated items, re-create deleted items with their
   original UUIDs), read back again to confirm the document equals the
   pre-images, then a structured error. If compensation itself fails, the
   tool reports `document_inconsistent`, the turn ends, and the user is
   offered the restore of the last checkpoint or turn snapshot (foundation
   restore flow). Spike 2A verifies re-creation with original UUIDs; if that
   is impossible, a failed delete is compensated by restoring the last
   checkpoint/turn snapshot instead.
9. Record a change entry (§8.1), report `changed_docs={"sch"}`.
10. Tool result: structured JSON (what was created/changed with full UUIDs and
    refs, final positions in mm, nets touched, connectivity verified), no
    image and **no ERC** by default.

**ERC and diff need saved files.** `kicad-cli` runs on a saved copy, and the
foundation's `save_copy` byte-copies sub-sheets from disk, so ERC after
unsaved edits would check stale sub-sheets and miss new sheets. Therefore the
ERC delta is computed at **plan step end** and at **turn end of an editing
turn**, and whenever the agent calls `run_erc`: in each case the schematic is
saved through the API first (then hashes recorded, then `save_copy`, then
`kicad-cli sch erc`). The delta is against the ERC of the turn's (or step's)
starting state. Per-call ERC is not offered.

Turn end: autosave, hash record, `publish_view` (unchanged), plus the
turn-end ERC delta for editing turns (§8.3).

### 5.2 Grid, positions and collisions

- All positions in mm, snapped to 1.27 mm; symbol origin positions as KiCad
  reports them.
- `geometry.py` maintains bounding boxes per sheet and pin end positions
  from the placed symbols' pins. `GetBoundingBox` is registered only for
  boards at the pinned commit, so schematic boxes are derived from the
  symbol definition's body graphics + pins + transform as returned by the API
  (spike 2A confirms the data is complete; fallback: pins' extent plus a
  margin).
- Placement refuses overlap with existing symbol bounding boxes (plus a
  margin) and offers the nearest free position in the error's suggestion;
  `sch_find_space` returns free areas of a requested size near a point or ref.
- Router: orthogonal L/Z routes between two pin ends, avoiding symbol bodies
  and never passing over a foreign pin end or wire end (that would connect it).
  If no clean route of ≤ 3 segments exists, `sch_connect` falls back to
  labels (when `via=auto`) or fails with a suggestion (`via=wire`).
- Junctions: added where the result has a T-connection, verified by read-back.

### 5.3 Concurrency and failure

- One project, one agent run at a time (foundation); tool calls are serial.
- **Cancellation between `BeginCommit` and `EndCommit`** (Stop, interrupt
  backstop, `CancelledError` in the tool handler): the context manager's
  cleanup is shielded (`asyncio.shield`) and always sends `drop_commit`
  (or completes the push if it was already sent), so the server never keeps
  the client in its active-commit set. If the cleanup itself fails or times
  out, the session is restarted (foundation restart path) rather than reused,
  because later writes would be staged into the stale commit.
- KiCad crash mid-call: the commit is lost with the process; foundation
  recovery (restart + offer snapshot restore) applies; the plan (if any) goes
  to `paused` with the step marked `failed`.
- External file change between turns: foundation hash check + `revert()`;
  an active plan is paused and the user is told the plan's base changed.

### 5.4 Step checkpoints and the turn snapshot

- The turn snapshot (`kind = turn`, taken by `on_first_mutation`) stays the
  turn's undo point: crash recovery (`_recover`, `latest_for_run`) and the
  History panel's "undo turn" use it, never a step checkpoint.
- A step checkpoint (`kind = step`, `plan_step_id`) is taken at
  `plan_step_done`: save through the API → record hashes → snapshot. It is
  listed nested under its turn and can be restored like any snapshot.
- Retention: `HISTORY_KEEP` counts turn snapshots only; a turn's step
  checkpoints are pruned together with it.
- Because hashes are recorded after every CopperChat save, a turn that later
  fails with `kicad_failed` (no autosave) is not mistaken for an external
  change at the next turn start: the on-disk state is the last step
  checkpoint, which matches the recorded hashes. The recovery notice offers
  both the last step checkpoint (keep finished steps) and the turn snapshot.

## 6. Agent tools (sub-project 2)

Rules from the foundation stay (structured JSON, full UUIDs, mm, filters,
errors only for real failures, success only after verification). New rule:
**every mutating tool states exactly what it changed**.

### 6.1 Read-only additions

| Tool | Returns |
|---|---|
| `lib_search_symbols(query, library?, limit?)` | `lib_id`, description, keywords, unit count, pin count, default footprint, footprint filters |
| `lib_get_symbol(lib_id)` | pins (number, name, electrical type, unit, position relative to origin), units, body styles, default fields |
| `lib_search_footprints(query, library?, limit?)` | `lib_id`, description, pad count |
| `sch_find_space(sheet?, size_mm, near?)` | free rectangles near a point or ref |
| `sch_get_pin(ref, pin)` | absolute position, net, connected items, whether it is free |
| `parts_get_notes(mpn)` | `notes.md` of a researched part (read-only); "not researched" otherwise |
| `sch_changes(scope=turn|step|plan)` | the change list recorded so far |

### 6.2 Mutating tools

| Tool | Does |
|---|---|
| `sch_place_symbol(lib_id, at, sheet?, rotation?, mirror?, unit?, reference?, value?, footprint?, fields?)` | places from the library; reference auto-assigned if omitted; checks the footprint exists |
| `sch_set_fields(ref, fields)` | value, footprint, MPN, manufacturer, datasheet, DNP, exclude flags; field visibility/position kept or auto-placed |
| `sch_move(items, to|by, rotation?, mirror?)` | moves symbols/labels; keeps attached wires connected (wire ends dragged) or fails |
| `sch_delete(items, with_dangling_wires=true)` | deletes; reports nets that changed |
| `sch_connect(from, to, via=auto|wire|label, net_name?)` | `from`/`to` = `REF.PIN` or a net name; wire route or stub+label; verified by netlist |
| `sch_add_label(net, at=REF.PIN|position, kind=local|global|hierarchical)` | label (+ stub at a pin) |
| `sch_add_power(net, at=REF.PIN|position, symbol?)` | power symbol from `power:` library |
| `sch_no_connect(REF.PIN…)` | no-connect markers |
| `sch_rename_net(old, new)` | renames all labels of a net (refuses global/hierarchy conflicts) |
| `sch_annotate(scope=new|sheet|all, mode=keep|renumber)` | references via CopperChat's annotator |
| `sch_add_sheet(name, file, at, size?, parent?)` | new hierarchical sheet + file |
| `sch_add_sheet_pin(sheet, name, direction)` | sheet pin + matching hierarchical label inside the sheet |
| `sch_add_wire(points, sheet?)` | low-level escape hatch; still snapped, collision-checked and verified |

`REF.PIN` uses the pin number (`U1.3`) or a unique pin name (`U1.VIN`).
Multi-unit parts address the unit implicitly through the pin.

### 6.3 Plan tools and protocol

| Tool | Does |
|---|---|
| `plan_propose(title, summary, steps[])` | creates a plan in `proposed`; each step: `title`, `intent`, `expected_changes` (counts by kind, refs/nets touched), `verification` (what will be checked) |
| `plan_step_begin(step)` / `plan_step_done(step, summary)` | brackets the execution of one step; `done` triggers the checkpoint and the step's ERC/visual check |
| `plan_revise(steps[], reason)` | only while `proposed`, or while `paused` (the user must approve again) |

WS frames (server → client, carry `projectId` like all frames):
`plan-updated{plan}` and `changes-updated{runId, stepId?, count}`.
Client → server: none new; approval goes through REST
(§7.2), which
enqueues a normal user message ("Plan approved …") so execution happens in a
regular, interruptible agent turn.

## 7. Plan before edit

### 7.1 When a plan is required

The server enforces it; the model is told the rule in the editing skill but
does not have to judge it alone.

- **Unit: one item change** = one schematic item created, modified or
  deleted, as listed in the tool's pre-computed change set (§5.1 step 3).
  Items: symbol (all field edits on one symbol in one call count once), wire
  segment, junction, no-connect, label, power symbol, sheet, sheet pin.
  Example: moving a symbol that drags three wire segments = 4 changes;
  `sch_connect` via labels on two pins = 2 stubs + 2 labels = 4.
- **Change budget per turn** (setting, default **12 item changes**, and at
  most **3 deleted symbols**), cumulative over the turn's calls outside a
  plan. The check runs after the change set is known and before anything is
  written (§5.1 step 4). A call that would exceed the budget is refused with
  `plan_required` and the suggestion to call `plan_propose`.
- Plan policy `always`: every mutating call needs an active plan step (the
  adapter refuses before the snapshot, §5.1 step 2). `never`: no budget
  (power users; still undoable).
- Calls made while a plan step is active are counted against that step's
  `expected_changes` (+50 % tolerance) instead of the turn budget; exceeding
  it pauses the plan with a structured error so the agent revises instead of
  improvising.
- While a plan is `running`, a mutating call **outside** an active step
  (between `plan_step_done` and the next `plan_step_begin`) is refused with
  `plan_step_required`.
- At most **one open plan** (`proposed`, `approved`, `running`, `paused`) per
  project. `plan_propose` while one is open is refused; the agent uses
  `plan_revise` or the user aborts/rejects the open one first.
- User messages that are not about the plan start normal turns; the turn
  budget applies to them and their edits are recorded as unplanned changes.

### 7.2 Plan life cycle

| From | Event | To | Notes |
|---|---|---|---|
| — | `plan_propose` | `proposed` | card shown; revision 1 |
| `proposed` | user Reject (REST) | `rejected` | terminal; a notice row tells the agent |
| `proposed` | user answers in chat → agent `plan_revise` | `proposed` | new revision; card updates |
| `proposed` | user Approve (REST) | `approved` | enqueues a user message "Plan *title* approved" — execution runs in that ordinary turn |
| `approved` | `plan_step_begin(1)` | `running` | |
| `running` | `plan_step_done(k)` with steps left | `running` | step checkpoint (§5.4), ERC delta, region render |
| `running` | `plan_step_done(last)` | `done` | terminal |
| `running` | Stop / interrupt | `paused` | step `interrupted`; tool commit dropped or completed atomically (§5.3) |
| `running` | step exceeds `expected_changes`, verification fails twice, unexplained ERC regression | `paused` | step `failed` with reason; push "needs attention" |
| `running` | turn ends with steps left (no error) | `paused` | reason `turn_ended` |
| `running` | KiCad crash / external change / server restart | `paused` | step `failed` / plan flagged |
| `paused` | user Resume (REST) | `approved` | enqueues "Resume plan *title* at step k"; next `plan_step_begin` → `running` |
| `paused` | agent `plan_revise` | `proposed` | user must approve again |
| `paused` | user Roll back to step k / to start (REST) | `paused` | restores step k's checkpoint (or the pre-plan turn snapshot); steps after k → `pending`; refused with `agent_busy` (409) while a run is active, like history restore |
| `proposed`, `approved`, `paused` | user Abort (REST) | `aborted` | terminal; the design is kept as it is |

REST: `POST /projects/{id}/plans/{planId}/approve|reject|resume|abort` and
`POST /projects/{id}/plans/{planId}/rollback {toStep: k | 0}`.
After each step the agent renders the affected region and must look at it
(skill rule, §8.3) before `plan_step_done`.
Plans live in SQLite; a server restart leaves a `running` plan `paused`.

### 7.3 Plan card (UI)

Title, summary, numbered steps with expected changes, per-step status
(pending / running / done / failed / interrupted), checkpoint link and
change count per finished step, buttons by state: Approve / Reject;
Stop; Resume / Roll back to… / Abort. Works on the phone layout (card in
the chat stream, buttons full width).

## 8. Visual control

### 8.1 Change log

Each mutating tool appends entries `{runId, stepId?, op: add|modify|delete,
kind, uuid, ref?, net?, before?, after?}` — `before`/`after` hold the changed
fields/positions read from the API, so the change list needs no file
parser. Shown per turn and per step; tapping selects the item in the viewer.

### 8.2 Before / after

- Default: the history preview of the turn/step snapshot (already rendered by
  Phase 7) next to the current agent view; toggle or slider in the viewer.
- If spike 2C confirms it: `kicad-cli sch diff` (PNG/SVG with added/removed
  marked, JSON) between the snapshot copy and the current saved copy, shown as
  a "diff" layer and used to cross-check the change log in tests.

### 8.3 Agent self-check

- Every mutating result states the verified connectivity (§5.1 step 7); the
  ERC delta is computed at each plan step end and at the end of an editing
  turn, on freshly saved files (§5.1).
- At each plan step end and at the end of an unplanned editing turn the
  agent renders `render_view(doc="sch", region="around <refs>")` and checks
  it against the skill's checklist (labels readable, no overlaps, signal flow
  direction, power symbols up/ground down, no wire over pins). This is a skill
  rule; the server adds a reminder line to the step result when no render
  was taken.
- `run_erc` at turn end is mandatory for editing turns (skill rule; the
  turn-end ERC delta is also shown to the user as a badge on the turn).

## 9. Skills and part knowledge

- New built-in skill **`kicad-schematic-editing`**: tool usage order
  (search library → inspect symbol → find space → place → connect → verify),
  `REF.PIN` addressing, when to use wires vs labels (delegates to the style
  skill), annotation rules, plan etiquette (propose when the budget says so,
  step size ~5–10 changes, never exceed the plan), the self-check checklist,
  and how to report missing library parts.
- **`kicad-schematic-style`** gains concrete placement recipes per circuit
  type: decoupling caps next to their supply pin, LDO/buck input left →
  output right, MCU in the centre with labelled buses, connector pinouts at
  the sheet edge, pull-ups above the signal, power flags.
- **Part notes**: when a part with `docs/parts/<MPN>/notes.md` is placed, the
  agent reads it (`parts_get_notes`) and follows its "typical application
  circuit" and "required external parts" sections; when it is not
  researched and the circuit around it is non-trivial, the skill tells the
  agent to call `research_part` first. The notes' "KiCad symbol/footprint
  mapping" section (part-notes template) feeds `lib_id` and footprint.
- Skills keep the three layers (built-in / user / project) and GUI from the
  foundation.

## 10. Security

- New tools are CopperChat MCP tools; the main agent's tool set stays
  `tools=["Skill"]` + MCP; the existing guard and `PreToolUse` hook cover
  them (spec lesson: CLI auto-approves Read/Glob in `cwd`).
- `sch_add_sheet(file)`: file name validated (no path separators beyond
  one optional relative sub-directory inside the project, `.kicad_sch`
  suffix, no overwrite of an existing file that is not already this sheet).
- `parts_get_notes`: MPN validated with the Phase 8 MPN rule; reads only
  `docs/parts/<MPN>/notes.md`.
- Library data comes only from KiCad's library API (no library file access by
  CopperChat), so no new path handling is introduced.
- Read-only projects (KiCad GUI lock) refuse every mutating tool in the
  `sdk_tools` adapter **before** `on_mutation` with a structured
  `read_only` error. This is new: Phase 7's hooks only skip the snapshot and
  the autosave for read-only sessions, which would otherwise let an edit
  "succeed" in KiCad's memory and then be silently lost.

## 11. Error handling

| Situation | Behaviour |
|---|---|
| Verification mismatch after a write | compensating commit from pre-images, re-verified; structured error with expected vs. found; nothing changed |
| Compensation fails | `document_inconsistent`; turn ends; restore offer for the last checkpoint / turn snapshot |
| Mutating tool on a read-only project | `read_only` error before any snapshot |
| Budget exceeded | `plan_required` error, nothing changed |
| Symbol / footprint not in libraries | error with closest matches; skill: ask the user, never invent |
| No clean wire route | `via=auto` → labels; `via=wire` → error with suggestion (move part / use labels) |
| Overlap at target position | error with nearest free position |
| Plan step exceeds expected changes | step paused, agent must `plan_revise` (user re-approves) |
| KiCad crash / timeout mid-step | foundation recovery; plan paused, step failed; restore offer |
| External change during an active plan | plan paused, user informed |
| ERC regression the agent cannot explain | step paused with the ERC delta attached; push "needs attention" |

## 12. Testing

- **Tool tests against a real headless api-server** (existing CI kicad job,
  `COPPERCHAT_REQUIRE_KICAD=1`), fixtures generated by KiCad: an empty
  project, `ecc83-pp`, `complex_hierarchy`, plus a fixture with a project
  symbol library.
- **Scenario tests (golden netlists)**: build reference circuits from an empty
  schematic only through the tools (LED + resistor; LDO with input/output
  caps; MCU with decoupling and an I2C sensor on a sub-sheet). Assert the
  exported netlist equals a golden netlist (by connectivity, not text), ERC
  has no errors, and every item lies on the grid.
- **Verification negative tests**: wire ending 0.01 mm off a pin, wire over a
  foreign pin end, label not on a wire → the tool must fail and leave the
  document unchanged (UUID set and netlist identical). Includes forced
  post-push mismatches (injected) to exercise the compensating commit for
  create, update and delete, and cancellation between `BeginCommit` and the
  push (next write must not land in a stale commit).
- **Read-only and budget tests**: a mutating call on a read-only session or
  over budget fails before any snapshot is taken.
- **ERC freshness test**: edit a sub-sheet and create a new sheet, then
  `run_erc` → the report covers the edits (no stale on-disk sub-sheet).
- **Undo tests**: after every scenario step, restoring the step checkpoint
  and the turn snapshot yields byte-identical documents to the recorded ones;
  rollback of a half-executed plan.
- **Round-trip tests**: every edited fixture loads in `kicad-cli` (ERC,
  netlist, SVG export) and in KiCanvas (existing Playwright viewer test on
  the edited fixtures).
- **Plan state machine** unit tests incl. interrupt, restart and external
  change; frontend tests for the plan card and changes view.
- **Live agent eval (opt-in, not CI-gating)**: a small suite of prompts
  ("add a 3.3 V LDO for this connector", "change all 0603 resistors to
  0402") run with the real model; scored on golden netlist match, ERC,
  number of tool calls and token cost; results tracked per release.

## 13. De-risk first (spikes)

Run before any implementation phase; results go to `docs/spikes/2x-*.md`
with a go / partial / no-go gate like the foundation spikes. Pinned KiCad
commit `0616634034` and kipy `87b945a0` unless a spike shows a reason to
move the pin.

Facts already checked in the source at the pinned commit (2026-10-17):
`API_HANDLER_SCH` creates/updates/deletes junctions, no-connects, wires
(`SCH_LINE`), text, labels (local/global/hierarchical/directive), groups,
symbols and sheets; `PlaceSymbolFromLibrary` (new in 11.0) places a library
symbol with optional reference, unit and orientation; kipy at `87b945a0`
exposes `place_symbol_from_library`, `create_items`, `update_items`,
`remove_items`, `begin_commit`/`push_commit`/`drop_commit`. In the
**headless** server there is no editor frame, so (a) automatic annotation on
placement only happens for symbols that always annotate, (b) field
auto-placement is skipped, (c) `RecalculateConnections` after updates is
skipped. (d) While a client has an open commit (`BeginCommit` … `EndCommit`),
created and deleted items are only staged in the commit and applied to the
screen at push time, so `GetItems` and the netlist cannot see them before the
push; and there is no API command to undo a pushed commit. Libraries: the
common `API_HANDLER_LIBRARIES` registers `GetLibraryItems`,
`GetItemsFromLibrary` and `LoadAllLibraries` (kipy `get_library_symbols`,
`get_library_footprints`, `get_library_symbol`, `load_all_libraries`); in the
headless server libraries must be loaded (`load_all_libraries`) before
`PlaceSymbolFromLibrary` finds a symbol. There is **no** annotate command or
`kicad-cli sch annotate`, and `GetBoundingBox` is only registered for
boards. `kicad-cli sch diff` exists (JSON, text, PNG, SVG).

| Spike | Questions | Gate |
|---|---|---|
| **2A — headless write round-trip** | place from stock + project library (refs, units, rotation, mirror); field update (value, footprint, custom field, DNP) incl. field positions; move/rotate with attached wires; delete; wire + label + junction + no-connect creation; does the netlist export job (and `GetSchematicNetlist`) reflect new connectivity without a frame (or is a save/reload/`revert` needed)? do wires that end on a pin connect, do T-joints need explicit junctions, are wires split? commit semantics: confirm staged creates/deletes are invisible until push, `drop_commit` leaves the document unchanged, and a compensating commit can re-create a deleted symbol/wire/label **with its original UUID** (§5.1 step 8); cancellation between begin and push (server state afterwards, §5.3); new sheet → file written on save, sheet pins ↔ hierarchical labels; saved files load in `kicad-cli` and KiCanvas; mid-turn `save()` + `save_copy` + ERC latency on a 100-symbol hierarchical schematic, and that the copy contains just-edited sub-sheets and newly created sheets; symbol bounding boxes from API data (body graphics + pins + transform) vs. KiCad's own plot | **go**: all core operations work via API (workarounds allowed if they stay API-only); **no-go** for any core operation needing a text edit → stop and escalate to the owner |
| **2B — library access** | `load_all_libraries` time and memory with the full stock libraries (~20 k symbols, ~15 k footprints); what `GetLibraryItems` returns per entry (description, keywords, footprint filters?) and whether `GetItemsFromLibrary` gives pins with positions/electrical types and units; project-specific libraries and `${KIPRJMOD}` tables; index build time and query latency | **go**: search < 300 ms after warm-up, all data from the API; **partial**: fields missing from the listing are fetched lazily per symbol |
| **2C — diff and highlight** | `kicad-cli sch diff` on snapshot copy vs. current (hierarchical incl.); JSON shape, PNG usefulness, runtime; KiCanvas multi-select / highlight of a set of refs (patched build, fork `peterus/kicanvas`) | **go / partial** (partial = change log + before/after previews only, no diff layer) |
| **2D — agent tool-shape eval** | with throwaway prototypes of `sch_place_symbol`, `sch_connect`, `sch_add_label` and the editing skill: can the current model build the three scenario circuits from §12 reliably? tool calls, tokens, failure modes, visual quality; is `sch_connect` with computed geometry enough or does the model need more geometry tools? | informs final tool list and default budget; no hard gate |

2A and 2B are independent and can run in parallel; 2D needs 2A's findings;
2C is independent.

## 14. Outlook

- **Sub-project 3** (Schematic → PCB): update PCB from schematic, footprint
  assignment checks across libraries, outline, rules, mechanical import —
  starts from a schematic CopperChat can now produce.
- **Library editing** (candidate, between 2 and 3 or within 3): create
  project-library symbols from a datasheet pin table (part notes) and
  footprints via IPC-7351 generators — needs its own spec and spikes (the API
  has a symbol-editor context, `headless_symbol_editor_context.cpp`).
- **Sub-project 4**: placement, routing, zones, fabrication output.
- Possible later: whole-sheet tidy, buses, variants.

## 15. Calls made by the author

The owner skipped the brainstorming questions on 2026-10-17; these decisions
were made by the spec author and are the points to check in review:

1. Scope boundary: no `.kicad_pcb` changes and no library editing in
   sub-project 2.
2. High-level, geometry-computing tools instead of letting the model draw
   coordinates (low-level `sch_add_wire` kept as escape hatch).
3. Plan requirement enforced by a server-side change budget (default 12 item
   changes, ≤ 3 symbol deletions per turn), policy configurable per project.
4. Plan approval via REST that enqueues a normal user message, so execution
   is an ordinary interruptible turn.
5. Checkpoint after every plan step (mid-turn save + snapshot).
6. CopperChat does its own annotation (headless KiCad does not).
7. Library search via KiCad's library API with an in-memory index in
   CopperChat; no library file parsing.
8. ERC delta at plan step end and editing-turn end on freshly saved files
   (no per-call ERC); mid-turn API saves are allowed.
9. New read-only `parts_get_notes` tool for the main agent.
10. `ParseAndCreateItemsFromString` banned as text generation.
