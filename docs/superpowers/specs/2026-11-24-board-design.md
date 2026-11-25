# CopperChat — Board layout (SP4 design)

Date: 2026-11-24
Status: **Approved by owner 2026-11-25** (including the spec calls). The owner decided the main points in chat on
2026-11-23 and 2026-11-24; §15.1 lists them. Every decision the author made alone is marked
**(spec call)** and is listed again in §15.2. The owner's answers to the draft's open questions
(2026-11-24) are in §15.3.

Inputs:

- the owner's decisions for SP4 (project memory `feature/sp4/brief`);
- spike 4A (`docs/spikes/4a-board.md`, prototypes in `spikes/4a-board/`);
- the foundation roadmap (`2026-10-15-foundation-design.md`, sub-projects 3 and 4);
- the sessions design (`2026-10-19-sessions-design.md`: the reserved `board` session type and
  `board.md`);
- the footprints design (`2026-11-17-footprints-design.md`: write path, approval guard, render
  pipeline), which this spec follows in structure and conventions;
- the OpenDriveHub requirements (`docs/design/requirements.md` in that repo), for the acceptance
  target only.

## 1. Purpose

### Why

CopperChat can build a schematic and create footprints, but the board is read-only. Today a user
has to leave the chat, open KiCad and lay out the PCB by hand. The foundation roadmap puts
"schematic → PCB" and "PCB layout" next, and the owner chose the board as the next sub-project
(2026-11-23).

The owner's goal is ambitious on purpose: **the in-chat agent takes a schematic to an orderable
PCB by itself.** The user steers only through chat prompts and never has to open the KiCad GUI.
The quality bar is strict: "order without rework". A clean DRC is necessary but not enough.

The owner's quality criteria, in their own words, made concrete:

1. all parts are placed;
2. no long traces;
3. straight segments, and direction changes **only by 45°** — no 90° corners, no acute angles
   (owner correction 2026-11-24);
4. clean, straight traces (no zig-zag jogs);
5. good, logical grouping of the parts by function.

The acceptance target is the **OpenDriveHub transmitter main board** (76 footprints, 4 sheets,
JLCPCB, self-assembly on a reflow hot plate with a stencil). After the release, the CopperChat
agent lays it out in PROD in a board session, the same way L201 was the acceptance case for the
footprint tools. The SP4 work itself builds the **functions** (tools, checks, chat flow, skills);
nobody lays out the OpenDriveHub board by hand or by script during SP4.

### What the agent can do after this

- create or update the board from the schematic (netlist import, re-sync that keeps placement);
- set the outline, mounting holes, keepouts, layer count, stackup, design rules and net classes
  for a fab preset (JLCPCB);
- group the parts into functional blocks and place them block by block, with helpers that
  cluster, legalise and score a placement;
- route critical and short nets with its own octilinear router, fill the rest with Freerouting,
  and clean up or reject what does not meet the quality bar;
- add ground planes, pours, fanout and stitching vias;
- check the board (DRC through the API, schematic parity, quality metrics) and loop until the
  checks pass, or ask the user when it is stuck;
- show the user an image and a short check report after each major step and wait for an ok;
- write the JLCPCB fab package (Gerbers incl. paste, drill, BOM, CPL, zip).

## 2. Scope

### Goals (v1)

1. A usable **Board session type** (reserved since SP3) with a step-by-step flow (§8) in which the
   agent asks the user the design questions (board size, layer count, connector edges, assembly
   method, …) and records the answers in `docs/design/board.md`.
2. Board write tools (§7) for every step from netlist import to fab export. All writes go through
   the KiCad IPC API.
3. **Generic** tools and skills: rules are phrased per part class ("decoupling cap next to the
   supply pin it serves", "switching regulator: hot loop compact"), never per part number. Nothing
   in code or skills is specific to OpenDriveHub.
4. Placement (owner approach A): the agent places **functional blocks** one by one; helpers
   cluster a block, legalise positions and score the result (§9).
5. Routing (approach A): **own octilinear router** for critical and short nets, locked afterwards;
   **Freerouting** fills the rest; its output is post-processed (90° corners chamfered to 2 × 45°)
   or the affected connections are rejected and re-routed (§10).
6. Planes, pours, fanout and stitching vias, each legalised by the API DRC task (§11).
7. Quality metrics with thresholds (§12) that turn the owner's criteria into numbers; hard gates
   block the fab export.
8. Images after each step (§13): board renders with a mm scale, per-layer views, a placement view
   with block outlines, violation markers; the live board view in KiCanvas during the turn (G8).
9. Two server-enforced checkpoints (§8.3): routing needs an approved placement, the fab export
   needs an approved final board. The other checkpoints are skill-enforced.
10. JLCPCB fab package (§14).
11. Undo covers every board change, including `.kicad_dru` (§6.4).

### Non-goals (v1)

- Editing the schematic from a board session. Schematic changes stay in schematic sessions (§6.5).
- KiCad's interactive router (walkaround, shove, length tuning). It is not reachable headless
  (G4) and needs an upstream feature request first.
- Length matching and impedance-controlled routing beyond "pair routed together, mismatch
  reported". USB 2.0 full speed and similar slow pairs are in scope; fast interfaces (USB HS, DDR,
  Ethernet PHY ↔ magnetics) are out of scope for v1 (spec call).
- Blind/buried/micro vias, HDI, flex. Through vias only (spec call).
- Panelisation, JLCPCB assembly rotation offsets in the CPL, LCSC part number lookup. The BOM/CPL
  files are written, but "JLC assembly" is not validated in v1 (spec call; the acceptance target
  is self-assembled).
- Mechanical import from DXF/STEP and an automatic case-fit check against a 3D model. The agent
  reads the outline numbers from the repo docs or asks the user. See G7 (§16) and owner decision D3 (§15.3).
- A full own autorouter (approach C). The design keeps the door open: the own router can take over
  more nets later.
- An "Approve" button in the chat UI. Approval stays a chat message, as for footprints (later
  option, together with the footprint button).

### Anti-goals (the feature fails if one of these happens, even if it "works")

- **Hand-written KiCad files.** CopperChat does not write `.kicad_pcb`, `.kicad_pro`,
  `.kicad_dru`, footprint or library files, and does not generate S-expressions or call
  `ParseAndCreateItemsFromString`. Every board change goes through the IPC API. `kicad-cli` is
  used only to export (netlist, DSN, SVG, 3D, fab files). The Specctra session (`.ses`) from
  Freerouting enters the board only through KiCad's `ImportSpecctraSession`. (The DSN/SES files
  are Freerouting interchange files, not KiCad documents; whether CopperChat may edit the
  exported DSN text: only as a last-resort fallback, owner decision D4.) File-system
  operations on KiCad data are limited to history snapshot and restore.
- **A silent design decision.** The agent picks a board size, layer count, connector edge,
  assembly side or fab without telling the user. It proposes, explains and asks (§8.2); the answer
  is recorded in `board.md`.
- **A 90° corner or an acute angle** on any routed track in a board the agent calls finished, or
  a fab package exported from such a board.
- **A board called "done" with open connections, DRC errors or DRC warnings the user has not accepted.**
- **Silent approval.** Routing starts on a placement the user never saw, or the fab package is
  written from a board the user never approved (§8.3).
- **Design-specific code.** A reference designator, MPN or OpenDriveHub net name in tools, metrics
  or skills.
- **Agent action on KiCad or Freerouting upstream.** No issues, MRs, comments or mailing-list
  posts. Upstream packages for patches are the owner's business (KiCad AI policy, §4).
- **A failed tool that leaves things behind.** A tool that reports an error leaves the board as it
  was (§6.3).
- **A change that cannot be undone** from the History panel.

## 3. Owner quality bar → measurable criteria

The owner's five criteria map to metrics in §12. A criterion is **hard** (blocks "done" and the
fab export) or **review** (reported, the agent must fix it or explain it to the user).

| Owner criterion | Metric(s) | Kind |
|---|---|---|
| all parts placed | M1 placement completeness | hard |
| no long traces | M5 detour ratio, M6 long connections | hard (outliers) + review |
| only 45° direction changes, no 90°, no acute | M4 angles | hard |
| straight, clean traces | M7 jogs and bend density | hard (micro-jogs) + review |
| logical grouping | M10 block compactness and intrusion | review + owner visual review |
| (implied by "order without rework") | M2 routing complete, M3 DRC/parity, M8 decoupling, M9 switcher loop, M11 planes, M12 widths, M14 fab rules | hard / review per metric |

## 4. Constraints

From spike 4A, the KiCad AI policy and the existing system:

