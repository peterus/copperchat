# CopperChat Foundation Implementation Plan (index)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This file is the **index**. Each phase lives in its own file under
> `docs/superpowers/plans/foundation/` and is executed by one child session as
> **exactly one PR**. A phase executor reads, in this order: this index (Global
> Constraints, Review Focus, Repository Layout, Interface Contracts, Phase
> Conventions), the spec, any spike result docs its phase names, then its own
> phase file. Nothing else is required.

**Goal:** Build CopperChat sub-project 1 "Foundation": a self-hosted web app
(one Docker image on top of a source-built KiCad 11 base image) where a user
picks a KiCad project, sees schematic and board in the browser, and chats with
a read-only Claude agent that inspects the design through the headless
`kicad-cli api-server`, runs ERC/DRC and looks at rendered "agent views".

**Architecture:** Python/FastAPI backend with one `kicad-cli api-server`
process per open project (driven via `kicad-python`/`kipy`), a Claude Agent SDK
runtime per project in streaming-input mode (queue, interrupt, resume, rate
limit, modelled on Kontor), SQLite for state, and a React/Vite/TypeScript PWA
that renders KiCanvas plus our own agent-view PNGs and a Kontor-grade chat
history. All design data comes from the running API server — never from a
CopperChat file parser.

**Tech Stack:** Python 3.13, FastAPI, uvicorn, aiosqlite, pydantic v2,
`kicad-python` (`kipy`), `claude-agent-sdk` (Python), Pillow, `pywebpush`, uv,
pytest/pytest-asyncio, ruff, mypy; TypeScript 5, React 19, Vite 7, zustand 5,
zod 4, Tailwind CSS 4 (CSS-variable theme), Vitest + Testing Library,
Playwright (smoke); KiCanvas; Docker / docker compose; GitHub Actions; GHCR.

**Spec:** `docs/superpowers/specs/2026-10-15-foundation-design.md`
(handoff context: `docs/superpowers/handoff/2026-10-15-coordinator-brief.md`).

---

## Phase overview

| # | Phase file | Goal (one line) | Depends on | Can run in parallel with |
|---|---|---|---|---|
| 1A | `foundation/phase-1a-spike-sdk-streaming.md` | Prove the Python Agent SDK pulls streaming input lazily at turn boundaries; prove interrupt/resume/tool-only config | — | 1B, 1C |
| 1B | `foundation/phase-1b-spike-kicanvas.md` | Prove KiCanvas renders KiCad 11 `.kicad_sch`/`.kicad_pcb` incl. click events (or decide own viewer) | — | 1A, 1C |
| 1C | `foundation/phase-1c-spike-api-server.md` | Prove `kicad-cli api-server` round-trip (GetItems, SaveCopyOfDocument, SaveDocument, PNG export, crash/restart) and pin the KiCad commit | — | 1A, 1B |
| 2 | `foundation/phase-2-kicad-base-image.md` | CI source build of the pinned KiCad master commit → `ghcr.io/peterus/copperchat-kicad-base:<commit>` | 1C | 3, 5 |
| 3 | `foundation/phase-3-backend-skeleton.md` | FastAPI app, config, SQLite + migrations, login, project discovery + lock detection, `kicad_session`, tool spec/registry, CI | 1C | 2 |
| 4 | `foundation/phase-4-kicad-tools-views.md` | Read-only `kicad_tools`, ERC/DRC, `render_view`, agent-view renderer, KiCanvas snapshots, fixture projects | 2, 3 | 5 |
| 5 | `foundation/phase-5-agent-runtime.md` | Agent runtime: push stream, run engine, queue/interrupt/resume/rate limit, message persistence, WebSocket protocol, usage, tool adapter + guards | 1A, 3 | 2, 4 |
| 6A | `foundation/phase-6a-frontend-shell-chat.md` | Frontend shell: login, project picker, responsive layout, themes, WS client, chat store + history with all Kontor lessons, composer, queue, Stop, usage | 5 | 4, 7 |
| 6B | `foundation/phase-6b-frontend-viewer.md` | Viewer: KiCanvas tabs, agent-view toggle, layer toggles, tap → `@REF`, `view-updated` refresh, fullscreen pinch-zoom | 1B, 4, 6A | 7, 8 |
| 7 | `foundation/phase-7-history-git-recovery.md` | Per-turn snapshots + undo, autosave/view refresh at turn end, external-change hash check, crash recovery, git tools + UI | 4, 5 | 6A, 6B, 8 |
| 8 | `foundation/phase-8-skills-research-uploads.md` | Three-layer skills + Skills page, `part-researcher` sub-agent + Parts page, image/PDF attachments | 5, 6A | 6B, 7 |
| 9 | `foundation/phase-9-pwa-push-release.md` | PWA, Web Push, app Dockerfile, compose (+ optional cloudflared), Cloudflare docs, README, release workflow, CI matrix | 6B, 7, 8 | — |

