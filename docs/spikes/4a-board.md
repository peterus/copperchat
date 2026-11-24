# Spike 4A — board (.kicad_pcb) via the KiCad IPC API, kicad-cli and Freerouting

## Question(s)
SP4: the in-chat agent takes a schematic to an orderable PCB (netlist into board, outline + holes,
stackup + rules for JLCPCB, placement, routing, zones, DRC loop, fab outputs), step by step, steered
only by chat prompts. Quality bar "order without rework": everything placed, no long traces, only
45°/90° bends, straight clean traces, logical grouping. Tools must be generic. What can we really do
headless in our pinned kicad-base image?

Q1 schematic → board · Q2 board setup · Q3 placement · Q4 routing (API tracks / Freerouting / own router)
· Q5 zones + DRC · Q6 quality metrics · Q7 showing the user · Q8 JLCPCB outputs · Q9 gaps.

## Setup
- Image `ghcr.io/peterus/copperchat-kicad-base:52b43fb8…-r98c78374f0daab9d` (KiCad master `52b43fb8`
  + our 4 API patches, kipy fork `e0c9c8e`). Headless `kicad-cli api-server <project>`; the board must be
  opened with `OpenDocument(DOCTYPE_PCB)` first (otherwise *"no handler … GetOpenDocuments"*). KiCad
  sources read at the pin: `pcbnew/api/api_handler_pcb*.cpp`, `api_handler_board.cpp`,
  `eeschema/eeschema_jobs_handler.cpp`, `pcbnew/specctra_import_export/specctra_export.cpp`,
  `api/proto/board/*.proto`.
- Test boards (`spikes/4a-board/boards.py`): **ecc83** = KiCad demo `ecc83-pp` (15 THT parts, 1 sheet,
  local `.pretty`), board replaced by an empty board created by KiCad; the demo's hand-routed board is
  the human reference. **OpenDriveHub main** = copy of `hardware/transmitter/pcb/main` +
  `footprints/OpenDriveHub.pretty` @ `2e17ac5` (`fetch_odh.sh`, gitignored; PROD copy untouched):
  **76 footprints** (the "~160 parts" are symbols incl. power symbols), 76 nets, 327 pads, 4 sheets,
  board 80 × 60 mm, 4 layers, M2 corner holes.
- Freerouting **2.5.0** (latest): the native `freerouting-cli` crashes on start of routing
  (*GraalVM `UnsupportedFeatureError … ComponentOutline` serialization*) and then hangs → the **JAR on
  Java 25** (class file 69; Java 21 refuses) is used, JRE copied from `eclipse-temurin:25-jre` into
  `.deps` (`fetch_deps.sh`). The kicad-base image has no Java.
- Harness `spikes/4a-board/` (`./run_all.sh`, docker + host ImageMagick for SVG→PNG): 9 probes,
  **87 checks, 84 pass**; the 3 failing checks are routing-completion findings (`q04c.hybrid_complete`, `q05.hybrid.gnd_fanout`, `q05.hybrid.complete`), kept failing on purpose, not crashes.
  Full run ≈ 20 min, timings below vary by a few % between runs; `results/*.json` holds the last run.

## Results

