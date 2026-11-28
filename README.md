# CopperChat

Chat with an AI agent about your KiCad projects — in the browser on desktop, iPad and
iPhone. CopperChat runs KiCad 11 headless next to your projects, shows schematic and board
in an interactive viewer, and lets an agent inspect the design through KiCad's own API, run
ERC/DRC, look at rendered views, edit the schematic, and work through requirements and design
docs in separate sessions.
**Powered by Claude.**

CopperChat is an independent open-source project. It is **not Claude Code** and is not
affiliated with or endorsed by Anthropic.

## What it does

- Project picker over a mounted folder (any depth, every `*.kicad_pro`); a project that is
  open in the KiCad GUI opens read-only with a banner.
- Schematic and board viewer (KiCanvas, with sheet tree, layers, pinch zoom), plus annotated
  "agent views" — the same images the agent looks at. Tap a part to mention it (`@R12`).
- Chat with message queue, Stop, resume after rate limits, usage display, image/PDF
  attachments, a save after every successful edit and per-turn undo snapshots.
- Skills (built-in, your own, per project) and a part researcher that collects datasheets
  and pinout/application notes under `docs/parts/<MPN>/` in the project.
- Git status, diff summary and commit — only when you ask.
- Installable app (PWA) with push notifications when a run finishes, the agent needs you,
  or a rate-limit window reopens.
- **Schematic editing** (since v0.2.0): the agent places parts
  from the KiCad libraries, wires and labels them, adds power symbols, edits fields, moves,
  deletes and duplicates parts, annotates and works with hierarchical sheets — only through
  KiCad's API, every change verified against KiCad's netlist before it is reported.
  - Larger changes come as a **plan** you approve or reject in the chat; every plan step
    ends with an ERC check and a checkpoint you can roll back to.
  - When the agent revises a plan, the plan card is posted again at the bottom of the chat
    with the agent's reason and **Approve** / **Reject**; earlier cards of that plan shrink
    to one line "Superseded by revision N" (click to view them read-only). A step the
    agent interrupted for the revision shows "Paused – revision awaiting approval" and
    continues once you approve. Rejecting such a mid-step revision (**Reject revision**)
    does not end the plan: it returns to the previous revision, paused, with no edit lost.
  - A per-turn change budget (Settings › Editing), a **Changes** panel with the change log,
    before/after highlights in the viewer, and undo per turn or plan step.
  - Part preferences (e.g. passives in 0805) and part research before active parts.

- **Sessions and design docs** (since v0.3.0): several chat sessions per project, each of a
  type — **Requirements** (writes the design docs, cannot touch the schematic),
  **Schematic** (draws from the docs) or **Free**. Design docs under `docs/design/`
  (`requirements.md`, `architecture.md`, `parts.md`, `decisions.md`) in a **Docs** panel
  with edit, approval, conflict check, history and diffs. Closing a session hands over:
  the agent updates the docs and the summary becomes a decision. See
  [docs/user/sessions.md](docs/user/sessions.md).
- **Repository context:** the agent can read the project's repository (README, specs,
  OpenSCAD enclosures, firmware) with read-only `repo_list` / `repo_grep` / `repo_read`
  tools, and gets a short project brief at the start of each session. Secrets, `.git`,
  ignored files and KiCad files as text stay out of reach. See
  [docs/user/repo-context.md](docs/user/repo-context.md).
- **Mouser parts data** (optional, `MOUSER_API_KEY`): the agent answers availability,
  price, stock, lead-time and lifecycle questions from Mouser's Search API, and the part
  researcher adds a dated "Sourcing (Mouser)" section to the part notes.