Execution waves (every phase in a wave can run in parallel once the previous
waves it depends on are merged):

| Wave | Phases |
|---|---|
| 1 | 1A, 1B, 1C |
| 2 | 2, 3 |
| 3 | 4, 5 |
| 4 | 6A, 7 |
| 5 | 6B, 8 |
| 6 | 9 |

**Go/no-go gates.** The three spikes can change later phases. Each spike doc
has a "Decision" section; the coordinator reads it before spawning dependants:

- **1A partial — expected** (source reading on 2026-10-15: the Python SDK
  writes each streaming-input item to the CLI's stdin as soon as the input
  iterator yields it): Phase 5 uses a *gated generator* — `RunEngine` releases
  the next queued message only after it has seen the previous turn's
  `ResultMessage`. This is the default design in Phase 5 regardless of 1A.
- **1A no-go** (gated generator does not keep turns separate, or interrupt
  breaks the long-lived stream): Phase 5 switches to the "one `query()` per
  turn + `resume`" fallback described in Phase 5, Task 5.4b. Queue semantics
  stay identical from the client's view.
- **1B no-go** (KiCanvas cannot parse KiCad 11 files or exposes no click
  events): Phase 6B implements the fallback "own viewer" path described in its
  file (render server-side PNG/SVG from the API + hit-test via
  `/api/projects/{id}/hit-test`) instead of KiCanvas.
- **1C kipy revision:** the PyPI `kicad-python` 0.8.0 wheel is broken
  (`import kipy.schematic` and `import kipy.board_jobs` fail). 1C records a
  working git revision ("kipy revision" in `docs/spikes/1c-api-server.md`);
  Phase 2 installs it in the base image and Phase 3 pins it via
  `[tool.uv.sources]`. 1C also records whether `RunSchematicJobExportPng` /
  `RunBoardJobExportPng` exist at the pinned commit; if not, Phase 4 renders
  via `kicad-cli sch|pcb export svg` + rasterisation.
- **1C no-go** on any core operation (GetItems on schematic, SaveDocument,
  SaveCopyOfDocument, PNG export): stop and escalate to the owner (status
  `blocked`) — the architecture depends on it. Partial gaps (e.g. one export
  missing) are recorded with the `kicad-cli` workaround that Phases 4/7 use.

---

## Global Constraints

Copied verbatim or near-verbatim from the spec; every task implicitly includes
these.

- Repository content (code, comments, docs, commit messages) is **English**.
- **Never hand-edit KiCad files** (`.kicad_sch`, `.kicad_pcb`, `.kicad_pro`,
  `.kicad_sym`, `fp-lib-table`, `sym-lib-table`, footprints). Only KiCad's API
  (`kipy` against `kicad-cli api-server`) or `kicad-cli` may create or change
  them — in code, in fixtures, in tests, in CI. Fixtures are copied verbatim
  from the KiCad source tree and upgraded only via `kicad-cli … upgrade`.
- CopperChat ships **no claude.ai login flow**. Model auth is
  `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`, the user's own
  subscription) or `ANTHROPIC_API_KEY`, read from the environment only.
- Missing/expired token: **startup error pointing to `claude setup-token`**.
- Branding: name "CopperChat"; must not be branded "Claude Code"; "Powered by
  Claude" is allowed.
- License: **GPL-3.0-only**; every new dependency must be GPL-3.0-compatible
  (MIT/BSD/Apache-2.0/ISC/LGPL/GPL-3.0 OK).
- KiCad: **KiCad 11 (master) built from source at a pinned commit**; headless
  `kicad-cli api-server`. The pinned commit lives in one file:
  `docker/kicad-base/KICAD_COMMIT`.
- Data source rule: agent tools read **only from the running API server** (or
  `kicad-cli` for ERC/DRC/exports) — **never from a CopperChat file parser**.
- Tool output rules: structured JSON; **full UUIDs**; **mm coordinates**;
  filters instead of pagination; errors only for real failures; success only
  after verification. Tool errors are structured `{what, why, suggestion}`.
- The **main agent has no file, shell or web tools** (SDK built-ins disabled:
  `tools=[]`). Only `part-researcher` has web + write access, writes restricted
  to `<project>/docs/parts/` by a `can_use_tool` path guard.
- Foundation agent is **read-only on design data**: no schematic or PCB
  editing tool exists in this sub-project.
- Git: never touched automatically; agent tools `git_status`,
  `git_diff_summary`, `git_commit(message)` only; commit only on explicit user
  request; **no `git push`, no history rewriting**.
- Save: autosave after every agent turn that changed a document.
- Undo: own snapshots per turn under `<project>/.kicad-agent/history/`.
- Deployment: self-hosted, `docker compose up`, **one user per instance**;
  app login (username + password) is **always on**.
