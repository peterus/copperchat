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
| Atomicity | One tool call = one KiCad commit. On any failure inside the call the commit is dropped; the document is unchanged |
| Verification | Every mutating tool reads its result back from the API (items + netlist) and only then reports success; mismatch = structured error after dropping/reverting the commit |
| Tool level | High-level, intent-shaped tools (`sch_connect`, `sch_place_symbol` …) plus one low-level escape hatch (`sch_add_wire`). Coordinates in mm on the 1.27 mm (50 mil) grid, snapped by the tool |
| Connection geometry | Computed by CopperChat (pin positions from the API, orthogonal routing around symbol bounding boxes, collision check against foreign pin ends); the model never has to hit a pin by hand |
| Annotation | CopperChat assigns references itself (next free number per prefix, all sheets) and writes them through the API; it does not depend on headless auto-annotation (spike 2A) |
| Plan before edit | Server-enforced **change budget** per turn; beyond it mutating tools refuse until an approved plan exists (§7) |
| Checkpoints | Turn snapshot (Phase 7, unchanged) + one checkpoint per executed plan step (save + snapshot) |
| Diff | Semantic change list from API data recorded by the tools themselves (no file parser); `kicad-cli sch diff` for the visual before/after if spike 2C confirms it |
| Libraries | Search index built read-only from the library tables and library files (library data, not design data); pins/units always taken from KiCad via the API (spike 2B decides the exact source) |
| Part knowledge | New read-only tool `parts_get_notes(mpn)` exposes `docs/parts/<MPN>/notes.md` to the main agent without a research run |
| Scope of agent tools | Main agent still has **no** file/shell/web tools; all new tools are CopperChat MCP tools behind the existing guard + `PreToolUse` hook |

## 4. Architecture

No new services or containers. Changes by backend module (names from the
foundation, spec §4.1 and plan C7a):

| Module | Change |
|---|---|
| `kicad_session` | `KicadSession.commit(label)` async context manager around kipy `begin_commit` / `push_commit` / `drop_commit`; mid-turn `save(doc)` for plan-step checkpoints (spike 1C: `save()` rewrites files — hashes recorded after our own save, as today) |
| `kicad_tools` | New `edit/` package: `library.py` (search/inspect), `place.py`, `connect.py` (router), `fields.py`, `transform.py`, `delete.py`, `annotate.py`, `sheets.py`, `verify.py` (read-back + ERC delta), `geometry.py` (grid, bounding boxes, pin ends, collision). Plain functions, no Claude dependency, like the foundation tools |
| `tools` | New providers `copperchat.kicad_tools.edit.specs`, `copperchat.plans.specs`, `copperchat.parts.specs`; mutating specs set `mutating=True`, `changes_documents=True` (existing `ToolSpec` fields) |
| `plans` (new) | Plan model, storage, state machine, change budget, step execution bookkeeping (§7) |
| `changes` (new) | Per-turn change log (what each tool changed, with UUIDs and refs), change list for the UI, link to snapshots/checkpoints (§8) |
| `history` | Checkpoint snapshots for plan steps (same store, `kind = turn | step`), restore to a checkpoint |
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
2. Budget check (§7.1). If refused → structured error, nothing changed.
3. First mutating call of the turn → Phase 7 `on_first_mutation` snapshot
   (unchanged; a failed snapshot fails the call).