- **Footprints** (unreleased): the agent reads any footprint (`fp_get`, `fp_list_local`),
  checks it against the symbol's pins and the datasheet's land pattern (`fp_check`) and
  draws it with a mm grid (`fp_render`) in every session type. In Schematic and Free
  sessions it can also create footprints from a cited datasheet (`fp_create`), approve
  them after you said ok in the chat (`fp_approve`) and delete local footprints the open schematic does not use (`fp_delete`, on request;
  History undo restores them) —
  only through KiCad's API.
  - One local library per repository at the repo root, `footprints/<repo>.pretty`
    (`_local` appended if a stock library has that name), referenced from each project's
    `fp-lib-table` as `${KIPRJMOD}`-relative row; other libraries stay read-only.
  - A new footprint carries `needs_user_review = yes` and its `Footprint source` until you
    approve it: the agent shows the render and every check, stops, and approves only in a
    later turn quoting your words. Assigning an unreviewed footprint is flagged.
  - Undo and history cover the library files and `fp-lib-table`, also across projects of
    the same repository.
  - Memory and time: the first footprint tool call in a session loads all footprint
    libraries into that project's api-server — measured about 5.7 s, and the server grows
    from about 230 MB to about 3.2 GB (plan for about 3 GB extra, 4 GB peak, per project
    that uses footprint tools). A render adds about 0.3–0.5 s. Sessions that never call a
    footprint tool do not pay this.
  - `COPPERCHAT_FOOTPRINT_TOOLS=off` removes the footprint tools from every session. Writes
    of a footprint call that crashed stay flagged (`needs review`) until the tools are on
    again (the next writing call cleans them up) or the turn is undone.

- **Board layout** (unreleased): in a **Board** session the agent lays out the board from
  the schematic, step by step with you — it asks the design questions (size, layer count,
  assembly, connector edges, mounting holes, keepouts, currents, special nets) with its
  own proposals, and after every step shows images and a short check report and waits for
  your ok or corrections. The schematic stays read-only there; every board change goes
  through KiCad's API, is undone from the History panel like any other turn and highlighted
  in the Changes panel.
  - Steps: board from the schematic → outline, mounting holes, keepouts, layer count and
    JLCPCB rules / stackup / net classes → functional blocks and a floorplan preview → whole-board
    placement → critical nets with CopperChat's own router → the rest with Freerouting →
    ground planes, fanout and stitching vias → tidy silk and the full check → fab package.
  - Quality gates (hard): only 45° direction changes (no 90°, no acute), T-junctions instead
    of Y-junctions, straight links between adjacent pads (no doglegs, no loops or jogs at pad
    entries), decoupling and crystal paths ≤ 5 mm, no long detours, JLCPCB rules, KiCad DRC
    and schematic parity; reviews for the rest.
  - Two approvals in the chat, enforced by the server: **"Approve this placement?"** before
    any routing and **"Approve this board for manufacturing?"** before the fab export; any
    later change makes the approval stale.
  - Fab package in `fab/jlcpcb/` (gerbers incl. paste for the stencil, Excellon drill, the
    upload zip, BOM, CPL, `fab-report.md`) and a bare-board STEP / GLB in `fab/3d/` (no part
    3D models yet).
  - Freerouting 2.5.0 (GPL-3.0) ships in the app image and runs locally as a separate,
    sandboxed process without network access; `COPPERCHAT_FREEROUTING=off` removes only the
    fill tool.
  - Memory (measured, for sizing — not a requirement): an active board project peaked at
    about **6 GB** at the same moment (a 76-part, 4-layer board): the
    project's api-server with the footprint libraries ≈ 3.1 GB plus the `kicad-cli`
    schematic-parity DRC of the full check ≈ 2.6 GB; the parity DRC waits until a running
    Freerouting fill (JVM ≈ 0.6 GB) and board computation (CPU worker ≈ 0.3–0.6 GB, restarted
    above 512 MB) have finished. The sum of the per-process peaks was ≈ 6.9 GB.
  - `COPPERCHAT_BOARD_TOOLS=off` removes the board write tools and the Board session type.

## Quick start

You need Docker with the compose plugin and a Claude subscription (or an API key).

1. Create a model token with **your own** Claude subscription, on any machine where the
   Claude CLI is installed and logged in:
   ```bash
   claude setup-token
   ```
   (Or use an `ANTHROPIC_API_KEY`.) CopperChat offers no login flow of its own;
   Anthropic's terms apply to the credentials you use with it.