- Mounts: `/projects` (any depth, projects discovered by `*.kicad_pro`) and
  `/data` (SQLite DB, SDK session store, user skills, password hash, push
  subscriptions, VAPID keys). `.env` holds `CLAUDE_CODE_OAUTH_TOKEN` or
  `ANTHROPIC_API_KEY`, admin credentials, optional `CLOUDFLARE_TUNNEL_TOKEN`.
- **Every WebSocket frame carries `projectId`**; frames are schema-validated on
  both sides.
- Images on `ghcr.io/peterus/…`; users never build KiCad locally.
- Web Push needs HTTPS; iOS needs the PWA installed (iOS ≥ 16.4).
- Testing: tool tests run against a **real headless `api-server`** with small
  fixture projects (**no KiCad mocks** for tool tests); snapshot tests for the
  agent view; chat-pipeline tests with a fake agent for Kontor's bug classes.
- Python ≥ 3.13 (Debian trixie in the base image), Node ≥ 20.19.

## Review Focus

Failure modes the spec implies but that are easy to leave untested. Each line
names the owning task where its test lives.

1. **Duplicate or out-of-order chat rows** when the WS replay races the HTTP
   baseline, on retransmit of the same `clientMessageId`, or on reconnect
   mid-stream — expected: exactly one row per server id, in id order, healed
   content. Tests: Phase 5 Task 5.2 (idempotent insert), Phase 6A Tasks 6A.4–6A.6.
2. **`kicad-cli api-server` hangs or dies mid-turn** — expected: the call times
   out, the server is restarted, the turn ends with a clear chat error, and
   the next turn works against the last autosaved state. Tests: Phase 3 Task
   3.7 (fake server process), Phase 4 Task 4.2 (real server kill), Phase 7
   Task 7.5.
3. **Project opened in KiCad GUI at the same time** (lock file present) —
   expected: project opens read-only with the "open in KiCad — close it
   there" banner; no autosave, no snapshot restore. Tests: Phase 3 Task 3.5,
   Phase 7 Task 7.2.
4. **Files changed outside CopperChat between turns** (`git pull`, KiCad GUI
   save) — expected: hash check before the turn reloads the documents and
   posts a chat notice; the agent never sees stale data. Test: Phase 7 Task 7.4.
5. **Path traversal / escape in user-controlled paths** — project ids,
   upload filenames, part MPN folder names, skill names, `part-researcher`
   write paths, history snapshot ids — expected: rejected with 400 / tool
   denial, nothing written outside the allowed root. Tests: Phase 3 Task 3.4
   (project ids), Phase 7 Task 7.1 (snapshot ids), Phase 8 Tasks 8.2, 8.4, 8.5.

---

## Repository layout (target after Phase 9)

```
backend/
  pyproject.toml              # uv project "copperchat", Python 3.13
  uv.lock
  src/copperchat/
    __init__.py
    __main__.py               # `python -m copperchat` → uvicorn
    config.py                 # Settings (pydantic-settings)
    errors.py                 # CopperChatError, StructuredToolError
    db/
      __init__.py             # Database (aiosqlite wrapper), migrate()
      migrations/0001_core.sql        # Phase 3
      migrations/0002_chat.sql        # Phase 5
      migrations/0003_history.sql     # Phase 7
      migrations/0004_skills.sql      # Phase 8
      migrations/0005_push.sql        # Phase 9
      repos/…                 # one module per table group
    web/
      app.py                  # create_app(settings) factory
      auth.py                 # login, session cookie, dependency `require_user`
      routes_projects.py      # Phase 3
      routes_messages.py      # Phase 5
      routes_views.py         # Phase 4
      routes_history.py       # Phase 7
      routes_git.py           # Phase 7
      routes_skills.py        # Phase 8
      routes_parts.py         # Phase 8
      routes_uploads.py       # Phase 8
      routes_push.py          # Phase 9
      ws.py                   # Phase 5
      static.py               # serves frontend/dist (Phase 3 stub, Phase 9 final)
    projects.py               # discovery, ids, lock detection (Phase 3)
    kicad_session/            # Phase 3
    tools/                    # ToolSpec, ToolContext, registry (Phase 3)
    kicad_tools/              # Phase 4
    views/                    # Phase 4
    agent/                    # Phase 5 (+ guards in 8)
    history/                  # Phase 7
    git_tools/                # Phase 7
    skills/                   # Phase 8 (assembly) + builtin/ content
    research/                 # Phase 8
    push/                     # Phase 9
  tests/
    conftest.py
    unit/…                    # no KiCad, no network
    kicad/…                   # @pytest.mark.kicad, need real kicad-cli api-server
    fixtures/projects/…       # Phase 4 (copied from KiCad demos)
frontend/
  package.json, vite.config.ts, tsconfig.json, index.html
  public/ (manifest.webmanifest, sw.js, icons — Phase 9)
  src/
    main.tsx, App.tsx
    api/ (http.ts, ws.ts)
    protocol/frames.ts        # zod mirror of backend protocol (Phase 6A)
    stores/ (auth.ts, projects.ts, chat.ts, view.ts, ui.ts)
    components/…, pages/…, layouts/…, theme/…
protocol-fixtures/            # shared JSON frame fixtures, validated by both test suites (Phase 5)
docker/
  kicad-base/Dockerfile, KICAD_COMMIT    # Phase 2
  app/Dockerfile                         # Phase 9
docker-compose.yml, .env.example         # Phase 9
.github/workflows/
  ci.yml                     # Phase 3 (backend), extended by 6A (frontend), 4 (kicad job)
  kicad-base.yml             # Phase 2
  release.yml                # Phase 9
docs/
  spikes/                    # Phase 1 result docs
  deployment/cloudflare.md   # Phase 9
  architecture.md            # Phase 9
```

