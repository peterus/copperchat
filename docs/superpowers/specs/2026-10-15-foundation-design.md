# CopperChat — Foundation (sub-project 1) design

Date: 2026-10-15
Status: draft, awaiting review

## 1. Purpose

CopperChat is a self-hosted web app for designing KiCad schematics and PCBs by
talking to an AI agent. The user sees the schematic and the board in the
browser and types prompts; the agent does the work through KiCad's own API and
verifies every step with ERC/DRC and rendered images it actually looks at.

It is an open-source project for any hardware project, not tied to a specific
board. Target users run it locally (single user per instance) and may reach it
from phone/tablet over their own tunnel.

### Why it exists

Existing MCP-based approaches failed on three points (measured 2026-10-15 with
kicad-mcp-pro 4.3.0 against KiCad 10.0.5/10.0.7):

- The agent had no feedback loop for PCB work — it could not see what it did.
- Schematic edits went through ad-hoc file writers because KiCad 10's IPC API
  has no schematic editing; PCB edits needed an open KiCad GUI.
- The tool layer misreported state: truncated UUIDs, false "verification
  failed" errors after successful deletes, a file parser that lost all net
  names on KiCad 10 files, placement heuristics reporting non-existent
  overlaps.

A spike on 2026-10-15 proved the loop that works: edit via IPC → save →
`kicad-cli` DRC → annotated "agent view" render → the agent reads the image →
corrects. KiCad 11's headless `kicad-cli api-server` (master since 2026-03-21)
removes the GUI requirement and adds schematic editing to the API.

## 2. Scope

The whole product is split into four sub-projects, each with its own spec,
plan and implementation cycle:

1. **Foundation** (this document) — infrastructure, viewer, chat, read-only
   agent tools, skills, part research, history, notifications.
2. **Schematic editing** — place/wire/label/edit symbols, ERC feedback loop,
   plan-before-edit, visual before/after diff.
3. **Schematic → PCB** — netlist import, footprint assignment, board outline,
   design rules, mechanical import (DXF/STEP).
4. **PCB layout** — placement, routing, zones, optional Freerouting,
   fabrication export.

The foundation's agent is **read-only on design data**: it answers questions,
inspects, runs ERC/DRC and renders views. Mutation infrastructure (history
snapshots, autosave, view refresh) is built and tested now so sub-project 2
only adds editing tools.

### Non-goals (foundation)

- Any schematic or PCB editing tool.
- Multiple simultaneously open projects (architecture supports it; UI does not).
- Hosted multi-tenant service, user accounts beyond the single local owner.
- Providers other than Claude (tools stay provider-neutral, no abstraction layer).
- `git push` or any history-rewriting git operation.

## 3. Key decisions

| Topic | Decision |
|---|---|
| Deployment | Self-hosted, `docker compose up`, one user per instance |
| KiCad | KiCad 11 (master) built from source at a pinned commit; headless `kicad-cli api-server` |
| Backend | Python, FastAPI, `kicad-python` (`kipy`), Claude Agent SDK (Python) |
| Frontend | TypeScript, Vite, React (same stack as Kontor, so its proven chat code and lessons transfer); responsive (desktop, iPad, iPhone), PWA |
| Agent | Claude Agent SDK; built-in file/shell/web tools disabled for the main agent |
| Auth (model) | `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` (user's own subscription), or `ANTHROPIC_API_KEY`. No login flow is offered by CopperChat itself |
| Auth (app) | Username + password login, always on |
| Remote access | Documented: Cloudflare Tunnel + Cloudflare Access; optional `cloudflared` compose service. Any HTTPS reverse proxy works |
| Viewer | KiCanvas for the human view; own annotated agent view for the agent (also viewable). Fallback if KiCanvas fails on KiCad 11 files: implement own viewer |
| Save | Autosave after every agent turn that changed a document |
| Undo | Own snapshots per turn under `<project>/.kicad-agent/history/`; git is never touched automatically |
| Git | Narrow agent tools (status, diff summary, commit) — commit only on explicit user request |

## 4. Architecture

Single application container built in two image layers:

- `copperchat-kicad-base:<kicad-commit>` — KiCad only (`kicad-cli`, libraries).
  Built from source in CI
  (1–2 h, cached), only when the pinned KiCad commit changes.
- `copperchat:<version>` — `FROM copperchat-kicad-base:<commit>` plus backend
  and built frontend. Builds in seconds. Switching KiCad = changing one build
  arg; a CI matrix can test the app against several base tags.

Images are published to `ghcr.io`; users never build KiCad locally.

`docker-compose.yml` mounts:

- `/projects` — the user's hardware directory (any depth; projects are
  discovered by `*.kicad_pro`).
