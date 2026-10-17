# CopperChat

Chat with an AI agent about your KiCad projects — in the browser on desktop, iPad and
iPhone. CopperChat runs KiCad 11 headless next to your projects, shows schematic and board
in an interactive viewer, and lets a read-only agent inspect the design through KiCad's own
API, run ERC/DRC and look at rendered views. **Powered by Claude.**

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

The agent never edits schematics or boards in this release.

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
   curl -fsSLO https://raw.githubusercontent.com/peterus/copperchat/v0.1.0/docker-compose.yml
   curl -fsSLO https://raw.githubusercontent.com/peterus/copperchat/v0.1.0/.env.example
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

Images are published as `ghcr.io/peterus/copperchat:<version>` (e.g. `0.1.0`), `:<major>.<minor>`
(e.g. `0.1`) and `:latest`; release notes are in [CHANGELOG.md](CHANGELOG.md). To pin a
version set `COPPERCHAT_VERSION=0.1.0` in `.env`. Updating: `docker compose pull && docker
compose up -d` (your projects and `/data` stay).

**From source** instead of the published image: in a clone of the repository run
`docker compose build && docker compose up -d`. The local build is tagged with the same image
name; `docker compose pull` switches back to the published image.

On plain `http://` from another machine in your LAN (not `localhost`), set
`COOKIE_SECURE=false`, otherwise the browser drops the login cookie. For anything beyond
your LAN use HTTPS (next section).

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
- **The main agent has no file, shell or web tools.** It only gets CopperChat's own
  read-only design tools (KiCad API, ERC/DRC, views, git status/diff/commit-on-request), the
  `Skill` tool and `research_part`. Design data is read through the running KiCad API
  server — never by a CopperChat file parser.
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
| `COPPERCHAT_VERSION` | image tag (default `latest`; e.g. `0.1` or `0.1.0` to pin) |
| `COPPERCHAT_PORT`, `COPPERCHAT_BIND` | host port (default 8080) and bind address (default: all addresses; `127.0.0.1` = this machine only) |
| `PUBLIC_BASE_URL` | public HTTPS URL (WebSocket origin check behind a proxy) |
| `VAPID_SUBJECT` | push contact, e.g. `mailto:you@example.com` |
| `CLOUDFLARE_TUNNEL_TOKEN` | optional `cloudflared` service (`--profile tunnel`) |
| `FORWARDED_ALLOW_IPS` | trusted reverse-proxy addresses (default: the compose `cloudflared`) |
| `COOKIE_SECURE=false` | only for plain-http LAN use |
| `DEV_FAKE_AGENT=true` | development: scripted fake agent, no token needed |

Mounts: `/projects` (your KiCad projects; CopperChat writes autosaves, `.kicad-agent/`
history and uploads, and `docs/parts/` there) and `/data` (database, agent sessions, user
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
  `BUILD_INFO` there), built from the pinned upstream commit with CopperChat's patches from
  `frontend/vendor-patches/kicanvas/` (KiCad 11 format support, touch gestures, host
  integration, no external font requests). Rebuild with `frontend/scripts/vendor-kicanvas.sh`.