---

## Interface Contracts

These names and shapes are binding across phases. A phase may add fields or
functions; it may not rename or change the ones below without updating this
index in the same PR.

### C1. Configuration — `copperchat.config` (Phase 3)

```python
class Settings(BaseSettings):            # env prefix: none; reads .env
    projects_root: Path = Path("/projects")
    data_dir: Path = Path("/data")
    admin_username: str = "admin"
    admin_password: SecretStr | None = None      # used once to seed the hash
    session_secret: SecretStr | None = None      # generated into /data if absent
    claude_code_oauth_token: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    kicad_cli: str = "kicad-cli"
    kicad_api_timeout_s: float = 30.0
    kicad_start_timeout_s: float = 60.0
    host: str = "0.0.0.0"
    port: int = 8080
    public_base_url: str | None = None           # for push click-through
    dev_fake_agent: bool = False                 # tests / UI dev without a token

def get_settings() -> Settings: ...
def check_model_credentials(s: Settings) -> None   # raises MissingCredentialsError
```

`MissingCredentialsError` message must contain the literal text
`claude setup-token`. `check_model_credentials` is skipped when
`dev_fake_agent=True`.

### C2. Database — `copperchat.db` (Phase 3)

```python
class Database:
    @classmethod
    async def open(cls, path: Path) -> "Database"    # WAL, foreign_keys=ON, runs migrate()
    async def execute(self, sql: str, params: Sequence | Mapping = ()) -> int   # lastrowid
    async def fetchone(self, sql, params=()) -> aiosqlite.Row | None
    async def fetchall(self, sql, params=()) -> list[aiosqlite.Row]
    def transaction(self) -> AsyncContextManager[None]
    async def close(self) -> None
```

Migrations: `backend/src/copperchat/db/migrations/NNNN_name.sql`, applied in
numeric order, version tracked with `PRAGMA user_version`. Numbers are
pre-assigned per phase (see layout) so parallel phases never collide.

Tables and owning phase:

| Table | Phase | Columns (minimum) |
|---|---|---|
| `users` | 3 | `id INTEGER PK, username TEXT UNIQUE, password_hash TEXT, created_at TEXT` |
| `projects` | 3 | `id TEXT PK, rel_path TEXT UNIQUE, name TEXT, last_opened_at TEXT` |
| `messages` | 5 | `id INTEGER PK AUTOINCREMENT, project_id TEXT, client_message_id TEXT NULL, run_id TEXT NULL, role TEXT, kind TEXT, content TEXT, status TEXT, metadata TEXT (JSON), created_at TEXT, updated_at TEXT`; `UNIQUE INDEX ux_messages_client ON messages(project_id, client_message_id) WHERE client_message_id IS NOT NULL` |
| `runs` | 5 | `id TEXT PK, project_id TEXT, started_at TEXT, finished_at TEXT NULL, reason TEXT NULL, usage TEXT (JSON) NULL` |
| `agent_sessions` | 5 | `project_id TEXT PK, sdk_session_id TEXT, updated_at TEXT` |
| `rate_limit_state` | 5 | `project_id TEXT PK, resets_at TEXT NULL` |
| `history_snapshots` | 7 | `id TEXT PK, project_id TEXT, run_id TEXT, created_at TEXT, label TEXT, files TEXT (JSON)` |
| `document_hashes` | 7 | `project_id TEXT, rel_path TEXT, sha256 TEXT, PRIMARY KEY(project_id, rel_path)` |
| `skill_prefs` | 8 | `project_id TEXT, skill_name TEXT, enabled INTEGER, PRIMARY KEY(project_id, skill_name)` |
| `push_subscriptions` | 9 | `id INTEGER PK, endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT, created_at TEXT` |

