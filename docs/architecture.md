# CopperChat architecture

CopperChat is a self-hosted web app: one Docker image per instance, one user, a FastAPI
backend that drives KiCad 11 headless (`kicad-cli api-server`) and the Claude Agent SDK, and
a React PWA. The full design is in `docs/superpowers/specs/2026-10-15-foundation-design.md`;
the phase plans are in `docs/superpowers/plans/`.

## Images

- `ghcr.io/peterus/copperchat-kicad-base:<commit>` — KiCad 11 built from source at the
  commit in `docker/kicad-base/KICAD_COMMIT`, plus `kicad-python` (kipy) in `/opt/venv`,
  runtime user `copperchat` (uid 1000). Built by `.github/workflows/kicad-base.yml`.
  `docker/kicad-base/SUPPORTED_COMMITS` lists every base tag CI tests against.
- `ghcr.io/peterus/copperchat:<version>` — `docker/app/Dockerfile`: base image + Python
  backend (uv, `/app/backend/.venv`) + built frontend (`/app/frontend/dist`). Built and
  pushed by `.github/workflows/release.yml` on tags `v<semver>` (`:latest` for
  non-prerelease versions). Runs `python -m copperchat` as `copperchat` (uid 1000).
- `docker-compose.yml` — the app plus an optional `cloudflared` service (profile `tunnel`),
  mounts `./projects` → `/projects` and `./data` → `/data`.

## Backend (`backend/src/copperchat`)

| Module | Role |
|---|---|
| `config` | `Settings` from env/.env; startup fails without model credentials (`claude setup-token` hint) unless `DEV_FAKE_AGENT` |
| `db` | SQLite (WAL) + numbered migrations `0001`–`0005` (`schema_migrations`) |
| `projects` | discovery of `*.kicad_pro`, ids, KiCad lock detection |
| `kicad_session` | one `kicad-cli api-server` per open project, restart on hang/crash |
| `tools`, `kicad_tools`, `views` | read-only agent tools (structured JSON, full UUIDs, mm), ERC/DRC, agent views and KiCanvas snapshots |
| `agent` | run engine per project: gated streaming input, queue, interrupt, resume, rate-limit parking, message persistence, WebSocket protocol, tool guard |
| `history`, `git_tools` | per-turn snapshots/undo, autosave, external-change detection, crash recovery, git status/diff/commit |
| `skills` | built-in + user + project skills; the agent-scoped `part-notes` template |
| `research` | `research_part` MCP tool: a separate, guarded SDK query (web + writes only under `docs/parts/<MPN>/`) whose messages are streamed into the chat as a nested "Part research" card |
| `push` | VAPID keys (`/data/vapid.json`), subscriptions, visibility registry, notifier on `run-finished` / `rate-limited` frames |
| `web` | FastAPI app, login cookie, REST routes, `/api/ws`, security headers (CSP), static PWA with SPA fallback |

Design data is read only through the running API server (or `kicad-cli` for ERC/DRC and
exports) — never by a CopperChat file parser. The main agent has no file, shell or web
tools: its only SDK built-in is `Skill`; everything else is CopperChat's own MCP tools.

## Frontend (`frontend/src`)

React 19 + Vite + zustand + zod + Tailwind 4. `protocol/frames.ts` mirrors the backend
protocol; `protocol-fixtures/*.json` are validated by both test suites. The chat store merges
the REST baseline and the WebSocket replay by server id. The viewer embeds a vendored
KiCanvas built from the fork `peterus/kicanvas` (`frontend/vendor-patches/kicanvas/README.md`). `public/sw.js` handles push only and
never caches; `push/visibility.ts` reports page visibility so pushes go out only while no
window is visible.

## Data flow of a turn

Composer → `send` frame → run engine queue → SDK streaming input → tool calls through the
in-process MCP server → messages persisted and broadcast → turn end: autosave, snapshot,
`view-updated` → viewer reload; a push notification if no client is visible.

## CI

`.github/workflows/ci.yml`:

- `backend` — ruff, mypy, pytest without KiCad, `scripts/check-docs.sh` (README compliance
  statements, compose pin = `KICAD_COMMIT`).
- `frontend` — typecheck, lint, vitest, build, KiCanvas bundle check, Playwright on the real
  KiCanvas bundle (API mocked).
- `kicanvas-reproducible` — rebuilds the vendored bundle and diffs it.
- `kicad (<commit>)` — matrix over `SUPPORTED_COMMITS`, real-KiCad tests inside the base
  image.
- `e2e` — builds the app image, checks the token-missing refusal, starts it with
  `DEV_FAKE_AGENT=true` and the `ecc83` fixture, and runs the Playwright happy path
  (`frontend/e2e/smoke-docker.spec.ts`: login → project → message → reply → viewer, service
  worker, no CSP violations).

`release.yml` (tags `v*` only) builds, re-checks the token refusal and pushes the image.