2. Get `docker-compose.yml` and `.env.example` of the release you want to run (or clone the
   repository), then:
   ```bash
   curl -fsSLO https://raw.githubusercontent.com/peterus/copperchat/v0.12.0/docker-compose.yml
   curl -fsSLO https://raw.githubusercontent.com/peterus/copperchat/v0.12.0/.env.example
   cp .env.example .env          # paste the token, set ADMIN_PASSWORD
   mkdir -p projects data
   sudo chown -R 1000:1000 projects data   # the container runs as uid 1000
   docker compose pull           # ghcr.io/peterus/copperchat:latest
   docker compose up -d
   ```
3. Put KiCad projects into `./projects`, open <http://localhost:8080> and sign in with
   `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

If `docker compose pull` reports `denied`, the image is not published (or not public) yet —
use the from-source path below. The container refuses to start without a token and tells
you to run `claude setup-token`. A different host port: `COPPERCHAT_PORT=8090` in `.env`.

Images are published as `ghcr.io/peterus/copperchat:<version>` (e.g. `0.12.0`), `:<major>.<minor>`
(e.g. `0.1`) and `:latest`; release notes are in [CHANGELOG.md](CHANGELOG.md). To pin a
version set `COPPERCHAT_VERSION=0.12.0` in `.env`. Updating: `docker compose pull && docker
compose up -d` (your projects and `/data` stay).

**Upgrading to 0.3.0: back up `data/` first** (`docker compose down && cp -a data
data.bak-0.2`). The database moves to a new schema that v0.2 cannot read; each project's
chat becomes "Session 1" and keeps its history and agent memory
([details](docs/user/sessions.md#upgrading-from-v02)).

**From source** instead of the published image: in a clone of the repository run
`docker compose build && docker compose up -d`. The local build is tagged with the same image
name; `docker compose pull` switches back to the published image.

On plain `http://` from another machine in your LAN (not `localhost`), set
`COOKIE_SECURE=false`, otherwise the browser drops the login cookie: the login page then
reports "your browser did not keep the session cookie" and the server log warns about it.
For anything beyond your LAN use HTTPS (next section).

## Remote access and notifications

Put CopperChat behind any HTTPS reverse proxy. A ready-made option is Cloudflare Tunnel +
Cloudflare Access: set `CLOUDFLARE_TUNNEL_TOKEN` and `PUBLIC_BASE_URL` in `.env` and run
`docker compose --profile tunnel up -d` — see
[docs/deployment/cloudflare.md](docs/deployment/cloudflare.md). Web Push needs HTTPS; on
iPhone/iPad add the app to the Home Screen first (iOS 16.4 or later), then enable
notifications in Settings › Notifications.

## Security notes

- **One user per instance.** The app login (username + password, argon2 hash, rate-limited)
  is always on, also behind Cloudflare Access.
- **The main agent has no file, shell or web tools.** It only gets CopperChat's own design
  tools (KiCad API reads, schematic edits, ERC/DRC, views, git status/diff/commit-on-request),
  the read-only repository tools, the `Skill` tool, `research_part` and — with a
  `MOUSER_API_KEY` — the read-only Mouser lookups `mouser_search` / `mouser_part` (the key
  stays on the server and is never shown to the agent or written to logs). Design data is read and written only through the
  running KiCad API server — never by a CopperChat file parser or writer. Edits are refused
  for a project open read-only, over the change budget, and while a plan is open outside
  its begun step. Design docs are read and written only under `docs/design/` (no
  symlinks, size-capped), and each session type gets only its own tools.