### C3. Projects — `copperchat.projects` (Phase 3)

```python
@dataclass(frozen=True)
class Project:
    id: str            # first 12 hex chars of sha1(rel_path of .kicad_pro, posix)
    name: str          # .kicad_pro stem
    rel_path: str      # posix path of the .kicad_pro relative to projects_root
    dir: Path          # absolute project directory
    pro_file: Path
    sch_file: Path | None   # <stem>.kicad_sch if it exists
    pcb_file: Path | None   # <stem>.kicad_pcb if it exists

@dataclass(frozen=True)
class LockStatus:
    locked: bool
    lock_files: tuple[Path, ...]   # KiCad lock files: "~<file>.lck" next to sch/pcb/pro

def discover_projects(root: Path) -> list[Project]       # skips hidden dirs and .kicad-agent
def get_project(root: Path, project_id: str) -> Project  # raises ProjectNotFound
def lock_status(project: Project) -> LockStatus
def agent_dir(project: Project) -> Path                  # <project.dir>/.kicad-agent
```

### C4. KiCad session — `copperchat.kicad_session` (Phase 3)

```python
DocKind = Literal["sch", "pcb"]

class KicadUnavailable(CopperChatError): ...      # start failed / crashed / timed out
class KicadCallTimeout(KicadUnavailable): ...

class KicadHandle:                    # passed to callables; wraps kipy objects
    kicad: kipy.KiCad
    def schematic(self) -> kipy.schematic.Schematic   # exact accessor from spike 1C
    def board(self) -> kipy.board.Board

class KicadSession:
    project: Project
    read_only: bool                   # True when lock_status(project).locked at open
    async def call(self, fn: Callable[[KicadHandle], T], *, timeout: float | None = None) -> T
        # runs fn in a worker thread; on timeout/connection error: restarts the
        # server, then raises KicadUnavailable (never retries silently)
    async def save(self, doc: DocKind) -> None          # SaveDocument; refuses if read_only
    async def save_copy(self, doc: DocKind, dest: Path) -> Path   # SaveCopyOfDocument
    async def restart(self) -> None
    async def close(self) -> None
    restart_count: int

class KicadSessionManager:
    def __init__(self, settings: Settings, launcher: ServerLauncher | None = None)
    async def open(self, project: Project) -> KicadSession   # closes any other open session first
    def current(self) -> KicadSession | None
    async def close_all(self) -> None

class ServerLauncher(Protocol):       # real: spawns `kicad-cli api-server …`; tests: fake
    async def start(self, project: Project, socket_path: Path) -> asyncio.subprocess.Process
```

Socket path: `/tmp/copperchat/<project.id>.sock` (directory created 0700).

### C5. Tool spec + registry — `copperchat.tools` (Phase 3)

```python
@dataclass(frozen=True)
class ImageBlock:
    mime: str                  # "image/png"
    data: bytes
    path: Path | None = None   # where it was also written (for chat thumbnails)

@dataclass
class ToolResult:
    data: dict[str, Any] | None = None
    images: list[ImageBlock] = field(default_factory=list)

class StructuredToolError(CopperChatError):
    def __init__(self, what: str, why: str, suggestion: str): ...
    def to_dict(self) -> dict[str, str]     # {"what","why","suggestion"}

@dataclass
class ToolContext:
    project: Project
    session: KicadSession
    settings: Settings
    run_id: str | None = None
    on_mutation: Callable[[], Awaitable[None]] | None = None   # Phase 7 wires history snapshot

@dataclass(frozen=True)
class ToolSpec:
    name: str                              # snake_case, unique
    description: str
    input_schema: dict[str, Any]           # JSON Schema object
    handler: Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]
    mutating: bool = False                 # foundation: only test doubles set True
    changes_documents: bool = False

# copperchat.tools.registry
def all_specs() -> list[ToolSpec]
```

`registry.all_specs()` concatenates module-level `TOOL_SPECS: list[ToolSpec]`
from a fixed provider list in `registry.py`. Each phase that adds tools
appends one import line there (Phase 4: `kicad_tools`, `views`; Phase 7:
`git_tools`; Phase 8: `research`). A provider module that fails to import is
an error, never skipped.

### C6. Views — `copperchat.views` (Phase 4)

```python
async def write_kicanvas_snapshot(session: KicadSession, doc: DocKind) -> Path
    # SaveCopyOfDocument into <project>/.kicad-agent/view/<stem>.<ext>; returns path
def snapshot_path(project: Project, doc: DocKind) -> Path

@dataclass(frozen=True)
class Region:                    # either a mm box or "around REF"
    x_mm: float | None = None; y_mm: float | None = None
    w_mm: float | None = None; h_mm: float | None = None
    around_ref: str | None = None; margin_mm: float = 5.0

async def render_view(session: KicadSession, doc: DocKind, mode: Literal["agent", "kicad"],
                      region: Region | None = None, layers: list[str] | None = None,
                      theme: Literal["agent", "light", "dark"] = "agent") -> ImageBlock
def net_color(net_name: str, theme: str) -> tuple[int, int, int]   # deterministic hash
```