| # | Constraint | Source |
|---|---|---|
| C1 | The board must be opened with `OpenDocument(DOCTYPE_PCB)` before any board command. The session already opens sch + pcb when the file exists (`kicad_session/session.py:_start`). A missing board is created with `CreateDocument(DOCTYPE_PCB)` + save, then opened. | spike Q1, `boards.py` |
| C2 | Netlist: the API schematic job returns a **stale** netlist headless (G2). Use the saved schematic + a fresh `kicad-cli sch export netlist --format kicadsexpr`, then `Board.import_netlist()` (`ImportNetlist`, same updater as "Update PCB from Schematic"). Re-import keeps placement; `delete_extra_footprints` removes deleted parts. | spike Q1 |
| C3 | Stackup cannot be set: `UpdateBoardStackup` has no handler (G1). Layer count, design rules (`SetBoardDesignRules`), net classes (`Project.set_net_classes`) and custom rules (`SetCustomDesignRules` → `.kicad_dru`) work. | spike Q2 |
| C4 | `Board.get_project()` sends the project path without the trailing `/` KiCad compares against (G3); append `/`. | spike Q2 |
| C5 | No routing command headless; `RunAction` is refused (G4). Tracks, arcs and vias are created as items. | spike Q4 |
| C6 | The DRC task (`StartDrcTask` → `GetTaskStatus` → `GetDrcTaskResult`) returns markers with item KIIDs in ≈ 0.2 s on a 76-part board. kipy has no wrapper (G5); raw protobuf works. | spike Q4 |
| C7 | Freerouting 2.5.0: the native CLI is broken; the **JAR on Java 25** works (G6). DSN via `kicad-cli pcb export specctra` or the API job `RunBoardJobExportSpecctraDsn` (sees the live board); SES via `ImportSpecctraSession`. `ignore_net_classes` is ineffective. Wider supply classes, filled planes or locked pre-routes in the DSN cost completion (6–11 unrouted). | spike Q4, Q5 |
| C8 | Zones: `create_items(Zone)` + `refill_zones` (≈ 0.7 s). Rule areas (`ZT_RULE_AREA`) for keepouts. | spike Q2, Q5 |
| C9 | Renders: `kicad-cli pcb export svg --mode-single --page-size-mode 2 --exclude-drawing-sheet` works on a **file**; the live board must first be saved to a copy (`SaveCopyOfDocument`). `kicad-cli pcb render` gives 3D PNGs headless. No 3D models in the image (G7). | spike Q7 |
| C10 | Memory: ≈ 1.9 GB RSS per api-server with a board loaded; Freerouting peaks at 0.4–0.6 GB heap. | spike Q1, Q4 |
| C11 | History snapshots cover `.kicad_pro`, `*.kicad_sch` and the project's `.kicad_pcb` (`history/store.document_files`), **not** `.kicad_dru` / `.kicad_prl`. | `history/store.py` |
| C12 | The schematic edit engine refuses `FootprintChangeSet` explicitly (`edit/engine.py`); the footprint feature runs its own engine from `spec.apply`, and `sdk_tools._apply` has only an `isinstance` branch that takes the library lock. `KicadSession.commit` and the modified-state tracking are schematic-only today (`DocKind = Literal["sch","pcb"]`, `commit` calls `h.schematic().begin_commit()`). | `kicad_tools/edit/engine.py`, `footprints/engine.py`, `kicad_session/session.py` |
| C13 | One KiCad session per CopperChat server; the per-project edit lock serialises writers. | `kicad_session/session.py` |
| C14 | **KiCad AI policy:** AI-assisted code is allowed with a human in the loop (trailer `Assisted-by: Claude`); every human-readable upstream text (commit message, MR/issue text, comments) is written by the owner; agents never act on KiCad's GitLab, mailing list or Discord; an MR needs an open issue assigned to its author; a feature needs a core-dev-approved feature request first. New KiCad patches are carried in `docker/kicad-base/patches/` with placeholder messages ("TODO-owner-write-message"), as patches 0001–0004 are. The same applies to the kipy fork. | coordinator brief |
| C15 | Freerouting is GPL-3; CopperChat is GPL-3. It runs as a separate, unmodified process. | `LICENSE`, spike |
| C16 | `Project` is a frozen dataclass whose `pcb_file` is set at discovery only when the file exists; the session opens only existing documents; history and `docs_of` skip a missing board; restore never deletes files missing from a manifest. | `projects.py`, `session.py`, `history/` |

## 5. Approaches considered

The owner chose approach **A** on 2026-11-24 after seeing the spike renders.

| Approach | Verdict |
|---|---|
| **A. Agent-led, hybrid.** The agent places per functional block with helper tools (cluster, legalise, score) and shows the placement before routing. Own octilinear router for critical/short nets (power, short 2-pin nets, USB pair, decoupling), locked. Freerouting fills the rest; its output is chamfered to 45° or rejected. Then GND planes, fanout, pours, stitching. A self-check loop (metrics + DRC) re-routes or asks the user. | **Chosen by the owner.** Uses each tool where the spike showed it is good: own router = exactly the wanted geometry on short nets (15/15 clean); Freerouting = completion on the bulk (100 % on the realistic board); the agent = judgement and user dialogue. Can migrate towards C later. |
| B. Freerouting for everything + clean-up. | **Rejected by the owner.** The spike's Freerouting board is legal but "ugly": signals on inner layers, power at minimum width, long buses, many jogs. A clean-up pass cannot fix layer and topology choices. |
| C. Own router only (full autorouter). | **Rejected for now by the owner.** Too large for v1. A grows towards C as the own router takes more nets. |
| D. Wait for KiCad's interactive router in the API. | Not available at our pin; no feature request exists; would need upstream work under the AI policy (C14). |
| E. A third-party autorouting service or ML placer. | Not considered: adds an external dependency and sends designs off-site. |

## 6. Architecture

### 6.1 Overview

```
chat (board session, skills: session-board, board-layout)
  │  pcb_* tools (two-phase writes, read tools)
  ▼
kicad_tools/board/            ← ToolSpecs, change sets, engine (API only)
  │ uses
  ▼
copperchat/board/             ← pure Python, no KiCad imports except the model loader
  model.py      BoardModel snapshot (parts, pads, nets, tracks, vias, zones, outline, rules)
  classify.py   part classes, net classes, functional blocks, critical nets
  placer/       cluster.py, layout.py, legalize.py, score.py
  router/       grid.py, astar.py, simplify.py, chamfer.py
  freerouting.py DSN/SES round trip, JRE subprocess
  planes.py     planes, pours, fanout, stitching candidates
  metrics.py    M1–M14, gate verdicts
  fab.py        JLCPCB package
  render.py     SVG wrapper (scale bar, block hulls, markers) → CairoSVG
  ▲
  │ raw protobuf helpers for gaps G3/G5
kicad_tools/board/api.py
```

- **Pure core (spec call).** Placement, routing geometry, chamfering and metrics work on a
  `BoardModel` (frozen dataclasses, mm floats) built from one API read. They are unit-testable
  without KiCad. Only `kicad_tools/board/` talks to KiCad.
- **`kicad_tools/board/api.py` (spec call):** raw protobuf wrappers for the DRC task,
  `GetTaskStatus`, Specctra export/import, `SaveDocumentToString`, `CreateDocument`, group naming
  and the project-path fix (G3, G5). No kipy fork change is needed; upstreaming wrappers to kipy
  is optional and owner business.
- The existing read tools (`pcb_list_footprints`, `pcb_get_footprint`, `pcb_get_net`, `run_drc`,
  `render_view`) stay unchanged.

### 6.2 Board change sets and the write path

Board writes follow the existing adapter (`agent/sdk_tools.py`): read-only check, edit lock,
gate (`before_prepare`), `prepare`, budget, turn snapshot, `apply`, `gate.record`, save.

- **`BoardChangeSet(ChangeSet)`** (spec call), like `FootprintChangeSet`: the schematic fields get
  defaults; it adds `ops` (typed board operations), `logical`, `pre_ids` (KIIDs present before the
  apply) and `kind` for the change log. Its engine `apply_board_change_set` runs from
  `spec.apply`, as the footprint engine does; `sdk_tools._apply` needs no new dispatch branch. The
  schematic engine refuses `BoardChangeSet` explicitly, like `FootprintChangeSet`.
- **Session generalisation (P1).** `KicadSession.commit` and the modified-state tracking become
  per `DocKind` (`sch`, `pcb`); today they are schematic-only (C12).
- **One commit per tool call where possible.** Item edits run inside
  `begin_commit` / `push_commit` (kipy) with a commit message `copperchat: <tool>`; on an error
  the commit is dropped (`drop_commit`). Tools that need the DRC task to see their own items
  (per-connection routing, fanout, stitching) are **multi-stage**: one pushed commit per stage
  plus compensation (§6.3). Whether the DRC task sees items of an open, unpushed commit is probed
  in P1; the design does not rely on it.
- **Read-back verification.** After the commit the engine re-reads the affected items and checks
  them against the intent (positions, nets, layers, widths). A mismatch raises and triggers
  compensation.
- **Results** set `changed_docs={"pcb"}` (`DocKind` has only `sch` and `pcb`), so autosave,
  history and the board view work as for schematics. Rule and net-class changes land in
  `.kicad_pro` / `.kicad_dru`; P2 verifies that a board save persists them and adds an explicit
  project save if it does not.
- **Change log** (spec call). Board entries use `sheet_path=""` (as footprint entries do) and
  kinds `board_<tool>` collected in a `BOARD_KINDS` set in backend (`changes/diff`) and frontend
  (`components/changes/highlight.ts`), so they are not treated as schematic items to highlight.
  One entry per tool call with counts, not one per track segment.
- **CPU-heavy work off the event loop** (spec call). Placer, router, post-processor and metrics
  run in a worker process (pool of 1 per server) with a wall-clock budget per call; KiCad API calls
  stay on the session's worker thread.

### 6.3 Multi-stage tools and compensation ("tool error = nothing written")

Some tools cannot be one commit: the Freerouting round trip (`ImportSpecctraSession`), DRC-in-the-
loop generators (fanout, stitching: add → refill → DRC → remove rejects), netlist import.

- **Compensation by item-ID diff (spec call).** Before the first stage the engine records the set
  of KIIDs of tracks, vias, zones and footprints plus the footprint positions. On failure it deletes
  every item that was not there before (API `remove_items`) and restores moved footprints. This is
  API-only and needs no file copy.
- **Netlist import** cannot be undone item by item reliably (it changes footprints, nets and
  values together). `pcb_sync_from_schematic` therefore always runs `dry_run` first, reports the
  changes, and applies only if the dry run had no errors. If the real import fails, the tool tells
  the user to undo the turn from the History panel (the turn snapshot exists, because the snapshot
  is taken before `apply`). This is the one documented exception (spec call).
- **`ImportSpecctraSession` side effects.** KiCad's Specctra import may delete existing unlocked
  tracks/vias and may re-apply component positions from the SES (to be verified at the pin in P6).
  Therefore `pcb_route_fill` snapshots the **full data** of all tracks, vias and footprint
  positions before the import (not only the KIIDs) and, on failure, deletes the new items and
  recreates the old ones through the API. A P6 test asserts that `placement_hash` is unchanged by
  the fill (otherwise positions are restored from the snapshot right after the import).
- **Cancellation** (turn stop): the JVM is killed, compensation runs shielded (as the existing
  `_uncancellable` paths do), then the cancellation is re-raised.
- If compensation itself fails, the tool raises `DocumentInconsistent` (existing error), as the
  schematic engine does.

### 6.4 Undo, history and snapshots

- `.kicad_pcb` and `.kicad_pro` are already covered (C11).
- **`.kicad_dru` is added to `document_files`** (spec call): custom rules (per-footprint DFM
  exceptions, §12 M14) live there and must be undoable. `.kicad_prl` stays out (view state only).
- **Files that did not exist** (spec call). A board created by `pcb_sync_from_schematic` and a
  first `.kicad_dru` must be undoable, but restore never deletes files today (C16). The manifest
  records `.kicad_pcb` and `.kicad_dru` as **absent** when they are missing at snapshot time;
  restoring such a snapshot deletes exactly those two files (only them, only inside the project
  directory). After creating a board the tool re-discovers the project and reopens the session
  (`sessions.reopen`, as restore does), so `Project.pcb_file` and the open documents are current.
  **Restore re-discovers too** (spec call): today `restore._reload` reopens with the pre-restore
  frozen `Project`; after the file operations it must re-discover the project before `reopen`,
  so a restore that deleted the board does not try to open a missing `.kicad_pcb`. Test:
  sync creates the board → undo → the session starts and `docs_of` is `{sch}`.
