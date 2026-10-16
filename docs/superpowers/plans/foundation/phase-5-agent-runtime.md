# Phase 5: Agent runtime

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this phase task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-project agent runtime modelled on Kontor: messages are persisted
`queued` first (idempotent on `clientMessageId`), fed through a push stream into
one long-lived Claude Agent SDK session per burst, streamed to clients over a
schema-validated WebSocket, with queue cancel, interrupt (with abort backstop),
session resume, rate-limit parking/auto-resume, usage reporting, a tool adapter
from our `ToolSpec` registry to SDK MCP tools, and `RunHooks` extension points
that Phase 7 fills.

**Depends on:** 1A (decision in `docs/spikes/1a-sdk-streaming.md`), 3.

**Parallel with:** 2, 4.

**Read first:** index `docs/superpowers/plans/2026-10-15-foundation-plan.md`
(Global Constraints, Review Focus 1, Interface Contracts C1, C2, C3, C4, C5, C7,
C8); spec `docs/superpowers/specs/2026-10-15-foundation-design.md` §5, §5.1,
§5.2 (client expectations the server must serve), §10 (rate limit, token
rows), §11; `docs/spikes/1a-sdk-streaming.md` (Decision section). Reference
(read-only): `~/CloudCLI/kontor/server/runEngine.ts`,
`server/runEngine/inputStream.ts`, `server/wsChat.ts`.

**PR title:** `foundation phase 5: agent runtime, queue, WebSocket protocol`

**Files:**

- Create: `backend/src/copperchat/db/migrations/0002_chat.sql`
- Create: `backend/src/copperchat/db/repos/agent_state.py`, `backend/src/copperchat/db/repos/messages.py`
- Create: `backend/src/copperchat/agent/__init__.py`, `protocol.py`, `events.py`, `sdk_tools.py`, `sdk_backend.py`, `push_stream.py`, `fake.py`, `broadcaster.py`, `engine.py`, `runtime.py` (all under `backend/src/copperchat/agent/`)
- Create: `backend/src/copperchat/web/routes_messages.py`, `backend/src/copperchat/web/ws.py`
- Create: `protocol-fixtures/*.json` (14 files, one per frame type)
- Modify: the module defining `ToolContext` / `ToolResult` (`copperchat.tools`, Phase 3), `backend/src/copperchat/web/auth.py`, `backend/src/copperchat/web/app.py`, `backend/src/copperchat/web/routes_projects.py`
- Test: `backend/tests/unit/agent/…`, `backend/tests/unit/db/test_messages_repo.py`, `backend/tests/unit/db/test_agent_state_repo.py`, `backend/tests/unit/web/test_ws.py`, `backend/tests/unit/web/test_routes_messages.py`

**Acceptance criteria:**

- Migration `0002_chat.sql` creates `messages`, `runs`, `agent_sessions`, `rate_limit_state` exactly as index C2 (plus indexes).
- Sending the same `clientMessageId` twice yields one row, one queue entry, one agent turn; the second send returns the original row (Review Focus 1).
- `GET /api/projects/{id}/messages?limit=&before=` pages newest-first with `limit+1` → `hasMore`, returns rows in ascending id order, and overlays live streaming content.
- Every frame type in C7 has a pydantic model and a fixture in `protocol-fixtures/`; every fixture is validated by a test; frames are serialised camelCase with `projectId` on every frame.
- `PushStream` reproduces Kontor's semantics (queue-first drain, reopen on push after close request, `drop`, `pending_count`, push-after-closed returns `False`).
- The engine pulls the next queued message only after the previous turn finished (gated input, independent of how eagerly the SDK reads its input iterable).
- Streamed text is persisted at least every 500 ms or 2048 chars and at segment end; thinking, tool use, tool result and images are separate rows.
- Interrupt cancels queued messages, closes the input stream, calls `backend.interrupt()` and cancels the burst task after a grace period (stuck Stop test).
- Cancelling a queued message after it was pulled returns `False` and the message proceeds.
- The SDK session id is stored per project and passed as `resume` (with `fork_session=True`) only at burst start.
- A rate-limit event parks the in-flight and queued messages, persists `resets_at`, publishes `rate-limited`, and auto-resumes via an asyncio timer (also after an app restart).
- A `KicadUnavailable` raised inside a tool ends the turn with an error row and the next queued message still runs.
- WebSocket `/api/ws`: cookie auth, Origin check, schema validation, replay of rows with id > `sinceMessageId` (with live content), `run-started` re-sent for an active run, `rate-limited` re-sent while parked, heartbeat `ping` every 25 s.
- Reconnect mid-run: a second socket subscribing with `sinceMessageId` receives the half-streamed row in full, and the subsequent deltas complete it without gaps or duplicates.
- SDK adapter: `tools=[]`, only `mcp__copperchat__*` tools allowed, `can_use_tool` denies everything else, CopperChat system prompt (no "Claude Code" branding), token passed via `env`, `include_partial_messages=True`.
- `dev_fake_agent=True` selects `FakeAgent`; no token needed.

**Verification commands:**

```bash
cd backend && uv sync && uv run pytest -m "not kicad" && uv run ruff check . && uv run ruff format --check . && uv run mypy src
```

**CI expectations:** the existing `backend` job (Phase 3) is green; no new job.
No test in this phase needs KiCad or network access; the SDK is never started
for real in CI (fake client / `FakeAgent`).

**Assumptions about Phase 3 (verify when you start; adapt names, not behaviour):**

- `Database.execute` commits when not inside `transaction()` and lets
  `sqlite3.IntegrityError` propagate unchanged; rows are `aiosqlite.Row`
  (mapping access by column name).
- `create_app` stores `Settings` in `app.state.settings`, `Database` in
  `app.state.db`, `KicadSessionManager` in `app.state.kicad_sessions`, created
  in the FastAPI lifespan. If Phase 3 chose other attribute names, use those in
  Task 5.13 — nothing else in this phase reads `app.state` directly.
- pytest-asyncio runs in `asyncio_mode = "auto"`; async fixtures use
  `pytest_asyncio.fixture`.
- `CopperChatError(message: str)` behaves like `Exception(message)`.
- `ToolContext` and `ToolResult` live in `copperchat.tools` (file
  `backend/src/copperchat/tools/spec.py` re-exported from `tools/__init__.py`;
  if Phase 3 put them directly in `tools/__init__.py`, modify that file).

**Design note — gated input (why the 1A outcome does not change the queue).**
The Python SDK's `stream_input` writes each item to the CLI as soon as the
input iterable yields it. CopperChat therefore never hands the SDK a raw
queue: the engine wraps its `PushStream` in a *gated* async generator that
pulls the next item only after the previous turn's `TurnFinished` has been
processed. "Pulled" in this phase always means "the engine started that
message's turn"; until then the message is `queued` and cancellable. Spike 1A
decides whether one long-lived SDK client per burst works at all (Task 5.4,
default) or whether each turn needs its own client resumed from the previous
turn (Task 5.4b). Both variants implement the same `AgentBackend` protocol and
see the same gated input.

---

## Interfaces

**Consumes** (exact, from earlier phases): `Database` (C2), `Project`,
`agent_dir` (C3), `KicadSession`, `KicadSessionManager`, `KicadUnavailable`,
`DocKind` (C4), `ToolSpec`, `ToolContext`, `ToolResult`, `ImageBlock`,
`StructuredToolError`, `registry.all_specs()` (C5), `Settings` (C1),
`require_user` (Phase 3 auth dependency), `create_app(settings)`.

**Produces** (binding for Phases 6A, 7, 8, 9 — new names marked NEW):

```python
# copperchat.db.repos.messages
def utcnow() -> str                                             # ISO-8601 UTC, ms, "Z"
@dataclass(frozen=True) class MessageRow: id, project_id, client_message_id, run_id, role, kind,
    content, status, metadata: dict, created_at, updated_at;  def to_wire(self) -> dict
class MessageRepo:
    async def insert_user(self, project_id, client_message_id, text, metadata=None) -> tuple[MessageRow, bool]
    async def insert(self, project_id, *, role, kind, content="", status="done", run_id=None,
                     metadata=None, client_message_id=None) -> int
    async def get(self, message_id) -> MessageRow | None
    async def page(self, project_id, *, limit, before=None) -> tuple[list[MessageRow], bool]
    async def since(self, project_id, since_id) -> list[MessageRow]
    async def set_content(self, message_id, content) -> None
    async def set_status(self, message_id, status) -> None
    async def assign_run(self, message_id, run_id, status) -> None
    async def merge_metadata(self, message_id, patch) -> dict
    async def ids_by_status(self, project_id, status) -> list[int]
    async def queued_user_rows(self, project_id) -> list[MessageRow]
    async def finalize_streaming(self, project_id) -> None
# copperchat.db.repos.agent_state
class RunRepo: start(run_id, project_id); finish(run_id, reason, usage); get(run_id) -> dict | None
class AgentSessionRepo: get(project_id) -> str | None; set(project_id, sdk_session_id)
class RateLimitRepo: get(project_id) -> datetime | None; set(project_id, resets_at | None)

# copperchat.agent.protocol — pydantic models for every C7 frame, plus:
client_frame_adapter: TypeAdapter[ClientFrame]; server_frame_adapter: TypeAdapter[ServerFrame]
def dump_frame(frame: ServerFrame | ClientFrame) -> str
ServerPingFrame  # NEW: S→C {"type":"ping","projectId":null} heartbeat every 25 s (client ignores or answers ping)
ErrorFrame.project_id: str | None   # NEW: null when the offending frame had no valid projectId

# copperchat.agent.events — QueuedItem, AgentEvent union (C7), AgentBackend (C7)
#   Usage gains rate_limit_status / rate_limit_resets_at (NEW fields); TurnFinished gains error: str | None

# copperchat.agent.engine
class TurnInfo: run_id, message_id, text, metadata                      # NEW
class TurnOutcome: reason, changed_docs: frozenset[DocKind], mutated, kicad_failed   # NEW
class RunHooks:                                                          # C7; methods NEW-exact
    async def on_engine_start(self) -> None
    async def on_turn_start(self, turn: TurnInfo) -> None
    async def on_first_mutation(self, turn: TurnInfo) -> None
    async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None
class RunEngine:  # C7 surface plus (NEW):
    async def submit_ex(self, client_message_id, text, attachments, metadata=None) -> tuple[MessageDTO, bool]
    async def post_notice(self, text: str, metadata: dict | None = None) -> MessageDTO
    async def post_error(self, text: str, metadata: dict | None = None) -> MessageDTO
    def add_agent_note(self, text: str) -> None      # prefixed to the next turn's prompt
    async def replay_and_subscribe(self, since_message_id: int) -> tuple[list[MessageDTO], Subscription, str | None]
    def to_dto(self, row: MessageRow) -> MessageDTO   # overlays live streaming content
    is_busy: bool; current_run_id: str | None; rate_limited_until: datetime | None; backend: AgentBackend
    async def start(self) -> None; async def close(self) -> None
    attachment_resolver: Callable[[str], AttachmentRef | None]   # Phase 8 replaces the default (always None)

# copperchat.agent.runtime
class AgentRuntime:  # app.state.agent_runtime (NEW)
    settings, db, broadcaster, sessions
    backend_factory: Callable[[Callable[[], ToolContext]], AgentBackend]
    hooks_factory: Callable[[Project, RunEngine], RunHooks]          # Phase 7 sets it
    def get(self, project_id: str) -> RunEngine | None
    async def ensure(self, project: Project) -> RunEngine
    async def close_all(self) -> None

# copperchat.agent.sdk_tools
SERVER_NAME = "copperchat"; def sdk_tool_name(name: str) -> str   # "mcp__copperchat__<name>"
def build_sdk_tool_list(specs, ctx_factory) -> list[SdkMcpTool[Any]]
def build_sdk_tools(specs, ctx_factory) -> McpSdkServerConfig      # C7
def mcp_result_to_event(tool_use_id: str, raw: dict) -> ToolResultEvt

# copperchat.tools (C5 additions, NEW fields with defaults)
ToolContext.user_message: str | None = None
ToolContext.on_documents_changed: Callable[[set[DocKind]], None] | None = None
ToolContext.on_kicad_unavailable: Callable[[Exception], None] | None = None
ToolResult.changed_docs: set[DocKind] = field(default_factory=set)

# copperchat.web.auth
def authenticate_connection(conn: HTTPConnection) -> str | None   # NEW; username or None

# protocol-fixtures/<type>.json format (NEW, shared with Phase 6A):
# {"type": "<frame type>", "directions": ["c2s"|"s2c", ...], "valid": [frame, ...], "invalid": [frame, ...]}
# valid frames are canonical: dump(parse(frame)) == frame.
```

