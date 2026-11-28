---
name: session-board
description: Use in a Board session: drives the step-by-step board flow S0–S9 from the schematic to the fab files, with a step report and the user's ok after every step. Load at the start of every board session and before the first board write.
sessions: board
---

# Board session

You lay out the board from the schematic, one step at a time, with the user. The
schematic is read-only here; you edit the board with the `pcb_*` tools. Load the
`board-layout` skill before you place or route: it has the part-class rules, the routing
rules and the owner's hard thresholds.

## Rules

- **Call `pcb_status` at the start of every turn.** It tells you whether a board exists,
  the outline, placed/unplaced parts, routed/unrouted connections, zones, the current
  `placement_hash` / `board_hash` and whether each checkpoint is approved or stale. Pick
  the step from it, not from memory.
- **Propose, then ask.** Look for the answer first in `requirements.md`,
  `architecture.md`, `parts.md`, `board.md` (`doc_read`) and the repository's mechanical
  files (`repo_list`, `repo_grep`, `repo_read`: case drawings, OpenSCAD files). Then
  propose a value with its reason and ask the user to confirm it ("I propose 4 layers
  because …; ok?") instead of asking an open question. Ask the questions of a step
  together, numbered, so the user can answer them in one message.
- **Step report.** Every step ends with: **at most 3 images** (`pcb_render`), a short
  check summary (what passed, what needs attention, with the metric ids), the decisions
  you took, and one question. Then stop the turn and wait for the user's ok or
  corrections. Never run two steps in one turn.
- **Two hard checkpoints, enforced by the server.** At S4 ask exactly **"Approve this
  placement?"**, at S8 exactly **"Approve this board for manufacturing?"**. Call
  `pcb_approve` only in a **later** turn — the one started by the user's approving
  message — with that stage's hash (the one you showed) and `user_quote` = the user's own
  words from that message (at least 2 words, copied verbatim). Never in the turn that
  showed the evidence, never while a plan runs, never with words the user did not write.
- **Changes after an approval need a new one.** Any part move, rotation, side change or
  lock change, and any outline or keepout change, makes the placement approval stale;
  routing tools then refuse `placement_not_approved`. Collect the moves you need, apply
  them in one go, show the result and ask "Approve this placement?" again. Any board change
  after the board approval (silk, copper, rules) makes it stale the same way.
- **Going back.** The user may correct anything at any point ("move the USB connector to
  the left edge"): apply it, re-run the checks of that step and show the result. Re-entering
  an earlier step makes the later checkpoints stale by their hashes; say which steps must
  be repeated.
- **Never edit the schematic.** If it must change (wrong or missing footprint, a missing
  pin, a part that should be excluded from the BOM, a mounting hole without its
  attribute), write what and why under `Open points` in `board.md`, ask the user to make
  the change in a Schematic session, then run `pcb_sync_from_schematic` again here.
- **Record as you go.** Answers and decisions go into `board.md` with `doc_patch_section`
  (`create: true` for a missing section; Board sessions cannot replace a whole document
  with doc_write), important decisions also into `decisions.md` with `doc_append`.
- **Never call the board done with open connections.** `pcb_status` must show 0 unrouted
  connections and the full check must have no hard failure before you ask for the board
  approval.
- **Tool errors** come as `{what, why, suggestion}` with a `code` (section "Errors"
  below). Follow the suggestion once; if the same call fails again, tell the user what
  failed and ask instead of looping.

### `board.md` v1 → v2

When `board.md` still has the v1 sections `## Stack-up` or `## Constraints`, move their
content once, with `doc_patch_section`, into the v2 sections: stack-up and net classes into
`## Stack-up and rules`, mechanical limits into `## Mechanical`, routing constraints into
`## Routing`; then leave each v1 section with one line "Moved to <section>." (no doc_write,
nothing lost).

### Tables the tools read (`board.md`)

- `## Summary`: a line `Assembly: <method>; sides: top` (or `sides: top+bottom`) — the sides
  SMD parts may use; hot-plate reflow means `top`. A line `Preset: jlcpcb-4l` (the preset id).
- `## Stack-up and rules`: `| Class | Width | Clearance | Via | Nets |`.
- `## Placement`: edge and fixed parts `| Ref | Edge/position | Reason |` (Edge/position:
  `left`, `left, 15 mm` = 15 mm along the left edge from its top end, `top, 20 mm` = 20 mm
  from the left end); blocks `| Block | Parts | Region | Notes |` (Region: `x, y, w, h` in
  mm, or `left` / `right` / `top` / `bottom` half, `top-left` … quarter).
- `## Part classes`: overrides only, `| Ref | Class | Reason |` (e.g. a cap that is not
  decoupling, a part `pcb_analyze` classified wrongly).
- `## Checks`: accepted warnings `| DRC type | Items/refs | Reason | Accepted by |`.

## S0 Intake

**Do:** `pcb_status`; `doc_list`, `doc_read` of `requirements.md`, `architecture.md`,
`parts.md`, `board.md`; search the repository for case / mechanical files. Check the
schematic: `run_erc` must be clean and every symbol must have a footprint
(`sch_list_symbols`); local footprints must be approved (`fp_list_local`, `fp_get`). If
not, stop here: list what is missing under `Open points` and send the user to a Schematic
session.

**Ask** the design questions below. Ask 1–6 now (with your proposals); 7 comes at S2 and
8 at S3. Skip a question whose answer is already in the docs — say where you found it.

1. **Board size and shape.** From a case or mechanical document when there is one (quote
   the file and the numbers). Otherwise propose a size from the parts: sum the courtyard
   areas (`pcb_sync_from_schematic {dry_run: true}` and `pcb_analyze` give the parts) and
   target a courtyard fill of at most **45 %**: `area = Σ courtyard / 0.45`, rounded up to
   whole mm, with the aspect ratio set by the connector edges. Above ≈ 35–40 % fill,
   placement still fits but routing gets tight; offer more area when the case allows it.
   Rectangle with optional corner radius.
2. **Layer count**, with a reason: e.g. 2 layers for a simple low-speed board, 4 layers when
   a module, a switching regulator and USB share a small board (solid ground reference,
   short return paths). Default proposal for 4 layers: Signal / GND / Signal + power /
   Signal (`jlcpcb-4l`, stackup `jlc-4l-1.6-7628`). Never decide it without the user.
3. **Fab and assembly.** Fab (JLCPCB is the preset), assembly method (hand, hot plate +
   stencil, JLC assembly) → allowed sides: hot-plate reflow → every SMD part on top. Write
   `Assembly: <method>; sides: top|top+bottom` into `board.md` → Summary.
4. **Edges.** Which connectors go to which edge and where along it, cable directions, parts
   that must be at a fixed position (display connector, buttons, LEDs seen through the
   case, the antenna end of a module).
5. **Mounting holes.** Count, size (e.g. M2 → 2.2 mm hole), position (corners, inset from
   the edges).
6. **Keepouts and height limits.** Case ribs, battery, antenna area, tall parts, areas
   without copper.
7. **Supply currents** per rail (at S2, if not in the docs): they set the net class widths.
8. **Special nets** (at S3, after `pcb_analyze` shows what it found): differential pairs,
   sensitive analogue nodes, RF, high-voltage nets.

**Show:** no image yet; the findings from the docs, the proposals, the questions. **Then
stop the turn and wait.** Record the answers in `board.md` (Summary, Mechanical,
Placement edges) in the next turn.

## S1 Board from schematic

**Do:** `pcb_sync_from_schematic` (first a `dry_run: true` when the board already has
parts, to show what would change). It creates the board if needed and puts new parts into a
staging area right of the outline. Explain every warning: a pad/pin mismatch names a part —
check it with `fp_check` and report the footprint problem under `Open points` (schematic
change). `schematic_unsaved`: the schematic has unsaved edits — ask the user to save or
finish the Schematic session.

**Show:** the sync report (parts added/changed/removed, nets, warnings), one
`pcb_render {view: "placement"}` if useful. Then stop the turn and wait.

## S2 Outline, holes, rules

**Do:**
1. `pcb_set_outline` with the agreed `width`, `height`, `corner_radius`, `holes`
   (positions or the corner default with `inset`) and `keepouts` (rule areas from
   question 6). Mounting holes are placed and locked; holes of the schematic are reused.
2. Ask question 7 (currents) if still open and propose net classes: one class per current
   range (outer-layer 1 oz rule of thumb ≈ 0.3 mm per ampere, at least the preset minimum),
   patterns as net-name globs (`*VBUS*`, `*+3V3`). Do not make a class wider than the
   current needs: a wide class has to neck down at fine-pitch pins and needs more room
   between parts.
3. `pcb_set_rules` **always with the agreed `layers` and the matching `preset`** (and the
   `stackup` preset when available): a preset alone never changes the layer count.
   `net_classes` from the table; `overrides` only to tighten. `stackup_unavailable`: the
   KiCad build has no stackup support — keep the layer count and preset, note it in
   `board.md`.

**Show:** `pcb_render {view: "top"}` (outline, holes, keepouts), the rules table and the net
classes, and write them to `board.md` → Stack-up and rules. Ask "Outline and rules ok?".
Then stop the turn and wait.

## S3 Blocks

**Do:** `pcb_analyze` (scope `all`). Check the part classes (a cap that is not decoupling,
a module without antenna, a part with odd keepouts far from its body) and fix them with
`| Ref | Class | Reason |` rows. Turn its block proposals into functional blocks: ESD and
filter parts join their connector's block; keep a regulator with its inductor and caps,
the MCU with its crystal and decoupling. Ask question 8 (special nets) with what
`pcb_analyze` detected (diff pairs, switch nodes, crystals). Then `pcb_define_blocks`,
the edge rows and optional block Regions in `board.md` → Placement, and
`pcb_floorplan {keep_channels}` as the preview: it reports the courtyard fill, the
regions and the channels between blocks, without writing.

`keep_channels` (routing gap around blocks and between clusters): 1.0–1.5 mm as the
default; 2–3 mm when the floorplan shows room to spare (courtyard fill well below 30 %);
0.5 mm only on a crowded board. Wider channels give the routers room and avoid detours.

**Show:** the block list (block → parts, edge or region), the floorplan render, the
courtyard fill, the special nets. Ask "Blocks and floorplan ok?". Then stop the turn and
wait.

## S4 Placement

**Do:**
1. Connectors and fixed parts first: an edge row in `board.md` puts the connector at its
   edge; a part that must sit at an exact spot: `pcb_move` with `locked: true`.
2. `pcb_place_blocks {keep_channels}`: places every block over the whole board in one step
   (edge blocks in their strip, the rest by connectivity, legalised, adjacent connected
   two-pad parts aligned for a straight link). Use `pcb_place_block` only to re-place a
   single block, then `pcb_legalize`.
3. `pcb_metrics {only: ["M1", "M8", "M9", "M10", "M13", "M16"]}` and the placement score
   (spread, congestion, gaps between blocks). Fix what you can with small `pcb_move`
   batches (a decoupling cap closer to its pin, an inductor turned so its pin faces the
   switch pin, a resistor aligned with its partner pad) until M1, M8, M9 and M10 pass, or
   show each remaining exception with its reason. M16 `alignable` pairs are placement
   hints (≤ 1 mm shifts).
4. `does_not_fit` (with `courtyard_fill`): never force an overlap. Ask: a larger board,
   the bottom side (only if the assembly allows it), smaller `keep_channels`, or fewer
   fixed positions.
5. Evidence: `pcb_render {view: "placement"}` and `pcb_check {level: "quick"}` with M1
   passing, both for the current `placement_hash`.

**Show:** the placement render (block hulls) + a zoom on the densest area if needed, the
quick-check summary (M1, M8, M9, M10, M13 values), what you changed and why. Ask
**"Approve this placement?"** and stop the turn. In the next turn, if the user approved:
`pcb_approve {stage: "placement", hash: <placement_hash you showed>, user_quote: "<their
words>"}`. If they asked for changes: apply them, show the new evidence and ask again.

## S5 Critical routing

**Do:** `pcb_analyze {scope: "nets"}` gives `critical_nets` with `critical_reasons`
(`class` = decoupling, switch node / hot loop, crystal, differential pair, short 2-pin nets;
`corridor` = nets with a straight corridor between adjacent pads; `intra_block` = nets that
stay inside one block). Confirm the set with the user in the S4 report or here (add the
special nets of question 8). Then `pcb_route {nets}` — at most 50 nets per call; corridor
nets in the first call, then the rest; never ground nets (planes connect them). Every
connection is DRC-checked and locked. Failed connections come back with a reason
(`no_path`, `too_large`, `timeout`, `drc_rejected`): look at the spot (`pcb_render` with a
`region` and `highlight_nets`) — usually a placement problem (a part in front of a pin, too
little room); propose the move, which needs a new placement approval.

**Show:** top and bottom renders with the critical nets highlighted, routed/failed counts,
`pcb_metrics {only: ["M4", "M5", "M7", "M8", "M12", "M15", "M16"]}`. Ask "Critical routing
ok?". Then stop the turn and wait.

## S6 Fill routing

**Do:** `pcb_route_fill` routes the remaining connections with Freerouting and makes the
result meet the bar (ground copper dropped, 90° corners chamfered, Y-junctions turned into
T-junctions, bad connections ripped and re-routed by the own router). "Nothing to fill"
means the critical routing already covered everything. When connections stay open or the
fill is refused, use the completion ladder, in this order:

- **(a)** more Freerouting effort: `passes` / `timeout_s` as the result's suggestion says
  (e.g. `timeout_s: 600` after a timeout).
- **(b)** free the critical signal nets next to the open connections:
  `pcb_unroute {nets: [those critical signal nets], unlock_only: true}`, then
  `pcb_route_fill {relock_nets: [the returned unlocked_nets]}`. Critical signal nets only —
  never ground or plane-net items.
- **(c)** `pcb_route` the rest with the own router: the nets the fill refused or left open
  (never ground). An open connection walled in by unlocked fill copper: clear a small
  area around it (`pcb_unroute {region: about 8 × 8 mm}`), route the open net first, then
  the nets you cleared.
- **(d)** report every open connection with its position (render with a region) and ask the
  user: a placement change (new placement approval), more layers, a larger board.

`fill_not_acceptable`: `rip_ratio` (more than 20 % of the fill would be ripped;
`worst_areas` shows where) → rung (c) for those nets, or more room in those areas before a
new fill; `timeout` → rung (a); `details.started: false` → another heavy board job was
running, retry the same call later; `freerouting_error` / `io_timeout` → retry once, then
report. **Never call the board done with open connections.**

**Show:** top and bottom renders, `pcb_status` connections (routed / unrouted), the fill
report (filled, ripped, relanded, re-routed), the metrics M2, M4, M5, M7, M12, M15, M16.
Ask "Routing ok?". Then stop the turn and wait.

## S7 Planes and pours

**Do:**
1. `pcb_zones {action: "add"}` without zones pours the default ground stack for the layer
   count (4 layers: solid GND plane on In1.Cu + GND pours on F.Cu and B.Cu; 2 layers: GND
   on B.Cu). Supply planes or extra pours only on request or with a reason. Zones refill
   after every change.
2. `pcb_fanout {net: "GND"}`: a short stub + via next to every ground SMD pad (never a via
   in a pad). Its report: `tied` (joined to a larger own-net pad), `via_pour` (connected by
   the pour on its layer — fine-pitch pads end here), `unconnected` (look at each one).
3. `pcb_stitch {net: "GND"}` (pitch 5 mm default; `edge_ring` on request): connects the
   pours on all layers. A ground island that holds only `via_pour` pads needs a stitch or
   fanout via inside it, else M2 reports it unconnected.

**Show:** the zones (layer, filled area), M2 and M11 (plane fill, signals crossing a void),
fanout/stitch counts and the pads that are not connected. Per-layer renders
(`pcb_render {view: "layer", layer: "In1.Cu"}`) only when the user asks or a problem needs
one. Ask "Planes ok?". Then stop the turn and wait.

## S8 Final check

**Do:**
1. `pcb_tidy_silk` (references next to their parts, fab-legal size). A hidden reference
   means no room next to the part; report it.
2. `pcb_check {level: "full"}`: KiCad's DRC, schematic parity, fab rules (M14) and every
   metric. Fix each hard failure with the step it belongs to (routing, placement, rules) and
   check again. Never weaken a global rule to pass a check.
3. A DRC **warning** is not a failure only when it is accepted: show it to the user with
   its reason and ask; add the `| DRC type | Items/refs | Reason | Accepted by |` row to
   `board.md` → Checks **only after the user accepted it** in a message. Footprint silk
   lines below the silk minimum are a warning only (no row needed).
4. **Footprint defects** (e.g. a footprint's own hole too close to its pads, a hole below
   the fab minimum, misplaced keepouts in a library footprint): not a layout problem.
   Show the markers (`pcb_render {markers: true, region}`), check the footprint with
   `fp_check`, and propose: fix the footprint (in a Schematic session, then
   `pcb_sync_from_schematic`) or — when the fab can make it — a custom rule scoped to that
   footprint with a reason (`pcb_set_rules {custom_rules}`), only with the user's ok.
5. Evidence: the full check without hard failures, `pcb_render {view: "top"}` and
   `{view: "bottom"}` for the current `board_hash`; a 3D view on request.

**Show:** top + bottom renders (+ one zoom or 3D if useful), the check summary (hard: all
pass; reviews with values; accepted warnings; custom rules), open points. Ask **"Approve
this board for manufacturing?"** and stop the turn. In the next turn, if approved:
`pcb_approve {stage: "board", hash: <board_hash you showed>, user_quote: "<their words>"}`.

## S9 Fab

**Do:** `pcb_fab_export {preset: "jlcpcb"}` → `fab/jlcpcb/` (gerbers incl. paste for the
stencil, drill, the upload zip, BOM, CPL, `fab-report.md`). Read the report: the BOM and CPL
must not contain mounting holes, fiducials or test points that are not assembled (excluded
parts are listed; a part that should be excluded needs its exclude attribute in the
schematic → Open point), holes below 0.3 mm (possible JLC surcharge), the order notes. The
CPL has no JLC rotation offsets and the BOM needs an `LCSC` (or `MPN`) field for JLC
assembly — say so when the user orders assembly. `pcb_export_3d {format: "step"}` for a
case check (bare board, no part models yet).

**Show:** the file list, the order notes (thickness, finish, layer count, stencil sides),
what to check before ordering. Offer a commit and call `git_commit` only when the user asks
for it in their message. Update `board.md` → Summary (status) and `decisions.md`.
Then stop the turn and wait.

## Errors

- `board_tools_disabled`: board editing is switched off on this server; tell the user.
- `no_board`: the project has no board yet → S1 `pcb_sync_from_schematic`.
- `schematic_unsaved`: the schematic has unsaved edits → ask the user to save it.
- `sync_failed`: the netlist import failed; report KiCad's message; after a real import
  failure, undo the turn from the History panel.
- `invalid_argument`: a wrong argument (the `why` says which); fix it once.
- `not_found`: unknown ref, net, layer, zone or block; use `details.closest`.
- `unknown_block`: the block is not defined → `pcb_define_blocks` (closest names given).
- `stackup_unavailable`: no stackup support in this KiCad; keep layers and preset.
- `does_not_fit`: no legal placement (`courtyard_fill`, `needed_mm2`, `available_mm2`); ask
  for more area, the bottom side or smaller channels — never overlap parts.
- `part_routed`: the part has tracks; move it with `unroute: true` (its connections are
  removed and must be routed again) or leave it.
- `placement_not_approved`: routing and copper tools need the current placement approved
  → S4 evidence, "Approve this placement?".
- `not_approved`: the fab export needs the current board approved (`details.reason`:
  `missing`, `stale`, `check_failed` = a hard failure in the full or export-time check,
  `changed_during_export`) → S8.
- `approval_not_allowed`: an approval guard failed (`details.guard`): evidence missing or for
  another hash, a board change after the evidence, no user message after it, the quote is
  not in the user's message, a plan running, or the same turn. Fix the cause; never
  invent a quote.
- `freerouting_unavailable`: the fill router is not installed; route with `pcb_route`.
- `fill_not_acceptable`: see S6 (`rip_ratio`, `timeout`, `freerouting_error`, `io_timeout`).
- `board_compute_timeout`: a board computation or the heavy-process gate did not finish in
  time (`details.waiting_for`); retry once, later.
- `refill_timeout`: KiCad was still filling zones; retry the call once.
- `drc_rejected` / `drc_timeout`: KiCad's DRC refused or did not finish; report it, retry
  once.
- `verification_failed`: the board read back differently than written; nothing was kept,
  report it.
- `document_inconsistent`: an edit could not be rolled back completely; stop editing and
  ask the user to undo the turn from the History panel.
- `output_path_unsafe` / `export_failed` / `render_failed`: the output folder is unsafe or
  the export/render failed; report it.
- `read_only`: the project is open in the KiCad GUI; ask the user to close it there.
- `save_failed`: the board could not be saved; report it.
- `plan_required` / `plan_step_required`: the edit needs a plan or a running plan step;
  follow the suggestion.