- Restore closes the session, copies files back and reopens it (existing behaviour); after a board
  restore the checkpoint hashes (§8.3) usually no longer match, and only the **latest** approval
  per stage counts (§8.3), so approvals must be given again. That is intended.
- Fab outputs (§14) are generated artifacts and are **not** snapshotted (spec call). They are
  regenerated on demand; the fab report records the board hash they came from.

### 6.5 Session type, profiles and docs

- **Board session enabled.** `POST /conversations type=board` no longer returns 400; the
  frontend's "Coming with board editing" option becomes active. The gate document stays
  `architecture.md` (sessions spec).
- **Profiles (`agent/profiles.py`):**
  - new group `PCB_READ` = new read-only board tools (§7) → `requirements`, `schematic`, `board`,
    `free` (spec call: read tools everywhere, like `FP_READ`);
  - new group `PCB_WRITES` = all writing `pcb_*` tools → `board` and `free` (spec call: Free gets
    them as it got `FP_WRITES`);
  - `board` also gets `FP_WRITES` (footprints spec §13.1: "board gets it with SP4") and
    `PLAN_TOOLS` (spec call: without them a board session under `plan_policy=always` could not
    write at all).
  - `SCH_WRITES` stay out of `board`: the schematic is read-only in board sessions (spec call).
    When the board needs a schematic change (wrong footprint assignment, missing pin), the agent
    says what and asks the user to make it in a schematic session (existing handover), then
    re-syncs.
- **System prompt.** The `board` paragraph in `SESSION_PARAGRAPHS` and `_EDITING` drop "the board
  is read-only" and point to the `session-board` skill.
- **`board.md` template v2** (§8.4) and **write policy** (spec call): board sessions may
  `doc_patch_section` any section of `board.md` (incl. `create=true` for a missing doc) and append
  to `board.md` and `decisions.md`; no `doc_write` (full replace). `doc_patch_section` is added to
  the board profile. Today board sessions may only whole-doc append. Existing v1 `board.md` files
  (sections "Stack-up", "Constraints") are migrated by the skill through patches.
- **Feature flag** `COPPERCHAT_BOARD_TOOLS` like `COPPERCHAT_FOOTPRINT_TOOLS` (spec call):
  **default off during P1–P8**, switched to default on in P9. Off removes `PCB_WRITES` from every
  profile including Free. The filter uses the **explicit `PCB_WRITES` name set**, not a `pcb_`
  prefix (the prefix is shared with the read tools, old and new); `PCB_READ` and the existing
  read tools stay available with the flag off. It also hides the Board session type (`POST type=board` stays 400, the dialog option disabled).
  `COPPERCHAT_FREEROUTING` (default on when the JRE is present) disables only `pcb_route_fill`.

### 6.6 Live board view (G8)

`LiveViewPublisher` (`history/live.py`) is generalised from `sch` to `sch|pcb` (spec call: in
scope). Three backend paths need a `pcb` branch: `hooks.on_documents_changed` (schedules only for
`sch` today), `LiveViewPublisher._publish` (requires and publishes `sch` only) and the turn-end
catch-up (`publish_disk_view` / `copy_disk_schematic`). After a board write the publisher
debounces 2 s, writes a board snapshot with `SaveCopyOfDocument` (existing `save_copy` path) and
sends a `ViewUpdatedFrame{docs:["pcb"], live:true}`. The frontend already accepts live `pcb`
frames (`stores/view.ts`). Board publishes are triggered **by the adapter after `apply`
returns** (one per tool call), not by the per-commit document hook, so multi-stage tools
(`pcb_route`, `pcb_route_fill`, `pcb_fanout`, `pcb_stitch`) never show half states; the hook
ignores `pcb` changes while a board tool is applying (spec call).

## 7. Tools

Conventions as in the footprints spec §7: one `ToolSpec` provider
`copperchat.kicad_tools.board.specs`; `_obj` schemas with `additionalProperties: false` and
explicit limits; `StructuredToolError` (`what` / `why` / `suggestion` / `code` / `details`);
JSON `data` plus `images`; writing tools are two-phase (`edit_spec`). Units: **mm, degrees,
board coordinates (KiCad: x right, y down)**. References are footprint references (`U301`),
`maxLength 32`; net names `maxLength 256`.

Every write tool returns a short **step report**: what changed, counts, the relevant metric deltas
and the next suggested step. Tool contracts below are the v1 surface (spec call for names and
grouping; the plan may refine fields, not semantics).

### 7.1 Read and analysis tools (`PCB_READ`)

| Tool | Input | Output |
|---|---|---|
| `pcb_status` | `{}` | board exists?, outline, layer count, footprint count, placed/unplaced (unplaced = courtyard not fully inside the outline, §7.2), routed/unrouted connections, zone count, checkpoint states (§8.3) with stale flags, the current `placement_hash` and `board_hash`, last check summary. Cheap; the agent calls it at the start of every turn. |
| `pcb_analyze` | `{scope?: "parts"\|"nets"\|"blocks"\|"all"}` | the classification of §9.1: part classes, net classes (ground, supply, switch node, diff pair, high-current guess, critical), proposed functional blocks (from sheets + clusters) and any overrides from `board.md`. Read-only proposal; nothing is written. |
| `pcb_render` | `{view: "top"\|"bottom"\|"layer"\|"placement"\|"3d", layer?, region?: {x,y,w,h}, highlight_nets?: [..≤20], markers?: bool, angle?: "top"\|"bottom"\|"iso"}` | PNG image(s) + the SVG link (§13), plus the `placement_hash` and `board_hash` of the rendered state (recorded as render evidence, §8.3). |
| `pcb_metrics` | `{only?: [metric ids]}` | M1–M14 values, thresholds, verdict per metric (pass / review / fail), worst offenders with refs/nets/positions. ≈ 0.1–0.5 s. |
| `pcb_check` | `{level: "quick"\|"full"}` | quick = API DRC task + metrics; full = + `kicad-cli pcb drc --schematic-parity` + fab-rule checks (§12 M14). Result: errors, warnings (each marked `accepted` when it matches a row of `board.md` → Checks, §12), metrics, overall verdict, and both `placement_hash` and `board_hash`. Records the result for the checkpoint guard (§8.3). |

### 7.2 Write tools (`PCB_WRITES`)

**Board from schematic**

