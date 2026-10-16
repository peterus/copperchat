# Phase 3: Backend skeleton

**Goal:** A runnable FastAPI backend (`python -m copperchat`) with configuration
(C1), SQLite + migration runner (C2), always-on login, project discovery with
lock detection (C3), the per-project `kicad-cli api-server` session manager
with crash/hang recovery (C4), the tool spec/registry package (C5), `/api/health`,
a static-frontend stub, and a CI workflow running lint, format check, mypy and
the non-KiCad test suite.

**Depends on:** 1C (spike result `docs/spikes/1c-api-server.md`: api-server
command line, kipy accessors, kipy revision).

**Parallel with:** 2.

**Read first:**
- `docs/superpowers/plans/2026-10-15-foundation-plan.md` — Global Constraints,
  Review Focus (lines 2, 3, 5), Interface Contracts C1–C5 and C8, Phase Conventions.
- `docs/superpowers/specs/2026-10-15-foundation-design.md` §3, §4, §4.1, §10, §11, §12.
- `docs/spikes/1c-api-server.md` (Decision section; recorded api-server command
  line; recorded kipy revision).

**PR title:** `foundation phase 3: backend skeleton`

**Files:**
- Create: `backend/pyproject.toml`, `backend/uv.lock`, `backend/.python-version`,
  `backend/README.md`
- Create: `backend/src/copperchat/__init__.py`, `__main__.py`, `config.py`, `errors.py`, `projects.py`
- Create: `backend/src/copperchat/db/__init__.py`, `db/migrations/0001_core.sql`,
  `db/repos/__init__.py`, `db/repos/projects.py`
- Create: `backend/src/copperchat/web/__init__.py`, `web/app.py`, `web/auth.py`,
  `web/health.py`, `web/routes_projects.py`, `web/static.py`
- Create: `backend/src/copperchat/kicad_session/__init__.py`, `kicad_session/errors.py`,
  `kicad_session/launcher.py`, `kicad_session/session.py`
- Create: `backend/src/copperchat/tools/__init__.py`, `tools/spec.py`, `tools/registry.py`
- Create: `backend/tests/__init__.py`, `backend/tests/conftest.py`,
  `backend/tests/support/__init__.py`, `backend/tests/support/fake_kicad.py`,
  `backend/tests/support/kicad_env.py`,
  `backend/tests/unit/__init__.py`, `backend/tests/unit/test_*.py` (listed per task),
  `backend/tests/kicad/__init__.py`, `backend/tests/kicad/test_session_real.py`
- Create: `.github/workflows/ci.yml`
- Modify: `.gitignore` (Python/uv entries)

**Acceptance criteria:**
- `cd backend && uv sync && uv run pytest -m "not kicad"` passes; ruff, ruff format
  check and `mypy src` (strict) are clean.
- Starting without `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` (and without
  `DEV_FAKE_AGENT=true`) fails at startup with a message containing `claude setup-token`.
- First start with `ADMIN_PASSWORD` seeds an argon2 hash into `users`; first start
  without it fails with a clear error. The session secret is generated into
  `/data/session_secret` (mode 0600) when not configured and reused afterwards.
- Login: cookie is `HttpOnly`, `SameSite=Lax`, `Secure` unless `COOKIE_SECURE=false`;
  after 5 failed logins within 60 s from one IP further attempts get 429.
- `GET /api/projects` lists projects found by `*.kicad_pro` at any depth, skipping
  hidden dirs, `.kicad-agent` and symlinked directories; project ids that are not
  12 lowercase hex chars return 400; unknown ids return 404 (Review Focus 5).
- `POST /api/projects/{id}/open` returns `{id, readOnly, lockFiles, kicadVersion}`;
  a KiCad lock file makes `readOnly: true` and `save()` refuses (Review Focus 3).
- A hung or crashed api-server makes `KicadSession.call` restart the server and raise
  `KicadUnavailable`/`KicadCallTimeout`; the next call succeeds (Review Focus 2).
- `tools.registry.all_specs()` returns `[]` and raises when a provider import fails.
- `GET /api/health` → `{"status":"ok","kicad":<version|null>}` without auth.

**Verification commands:**
```bash
cd backend && uv sync && uv run pytest -m "not kicad" && uv run ruff check . && uv run ruff format --check . && uv run mypy src
cd backend && uv run pytest -m kicad tests/kicad/test_session_real.py   # only where kicad-cli has api-server
```

**CI expectations:** workflow `CI`, job `backend` green on the PR. No `kicad` job
yet (added by Phase 4).

---

## Task 3.1: Package skeleton, tooling, errors, configuration, CI

**Files:**
- Create: `backend/pyproject.toml`, `backend/.python-version`, `backend/README.md`, `backend/uv.lock` (generated)
- Create: `backend/src/copperchat/__init__.py`, `backend/src/copperchat/errors.py`, `backend/src/copperchat/config.py`
- Create: `backend/tests/__init__.py`, `backend/tests/unit/__init__.py`, `backend/tests/kicad/__init__.py`, `backend/tests/support/__init__.py`, `backend/tests/conftest.py`
- Create: `.github/workflows/ci.yml`
- Modify: `.gitignore`
- Test: `backend/tests/unit/test_config.py`, `backend/tests/unit/test_kipy_imports.py`

**Interfaces:**
- Consumes: nothing.
- Produces (C1 + extensions):
  - `copperchat.config.Settings` with all C1 fields plus **new** fields
    `cookie_secure: bool = True`, `login_max_failures: int = 5`,
    `login_window_s: float = 60.0`, `frontend_dist: Path = Path("/app/frontend/dist")`.
  - `get_settings() -> Settings`, `check_model_credentials(s: Settings) -> None`.
  - `copperchat.errors`: `CopperChatError(message)` with class attrs `code: str`,
    `http_status: int`; `ConfigError`, `MissingCredentialsError`, `ProjectNotFound`,
    `InvalidProjectId`, `ReadOnlyProject`, `DocumentNotOpen`,
    `StructuredToolError(what, why, suggestion)` with `.to_dict()`.

- [ ] **Step 1: Write the failing test**

`backend/tests/conftest.py`:
```python
from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from copperchat.config import Settings

_CREDENTIAL_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ADMIN_PASSWORD",
                   "SESSION_SECRET", "DEV_FAKE_AGENT")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never let the developer's real credentials leak into tests."""
    for name in _CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def _make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "projects_root": tmp_path / "projects",
            "data_dir": tmp_path / "data",
            "admin_password": "test-password",
            "claude_code_oauth_token": "test-token",
            "cookie_secure": False,
            "frontend_dist": tmp_path / "no-frontend",
            "kicad_cli": "kicad-cli-not-installed",
            "kicad_api_timeout_s": 2.0,
            "kicad_start_timeout_s": 3.0,
        }
        values.update(overrides)
        (values["projects_root"]).mkdir(parents=True, exist_ok=True)
        return Settings(_env_file=None, **values)  # type: ignore[call-arg]

    return _make
```

`backend/tests/unit/test_config.py`:
```python
from collections.abc import Callable
from pathlib import Path

import pytest

from copperchat.config import Settings, check_model_credentials
from copperchat.errors import MissingCredentialsError, StructuredToolError


def test_defaults_match_contract(tmp_path: Path) -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.projects_root == Path("/projects")
    assert s.data_dir == Path("/data")
    assert s.admin_username == "admin"
    assert s.kicad_cli == "kicad-cli"
    assert s.kicad_api_timeout_s == 30.0
    assert s.kicad_start_timeout_s == 60.0
    assert (s.host, s.port) == ("0.0.0.0", 8080)
    assert s.cookie_secure is True
    assert s.dev_fake_agent is False


def test_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-xyz")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.claude_code_oauth_token is not None
    assert s.claude_code_oauth_token.get_secret_value() == "sk-ant-oat-xyz"
    assert s.cookie_secure is False


def test_missing_credentials_points_to_setup_token(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(claude_code_oauth_token=None, anthropic_api_key=None)
    with pytest.raises(MissingCredentialsError) as info:
        check_model_credentials(s)
    assert "claude setup-token" in str(info.value)


def test_blank_token_counts_as_missing(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(claude_code_oauth_token="   ")
    with pytest.raises(MissingCredentialsError):
        check_model_credentials(s)


@pytest.mark.parametrize("field", ["claude_code_oauth_token", "anthropic_api_key"])
def test_either_credential_is_enough(make_settings: Callable[..., Settings], field: str) -> None:
    s = make_settings(claude_code_oauth_token=None, **{field: "secret"})
    check_model_credentials(s)


def test_fake_agent_skips_check(make_settings: Callable[..., Settings]) -> None:
    check_model_credentials(make_settings(claude_code_oauth_token=None, dev_fake_agent=True))


def test_structured_tool_error_dict() -> None:
    err = StructuredToolError("Symbol R99 not found", "No symbol has that reference",
                              "Did you mean: R1, R9?")
    assert err.to_dict() == {"what": "Symbol R99 not found",
                             "why": "No symbol has that reference",
                             "suggestion": "Did you mean: R1, R9?"}
```

`backend/tests/unit/test_kipy_imports.py`:
```python
def test_kipy_modules_we_depend_on_import() -> None:
    # kicad-python 0.8.0 from PyPI fails here (kipy.schematic imports a missing
    # PageSettings; board_jobs_pb2 references an undefined kiapi.common.types.Units).
    # The pinned git revision in pyproject.toml must pass.
    import kipy
    import kipy.board
    import kipy.board_jobs
    import kipy.schematic

    assert hasattr(kipy, "KiCad")
    assert hasattr(kipy.schematic, "Schematic")
    assert hasattr(kipy.board, "Board")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit -q`
Expected: FAIL — `pyproject.toml` missing / `ModuleNotFoundError: No module named 'copperchat'`.

- [ ] **Step 3: Write minimal implementation**

`backend/.python-version`:
```
3.13
```

`backend/pyproject.toml`:
```toml
[project]
name = "copperchat"
version = "0.1.0"
description = "CopperChat - chat with an AI agent about your KiCad project"
readme = "README.md"
license = "GPL-3.0-only"
requires-python = ">=3.13"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "aiosqlite>=0.20",
    "argon2-cffi>=23.1",
    "itsdangerous>=2.2",
    "kicad-python>=0.8.0",
]

[dependency-groups]
dev = [
    "httpx>=0.27",
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "ruff>=0.8",
    "mypy>=1.13",
]

[project.scripts]
copperchat = "copperchat.__main__:main"

[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/copperchat"]

# kicad-python 0.8.0 on PyPI is broken for KiCad 11 (see tests/unit/test_kipy_imports.py).
# Use the revision recorded in docs/spikes/1c-api-server.md ("kipy revision").
[tool.uv.sources]
kicad-python = { git = "https://gitlab.com/kicad/code/kicad-python.git", rev = "<rev from docs/spikes/1c-api-server.md>" }

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
addopts = "-ra --strict-markers"
markers = [
    "kicad: needs a real `kicad-cli api-server` (KiCad 11 master); run with -m kicad",
]

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "RUF"]

[tool.mypy]
python_version = "3.13"
strict = true
mypy_path = "src"
```

