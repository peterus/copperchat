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
  attachments, autosave after every turn and per-turn undo snapshots.
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

Boards (PCB) stay read-only.

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
   curl -fsSLO https://raw.githubusercontent.com/peterus/copperchat/v0.3.0/docker-compose.yml
   curl -fsSLO https://raw.githubusercontent.com/peterus/copperchat/v0.3.0/.env.example
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

Images are published as `ghcr.io/peterus/copperchat:<version>` (e.g. `0.3.0`), `:<major>.<minor>`
(e.g. `0.1`) and `:latest`; release notes are in [CHANGELOG.md](CHANGELOG.md). To pin a
version set `COPPERCHAT_VERSION=0.3.0` in `.env`. Updating: `docker compose pull && docker
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
  the `Skill` tool and `research_part`. Design data is read and written only through the
  running KiCad API server — never by a CopperChat file parser or writer. Edits are refused
  for a project open read-only, over the change budget, and while a plan is open outside
  its begun step. Design docs are read and written only under `docs/design/` (no
  symlinks, size-capped), and each session type gets only its own tools.
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
| `COPPERCHAT_VERSION` | image tag (default `latest`; e.g. `0.3` or `0.3.0` to pin) |
| `COPPERCHAT_PORT`, `COPPERCHAT_BIND` | host port (default 8080) and bind address (default: all addresses; `127.0.0.1` = this machine only) |
| `PUBLIC_BASE_URL` | public HTTPS URL (WebSocket origin check behind a proxy) |
| `VAPID_SUBJECT` | push contact, e.g. `mailto:you@example.com` |
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
