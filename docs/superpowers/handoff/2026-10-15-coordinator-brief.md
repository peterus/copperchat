# Coordinator brief — 2026-10-15

Handoff from the brainstorming session (run in the OpenDriveHub Kontor project)
to the CopperChat coordinator session. Read this, then the spec.

## Where we are

- Spec (approved by the owner): `docs/superpowers/specs/2026-10-15-foundation-design.md`
  — sub-project 1 "Foundation". Sub-projects 2–4 are only outlined there.
- Repo: `github.com/peterus/copperchat`, GPL-3.0-only, branch `main`.
- **Next step: implementation plan for the Foundation** (superpowers
  `writing-plans`), then implementation.

## How the owner wants to work

- **Delegate heavily to Kontor child sessions** (`kontor-manager-cycle`): the
  coordinator stays lean — spawn, wait for `done`, review/merge over the GitHub
  API, never work in the main checkout.
- The plan itself should be written by a child (spec → plan → PR), reviewed and
  merged by the coordinator. Then one child per plan phase / PR.
- Communication with the owner: German. Repo content: English.
- No hand-editing of KiCad files, ever — only through KiCad's API / `kicad-cli`.
  CopperChat's main agent therefore has no file/shell/web tools.

## Suggested plan shape (for the plan-writing child)

Order matters — de-risk first (spec §11):

1. **Spikes** (throwaway, results recorded in `docs/`):
   - Python Agent SDK pulls streaming input lazily at turn boundaries.
   - KiCanvas renders KiCad 11 (master) `.kicad_sch` / `.kicad_pcb`, incl. click events.
   - `kicad-cli api-server` round-trip for schematic + PCB: `GetItems`,
     `SaveCopyOfDocument`, `SaveDocument`, PNG export, crash/restart behaviour.
2. **KiCad base image** — source build of a pinned master commit in GitHub
   Actions, published to `ghcr.io/peterus/copperchat-kicad-base:<commit>`.
3. **Backend skeleton** — FastAPI, SQLite, auth, project discovery,
   `kicad_session`.
4. **Read-only `kicad_tools` + `views`** (agent view renderer, KiCanvas snapshots).
5. **Agent runtime** — streaming input, queue, interrupt, resume, rate limit,
   WebSocket protocol.
6. **Frontend** — chat history (Kontor lessons, spec §5.2), viewer, responsive
   layout, themes.
7. **Skills, part researcher, history/undo, git tools, uploads, usage display.**
8. **PWA + Web Push, Cloudflare docs, compose file, README.**

Phases 3–8 can partly run in parallel children once 1–2 are done.

## Environment facts (verified 2026-10-15 on the owner's machine)

- KiCad source: `~/build/kicad` (git clone of gitlab.com/kicad/code/kicad),
  Ninja build in `~/build/kicad/build`, currently checked out and installed at
  **10.0.7** in `/usr/local` (GUI, `kicad-cli`, Python bindings in
  `/usr/local/lib/python3/dist-packages`). Flatpak KiCad was removed.
  `kicad-cli api-server` exists only on **master** (since 2026-03-21), not 10.0.x.
- Linux nightly AppImages with `api-server`: downloads.kicad.org/kicad/linux/explore/nightlies
  (latest seen: 10.99.0.5265, commit 2026-10-11). Docker `kicad/kicad:nightly`
  is stale (2026-02-27) — unusable.
- Master API surface (checked 2026-10-15): schematic handler has GetItems,
  PlaceSymbolFromLibrary, GetSchematicHierarchy, GetSchematicNetlist,
  SaveDocument(As), SaveCopyOfDocument, RunSchematicJobExport{Png,Svg,Pdf,…};
  board handler adds PlaceFootprintFromLibrary, RefillZones, GetConnectedItems,
  design-rule get/set, Specctra DSN/SES, RunBoardJobExport*. No ERC/DRC via API
  → `kicad-cli`.
- Agent auth: `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` (owner's Max
  subscription). Agent SDK docs forbid third parties *offering* claude.ai login;
  CopperChat must not ship a login flow — users bring their own token.
- Feedback-loop proof (spike): `~/tmp/pcb-spike/` — `agentview.py` (throwaway
  agent-view renderer via pcbnew SWIG, to be re-done from API data),
  `av0..av4.png` renders, DRC JSONs. Lessons from kicad-mcp-pro 4.3.0: UUIDs
  truncated to 8 chars, false "verification failed" on delete, first read after
  write sometimes "Incorrect state", file parser lost net names on KiCad 10
  files, placement heuristics report phantom overlaps.

## Reference: Kontor (the owner's own agent web UI)

`~/CloudCLI/kontor` — copy its proven patterns (TypeScript there, Python here):

- Agent run engine / queue / interrupt / resume: `server/runEngine.ts`,
  `server/runEngine/inputStream.ts`, `server/wsChat.ts`.
- Chat history client: `src/stores/chat.ts`, `src/components/MessageList.tsx`.
- Push/PWA: `server/pushDispatcher.ts`, `server/pushConfig.ts`, `public/sw.js`,
  `public/manifest.webmanifest`.
- Specs: `docs/superpowers/specs/2026-04-30-chat-pipeline-design.md`,
  `2026-05-02-session-naming-and-history-perf-design.md`,
  `2026-06-23-message-queueing-streaming-input-design.md`.