| # | Question | Gate | Result |
|---|---|---|---|
| 1 | Schematic → board | **go** | `kicad-cli sch export netlist --format kicadsexpr` (0.8 s ecc83 / 2.7 s ODH) → `Board.import_netlist()` (`ImportNetlist`, handler = `BOARD_NETLIST_UPDATER`, same as "Update PCB from Schematic"): ecc83 15 fps in 0.02 s, **ODH 76 fps / 76 nets in 2.5 s, 0 errors**, 2 warnings (*"No net found for component J402 pad 10/9 (no pin in symbol)"* — a real symbol/footprint pin mismatch). `dry_run` changes nothing. Project-local library via `${KIPRJMOD}/../../../../footprints/OpenDriveHub.pretty` resolves (L201 placed with its 2 pads). Every footprint carries `symbol_sheet_name` (`/Power/` 50, `/MCU/` 12, `/Display_SD_RTC/` 10, `/Link/` 4), `symbol_path`, `symbol_sheet_filename`, `symbol_footprint_filters` → grouping by sheet is free. New footprints land spread out (not stacked). **Re-sync**: move all parts, change R1 value + delete R2 in the schematic (API), save, re-export, re-import → **placement kept**, value updated, R2 removed with `delete_extra_footprints`. **Trap:** the schematic API job `Schematic.export_netlist()` returns a **stale** netlist (neither the unsaved nor the saved edit) — headless, `EESCHEMA_JOBS_HANDLER` caches its own copy of the schematic (`m_cliSchematic`, loaded from disk once per server). Use save + fresh `kicad-cli`. RSS of a board server after the ODH import: **≈1.9 GB** (footprint libraries). |
| 2 | Board setup | **go**, stackup **no-go** | One `BoardRectangle` on Edge.Cuts with `corner_radius` (rounded outline, DRC + DSN export accept it). Mounting holes: `place_footprint_from_library("MountingHole:MountingHole_2.2mm_M2", pos)` works headless after a lazy `get_library_footprint` (0.06 s; no `load_all_libraries` needed); schematic mounting holes (ecc83 P5–P8) can be moved to the corners instead. Layers: `set_enabled_layers(4, …)` 2→4→2→4 works (0.03 s; 4→2 deletes inner content). **Stackup: `UpdateBoardStackup` → "no handler"** (proto exists); KiCad keeps a default 4-layer stackup (3 × 0.48 mm dielectric) and writes no `(stackup …)` → JLC stackups (JLC04161H-7628 etc.) and impedance are **not settable**. Design rules: `SetBoardDesignRules` (min clearance/track/via/drill/annular/edge/hole, predefined track + via sizes) round-trips and lands in `.kicad_pro`. Net classes: `Project.set_net_classes` + pattern assignment (`GND` → class) apply to board nets (`get_netclass_for_nets` shows the composite class); **trap:** `Board.get_project()` sends the path without the trailing `/` that KiCad compares against → *"project … is not open at path"*; fix by appending `/`. Custom rules: `SetCustomDesignRules` (structured `CustomRule`) → `CRS_VALID`, written to `.kicad_dru`. Keepout: `Zone` type `ZT_RULE_AREA` with keepout tracks/vias/copper on 4 layers. |
| 3 | Placement | **go** (API), placer **prototype** | Move/rotate (`update_items` of **76 footprints in 0.06–0.09 s**), flip (`flip_items`, 0.014 s, layer → B.Cu), bounding boxes (`get_item_bounding_box`, all at once ≈ 0.01 s), courtyards as `F/B.CrtYd` shapes on every footprint (80/80), groups via `create_items(Group)` (kipy `Group.name` has no setter → set `proto.name`). No placement helper in the API; no ratsnest query (computed ourselves: per-net Euclidean MST). **Generic placer** (`place.py`, no design knowledge): clusters = IC/connector (≥ 5 pads) + small parts sharing its signal nets (+ decoupling caps by "supply + ground"), cluster layout around the anchor pad sides, **greedy constructive block placement** (big blocks first, every 1 mm grid position × 4 rotations, cost = net distance to placed pads + pull to the own sheet + connectors to the edge), local refinement of 2-pad parts, 0.05 mm snap. Legal by construction (KiCad DRC: **0 courtyard overlaps** on both boards). ecc83: airwire 323 → 252 mm (human layout 238 mm), 0.6 s. **ODH: airwire 2 667 → 1 466 mm, 3.1 s**, all 76 parts inside 80 × 60. Judged by render (`results/q03_odh_placed.png`): sheets are grouped (power left, MCU centre, SD/RTC right, connectors on edges), but dense with silk overlaps (63) and decoupling not tight (Q6). First version without "big blocks first" left the CR1220 holder (BT401) off-board → the agent must report "does not fit" and ask (bigger board / bottom side). |
| 4 | Routing | see recommendation | **(a) API:** `Track`/`Via`/`ArcTrack` via `create_items` work (2 segments in 0.01 s); a netless track starting on a pad gets the pad's net; connectivity readable (`get_connected_items`). **The DRC task gives per-item feedback**: `StartDrcTask` → `GetTaskStatus` → `GetDrcTaskResult` returns `DrcMarker`s with the involved KIIDs, type and text (*"Clearance violation (netclass 'Default' clearance 0.4000 mm; actual 0.2814 mm)"*) in **0.05 s (ecc83) / 0.2 s (ODH)** → DRC can be the oracle inside a router loop. **KiCad's interactive router (walkaround/shove, diff pairs, length tuning) is not reachable:** `RunAction` → *"RunAction is not available in headless mode"*; no routing command exists in the API. **(b) Freerouting:** `kicad-cli pcb export specctra` (0.5 s; the API job `RunBoardJobExportSpecctraDsn` also works and sees the live board) → Freerouting JAR → `ImportSpecctraSession` (0.06 s, 0 skipped). ecc83: 20 connections, **100 % in 3.7 s**, 0 vias, DRC copper-clean. **ODH (4 layers, default 0.2 mm class): 225 connections 100 % routed in ≈ 60 s** (15 s routing + optimizer), 80 vias, 823 segments, **0 non-45° segments**, 5 acute bends, detour median 1.16 (worst 1.8), DRC copper-clean. But: signals run on In1/In2 (no planes), power at 0.2 mm, long buses around the module, many 45° jogs (`results/q04_odh_freerouting.png`). Completion is fragile: with 0.4 mm supply/GND classes or with filled GND planes in the DSN (exported as protected wiring) **6–11 connections stay unrouted** and the run takes 170–195 s (the optimizer is skipped when not complete); `--router.autorouter.ignore_net_classes=GND` did **not** stop it routing GND. **(c) Own router:** `router.py` = octilinear grid A* (0.05 mm, states with direction, 45°/90° turns cost, 135° forbidden, ends snapped octilinear), obstacles from API pads/tracks/vias, every result checked by the DRC task: **15/15 short 2-pin nets (crystal, USB, inductor, op-amp, …) routed in 6.5 s, 50 segments, all 0/45/90, 0 acute, 0 DRC rejects** (`results/q04c_odh_astar_top.png`). Freerouting on top of these fixed pre-routes then leaves **7 unconnected** (137 s) — pre-routes must leave channels or be unlocked for Freerouting. |
| 5 | Zones + DRC | **go** | Copper zones via `create_items(Zone)` (GND on In1/In2, outer pours), `refill_zones` blocking: 0.56 s (inner) / 0.73 s (all) on ODH; filled polygons readable. **Hybrid flow that works:** Freerouting everything (100 %) → delete its GND copper via the API (239 items) → GND planes In1/In2 → `fanout.py` (SMD GND pad → 45/90° stub + via, candidates in 8 directions × 2 distances, legalised by refill + DRC task per round: **48/64 pads in 13.8 s**, the rest are fine-pitch/QFN pads) → outer GND pours → **2 unconnected** left. Stitching vias legalised the same way: 192 grid candidates, 66 rejected by DRC, 126 kept in 2.0 s (some become dangling vias where no pour exists → filter by `DANGLING_VIA` too). **DRC:** API task 0.2 s vs `kicad-cli pcb drc --format json --schematic-parity` 7.1 s (process + load); same unconnected count; reports courtyard, silk (63 overlap / 65 over copper), edge, hole, parity (4: the 4 added holes), `lib_footprint_mismatch` (J402). Real DFM hits found by DRC on ODH: ESP32-S3-WROOM-1U thermal vias 0.2 mm < 0.3 mm min drill (16×), USB-C (GT-USB-7010ASV / stock footprint) pad-to-NPTH 0.19 < 0.25 mm (4×) → footprint-level rule exceptions needed. |
| 6 | Quality metrics | **go** (cheap) | `metrics.py` from API data in ≈ 0.1 s: non-45° segments, bend histogram (0/45/90/135), acute bends, dangling ends, track length vs MST airwire per net (detour), vias per net, decoupling distance (C* with ground + supply-net pad → nearest supply pad of a ≥ 5-pad part), compactness per sheet (bbox / sum of courtyards). Human ecc83: 211 mm, detour 1.08, 45° bends 40 / 90° 1; ours + Freerouting: 273 mm, detour 1.07, all 0/45/90. ODH ours: 11 decoupling caps classified, **6 farther than 3 mm** (worst C303 6.1 mm to U301 3V3), compactness `/Power/` 3.95 vs `/MCU/` 1.92 — the metrics separate "legal" from "good" and point at the fixes. |
| 7 | Showing the user | **go** | `kicad-cli pcb export svg --mode-single --page-size-mode 2 --exclude-drawing-sheet --layers <bottom→top>` = board-area composite or per-layer SVG in ≈ 0.6 s (PNG export exists but plots the whole sheet; `--scale 0` autoscale is unreliable); the image has no rasteriser (spike uses host ImageMagick; product: cairosvg/resvg or send SVG). 3D: `kicad-cli pcb render` headless works (top basic 1.2 s, iso `--quality high --floor` 8.5–11 s). STEP 0.6 s / GLB 0.6 s — but **no 3D models in the image** (`${KICAD10_3DMODEL_DIR}` missing: 22 "Could not add 3D model" on ecc83) → bare boards, useless for case fit until models are provided. In-memory board string: `SaveDocumentToString` 99 k chars in 0.025 s. KiCanvas board view today: refreshed **only at turn end** (or restore/external reload) from a `SaveCopyOfDocument` snapshot (`kicad_session/session.py:691`); the 2 s-debounced live view (`history/live.py`) is schematic-only (`hooks.py:201`), the frontend already accepts live `pcb` frames. |
| 8 | JLCPCB outputs | **go** | `kicad-cli pcb export gerbers --layers … --subtract-soldermask --check-zones` (all Cu incl. In1/In2, F/B.Paste for the stencil, mask, silk, Edge.Cuts, `.gbrjob`) ≈ 1.2 s; `export drill --excellon-separate-th --generate-map -u mm` (PTH/NPTH); `export pos --format csv --units mm` → JLC CPL (Designator, Mid X/Y, Layer, Rotation; 76 rows); `export bom --fields Value,Reference,Footprint,${QUANTITY},LCSC,MPN --group-by Value,Footprint` → JLC BOM (41 lines on ODH; LCSC field empty in this design). Zip for upload. Not covered: JLC's per-part rotation offsets in CPL, LCSC part numbers (Mouser-sourced, self-reflow here — stencil + bare PCB is the main order). |