---

### Task 5.1: Migration 0002 and run/session/rate-limit repos

**Files:**
- Create: `backend/src/copperchat/db/migrations/0002_chat.sql`
- Create: `backend/src/copperchat/db/repos/agent_state.py`
- Test: `backend/tests/unit/db/test_agent_state_repo.py`

**Interfaces:**
- Consumes: `Database.open`, `execute`, `fetchone`.
- Produces: tables `messages`, `runs`, `agent_sessions`, `rate_limit_state`; `RunRepo`, `AgentSessionRepo`, `RateLimitRepo`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/db/test_agent_state_repo.py
from datetime import UTC, datetime

import pytest_asyncio

from copperchat.db import Database
from copperchat.db.repos.agent_state import AgentSessionRepo, RateLimitRepo, RunRepo


@pytest_asyncio.fixture
async def db(tmp_path):
    d = await Database.open(tmp_path / "t.db")
    yield d
    await d.close()


async def test_chat_tables_exist(db):
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type IN ('table','index')")
    names = {r["name"] for r in rows}
    assert {"messages", "runs", "agent_sessions", "rate_limit_state", "ux_messages_client"} <= names


async def test_run_start_and_finish(db):
    runs = RunRepo(db)
    await runs.start("r1", "a1b2c3d4e5f6")
    await runs.finish("r1", "completed", {"inputTokens": 3})
    run = await runs.get("r1")
    assert run is not None
    assert run["reason"] == "completed"
    assert run["usage"] == {"inputTokens": 3}
    assert run["finished_at"] is not None


async def test_agent_session_upsert(db):
    repo = AgentSessionRepo(db)
    assert await repo.get("p") is None
    await repo.set("p", "s1")
    await repo.set("p", "s2")
    assert await repo.get("p") == "s2"


async def test_rate_limit_roundtrip_and_clear(db):
    repo = RateLimitRepo(db)
    when = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    await repo.set("p", when)
    assert await repo.get("p") == when
    await repo.set("p", None)
    assert await repo.get("p") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/db/test_agent_state_repo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'copperchat.db.repos.agent_state'`.

- [ ] **Step 3: Write minimal implementation**

```sql
-- backend/src/copperchat/db/migrations/0002_chat.sql
CREATE TABLE messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id        TEXT NOT NULL,
    client_message_id TEXT NULL,
    run_id            TEXT NULL,
    role              TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    kind              TEXT NOT NULL CHECK (kind IN ('text', 'thinking', 'tool_use', 'tool_result', 'image', 'error', 'notice')),
    content           TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL CHECK (status IN ('queued', 'streaming', 'done', 'cancelled', 'error')),
    metadata          TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_messages_client ON messages(project_id, client_message_id)
    WHERE client_message_id IS NOT NULL;
CREATE INDEX ix_messages_project_id ON messages(project_id, id);
CREATE INDEX ix_messages_project_status ON messages(project_id, status);

CREATE TABLE runs (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT NULL,
    reason      TEXT NULL,
    usage       TEXT NULL
);
CREATE INDEX ix_runs_project ON runs(project_id, started_at);