The `rev` value is copied literally from the spike doc's "kipy revision"
line. If the spike doc does not record one, use the newest commit of
`https://gitlab.com/kicad/code/kicad-python` `main` for which
`tests/unit/test_kipy_imports.py` passes, and write that SHA into
`backend/README.md` under "Dependencies" in the same commit. If a released
PyPI version newer than 0.8.0 passes the test, delete the `[tool.uv.sources]`
block and raise the lower bound to that version instead.

`backend/src/copperchat/__init__.py`:
```python
"""CopperChat backend."""

__version__ = "0.1.0"
```

`backend/src/copperchat/errors.py`:
```python
from __future__ import annotations


class CopperChatError(Exception):
    """Base class for all expected CopperChat errors (mapped to HTTP by web.app)."""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str = "") -> None:
        self.message = message or type(self).__name__
        super().__init__(self.message)


class ConfigError(CopperChatError):
    code = "config_error"


class MissingCredentialsError(ConfigError):
    code = "missing_credentials"


class ProjectNotFound(CopperChatError):
    code = "project_not_found"
    http_status = 404


class InvalidProjectId(CopperChatError):
    code = "invalid_project_id"
    http_status = 400


class ReadOnlyProject(CopperChatError):
    code = "read_only"
    http_status = 409


class DocumentNotOpen(CopperChatError):
    code = "document_not_open"
    http_status = 409


class StructuredToolError(CopperChatError):
    """A tool failure the agent can act on: what failed, why, and what to do instead."""

    code = "tool_error"
    http_status = 422

    def __init__(self, what: str, why: str, suggestion: str) -> None:
        self.what = what
        self.why = why
        self.suggestion = suggestion
        super().__init__(f"{what}: {why}")

    def to_dict(self) -> dict[str, str]:
        return {"what": self.what, "why": self.why, "suggestion": self.suggestion}
```

`backend/src/copperchat/config.py`:
```python
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from copperchat.errors import MissingCredentialsError

MISSING_CREDENTIALS_MESSAGE = (
    "No model credentials configured. Run `claude setup-token` on a machine where the "
    "Claude CLI is logged in to your own subscription and put the printed token into "
    "CLAUDE_CODE_OAUTH_TOKEN in your .env (or set ANTHROPIC_API_KEY). CopperChat has no "
    "login flow of its own; Anthropic's terms apply to your credentials."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore")

    projects_root: Path = Path("/projects")
    data_dir: Path = Path("/data")
    admin_username: str = "admin"
    admin_password: SecretStr | None = None
    session_secret: SecretStr | None = None
    claude_code_oauth_token: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    kicad_cli: str = "kicad-cli"
    kicad_api_timeout_s: float = 30.0
    kicad_start_timeout_s: float = 60.0
    host: str = "0.0.0.0"
    port: int = 8080
    public_base_url: str | None = None
    dev_fake_agent: bool = False
    # Extensions (Phase 3)
    cookie_secure: bool = True
    login_max_failures: int = 5
    login_window_s: float = 60.0
    frontend_dist: Path = Path("/app/frontend/dist")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def _present(secret: SecretStr | None) -> bool:
    return secret is not None and secret.get_secret_value().strip() != ""


def check_model_credentials(s: Settings) -> None:
    """Fail fast at startup when no model credential is configured.

    Expired tokens can only be detected on the first model call; Phase 5 maps that
    runtime error to the same message.
    """
    if s.dev_fake_agent:
        return
    if _present(s.claude_code_oauth_token) or _present(s.anthropic_api_key):
        return
    raise MissingCredentialsError(MISSING_CREDENTIALS_MESSAGE)
```

Empty package markers: `backend/tests/__init__.py`, `backend/tests/unit/__init__.py`,
`backend/tests/kicad/__init__.py`, `backend/tests/support/__init__.py` (each an empty file).

`backend/README.md`:
````markdown
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
`PUBLIC_BASE_URL`, `DEV_FAKE_AGENT`, `COOKIE_SECURE`, `FRONTEND_DIST`.

## Dependencies

`kicad-python` is pinned to a git revision (see `pyproject.toml`) because the
0.8.0 wheel on PyPI cannot import `kipy.schematic` / `kipy.board_jobs`.
````

`.gitignore` additions:
```
# Python / uv
__pycache__/
*.pyc
.venv/
.mypy_cache/
.ruff_cache/
.pytest_cache/
```

`.github/workflows/ci.yml`:
```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  backend:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: backend
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true
          cache-dependency-glob: backend/uv.lock
      - run: uv python install 3.13
      - run: uv sync --locked
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run mypy src
      - run: uv run pytest -m "not kicad"
```

Then generate the lock file: `cd backend && uv lock`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv sync && uv run pytest tests/unit -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: all tests PASS (9 passed), ruff/mypy report no issues.

- [ ] **Step 5: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/.python-version backend/README.md \
  backend/src/copperchat/__init__.py backend/src/copperchat/errors.py backend/src/copperchat/config.py \
  backend/tests .github/workflows/ci.yml .gitignore
git commit -m "feat: backend skeleton with settings, errors and CI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 3.2: Database and migration runner (C2)

**Files:**
- Create: `backend/src/copperchat/db/__init__.py`, `backend/src/copperchat/db/migrations/0001_core.sql`
- Test: `backend/tests/unit/test_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces: C2 `Database` exactly as in the index, plus `migrate(db: Database,
  directory: Path = MIGRATIONS_DIR) -> None` and `list_migrations(directory) ->
  list[tuple[int, Path]]`. Applied migrations are additionally recorded in an
  internal table `schema_migrations(version INTEGER PRIMARY KEY, name TEXT,
  applied_at TEXT)` so that a migration merged out of numeric order (e.g. 0003
  after 0004) is still applied; `PRAGMA user_version` holds the highest applied
  number as the contract requires. Tables `users`, `projects`.

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/test_db.py`:
```python
import asyncio
from pathlib import Path

import pytest

from copperchat.db import Database, list_migrations, migrate


async def test_open_applies_core_migration(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "x.db")
    try:
        tables = {r["name"] for r in await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"users", "projects", "schema_migrations"} <= tables
        row = await db.fetchone("PRAGMA user_version")
        assert row is not None and row[0] >= 1
        fk = await db.fetchone("PRAGMA foreign_keys")
        assert fk is not None and fk[0] == 1
        mode = await db.fetchone("PRAGMA journal_mode")
        assert mode is not None and mode[0] == "wal"
    finally:
        await db.close()


async def test_reopen_is_idempotent(tmp_path: Path) -> None:
    for _ in range(2):
        db = await Database.open(tmp_path / "x.db")
        await db.close()


async def test_execute_returns_lastrowid_and_fetch(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "x.db")
    try:
        rid = await db.execute(
            "INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?)",
            ("a", "h", "2026-10-15T00:00:00Z"))
        assert rid == 1
        row = await db.fetchone("SELECT username FROM users WHERE id = :id", {"id": rid})
        assert row is not None and row["username"] == "a"
    finally:
        await db.close()


async def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "x.db")
    try:
        with pytest.raises(RuntimeError):
            async with db.transaction():
                await db.execute("INSERT INTO projects(id, rel_path, name) VALUES ('p','a.kicad_pro','a')")
                raise RuntimeError("boom")
        assert await db.fetchall("SELECT * FROM projects") == []
    finally:
        await db.close()


async def test_transactions_are_serialised(tmp_path: Path) -> None:
    db = await Database.open(tmp_path / "x.db")
    try:
        async def writer(i: int) -> None:
            async with db.transaction():
                await db.execute("INSERT INTO projects(id, rel_path, name) VALUES (?,?,?)",
                                 (f"p{i}", f"{i}.kicad_pro", str(i)))
                await asyncio.sleep(0)
        await asyncio.gather(*(writer(i) for i in range(10)))
        rows = await db.fetchall("SELECT id FROM projects")
        assert len(rows) == 10
    finally:
        await db.close()


async def test_out_of_order_migration_is_still_applied(tmp_path: Path) -> None:
    mig = tmp_path / "migs"
    mig.mkdir()
    (mig / "0001_a.sql").write_text("CREATE TABLE a (x INTEGER);")
    (mig / "0004_d.sql").write_text("CREATE TABLE d (x INTEGER);")
    db = await Database.open(tmp_path / "x.db")
    try:
        await migrate(db, mig)
        (mig / "0003_c.sql").write_text("CREATE TABLE c (x INTEGER);")
        await migrate(db, mig)
        names = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master")}
        # 0001 is already applied by Database.open (real 0001_core.sql), so the
        # test's 0001_a.sql is skipped as "version 1 already applied".
        assert {"c", "d"} <= names and "a" not in names
        uv = await db.fetchone("PRAGMA user_version")
        assert uv is not None and uv[0] == 4
    finally:
        await db.close()


def test_list_migrations_rejects_duplicates(tmp_path: Path) -> None:
    (tmp_path / "0002_x.sql").write_text("")
    (tmp_path / "0002_y.sql").write_text("")
    with pytest.raises(RuntimeError, match="duplicate"):
        list_migrations(tmp_path)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_db.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'copperchat.db'`.

- [ ] **Step 3: Write minimal implementation**

`backend/src/copperchat/db/migrations/0001_core.sql`:
```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    rel_path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    last_opened_at TEXT
);
```