Agent-view PNGs are also written to `<project>/.kicad-agent/renders/<uuid>.png`
and served at `GET /api/projects/{id}/renders/{file}`.

### C7. Agent runtime + protocol — `copperchat.agent` (Phase 5)

Message DTO (REST and WS, camelCase on the wire):

```jsonc
{
  "id": 42, "projectId": "a1b2c3d4e5f6", "clientMessageId": "uuid-or-null",
  "runId": "uuid-or-null",
  "role": "user" | "assistant" | "system",
  "kind": "text" | "thinking" | "tool_use" | "tool_result" | "image" | "error" | "notice",
  "content": "string",
  "status": "queued" | "streaming" | "done" | "cancelled" | "error",
  "metadata": { /* tool name/input, image urls, attachments, usage … */ },
  "createdAt": "ISO-8601"
}
```

WebSocket `/api/ws` frames — every frame has `type` and `projectId`
(`ping`/`pong` carry `projectId: null`):

| Direction | `type` | Extra fields |
|---|---|---|
| C→S | `subscribe` | `sinceMessageId: int` |
| C→S | `send` | `clientMessageId: str, text: str, attachments: AttachmentRef[]` |
| C→S | `interrupt` | `runId: str \| null` |
| C→S | `cancel-queued` | `messageId: int` |
| C→S | `ping` | — |
| S→C | `message` | `message: MessageDTO` |
| S→C | `message-delta` | `messageId: int, contentDelta?: str, statusPatch?: str, metadataPatch?: object` |
| S→C | `run-started` | `runId: str` |
| S→C | `run-finished` | `runId: str, reason: "completed" \| "interrupted" \| "error" \| "rate_limited"` |
| S→C | `view-updated` | `docs: ("sch"\|"pcb")[], version: int` |
| S→C | `usage` | `usage: {inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens, costUsd?: number, rateLimit?: {status, resetsAt?: str}}` |
| S→C | `rate-limited` | `resetsAt: str` |
| S→C | `error` | `code: str, message: str, clientMessageId?: str` |
| S→C | `pong` | — |

`AttachmentRef = {id: str, kind: "image" | "pdf", name: str}` (ids from
Phase 8 upload endpoint; Phase 5 accepts an empty list and rejects unknown
ids with `error{code:"unknown_attachment"}`).

Backend models: `copperchat.agent.protocol` (pydantic, `alias_generator=to_camel`,
discriminated union on `type`). Frontend mirror: `frontend/src/protocol/frames.ts`
(zod). Shared fixtures: `protocol-fixtures/<type>.json`, each validated by
both test suites.

Runtime surface:

```python
class AgentBackend(Protocol):          # real SDK adapter or FakeAgent
    def start(self, project: Project, input_stream: AsyncIterator[QueuedItem],
              resume_session_id: str | None) -> AsyncIterator[AgentEvent]
    async def interrupt(self) -> None

class RunEngine:                        # one per project
    async def submit(self, client_message_id: str, text: str, attachments: list[AttachmentRef]) -> MessageDTO
    async def cancel_queued(self, message_id: int) -> bool
    async def interrupt(self, run_id: str | None) -> None
    hooks: RunHooks                     # on_turn_start / on_turn_end / on_first_mutation — Phase 7 fills

class Broadcaster:                      # fan-out of S→C frames per project
    async def publish(self, project_id: str, frame: ServerFrame) -> None
    def subscribe(self, project_id: str) -> AsyncIterator[ServerFrame]

def build_sdk_tools(specs: list[ToolSpec], ctx_factory: Callable[[], ToolContext]) -> McpSdkServerConfig
```

`AgentEvent` (internal, adapter → engine) is a tagged union:
`TurnStarted, TextDelta(text), ThinkingDelta(text), ToolUse(id,name,input),
ToolResultEvt(id, content, images, is_error), Usage(...), RateLimited(resets_at),
TurnFinished(reason), SessionId(id)`.

### C7a. Contract additions introduced by the phase files

These extend C1–C8 and are equally binding.

- **C1 Settings (Phase 3):** `cookie_secure: bool = True`,
  `login_max_failures: int = 5`, `login_window_s: float = 60.0`,
  `frontend_dist: Path = Path("/app/frontend/dist")`.
- **App factory (Phase 3):** `create_app(settings, *, launcher=None, client_factory=None)`;
  `app.state.settings`, `app.state.db`, `app.state.sessions`
  (`KicadSessionManager`), `app.state.serializer`, `app.state.login_limiter`;
  `app.state.agent_runtime` (`AgentRuntime`, Phase 5, with `hooks_factory`
  and `backend_factory`).
