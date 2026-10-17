# CopperChat frontend

Vite + React 19 + TypeScript + Tailwind 4. Node >= 20.19.

```bash
cd frontend
npm ci
npm run typecheck && npm run lint && npm test -- --run && npm run build
```

If your shell exports `NODE_ENV=production`, prefix npm commands with `env -u NODE_ENV`
(otherwise `npm ci` skips the dev dependencies).

## Run locally against the real backend

1. Backend (fake agent: no model token needed; opening a project still starts
   `kicad-cli api-server`, so a KiCad 11 nightly must be on `PATH` or in `KICAD_CLI`):

   ```bash
   cd backend
   uv sync
   DEV_FAKE_AGENT=true ADMIN_PASSWORD=dev PROJECTS_ROOT=~/hardware DATA_DIR=/tmp/cc-data \
     COOKIE_SECURE=false uv run python -m copperchat        # listens on 0.0.0.0:8080
   ```

   `ADMIN_PASSWORD` seeds the `admin` user only on the first start with a fresh `DATA_DIR`.
   `COOKIE_SECURE=false` is required over plain http, otherwise the browser drops the
   session cookie and every request is 401.

2. Frontend dev server with the proxy (`/api` incl. the `/api/ws` WebSocket → backend):

   ```bash
   cd frontend
   npm run dev                                            # http://localhost:5173
   COPPERCHAT_BACKEND=http://other-host:8080 npm run dev  # backend elsewhere
   ```

   Sign in as `admin` / `dev`.

**LAN access (iPad / iPhone):** the dev server already listens on all interfaces
(`server.host: true`, same as `vite --host`); open `http://<lan-ip>:5173`. Vite only accepts
IP addresses and `localhost` as Host by default — for a hostname such as `mybox.local` add it
to `server.allowedHosts`.

**Origin must match Host:** the backend refuses WebSockets whose `Origin` does not match the
`Host` (or `X-Forwarded-Host`, or `PUBLIC_BASE_URL`) with close code 4403. The Vite proxy
therefore forwards the browser's Host unchanged (`changeOrigin: false`) — do not change that,
and behind another reverse proxy pass `Host`/`X-Forwarded-Host` through.

Production: `npm run build` and point the backend's `FRONTEND_DIST` at `frontend/dist`; the
backend then serves the app itself on port 8080.

## End-to-end smoke (local only, not in CI)

```bash
npx playwright install chromium   # once
npm run e2e                       # starts its own Vite on E2E_PORT (default 5199), API mocked
```

Against a running backend + dev server (the opt-in test needs an openable project):

```bash
E2E_BASE_URL=http://localhost:5173 E2E_USERNAME=admin E2E_PASSWORD=dev npm run e2e
```