`backend/src/copperchat/db/__init__.py`:
```python
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

Params = Sequence[Any] | Mapping[str, Any]


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Database:
    """Single aiosqlite connection in autocommit mode; explicit transactions are serialised."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        self._tx_task: asyncio.Task[Any] | None = None

    @classmethod
    async def open(cls, path: Path) -> Database:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        db = cls(conn)
        await migrate(db)
        return db

    def _in_own_transaction(self) -> bool:
        return self._tx_task is not None and self._tx_task is asyncio.current_task()

    async def execute(self, sql: str, params: Params = ()) -> int:
        if self._in_own_transaction():
            cur = await self._conn.execute(sql, params)
            return cur.lastrowid or 0
        async with self._lock:
            cur = await self._conn.execute(sql, params)
            return cur.lastrowid or 0

    async def executescript(self, script: str) -> None:
        async with self._lock:
            await self._conn.executescript(script)

    async def fetchone(self, sql: str, params: Params = ()) -> aiosqlite.Row | None:
        async with self._conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Params = ()) -> list[aiosqlite.Row]:
        async with self._conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    def transaction(self) -> AbstractAsyncContextManager[None]:
        return self._transaction()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        if self._in_own_transaction():
            raise RuntimeError("nested transactions are not supported")
        async with self._lock:
            self._tx_task = asyncio.current_task()
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
            else:
                await self._conn.execute("COMMIT")
            finally:
                self._tx_task = None

    async def close(self) -> None:
        await self._conn.close()


def list_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    found: dict[int, Path] = {}
    for path in sorted(directory.iterdir()):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            continue
        number = int(match.group(1))
        if number in found:
            raise RuntimeError(f"duplicate migration number {number:04d}: "
                               f"{found[number].name}, {path.name}")
        found[number] = path
    return sorted(found.items())


async def migrate(db: Database, directory: Path = MIGRATIONS_DIR) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    applied = {r["version"] for r in await db.fetchall("SELECT version FROM schema_migrations")}
    for number, path in list_migrations(directory):
        if number in applied:
            continue
        name = path.name.replace("'", "''")
        highest = max([number, *applied])
        await db.executescript(
            "BEGIN;\n"
            f"{path.read_text(encoding='utf-8')}\n"
            f"INSERT INTO schema_migrations(version, name, applied_at) "
            f"VALUES ({number}, '{name}', '{utcnow_iso()}');\n"
            f"PRAGMA user_version = {highest};\n"
            "COMMIT;")
        applied.add(number)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_db.py -q && uv run mypy src`
Expected: 7 passed; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/db backend/tests/unit/test_db.py
git commit -m "feat: sqlite database wrapper with migration runner

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 3.3: App factory, login, health, static stub, entry point

**Files:**
- Create: `backend/src/copperchat/web/__init__.py` (empty), `web/app.py`, `web/auth.py`, `web/health.py`, `web/static.py`, `backend/src/copperchat/__main__.py`
- Test: `backend/tests/unit/test_auth.py`, `backend/tests/unit/test_app.py`

**Interfaces:**
- Consumes: C1 `Settings`, `check_model_credentials`; C2 `Database`.
- Produces:
  - `copperchat.web.app.create_app(settings: Settings) -> FastAPI` (Task 3.5 adds
    keyword args `launcher`, `client_factory`). Lifespan stores `app.state.settings`,
    `app.state.db`, `app.state.serializer`, `app.state.login_limiter`.
  - `copperchat.web.auth`: `router`, `User(id: int, username: str)`,
    `async def require_user(request: Request) -> User` (FastAPI dependency),
    `async def user_from_cookie(state: Any, cookie: str | None) -> User | None`
    (for the Phase 5 WebSocket), `COOKIE_NAME = "copperchat_session"`,
    `LoginRateLimiter`, `ensure_admin`, `load_or_create_session_secret`.
  - `copperchat.web.health.router` (`GET /api/health`), `kicad_cli_version(cli: str) -> str | None`.
  - `copperchat.web.static.mount_frontend(app: FastAPI, dist: Path) -> None`.
  - C8: `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`, `GET /api/health`.

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/test_auth.py`:
```python
import stat
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.errors import ConfigError
from copperchat.web.app import create_app
from copperchat.web.auth import COOKIE_NAME, LoginRateLimiter

GOOD = {"username": "admin", "password": "test-password"}


def test_login_sets_hardened_cookie(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings(cookie_secure=True))) as client:
        r = client.post("/api/auth/login", json=GOOD)
        assert r.status_code == 204
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=")
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie or "SameSite=Lax" in cookie
        assert "Secure" in cookie


def test_cookie_secure_can_be_disabled(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings(cookie_secure=False))) as client:
        r = client.post("/api/auth/login", json=GOOD)
        assert "Secure" not in r.headers["set-cookie"]