- **C2 (Phase 3):** a `schema_migrations` table records applied migration
  names in addition to `PRAGMA user_version`, so a migration merged out of
  numeric order (e.g. Phase 8's 0004 before Phase 7's 0003) is still applied.
- **C4 (Phase 3):** `KicadSessionManager(settings, launcher=None, *, client_factory=None)`,
  `ClientFactory = Callable[[str, int], Any]`, `KicadSession.view_version: int`,
  `.socket_path`, `.process`, `socket_path_for(project)`. Schematic access uses
  `Schematic(client, doc)` from `get_open_documents(DOCTYPE_SCHEMATIC)` (kipy
  has no `get_schematic()`); kipy errors are `kipy.errors.ConnectionError` /
  `ApiError`.
- **Errors (Phase 3):** `DocumentNotOpen`, `ReadOnlyProject`,
  `InvalidProjectId`, `ToolRegistryError`; `web.auth.user_from_cookie(state, cookie)`,
  `web.auth.authenticate_connection(conn) -> str | None` (Phase 5).
- **C5 (Phase 5):** `ToolContext.user_message: str | None`,
  `ToolContext.on_documents_changed`, `ToolContext.on_kicad_unavailable`;
  `ToolResult.changed_docs: set[DocKind]`. `registry._PROVIDERS` is a tuple
  of module names (`copperchat.kicad_tools.specs`, `copperchat.views.specs`,
  `copperchat.git_tools.specs`, `copperchat.research.specs`).
- **C7 (Phase 5):** server heartbeat frame `ping` (S→C, `projectId: null`);
  `error.projectId` may be `null` (unparseable client frame);
  `RunHooks.on_engine_start / on_turn_start / on_first_mutation / on_turn_end(turn, outcome)`
  with `TurnInfo`, `TurnOutcome`; `RunEngine.submit_ex`, `post_notice`,
  `post_error`, `add_agent_note`, `replay_and_subscribe`, `to_dto`.
  Protocol fixture file format: `{"type", "directions", "valid": [...], "invalid": [...]}`.
- **C7 (Phase 9):** C→S frame `visibility{projectId, visible: bool}` (push is
  sent only when no client is visible).
- **Message metadata keys:** `tool_use` → `{name, input}`; `tool_result` →
  `{isError, images: [{url}]}`; `image` → `{url}`; notices may carry
  `{action: "restore", snapshotId}` (Phase 7).
- **Frontend (Phase 6A):** `@/api/http` (`apiGet`, `apiPost`, `apiPut`,
  `apiDelete`, `apiUrl`, `setUnauthorizedHandler`, `ApiError`); `@/api/ws`
  (`createWsClient`, `wsClient`, `ConnectionState`); `@/stores/chat`
  (`createChatStore`); `@/hooks/useChat` (`useChat(projectId)`,
  `ChatStoreContext`); `@/stores/composer` (`useComposer` with `insertText`,
  `addAttachment`, `removeAttachment`, `clear`);
  `@/components/chat/composerSlots` (`composerAttachmentSlot.register(Component)`,
  slot props `{projectId, attachments, setAttachments}`); `@/layouts/panels`
  (`registerPanel({id, title, icon, component, order?})`, `usePanels()`;
  panel components get `{projectId}`; icons are lucide-react components;
  panel modules are imported from `frontend/src/panels/index.ts`; reserved
  ids `history`, `git`, `skills`, `parts`); `@/stores/ui`
  (`useUi().resolvedTheme`); `@/stores/projects` (`useProjects().current`).
  Stubs owned by later phases: `ViewerSlot` (6B), `NotificationsSection` (9).
- **Phase 2:** `scripts/kicad-base-smoke.sh <image>`; `docker/kicad-base/{KICAD_COMMIT,LIBS_COMMITS,SUPPORTED_COMMITS,smoke_ping.py}`;
  base image has kipy in `/opt/venv`, KiCad demos in `/usr/share/kicad/demos`,
  user `copperchat` (uid 1000); tags `:<commit>` and `:<first 10 chars>`.
  Spike 1C records a "Schematic accessor" field used by `KicadHandle.schematic()`.
- **Phase 7:** `app.state.history` (`HistoryService`), `install_history(app)`;
  `GET /projects/{id}/history/{snapshotId}/preview` → PNG; errors
  `InvalidSnapshotId` (400), `SnapshotNotFound` (404), `AgentBusy` (409);
  notice `action` values `restore`, `restored`, `external-change`.
- **Phase 8:** table `uploads`; `copperchat.agent.attachments.{attachment_exists, build_user_content}`;
  `copperchat.skills.assembly.{SkillBundle, assemble_skills, apply_skills}`.