- `/data` — CopperChat state: SQLite DB, Claude SDK session store, user-level
  skills, password hash, push subscriptions, VAPID keys.

and reads `.env` (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`, admin
credentials, optional `CLOUDFLARE_TUNNEL_TOKEN`).

### 4.1 Backend modules

| Module | Responsibility |
|---|---|
| `kicad_session` | Per-project session manager. Starts `kicad-cli api-server <project> --socket <per-project path>`, holds the `kipy` client, health-checks, restarts on crash/hang. Keyed by project so multiple sessions are possible later; the UI opens one at a time and switching saves + stops the old server. |
| `kicad_tools` | Plain Python functions with no Claude dependency. Foundation: read-only queries, ERC/DRC via `kicad-cli`, exports. All data comes from the running API server — never from a CopperChat file parser. |
| `views` | (1) KiCanvas snapshots via `SaveCopyOfDocument` into `.kicad-agent/view/`; (2) the agent view renderer, drawn from API data (no deprecated pcbnew SWIG bindings). |
| `history` | Pre-mutation snapshot per turn, list, restore. |
| `agent` | Claude Agent SDK runtime: streaming-input session per project, message queue, interrupt, resume, tool registration, skills assembly, sub-agents, `canUseTool` guards. |
| `research` | The `part-researcher` sub-agent definition and its storage under `<project>/docs/parts/`. |
| `git_tools` | `git_status`, `git_diff_summary`, `git_commit` scoped to the current project. |
| `push` | Web Push (VAPID) subscriptions and dispatch. |
| `web` | FastAPI: auth, REST (projects, history pages, skills, parts, uploads), WebSocket, static frontend. |
| `db` | SQLite: messages, runs, queue, sessions, history index, push subscriptions. |

### 4.2 Frontend

- Project picker (from `/projects`), with lock-file warning.
- Viewer: schematic / PCB tabs (KiCanvas), toggle to agent view, layer toggles.
  Tapping a component inserts `@REF` into the prompt (requires KiCanvas click
  events; otherwise provided by our own viewer).
- Chat: history with lazy loading, streaming, collapsible tool calls, inline
  agent-view thumbnails (tap → fullscreen), image/PDF attachments, queued
  messages with cancel, Stop button, usage indicator.
- History panel: turns with preview, undo.
- Skills page, Parts page, Git status + "Commit" button (sends a commit prompt).
- Settings: theme (light / dark / system), notifications, account.

Layouts: desktop and iPad landscape = viewer left, chat right, resizable
divider; iPad portrait = chat as collapsible sidebar; iPhone = single column
with bottom tab bar (Chat, View, History, More), fullscreen pinch-zoom viewer,
composer pinned above the keyboard. Theming via CSS variables throughout; the
GUI-facing agent view follows the theme, the agent-facing render is always one
fixed style.

## 5. Agent runtime and data flow

Modelled on Kontor (`~/CloudCLI/kontor/server/runEngine.ts`, `wsChat.ts`;
specs `2026-04-30-chat-pipeline-design.md`,
`2026-06-23-message-queueing-streaming-input-design.md`).

1. Browser sends `send{clientMessageId, text, attachments}`. Server INSERTs the
   message with `status=queued` first (unique index on `client_message_id`
   makes retransmits no-ops), then checks for an active run.
2. Each project has one long-lived SDK query in streaming-input mode fed by a
   push-stream queue. The SDK pulls the next queued message only at a turn
   boundary; queued messages can be cancelled. **To verify early:** the
   Python SDK pulls lazily like the TypeScript SDK does.
3. Every turn gets a `runId`; `run-started` / `run-finished{reason}` frame it.
4. Streamed text is persisted periodically (not only at turn end); thinking,
   tool use and tool results are separate message rows.
5. Before the first mutating tool call in a turn, `history` snapshots the
   project (unused by foundation tools, exercised by tests).
6. At turn end, if a document changed: `SaveDocument` (autosave), then
   `SaveCopyOfDocument` into `.kicad-agent/view/`, then `view-updated`.
7. Interrupt cancels queued messages, closes the input stream, calls
   `interrupt()` with abort as backstop.
8. Session resume: SDK session id stored per project; `resume` +
   `forkSession` only at burst start, never mid-burst.
9. Rate limit: messages are parked and auto-resumed when the window reopens;
   the UI shows when.

### 5.1 WebSocket protocol

Client → server: `subscribe{projectId, sinceMessageId}`, `send`,
`interrupt{runId}`, `cancel-queued{messageId}`, `ping`.
Server → client: `message`, `message-delta{messageId, contentDelta |
statusPatch | metadataPatch}`, `run-started`, `run-finished`, `view-updated`,
`usage`, `error`. **Every frame carries `projectId`.** Frames are validated
with a schema on both sides.

### 5.2 Chat history in the client (Kontor's lessons, built in from day one)

- Initial load: `GET …/messages?limit=50` (newest); **subscribe only after**
  the HTTP baseline set `lastSeenId` (Kontor's worst bug class: racing replay
  and HTTP page → duplicates/out-of-order). Guard against double mount with a
  shared in-flight promise.
- Older pages: `before=<oldestId>&limit=50`; server fetches `limit+1` to set
  `hasMore`. Triggered by an IntersectionObserver on a top sentinel, gated on
  "not stuck to bottom".
- Scroll anchoring to the topmost visible message element (not height deltas).
- Stick-to-bottom within 80 px; otherwise a "↓ newest" pill.
- Store: messages keyed by server id; optimistic rows use negative ids from a
  counter and are replaced by the server row with the same `clientMessageId`;
  every update produces new objects (no in-place mutation); live rows are
  inserted in id order.
- Reconnect (and `visibilitychange` → visible): reset `runActive`
  (server re-sends `run-started`), re-subscribe from `lastSeenId`, HTTP-fetch
  the last 50 and merge by id (heals half-streamed rows). A delta for an
  unknown message id triggers this heal instead of being dropped. Failed
  older-page loads reset their loading flag.
- Socket: singleton, exponential backoff 0.5–15 s, outgoing queue flushed on
  open; server heartbeat pings.

## 6. Agent tools (foundation)

Rules: data only from the API server; structured JSON; full UUIDs; mm
coordinates; filters instead of pagination; errors only for real failures,
success only after verification.

| Tool | Returns |
|---|---|
| `project_info` | files, sheets, board size/outline, layers, design rules, KiCad version |
| `sch_list_symbols(filter)`, `sch_get_symbol(ref)` | symbols with value, footprint, fields; pins with nets |
| `sch_get_net(name)` | all schematic pins on a net |
| `pcb_list_footprints(filter)`, `pcb_get_footprint(ref)` | position, rotation, side, pads with net and position |
| `pcb_get_net(name)` | pads, tracks, vias of a net with full ids |
| `run_erc`, `run_drc` | violations: type, severity, position, items (via `kicad-cli` on the saved file) |
| `render_view(doc, mode, region?, layers?)` | an image the agent sees |
| `git_status`, `git_diff_summary`, `git_commit(message)` | project-scoped; commit only when the user asked |
| `research_part(query)` | delegates to the `part-researcher` sub-agent, returns a concise summary |

`render_view`:

- `mode=agent`: mm grid with absolute coordinates, reference + position on
  every part, **stable net colours** (deterministic hash), unrouted
  connections, ERC/DRC markers, label de-cluttering.
- `mode=kicad`: KiCad's own PNG export via the API.
- `region`: crop by mm box or "around REF" — essential for dense boards.
- Schematic: KiCad PNG export + coordinate grid + ERC markers overlay.
- Images are returned as image content in the tool result and also shown in
  the chat.

## 7. Skills

Three layers, most specific wins:

| Layer | Location | Owner |
|---|---|---|
| Built-in | inside the image, read-only | CopperChat releases |
| User | `/data/skills/` | the user, across projects |
| Project | `<project>/.kicad-agent/skills/` (committable) | per board |

The backend assembles the effective skill set per session and passes it to the
SDK. GUI: list with origin badge, "customise" (copies built-in into the user
layer and opens an editor), per-project enable/disable, "reset to original",
and an "original updated" notice with diff when a customised built-in changes
upstream.

The built-in schematic skill expresses style **by circuit type** (labels for
MCU/bus-heavy sections, wired left-to-right signal flow for analog paths)
rather than a single global rule. The OpenDriveHub `kicad-hardware` skill is
the starting material for the built-in content (decoupling, pull-ups, library
policy, validation discipline).

## 8. Part research sub-agent

`part-researcher` (SDK sub-agent) answers questions such as "how is the
TPS62133 wired and laid out?":

1. Look in `<project>/docs/parts/<MPN>/` for `datasheet.pdf` and `notes.md`.
2. Otherwise search the web, download the datasheet, write `notes.md`
   (pinout, reference circuit, layout recommendations, pitfalls, sources).
3. Return a concise summary to the main agent; the main agent's context stays
   small. Later questions reuse `notes.md`.

Only this sub-agent has web and write access, and writes are restricted to
`docs/parts/` by a `canUseTool` path guard. The main agent has no file, shell
or web tools. PDFs and images attached in chat are stored under `docs/parts/`
(PDFs) or `.kicad-agent/uploads/` (images). The GUI "Parts" page shows and
edits the notes; user edits take precedence.

## 9. Notifications

Web Push with VAPID (pattern: Kontor `server/pushDispatcher.ts`,
`public/sw.js`, `manifest.webmanifest`). Requires HTTPS and, on iOS, the PWA
installed to the home screen (iOS ≥ 16.4). Events: run finished, agent needs
attention (error, question), rate-limit window reopened. Sent only when no
client of that user is visible; tapping opens the project.

## 10. Error handling

| Situation | Behaviour |
|---|---|
| API server crash / hang (timeout) | restart with the project; current turn ends with a clear chat error; last autosaved state is current |
| Crash mid-mutation | offer restoring the turn's pre-mutation snapshot |
| KiCad lock file present | open read-only with "open in KiCad — close it there" |
| Files changed outside (e.g. `git pull`) | hash check before each turn; reload and note it in the chat |
| Rate limit | queue parked, auto-resume, UI shows the time |
| Missing/expired token | startup error pointing to `claude setup-token` |
| Tool errors | structured error (what, why, suggestion) to the agent; no false alarms |

## 11. Testing

- Tool tests against a real headless `api-server` in CI with small fixture
  projects in the repo (no KiCad mocks).
- Snapshot tests for agent-view rendering.
- Chat pipeline tests for Kontor's bug classes with a fake agent: page/replay
  ordering, duplicate sends, reconnect mid-run, stuck Stop, half-streamed
  heal, scroll anchoring.
- CI matrix over supported `kicad-base` tags.
- **First plan steps (de-risking):** Python SDK lazy pull at turn boundaries;
  KiCanvas rendering of KiCad 11 files including click events; `api-server`
  schematic + PCB round-trip (`GetItems`, `SaveCopyOfDocument`, PNG export).

## 12. Open source and compliance

- Name: CopperChat. Must not be branded "Claude Code"; "Powered by Claude" is
  allowed (Agent SDK branding guidelines).
- CopperChat offers no claude.ai login of its own; every user brings their own
  credentials (`claude setup-token` or API key). README states that
  Anthropic's terms apply to the user's credentials.
- License: to be decided before the first public push.

## 13. Outlook (later sub-projects, not specified here)

- Sub-project 2: editing tools, plan-before-edit for larger changes, visual
  before/after diff per turn.
- Sub-project 3: netlist → board, footprints, outline, rules, DXF/STEP
  mechanical import with fit check.
- Sub-project 4: placement, routing, zones, Freerouting via DSN/SES,
  fabrication package (Gerber, drill, BOM, CPL) with pre-export checks.
- Optional: `git push` behind an explicit setting.