- `pcb_sync_from_schematic {delete_extra?: bool=true, dry_run?: bool=false}`
  - Creates the board if the project has none (C1).
  - Refuses (`schematic_unsaved`) if the schematic has unsaved changes (it is read-only in board
    sessions, so this only happens with a parallel schematic session).
  - Exports the netlist with `kicad-cli` (C2), runs `ImportNetlist` as dry run, then for real.
  - Report: added / removed / changed footprints, nets, warnings (e.g. "no net for pad 10 of J402:
    the symbol has no such pin") with the suggestion to check it with `fp_check`.
  - New footprints are moved into a **staging area** (spec call), so "unplaced" is unambiguous:
    a grid to the right of the outline, or — before an outline exists (S1 runs before S2) — to the
    right of the bounding box of the already placed footprints, or of `(0,0)` on an empty board.
    No group or stored box is needed (spec call): a footprint counts as **unplaced** when its
    courtyard is not fully inside the outline; before an outline exists every footprint is
    unplaced and M1 is not evaluated. (KiCad items belong to at most one group, so groups are
    reserved for functional blocks `blk:<name>`.)
  - Cost 3.

**Setup**

- `pcb_set_outline {shape: "rect", width, height, corner_radius?, origin?: {x,y}, holes?:
  {footprint: lib_id, inset, positions?: [{x,y}]}, keepouts?: [{name, polygon|rect, layers,
  no: ["tracks","vias","copper","footprints"]}]}` — replaces the outline (one `BoardRectangle` on
  Edge.Cuts) and the holes the tool placed before (recognised as `MountingHole*` footprints
  **without a schematic link** (`symbol_path` empty), spec call; they never join a block group). v1 shapes: rectangle with rounded corners
  (spec call; polygon outlines later). Mounting holes from the library via
  `place_footprint_from_library`, default `MountingHole:MountingHole_2.2mm_M2` only if the user
  chose M2; schematic mounting holes are moved instead of duplicated. Cost 2.
- `pcb_set_rules {layers?: 2|4|6, preset?: "jlcpcb-2l"|"jlcpcb-4l"|"jlcpcb-6l", overrides?:
  {...min values}, net_classes?: [{name, track_width, clearance, via_diameter, via_drill,
  diff_pair_width?, diff_pair_gap?, patterns: [net name globs]}], custom_rules?: [{name,
  condition, constraint, min?, max?, reason}], stackup?: "<preset id>"}` — layer count
  (`set_enabled_layers`), board design rules, net classes, custom rules (structured
  `SetCustomDesignRules`), and stackup via patch 0005 (G1, §16). Stackup presets v1 (spec call,
  values from JLC's published stackups, verified in P2b): `jlc-2l-1.6`, `jlc-4l-1.6-7628`
  (JLC04161H-7628), `jlc-6l-1.6`. Every custom rule carries a
  `reason`, written as the rule comment. Cost 2.

**Placement** (§9)

- `pcb_define_blocks {blocks: [{name, refs: [..≤200], anchor?: ref}], replace?: bool}` — stores
  functional blocks as KiCad **groups** named `blk:<name>` (spec call: persistent in the board,
  visible in KiCad, undoable). Cost 1.
- `pcb_place_block {block, region: {x,y,w,h} | {edge: "top"|"bottom"|"left"|"right", offset?},
  side?: "top"|"bottom", rotation?: 0|90|180|270, keep_channels?: mm, fixed?: [{ref, x, y, rot,
  side}]}` — clusters the block around its anchor (§9.2), packs it into the region, legalises
  against everything already placed, applies it in one commit. Fails with `does_not_fit` and the
  needed area instead of placing partially. Cost 1.
- `pcb_move {items: [{ref, x?, y?, rot?, side?, locked?}] ≤200, legalize?: bool}` — exact moves by
  the agent (e.g. a connector on the user's edge, a decoupling cap tightened). Cost
  `ceil(n/10)`.
- `pcb_legalize {scope: "all"|"block", block?, max_shift?: mm}` — removes courtyard overlaps and
  outline/keepout violations with minimal displacement; locked parts never move. Cost 1.
- **Moves on a routed board** (`pcb_move`, `pcb_legalize`, `pcb_place_block`; spec call): a part
  with tracks or vias attached is refused (`part_routed`) unless `unroute: true` is passed; then
  the tool first removes **all** copper of the affected connections, **locked items included**
  (critical routes, fanout and stitching vias on the moved part's pads; i.e. `pcb_unroute` with
  `include_locked: true` scoped to those connections), and reports them as unrouted, so locked
  routes must be re-done. A test covers a move with a locked route and a fanout via. No dangling tracks are left.

**Routing** (§10)

- `pcb_route {nets: [..≤50], layers?: [..], width_from?: "netclass", lock?: bool=true,
  allow_vias?: bool=true}` — own octilinear router, per connection; each result checked by the DRC
  task before it is kept. Report per net: routed/failed with the reason. Cost `ceil(n/5)`.
- `pcb_route_fill {timeout_s?: ≤600, passes?: ≤100, relock_nets?: [..≤50]}` — Freerouting for all unrouted
  connections, then the post-processor (§10.3). Report: completion, rejected connections, metrics.
  Cost 5.
- `pcb_unroute {nets?: [..], region?: {x,y,w,h}, include_locked?: bool=false, unlock_only?: bool=false}` — deletes tracks
  and vias (with `unlock_only`: only clears their `locked` flag). Cost 1.

**Copper**

- `pcb_zones {action: "add"|"update"|"delete", zones: [{name, net, layers, outline: "board"|polygon,
  priority?, clearance?, thermal?: {gap, spoke_width}}]}` — then refill. Cost 1 per zone.
- `pcb_fanout {net, via?: {diameter, drill}, max_stub?: mm}` — SMD pads of a plane net get a
  short 45° stub + via, legalised by DRC (§11). Cost 2.
- `pcb_stitch {net, pitch?: mm, edge_ring?: bool}` — stitching vias, legalised by DRC, dangling
  vias removed. Cost 2.
- `pcb_tidy_silk {}` — reference designators placed next to their part, outside pads and other
  silk, uniform size (≥ 0.8 mm height, 0.15 mm line), on the part's side; overlaps that cannot be
  resolved are hidden only with a report line. Cost 1.

**Checkpoints and output**

- `pcb_approve {stage: "placement"|"board", hash, user_quote}` — records an approval (§8.3); `hash` is the stage's hash (`placement_hash` for `placement`, `board_hash` for `board`); a placement approval with a `board_hash` is refused (guard test).
  Cost 1.
- `pcb_fab_export {preset: "jlcpcb"}` — §14. Refused (`not_approved`) unless the `board`
  checkpoint is valid. Cost 0 (writes no KiCad data).
- `pcb_export_3d {format: "step"|"glb"}` — exports the 3D board to `<project>/fab/3d/`
  (without part models until G7 is solved, the result says so). Same path handling as
  `pcb_fab_export` (one shared helper): `fab/` and `fab/3d/` resolved, refused if a symlink or
  outside the project, written to a temp file and renamed; tested with a symlinked `fab/`. Cost 0.

`fab/` is a normal repo directory: not gitignored, committed when the user accepts the git commit
offered at S9 (spec call).

### 7.3 Budget and plan gate

Board tools count against the per-turn change budget (`EditingSettings.change_budget`, default 20)
with the costs given above (spec call). A whole-board placement of 8 blocks costs 8, a fill route
5, so one major step fits into one turn without a plan. Plans stay possible (`plan_propose`). `pcb_approve` is added to
`PLAN_EXEMPT_TOOLS` (like `fp_approve`) and is never allowed while a plan is running; otherwise
it could never run under `plan_policy=always`. All other board write tools follow the plan gate.

## 8. The step-by-step chat flow

### 8.1 Steps

The `session-board` skill drives the flow. Each step ends with a **step report**: images (§13), a
short check summary (what passed, what needs attention), the decisions taken, and a question.
The agent then **stops the turn and waits** for the user's ok or corrections (owner decision).

| Step | What the agent does | Ends with |
|---|---|---|
| S0 Intake | reads `requirements.md`, `architecture.md`, `parts.md`, `board.md`; checks the schematic is ERC-clean and every symbol has a footprint (local footprints approved); asks the first design questions (§8.2) | questions; answers recorded in `board.md` |
| S1 Board from schematic | `pcb_sync_from_schematic`; explains warnings (pin/pad mismatches) | sync report |
| S2 Outline, holes, rules | `pcb_set_outline`, `pcb_set_rules` (layers, preset, stackup, net classes from the answers) | outline render + rules table; ok? |
| S3 Blocks | `pcb_analyze`, proposes functional blocks and their rough positions (a floorplan sketch: block rectangles on the outline) | block list + floorplan render; ok? |
| S4 Placement | `pcb_define_blocks`, connectors/fixed parts first, then blocks with `pcb_place_block`, tightening with `pcb_move`, `pcb_legalize`; `pcb_metrics` (placement part) until M1/M8/M9/M10 pass | placement render with block hulls + metrics; **"Approve this placement?"** (hard checkpoint) |
| S5 Critical routing | `pcb_route` for the critical set (§10.1), locked | top/bottom render with these nets highlighted; ok? |
| S6 Fill routing | `pcb_route_fill`; rejected connections re-routed with `pcb_route`; when stuck: small placement changes (§8.3) or questions (more layers, larger board) | routed renders + metrics; ok? |
| S7 Planes and pours | `pcb_zones` (GND planes, outer pours), `pcb_fanout`, `pcb_stitch`, refill | per-layer renders; ok? |
| S8 Final check | `pcb_tidy_silk`, `pcb_check full`, fix loop, 3D render | full report + all views; **"Approve this board for manufacturing?"** (hard checkpoint) |
| S9 Fab | `pcb_fab_export jlcpcb`; offers a git commit with the git tools | file list + order notes |

The user can jump in at any point with a correction ("move the USB connector to the left edge");
the agent applies it, re-runs the affected checks and shows the result. Re-entering an earlier
step invalidates later checkpoints automatically (hashes, §8.3).

### 8.2 Design questions the agent asks

The skill lists the questions and when to ask them. The agent first looks for the answer in the
repo docs (requirements, architecture, mechanical files), proposes a value with a reason, and asks
for confirmation instead of asking open questions (spec call). Asked at S0 unless noted:

1. Board size and shape: from a case/mechanical doc if present, else the agent proposes a size
   from the summed courtyard area (target fill ≤ 45 %, spec call) and asks.
2. Layer count: the agent's proposal with reason (e.g. 4 layers for a module + switcher + USB on a
   small board: solid GND reference, short returns); **not hard-coded**.
3. Fab and assembly: fab (default from requirements; JLCPCB preset in v1), assembly method (hand,
   hot plate + stencil, JLC assembly) → allowed sides for SMD parts. Hot plate → all SMD on top.
4. Edges: which connectors go to which edge, cable directions, parts that must be at a position
   (display connector, buttons, LEDs visible through the case, antenna end).
5. Mounting holes: size and position (corners, inset).
6. Keepouts and height limits (case ribs, battery, antenna area).
7. Currents per supply rail if not in the docs (for net class widths) — asked at S2.
8. Special nets: differential pairs, sensitive analogue nodes, RF — asked at S3 after
   `pcb_analyze` shows what it detected.

### 8.3 Checkpoints (spec call: two hard, the rest soft)

Owner: "after each major step show images + a short check report and wait for ok/corrections,
at least after placement, before routing". The server enforces the two checkpoints whose skipping
would be expensive; the others are enforced by the skill (the agent stops and asks).

- **Placement checkpoint.** `pcb_route`, `pcb_route_fill`, `pcb_zones`, `pcb_fanout` and
  `pcb_stitch` are refused (`placement_not_approved`) unless a `placement` approval exists whose
  `placement_hash` equals the current one. `placement_hash` = hash of (ref, x, y, rotation, side,
  locked) of all footprints, rounded to 0.01 mm / 0.1°, **plus the outline and the rule areas
  (keepouts)**, so `pcb_set_outline` after approval also invalidates it (spec call).
- **Board checkpoint.** `pcb_fab_export` is refused unless a `board` approval exists whose
  `board_hash` equals the current one and the last `pcb_check full` for that hash had no hard
  failure. `board_hash` covers footprints (incl. their texts and silk), tracks, vias, zone
  outlines and settings, board graphics, the design rules, net classes and the `.kicad_dru` text,
  so e.g. `pcb_tidy_silk` after approval invalidates it.
- **Only the latest approval per (project, board file, stage) counts** (spec call); an undo back
  to an earlier state does not revive an older approval.
- **Evidence for an approval** (spec call): `placement` needs a `pcb_render view=placement` and
  a `pcb_check quick` with M1 passing for that `placement_hash`; `board` needs `pcb_check full`
  without hard failures plus `pcb_render` top and bottom for that `board_hash`.
- **Approval guards** — the same guards as `fp_approve` (footprints spec §7.7, guards 3–6): the
  stage's render and check were produced in this conversation for this hash, a **human** user
  message came after them, `user_quote` is a substring of it, no plan is running and the turn was
  started by that message.
- **Small changes after placement approval** (spec call): moves made by the router stage are not
  allowed; `pcb_move` after approval invalidates the approval. The skill tells the agent to batch
  needed moves, show them, and get a new ok. (A tolerance rule — "up to 3 parts by < 1 mm keep the
  approval" — was considered and rejected: it would make the hash check fuzzy.)
- **Storage:** new table `board_checkpoints(project_id, board_file, stage, hash, conversation_id,
  run_id, user_quote, approved_at)` plus `board_reviews(project_id, board_file, conversation_id, stage,
  placement_hash, board_hash, run_id, kind, view, check_level, hard_failures, m1_pass,
  created_at)` (indexes on (project_id, board_file, placement_hash) and (project_id, board_file,
  board_hash); both hashes of the state the evidence was produced for — the placement guard
  matches `placement_hash`, the board guard `board_hash`) in a new migration (`0013_board.sql`): one row per piece of
  evidence, `kind` ∈ {`render`, `check`}; `view` is set for renders (`placement`, `top`,
  `bottom`, …), `check_level` (`quick`/`full`), `hard_failures` (count) and `m1_pass` for checks. "The last `pcb_check full` for
  that hash" = the newest `check` row with level `full` for (project, board file, board_hash). Approvals are per project and board file,
  so they survive a new session on the same board (spec call); hashes make them expire
  automatically.

### 8.4 `board.md` (template v2)

The board session's design document. The agent records answers and decisions here as it goes;
`pcb_analyze` and the metrics read the machine-readable tables (spec call: same table parsing
style as `parts.md` Key parts).

```markdown
# Board

## Summary
<!-- Fab, layer count, size, assembly method, status. -->

## Mechanical
<!-- Outline, holes, keepouts, height limits, source (case file / user statement). -->

## Stack-up and rules
<!-- Preset, stackup, net classes table: | Class | Width | Clearance | Via | Nets | -->

## Placement
<!-- Edge and fixed-part table: | Ref | Edge/position | Reason | -->
<!-- Blocks table: | Block | Parts | Region | Notes | -->

## Part classes
<!-- Overrides only: | Ref | Class | Reason |  (e.g. a cap that is not decoupling) -->

## Routing
<!-- Critical nets, pairs, special handling. -->

## Checks
<!-- Last check summary, accepted warnings with reasons. -->

## Open points
```

## 9. Placement (approach A)

### 9.1 Classification (`board/classify.py`, generic)

Inputs: footprints (pad count, pad nets, courtyard, reference prefix, footprint library name,
`symbol_sheet_name`), net names, net topology. No MPN or design knowledge.

| Class | Rule (spec call; overridable in `board.md` → Part classes) |
|---|---|
| ground net | name matches `GND`, `AGND`, `DGND`, `PGND`, `VSS`, `*GND*`, or the net with the most pads among nets named like ground |
| supply net | name matches a supply pattern (`VCC`, `VDD*`, `VBUS`, `VBAT`, `VSYS`, `+?\d+V\d*`, `3V3`, …) or has ≥ 4 pads on ≥ 3 parts and at least one 2-pad cap to ground |
| anchor | ≥ 5 pads (IC, module, connector) |
| connector | reference prefix `J`/`P`/`CN`/`USB` or footprint library `Connector*` |
| decoupling cap | 2-pad C between a supply net and a ground net; served pin = nearest supply pin of an anchor on the same net |
| switching regulator | an anchor pin net that connects only that IC and one inductor (switch node); its input cap(s) = caps between the IC's input supply and ground; its output cap(s) = caps on the inductor's other net |
| crystal / oscillator | reference `Y`/`X` or footprint library `Crystal*`/`Oscillator*`; its load caps = caps between a crystal net and ground |
| differential pair | net names with suffix pairs `P/N`, `+/-`, `_DP/_DM`, `D+/D-` on the same anchor pins |
| module with antenna | footprint with an embedded keepout/rule area, or user statement (agent asks) |
| mounting hole, fiducial, test point | library name / reference prefix |
| other | everything else |

**Functional blocks** (proposal): one cluster per anchor with its satellites (spike `place.py`
idea: small parts sharing its signal nets, its decoupling caps, its switcher passives), grouped by
schematic sheet; leftovers join the anchor they share the most nets with. The agent edits the
proposal and confirms it with the user (S3).

### 9.2 Placement helpers (`board/placer/`)

From the spike prototype (`place.py`: legal by construction, 76 parts in 3 s), extended:

- **Cluster layout** (`layout.py`): anchor at the origin; satellites on the side of the anchor pad
  they connect to, rotated so the connected pad faces the anchor; decoupling caps placed by the
  part-class rules (§9.3) first, at the minimum courtyard gap.
- **Block packing** (`pcb_place_block`): candidate positions on a 0.5 mm grid × 4 rotations inside
  the region; cost = airwire to placed parts + pull to the region centre + connector-to-edge
  term; `keep_channels` reserves a routing gap around the block (default 0.5 mm, spec call).
- **Legaliser** (`legalize.py`): pushes overlapping courtyards apart and clamps into the outline
  minus edge margin and keepouts; locked parts are fixed; snaps to 0.05 mm.
- **Score** (`score.py`): the placement metrics M1, M8, M9, M10 plus total airwire (octilinear
  MST per net) and a routability estimate (pin density per 5 × 5 mm cell, max ≤ a threshold, spec
  call; tuned in the plan).
- **Side policy:** SMD on top only unless the user allowed bottom (assembly answer, §8.2).
- **Does not fit:** the tool reports the missing area; the agent asks the user (bigger board,
  bottom side, smaller parts) instead of overlapping or leaving parts outside (spike: BT401 case).

### 9.3 Part-class rules (generic, in the `board-layout` skill and partly in metrics)

| Part class | Rule | Checked by |
|---|---|---|
| decoupling cap | on the same side as its IC, pad-to-served-pin path ≤ 3 mm (review > 3 mm, fail > 5 mm); the smallest value closest; its ground pad gets a via next to the pad | M8 |
| switching regulator | input cap, IC, and (buck) the catch path form a minimal hot loop: input cap ≤ 3 mm from VIN/GND pins, inductor ≤ 3 mm from the SW pin, switch-node copper ≤ 5 mm and not under sensitive parts; feedback divider next to the FB pin, away from the inductor | M9 |
| crystal | ≤ 5 mm from the MCU pins, load caps next to it, no other signals between, GND pour under it on the adjacent layer | M8-style distance check (spec call) |
| connector | on its assigned edge (body ≤ 0.5 mm from or overhanging the outline as the footprint requires), its ESD/filter parts between connector and the rest | M13 |
| differential pair | routed together, same layer, constant gap, length mismatch reported | M12 |
| module with antenna | antenna end at the board edge or beyond, keepout on all layers under it | M13 |
| power path | high-current nets on outer layers with class widths, short and direct; no vias in series unless doubled | M12 |
| mounting hole | inside its corner inset, keepout ring for copper | DRC |

The skill states these as rules per class; the metrics implement the checkable part (spec call:
the table above is the v1 set).

## 10. Routing (approach A)

### 10.1 Critical set (own router first)

`pcb_analyze` proposes the critical set; the agent confirms it with the user at S5. Default
(spec call): decoupling connections, switch-node and hot-loop nets, crystal nets, differential
pairs, and every **2-pin net with an airwire ≤ 10 mm**. Ground connections are not routed as
tracks except short stubs (planes, §11).

### 10.2 Own octilinear router (`board/router/`)

From the spike prototype (`router.py`: 15/15 short nets clean), extended:

- Grid A* on a 0.05 mm grid (configurable), state = (cell, layer, direction).
- **Allowed direction changes: 0° and 45° only** (owner rule). A 90° turn is made of two 45° turns
  with a straight piece of at least `max(1.0 × width, 0.25 mm)` between them (spec call); 90° and
  135° turns at one vertex are forbidden in the search. Two **opposite-sense** 45° bends (an
  S-offset) need a straight piece of at least `2 × width` between them, so router output never
  contains an M7 micro-jog (spec call); a property test asserts M4 = M7 = 0 on router output.
- **Layer changes by vias** (spec call: v1 supports vias, not only single-layer routes as in the
  spike) with a via cost; preferred layer per net class (power on outer layers).
- **Pad entry/exit:** first segment leaves along the pad's long axis or at 45°, never from a pad
  corner at a shallow angle.
- Obstacles: copper of other nets inflated by clearance + half width, keepouts, board edge
  clearance; widths and clearances from the net class.
- Post-processing: merge collinear segments. The final approach to a pad is computed from the
  real pad centre (off-grid), not from the grid cell, so the last segment stays exactly
  octilinear (M4); if that is impossible, a short octilinear stub from the pad centre to the grid
  is added.
- **Verification:** every routed connection is committed and checked by the DRC task; a rejected
  connection is removed and retried with a higher clearance margin, then reported as failed.
- **Locking:** routed critical connections are set `locked` (KiCad item flag).
- Differential pairs: routed as a coupled pair (two paths at constant offset) on one layer;
  length mismatch reported (M12). Spec call: v1 coupled routing for straight-ish pairs only;
  when coupling fails, route both nets with a gap cost and report.

### 10.3 Freerouting fill and post-processing

- **Runtime (G6):** Freerouting **2.5.0 JAR** + **Java 25 JRE** in the **app image** (not
  kicad-base, spec call: no KiCad rebuild needed), pinned by version + SHA-256, run with
  `--gui.enabled=false`, `-Xmx1g`, a wall-clock timeout (default 300 s, max 600 s), in a subprocess,
  cancelled when the turn is stopped. Limits (spec call): `-Xmx1g -XX:MaxMetaspaceSize=256m`,
  `RLIMIT_CPU` = timeout + margin, `RLIMIT_NOFILE`, no `RLIMIT_AS` (the JVM reserves far more
  virtual memory than its heap and would not start); P6 has a start-up test under these limits.
  **No network:** analytics/telemetry and update checks are switched off with the pinned
  version's options (verified in P6), and the process runs without network access (own network
  namespace, e.g. `unshare -n`, when the container allows it; else the options plus a test that
  no connection is attempted). Designs never leave the host (§5 E). Sources: the Freerouting GitHub release JAR
  and the Eclipse Temurin 25 JRE tarball (linux x64; the app image is amd64-only today), both
  pinned by SHA-256 in one fetch script shared by the Dockerfile and CI; licence notices
  (Freerouting GPL-3, Temurin GPL-2 + Classpath Exception) in the image.
- **Flow (spike-proven, spec call):**
  1. export DSN through the API job (sees the live board, C7);
  2. locked critical routes go in as protected wiring;
  3. run Freerouting on all remaining connections, including ground (C7: it cannot skip GND);
  4. import the SES (`ImportSpecctraSession`);
  5. delete the imported ground copper — only ground items whose KIIDs are **not** in the
     pre-import snapshot (§6.3). Because the SES import may delete and re-create unlocked
     items, `pcb_fanout` and `pcb_stitch` create their stubs and vias **locked** (exported as
     protected wiring), so they keep their KIIDs and survive a re-fill; on a re-fill after S7
     the zone fills are cleared before the DSN export and refilled after the import (whether
     the API can clear fills is probed in P6; else the plane zones are removed and recreated
     from the pre-import snapshot). A P6 test asserts that fill-after-fanout keeps the same
     via positions (planes + fanout connect ground, §11);
  6. post-process (below), then metrics.
- **Ladder when completion < 100 %** (spec call): (a) re-run with more passes; (b) unlock the
  critical routes next to the unrouted connections and re-run — `pcb_unroute` gets an
  `unlock_only: true` option that clears the `locked` flag of the selected tracks/vias without
  deleting them. Step (b) selects **by net, critical signal nets only**; ground/plane-net items
  (fanout stubs, stitching vias) are never unlocked by it, and `unlock_only` with a `region`
  skips plane-net items unless they are named in `nets` — otherwise the re-fill's SES import
  could re-create them with new KIIDs and step 5 would delete them (the `session-board` skill names it for this step; the unlocked nets are returned
  by `pcb_unroute` and passed to `pcb_route_fill {relock_nets: [...]}`, whose post-processor
  re-locks them if they still pass M4/M7; a P6 test covers unlock → fill → re-lock); (c) route the rest with the own
  router (with vias); (d) report the open connections with positions and ask (placement change,
  more layers, larger board). The agent never calls the board done with open connections.
- **Post-processor** (`router/chamfer.py`, spec call for the parameters):
  - merge collinear segments; remove zero-length and duplicate segments;
  - **chamfer** every 90° corner: replace the vertex by a 45° segment of length
    `c = max(1.0 × width, 0.25 mm)` up to `3 × width`, the largest that passes DRC;
  - **acute (135° change) and non-octilinear segments**: rip the connection and re-route it with
    the own router;
  - **micro-jogs** and too-short chamfers (§12 definitions) and detours over the
    M5/M6 thresholds: rip and re-route with the own router;
  - every change is checked by the DRC task; what cannot be fixed is reported per connection.
- **Layers during the fill.** The planes are only poured at S7, so the fill must be told which
  layers it may use. Preferred mechanism (spec call): set the KiCad **layer type** of plane layers
  to "power" in the board setup before the DSN export, so KiCad's Specctra export marks them as
  plane layers and Freerouting does not route signals there — whether the layer type is settable
  through the API is probed in P2. Fallback: Freerouting CLI options; last resort: editing the
  exported DSN text (owner decision D4). The default 4-layer proposal is **Sig / GND / Sig+Power /
  Sig** (owner decision D1): In1 solid GND plane, In2 routable with power pours.

### 10.4 Why not chamfer-only (rejection rule)

If more than 20 % of Freerouting's connections need a rip-and-reroute (spec call), the
post-processor stops and reports "fill routing not acceptable" with the worst areas marked; the
agent then improves the placement (channels) or the critical routing and retries. This prevents
a board that is "fixed" piece by piece into a mess.

## 11. Planes, pours, fanout, stitching

- **GND planes:** for 4 layers, In1.Cu = solid GND (spec call), In2.Cu = signals + power pours in
  the default stack (D1) or a plane per the stack the user approved; for 2 layers, GND pour on B.Cu with as few cuts as possible.
- **Outer pours:** GND on F.Cu/B.Cu in free areas, priority below planes, thermal reliefs from the
  rules.
- **Fanout** (`pcb_fanout`, spike `fanout.py`): each SMD pad of a plane net gets a short stub
  (≤ 1 mm, 45° allowed) + via, candidates in 8 directions × 2 distances, legalised by refill + DRC
  task. Spike: 48/64 pads; fine-pitch/QFN pads need via-in-pad or pad-to-pour connection —
  v1 connects those through the outer pour where possible and reports the rest (spec call: no
  via-in-pad, it costs extra at JLC).
- **Stitching** (`pcb_stitch`): grid vias on GND (default pitch 5 mm, spec call) and an edge ring
  when asked; DRC-rejected and dangling vias removed.
- After every zone change: `refill_zones` (blocking).

## 12. Quality metrics (`board/metrics.py`)

All metrics come from one API read (`BoardModel`) plus the DRC task; ≈ 0.1–0.5 s on 76 parts.
Thresholds are **spec calls** unless marked owner; the plan may tune review thresholds on the
synthetic fixtures, but **hard thresholds change only with owner OK**.

| # | Metric | Definition | Hard (fail) | Review |
|---|---|---|---|---|
| M1 | Placement completeness | every footprint's courtyard fully inside the outline (= placed, §7.2), 0 courtyard overlaps, SMD on allowed sides | any violation | — |
| M2 | Routing completeness | unconnected items (DRC task) | > 0 | — |
| M3 | DRC + parity | errors; warnings not accepted (§12 notes); schematic parity issues (full check) | errors > 0, warnings not accepted > 0 | each accepted warning listed |
| M4 | Angles (owner) | segments not at 0/45/90/135/… (±0.5°); direction change at a 2-track vertex of the same net and layer ∉ {0°, 45°}; off-pad T-junction where a branch meets at 90° | any | — |
| M5 | Detour | per connection: routed length / octilinear shortest distance (pad to pad, same layer, ignoring obstacles) | any signal connection > 2.0 and > 5 mm extra | median > 1.15; any > 1.5 (critical nets > 1.25) |
| M6 | Long connections | routed length of a connection > 50 % of the board diagonal | — | each listed with a reason or fixed |
| M7 | Straightness | micro-jogs and too-short chamfers (definitions in the notes below); bends per 10 mm per connection | micro-jogs + too-short chamfers > 0 | bends/10 mm > 1.0 average |
| M8 | Decoupling / crystal | path length decoupling pad → served pin; crystal → MCU pins; side | > 5 mm or other side | > 3 mm |
| M9 | Switcher hot loop | input cap → VIN/GND pins, inductor → SW pin, switch-node copper length | — | > 3 mm / > 3 mm / > 5 mm |
| M10 | Grouping | per block: hull area / sum of courtyards; foreign parts inside a block hull | — | ratio > 3.0; intrusions > 0 |
| M11 | Planes | GND plane fill ratio per plane layer; signals crossing a plane void on the adjacent layer | — | fill < 85 %; crossings > 0 |
| M12 | Widths and pairs | track width < net class width; pair length mismatch; pair gap variation | width violation | mismatch > 1 mm |
| M13 | Edges and keepouts | connectors on their assigned edge; antenna keepout respected | violation | — |
| M14 | Fab rules (JLCPCB preset) | min track/space, via drill/diameter, annular ring, hole-to-edge, copper-to-edge, silk size, silk over pads, paste on all SMD pads | violation without a recorded custom-rule exception | — |

Notes:

- M4 is the owner's corrected rule: **no 90° corners**. Pad entries are not bends.
- **M8/M9 per stage** (spec call): before routing (S4, placement approval) the distances are
  **estimates** = octilinear pad-to-pad distance; after routing they are the routed path length.
  The hard gate (M8 > 5 mm) uses the routed length at S8; at S4 the estimate must be within the
  review threshold (≤ 3 mm) or the exception is shown to the user, so a passing placement does
  not first fail at S8 by surprise (routing adds detour; the estimate uses the review threshold
  for that margin).
- **Definitions** (spec call). A *connection* is a pad pair of the net's ratsnest (octilinear
  MST over its pads); its routed length is the shortest path between the two pads through the
  net's track/via graph (vias count their layer distance as 0). A *chamfer* is a pair of
  same-sense 45° bends (net 90° change) with a straight piece ≥ max(1 × width, 0.25 mm) between
  them; a *micro-jog* is a pair of **opposite-sense** bends closer than 2 × width; a *too-short
  chamfer* is a same-sense pair whose straight piece is shorter than max(1 × width, 0.25 mm).
  These are the **only** definitions: M7's hard count = micro-jogs + too-short chamfers, used
  identically by the metric, the router (§10.2) and the post-processor (§10.3). A valid chamfer
  is never a micro-jog. Metric tests cover a short same-sense pair (fails) and a valid chamfer
  (passes).
- **Accepted warnings** (M3): stored as a machine-readable table in `board.md` → Checks
  (`| DRC type | Items/refs | Reason | Accepted by |`), matched per check run (spec call); the agent
  adds a row only after the user accepted it. KiCad exclusions are not used.
- M3/M14: per-footprint DFM exceptions (spike: module thermal vias 0.2 mm, USB-C pad-to-NPTH
  0.19 mm) are handled as custom rules scoped to that footprint with a reason (G10), shown to the
  user in the check report; the agent never weakens a global rule to make a check pass.
- **JLCPCB preset values** (spec call, to be verified against JLC's current capability page in the
  plan and recorded there): 2/4-layer standard: track/space ≥ 0.127 mm (0.15 mm preferred), via
  ≥ 0.3 mm drill / 0.6 mm diameter, min drill 0.3 mm, annular ≥ 0.13 mm, copper-to-edge ≥ 0.3 mm,
  silk text ≥ 0.8 mm height / 0.15 mm line. Board thickness 1.6 mm, 1 oz outer.

## 13. Showing the board in chat

- **`pcb_render`** saves a board copy (`SaveCopyOfDocument`, C9), runs `kicad-cli pcb export svg`
  for the view's layers, wraps the SVG (mm scale bar, origin, board size label, optional block
  hulls with names, optional violation markers with ids that match the report; every injected string is
  XML-escaped, and the result goes through the same sanitiser allow-list as footprint renders,
  with the board limits below), and rasterises
  with the existing CairoSVG worker (`views/_raster_worker.py`, G9: no new dependency). The SVG
  link uses the existing renders route (`png|svg`, CSP) from the footprints feature.