## Findings
1. **Board editing headless is broad and fast.** Netlist import, outline, holes, layers, rules,
   net classes, keepouts, footprint moves (76 in < 0.1 s), tracks/vias/arcs, zones + refill, DRC with
   per-item markers (0.2 s), Specctra export/import, all fab outputs — no GUI needed.
2. **Routing is the hard part, as expected.** KiCad's own router is unreachable headless. Freerouting
   routes the realistic board completely in ~1 min with clean 45/90 geometry, but its layer usage, power
   widths, plane handling and "tidiness" are not at the owner's bar, and completion collapses when
   tracks get wider or channels are taken. Our own octilinear A* produces exactly the wanted geometry
   for short/critical nets and the DRC task makes every own-router step verifiable.
3. **Placement quality drives routing quality.** The generic placer is legal and grouped by sheet in
   3 s, but dense; wider tracks / planes / pre-routes then leave 2–11 connections open. The flow needs a
   placement ↔ routing loop (spread clusters, keep routing channels) judged by metrics.
4. **Two KiCad behaviours to design around:** stale schematic jobs in a headless server (use save +
   kicad-cli) and the missing stackup handler.

## Routing recommendation (evidence above)
**Hybrid, agent-orchestrated, DRC-in-the-loop:**
1. Placement: generic constructive placer (`place.py` idea) + user-steered constraints from chat
   (fixed parts, edge connectors, keepouts) + spacing for routing channels; judged by airwire,
   decoupling distance, compactness and a render shown to the user.