4. `async with session.commit(label)`:
   - resolve targets (refs → UUIDs + sheet path; pins → absolute positions),
   - compute geometry (snap, route, collision check),
   - `create_items` / `update_items` / `remove_items` /
     `place_symbol_from_library`,
   - **read back**: affected items by id, plus the netlist for any
     connectivity change (netlist export job, as `sch_get_net` does today),
   - compare with intent (e.g. "pin U1.3 and R5.1 are on the same net and
     no other net merged into it"); mismatch → raise → commit dropped.
5. Record a change entry (§8.1), report `changed_docs={"sch"}`.
6. Run **ERC on an in-memory copy** (save-copy procedure from spike 1C to a
   temp dir + `kicad-cli sch erc`) and return the **delta** against the
   turn-start ERC (new / resolved violations near the edited items). If ERC
   after every call proves too slow (spike 2A measures it), the delta is
   computed once per plan step / at turn end instead and the per-call result
   says so.
7. Tool result: structured JSON (what was created/changed with full UUIDs and
   refs, final positions in mm, nets touched, ERC delta), no image by default.

Turn end is unchanged: autosave, hash record, `publish_view`.

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
- KiCad crash mid-call: the commit is lost with the process; foundation
  recovery (restart + offer snapshot restore) applies; the plan (if any) goes
  to `paused` with the step marked `failed`.
- External file change between turns: foundation hash check + `revert()`;
  an active plan is paused and the user is told the plan's base changed.

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
(`POST /projects/{id}/plans/{planId}/approve|reject|resume|abort`), which
enqueues a normal user message ("Plan approved …") so execution happens in a
regular, interruptible agent turn.

## 7. Plan before edit

### 7.1 When a plan is required

The server enforces it; the model is told the rule in the editing skill but
does not have to judge it alone.

- **Change budget per turn** (setting, default **12 item changes** and
  **no deletion of more than 3 symbols**). Each mutating call counts the
  items it creates/changes/deletes. A call that would exceed the budget is
  refused with `plan_required` and the suggestion to call `plan_propose`.
- Plan policy `always`: every mutating call needs an active approved plan
  step. `never`: no budget (power users; still undoable).
- Calls made while an approved plan's step is active are counted against
  that step's `expected_changes` (+50 % tolerance) instead of the budget;
  exceeding it pauses the step with a structured error so the agent revises
  instead of improvising.

### 7.2 Plan life cycle

`proposed → approved → running → (paused ↔ running) → done | aborted`

- `proposed`: shown as a card; the user approves, rejects, or answers in the
  chat (the agent then calls `plan_revise`).
- `running`: steps execute in order. After each step: save, checkpoint
  snapshot, ERC delta, agent-view render of the affected region (the agent
  must look at it — skill rule), `plan_step_done`.
- **Stop** (foundation interrupt) during a step: the current tool call's
  commit is dropped or completes atomically; the plan goes to `paused` with
  the step `interrupted`. The user can **resume** (agent continues at that
  step), **roll back** to any finished step's checkpoint or to the pre-plan
  snapshot, or **abort** (keeps what is done, closes the plan).
- Errors the agent cannot fix within the step (verification failure twice,
  ERC regressions it cannot explain) → `paused` with the reason; push
  notification "needs attention" (foundation §9).
- Plans live in SQLite; a restart leaves a running plan `paused`.

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

- Every mutating result carries the ERC delta (§5.1).
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
- Library indexing reads library files only via the configured tables; paths
  resolved through KiCad's variables; no write access.
- Read-only projects (KiCad GUI lock) refuse every mutating tool before the
  snapshot (as Phase 7 does).

## 11. Error handling

| Situation | Behaviour |
|---|---|
| Verification mismatch after a write | commit dropped; structured error with what was expected vs. found; nothing changed |
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
  document unchanged (UUID set and netlist identical).
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
skipped. There is **no** library listing/search command and **no** annotate
command or `kicad-cli sch annotate`, and `GetBoundingBox` is only
registered for boards. `kicad-cli sch diff` exists (JSON, text, PNG, SVG).

| Spike | Questions | Gate |
|---|---|---|
| **2A — headless write round-trip** | place from stock + project library (refs, units, rotation, mirror); field update (value, footprint, custom field, DNP) incl. field positions; move/rotate with attached wires; delete; wire + label + junction + no-connect creation; does the netlist export job (and `GetSchematicNetlist`) reflect new connectivity without a frame (or is a save/reload/`revert` needed)? do wires that end on a pin connect, do T-joints need explicit junctions, are wires split? commit/drop semantics (does `drop_commit` really undo?); new sheet → file written on save, sheet pins ↔ hierarchical labels; saved files load in `kicad-cli` and KiCanvas; ERC-on-copy latency per call on a 100-symbol schematic; symbol bounding boxes from API data (body graphics + pins + transform) vs. KiCad's own plot | **go**: all core operations work via API (workarounds allowed if they stay API-only); **no-go** for any core operation needing a text edit → stop and escalate to the owner |
| **2B — library access** | how to search ~20 k stock symbols and footprints: read-only index of `.kicad_sym` / `.pretty` names, keywords, descriptions vs. any API route (e.g. scratch placement to read pins); index build time and memory; project-specific libraries; KiCad variables in table paths | **go**: search < 300 ms, pin data from KiCad itself |
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
7. Library search from a read-only index of library files (library data, not
   design data); pins always from KiCad.
8. ERC delta in every mutating tool result (falls back to per step if spike
   2A shows it is too slow).
9. New read-only `parts_get_notes` tool for the main agent.
10. `ParseAndCreateItemsFromString` banned as text generation.