- **Views:** `top` (F.Cu, F.Silk, F.Mask outline, Edge.Cuts), `bottom` (mirrored), `layer`
  (one copper layer + Edge.Cuts), `placement` (courtyards, refs, block hulls, airwires optional),
  `3d` (`kicad-cli pcb render`, top/bottom/iso; bare board until G7).
- **Size:** PNG ≤ 1600 px on the long side (spec call); a step report uses at most 3 images.
- **SVG limits:** the footprint sanitiser caps (`views/footprint_render.py`: 10 k elements,
  2000 coords per element, 4 MiB) are too small for boards with zone fills. Board renders get
  their own limits and simplify zone polygons before wrapping (spec call; values set in P1 on
  the fixtures).
- **Live view:** KiCanvas updates during the turn (§6.6).

## 14. Fab output (JLCPCB)

`pcb_fab_export {preset:"jlcpcb"}`:

- output dir `<project dir>/fab/jlcpcb/` (spec call), replaced on every export. The path is
  resolved and refused if `fab/` or `fab/jlcpcb/` is a symlink or resolves outside the project
  directory (as `projects.py` refuses symlinks); the export is written to a temp dir next to it
  and swapped in two steps (old dir → `jlcpcb.old-<id>`, new dir → `jlcpcb`, then the old one is
  removed; if the second rename fails the old one is renamed back), so a failed export leaves the
  old package intact; a test covers re-export over an existing package. Contents:
  - `gerbers/`: all copper layers, F/B mask, F/B silk, **F/B paste** (stencil), Edge.Cuts,
    `.gbrjob` (`--subtract-soldermask --check-zones`);
  - drill: Excellon, PTH and NPTH separate, mm, drill map;
  - `<board>-jlcpcb.zip` with gerbers + drill (the upload file);
  - `bom-jlcpcb.csv` (Comment, Designator, Footprint, LCSC/MPN) and `cpl-jlcpcb.csv`
    (Designator, Mid X, Mid Y, Layer, Rotation) — marked "not validated for JLC assembly" (§2);
  - `fab-report.md`: board hash, date, layer count, stackup, size, rule preset, metrics summary,
    accepted exceptions, order notes (thickness, surface finish, stencil).