2. Critical/short nets first with **our own octilinear router** (decoupling, switcher loop, crystal,
   USB pair, short 2-pin nets): deterministic 0/45/90, rule widths per net class, each step checked by the
   API DRC task; locked afterwards.
3. Bulk signals with **Freerouting 2.5.0 (JAR, Java 25)** over DSN/SES; route all nets, then delete its
   GND copper through the API and connect GND by planes + **fanout** + pours; refill + DRC.
4. Clean-up + quality gates (metrics) + DRC loop; on open connections or bad metrics the agent
   re-places/re-routes and asks the user (bigger board, more layers, bottom side).
Not recommended: a full own autorouter (large), waiting for KiCad's interactive router in the API
(not on the roadmap at this pin), Freerouting alone (fails the owner's quality bar).

## Q9 — gaps and effort
| Gap | Where | Effort |
|---|---|---|
| G1 `UpdateBoardStackup` has no handler (stackup thickness/material/finish/impedance not settable) | KiCad patch (handler mirrors `handleGetStackup`; + QA test) | S–M (≈ 1–2 days) |
| G2 headless schematic jobs use a cached disk copy (`m_cliSchematic`) → stale netlist/exports after API edits | KiCad patch (use the API document / drop cache on save) or always save + kicad-cli | S |
| G3 project commands from a board document fail on the missing trailing `/` | kipy fix (normalise path) or KiCad compare normalised | XS |
| G4 no routing command (PNS walkaround/shove, diff pair, length tuning) headless | KiCad feature (needs core-dev feature request first) | L (upstream) — work around with own router |
| G5 kipy wrappers missing: DRC task, `GetTaskStatus`, Specctra import/export job, `SaveDocumentToString`; `Group.name` setter | kipy fork (raw protobuf works today) | XS–S |
| G6 Freerouting: native CLI 2.5.0 broken; `ignore_net_classes` ineffective; needs Java 25 (+≈190 MB JRE) | image/infra (JRE in app image or sidecar); upstream bug report optional | S |
| G7 3D models not in the image → STEP/3D renders without parts (case fit impossible) | infra: install/download KiCad 3D models (large) or fetch per footprint on demand | M |
| G8 live board view only at turn end | CopperChat (generalise `LiveViewPublisher` to `pcb`, board snapshot via `SaveCopyOfDocument`) | S |
| G9 no PNG rasteriser in the image | CopperChat dependency (cairosvg/resvg) or serve SVG | XS |
| G10 footprint-level DFM exceptions (ESP32 0.2 mm thermal vias, USB-C pad–NPTH) | custom rules per footprint (`SetCustomDesignRules`, works) + agent knowledge | S |

## Risks
- Routing quality at the owner's bar needs real iteration on placement + router; Freerouting is a
  third-party Java dependency (GPL-3, separate process) whose behaviour changes between releases.
- Memory: ≈ 1.9 GB per board server (footprint libraries), Freerouting allocates heavily (≈ 0.4–0.6 GB
  peak heap, 100+ GB churn on hard boards).
- DRC-legalised generators (fanout, stitching) run several refill + DRC rounds; fine at 76 parts (≈ 14 s),
  to be measured on bigger boards.

## For later / not tested
- JLC stackup + impedance (blocked by G1), diff-pair routing, length matching, teardrops (API
  `TeardropDefaults` exists, not probed), thermal reliefs per pad, silkscreen cleanup (63 overlaps),
  bottom-side placement policy for reflow, JLC CPL rotation offsets, panelisation.
- Track-width DRC hits (2–4 `track_width` after Freerouting on ODH) not analysed.
- Real case fit (STEP vs `assembly_check.scad`) once 3D models exist.