CREATE TABLE agent_sessions (
    project_id     TEXT PRIMARY KEY,
    sdk_session_id TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE rate_limit_state (
    project_id TEXT PRIMARY KEY,
    resets_at  TEXT NULL
);
```

```python
# backend/src/copperchat/db/repos/agent_state.py
"""Runs, SDK session ids and rate-limit state per project."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from copperchat.db import Database
from copperchat.db.repos.messages import utcnow


class RunRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(self, run_id: str, project_id: str) -> None:
        await self._db.execute(
            "INSERT INTO runs (id, project_id, started_at) VALUES (?, ?, ?)",
            (run_id, project_id, utcnow()),
        )

    async def finish(self, run_id: str, reason: str, usage: dict[str, Any] | None) -> None:
        await self._db.execute(
            "UPDATE runs SET finished_at = ?, reason = ?, usage = ? WHERE id = ?",
            (utcnow(), reason, json.dumps(usage) if usage is not None else None, run_id),
        )

    async def get(self, run_id: str) -> dict[str, Any] | None:
        row = await self._db.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
        if row is None:
            return None
        out = dict(row)
        out["usage"] = json.loads(out["usage"]) if out["usage"] else None
        return out


class AgentSessionRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, project_id: str) -> str | None:
        row = await self._db.fetchone(
            "SELECT sdk_session_id FROM agent_sessions WHERE project_id = ?", (project_id,)
        )
        return None if row is None else str(row["sdk_session_id"])

    async def set(self, project_id: str, sdk_session_id: str) -> None:
        await self._db.execute(
            "INSERT INTO agent_sessions (project_id, sdk_session_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET sdk_session_id = excluded.sdk_session_id, "
            "updated_at = excluded.updated_at",
            (project_id, sdk_session_id, utcnow()),
        )


class RateLimitRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, project_id: str) -> datetime | None:
        row = await self._db.fetchone(
            "SELECT resets_at FROM rate_limit_state WHERE project_id = ?", (project_id,)
        )
        if row is None or row["resets_at"] is None:
            return None
        return datetime.fromisoformat(str(row["resets_at"]))

    async def set(self, project_id: str, resets_at: datetime | None) -> None:
        await self._db.execute(
            "INSERT INTO rate_limit_state (project_id, resets_at) VALUES (?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET resets_at = excluded.resets_at",
            (project_id, resets_at.isoformat() if resets_at is not None else None),
        )
```

`agent_state.py` imports `utcnow` from `messages.py`, so create the
`messages.py` module in this step with only `utcnow` (Task 5.2 extends it):

```python
# backend/src/copperchat/db/repos/messages.py
"""Chat message persistence."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/db/test_agent_state_repo.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/db/migrations/0002_chat.sql backend/src/copperchat/db/repos/agent_state.py backend/src/copperchat/db/repos/messages.py backend/tests/unit/db/test_agent_state_repo.py
git commit -m "feat: chat schema migration and run/session/rate-limit repos" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.2: Message repository with idempotent insert and paging

Owns **Review Focus 1** (server side): a retransmitted `clientMessageId`
returns the same row, creates no second row and no second queue entry (the
queue half is asserted again in Task 5.9).

**Files:**
- Modify: `backend/src/copperchat/db/repos/messages.py`
- Test: `backend/tests/unit/db/test_messages_repo.py`

**Interfaces:**
- Consumes: `Database`.
- Produces: `MessageRow`, `MessageRepo` (signatures in Interfaces above).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/db/test_messages_repo.py
import asyncio

import pytest_asyncio

from copperchat.db import Database
from copperchat.db.repos.messages import MessageRepo

P = "a1b2c3d4e5f6"


@pytest_asyncio.fixture
async def repo(tmp_path):
    db = await Database.open(tmp_path / "t.db")
    yield MessageRepo(db)
    await db.close()


async def test_insert_user_is_idempotent_on_client_message_id(repo):
    first, created1 = await repo.insert_user(P, "cmid-1", "hello")
    again, created2 = await repo.insert_user(P, "cmid-1", "hello (retransmit)")
    assert created1 is True and created2 is False
    assert again.id == first.id
    assert again.content == "hello"
    assert first.status == "queued" and first.role == "user" and first.kind == "text"
    rows, _ = await repo.page(P, limit=50)
    assert [r.id for r in rows] == [first.id]


async def test_concurrent_duplicate_inserts_yield_one_row(repo):
    results = await asyncio.gather(*(repo.insert_user(P, "same", "x") for _ in range(5)))
    assert len({row.id for row, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


async def test_same_client_id_in_other_project_is_independent(repo):
    a, _ = await repo.insert_user(P, "c", "x")
    b, created = await repo.insert_user("ffffffffffff", "c", "x")
    assert created and a.id != b.id


async def test_page_returns_ascending_and_has_more(repo):
    ids = [await repo.insert(P, role="assistant", kind="text", content=str(i)) for i in range(5)]
    rows, has_more = await repo.page(P, limit=2)
    assert [r.id for r in rows] == ids[3:]
    assert has_more is True
    older, has_more2 = await repo.page(P, limit=2, before=rows[0].id)
    assert [r.id for r in older] == ids[1:3]
    assert has_more2 is True
    oldest, has_more3 = await repo.page(P, limit=2, before=older[0].id)
    assert [r.id for r in oldest] == ids[:1]
    assert has_more3 is False


async def test_since_and_status_helpers(repo):
    m1, _ = await repo.insert_user(P, "a", "one")
    m2, _ = await repo.insert_user(P, "b", "two")
    assert [r.id for r in await repo.since(P, m1.id)] == [m2.id]
    assert await repo.ids_by_status(P, "queued") == [m1.id, m2.id]
    await repo.assign_run(m1.id, "run-1", "done")
    row = await repo.get(m1.id)
    assert row is not None and row.run_id == "run-1" and row.status == "done"
    assert [r.id for r in await repo.queued_user_rows(P)] == [m2.id]


async def test_metadata_merge_and_wire_shape(repo):
    mid = await repo.insert(P, role="assistant", kind="tool_use", metadata={"name": "x"})
    merged = await repo.merge_metadata(mid, {"input": {"a": 1}})
    assert merged == {"name": "x", "input": {"a": 1}}
    row = await repo.get(mid)
    assert row is not None
    wire = row.to_wire()
    assert set(wire) == {"id", "projectId", "clientMessageId", "runId", "role", "kind",
                         "content", "status", "metadata", "createdAt"}
    assert wire["createdAt"].endswith("Z")


async def test_finalize_streaming_marks_leftovers_done(repo):
    mid = await repo.insert(P, role="assistant", kind="text", status="streaming", content="half")
    await repo.finalize_streaming(P)
    row = await repo.get(mid)
    assert row is not None and row.status == "done" and row.metadata.get("recovered") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/db/test_messages_repo.py -v`
Expected: FAIL with `ImportError: cannot import name 'MessageRepo'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/db/repos/messages.py
"""Chat message persistence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from copperchat.db import Database

_COLS = (
    "id, project_id, client_message_id, run_id, role, kind, content, status, "
    "metadata, created_at, updated_at"
)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class MessageRow:
    id: int
    project_id: str
    client_message_id: str | None
    run_id: str | None
    role: str
    kind: str
    content: str
    status: str
    metadata: dict[str, Any]
    created_at: str
    updated_at: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "projectId": self.project_id,
            "clientMessageId": self.client_message_id,
            "runId": self.run_id,
            "role": self.role,
            "kind": self.kind,
            "content": self.content,
            "status": self.status,
            "metadata": self.metadata,
            "createdAt": self.created_at,
        }


def _row(r: Mapping[str, Any]) -> MessageRow:
    return MessageRow(
        id=int(r["id"]),
        project_id=str(r["project_id"]),
        client_message_id=r["client_message_id"],
        run_id=r["run_id"],
        role=str(r["role"]),
        kind=str(r["kind"]),
        content=str(r["content"]),
        status=str(r["status"]),
        metadata=json.loads(r["metadata"] or "{}"),
        created_at=str(r["created_at"]),
        updated_at=str(r["updated_at"]),
    )


class MessageRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, message_id: int) -> MessageRow | None:
        r = await self._db.fetchone(f"SELECT {_COLS} FROM messages WHERE id = ?", (message_id,))
        return None if r is None else _row(r)

    async def get_by_client_id(self, project_id: str, client_message_id: str) -> MessageRow | None:
        r = await self._db.fetchone(
            f"SELECT {_COLS} FROM messages WHERE project_id = ? AND client_message_id = ?",
            (project_id, client_message_id),
        )
        return None if r is None else _row(r)

    async def insert(
        self,
        project_id: str,
        *,
        role: str,
        kind: str,
        content: str = "",
        status: str = "done",
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        client_message_id: str | None = None,
    ) -> int:
        now = utcnow()
        return await self._db.execute(
            "INSERT INTO messages (project_id, client_message_id, run_id, role, kind, content, "
            "status, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, client_message_id, run_id, role, kind, content, status,
             json.dumps(metadata or {}), now, now),
        )

    async def insert_user(
        self,
        project_id: str,
        client_message_id: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[MessageRow, bool]:
        """Insert a queued user message. A retransmit of the same clientMessageId
        returns the original row and created=False (no second row, no re-queue)."""
        existing = await self.get_by_client_id(project_id, client_message_id)
        if existing is not None:
            return existing, False
        try:
            new_id = await self.insert(
                project_id, role="user", kind="text", content=text, status="queued",
                metadata=metadata, client_message_id=client_message_id,
            )
        except sqlite3.IntegrityError:
            raced = await self.get_by_client_id(project_id, client_message_id)
            if raced is None:
                raise
            return raced, False
        row = await self.get(new_id)
        if row is None:
            raise RuntimeError(f"message {new_id} vanished after insert")
        return row, True

    async def page(
        self, project_id: str, *, limit: int, before: int | None = None
    ) -> tuple[list[MessageRow], bool]:
        if before is None:
            rows = await self._db.fetchall(
                f"SELECT {_COLS} FROM messages WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                (project_id, limit + 1),
            )
        else:
            rows = await self._db.fetchall(
                f"SELECT {_COLS} FROM messages WHERE project_id = ? AND id < ? "
                "ORDER BY id DESC LIMIT ?",
                (project_id, before, limit + 1),
            )
        has_more = len(rows) > limit
        return [_row(r) for r in reversed(rows[:limit])], has_more

    async def since(self, project_id: str, since_id: int) -> list[MessageRow]:
        rows = await self._db.fetchall(
            f"SELECT {_COLS} FROM messages WHERE project_id = ? AND id > ? ORDER BY id",
            (project_id, since_id),
        )
        return [_row(r) for r in rows]

    async def set_content(self, message_id: int, content: str) -> None:
        await self._db.execute(
            "UPDATE messages SET content = ?, updated_at = ? WHERE id = ?",
            (content, utcnow(), message_id),
        )

    async def set_status(self, message_id: int, status: str) -> None:
        await self._db.execute(
            "UPDATE messages SET status = ?, updated_at = ? WHERE id = ?",
            (status, utcnow(), message_id),
        )

    async def assign_run(self, message_id: int, run_id: str, status: str) -> None:
        await self._db.execute(
            "UPDATE messages SET run_id = ?, status = ?, updated_at = ? WHERE id = ?",
            (run_id, status, utcnow(), message_id),
        )

    async def merge_metadata(self, message_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        row = await self.get(message_id)
        merged = {**(row.metadata if row else {}), **patch}
        await self._db.execute(
            "UPDATE messages SET metadata = ?, updated_at = ? WHERE id = ?",
            (json.dumps(merged), utcnow(), message_id),
        )
        return merged

    async def ids_by_status(self, project_id: str, status: str) -> list[int]:
        rows = await self._db.fetchall(
            "SELECT id FROM messages WHERE project_id = ? AND status = ? ORDER BY id",
            (project_id, status),
        )
        return [int(r["id"]) for r in rows]

    async def queued_user_rows(self, project_id: str) -> list[MessageRow]:
        rows = await self._db.fetchall(
            f"SELECT {_COLS} FROM messages WHERE project_id = ? AND role = 'user' "
            "AND status = 'queued' ORDER BY id",
            (project_id,),
        )
        return [_row(r) for r in rows]

    async def finalize_streaming(self, project_id: str) -> None:
        """Rows left 'streaming' by a crashed process keep their persisted content."""
        for r in await self._db.fetchall(
            "SELECT id FROM messages WHERE project_id = ? AND status = 'streaming'", (project_id,)
        ):
            await self.merge_metadata(int(r["id"]), {"recovered": True})
            await self.set_status(int(r["id"]), "done")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/db -v`
Expected: all passed (7 new + 4 from Task 5.1).

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/db/repos/messages.py backend/tests/unit/db/test_messages_repo.py
git commit -m "feat: message repo with idempotent insert and id paging" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.3: WebSocket protocol models and shared fixtures

**Files:**
- Create: `backend/src/copperchat/agent/__init__.py` (empty docstring module), `backend/src/copperchat/agent/protocol.py`
- Create: `protocol-fixtures/{subscribe,send,interrupt,cancel-queued,ping,message,message-delta,run-started,run-finished,view-updated,usage,rate-limited,error,pong}.json`
- Test: `backend/tests/unit/agent/test_protocol.py`

**Interfaces:**
- Produces: all frame models, `MessageDTO`, `AttachmentRef`, `UsagePayload`, `RateLimitPayload`, `ClientFrame`, `ServerFrame`, `client_frame_adapter`, `server_frame_adapter`, `dump_frame`, fixture file format (see Interfaces).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/agent/test_protocol.py
import json
import typing
from pathlib import Path

import pytest
from pydantic import ValidationError

from copperchat.agent.protocol import (
    ClientFrame, MessageDeltaFrame, PongFrame, ServerFrame, client_frame_adapter, dump_frame,
    server_frame_adapter,
)

FIXTURES = Path(__file__).resolve().parents[4] / "protocol-fixtures"
ADAPTERS = {"c2s": client_frame_adapter, "s2c": server_frame_adapter}


def _types(union: object) -> set[str]:
    members = typing.get_args(typing.get_args(union)[0])
    return {typing.get_args(m.model_fields["type"].annotation)[0] for m in members}


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_fixture_frames(path: Path) -> None:
    spec = json.loads(path.read_text())
    assert spec["type"] == path.stem
    assert spec["valid"], "every fixture needs at least one valid frame"
    for direction in spec["directions"]:
        adapter = ADAPTERS[direction]
        for frame in spec["valid"]:
            parsed = adapter.validate_python(frame)
            assert parsed.type == spec["type"]
            assert json.loads(dump_frame(parsed)) == frame, "fixtures must be canonical"
        for frame in spec["invalid"]:
            with pytest.raises(ValidationError):
                adapter.validate_python(frame)


def test_every_frame_type_has_a_fixture() -> None:
    present = {p.stem for p in FIXTURES.glob("*.json")}
    assert _types(ClientFrame) | _types(ServerFrame) == present


def test_delta_omits_absent_patches_and_pong_keeps_null_project() -> None:
    delta = MessageDeltaFrame(project_id="a1b2c3d4e5f6", message_id=3, content_delta="x")
    assert json.loads(dump_frame(delta)) == {
        "type": "message-delta", "projectId": "a1b2c3d4e5f6", "messageId": 3, "contentDelta": "x"}
    assert json.loads(dump_frame(PongFrame())) == {"type": "pong", "projectId": None}


def test_delta_requires_a_patch() -> None:
    with pytest.raises(ValidationError):
        MessageDeltaFrame(project_id="a1b2c3d4e5f6", message_id=3)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/agent/test_protocol.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'copperchat.agent'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/agent/__init__.py
"""Agent runtime: protocol, run engine, SDK adapter."""
```

```python
# backend/src/copperchat/agent/protocol.py
"""WebSocket/REST wire protocol (index contract C7). camelCase on the wire."""

from __future__ import annotations

import json
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, StringConstraints, TypeAdapter,
    model_serializer, model_validator,
)
from pydantic.alias_generators import to_camel

ProjectId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12}$")]
Role = Literal["user", "assistant", "system"]
Kind = Literal["text", "thinking", "tool_use", "tool_result", "image", "error", "notice"]
Status = Literal["queued", "streaming", "done", "cancelled", "error"]
FinishReason = Literal["completed", "interrupted", "error", "rate_limited"]


class Wire(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )
    # Optional fields that are left out of the JSON when None (all others are always emitted).
    OMIT_IF_NONE: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def _omit_none(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for name in self.OMIT_IF_NONE:
            for key in (name, to_camel(name)):
                if key in data and data[key] is None:
                    del data[key]
        return data


class AttachmentRef(Wire):
    id: str = Field(min_length=1, max_length=100)
    kind: Literal["image", "pdf"]
    name: str = Field(min_length=1, max_length=255)


class MessageDTO(Wire):
    id: int
    project_id: ProjectId
    client_message_id: str | None
    run_id: str | None
    role: Role
    kind: Kind
    content: str
    status: Status
    metadata: dict[str, Any]
    created_at: str


# ---- client → server
class SubscribeFrame(Wire):
    type: Literal["subscribe"] = "subscribe"
    project_id: ProjectId
    since_message_id: int = Field(ge=0)


class SendFrame(Wire):
    type: Literal["send"] = "send"
    project_id: ProjectId
    client_message_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=100_000)
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=20)


class InterruptFrame(Wire):
    type: Literal["interrupt"] = "interrupt"
    project_id: ProjectId
    run_id: str | None


class CancelQueuedFrame(Wire):
    type: Literal["cancel-queued"] = "cancel-queued"
    project_id: ProjectId
    message_id: int = Field(ge=1)


class PingFrame(Wire):
    type: Literal["ping"] = "ping"
    project_id: None = None


ClientFrame = Annotated[
    SubscribeFrame | SendFrame | InterruptFrame | CancelQueuedFrame | PingFrame,
    Field(discriminator="type"),
]


# ---- server → client
class MessageFrame(Wire):
    type: Literal["message"] = "message"
    project_id: ProjectId
    message: MessageDTO


class MessageDeltaFrame(Wire):
    OMIT_IF_NONE: ClassVar[frozenset[str]] = frozenset(
        {"content_delta", "status_patch", "metadata_patch"}
    )
    type: Literal["message-delta"] = "message-delta"
    project_id: ProjectId
    message_id: int
    content_delta: str | None = None
    status_patch: Status | None = None
    metadata_patch: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _one_patch(self) -> MessageDeltaFrame:
        if self.content_delta is None and self.status_patch is None and self.metadata_patch is None:
            raise ValueError("message-delta needs contentDelta, statusPatch or metadataPatch")
        return self


class RunStartedFrame(Wire):
    type: Literal["run-started"] = "run-started"
    project_id: ProjectId
    run_id: str


class RunFinishedFrame(Wire):
    type: Literal["run-finished"] = "run-finished"
    project_id: ProjectId
    run_id: str
    reason: FinishReason


class ViewUpdatedFrame(Wire):
    type: Literal["view-updated"] = "view-updated"
    project_id: ProjectId
    docs: list[Literal["sch", "pcb"]] = Field(min_length=1)
    version: int = Field(ge=0)


class RateLimitPayload(Wire):
    OMIT_IF_NONE: ClassVar[frozenset[str]] = frozenset({"resets_at"})
    status: Literal["allowed", "allowed_warning", "rejected"]
    resets_at: str | None = None


class UsagePayload(Wire):
    OMIT_IF_NONE: ClassVar[frozenset[str]] = frozenset({"cost_usd", "rate_limit"})
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_creation_tokens: int = Field(ge=0)
    cost_usd: float | None = None
    rate_limit: RateLimitPayload | None = None


class UsageFrame(Wire):
    type: Literal["usage"] = "usage"
    project_id: ProjectId
    usage: UsagePayload


class RateLimitedFrame(Wire):
    type: Literal["rate-limited"] = "rate-limited"
    project_id: ProjectId
    resets_at: str


class ErrorFrame(Wire):
    OMIT_IF_NONE: ClassVar[frozenset[str]] = frozenset({"client_message_id"})
    type: Literal["error"] = "error"
    project_id: ProjectId | None
    code: str = Field(min_length=1)
    message: str
    client_message_id: str | None = None


class ServerPingFrame(Wire):
    type: Literal["ping"] = "ping"
    project_id: None = None


class PongFrame(Wire):
    type: Literal["pong"] = "pong"
    project_id: None = None


ServerFrame = Annotated[
    MessageFrame | MessageDeltaFrame | RunStartedFrame | RunFinishedFrame | ViewUpdatedFrame
    | UsageFrame | RateLimitedFrame | ErrorFrame | ServerPingFrame | PongFrame,
    Field(discriminator="type"),
]

client_frame_adapter: TypeAdapter[ClientFrame] = TypeAdapter(ClientFrame)
server_frame_adapter: TypeAdapter[ServerFrame] = TypeAdapter(ServerFrame)


def dump_frame(frame: Wire) -> str:
    return json.dumps(
        frame.model_dump(mode="json", by_alias=True), separators=(",", ":"), ensure_ascii=False
    )
```

Fixtures (each file exactly as below):

```json
// protocol-fixtures/subscribe.json
{"type": "subscribe", "directions": ["c2s"],
 "valid": [{"type": "subscribe", "projectId": "a1b2c3d4e5f6", "sinceMessageId": 0},
           {"type": "subscribe", "projectId": "a1b2c3d4e5f6", "sinceMessageId": 42}],
 "invalid": [{"type": "subscribe", "projectId": "a1b2c3d4e5f6"},
             {"type": "subscribe", "projectId": "../../etc", "sinceMessageId": 0},
             {"type": "subscribe", "projectId": "a1b2c3d4e5f6", "sinceMessageId": -1}]}
```

```json
// protocol-fixtures/send.json
{"type": "send", "directions": ["c2s"],
 "valid": [{"type": "send", "projectId": "a1b2c3d4e5f6", "clientMessageId": "6f1c0c5e-2f39-4b8e-9a51-0d7f0e0f4a11", "text": "What is U3?", "attachments": []},
           {"type": "send", "projectId": "a1b2c3d4e5f6", "clientMessageId": "c2", "text": "see image", "attachments": [{"id": "up-1", "kind": "image", "name": "photo.png"}]}],
 "invalid": [{"type": "send", "projectId": "a1b2c3d4e5f6", "clientMessageId": "c3", "text": "", "attachments": []},
             {"type": "send", "projectId": "a1b2c3d4e5f6", "text": "no id", "attachments": []},
             {"type": "send", "projectId": "a1b2c3d4e5f6", "clientMessageId": "c4", "text": "x", "attachments": [{"id": "u", "kind": "zip", "name": "a.zip"}]}]}
```

```json
// protocol-fixtures/interrupt.json
{"type": "interrupt", "directions": ["c2s"],
 "valid": [{"type": "interrupt", "projectId": "a1b2c3d4e5f6", "runId": "0b9a7c2e-1111-4f3e-8c55-6a8d2f1e9b00"},
           {"type": "interrupt", "projectId": "a1b2c3d4e5f6", "runId": null}],
 "invalid": [{"type": "interrupt", "runId": null}]}
```

```json
// protocol-fixtures/cancel-queued.json
{"type": "cancel-queued", "directions": ["c2s"],
 "valid": [{"type": "cancel-queued", "projectId": "a1b2c3d4e5f6", "messageId": 17}],
 "invalid": [{"type": "cancel-queued", "projectId": "a1b2c3d4e5f6", "messageId": 0},
             {"type": "cancel-queued", "projectId": "a1b2c3d4e5f6", "clientMessageId": "c1"}]}
```

```json
// protocol-fixtures/ping.json
{"type": "ping", "directions": ["c2s", "s2c"],
 "valid": [{"type": "ping", "projectId": null}],
 "invalid": [{"type": "ping", "projectId": "a1b2c3d4e5f6"}]}
```

```json
// protocol-fixtures/message.json
{"type": "message", "directions": ["s2c"],
 "valid": [{"type": "message", "projectId": "a1b2c3d4e5f6", "message": {"id": 42, "projectId": "a1b2c3d4e5f6", "clientMessageId": "c1", "runId": null, "role": "user", "kind": "text", "content": "What is U3?", "status": "queued", "metadata": {}, "createdAt": "2026-10-15T12:00:00.000Z"}},
           {"type": "message", "projectId": "a1b2c3d4e5f6", "message": {"id": 43, "projectId": "a1b2c3d4e5f6", "clientMessageId": null, "runId": "r1", "role": "assistant", "kind": "tool_use", "content": "sch_get_symbol", "status": "done", "metadata": {"toolUseId": "t1", "name": "sch_get_symbol", "input": {"ref": "U3"}}, "createdAt": "2026-10-15T12:00:01.000Z"}}],
 "invalid": [{"type": "message", "projectId": "a1b2c3d4e5f6", "message": {"id": 44, "projectId": "a1b2c3d4e5f6", "role": "user", "kind": "text", "content": "x", "status": "queued", "metadata": {}, "createdAt": "2026-10-15T12:00:00.000Z"}},
             {"type": "message", "projectId": "a1b2c3d4e5f6", "message": {"id": 45, "projectId": "a1b2c3d4e5f6", "clientMessageId": null, "runId": null, "role": "robot", "kind": "text", "content": "x", "status": "done", "metadata": {}, "createdAt": "2026-10-15T12:00:00.000Z"}}]}
```

```json
// protocol-fixtures/message-delta.json
{"type": "message-delta", "directions": ["s2c"],
 "valid": [{"type": "message-delta", "projectId": "a1b2c3d4e5f6", "messageId": 43, "contentDelta": "Hel"},
           {"type": "message-delta", "projectId": "a1b2c3d4e5f6", "messageId": 43, "statusPatch": "done"},
           {"type": "message-delta", "projectId": "a1b2c3d4e5f6", "messageId": 43, "metadataPatch": {"rateLimited": true}}],
 "invalid": [{"type": "message-delta", "projectId": "a1b2c3d4e5f6", "messageId": 43},
             {"type": "message-delta", "projectId": "a1b2c3d4e5f6", "messageId": 43, "statusPatch": "parked"}]}
```

```json
// protocol-fixtures/run-started.json
{"type": "run-started", "directions": ["s2c"],
 "valid": [{"type": "run-started", "projectId": "a1b2c3d4e5f6", "runId": "r1"}],
 "invalid": [{"type": "run-started", "projectId": "a1b2c3d4e5f6"}]}
```

```json
// protocol-fixtures/run-finished.json
{"type": "run-finished", "directions": ["s2c"],
 "valid": [{"type": "run-finished", "projectId": "a1b2c3d4e5f6", "runId": "r1", "reason": "completed"},
           {"type": "run-finished", "projectId": "a1b2c3d4e5f6", "runId": "r1", "reason": "rate_limited"}],
 "invalid": [{"type": "run-finished", "projectId": "a1b2c3d4e5f6", "runId": "r1", "reason": "done"}]}
```

```json
// protocol-fixtures/view-updated.json
{"type": "view-updated", "directions": ["s2c"],
 "valid": [{"type": "view-updated", "projectId": "a1b2c3d4e5f6", "docs": ["sch", "pcb"], "version": 1760529600123}],
 "invalid": [{"type": "view-updated", "projectId": "a1b2c3d4e5f6", "docs": [], "version": 1},
             {"type": "view-updated", "projectId": "a1b2c3d4e5f6", "docs": ["3d"], "version": 1}]}
```

```json
// protocol-fixtures/usage.json
{"type": "usage", "directions": ["s2c"],
 "valid": [{"type": "usage", "projectId": "a1b2c3d4e5f6", "usage": {"inputTokens": 1200, "outputTokens": 340, "cacheReadTokens": 9000, "cacheCreationTokens": 0}},
           {"type": "usage", "projectId": "a1b2c3d4e5f6", "usage": {"inputTokens": 1, "outputTokens": 2, "cacheReadTokens": 0, "cacheCreationTokens": 0, "costUsd": 0.0123, "rateLimit": {"status": "allowed_warning", "resetsAt": "2026-10-15T17:00:00Z"}}}],
 "invalid": [{"type": "usage", "projectId": "a1b2c3d4e5f6", "usage": {"inputTokens": -1, "outputTokens": 0, "cacheReadTokens": 0, "cacheCreationTokens": 0}}]}
```

```json
// protocol-fixtures/rate-limited.json
{"type": "rate-limited", "directions": ["s2c"],
 "valid": [{"type": "rate-limited", "projectId": "a1b2c3d4e5f6", "resetsAt": "2026-10-15T17:00:00+00:00"}],
 "invalid": [{"type": "rate-limited", "projectId": "a1b2c3d4e5f6"}]}
```

```json
// protocol-fixtures/error.json
{"type": "error", "directions": ["s2c"],
 "valid": [{"type": "error", "projectId": "a1b2c3d4e5f6", "code": "unknown_attachment", "message": "unknown attachment: up-9", "clientMessageId": "c1"},
           {"type": "error", "projectId": null, "code": "invalid_frame", "message": "Field required"}],
 "invalid": [{"type": "error", "projectId": "a1b2c3d4e5f6", "code": "", "message": "x"}]}
```

```json
// protocol-fixtures/pong.json
{"type": "pong", "directions": ["s2c"],
 "valid": [{"type": "pong", "projectId": null}],
 "invalid": [{"type": "pong"}, {"type": "pong", "projectId": null, "timestamp": 1}]}
```

(The `// path` lines above are labels only — the files contain just the JSON
object. Note `{"type":"pong"}` is invalid because `projectId` is required to
be present: give `PongFrame.project_id` no default in validation by adding
`model_config` override is unnecessary — instead the test fixture relies on
`extra="forbid"` for the second case and on the following validator for the
first.) Add to `PongFrame` and `ServerPingFrame` and `PingFrame`:

```python
    @model_validator(mode="before")
    @classmethod
    def _require_project_key(cls, data: Any) -> Any:
        if isinstance(data, dict) and "projectId" not in data and "project_id" not in data:
            raise ValueError("projectId must be present (null)")
        return data
```

and construct them in code as `PongFrame(project_id=None)` /
`ServerPingFrame(project_id=None)`. Update the test line
`dump_frame(PongFrame())` to `dump_frame(PongFrame(project_id=None))`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/agent/test_protocol.py -v`
Expected: 17 passed (14 fixtures + 3).

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent/__init__.py backend/src/copperchat/agent/protocol.py protocol-fixtures backend/tests/unit/agent/test_protocol.py
git commit -m "feat: websocket protocol models and shared frame fixtures" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.4: Agent events, SDK options, event mapper, streaming SDK backend

Implements the **1A go** variant. If `docs/spikes/1a-sdk-streaming.md`
records **no-go**, implement this task *without* the `SdkStreamingBackend`
class and its two tests (`test_streaming_backend_*`), then do Task 5.4b.

**Files:**
- Create: `backend/src/copperchat/agent/events.py`, `backend/src/copperchat/agent/sdk_backend.py`
- Test: `backend/tests/unit/agent/test_sdk_backend.py`

**Interfaces:**
- Consumes: `claude_agent_sdk` (`ClaudeSDKClient`, `ClaudeAgentOptions`, message/block types, `PermissionResultAllow/Deny`, `ToolPermissionContext`), `Settings`, `Project`, `ImageBlock`.
- Produces: `QueuedItem`, `AgentEvent` members, `AgentBackend`; `SYSTEM_PROMPT`, `build_options(...)`, `make_tool_guard(allowed)`, `SdkEventMapper`, `SdkStreamingBackend`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/agent/test_sdk_backend.py
import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage, PermissionResultAllow, PermissionResultDeny, RateLimitEvent, RateLimitInfo,
    ResultMessage, StreamEvent, SystemMessage, TextBlock, ToolPermissionContext, ToolResultBlock,
    ToolUseBlock, UserMessage,
)
from pydantic import SecretStr

from copperchat.agent.events import (
    QueuedItem, RateLimited, SessionId, TextDelta, ThinkingDelta, ToolResultEvt, ToolUse,
    TurnFinished, Usage,
)
from copperchat.agent.sdk_backend import (
    SdkEventMapper, SdkStreamingBackend, build_options, make_tool_guard,
)
from copperchat.config import Settings
from copperchat.projects import Project


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "demo"
    d.mkdir()
    return Project(id="a1b2c3d4e5f6", name="demo", rel_path="demo/demo.kicad_pro", dir=d,
                   pro_file=d / "demo.kicad_pro", sch_file=None, pcb_file=None)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    return Settings(_env_file=None, projects_root=tmp_path, data_dir=tmp_path / "data",
                    claude_code_oauth_token=SecretStr("tok-123"))


def test_options_disable_builtins_and_allow_only_our_tools(settings, project):
    opts = build_options(settings=settings, project=project, mcp_server={"type": "sdk", "name": "copperchat", "instance": None},  # type: ignore[typeddict-item]
                         tool_names=["project_info", "run_erc"], resume_session_id=None, fork_session=False)
    assert opts.tools == []
    assert opts.allowed_tools == ["mcp__copperchat__project_info", "mcp__copperchat__run_erc"]
    assert set(opts.mcp_servers) == {"copperchat"}  # type: ignore[arg-type]
    assert isinstance(opts.system_prompt, str)
    assert "CopperChat" in opts.system_prompt and "Claude Code" not in opts.system_prompt
    assert opts.include_partial_messages is True
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-123"
    assert "ANTHROPIC_API_KEY" not in opts.env
    assert opts.env["CLAUDE_CONFIG_DIR"] == str(settings.data_dir / "claude")
    assert opts.setting_sources == []
    assert opts.resume is None and opts.fork_session is False
    assert opts.can_use_tool is not None


def test_options_resume_with_fork(settings, project):
    opts = build_options(settings=settings, project=project, mcp_server={"type": "sdk", "name": "copperchat", "instance": None},  # type: ignore[typeddict-item]
                         tool_names=[], resume_session_id="s-1", fork_session=True)
    assert opts.resume == "s-1" and opts.fork_session is True


async def test_tool_guard_denies_everything_not_registered():
    guard = make_tool_guard(frozenset({"mcp__copperchat__run_erc"}))
    ctx = ToolPermissionContext(tool_use_id="t1")
    assert isinstance(await guard("mcp__copperchat__run_erc", {}, ctx), PermissionResultAllow)
    for name in ("Bash", "Read", "WebFetch", "mcp__other__x"):
        res = await guard(name, {}, ctx)
        assert isinstance(res, PermissionResultDeny) and res.interrupt is False


def _stream(event: dict[str, Any]) -> StreamEvent:
    return StreamEvent(uuid="u", session_id="s1", event=event)


def test_mapper_streams_partials_and_does_not_duplicate_blocks():
    m = SdkEventMapper()
    out = []
    out += m.map(SystemMessage(subtype="init", data={"session_id": "s1"}))
    out += m.map(_stream({"type": "message_start", "message": {"id": "msg_1"}}))
    out += m.map(_stream({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}}))
    out += m.map(_stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}))
    out += m.map(AssistantMessage(content=[TextBlock(text="Hi"), ToolUseBlock(id="t1", name="mcp__copperchat__run_erc", input={})],
                                  model="m", message_id="msg_1", session_id="s1"))
    assert out == [SessionId("s1"), ThinkingDelta("hmm"), TextDelta("Hi"),
                   ToolUse(id="t1", name="mcp__copperchat__run_erc", input={})]


def test_mapper_uses_blocks_when_no_partials_were_seen():
    m = SdkEventMapper()
    out = m.map(AssistantMessage(content=[TextBlock(text="Whole")], model="m", message_id="msg_2"))
    assert out == [TextDelta("Whole")]


def test_mapper_tool_result_with_image():
    m = SdkEventMapper()
    block = ToolResultBlock(tool_use_id="t1", is_error=False, content=[
        {"type": "text", "text": "{\"ok\": true}"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "UE5H"}},
    ])
    [ev] = m.map(UserMessage(content=[block]))
    assert isinstance(ev, ToolResultEvt)
    assert ev.id == "t1" and ev.content == "{\"ok\": true}" and ev.is_error is False
    assert ev.images[0].mime == "image/png" and ev.images[0].data == b"PNG"


def _result(**kw: Any) -> ResultMessage:
    base: dict[str, Any] = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="s1", total_cost_usd=0.5,
                                usage={"input_tokens": 10, "output_tokens": 3,
                                       "cache_read_input_tokens": 7, "cache_creation_input_tokens": 1})
    base.update(kw)
    return ResultMessage(**base)


def test_mapper_result_usage_and_reasons():
    m = SdkEventMapper()
    m.map(SystemMessage(subtype="init", data={"session_id": "s1"}))
    usage, fin = m.map(_result())
    assert usage == Usage(input_tokens=10, output_tokens=3, cache_read_tokens=7,
                          cache_creation_tokens=1, cost_usd=0.5)
    assert fin == TurnFinished(reason="completed")
    assert m.map(_result(terminal_reason="aborted_streaming"))[1] == TurnFinished(reason="interrupted")
    err = m.map(_result(is_error=True, errors=["boom"]))[1]
    assert err == TurnFinished(reason="error", error="boom")


def test_mapper_rate_limit_rejected_and_status_on_usage():
    m = SdkEventMapper()
    resets = int(datetime(2026, 10, 15, 17, 0, tzinfo=UTC).timestamp())
    [ev] = m.map(RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected", resets_at=resets),
                                uuid="u", session_id="s1"))
    assert ev == RateLimited(resets_at=datetime(2026, 10, 15, 17, 0, tzinfo=UTC))
    usage = m.map(_result())[0]
    assert isinstance(usage, Usage) and usage.rate_limit_status == "rejected"


class FakeSdkClient:
    """Mimics ClaudeSDKClient: consumes the prompt iterable, replies with scripted messages."""

    def __init__(self, options: Any, replies: list[list[Any]]) -> None:
        self.options = options
        self.replies = replies
        self.prompts: list[dict[str, Any]] = []
        self.out: asyncio.Queue[Any] = asyncio.Queue()
        self.interrupts = 0
        self.disconnected = False

    async def connect(self, prompt: Any) -> None:
        async def pump() -> None:
            async for p in prompt:
                self.prompts.append(p)
                for msg in self.replies.pop(0):
                    await self.out.put(msg)
            await self.out.put(None)

        self.task = asyncio.create_task(pump())

    async def receive_messages(self):  # type: ignore[no-untyped-def]
        while (msg := await self.out.get()) is not None:
            yield msg

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def disconnect(self) -> None:
        self.disconnected = True


async def test_streaming_backend_maps_messages_and_sends_user_prompts(settings, project):
    clients: list[FakeSdkClient] = []

    def factory(options: Any) -> FakeSdkClient:
        c = FakeSdkClient(options, [[_stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}}), _result()]])
        clients.append(c)
        return c

    backend = SdkStreamingBackend(settings=settings, specs=[], ctx_factory=lambda: None,  # type: ignore[arg-type,return-value]
                                  client_factory=factory)

    async def items():
        yield QueuedItem(message_id=1, client_message_id="c1", text="hello")

    events = [ev async for ev in backend.start(project, items(), "s-old")]
    assert TextDelta("ok") in events and events[-1] == TurnFinished(reason="completed")
    c = clients[0]
    assert c.prompts[0]["type"] == "user"
    assert c.prompts[0]["message"] == {"role": "user", "content": "hello"}
    assert c.options.resume == "s-old" and c.options.fork_session is True
    assert c.disconnected is True


async def test_streaming_backend_interrupt_delegates_to_client(settings, project):
    gate = asyncio.Event()
    client_box: list[FakeSdkClient] = []

    def factory(options: Any) -> FakeSdkClient:
        c = FakeSdkClient(options, [[_result()]])
        client_box.append(c)
        return c

    backend = SdkStreamingBackend(settings=settings, specs=[], ctx_factory=lambda: None,  # type: ignore[arg-type,return-value]
                                  client_factory=factory)

    async def items():
        yield QueuedItem(message_id=1, client_message_id="c1", text="x")
        await gate.wait()

    gen = backend.start(project, items(), None)
    first = await gen.__anext__()
    await backend.interrupt()
    assert client_box[0].interrupts == 1
    gate.set()
    rest = [first] + [ev async for ev in gen]
    assert rest[-1] == TurnFinished(reason="completed")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/agent/test_sdk_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'copperchat.agent.events'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/agent/events.py
"""Internal events between an agent backend (SDK adapter or FakeAgent) and the RunEngine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from copperchat.agent.protocol import AttachmentRef
from copperchat.projects import Project
from copperchat.tools import ImageBlock


@dataclass(frozen=True)
class QueuedItem:
    message_id: int
    client_message_id: str
    text: str
    attachments: tuple[AttachmentRef, ...] = ()


@dataclass(frozen=True)
class TurnStarted:
    """Informational; the engine treats the gated input pull as the authoritative turn start."""


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultEvt:
    id: str
    content: str
    images: tuple[ImageBlock, ...] = ()
    is_error: bool = False


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float | None = None
    rate_limit_status: Literal["allowed", "allowed_warning", "rejected"] | None = None
    rate_limit_resets_at: datetime | None = None


@dataclass(frozen=True)
class RateLimited:
    resets_at: datetime


@dataclass(frozen=True)
class TurnFinished:
    reason: Literal["completed", "interrupted", "error"]
    error: str | None = field(default=None)


@dataclass(frozen=True)
class SessionId:
    id: str


AgentEvent = (
    TurnStarted | TextDelta | ThinkingDelta | ToolUse | ToolResultEvt | Usage | RateLimited
    | TurnFinished | SessionId
)


class AgentBackend(Protocol):
    def start(
        self,
        project: Project,
        input_stream: AsyncIterator[QueuedItem],
        resume_session_id: str | None,
    ) -> AsyncIterator[AgentEvent]: ...

    async def interrupt(self) -> None: ...
```

```python
# backend/src/copperchat/agent/sdk_backend.py
"""Claude Agent SDK adapter: options, guard, SDK message → AgentEvent mapping, backends."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, McpSdkServerConfig,
    PermissionResultAllow, PermissionResultDeny, RateLimitEvent, RateLimitInfo, ResultMessage,
    StreamEvent, SystemMessage, TextBlock, ThinkingBlock, ToolPermissionContext, ToolResultBlock,
    ToolUseBlock, UserMessage,
)
from claude_agent_sdk import PermissionResult

from copperchat.agent.events import (
    AgentEvent, QueuedItem, RateLimited, SessionId, TextDelta, ThinkingDelta, ToolResultEvt,
    ToolUse, TurnFinished, Usage,
)
from copperchat.agent.sdk_tools import SERVER_NAME, build_sdk_tools, sdk_tool_name
from copperchat.config import Settings
from copperchat.projects import Project
from copperchat.tools import ImageBlock, ToolContext, ToolSpec

SYSTEM_PROMPT = """You are CopperChat, an assistant for KiCad schematic and PCB design, running \
inside the self-hosted CopperChat web app (Powered by Claude).

You work on exactly one KiCad project: "{project_name}".
- You have no file, shell or web access. Everything you know about the design comes from your \
CopperChat tools, which query the running KiCad API server.
- Never guess design data: call a tool, then answer from its result. Report UUIDs in full and \
coordinates in millimetres.
- This release is read-only: you cannot edit schematics or boards. If asked to change the \
design, explain what you would change and that editing is not available yet.
- Use render_view to look at the design when spatial reasoning matters, and run_erc / run_drc \
to check it.
- Git: call git_commit only when the user explicitly asked for a commit in their current \
message. Never push.
- Lines starting with "[CopperChat notice]" come from the application, not from the user."""

ClientFactory = Callable[[ClaudeAgentOptions], Any]


def make_tool_guard(allowed: frozenset[str]):  # type: ignore[no-untyped-def]
    async def guard(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResult:
        if tool_name in allowed:
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(
            message=f"Tool '{tool_name}' is not available in CopperChat.", interrupt=False
        )

    return guard


def build_options(
    *,
    settings: Settings,
    project: Project,
    mcp_server: McpSdkServerConfig,
    tool_names: Sequence[str],
    resume_session_id: str | None,
    fork_session: bool,
) -> ClaudeAgentOptions:
    allowed = [sdk_tool_name(n) for n in tool_names]
    env = {"CLAUDE_CONFIG_DIR": str(settings.data_dir / "claude")}
    if settings.claude_code_oauth_token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token.get_secret_value()
    if settings.anthropic_api_key is not None:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key.get_secret_value()
    return ClaudeAgentOptions(
        tools=[],
        allowed_tools=allowed,
        mcp_servers={SERVER_NAME: mcp_server},
        system_prompt=SYSTEM_PROMPT.format(project_name=project.name),
        permission_mode="default",
        can_use_tool=make_tool_guard(frozenset(allowed)),
        resume=resume_session_id,
        fork_session=fork_session,
        cwd=str(project.dir),
        env=env,
        include_partial_messages=True,
        setting_sources=[],
    )


def _tool_result_content(content: str | list[dict[str, Any]] | None) -> tuple[str, tuple[ImageBlock, ...]]:
    if content is None:
        return "", ()
    if isinstance(content, str):
        return content, ()
    texts: list[str] = []
    images: list[ImageBlock] = []
    for item in content:
        if item.get("type") == "text":
            texts.append(str(item.get("text", "")))
        elif item.get("type") == "image":
            src = item.get("source", {})
            if src.get("type") == "base64":
                images.append(ImageBlock(mime=str(src.get("media_type", "image/png")),
                                         data=base64.b64decode(src.get("data", ""))))
    return "\n".join(texts), tuple(images)


class SdkEventMapper:
    """Stateful mapping of SDK messages to AgentEvents (one mapper per SDK client)."""

    def __init__(self) -> None:
        self._session_id: str | None = None
        self._current_stream_msg: str | None = None
        self._streamed_msgs: set[str] = set()
        self._partial_since_last_assistant = False
        self._rate: RateLimitInfo | None = None

    def map(self, msg: object) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        sid = getattr(msg, "session_id", None)
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            sid = msg.data.get("session_id")
        if isinstance(sid, str) and sid and sid != self._session_id:
            self._session_id = sid
            out.append(SessionId(sid))

        if isinstance(msg, StreamEvent):
            if msg.parent_tool_use_id:
                return out
            ev = msg.event
            if ev.get("type") == "message_start":
                self._current_stream_msg = ev.get("message", {}).get("id")
            elif ev.get("type") == "content_block_delta":
                delta = ev.get("delta", {})
                kind = delta.get("type")
                if kind in ("text_delta", "thinking_delta"):
                    self._partial_since_last_assistant = True
                    if self._current_stream_msg:
                        self._streamed_msgs.add(self._current_stream_msg)
                    if kind == "text_delta":
                        out.append(TextDelta(str(delta.get("text", ""))))
                    else:
                        out.append(ThinkingDelta(str(delta.get("thinking", ""))))
        elif isinstance(msg, AssistantMessage):
            if msg.parent_tool_use_id:
                return out
            streamed = (msg.message_id in self._streamed_msgs) if msg.message_id \
                else self._partial_since_last_assistant
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    out.append(ToolUse(id=block.id, name=block.name, input=dict(block.input)))
                elif isinstance(block, TextBlock) and not streamed:
                    out.append(TextDelta(block.text))
                elif isinstance(block, ThinkingBlock) and not streamed:
                    out.append(ThinkingDelta(block.thinking))
            self._partial_since_last_assistant = False
        elif isinstance(msg, UserMessage):
            if msg.parent_tool_use_id or isinstance(msg.content, str):
                return out
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    text, images = _tool_result_content(block.content)
                    out.append(ToolResultEvt(id=block.tool_use_id, content=text, images=images,
                                             is_error=bool(block.is_error)))
        elif isinstance(msg, RateLimitEvent):
            self._rate = msg.rate_limit_info
            if msg.rate_limit_info.status == "rejected":
                ts = msg.rate_limit_info.resets_at
                resets = (datetime.fromtimestamp(ts, UTC) if ts
                          else datetime.now(UTC) + timedelta(minutes=5))
                out.append(RateLimited(resets_at=resets))
        elif isinstance(msg, ResultMessage):
            u = msg.usage or {}
            rate = self._rate
            out.append(Usage(
                input_tokens=int(u.get("input_tokens", 0)),
                output_tokens=int(u.get("output_tokens", 0)),
                cache_read_tokens=int(u.get("cache_read_input_tokens", 0)),
                cache_creation_tokens=int(u.get("cache_creation_input_tokens", 0)),
                cost_usd=msg.total_cost_usd,
                rate_limit_status=rate.status if rate else None,
                rate_limit_resets_at=(datetime.fromtimestamp(rate.resets_at, UTC)
                                      if rate and rate.resets_at else None),
            ))
            if msg.terminal_reason in ("aborted_streaming", "aborted_tools"):
                out.append(TurnFinished(reason="interrupted"))
            elif msg.is_error:
                detail = "; ".join(msg.errors or []) or msg.result or msg.subtype
                out.append(TurnFinished(reason="error", error=detail))
            else:
                out.append(TurnFinished(reason="completed"))
        return out


def user_prompt(item: QueuedItem) -> dict[str, Any]:
    # If docs/spikes/1a-sdk-streaming.md records a different user-message shape, use that.
    return {
        "type": "user",
        "message": {"role": "user", "content": item.text},
        "parent_tool_use_id": None,
        "session_id": "default",
    }


async def _prompts(items: AsyncIterator[QueuedItem]) -> AsyncIterator[dict[str, Any]]:
    async for item in items:
        yield user_prompt(item)


class SdkStreamingBackend:
    """1A go: one long-lived ClaudeSDKClient per burst, fed by the engine's gated input."""

    def __init__(
        self,
        *,
        settings: Settings,
        specs: Sequence[ToolSpec],
        ctx_factory: Callable[[], ToolContext],
        client_factory: ClientFactory = ClaudeSDKClient,
    ) -> None:
        self._settings = settings
        self._specs = list(specs)
        self._ctx_factory = ctx_factory
        self._client_factory = client_factory
        self._client: Any = None

    async def start(
        self,
        project: Project,
        input_stream: AsyncIterator[QueuedItem],
        resume_session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        options = build_options(
            settings=self._settings,
            project=project,
            mcp_server=build_sdk_tools(self._specs, self._ctx_factory),
            tool_names=[s.name for s in self._specs],
            resume_session_id=resume_session_id,
            fork_session=resume_session_id is not None,
        )
        client = self._client_factory(options)
        await client.connect(prompt=_prompts(input_stream))
        self._client = client
        mapper = SdkEventMapper()
        try:
            async for msg in client.receive_messages():
                for ev in mapper.map(msg):
                    yield ev
        finally:
            self._client = None
            await client.disconnect()

    async def interrupt(self) -> None:
        client = self._client
        if client is not None:
            await client.interrupt()
```

`sdk_backend.py` imports `copperchat.agent.sdk_tools`; create that module now
with exactly the code from Task 5.5 Step 3 (Task 5.5 then adds its tests and
the `ToolContext` fields; if you prefer strict ordering, do Task 5.5 first —
the two tasks are independent apart from this import).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/agent/test_sdk_backend.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent/events.py backend/src/copperchat/agent/sdk_backend.py backend/tests/unit/agent/test_sdk_backend.py
git commit -m "feat: agent events, SDK options/guard, SDK message mapper and streaming backend" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.4b: Per-turn SDK backend (1A no-go fallback)

**Execute only if** `docs/spikes/1a-sdk-streaming.md` records **no-go** for
the long-lived streaming client. Same `AgentBackend` protocol, same gated
input; each queued message gets its own `ClaudeSDKClient` resumed from the
previous turn's session id. `fork_session=True` only for the first turn of a
burst (and only when a stored session id exists), never mid-burst. Queue
semantics are unchanged for clients.

**Files:**
- Modify: `backend/src/copperchat/agent/sdk_backend.py` (add `SdkPerTurnBackend`)
- Modify: `backend/src/copperchat/agent/runtime.py` (Task 5.13: use `SdkPerTurnBackend` instead of `SdkStreamingBackend` in `_default_backend_factory`)
- Test: `backend/tests/unit/agent/test_sdk_backend_per_turn.py`

**Interfaces:**
- Produces: `SdkPerTurnBackend(settings=, specs=, ctx_factory=, client_factory=)` implementing `AgentBackend`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/agent/test_sdk_backend_per_turn.py
import asyncio
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ResultMessage, StreamEvent
from pydantic import SecretStr

from copperchat.agent.events import QueuedItem, SessionId, TextDelta, TurnFinished
from copperchat.agent.sdk_backend import SdkPerTurnBackend
from copperchat.config import Settings
from copperchat.projects import Project


def _result(session_id: str) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                         num_turns=1, session_id=session_id, usage={})


class OneShotClient:
    def __init__(self, options: Any, session_id: str, log: list["OneShotClient"]) -> None:
        self.options, self.session_id = options, session_id
        self.prompts: list[dict[str, Any]] = []
        self.interrupts = 0
        self.disconnected = False
        log.append(self)

    async def connect(self, prompt: Any) -> None:
        async for p in prompt:
            self.prompts.append(p)

    async def receive_messages(self):  # type: ignore[no-untyped-def]
        yield StreamEvent(uuid="u", session_id=self.session_id,
                          event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}})
        yield _result(self.session_id)
        await asyncio.Event().wait()  # a real client keeps the stream open; backend must stop at the result

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(_env_file=None, projects_root=tmp_path, data_dir=tmp_path,
                        claude_code_oauth_token=SecretStr("t"))
    d = tmp_path / "demo"
    d.mkdir()
    project = Project(id="a1b2c3d4e5f6", name="demo", rel_path="demo/demo.kicad_pro", dir=d,
                      pro_file=d / "demo.kicad_pro", sch_file=None, pcb_file=None)
    return settings, project


async def test_one_client_per_turn_resume_chain_and_fork_only_first(setup):
    settings, project = setup
    log: list[OneShotClient] = []
    ids = iter(["s-fork", "s-2"])
    backend = SdkPerTurnBackend(settings=settings, specs=[], ctx_factory=lambda: None,  # type: ignore[arg-type,return-value]
                                client_factory=lambda o: OneShotClient(o, next(ids), log))

    async def items():
        yield QueuedItem(message_id=1, client_message_id="a", text="one")
        yield QueuedItem(message_id=2, client_message_id="b", text="two")

    events = [ev async for ev in backend.start(project, items(), "s-stored")]
    assert [type(e) for e in events].count(TurnFinished) == 2
    assert SessionId("s-fork") in events and TextDelta("hi") in events
    assert (log[0].options.resume, log[0].options.fork_session) == ("s-stored", True)
    assert (log[1].options.resume, log[1].options.fork_session) == ("s-fork", False)
    assert [c.prompts[0]["message"]["content"] for c in log] == ["one", "two"]
    assert all(c.disconnected for c in log)


async def test_interrupt_targets_current_client(setup):
    settings, project = setup
    log: list[OneShotClient] = []
    backend = SdkPerTurnBackend(settings=settings, specs=[], ctx_factory=lambda: None,  # type: ignore[arg-type,return-value]
                                client_factory=lambda o: OneShotClient(o, "s", log))

    async def items():
        yield QueuedItem(message_id=1, client_message_id="a", text="one")

    gen = backend.start(project, items(), None)
    await gen.__anext__()  # SessionId of the first client
    await backend.interrupt()
    assert log[0].interrupts == 1
    await gen.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/agent/test_sdk_backend_per_turn.py -v`
Expected: FAIL with `ImportError: cannot import name 'SdkPerTurnBackend'`.

- [ ] **Step 3: Write minimal implementation**

Append to `backend/src/copperchat/agent/sdk_backend.py`:

```python
async def _single(item: QueuedItem) -> AsyncIterator[dict[str, Any]]:
    yield user_prompt(item)


class SdkPerTurnBackend:
    """1A no-go: one ClaudeSDKClient per queued message, resumed from the previous turn."""

    def __init__(
        self,
        *,
        settings: Settings,
        specs: Sequence[ToolSpec],
        ctx_factory: Callable[[], ToolContext],
        client_factory: ClientFactory = ClaudeSDKClient,
    ) -> None:
        self._settings = settings
        self._specs = list(specs)
        self._ctx_factory = ctx_factory
        self._client_factory = client_factory
        self._client: Any = None

    async def start(
        self,
        project: Project,
        input_stream: AsyncIterator[QueuedItem],
        resume_session_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        session_id = resume_session_id
        first = True
        async for item in input_stream:
            options = build_options(
                settings=self._settings,
                project=project,
                mcp_server=build_sdk_tools(self._specs, self._ctx_factory),
                tool_names=[s.name for s in self._specs],
                resume_session_id=session_id,
                fork_session=first and session_id is not None,
            )
            first = False
            client = self._client_factory(options)
            await client.connect(prompt=_single(item))
            self._client = client
            mapper = SdkEventMapper()
            try:
                async for msg in client.receive_messages():
                    for ev in mapper.map(msg):
                        if isinstance(ev, SessionId):
                            session_id = ev.id
                        yield ev
                    if isinstance(msg, ResultMessage):
                        break
            finally:
                self._client = None
                await client.disconnect()

    async def interrupt(self) -> None:
        client = self._client
        if client is not None:
            await client.interrupt()
```

In `runtime.py` (Task 5.13), import and construct `SdkPerTurnBackend`
instead of `SdkStreamingBackend` (same keyword arguments).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/agent/test_sdk_backend_per_turn.py tests/unit/agent/test_sdk_backend.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent/sdk_backend.py backend/tests/unit/agent/test_sdk_backend_per_turn.py
git commit -m "feat: per-turn SDK backend (spike 1A fallback)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.5: Tool adapter `build_sdk_tools` and ToolContext turn callbacks

**Files:**
- Create: `backend/src/copperchat/agent/sdk_tools.py`
- Modify: `copperchat.tools` (`ToolContext`, `ToolResult` — see Assumptions for the file)
- Test: `backend/tests/unit/agent/test_sdk_tools.py`

**Interfaces:**
- Consumes: `ToolSpec`, `ToolContext`, `ToolResult`, `ImageBlock`, `StructuredToolError`, `KicadUnavailable`; `claude_agent_sdk.SdkMcpTool`, `create_sdk_mcp_server`.
- Produces: `SERVER_NAME`, `sdk_tool_name`, `build_sdk_tool_list`, `build_sdk_tools`, `mcp_result_to_event`; new `ToolContext`/`ToolResult` fields (see Interfaces).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/agent/test_sdk_tools.py
import json
from pathlib import Path
from typing import Any, cast

import pytest

from copperchat.agent.sdk_tools import (
    build_sdk_tool_list, build_sdk_tools, mcp_result_to_event, sdk_tool_name,
)
from copperchat.kicad_session import KicadUnavailable
from copperchat.projects import Project
from copperchat.tools import ImageBlock, StructuredToolError, ToolContext, ToolResult, ToolSpec

SCHEMA = {"type": "object", "properties": {}}


def _project(tmp_path: Path) -> Project:
    return Project(id="a1b2c3d4e5f6", name="demo", rel_path="demo/demo.kicad_pro", dir=tmp_path,
                   pro_file=tmp_path / "demo.kicad_pro", sch_file=None, pcb_file=None)


@pytest.fixture
def calls() -> list[Any]:
    return []


@pytest.fixture
def ctx_factory(tmp_path: Path, calls: list[Any]):
    def make() -> ToolContext:
        async def on_mutation() -> None:
            calls.append("mutation")

        return ToolContext(
            project=_project(tmp_path), session=cast(Any, None), settings=cast(Any, None),
            on_mutation=on_mutation,
            on_documents_changed=lambda docs: calls.append(("changed", docs)),
            on_kicad_unavailable=lambda exc: calls.append(("kicad", str(exc))),
        )

    return make


def _spec(handler, **kw) -> ToolSpec:  # type: ignore[no-untyped-def]
    return ToolSpec(name=kw.pop("name", "probe"), description="d", input_schema=SCHEMA,
                    handler=handler, **kw)


def _tool(spec: ToolSpec, ctx_factory):  # type: ignore[no-untyped-def]
    [t] = build_sdk_tool_list([spec], ctx_factory)
    return t


async def test_success_returns_json_text_and_image_blocks(ctx_factory):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(data={"echo": args["x"]}, images=[ImageBlock(mime="image/png", data=b"PNG")])

    raw = await _tool(_spec(handler), ctx_factory).handler({"x": 1})
    assert raw.get("is_error", False) is False
    assert json.loads(raw["content"][0]["text"]) == {"echo": 1}
    assert raw["content"][1] == {"type": "image", "data": "UE5H", "mimeType": "image/png"}


async def test_structured_error_becomes_is_error_json(ctx_factory):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise StructuredToolError("no such ref", "U99 is not in the schematic", "list symbols first")

    raw = await _tool(_spec(handler), ctx_factory).handler({})
    assert raw["is_error"] is True
    assert json.loads(raw["content"][0]["text"]) == {"error": {
        "what": "no such ref", "why": "U99 is not in the schematic", "suggestion": "list symbols first"}}


async def test_kicad_unavailable_reports_to_context(ctx_factory, calls):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise KicadUnavailable("api-server timed out")

    raw = await _tool(_spec(handler), ctx_factory).handler({})
    assert raw["is_error"] is True
    assert "KiCad" in json.loads(raw["content"][0]["text"])["error"]["what"]
    assert calls == [("kicad", "api-server timed out")]


async def test_mutating_tool_calls_on_mutation_first_and_reports_changed_docs(ctx_factory, calls):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        calls.append("handler")
        return ToolResult(data={"ok": True}, changed_docs={"sch"})

    await _tool(_spec(handler, mutating=True, changes_documents=True), ctx_factory).handler({})
    assert calls == ["mutation", "handler", ("changed", {"sch"})]


async def test_context_factory_failure_is_a_tool_error(tmp_path):
    def broken() -> ToolContext:
        raise KicadUnavailable("no session")

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise AssertionError("must not run")

    raw = await _tool(_spec(handler), broken).handler({})
    assert raw["is_error"] is True


def test_names_and_server_config(ctx_factory):
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult()

    assert sdk_tool_name("run_erc") == "mcp__copperchat__run_erc"
    server = build_sdk_tools([_spec(handler)], ctx_factory)
    assert server["type"] == "sdk" and server["name"] == "copperchat"


def test_mcp_result_to_event_roundtrip():
    ev = mcp_result_to_event("t1", {"content": [{"type": "text", "text": "a"},
                                                {"type": "image", "data": "UE5H", "mimeType": "image/png"}],
                                    "is_error": True})
    assert ev.id == "t1" and ev.content == "a" and ev.is_error is True
    assert ev.images[0].data == b"PNG"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/agent/test_sdk_tools.py -v`
Expected: FAIL with `TypeError: ToolContext.__init__() got an unexpected keyword argument 'on_documents_changed'` (or `ModuleNotFoundError` for `sdk_tools` if Task 5.4 has not created it yet).

- [ ] **Step 3: Write minimal implementation**

Add to the `ToolContext` dataclass (after `on_mutation`) and to `ToolResult`:

```python
# in copperchat.tools — ToolContext gains (keep existing fields unchanged):
    user_message: str | None = None                       # text of the user message that started this turn
    on_documents_changed: Callable[[set[DocKind]], None] | None = None
    on_kicad_unavailable: Callable[[Exception], None] | None = None

# ToolResult gains:
    changed_docs: set[DocKind] = field(default_factory=set)   # docs this call changed (mutating tools)
```

(`DocKind` is imported from `copperchat.kicad_session`; if that creates an
import cycle, use `Literal["sch", "pcb"]` inline.)

```python
# backend/src/copperchat/agent/sdk_tools.py
"""Adapter from our provider-neutral ToolSpec registry to in-process SDK MCP tools."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server

from copperchat.agent.events import ToolResultEvt
from copperchat.kicad_session import KicadUnavailable
from copperchat.tools import ImageBlock, StructuredToolError, ToolContext, ToolResult, ToolSpec

log = logging.getLogger(__name__)
SERVER_NAME = "copperchat"


def sdk_tool_name(name: str) -> str:
    return f"mcp__{SERVER_NAME}__{name}"


def _error(err: StructuredToolError) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps({"error": err.to_dict()}, ensure_ascii=False)}],
        "is_error": True,
    }


def _success(result: ToolResult) -> dict[str, Any]:
    payload = result.data if result.data is not None else {"ok": True}
    content: list[dict[str, Any]] = [
        {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
    ]
    for img in result.images:
        content.append({"type": "image", "data": base64.b64encode(img.data).decode("ascii"),
                        "mimeType": img.mime})
    return {"content": content}


def _make_handler(spec: ToolSpec, ctx_factory: Callable[[], ToolContext]):  # type: ignore[no-untyped-def]
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            ctx = ctx_factory()
        except KicadUnavailable as exc:
            return _error(StructuredToolError(
                "KiCad is not available", str(exc),
                "Ask the user to reopen the project in CopperChat, then try again."))
        try:
            if spec.mutating and ctx.on_mutation is not None:
                await ctx.on_mutation()
            result = await spec.handler(ctx, args)
        except StructuredToolError as err:
            return _error(err)
        except KicadUnavailable as exc:
            if ctx.on_kicad_unavailable is not None:
                ctx.on_kicad_unavailable(exc)
            return _error(StructuredToolError(
                f"{spec.name} failed because KiCad stopped responding", str(exc),
                "The KiCad API server was restarted and this turn ends. Ask again to retry."))
        except Exception as exc:  # a bug in a tool must reach the agent as a clear error
            log.exception("tool %s failed", spec.name)
            return _error(StructuredToolError(
                f"{spec.name} failed unexpectedly", f"{type(exc).__name__}: {exc}",
                "This is a CopperChat bug; do not retry the same call."))
        if result.changed_docs and ctx.on_documents_changed is not None:
            ctx.on_documents_changed(set(result.changed_docs))
        return _success(result)

    return handler


def build_sdk_tool_list(
    specs: Sequence[ToolSpec], ctx_factory: Callable[[], ToolContext]
) -> list[SdkMcpTool[Any]]:
    return [
        SdkMcpTool(name=s.name, description=s.description, input_schema=s.input_schema,
                   handler=_make_handler(s, ctx_factory))
        for s in specs
    ]


def build_sdk_tools(
    specs: Sequence[ToolSpec], ctx_factory: Callable[[], ToolContext]
) -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name=SERVER_NAME, version="1.0.0", tools=build_sdk_tool_list(specs, ctx_factory)
    )


def mcp_result_to_event(tool_use_id: str, raw: dict[str, Any]) -> ToolResultEvt:
    texts: list[str] = []
    images: list[ImageBlock] = []
    for item in raw.get("content", []):
        if item.get("type") == "text":
            texts.append(str(item.get("text", "")))
        elif item.get("type") == "image":
            images.append(ImageBlock(mime=str(item.get("mimeType", "image/png")),
                                     data=base64.b64decode(item.get("data", ""))))
    return ToolResultEvt(id=tool_use_id, content="\n".join(texts), images=tuple(images),
                         is_error=bool(raw.get("is_error", False)))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/agent/test_sdk_tools.py tests/unit -q`
Expected: all passed (existing Phase 3 tool tests still pass — the new fields have defaults).

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent/sdk_tools.py backend/src/copperchat/tools backend/tests/unit/agent/test_sdk_tools.py
git commit -m "feat: ToolSpec to SDK MCP tool adapter with structured errors and turn callbacks" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