- Refused unless the `board` checkpoint is valid (§8.3).
- Uses `kicad-cli` on the saved board only (no API writes).

## 15. Decisions

### 15.1 Decided by the owner (2026-11-23 … 2026-11-24)

- SP4 = the board. The agent does everything; the user steers only via chat prompts, never the
  KiCad GUI.
- Quality bar strict ("order without rework"); criteria: all parts placed, no long traces,
  straight segments, **only 45° direction changes (no 90°, no acute)**, clean straight traces,
  logical grouping.
- Step-by-step flow; the agent asks design questions (board size, layer count, …) instead of
  deciding silently; after each major step images + a short check report and wait for ok (at
  least after placement, before routing).
- Tools and skills generic; rules per part class, not per part.
- Approach A (agent-led placement with helpers; own octilinear router for critical/short nets,
  locked; Freerouting fills the rest, post-processed or rejected; planes/fanout/pours/stitching;
  self-check loop). B and C rejected.
- Fab: JLCPCB; self-assembly with hot plate + stencil for the acceptance board. 4 layers is the
  agent's proposal to the user, not hard-coded.
- Acceptance target: OpenDriveHub transmitter main board, laid out by the agent in PROD after the
  release.

### 15.2 Spec calls (made by the author, for review)

Listed in order of appearance; each is marked "(spec call)" in the text.

