# CopperChat backend

Python 3.13, FastAPI, managed with [uv](https://docs.astral.sh/uv/).

## Development commands

```bash
cd backend
uv sync                                   # install deps incl. dev group
uv run pytest -m "not kicad"              # unit tests, no KiCad needed
uv run pytest -m kicad                    # needs `kicad-cli api-server` (KiCad 11 master) on PATH
uv run ruff check . && uv run ruff format --check .
uv run mypy src
DEV_FAKE_AGENT=true ADMIN_PASSWORD=dev PROJECTS_ROOT=~/hardware DATA_DIR=/tmp/cc-data \
  COOKIE_SECURE=false uv run python -m copperchat
```

KiCad tests run inside `ghcr.io/peterus/copperchat-kicad-base:$(cat ../docker/kicad-base/KICAD_COMMIT)`
or with the KiCad nightly AppImage described in `docs/spikes/1c-api-server.md`.

## Configuration

All settings are environment variables (or `.env`): `PROJECTS_ROOT`, `DATA_DIR`,
`ADMIN_USERNAME`, `ADMIN_PASSWORD` (seeds the login once), `SESSION_SECRET`
(generated into `$DATA_DIR/session_secret` if unset), `CLAUDE_CODE_OAUTH_TOKEN`
(from `claude setup-token`) or `ANTHROPIC_API_KEY`, `KICAD_CLI`,
`KICAD_API_TIMEOUT_S`, `KICAD_START_TIMEOUT_S`, `HOST`, `PORT`,
`PUBLIC_BASE_URL`, `DEV_FAKE_AGENT`, `COOKIE_SECURE`, `FRONTEND_DIST`,
`FORWARDED_ALLOW_IPS` (proxies trusted for `X-Forwarded-For`, default `127.0.0.1`).

## Dependencies

`kicad-python` is pinned to a git revision (see `pyproject.toml`) because the
0.8.0 wheel on PyPI cannot import `kipy.schematic` / `kipy.board_jobs`.