def test_me_requires_login(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/api/auth/me").status_code == 401
        client.post("/api/auth/login", json=GOOD)
        r = client.get("/api/auth/me")
        assert r.status_code == 200 and r.json() == {"username": "admin"}
        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/auth/me").status_code == 401


def test_wrong_password_and_unknown_user_look_identical(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        a = client.post("/api/auth/login", json={"username": "admin", "password": "x"})
        b = client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
        assert a.status_code == b.status_code == 401
        assert a.json() == b.json()


def test_rate_limit_after_five_failures(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        for _ in range(5):
            assert client.post("/api/auth/login",
                               json={"username": "admin", "password": "bad"}).status_code == 401
        r = client.post("/api/auth/login", json=GOOD)
        assert r.status_code == 429
        assert int(r.headers["retry-after"]) >= 1


def test_rate_limiter_window_expires() -> None:
    now = [0.0]
    limiter = LoginRateLimiter(max_failures=2, window_s=60, clock=lambda: now[0])
    limiter.record_failure("1.2.3.4")
    limiter.record_failure("1.2.3.4")
    assert limiter.retry_after("1.2.3.4") is not None
    assert limiter.retry_after("5.6.7.8") is None
    now[0] = 61.0
    assert limiter.retry_after("1.2.3.4") is None


def test_admin_seeded_once_and_password_not_reseeded(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    with TestClient(create_app(settings)):
        pass
    changed = settings.model_copy(update={"admin_password": None})
    with TestClient(create_app(changed)) as client:
        assert client.post("/api/auth/login", json=GOOD).status_code == 204


def test_first_start_without_admin_password_fails(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ConfigError, match="ADMIN_PASSWORD"), \
            TestClient(create_app(make_settings(admin_password=None))):
        pass


def test_session_secret_generated_0600_and_reused(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    with TestClient(create_app(settings)) as client:
        client.post("/api/auth/login", json=GOOD)
        cookie = client.cookies.get(COOKIE_NAME)
    secret_file = settings.data_dir / "session_secret"
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    with TestClient(create_app(settings)) as client2:
        client2.cookies.set(COOKIE_NAME, cookie or "")
        assert client2.get("/api/auth/me").status_code == 200


def test_tampered_cookie_rejected(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        client.cookies.set(COOKIE_NAME, "eyJ1aWQiOjF9.forged.sig")
        assert client.get("/api/auth/me").status_code == 401
```

`backend/tests/unit/test_app.py`:
```python
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.errors import MissingCredentialsError
from copperchat.web.app import create_app


def test_startup_fails_without_credentials(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(MissingCredentialsError, match="claude setup-token"), \
            TestClient(create_app(make_settings(claude_code_oauth_token=None))):
        pass


def test_health_without_kicad(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "kicad": None}


def test_health_reports_kicad_version(make_settings: Callable[..., Settings], tmp_path: Path) -> None:
    fake_cli = tmp_path / "fake-kicad-cli"
    fake_cli.write_text("#!/bin/sh\necho 10.99.0-test\n")
    fake_cli.chmod(0o755)
    with TestClient(create_app(make_settings(kicad_cli=str(fake_cli)))) as client:
        assert client.get("/api/health").json()["kicad"] == "10.99.0-test"


def test_frontend_served_when_dist_exists(make_settings: Callable[..., Settings], tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>CopperChat</title>")
    with TestClient(create_app(make_settings(frontend_dist=dist))) as client:
        assert "CopperChat" in client.get("/").text
        assert client.get("/api/health").status_code == 200


def test_no_frontend_returns_404_on_root(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/").status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_auth.py tests/unit/test_app.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'copperchat.web'`.

- [ ] **Step 3: Write minimal implementation**

`backend/src/copperchat/web/auth.py`:
```python
from __future__ import annotations

import math
import os
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel

from copperchat.config import Settings
from copperchat.db import Database, utcnow_iso
from copperchat.errors import ConfigError

COOKIE_NAME = "copperchat_session"
SESSION_MAX_AGE_S = 30 * 24 * 3600
_SALT = "copperchat-session"
_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("copperchat-timing-equaliser")

router = APIRouter(prefix="/api/auth", tags=["auth"])


@dataclass(frozen=True)
class User:
    id: int
    username: str


class LoginBody(BaseModel):
    username: str
    password: str


class LoginRateLimiter:
    """Counts failed logins per client key in a sliding window."""

    def __init__(self, max_failures: int, window_s: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._max = max_failures
        self._window = window_s
        self._clock = clock
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> deque[float]:
        q = self._failures[key]
        cutoff = self._clock() - self._window
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def retry_after(self, key: str) -> float | None:
        q = self._prune(key)
        if len(q) < self._max:
            return None
        return max(1.0, q[0] + self._window - self._clock())

    def record_failure(self, key: str) -> None:
        self._prune(key).append(self._clock())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


def load_or_create_session_secret(settings: Settings) -> str:
    if settings.session_secret is not None and settings.session_secret.get_secret_value():
        return settings.session_secret.get_secret_value()
    path: Path = settings.data_dir / "session_secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secret)
    return secret


def make_serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=_SALT)


async def ensure_admin(db: Database, settings: Settings) -> None:
    row = await db.fetchone("SELECT id FROM users WHERE username = ?", (settings.admin_username,))
    if row is not None:
        return
    password = settings.admin_password.get_secret_value() if settings.admin_password else ""
    if not password:
        raise ConfigError(
            "ADMIN_PASSWORD must be set on first start; it seeds the login for "
            f"user '{settings.admin_username}' and is stored only as an argon2 hash.")
    await db.execute("INSERT INTO users(username, password_hash, created_at) VALUES (?,?,?)",
                     (settings.admin_username, _hasher.hash(password), utcnow_iso()))


async def user_from_cookie(state: Any, cookie: str | None) -> User | None:
    if not cookie:
        return None
    try:
        payload = state.serializer.loads(cookie, max_age=SESSION_MAX_AGE_S)
    except BadSignature:
        return None
    uid = payload.get("uid") if isinstance(payload, dict) else None
    if not isinstance(uid, int):
        return None
    row = await state.db.fetchone("SELECT id, username FROM users WHERE id = ?", (uid,))
    return User(id=row["id"], username=row["username"]) if row else None


async def require_user(request: Request) -> User:
    user = await user_from_cookie(request.app.state, request.cookies.get(COOKIE_NAME))
    if user is None:
        raise HTTPException(status_code=401, detail="not logged in")
    return user


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/login", status_code=204)
async def login(body: LoginBody, request: Request, response: Response) -> None:
    state = request.app.state
    key = _client_key(request)
    wait = state.login_limiter.retry_after(key)
    if wait is not None:
        raise HTTPException(status_code=429, detail="too many failed logins",
                            headers={"Retry-After": str(math.ceil(wait))})
    row = await state.db.fetchone("SELECT id, password_hash FROM users WHERE username = ?",
                                  (body.username,))
    try:
        _hasher.verify(row["password_hash"] if row else _DUMMY_HASH, body.password)
        ok = row is not None
    except (VerificationError, InvalidHashError):
        ok = False
    if not ok or row is None:
        state.login_limiter.record_failure(key)
        raise HTTPException(status_code=401, detail="invalid username or password")
    state.login_limiter.reset(key)
    settings: Settings = state.settings
    response.set_cookie(COOKIE_NAME, state.serializer.dumps({"uid": row["id"]}),
                        max_age=SESSION_MAX_AGE_S, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/")


@router.post("/logout", status_code=204)
async def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/me")
async def me(request: Request) -> dict[str, str]:
    user = await require_user(request)
    return {"username": user.username}
```

`backend/src/copperchat/web/health.py`:
```python
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api", tags=["health"])


async def kicad_cli_version(cli: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, "version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except (FileNotFoundError, PermissionError):
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except TimeoutError:
        proc.kill()
        return None
    text = out.decode().strip()
    return text if proc.returncode == 0 and text else None


@router.get("/health")
async def health(request: Request) -> dict[str, str | None]:
    state = request.app.state
    if not hasattr(state, "kicad_version"):
        state.kicad_version = await kicad_cli_version(state.settings.kicad_cli)
    return {"status": "ok", "kicad": state.kicad_version}
```

`backend/src/copperchat/web/static.py`:
```python
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


def mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built PWA at / when present. Phase 9 adds SPA fallback + cache headers."""
    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
```

`backend/src/copperchat/web/app.py`:
```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from copperchat.config import Settings, check_model_credentials
from copperchat.db import Database
from copperchat.errors import CopperChatError, StructuredToolError
from copperchat.web import auth, health
from copperchat.web.static import mount_frontend


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(CopperChatError)
    async def _handle(_: Request, exc: CopperChatError) -> JSONResponse:
        body: dict[str, object] = {"code": exc.code, "message": exc.message}
        if isinstance(exc, StructuredToolError):
            body["details"] = exc.to_dict()
        return JSONResponse(status_code=exc.http_status, content={"error": body})


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        check_model_credentials(settings)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = await Database.open(settings.data_dir / "copperchat.db")
        try:
            await auth.ensure_admin(db, settings)
            app.state.settings = settings
            app.state.db = db
            app.state.serializer = auth.make_serializer(
                auth.load_or_create_session_secret(settings))
            app.state.login_limiter = auth.LoginRateLimiter(
                settings.login_max_failures, settings.login_window_s)
            yield
        finally:
            await db.close()

    app = FastAPI(title="CopperChat", lifespan=lifespan)
    _install_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(health.router)
    mount_frontend(app, settings.frontend_dist)  # must stay last: it mounts "/"
    return app
```

`backend/src/copperchat/__main__.py`:
```python
from __future__ import annotations

import sys

import uvicorn

from copperchat.config import check_model_credentials, get_settings
from copperchat.errors import ConfigError
from copperchat.web.app import create_app


def main() -> None:
    settings = get_settings()
    try:
        check_model_credentials(settings)
    except ConfigError as exc:
        print(f"CopperChat cannot start: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit -q && uv run mypy src && uv run ruff check .`
Expected: all PASS; mypy and ruff clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/web backend/src/copperchat/__main__.py \
  backend/tests/unit/test_auth.py backend/tests/unit/test_app.py
git commit -m "feat: app factory with login, rate limiting, health and static stub

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 3.4: Project discovery, ids, lock detection, path-traversal safety (C3)

**Files:**
- Create: `backend/src/copperchat/projects.py`, `backend/src/copperchat/db/repos/__init__.py` (empty), `backend/src/copperchat/db/repos/projects.py`, `backend/src/copperchat/web/routes_projects.py`
- Modify: `backend/src/copperchat/web/app.py` (include `routes_projects.router`)
- Test: `backend/tests/unit/test_projects.py`, `backend/tests/unit/test_routes_projects.py`

**Interfaces:**
- Consumes: C1, C2, `require_user`.
- Produces: C3 exactly (`Project`, `LockStatus`, `discover_projects`, `get_project`,
  `lock_status`, `agent_dir`) plus `project_id_for(rel_path: str) -> str` and
  `PROJECT_ID_RE`. `db.repos.projects.upsert_projects(db, projects)`,
  `touch_opened(db, project_id)`. C8 `GET /api/projects`.

Test note: discovery tests create **zero-byte marker files** named `*.kicad_pro`
etc. They are never opened by KiCad or parsed by anything — only their names
matter to discovery. This is not editing KiCad content; real KiCad files enter
the repo only in Phase 4 via `kicad-cli`. **Decided by the owner
(2026-10-16):** empty `*.kicad_pro` (and `.kicad_sch`/`.kicad_pcb`/`.lck`)
markers in `tmp_path` are allowed in unit tests (Tasks 3.4, 3.5, 3.7); any test
that reads KiCad content uses only KiCad/kicad-cli-generated fixtures.

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/test_projects.py`:
```python
import hashlib
from pathlib import Path

import pytest

from copperchat.errors import InvalidProjectId, ProjectNotFound
from copperchat.projects import (agent_dir, discover_projects, get_project, lock_status,
                                 project_id_for)


def marker(root: Path, rel: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def test_discovers_at_any_depth_with_files(tmp_path: Path) -> None:
    marker(tmp_path, "a/b/c/amp.kicad_pro")
    marker(tmp_path, "a/b/c/amp.kicad_sch")
    marker(tmp_path, "top.kicad_pro")
    marker(tmp_path, "top.kicad_pcb")
    projects = discover_projects(tmp_path)
    assert [p.rel_path for p in projects] == ["a/b/c/amp.kicad_pro", "top.kicad_pro"]
    amp, top = projects
    assert amp.name == "amp" and amp.sch_file is not None and amp.pcb_file is None
    assert top.pcb_file is not None and top.sch_file is None
    assert amp.dir == (tmp_path / "a/b/c").resolve()
    assert amp.id == hashlib.sha1(b"a/b/c/amp.kicad_pro").hexdigest()[:12]


def test_skips_hidden_agent_and_symlinked_dirs(tmp_path: Path) -> None:
    root = tmp_path / "root"
    marker(root, ".git/x.kicad_pro")
    marker(root, "p/.kicad-agent/view/p.kicad_pro")
    marker(root, ".hidden/y.kicad_pro")
    outside = tmp_path / "outside"
    marker(outside, "evil.kicad_pro")
    (root / "link").symlink_to(outside, target_is_directory=True)
    assert discover_projects(root) == []


def test_id_is_stable_posix_hash() -> None:
    assert project_id_for("x/y.kicad_pro") == hashlib.sha1(b"x/y.kicad_pro").hexdigest()[:12]


@pytest.mark.parametrize("bad", ["", "../../etc/passwd", "..", "ABCDEF123456", "abc",
                                 "a1b2c3d4e5f6/..", "a1b2c3d4e5f6a", "a1b2c3d4e5f%",
                                 "a1b2c3d4e5f6\x00", "/etc/passwd"])
def test_invalid_ids_rejected_before_touching_fs(tmp_path: Path, bad: str) -> None:
    with pytest.raises(InvalidProjectId):
        get_project(tmp_path, bad)


def test_unknown_id(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFound):
        get_project(tmp_path, "0123456789ab")


def test_lock_status(tmp_path: Path) -> None:
    marker(tmp_path, "p/demo.kicad_pro")
    marker(tmp_path, "p/demo.kicad_sch")
    marker(tmp_path, "p/demo.kicad_pcb")
    project = discover_projects(tmp_path)[0]
    assert lock_status(project).locked is False
    lock = marker(tmp_path, "p/~demo.kicad_sch.lck")
    status = lock_status(project)
    assert status.locked is True
    assert status.lock_files == (lock.resolve(),)
    assert agent_dir(project) == project.dir / ".kicad-agent"
```

`backend/tests/unit/test_routes_projects.py`:
```python
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.web.app import create_app

GOOD = {"username": "admin", "password": "test-password"}


def marker(root: Path, rel: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).touch()


def test_list_requires_login(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/api/projects").status_code == 401


def test_list_projects_camel_case(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    marker(settings.projects_root, "boards/amp/amp.kicad_pro")
    marker(settings.projects_root, "boards/amp/amp.kicad_sch")
    marker(settings.projects_root, "boards/amp/~amp.kicad_sch.lck")
    with TestClient(create_app(settings)) as client:
        client.post("/api/auth/login", json=GOOD)
        body = client.get("/api/projects").json()
    assert len(body) == 1
    item = body[0]
    assert set(item) == {"id", "name", "relPath", "hasSch", "hasPcb", "locked"}
    assert item["relPath"] == "boards/amp/amp.kicad_pro"
    assert (item["hasSch"], item["hasPcb"], item["locked"]) == (True, False, True)


@pytest.mark.parametrize("bad_id", ["..%2F..%2Fetc", "%2e%2e", "ABCDEF123456", "x" * 12,
                                    "a1b2c3d4e5f6%00"])
def test_open_rejects_traversal_ids(make_settings: Callable[..., Settings], bad_id: str) -> None:
    with TestClient(create_app(make_settings())) as client:
        client.post("/api/auth/login", json=GOOD)
        r = client.post(f"/api/projects/{bad_id}/open")
        assert r.status_code in (400, 404)
        # Ids with percent-encoding may be decoded/normalised by the router before
        # reaching our validator (404 is acceptable there); plain ids must be 400.
        if "%" not in bad_id:
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "invalid_project_id"


def test_open_unknown_id_404(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        client.post("/api/auth/login", json=GOOD)
        r = client.post("/api/projects/0123456789ab/open")
        assert r.status_code == 404
```

(`POST /open` is wired in Task 3.5; in this task the route exists and resolves
the project, returning 501 for a valid id until 3.5 replaces the body. The two
tests above only exercise the id validation path.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_projects.py tests/unit/test_routes_projects.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'copperchat.projects'`.

- [ ] **Step 3: Write minimal implementation**

`backend/src/copperchat/projects.py`:
```python
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from copperchat.errors import InvalidProjectId, ProjectNotFound

PROJECT_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_SKIP_DIRS = frozenset({".kicad-agent", "node_modules", "__pycache__"})


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    rel_path: str
    dir: Path
    pro_file: Path
    sch_file: Path | None
    pcb_file: Path | None


@dataclass(frozen=True)
class LockStatus:
    locked: bool
    lock_files: tuple[Path, ...]


def project_id_for(rel_path: str) -> str:
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:12]


def _make_project(root: Path, pro: Path) -> Project:
    rel = pro.relative_to(root).as_posix()
    stem = pro.stem
    sch = pro.with_name(f"{stem}.kicad_sch")
    pcb = pro.with_name(f"{stem}.kicad_pcb")
    return Project(id=project_id_for(rel), name=stem, rel_path=rel, dir=pro.parent,
                   pro_file=pro, sch_file=sch if sch.is_file() else None,
                   pcb_file=pcb if pcb.is_file() else None)


def discover_projects(root: Path) -> list[Project]:
    root = root.resolve()
    found: list[Project] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in _SKIP_DIRS and not (here / d).is_symlink())
        for name in sorted(filenames):
            path = here / name
            if name.endswith(".kicad_pro") and not name.startswith(".") and not path.is_symlink():
                found.append(_make_project(root, path))
    return sorted(found, key=lambda p: p.rel_path)


def get_project(root: Path, project_id: str) -> Project:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise InvalidProjectId(f"invalid project id {project_id!r}")
    resolved_root = root.resolve()
    for project in discover_projects(resolved_root):
        if project.id == project_id:
            if not project.dir.resolve().is_relative_to(resolved_root):
                raise ProjectNotFound(f"project {project_id} is outside the projects root")
            return project
    raise ProjectNotFound(f"no project with id {project_id}")


def lock_status(project: Project) -> LockStatus:
    candidates = [f for f in (project.pro_file, project.sch_file, project.pcb_file) if f]
    locks = tuple(
        lock.resolve() for f in candidates
        if (lock := project.dir / f"~{f.name}.lck").exists())
    return LockStatus(locked=bool(locks), lock_files=locks)


def agent_dir(project: Project) -> Path:
    return project.dir / ".kicad-agent"
```

`backend/src/copperchat/db/repos/projects.py`:
```python
from __future__ import annotations

from collections.abc import Iterable

from copperchat.db import Database, utcnow_iso
from copperchat.projects import Project


async def upsert_projects(db: Database, projects: Iterable[Project]) -> None:
    async with db.transaction():
        for p in projects:
            await db.execute(
                "INSERT INTO projects(id, rel_path, name) VALUES (?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET rel_path=excluded.rel_path, name=excluded.name",
                (p.id, p.rel_path, p.name))


async def touch_opened(db: Database, project_id: str) -> None:
    await db.execute("UPDATE projects SET last_opened_at = ? WHERE id = ?",
                     (utcnow_iso(), project_id))
```

`backend/src/copperchat/web/routes_projects.py`:
```python
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from copperchat.db.repos.projects import upsert_projects
from copperchat.projects import discover_projects, get_project, lock_status
from copperchat.web.auth import User, require_user

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("")
async def list_projects(request: Request, _: User = Depends(require_user)) -> list[dict[str, Any]]:
    projects = discover_projects(request.app.state.settings.projects_root)
    await upsert_projects(request.app.state.db, projects)
    return [{"id": p.id, "name": p.name, "relPath": p.rel_path,
             "hasSch": p.sch_file is not None, "hasPcb": p.pcb_file is not None,
             "locked": lock_status(p).locked} for p in projects]


@router.post("/{project_id}/open")
async def open_project(project_id: str, request: Request,
                       _: User = Depends(require_user)) -> dict[str, Any]:
    get_project(request.app.state.settings.projects_root, project_id)
    raise HTTPException(status_code=501, detail="opening is implemented in Task 3.5")
```

`backend/src/copperchat/web/app.py` — add the router (before `mount_frontend`):
```python
from copperchat.web import auth, health, routes_projects
...
    app.include_router(health.router)
    app.include_router(routes_projects.router)
    mount_frontend(app, settings.frontend_dist)  # must stay last: it mounts "/"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit -q && uv run mypy src`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/projects.py backend/src/copperchat/db/repos \
  backend/src/copperchat/web/routes_projects.py backend/src/copperchat/web/app.py \
  backend/tests/unit/test_projects.py backend/tests/unit/test_routes_projects.py
git commit -m "feat: project discovery, lock detection and id validation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 3.5: KiCad session lifecycle and the open endpoint (read-only on lock)

**Files:**
- Create: `backend/src/copperchat/kicad_session/__init__.py`, `kicad_session/errors.py`, `kicad_session/launcher.py` (`ServerLauncher` protocol + real `KicadCliLauncher`), `kicad_session/session.py`
- Create: `backend/tests/support/fake_kicad.py`
- Modify: `backend/src/copperchat/web/app.py`, `backend/src/copperchat/web/routes_projects.py`
- Test: `backend/tests/unit/test_kicad_session.py`, `backend/tests/unit/test_open_project.py`, `backend/tests/unit/test_launcher.py`

**Interfaces:**
- Consumes: C1, C3.
- Produces: C4 (`DocKind`, `KicadUnavailable`, `KicadCallTimeout`, `KicadHandle`,
  `KicadSession`, `KicadSessionManager`, `ServerLauncher`) plus **new**:
  - `ClientFactory = Callable[[str, int], Any]` — `(socket_uri, timeout_ms) -> kipy.KiCad`-like.
  - `KicadSessionManager.__init__(settings, launcher=None, *, client_factory=None)`.
  - `KicadSession.view_version: int` (starts 0; bumped by `views.write_kicanvas_snapshot`, Phase 4).
  - `KicadSession.socket_path: Path`, `KicadSession.process: asyncio.subprocess.Process | None`.
  - `socket_path_for(project: Project) -> Path` → `/tmp/copperchat/<id>.sock`.
  - `create_app(settings, *, launcher=None, client_factory=None)`; `app.state.sessions`.
  - `KicadCliLauncher(kicad_cli: str, log_dir: Path)` implementing `ServerLauncher`
    (the default launcher of `KicadSessionManager`, so it lives in this task).
  - C8 `POST /api/projects/{id}/open` → `{id, readOnly, lockFiles, kicadVersion}`
    where `lockFiles` are file names relative to the project directory.

- [ ] **Step 1: Write the failing test**

`backend/tests/support/fake_kicad.py`:
```python
"""Test doubles for the kicad-cli api-server process and the kipy client."""
from __future__ import annotations

import asyncio
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kipy.errors import ConnectionError as KipyConnectionError

from copperchat.projects import Project


@dataclass
class FakeServer:
    """Shared state between FakeLauncher and FakeKiCad clients."""

    dead: bool = False
    starts: int = 0
    clients: int = 0
    fail_start_with_exit_code: int | None = None
    release: threading.Event = field(default_factory=threading.Event)
    processes: list[asyncio.subprocess.Process] = field(default_factory=list)

    def crash(self) -> None:
        self.dead = True
        for proc in self.processes:
            if proc.returncode is None:
                proc.kill()


class FakeLauncher:
    def __init__(self, server: FakeServer) -> None:
        self.server = server

    async def start(self, project: Project, socket_path: Path) -> asyncio.subprocess.Process:
        self.server.starts += 1
        code = self.server.fail_start_with_exit_code
        script = f"import sys; sys.exit({code})" if code is not None else "import time; time.sleep(3600)"
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", script)
        self.server.processes.append(proc)
        if code is not None:
            # Simulated start failure: let the process exit before the session
            # polls it, and never mark the server alive.
            await proc.wait()
            return proc
        self.server.dead = False
        return proc


class FakeKiCad:
    def __init__(self, server: FakeServer) -> None:
        self._server = server

    def _check(self) -> None:
        if self._server.dead:
            raise KipyConnectionError("Failed to send command to KiCad: connection refused")

    def ping(self) -> None:
        self._check()

    def get_version(self) -> Any:
        self._check()
        return SimpleNamespace(full_version="11.0.0-fake", major=11, minor=0, patch=0)

    def hang(self) -> None:
        """Simulates an api-server that never answers."""
        self._server.release.wait(10)


class FakeClientFactory:
    def __init__(self, server: FakeServer) -> None:
        self.server = server

    def __call__(self, socket_uri: str, timeout_ms: int) -> FakeKiCad:
        assert socket_uri.startswith("ipc://")
        self.server.clients += 1
        return FakeKiCad(self.server)
```

`backend/tests/unit/test_kicad_session.py`:
```python
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from copperchat.config import Settings
from copperchat.errors import ReadOnlyProject
from copperchat.kicad_session import KicadSessionManager, KicadUnavailable, socket_path_for
from copperchat.projects import Project, discover_projects
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer


def make_project(root: Path, locked: bool = False) -> Project:
    d = root / "demo"
    d.mkdir(parents=True, exist_ok=True)
    for suffix in ("kicad_pro", "kicad_sch", "kicad_pcb"):
        (d / f"demo.{suffix}").touch()   # filename markers only, never opened (approved by owner 2026-10-16)
    if locked:
        (d / "~demo.kicad_pcb.lck").touch()
    return discover_projects(root)[0]


@pytest.fixture
def server() -> FakeServer:
    s = FakeServer()
    yield s
    s.release.set()


@pytest.fixture
async def manager(make_settings: Callable[..., Settings],
                  server: FakeServer) -> AsyncIterator[KicadSessionManager]:
    m = KicadSessionManager(make_settings(), FakeLauncher(server),
                            client_factory=FakeClientFactory(server))
    yield m
    await m.close_all()


async def test_open_starts_server_and_connects(manager: KicadSessionManager, server: FakeServer,
                                               make_settings: Callable[..., Settings]) -> None:
    project = make_project(make_settings().projects_root)
    session = await manager.open(project)
    assert manager.current() is session
    assert server.starts == 1
    assert session.read_only is False
    assert session.socket_path == socket_path_for(project)
    assert session.socket_path.parent.stat().st_mode & 0o777 == 0o700
    version = await session.call(lambda h: h.kicad.get_version().full_version)
    assert version == "11.0.0-fake"


async def test_reopening_same_project_reuses_session(manager: KicadSessionManager,
                                                     server: FakeServer,
                                                     make_settings: Callable[..., Settings]) -> None:
    project = make_project(make_settings().projects_root)
    a = await manager.open(project)
    b = await manager.open(project)
    assert a is b and server.starts == 1


async def test_opening_other_project_closes_previous(manager: KicadSessionManager,
                                                     server: FakeServer, tmp_path: Path) -> None:
    first = make_project(tmp_path / "r1")
    second = make_project(tmp_path / "r2")
    s1 = await manager.open(first)
    proc = s1.process
    s2 = await manager.open(second)
    assert manager.current() is s2
    assert proc is not None and proc.returncode is not None
    with pytest.raises(KicadUnavailable):
        await s1.call(lambda h: h.kicad.ping())


async def test_locked_project_is_read_only_and_refuses_save(
        manager: KicadSessionManager, make_settings: Callable[..., Settings]) -> None:
    project = make_project(make_settings().projects_root, locked=True)
    session = await manager.open(project)
    assert session.read_only is True
    with pytest.raises(ReadOnlyProject):
        await session.save("pcb")


async def test_start_fails_when_process_exits(make_settings: Callable[..., Settings],
                                              server: FakeServer) -> None:
    server.fail_start_with_exit_code = 3
    m = KicadSessionManager(make_settings(), FakeLauncher(server),
                            client_factory=FakeClientFactory(server))
    with pytest.raises(KicadUnavailable, match="exited with code 3"):
        await m.open(make_project(make_settings().projects_root))
    assert m.current() is None


async def test_start_times_out_when_never_ready(make_settings: Callable[..., Settings],
                                                server: FakeServer) -> None:
    server.dead = True

    class NeverReady(FakeClientFactory):
        def __call__(self, socket_uri: str, timeout_ms: int):  # type: ignore[no-untyped-def]
            client = super().__call__(socket_uri, timeout_ms)
            self.server.dead = True
            return client

    m = KicadSessionManager(make_settings(kicad_start_timeout_s=0.5), FakeLauncher(server),
                            client_factory=NeverReady(server))
    with pytest.raises(KicadUnavailable, match="not ready"):
        await m.open(make_project(make_settings().projects_root))
```

`backend/tests/unit/test_open_project.py`:
```python
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.web.app import create_app
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer

GOOD = {"username": "admin", "password": "test-password"}


def markers(root: Path, locked: bool) -> None:
    d = root / "demo"
    d.mkdir(parents=True, exist_ok=True)
    for suffix in ("kicad_pro", "kicad_sch", "kicad_pcb"):
        (d / f"demo.{suffix}").touch()   # empty markers (approved by owner 2026-10-16)
    if locked:
        (d / "~demo.kicad_sch.lck").touch()
        (d / "~demo.kicad_pcb.lck").touch()


def _open(settings: Settings) -> dict[str, object]:
    server = FakeServer()
    app = create_app(settings, launcher=FakeLauncher(server),
                     client_factory=FakeClientFactory(server))
    try:
        with TestClient(app) as client:
            client.post("/api/auth/login", json=GOOD)
            project_id = client.get("/api/projects").json()[0]["id"]
            r = client.post(f"/api/projects/{project_id}/open")
            assert r.status_code == 200
            body: dict[str, object] = r.json()
            assert body["id"] == project_id
            return body
    finally:
        server.release.set()


def test_open_unlocked(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    markers(settings.projects_root, locked=False)
    body = _open(settings)
    assert body["readOnly"] is False
    assert body["lockFiles"] == []
    assert body["kicadVersion"] == "11.0.0-fake"


def test_open_locked_returns_read_only_banner_data(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    markers(settings.projects_root, locked=True)
    body = _open(settings)
    assert body["readOnly"] is True
    assert body["lockFiles"] == ["~demo.kicad_pcb.lck", "~demo.kicad_sch.lck"]
```

`backend/tests/unit/test_launcher.py` (approved by owner 2026-10-16: the empty `*.kicad_pro` is a filename marker for discovery only; no KiCad content is read):
```python
import sys
from pathlib import Path

from copperchat.kicad_session.launcher import KicadCliLauncher
from copperchat.projects import discover_projects


async def test_launcher_builds_command_line_and_logs(tmp_path: Path) -> None:
    fake = tmp_path / "kicad-cli"
    fake.write_text(f"#!{sys.executable}\nimport sys\nprint(' '.join(sys.argv[1:]), file=sys.stderr)\n")
    fake.chmod(0o755)
    proj_dir = tmp_path / "projects" / "demo"
    proj_dir.mkdir(parents=True)
    (proj_dir / "demo.kicad_pro").touch()   # filename marker only
    project = discover_projects(tmp_path / "projects")[0]
    launcher = KicadCliLauncher(str(fake), tmp_path / "logs")
    proc = await launcher.start(project, Path("/tmp/copperchat/x.sock"))
    assert await proc.wait() == 0
    log = (tmp_path / "logs" / f"api-server-{project.id}.log").read_text()
    assert f"api-server {project.pro_file} --socket /tmp/copperchat/x.sock" in log
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_kicad_session.py tests/unit/test_open_project.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'copperchat.kicad_session'`.

- [ ] **Step 3: Write minimal implementation**

`backend/src/copperchat/kicad_session/errors.py`:
```python
from copperchat.errors import CopperChatError


class KicadUnavailable(CopperChatError):
    """The api-server failed to start, crashed, or was restarted after a hang."""

    code = "kicad_unavailable"
    http_status = 503


class KicadCallTimeout(KicadUnavailable):
    code = "kicad_timeout"
```

`backend/src/copperchat/kicad_session/launcher.py`:
```python
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Protocol

from copperchat.projects import Project


class ServerLauncher(Protocol):
    async def start(self, project: Project, socket_path: Path) -> asyncio.subprocess.Process: ...


class KicadCliLauncher:
    """Starts `kicad-cli api-server <project.kicad_pro> --socket <path>`.

    If docs/spikes/1c-api-server.md records a different command line, use that.
    """

    def __init__(self, kicad_cli: str, log_dir: Path) -> None:
        self._kicad_cli = kicad_cli
        self._log_dir = log_dir

    async def start(self, project: Project, socket_path: Path) -> asyncio.subprocess.Process:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / f"api-server-{project.id}.log"
        env = {**os.environ, "LANG": "C", "LC_ALL": "C"}
        with log_path.open("ab") as log:
            return await asyncio.create_subprocess_exec(
                self._kicad_cli, "api-server", str(project.pro_file), "--socket", str(socket_path),
                cwd=project.dir, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=log, stderr=log)
```
(`asyncio.create_subprocess_exec` dups the file descriptor, so closing `log`
after spawning is safe.)

`backend/src/copperchat/kicad_session/session.py`:
```python
from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from kipy.errors import ConnectionError as KipyConnectionError

from copperchat.config import Settings
from copperchat.errors import DocumentNotOpen, ReadOnlyProject
from copperchat.kicad_session.errors import KicadCallTimeout, KicadUnavailable
from copperchat.kicad_session.launcher import ServerLauncher
from copperchat.projects import Project, lock_status

if TYPE_CHECKING:
    import kipy
    import kipy.board
    import kipy.schematic

DocKind = Literal["sch", "pcb"]
T = TypeVar("T")
ClientFactory = Callable[[str, int], Any]
SOCKET_DIR = Path("/tmp/copperchat")
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    KipyConnectionError, ConnectionError, BrokenPipeError)


def socket_path_for(project: Project) -> Path:
    return SOCKET_DIR / f"{project.id}.sock"


def default_client_factory(socket_uri: str, timeout_ms: int) -> Any:
    from kipy import KiCad

    return KiCad(socket_path=socket_uri, client_name="copperchat", timeout_ms=timeout_ms)


class KicadHandle:
    """What callables passed to KicadSession.call receive. Runs in a worker thread."""

    def __init__(self, kicad: Any) -> None:
        self.kicad = kicad

    def board(self) -> kipy.board.Board:
        from kipy.proto.common.types.base_types_pb2 import DocumentType

        if not self.kicad.get_open_documents(DocumentType.DOCTYPE_PCB):
            raise DocumentNotOpen("the project has no board open in the api-server")
        board: kipy.board.Board = self.kicad.get_board()
        return board

    def schematic(self) -> kipy.schematic.Schematic:
        # If docs/spikes/1c-api-server.md records a different accessor, use that.
        from kipy.proto.common.types.base_types_pb2 import DocumentType
        from kipy.schematic import Schematic

        docs = self.kicad.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
        if not docs:
            raise DocumentNotOpen("the project has no schematic open in the api-server")
        return Schematic(self.kicad._client, docs[0])


class KicadSession:
    def __init__(self, project: Project, settings: Settings, launcher: ServerLauncher,
                 client_factory: ClientFactory, read_only: bool) -> None:
        self.project = project
        self.read_only = read_only
        self.restart_count = 0
        self.view_version = 0
        self.socket_path = socket_path_for(project)
        self.process: asyncio.subprocess.Process | None = None
        self._settings = settings
        self._launcher = launcher
        self._client_factory = client_factory
        self._kicad: Any = None
        self._lock = asyncio.Lock()
        self._closed = False

    # -- lifecycle -----------------------------------------------------------------
    async def start(self) -> None:
        SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(SOCKET_DIR, 0o700)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        self.process = await self._launcher.start(self.project, self.socket_path)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.kicad_start_timeout_s
        timeout_ms = int(self._settings.kicad_api_timeout_s * 1000)
        last_error: BaseException | None = None
        while True:
            if self.process.returncode is not None:
                code = self.process.returncode
                self.process = None
                raise KicadUnavailable(
                    f"kicad-cli api-server exited with code {code} during startup "
                    f"(log: {self._settings.data_dir / 'logs'})")
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._kill()
                raise KicadUnavailable(
                    f"kicad-cli api-server not ready after "
                    f"{self._settings.kicad_start_timeout_s:.0f}s: {last_error}")
            try:
                client = self._client_factory(f"ipc://{self.socket_path}", timeout_ms)
                await asyncio.wait_for(asyncio.to_thread(client.ping), timeout=min(2.0, remaining))
                self._kicad = client
                return
            except Exception as exc:  # not ready yet: socket missing, refused, slow
                last_error = exc
            await asyncio.sleep(0.2)

    async def _kill(self) -> None:
        proc, self.process, self._kicad = self.process, None, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def _restart_locked(self) -> None:
        await self._kill()
        self.restart_count += 1
        await self.start()

    async def restart(self) -> None:
        async with self._lock:
            await self._restart_locked()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._kill()

    # -- calls ---------------------------------------------------------------------
    async def call(self, fn: Callable[[KicadHandle], T], *, timeout: float | None = None) -> T:
        limit = timeout if timeout is not None else self._settings.kicad_api_timeout_s
        async with self._lock:
            if self._closed:
                raise KicadUnavailable("the KiCad session for this project is closed")
            if self._kicad is None:  # an earlier restart failed: try once more now
                await self.start()
            handle = KicadHandle(self._kicad)
            try:
                return await asyncio.wait_for(asyncio.to_thread(fn, handle), timeout=limit)
            except TimeoutError:
                await self._restart_after_failure("timed out")
                raise KicadCallTimeout(
                    f"KiCad API call timed out after {limit:.0f}s; the api-server was "
                    "restarted with the last saved state") from None
            except _CONNECTION_ERRORS as exc:
                await self._restart_after_failure(str(exc))
                raise KicadUnavailable(
                    f"lost connection to the KiCad api-server ({exc}); it was restarted "
                    "with the last saved state") from exc
            except Exception:
                if self.process is not None and self.process.returncode is not None:
                    await self._restart_after_failure("process died")
                    raise KicadUnavailable(
                        "the KiCad api-server died during the call; it was restarted") from None
                raise

    async def _restart_after_failure(self, reason: str) -> None:
        try:
            await self._restart_locked()
        except KicadUnavailable as exc:
            raise KicadUnavailable(
                f"KiCad api-server failed ({reason}) and could not be restarted: {exc.message}"
            ) from exc

    async def save(self, doc: DocKind) -> None:
        if self.read_only:
            raise ReadOnlyProject(
                "project is open in KiCad (lock file present) - close it there first")

        def _save(h: KicadHandle) -> None:
            (h.schematic() if doc == "sch" else h.board()).save()

        await self.call(_save)

    async def save_copy(self, doc: DocKind, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _copy(h: KicadHandle) -> None:
            target = h.schematic() if doc == "sch" else h.board()
            target.save_as(str(dest), overwrite=True, include_project=False)

        await self.call(_copy)
        return dest


class KicadSessionManager:
    def __init__(self, settings: Settings, launcher: ServerLauncher | None = None, *,
                 client_factory: ClientFactory | None = None) -> None:
        if launcher is None:
            from copperchat.kicad_session.launcher import KicadCliLauncher

            launcher = KicadCliLauncher(settings.kicad_cli, settings.data_dir / "logs")
        self._settings = settings
        self._launcher = launcher
        self._client_factory = client_factory or default_client_factory
        self._current: KicadSession | None = None
        self._lock = asyncio.Lock()

    async def open(self, project: Project) -> KicadSession:
        async with self._lock:
            if self._current is not None and self._current.project.id == project.id:
                return self._current
            if self._current is not None:
                await self._current.close()
                self._current = None
            session = KicadSession(project, self._settings, self._launcher,
                                   self._client_factory, read_only=lock_status(project).locked)
            await session.start()
            self._current = session
            return session

    def current(self) -> KicadSession | None:
        return self._current

    async def close_all(self) -> None:
        async with self._lock:
            if self._current is not None:
                await self._current.close()
                self._current = None
```

(`KicadCliLauncher` is imported lazily so unit tests that inject a fake
launcher never touch it.)

`backend/src/copperchat/kicad_session/__init__.py`:
```python
from copperchat.kicad_session.errors import KicadCallTimeout, KicadUnavailable
from copperchat.kicad_session.launcher import ServerLauncher
from copperchat.kicad_session.session import (ClientFactory, DocKind, KicadHandle, KicadSession,
                                              KicadSessionManager, socket_path_for)

__all__ = ["ClientFactory", "DocKind", "KicadCallTimeout", "KicadHandle", "KicadSession",
           "KicadSessionManager", "KicadUnavailable", "ServerLauncher", "socket_path_for"]
```

`backend/src/copperchat/web/app.py` — new signature and session manager in the lifespan:
```python
from copperchat.kicad_session import ClientFactory, KicadSessionManager, ServerLauncher
...
def create_app(settings: Settings, *, launcher: ServerLauncher | None = None,
               client_factory: ClientFactory | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        check_model_credentials(settings)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = await Database.open(settings.data_dir / "copperchat.db")
        sessions = KicadSessionManager(settings, launcher, client_factory=client_factory)
        try:
            await auth.ensure_admin(db, settings)
            app.state.settings = settings
            app.state.db = db
            app.state.sessions = sessions
            app.state.serializer = auth.make_serializer(
                auth.load_or_create_session_secret(settings))
            app.state.login_limiter = auth.LoginRateLimiter(
                settings.login_max_failures, settings.login_window_s)
            yield
        finally:
            await sessions.close_all()
            await db.close()
    ...
```

`backend/src/copperchat/web/routes_projects.py` — replace `open_project`:
```python
from copperchat.db.repos.projects import touch_opened, upsert_projects
from copperchat.kicad_session import KicadSessionManager
...
@router.post("/{project_id}/open")
async def open_project(project_id: str, request: Request,
                       _: User = Depends(require_user)) -> dict[str, Any]:
    state = request.app.state
    project = get_project(state.settings.projects_root, project_id)
    sessions: KicadSessionManager = state.sessions
    session = await sessions.open(project)
    await touch_opened(state.db, project.id)
    version: str = await session.call(lambda h: str(h.kicad.get_version().full_version))
    locks = lock_status(project)
    return {"id": project.id, "readOnly": session.read_only,
            "lockFiles": sorted(p.name for p in locks.lock_files),
            "kicadVersion": version}
```
and drop the now-unused `HTTPException` import.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit -q && uv run mypy src && uv run ruff check .`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/kicad_session backend/src/copperchat/web \
  backend/tests/support/fake_kicad.py backend/tests/unit/test_kicad_session.py \
  backend/tests/unit/test_open_project.py backend/tests/unit/test_launcher.py
git commit -m "feat: kicad session manager and project open endpoint with read-only lock handling

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 3.6: KiCad test environment helpers, marker gating, real api-server test

**Files:**
- Create: `backend/tests/support/kicad_env.py`
- Modify: `backend/tests/conftest.py` (kicad marker gating)
- Test: `backend/tests/kicad/test_session_real.py`

**Interfaces:**
- Consumes: C3, C4, `KicadCliLauncher` (Task 3.5).
- Produces (test support, reused by Phase 4): `tests.support.kicad_env.api_server_available(kicad_cli: str = "kicad-cli") -> bool`;
  `tests.support.kicad_env.kicad_demo_dir(kicad_cli: str = "kicad-cli") -> Path | None`.
  Env var `COPPERCHAT_REQUIRE_KICAD=1` turns "skip" into "fail" for `-m kicad` runs
  (used by the Phase 4 CI job).

- [ ] **Step 1: Write the failing test**

`backend/tests/kicad/test_session_real.py`:
```python
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from copperchat.config import Settings
from copperchat.kicad_session import KicadSessionManager
from copperchat.projects import discover_projects
from tests.support.kicad_env import kicad_demo_dir

pytestmark = pytest.mark.kicad


async def test_start_ping_close(make_settings: Callable[..., Settings], tmp_path: Path) -> None:
    demos = kicad_demo_dir()
    if demos is None or not (demos / "ecc83").is_dir():
        pytest.skip("KiCad demos not installed next to kicad-cli")
    settings = make_settings(kicad_cli="kicad-cli", kicad_start_timeout_s=60.0,
                             kicad_api_timeout_s=30.0)
    shutil.copytree(demos / "ecc83", settings.projects_root / "ecc83")  # verbatim copy
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "ecc83-pp")
    manager = KicadSessionManager(settings)
    try:
        session = await manager.open(project)
        await session.call(lambda h: h.kicad.ping())
        version = await session.call(lambda h: h.kicad.get_version().full_version)
        assert version
        process = session.process
    finally:
        await manager.close_all()
    assert process is not None and process.returncode is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/kicad/test_session_real.py -q`
Expected: FAIL at collection — `ModuleNotFoundError: No module named 'tests.support.kicad_env'`
(on every machine, with or without KiCad).

- [ ] **Step 3: Write minimal implementation**

`backend/tests/support/kicad_env.py`:
```python
"""Detects a usable KiCad 11 `kicad-cli api-server` for @pytest.mark.kicad tests."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def api_server_available(kicad_cli: str = "kicad-cli") -> bool:
    if shutil.which(kicad_cli) is None:
        return False
    result = subprocess.run([kicad_cli, "api-server", "--help"], capture_output=True, timeout=30)
    return result.returncode == 0


def kicad_demo_dir(kicad_cli: str = "kicad-cli") -> Path | None:
    exe = shutil.which(kicad_cli)
    if exe is None:
        return None
    candidate = Path(exe).resolve().parent.parent / "share" / "kicad" / "demos"
    return candidate if candidate.is_dir() else None
```

`backend/tests/conftest.py` — append:
```python
import os

from tests.support.kicad_env import api_server_available


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not any(item.get_closest_marker("kicad") for item in items):
        return
    if api_server_available():
        return
    if os.environ.get("COPPERCHAT_REQUIRE_KICAD") == "1":
        raise pytest.UsageError("COPPERCHAT_REQUIRE_KICAD=1 but `kicad-cli api-server` is missing")
    skip = pytest.mark.skip(reason="`kicad-cli api-server` (KiCad 11 master) not available")
    for item in items:
        if item.get_closest_marker("kicad"):
            item.add_marker(skip)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest -m "not kicad" -q && uv run pytest -m kicad -q`
Expected: unit suite PASS; the kicad test PASSES where `kicad-cli api-server` and
the installed demos exist, otherwise it is reported as SKIPPED.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/support/kicad_env.py backend/tests/conftest.py \
  backend/tests/kicad/test_session_real.py
git commit -m "test: kicad environment helpers, marker gating and real api-server session test

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 3.7: Crash and hang recovery (Review Focus 2)

**Files:**
- Test: `backend/tests/unit/test_kicad_session_recovery.py`
- Modify: `backend/src/copperchat/kicad_session/session.py` only if a test exposes a gap.

**Interfaces:**
- Consumes: C4 from Task 3.5, `tests/support/fake_kicad.py`.
- Produces: the guarantee "call timeout or lost connection → server restarted →
  `KicadCallTimeout`/`KicadUnavailable` raised → next call works", relied upon by
  Phase 5 (turn error) and Phase 7 Task 7.4.

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/test_kicad_session_recovery.py`:
```python
import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from copperchat.config import Settings
from copperchat.kicad_session import (KicadCallTimeout, KicadSession, KicadSessionManager,
                                      KicadUnavailable)
from copperchat.projects import discover_projects
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer


@pytest.fixture
def server() -> FakeServer:
    s = FakeServer()
    yield s
    s.release.set()


@pytest.fixture
async def session(make_settings: Callable[..., Settings],
                  server: FakeServer) -> AsyncIterator[KicadSession]:
    settings = make_settings(kicad_api_timeout_s=0.5)
    d: Path = settings.projects_root / "demo"
    d.mkdir(parents=True)
    (d / "demo.kicad_pro").touch()   # filename marker only
    manager = KicadSessionManager(settings, FakeLauncher(server),
                                  client_factory=FakeClientFactory(server))
    s = await manager.open(discover_projects(settings.projects_root)[0])
    yield s
    await manager.close_all()


async def test_hang_times_out_restarts_and_next_call_works(session: KicadSession,
                                                           server: FakeServer) -> None:
    old_process = session.process
    with pytest.raises(KicadCallTimeout, match="restarted"):
        await session.call(lambda h: h.kicad.hang())
    # Unblock the abandoned worker thread now so the default executor does not
    # block loop teardown (the fixture's release.set() would come too late).
    server.release.set()
    assert session.restart_count == 1
    assert server.starts == 2
    assert old_process is not None and old_process.returncode is not None
    assert await session.call(lambda h: h.kicad.get_version().full_version) == "11.0.0-fake"


async def test_crash_raises_unavailable_then_recovers(session: KicadSession,
                                                      server: FakeServer) -> None:
    server.crash()
    with pytest.raises(KicadUnavailable) as info:
        await session.call(lambda h: h.kicad.ping())
    assert not isinstance(info.value, KicadCallTimeout)
    assert session.restart_count == 1
    await session.call(lambda h: h.kicad.ping())


async def test_call_is_never_retried_silently(session: KicadSession, server: FakeServer) -> None:
    calls = 0

    def flaky(h: object) -> None:
        nonlocal calls
        calls += 1
        # Runs in the worker thread: only flip the flag; never kill asyncio
        # subprocesses (crash()) from outside the event loop thread.
        server.dead = True
        h.kicad.ping()  # type: ignore[attr-defined]

    with pytest.raises(KicadUnavailable):
        await session.call(flaky)
    assert calls == 1


async def test_custom_timeout_overrides_default(session: KicadSession,
                                                server: FakeServer) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(KicadCallTimeout):
        await session.call(lambda h: h.kicad.hang(), timeout=0.1)
    server.release.set()   # free the worker thread before teardown
    assert loop.time() - started < 5


async def test_failed_restart_reported_then_lazy_start_on_next_call(
        session: KicadSession, server: FakeServer) -> None:
    server.fail_start_with_exit_code = 1
    server.crash()
    with pytest.raises(KicadUnavailable, match="could not be restarted"):
        await session.call(lambda h: h.kicad.ping())
    server.fail_start_with_exit_code = None
    await session.call(lambda h: h.kicad.ping())


async def test_concurrent_calls_are_serialised(session: KicadSession) -> None:
    order: list[int] = []

    def record(i: int) -> Callable[[object], None]:
        def _fn(_: object) -> None:
            order.append(i)
        return _fn

    await asyncio.gather(*(session.call(record(i)) for i in range(5)))
    assert sorted(order) == list(range(5)) and len(order) == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_kicad_session_recovery.py -q`
Expected: the tests run against the Task 3.5 implementation. If all pass on the
first run, deliberately break `_restart_after_failure` (comment out the
`_restart_locked()` call) and re-run to confirm `test_hang_times_out_restarts_and_next_call_works`
fails with `assert 0 == 1` on `restart_count`; then restore it. This proves the
tests guard the behaviour.

- [ ] **Step 3: Write minimal implementation**

No new code is expected: the restart logic was written in Task 3.5
(`KicadSession.call`, `_restart_after_failure`, lazy `start()` when `_kicad is None`).
If `test_failed_restart_reported_then_lazy_start_on_next_call` fails because
`_kill()` left `self.process` set, make `start()` failure paths always clear
`self.process` and `self._kicad` (as in the Task 3.5 code).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit -q`
Expected: all PASS, no warnings about un-awaited coroutines or hanging threads at exit.

- [ ] **Step 5: Commit**

```bash
git add backend/tests/unit/test_kicad_session_recovery.py backend/src/copperchat/kicad_session/session.py
git commit -m "test: kicad session restarts after hang or crash and recovers on next call

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Task 3.8: Tool spec and registry (C5)

**Files:**
- Create: `backend/src/copperchat/tools/__init__.py`, `tools/spec.py`, `tools/registry.py`
- Test: `backend/tests/unit/test_tools_registry.py`

**Interfaces:**
- Consumes: C3 `Project`, C4 `KicadSession`, C1 `Settings`, `StructuredToolError`.
- Produces: C5 exactly (`ImageBlock`, `ToolResult`, `StructuredToolError` re-export,
  `ToolContext`, `ToolSpec`, `registry.all_specs()`), plus `ToolRegistryError(CopperChatError)`
  and module constant `registry._PROVIDERS: tuple[str, ...]` (dotted module names;
  Phase 4 adds `"copperchat.kicad_tools"`, `"copperchat.views"`; Phase 7
  `"copperchat.git_tools"`; Phase 8 `"copperchat.research"`).

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/test_tools_registry.py`:
```python
import sys
import types
from collections.abc import Iterator

import pytest

from copperchat.tools import ToolContext, ToolResult, ToolSpec, registry
from copperchat.tools.registry import ToolRegistryError


async def _noop(ctx: ToolContext, args: dict[str, object]) -> ToolResult:
    return ToolResult(data={"ok": True})


def _spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, description="d",
                    input_schema={"type": "object", "properties": {}}, handler=_noop)


@pytest.fixture
def fake_module() -> Iterator[types.ModuleType]:
    mod = types.ModuleType("copperchat_test_provider")
    mod.TOOL_SPECS = [_spec("alpha"), _spec("beta")]  # type: ignore[attr-defined]
    sys.modules[mod.__name__] = mod
    yield mod
    del sys.modules[mod.__name__]


def test_empty_registry() -> None:
    assert registry.all_specs() == []


def test_collects_specs(monkeypatch: pytest.MonkeyPatch, fake_module: types.ModuleType) -> None:
    monkeypatch.setattr(registry, "_PROVIDERS", (fake_module.__name__,))
    assert [s.name for s in registry.all_specs()] == ["alpha", "beta"]


def test_missing_provider_import_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_PROVIDERS", ("copperchat.does_not_exist",))
    with pytest.raises(ModuleNotFoundError):
        registry.all_specs()


def test_provider_without_tool_specs_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_PROVIDERS", ("copperchat.errors",))
    with pytest.raises(ToolRegistryError, match="TOOL_SPECS"):
        registry.all_specs()


def test_duplicate_names_raise(monkeypatch: pytest.MonkeyPatch,
                               fake_module: types.ModuleType) -> None:
    fake_module.TOOL_SPECS.append(_spec("alpha"))  # type: ignore[attr-defined]
    monkeypatch.setattr(registry, "_PROVIDERS", (fake_module.__name__,))
    with pytest.raises(ToolRegistryError, match="duplicate"):
        registry.all_specs()


@pytest.mark.parametrize("bad", ["CamelCase", "with-dash", "", "1abc"])
def test_rejects_bad_names_and_schemas(monkeypatch: pytest.MonkeyPatch,
                                       fake_module: types.ModuleType, bad: str) -> None:
    fake_module.TOOL_SPECS = [_spec(bad)]  # type: ignore[attr-defined]
    monkeypatch.setattr(registry, "_PROVIDERS", (fake_module.__name__,))
    with pytest.raises(ToolRegistryError):
        registry.all_specs()


def test_rejects_non_object_schema(monkeypatch: pytest.MonkeyPatch,
                                   fake_module: types.ModuleType) -> None:
    fake_module.TOOL_SPECS = [ToolSpec(name="x", description="d",  # type: ignore[attr-defined]
                                       input_schema={"type": "string"}, handler=_noop)]
    monkeypatch.setattr(registry, "_PROVIDERS", (fake_module.__name__,))
    with pytest.raises(ToolRegistryError, match="object"):
        registry.all_specs()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_tools_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'copperchat.tools'`.

- [ ] **Step 3: Write minimal implementation**

`backend/src/copperchat/tools/spec.py`:
```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession
from copperchat.projects import Project

__all__ = ["ImageBlock", "StructuredToolError", "ToolContext", "ToolResult", "ToolSpec"]


@dataclass(frozen=True)
class ImageBlock:
    mime: str
    data: bytes
    path: Path | None = None


@dataclass
class ToolResult:
    data: dict[str, Any] | None = None
    images: list[ImageBlock] = field(default_factory=list)


@dataclass
class ToolContext:
    project: Project
    session: KicadSession
    settings: Settings
    run_id: str | None = None
    on_mutation: Callable[[], Awaitable[None]] | None = None


ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    mutating: bool = False
    changes_documents: bool = False
```

`backend/src/copperchat/tools/registry.py`:
```python
from __future__ import annotations

import importlib
import re

from copperchat.errors import CopperChatError
from copperchat.tools.spec import ToolSpec

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Fixed provider list. Each phase that adds tools appends one module name here.
# A provider that fails to import is an error - never skipped.
_PROVIDERS: tuple[str, ...] = ()


class ToolRegistryError(CopperChatError):
    code = "tool_registry_error"


def all_specs() -> list[ToolSpec]:
    specs: list[ToolSpec] = []
    for module_name in _PROVIDERS:
        module = importlib.import_module(module_name)
        provided = getattr(module, "TOOL_SPECS", None)
        if not isinstance(provided, list):
            raise ToolRegistryError(f"{module_name} does not define TOOL_SPECS: list[ToolSpec]")
        specs.extend(provided)
    seen: set[str] = set()
    for spec in specs:
        if not _NAME_RE.fullmatch(spec.name):
            raise ToolRegistryError(f"tool name {spec.name!r} is not snake_case")
        if spec.name in seen:
            raise ToolRegistryError(f"duplicate tool name {spec.name!r}")
        if spec.input_schema.get("type") != "object":
            raise ToolRegistryError(f"tool {spec.name!r}: input_schema must be a JSON object schema")
        seen.add(spec.name)
    return specs
```

`backend/src/copperchat/tools/__init__.py`:
```python
from copperchat.tools import registry
from copperchat.tools.spec import (ImageBlock, StructuredToolError, ToolContext, ToolHandler,
                                   ToolResult, ToolSpec)

__all__ = ["ImageBlock", "StructuredToolError", "ToolContext", "ToolHandler", "ToolResult",
           "ToolSpec", "registry"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest -m "not kicad" -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: everything PASSES and is clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/tools backend/tests/unit/test_tools_registry.py
git commit -m "feat: tool spec types and provider registry

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Phase self-review

**Spec sections covered:** §3 (backend stack, auth model, app login always on),
§4 (mounts `/projects`, `/data`; `.env`), §4.1 (`kicad_session` per-project
manager with restart on crash/hang, `web`, `db`), §4.2 (data for the project
picker incl. lock warning), §10 (API server crash/hang → restart + clear error;
lock file → read-only; missing token → startup error naming `claude setup-token`),
§11 (fake-process tests for crash/hang, one real api-server test), §12 (no login
flow, GPL-compatible deps: FastAPI MIT, uvicorn BSD, pydantic MIT, aiosqlite MIT,
argon2-cffi MIT, itsdangerous BSD, kicad-python MIT).

**Review Focus owned here:** 2 → Task 3.7; 3 → Task 3.5 (`test_open_locked_returns_read_only_banner_data`,
`test_locked_project_is_read_only_and_refuses_save`); 5 (project ids) → Task 3.4.

**Deviations / additions to the index:**
- Settings extensions `cookie_secure`, `login_max_failures`, `login_window_s`, `frontend_dist`.
- `KicadSessionManager(..., *, client_factory=None)`, `ClientFactory`,
  `KicadSession.view_version`, `KicadSession.socket_path`, `KicadSession.process`,
  `socket_path_for()`; `create_app(settings, *, launcher=None, client_factory=None)`;
  `app.state.{settings,db,sessions,serializer,login_limiter}`; `web.auth.user_from_cookie`.
- Errors `DocumentNotOpen`, `ReadOnlyProject`, `InvalidProjectId`, `ToolRegistryError`.
- Migration runner also keeps a `schema_migrations` table so out-of-order merges
  (0003 after 0004) are applied; `user_version` still holds the highest number.
- `kicad-python` pinned to a git revision because the 0.8.0 wheel cannot import
  `kipy.schematic` / `kipy.board_jobs` (verified 2026-10-15).
- Unit tests use zero-byte `*.kicad_pro` marker files for discovery only (never
  opened by KiCad) — approved by owner 2026-10-16; the real api-server test copies the installed KiCad demos verbatim.