1. Fast interfaces (USB HS, DDR, Ethernet) and impedance-controlled routing out of v1.
2. Through vias only.
3. JLC assembly (CPL rotation offsets, LCSC numbers) not validated in v1; files still written.
4. Pure-Python core (`copperchat/board/`) separate from the KiCad layer.
5. Raw protobuf wrappers in `kicad_tools/board/api.py` for G3/G5; no kipy fork change needed.
6. `BoardChangeSet` + own engine, one commit per tool where possible (see 40, 41 for engine and change log).
7. Compensation by KIID diff for multi-stage tools; netlist import as the one exception (dry run
   first, undo via History).
8. `.kicad_dru` added to history snapshots; fab outputs not snapshotted.
9. `PCB_READ` in all session types; `PCB_WRITES` in Board and Free; `FP_WRITES` added to Board;
   schematic stays read-only in Board.
10. Board sessions may patch any `board.md` section.
11. Feature flags `COPPERCHAT_BOARD_TOOLS` and `COPPERCHAT_FREEROUTING`.
12. Live board view in scope (G8).
13. Tool names (`pcb_*`) and grouping; staging area for new footprints.
14. Outline v1 = rounded rectangle only.
15. Functional blocks stored as KiCad groups `blk:<name>`.
16. Tool costs for the change budget (§7.2), no board-specific budget.
17. Agent proposes values and asks for confirmation; board size from ≤ 45 % courtyard fill.
18. Two hard checkpoints (placement → routing, board → fab); others skill-enforced; any move
    after placement approval needs a new approval; approvals stored per project + board file.
19. `board.md` v2 sections incl. machine-readable tables for edges, blocks, class overrides.
20. Classification rules of §9.1.
21. Placement grid 0.5 mm, default channel gap 0.5 mm, routability estimate per 5 × 5 mm cell.
22. Part-class rule table §9.3 as the v1 set.
23. Critical set default (incl. 2-pin nets with airwire ≤ 10 mm).
24. Own router: 90° only as 2 × 45° with a straight piece ≥ max(1 × width, 0.25 mm); vias
    supported; coupled pairs for straight-ish pairs only.
25. Freerouting in the app image (not kicad-base), pinned + SHA-256, timeout 300/600 s, `-Xmx1g`.
26. Spike-proven fill flow (route all incl. GND, then delete GND copper) and the completion ladder.
27. Chamfer parameters; acute/non-octilinear/micro-jogs → rip and re-route.
28. Fill rejection when > 20 % of connections need rip-and-reroute.
29. Default stack Sig/GND/Sig+Pwr/Sig (confirmed by the owner, D1).
30. No via-in-pad; fine-pitch GND pads via the outer pour.
31. Stitching pitch 5 mm.
32. All metric thresholds in §12 (hard ones change only with owner OK).
33. JLCPCB preset values (to be verified in the plan).
34. Renders: SVG → CairoSVG, ≤ 1600 px, ≤ 3 images per step report.
35. Fab output in `<project>/fab/jlcpcb/`, replaced on each export.
36. Gap handling of §16 (per gap).
37. Testing approach of §18 (ODH not in CI; e2e golden with its own marker and CI job).
38. Phasing of §17 (`sp4_synth` fixture in P1).
39. Board creation: re-discover + reopen; "absent" manifest entries so restore deletes a created
    `.kicad_pcb` / `.kicad_dru`.
40. Board engine runs from `spec.apply`; per-`DocKind` commit; multi-stage tools for anything the
    DRC task must see; CPU work in a worker process.
41. Change-log entries with `sheet_path=""` and `BOARD_KINDS`.
42. `PLAN_TOOLS` and `doc_patch_section` in the board profile; `pcb_approve` plan-exempt.
43. `COPPERCHAT_BOARD_TOOLS` default off until P9, hiding the Board session type.
44. Fill snapshot of full copper data before `ImportSpecctraSession`; shielded compensation on
    cancel.
45. Staging-area placement before an outline exists; unplaced = courtyard not inside the outline; tool-placed holes recognised by having no schematic link; groups only for blocks.
46. Only the latest approval per stage counts; approval evidence per stage; `board_hash` content.
47. Connection / chamfer / micro-jog definitions; accepted DRC warnings as a `board.md` table.
48. Plane layers kept out of the fill via the KiCad layer type (probe in P2).
49. Stackup preset ids; Freerouting/JRE sources (amd64); `fab/` committed, 3D in `fab/3d/`.
50. Board-specific SVG limits.
51. Flag filter by explicit `PCB_WRITES` names; read tools stay with the flag off.
52. Board view publishes once per tool call from the adapter.
53. Moves of routed parts refused unless `unroute: true`.
54. `board_reviews` rows per evidence (render view / check level + verdict).
55. Router: ≥ 2 × width between opposite-sense bends.
56. Freerouting limits (no `RLIMIT_AS`) and no network access; GND deletion scoped to new KIIDs.
57. Fab export refuses symlinks / outside paths and swaps atomically.
58. M8/M9 estimates before routing, routed lengths after.
59. Restore re-discovers the project before reopening.
60. `placement_hash` includes lock flags, outline and keepouts.
61. Fanout/stitching vias created locked; zone fills cleared around a re-fill.
62. One M7 definition (micro-jog + too-short chamfer).
63. `board_reviews` with project/board columns and structured check fields.
64. Two-step directory swap for the fab export.
65. Both hashes returned by status/render/check, stored in `board_reviews`; `pcb_approve` takes the stage's hash.
66. Moves with `unroute: true` remove locked copper of the affected connections too.
67. `pcb_unroute unlock_only` for ladder step (b) (critical signal nets only, never plane-net items); shared fab path helper for the 3D export.

### 15.3 Owner decisions on the open questions (2026-11-24)

The owner answered all five open questions of the draft as recommended:

- **D1 — Default 4-layer stack: Sig / GND / Sig+Power / Sig.** In1 is a solid GND plane, In2
  carries signals and power pours. The agent still proposes the stack to the user per board; this
  sets the default proposal and what the tools must support.
- **D2 — Hard thresholds approved as written** (§12): M4 ±0.5°, no 90° and no acute bends; M5 fail
  when a signal connection's detour is > 2.0 and > 5 mm extra; M7 no micro-jogs; M8 decoupling
  > 5 mm fails; M14 the JLCPCB preset. They change only with the owner's OK.
- **D3 — 3D models / STEP case fit deferred to the optional phase P10.** Case fit is the owner's
  review against the repo's CAD; the agent takes the outline numbers from the docs or the user.
- **D4 — Editing the exported DSN text is allowed only as a last-resort fallback**, after the
  KiCad layer type and Freerouting's CLI options. The DSN is a Freerouting interchange file and
  never enters KiCad except through the SES import.
- **D5 — PROD memory of ≈ 4.5–5 GB per active board project is accepted** and documented as a
  host requirement (README, P9). `FP_WRITES` stays in board sessions.

## 16. Gaps from spike 4A and how they are handled