- **HTTP additions:** `GET /auth/me` → `{username}`; `GET /skills/{name}`,
  `POST /skills/{name}/acknowledge-update`, `GET /projects/{id}/uploads/{uploadId}` (8);
  `GET /projects/{id}/hit-test` (6B fallback only); `agent-view` takes
  `layers` comma-separated.

### Owner review requested

Provisional decisions taken while planning (2026-10-16); the owner may
override them in the plan PR review:

1. **`research_part`** runs a separate one-shot SDK query with the
   `part-researcher` configuration. The main agent keeps `tools=[]` (no
   built-in `Agent` tool, no `agents=`), because SDK sub-agents are only
   reachable through the built-in `Agent` tool.
2. **Skills** may require the main agent to have the built-in `Skill` tool
   (`tools=["Skill"]`); it is neither a file, shell nor web tool.
3. **Discovery unit tests** may create empty `*.kicad_pro` marker files in
   `tmp_path`; every test that reads KiCad content uses only KiCad- or
   `kicad-cli`-generated fixtures.

### C8. HTTP API (owning phase in brackets; all under `/api`, all except
login require the session cookie)

- `POST /auth/login {username,password}` → 204 + cookie; `POST /auth/logout`; `GET /auth/me` [3]
- `GET /health` (no auth) → `{"status":"ok","kicad":"<version or null>"}` [3]
- `GET /projects` → `[{id,name,relPath,hasSch,hasPcb,locked}]` [3]
- `POST /projects/{id}/open` → `{id, readOnly, lockFiles, kicadVersion}` [3]
- `GET /projects/{id}/messages?limit=50&before=<id>` → `{messages, hasMore}` [5]
- `GET /projects/{id}/view/{doc}` → KiCanvas snapshot file, `ETag` = view version [4]
- `GET /projects/{id}/agent-view?doc=&theme=&layers=` → PNG [4]
- `GET /projects/{id}/renders/{file}` → PNG [4]
- `GET /projects/{id}/history`, `POST /projects/{id}/history/{snapshotId}/restore` [7]
- `GET /projects/{id}/git/status` [7]
- `GET /skills?projectId=`, `POST /skills/{name}/customise`, `PUT /skills/user/{name}`, `POST /skills/{name}/reset`, `PUT /projects/{id}/skills/{name}` `{enabled}` [8]
- `GET /projects/{id}/parts`, `GET|PUT /projects/{id}/parts/{mpn}/notes`, `GET /projects/{id}/parts/{mpn}/datasheet` [8]
- `POST /projects/{id}/uploads` (multipart) → `AttachmentRef` [8]
- `GET /push/vapid-public-key`, `POST /push/subscribe`, `DELETE /push/subscribe` [9]

---

## Phase Conventions (apply to every phase)

- **Branch/PR:** one PR per phase against `main` of `peterus/copperchat`,
  title `foundation phase <N>: <short goal>`. Use `gh` CLI (the GitHub MCP
  plugin is broken). PR body ends with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`; commits
  end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **TDD:** every task: failing test → run (see it fail) → minimal code → run
  (pass) → commit. Commit per task with conventional prefixes
  (`feat:`, `test:`, `ci:`, `docs:`, `chore:`).
- **Commands** (from repo root):
  - backend: `cd backend && uv sync && uv run pytest -m "not kicad" && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
  - backend KiCad tests: `cd backend && uv run pytest -m kicad` (needs
    `kicad-cli` with `api-server` on `PATH`, i.e. inside the base image or with
    the nightly AppImage from Phase 1C's doc)
  - frontend: `cd frontend && npm ci && npm run typecheck && npm run lint && npm test -- --run && npm run build`
- **CI expectation:** the PR's checks are green. Jobs that need KiCad run in
  `ghcr.io/peterus/copperchat-kicad-base:<KICAD_COMMIT>` once Phase 2 is
  merged; before that, the `kicad` job is absent (not "skipped green").
- **Review loop:** after CI is green, request Copilot
  (`gh api /repos/peterus/copperchat/pulls/<PR>/requested_reviewers -X POST -f 'reviewers[]=copilot-pull-request-reviewer[bot]'`)
  and run the `copilot-loop` recipe to zero (fallback: `review-loop`).
- **Done report:** `kontor_send_to_session(<coordinator>, status:'done')` with
  PR number, CI status, review residuals, and any deviation from this plan
  (deviations from an Interface Contract must also be reflected in this index
  in the same PR).
- **Kontor reference code** (read-only, owner's machine): `~/CloudCLI/kontor`.
  Port patterns, not code verbatim; Kontor is TypeScript, ours is Python on
  the backend.
- **No placeholders in merged code**: no `TODO`/`pass`-bodies for features the
  phase owns. Stubs explicitly owned by a later phase are allowed only where
  this plan says so.