- **Repository reads are confined.** The repository tools read only inside the project's
  repository root, never `.git`, `.env*`, keys, credentials, `.gitignore`d files or
  submodules, never KiCad files or binaries as text, with size and time caps, and are
  checked by the tool guard and again in the tool
  ([block list and limits](docs/user/repo-context.md#what-the-agent-cannot-see)).
- **The part researcher is guarded.** It runs as a separate agent query with web access
  (public hosts only, no private/loopback addresses, redirects re-checked) and may write only
  `.md`/`.txt` files under `<project>/docs/parts/<MPN>/` and read only `docs/parts/` and the
  project's uploads.
- Git is never touched automatically; there is no `git push` and no history rewriting.
- Model credentials live only in `.env` / the container environment and are passed to the
  agent process, never to the browser.
- The web app sends a strict Content-Security-Policy (same-origin scripts, no third-party
  requests); the service worker only shows notifications and never caches.
- Push subscriptions are accepted only for the browsers' push services (Google, Mozilla,
  Apple, Microsoft); keys live in `/data/vapid.json`.

## Configuration

| Variable | Meaning |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` | model credentials (one required) |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | app login (always on; the password seeds the hash on first start) |
| `COPPERCHAT_VERSION` | image tag (default `latest`; e.g. `0.7` or `0.12.0` to pin) |
| `COPPERCHAT_PORT`, `COPPERCHAT_BIND` | host port (default 8080) and bind address (default: all addresses; `127.0.0.1` = this machine only) |
| `PUBLIC_BASE_URL` | public HTTPS URL (WebSocket origin check behind a proxy) |
| `VAPID_SUBJECT` | push contact, e.g. `mailto:you@example.com` |
| `MOUSER_API_KEY` | optional Mouser Search API key: price, stock and lifecycle lookups (`mouser_search` / `mouser_part`) |
| `COPPERCHAT_GIT_NAME`, `COPPERCHAT_GIT_EMAIL` | optional fallback author/committer identity for `git_commit`; used only when the repository (or git config) has no `user.name` / `user.email` |
| `COPPERCHAT_FOOTPRINT_TOOLS` | `on` (default) or `off`: footprint tools (`fp_*`); `off` saves the ~3 GB per api-server they need |
| `COPPERCHAT_BOARD_TOOLS` | `on` (default) or `off`: board layout tools (`pcb_*` writes) and the Board session type; `off` restores the read-only board |
| `COPPERCHAT_FREEROUTING` | `on` (default) or `off`: `off` removes only the Freerouting fill (`pcb_route_fill`) |
| `COPPERCHAT_FREEROUTING_HOME` | Freerouting runtime folder (default `/opt/freerouting`; the image ships it) |
| `CLOUDFLARE_TUNNEL_TOKEN` | optional `cloudflared` service (`--profile tunnel`) |
| `FORWARDED_ALLOW_IPS` | trusted reverse-proxy addresses (default: the compose `cloudflared`) |
| `COOKIE_SECURE=false` | only for plain-http LAN use |
| `DEV_FAKE_AGENT=true` | development: scripted fake agent, no token needed |

Mounts: `/projects` (your KiCad projects; CopperChat writes autosaves, `.kicad-agent/`
history and uploads, `docs/parts/` and `docs/design/` there) and `/data` (database, agent sessions, user
skills, password hash, session secret, push subscriptions, VAPID keys, logs). Both must be
writable by uid 1000.

## Development

See [docs/architecture.md](docs/architecture.md).

```bash
cd backend && uv sync && uv run pytest -m "not kicad"     # backend (Python 3.13)
cd frontend && npm ci && npm run dev                       # frontend on :5173, proxies /api to :8080
```

`DEV_FAKE_AGENT=true` runs the backend without a model token. Tests that need a real KiCad
API server run inside `ghcr.io/peterus/copperchat-kicad-base:<commit>`
(`uv run pytest -m kicad`).

## License

GPL-3.0-only. See [LICENSE](LICENSE).

## Third-party software

- **KiCad** (GPL-3.0-or-later) — built from source in the base image
  (`docker/kicad-base/`).
- **KiCanvas** (MIT) — vendored in `frontend/public/vendor/kicanvas/` (see `LICENSE.md` and
  `BUILD_INFO` there), built from a pinned commit of branch `copperchat` of the fork
  [peterus/kicanvas](https://github.com/peterus/kicanvas) (upstream plus KiCad 11 format
  support, touch gestures, host integration, no external font requests; see
  `frontend/vendor-patches/kicanvas/README.md`). Rebuild with `frontend/scripts/vendor-kicanvas.sh`.