| Gap | Handling | Call |
|---|---|---|
| G1 `UpdateBoardStackup` has no handler | **In scope:** (labelled "4A-G1" in the patches README table, to avoid a clash with spike 3A's G1) KiCad patch `0005-TODO-owner-write-message-board-stackup-handler.patch` (name per the patches README pattern) (handler mirroring `handleGetStackup`, QA test), carried in `docker/kicad-base/patches/` like 0001–0004; kipy needs no proto change (proto exists). Commit text written by the owner; nothing upstream by agents (C14). Until the patched base image is published, `pcb_set_rules.stackup` returns `stackup_unavailable` and the default stackup is used. | spec call |
| G2 stale schematic jobs headless | **Workaround:** always save + `kicad-cli` netlist export (C2). No patch in SP4. | spec call |
| G3 project path without `/` | **Workaround** in `kicad_tools/board/api.py`. A kipy fix is optional, owner business. | spec call |
| G4 no routing command headless | **Deferred upstream** (needs a feature request by the owner). Own router replaces it. | spec call |
| G5 kipy wrappers missing | **Workaround:** raw protobuf in `kicad_tools/board/api.py`. | spec call |
| G6 Freerouting native CLI broken, Java 25 | **In scope:** JAR + JRE 25 in the app image (≈ +190 MB), pinned + SHA-256; `ignore_net_classes` not used (delete GND copper instead). No upstream report by agents. | spec call |
| G7 no 3D models in the image | **Deferred to an optional last phase (P10)**: fetch the KiCad 3D models for the footprints in use on demand into a cache volume (pinned to the KiCad library version) so STEP exports carry parts. v1 STEP/3D renders are bare boards and say so. The case-fit check stays an owner review against the repo's CAD (owner decision D3). | owner (D3) |
| G8 live board view at turn end only | **In scope** (§6.6). | spec call |
| G9 no rasteriser | **Solved already:** CairoSVG from the footprint feature (§13). | spec call |
| G10 footprint-level DFM exceptions | **In scope:** custom rules per footprint with a reason (`pcb_set_rules.custom_rules`), surfaced in every check report (§12). | spec call |

## 17. Phases (proposal for the plan)

Each phase is one PR with its own review loop; every phase is useful on its own and leaves the
product releasable (write tools behind `COPPERCHAT_BOARD_TOOLS` until P9).

| Phase | Content | Depends on |
|---|---|---|
| P1 Foundation | `copperchat/board/model.py`, `kicad_tools/board/api.py` (G3/G5), `BoardChangeSet` + engine + compensation, per-`DocKind` commit/modified state, worker process, `.kicad_dru` + "absent" manifest entries in history, board session type behind the flag (read-only tools), profiles (incl. `PLAN_TOOLS`, `doc_patch_section`), change-log `BOARD_KINDS`, `pcb_status`, `pcb_render` (G9 path, board SVG limits; block hulls once groups exist), live board view (G8), `board.md` v2 + write policy, `session-board` skill skeleton, probe: DRC visibility inside an open commit; **`sp4_synth` fixture generator + fixture** | — |
| P2 Sync + setup | `pcb_sync_from_schematic` (create board + reopen), `pcb_set_outline`, `pcb_set_rules` (without stackup), JLC preset; probe: layer type "power" via API (§10.3) | P1 |
| P2b Stackup patch | KiCad patch 0005 (G1) + kicad-base rebuild + `pcb_set_rules.stackup` | P2 (parallel; needs owner text for the patch message) |
| P3 Analysis + metrics + checks | `classify.py`, `metrics.py` (M1–M14), `pcb_analyze`, `pcb_metrics`, `pcb_check`, checkpoint tables + `pcb_approve` + guards | P1 |
| P4 Placement | placer (cluster/layout/legalise/score), `pcb_define_blocks`, `pcb_place_block`, `pcb_move`, `pcb_legalize`, placement checkpoint enforcement | P2, P3 |
| P5 Own router | router (grid, A*, layers/vias, pairs, simplify), `pcb_route`, `pcb_unroute`, locking | P4 |
| P6 Freerouting | JRE + JAR in the app image + CI fetch script, runner, post-processor (chamfer, rip-and-reroute), `pcb_route_fill`, `COPPERCHAT_FREEROUTING` | P5 |
| P7 Copper | `pcb_zones`, `pcb_fanout`, `pcb_stitch`, `pcb_tidy_silk` | P5 |
| P8 Fab | `pcb_fab_export` (board checkpoint), `pcb_export_3d`, fab report | P3, P7 |
| P9 Skills + acceptance | `session-board` full flow, `board-layout` skill (part-class rules, questions), `copperchat-tools` list, system prompt, synthetic end-to-end golden, ODH dry-run script + report, README/CHANGELOG, flag default on | P1–P8 |
| P10 (optional) 3D models | on-demand 3D model cache (G7), STEP with parts | P8 |

Order: P1 → (P2, P3 in parallel) → P4 → P5 → (P6, P7 in parallel) → P8 → P9; P2b whenever the
owner has written the patch message; P10 only with owner OK.

Phase test notes: P6 deletes the fill's ground copper, so M2 = 0 is only reachable with P7; P6
tests assert completion of the non-ground nets and M4/M7, and the end-to-end golden (P9) asserts
M2. P4 uses the P3 metrics for scoring.

## 18. Testing

### 18.1 Unit tests (`backend/tests/unit/board/`, no KiCad)

- `BoardModel` builders for synthetic boards (pads, nets, courtyards as plain data).
- Classification on synthetic netlists: decoupling, switcher (switch node), crystal, pairs,
  overrides from a `board.md` table.
- Placer: legality (no overlaps, inside outline, keepouts, locks) on random synthetic parts
  (property-based), `does_not_fit` reporting, determinism (same input → same output).
- Router: octilinear paths only, no 90°/acute vertices (property-based on random obstacle maps),
  via insertion, pad-exit rule, coupled pair offset.
- Post-processor: chamfer on hand-made 90° corners, acute detection, micro-jog detection, merge.
- Metrics: each metric on a hand-made geometry with a known answer (incl. M4 corner cases:
  pad entries, T-junctions, chamfers).
- Checkpoint guards (like `fp_approve`): hash mismatch, no human message, quote not a substring,
  plan running.

### 18.2 KiCad integration tests (`pytest -m kicad`, `backend/tests/kicad/board/`)

- **Fixtures:** `ecc83` (existing, 15 THT parts, 2 layers, with the demo's hand-routed board as
  human reference) and a new **synthetic SMD fixture** `sp4_synth` (≈ 25 parts: MCU-like IC with
  decoupling, buck regulator with inductor, crystal, USB-C with ESD, LED, connector, 2 sheets),
  created by a generator script through the KiCad API (schematic) with stock footprints from the
  image libraries and committed (KiCad-written, never hand-written); `scripts/refresh-fixtures.sh`
  calls the generator. Built in P1, used from P2 on.
- Per tool: sync (create board, re-sync keeps placement, delete extra), outline/rules round trip
  (`.kicad_pro`, `.kicad_dru`), place block legal, route a critical net (DRC-clean, octilinear),
  fill route + post-process (M4 = 0), zones/fanout/stitch, fab export file set, compensation
  (inject a failure after stage 2 → board unchanged), undo through history restore.
- **End-to-end golden on `sp4_synth`:** full flow with scripted "user" approvals → all hard gates
  pass; metrics recorded as a golden with tolerances. Runtime budget ≤ 3 min. It gets a new
  marker `kicad_e2e`, is deselected from the 8 round-robin `kicad` shards
  (`tests/support/sharding.py`, `unit/test_sharding.py` updated) and runs in its own CI job
  (spec call).
- Freerouting in CI: the kicad jobs run inside the kicad-base container, which has no Java; they
  fetch the pinned JAR + JRE with the same checksum-verified script as the Dockerfile (cached by
  `actions/cache`). Without it the fill tests skip with a visible reason locally, never in CI.

### 18.3 OpenDriveHub (not in CI)

The ODH board is the owner's design in another repo; it is not copied into this repo (spec call).
P9 adds a manual dry-run script (like the spike harness: copy of the ODH project into a tmp dir,
the flow driven through the tools with fixed answers) whose report (metrics, renders, runtime,
memory) goes into the P9 PR. The real acceptance is in PROD (§19).

### 18.4 Agent behaviour

A small eval in the style of spike 2D (live Claude, `tests/live`, P9, not CI-gated): a board session on `sp4_synth` checks that the
agent asks the S0 questions, stops after placement, and does not call `pcb_route` before an
approval (the server guard makes the last one deterministic).

## 19. Acceptance (after release, owner)

In PROD, a board session on the OpenDriveHub transmitter main board, driven only by chat:

1. The agent asks the design questions (at least size/outline, layer count, assembly/sides,
   connector edges, mounting holes) and records the answers in `board.md`.
2. Every step ends with images and a check report; the agent waits after placement and before fab.
3. Final `pcb_check full`: **all hard gates pass** — M1 complete, M2 0 unconnected, M3 0 errors and
   0 warnings that the user has not accepted, parity clean, M4 0 non-45° bends / 0 acute / 0 non-octilinear,
   M7 0 micro-jogs and 0 too-short chamfers, M12 widths, M13 edges/keepouts, M14 JLC rules.
4. Review metrics: detour median ≤ 1.15, every decoupling cap ≤ 3 mm, switcher loop metrics
   within limits, block ratio ≤ 3.0 — or each exception explained to and accepted by the owner.
5. The owner's visual review of the renders and of the board in KiCad's GUI: logical grouping,
   clean straight traces, "would order without rework".
6. The fab zip loads in JLCPCB's online Gerber viewer without errors (owner).
7. Undo from the History panel restores an earlier step.

## 20. Risks

| Risk | Mitigation |
|---|---|
| Routing quality at the owner's bar is not reached on dense boards (the spike's placement left 2–11 open connections with wide classes/planes) | placement ↔ routing loop with channel gaps; completion ladder (§10.3); the agent asks for more area/layers instead of lowering the bar; acceptance metrics known early via the ODH dry run in P9 |
| Freerouting changes behaviour between releases; third-party Java process | pinned version + checksum; post-processor and metrics judge its output; fill can be disabled |
| Own router grows into a full autorouter (scope creep) | v1 scope fixed in §10.2; the critical set is bounded |
| Tool calls that run minutes (fill, fanout) hit SDK/tool timeouts or block the turn | timeouts per tool (§10.3); the plan checks the SDK tool-call limit and progress notices |
| Memory in PROD: ≈ 1.9 GB board server; opening a footprint document (FP_WRITES in board sessions) can add ≈ 1.8 GB (up to 4 GB seen, footprints spec C4); Freerouting ≤ 1 GB heap → ≈ 4.5–5 GB per active board project | one session per server (existing); Freerouting heap capped; measured in P1/P9 and documented as a host requirement (owner decision D5) |
| The fill holds the edit lock for minutes, blocking other writes and the live view | read tools do not take the edit lock; the fill publishes once at the end; budget ≤ 600 s |
| Token cost of image-heavy step reports | ≤ 3 images per report; per-layer images only on request |
| Upstream KiCad patch 0005 needs owner time | stackup is optional for v1 (default stackup works for 1.6 mm JLC boards); patch phase independent |
| KiCad API behaviour changes at the next pin bump | integration tests per tool; `SUPPORTED_COMMITS` matrix |
| Generic classification misfires on unusual designs | overrides in `board.md`; the agent shows the classification at S3 and asks |

## 21. Left to the builder (plan level)

- Exact field schemas per tool (within the contracts of §7), error codes, and report texts.
- Tuning of review thresholds and router costs on the fixtures (hard thresholds need owner OK).
- Verification of the JLCPCB preset against JLC's current capability page.
- Router performance budget (grid size vs. board size), A* heuristics, caching of the obstacle map.
- How progress of long tools is shown (status notices) within the SDK limits.
- The `sp4_synth` fixture design and generator script.
- Migration numbering and the guard implementation shared with `fp_approve`.
