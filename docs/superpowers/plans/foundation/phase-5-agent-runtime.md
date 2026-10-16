# Phase 5: Agent runtime

> **Cross-phase corrections apply:** see index § "Cross-phase corrections" (X2, X5, X15, X17, X18, X19); they supersede conflicting code in this file.

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
- Sub-agent messages (SDK messages with `parent_tool_use_id`) are not dropped: their tool calls, tool results and text are persisted as complete rows with `metadata.parentToolUseId` (Task 5.11b); the main agent's streaming segment is never mixed with sub-agent text; interrupt and usage cover sub-agent work.
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
  `app.state.db`, `KicadSessionManager` in `app.state.sessions`, created in
  the FastAPI lifespan (Phase 3, binding). Only Task 5.12/5.13 read `app.state`.
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
#   ToolUse / ToolResultEvt gain parent_tool_use_id: str | None = None; NEW SubagentText(parent_tool_use_id, text)  (Task 5.11b)

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
def default_backend_factory(settings: Settings, specs: Sequence[ToolSpec]) -> BackendFactory   # NEW
# copperchat.agent.sdk_backend: SdkAgentBackend = SdkStreamingBackend (or SdkPerTurnBackend on 1A no-go)  # NEW
# copperchat.agent.push_stream: PushStream[T] push/request_close/drop/pending_count/pending_ids/release_next  # NEW
# copperchat.agent.broadcaster: Broadcaster(max_queue=1000), Subscription(close(), dropped)  # NEW
# copperchat.web.ws: HEARTBEAT_S = 25.0; close codes 4401/4403/4408  # NEW
# GET /api/projects/{id}/images/{name}  # NEW route (tool images under agent_dir/images)

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
async def authenticate_connection(conn: HTTPConnection) -> str | None   # NEW; username or None

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
    assert json.loads(dump_frame(PongFrame(project_id=None))) == {"type": "pong", "projectId": None}


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


def _require_project_key(data: Any) -> Any:
    """ping/pong frames carry an explicit `"projectId": null`; the key must be present."""
    if isinstance(data, dict) and "projectId" not in data and "project_id" not in data:
        raise ValueError("projectId must be present (null)")
    return data


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

    @model_validator(mode="before")
    @classmethod
    def _require_project_key(cls, data: Any) -> Any:
        return _require_project_key(data)


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

    @model_validator(mode="before")
    @classmethod
    def _require_project_key(cls, data: Any) -> Any:
        return _require_project_key(data)


class PongFrame(Wire):
    type: Literal["pong"] = "pong"
    project_id: None = None

    @model_validator(mode="before")
    @classmethod
    def _require_project_key(cls, data: Any) -> Any:
        return _require_project_key(data)


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

The `// path` lines above are labels only; each file contains just the JSON
object. `{"type":"pong"}` is invalid because the `mode="before"` validator
`_require_project_key` on `PingFrame`, `ServerPingFrame` and `PongFrame`
requires the `projectId` key to be present (its value may only be `null`);
`{"type":"pong","projectId":null,"timestamp":1}` is invalid via
`extra="forbid"`. In code, always construct these frames as
`PongFrame(project_id=None)` / `ServerPingFrame(project_id=None)`.

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

---

### Task 5.6: `PushStream` with gated release

Python port of Kontor's `server/runEngine/inputStream.ts` plus the gate from
finding 1: the stream yields its first item immediately, every further item
only after the engine called `release_next()` (i.e. after it processed the
previous turn's `TurnFinished`).

**Files:**
- Create: `backend/src/copperchat/agent/push_stream.py`
- Test: `backend/tests/unit/agent/test_push_stream.py`

**Interfaces:**
- Consumes: `QueuedItem` (only `message_id` is read).
- Produces: `PushStream[T]` with `push(item) -> bool`, `request_close() -> None`, `drop(message_id) -> bool`, `pending_count() -> int`, `pending_ids() -> list[int]`, `release_next() -> None`, `closed: bool`, async iteration.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/agent/test_push_stream.py
import asyncio

from copperchat.agent.events import QueuedItem
from copperchat.agent.push_stream import PushStream


def item(i: int) -> QueuedItem:
    return QueuedItem(message_id=i, client_message_id=f"c{i}", text=f"m{i}")


async def _next(it, timeout: float = 0.2):  # type: ignore[no-untyped-def]
    return await asyncio.wait_for(it.__anext__(), timeout)


async def test_first_item_immediately_next_only_after_release():
    s: PushStream[QueuedItem] = PushStream()
    s.push(item(1))
    s.push(item(2))
    it = aiter(s)
    assert (await _next(it)).message_id == 1
    pending = asyncio.ensure_future(it.__anext__())
    await asyncio.sleep(0.05)
    assert not pending.done(), "second item must wait for release_next()"
    assert s.pending_count() == 1
    s.release_next()
    assert (await asyncio.wait_for(pending, 0.2)).message_id == 2


async def test_queue_first_drain_before_close():
    s: PushStream[QueuedItem] = PushStream()
    s.push(item(1))
    s.request_close()
    it = aiter(s)
    assert (await _next(it)).message_id == 1
    s.release_next()
    try:
        await _next(it)
        raise AssertionError("expected end of stream")
    except StopAsyncIteration:
        pass
    assert s.closed is True
    assert s.push(item(9)) is False, "push after close must fail so the caller starts a new burst"


async def test_push_after_close_request_reopens():
    s: PushStream[QueuedItem] = PushStream()
    s.push(item(1))
    it = aiter(s)
    await _next(it)
    s.request_close()
    assert s.push(item(2)) is True
    s.release_next()
    assert (await _next(it)).message_id == 2
    assert s.closed is False


async def test_drop_removes_only_unpulled_items():
    s: PushStream[QueuedItem] = PushStream()
    for i in (1, 2, 3):
        s.push(item(i))
    it = aiter(s)
    await _next(it)
    assert s.drop(1) is False, "already pulled"
    assert s.drop(2) is True
    assert s.drop(2) is False
    assert s.pending_ids() == [3]
    s.release_next()
    assert (await _next(it)).message_id == 3


async def test_close_request_wakes_waiting_consumer():
    s: PushStream[QueuedItem] = PushStream()
    it = aiter(s)
    waiter = asyncio.ensure_future(it.__anext__())
    await asyncio.sleep(0.01)
    s.request_close()
    try:
        await asyncio.wait_for(waiter, 0.2)
        raise AssertionError("expected end of stream")
    except StopAsyncIteration:
        pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/agent/test_push_stream.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'copperchat.agent.push_stream'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/agent/push_stream.py
"""Push-based async input stream (port of Kontor inputStream.ts) with a turn gate."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from typing import Generic, Protocol, TypeVar


class _HasMessageId(Protocol):
    @property
    def message_id(self) -> int: ...


T = TypeVar("T", bound=_HasMessageId)


class PushStream(Generic[T]):
    def __init__(self) -> None:
        self._queue: deque[T] = deque()
        self._closing = False
        self.closed = False
        self._released = True  # the first item of a burst needs no release
        self._wake = asyncio.Event()

    def push(self, item: T) -> bool:
        """Enqueue; False if the generator already returned (caller starts a new burst)."""
        if self.closed:
            return False
        self._queue.append(item)
        self._closing = False  # a fresh message cancels a pending close (reopen)
        self._wake.set()
        return True

    def request_close(self) -> None:
        """End the stream once the queue is empty and no push re-opened it."""
        self._closing = True
        self._wake.set()

    def release_next(self) -> None:
        """Called by the engine after it processed the previous turn's TurnFinished."""
        self._released = True
        self._wake.set()

    def drop(self, message_id: int) -> bool:
        for queued in self._queue:
            if queued.message_id == message_id:
                self._queue.remove(queued)
                self._wake.set()
                return True
        return False

    def pending_count(self) -> int:
        return len(self._queue)

    def pending_ids(self) -> list[int]:
        return [q.message_id for q in self._queue]

    def __aiter__(self) -> AsyncIterator[T]:
        return self._gen()

    async def _gen(self) -> AsyncIterator[T]:
        while True:
            # Queue-first ordering is load-bearing: drain before honouring close.
            if self._queue and self._released:
                self._released = False
                yield self._queue.popleft()
                continue
            if not self._queue and self._closing:
                self.closed = True
                return
            self._wake.clear()
            await self._wake.wait()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/agent/test_push_stream.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent/push_stream.py backend/tests/unit/agent/test_push_stream.py
git commit -m "feat: gated push stream for queued agent input" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.7: `FakeAgent` scriptable backend

Used by every engine/WebSocket test and by `dev_fake_agent=True`. Each pulled
`QueuedItem` runs one script; scripts are lists of steps.

**Files:**
- Create: `backend/src/copperchat/agent/fake.py`
- Test: `backend/tests/unit/agent/test_fake.py`

**Interfaces:**
- Consumes: `AgentBackend`, `AgentEvent` members, `QueuedItem`, `build_sdk_tool_list`, `sdk_tool_name`, `mcp_result_to_event`, `ToolSpec`, `ToolContext`.
- Produces: steps `Text(text)`, `Think(text)`, `Tool(name, args)`, `Wait(event)`, `Emit(event)`, `Finish(reason="completed", error=None)`; `FakeAgent(script_for=None, *, specs=(), ctx_factory=None, ignore_interrupt=False)` with `.prompts: list[QueuedItem]`, `.resume_ids: list[str | None]`, `.interrupts: int`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/agent/test_fake.py
import asyncio
from typing import Any, cast

from copperchat.agent.events import (
    QueuedItem, SessionId, TextDelta, ToolResultEvt, ToolUse, TurnFinished, Usage,
)
from copperchat.agent.fake import FakeAgent, Finish, Text, Tool, Wait
from copperchat.tools import ToolContext, ToolResult, ToolSpec

PROJECT = cast(Any, None)


async def _items(*texts: str):  # type: ignore[no-untyped-def]
    for i, t in enumerate(texts, 1):
        yield QueuedItem(message_id=i, client_message_id=f"c{i}", text=t)


async def test_default_script_echoes_and_reports_usage():
    agent = FakeAgent()
    events = [e async for e in agent.start(PROJECT, _items("hi"), "s-0")]
    assert events[0] == SessionId("fake-session-1")
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "You said: hi"
    assert isinstance(events[-2], Usage) and events[-1] == TurnFinished(reason="completed")
    assert agent.resume_ids == ["s-0"] and [p.text for p in agent.prompts] == ["hi"]


async def test_tool_step_calls_registered_handler():
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(data={"n": args["n"]})

    spec = ToolSpec(name="probe", description="d", input_schema={"type": "object"}, handler=handler)
    agent = FakeAgent(lambda item: [Tool("probe", {"n": 2})], specs=[spec],
                      ctx_factory=lambda: cast(ToolContext, None))
    events = [e async for e in agent.start(PROJECT, _items("x"), None)]
    use = next(e for e in events if isinstance(e, ToolUse))
    res = next(e for e in events if isinstance(e, ToolResultEvt))
    assert use.name == "mcp__copperchat__probe" and res.id == use.id
    assert res.content == '{"n": 2}' and res.is_error is False


async def test_interrupt_ends_waiting_turn():
    gate = asyncio.Event()
    agent = FakeAgent(lambda item: [Text("a"), Wait(gate), Text("never")])
    gen = agent.start(PROJECT, _items("x"), None)
    assert await gen.__anext__() == SessionId("fake-session-1")
    assert await gen.__anext__() == TextDelta("a")
    nxt = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0.01)
    await agent.interrupt()
    assert await asyncio.wait_for(nxt, 0.5) == TurnFinished(reason="interrupted")
    assert agent.interrupts == 1


async def test_ignore_interrupt_keeps_waiting():
    gate = asyncio.Event()
    agent = FakeAgent(lambda item: [Wait(gate), Finish()], ignore_interrupt=True)
    gen = agent.start(PROJECT, _items("x"), None)
    await gen.__anext__()
    nxt = asyncio.ensure_future(gen.__anext__())
    await agent.interrupt()
    await asyncio.sleep(0.05)
    assert not nxt.done()
    nxt.cancel()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/agent/test_fake.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'copperchat.agent.fake'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/agent/fake.py
"""Scriptable in-process AgentBackend for tests and dev_fake_agent=True (no token needed)."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from copperchat.agent.events import (
    AgentEvent, QueuedItem, SessionId, TextDelta, ThinkingDelta, ToolUse, TurnFinished, Usage,
)
from copperchat.agent.sdk_tools import build_sdk_tool_list, mcp_result_to_event, sdk_tool_name
from copperchat.projects import Project
from copperchat.tools import ToolContext, ToolSpec


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Think:
    text: str


@dataclass(frozen=True)
class Tool:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Wait:
    event: asyncio.Event


@dataclass(frozen=True)
class Emit:
    event: AgentEvent


@dataclass(frozen=True)
class Finish:
    reason: Literal["completed", "interrupted", "error"] = "completed"
    error: str | None = None


Step = Text | Think | Tool | Wait | Emit | Finish
ScriptFor = Callable[[QueuedItem], list[Step]]


def echo_script(item: QueuedItem) -> list[Step]:
    return [Text("You said: "), Text(item.text)]


class FakeAgent:
    def __init__(
        self,
        script_for: ScriptFor | None = None,
        *,
        specs: Sequence[ToolSpec] = (),
        ctx_factory: Callable[[], ToolContext] | None = None,
        ignore_interrupt: bool = False,
    ) -> None:
        self._script_for = script_for or echo_script
        self._specs = list(specs)
        self._ctx_factory = ctx_factory
        self._ignore_interrupt = ignore_interrupt
        self._interrupted = asyncio.Event()
        self._tool_ids = itertools.count(1)
        self._burst = 0
        self.prompts: list[QueuedItem] = []
        self.resume_ids: list[str | None] = []
        self.interrupts = 0

    async def interrupt(self) -> None:
        self.interrupts += 1
        if not self._ignore_interrupt:
            self._interrupted.set()

    async def start(
        self, project: Project, input_stream: AsyncIterator[QueuedItem], resume_session_id: str | None
    ) -> AsyncIterator[AgentEvent]:
        self.resume_ids.append(resume_session_id)
        self._burst += 1
        yield SessionId(f"fake-session-{self._burst}")
        async for item in input_stream:
            self.prompts.append(item)
            self._interrupted.clear()
            async for ev in self._run(item):
                yield ev

    async def _run(self, item: QueuedItem) -> AsyncIterator[AgentEvent]:
        for step in self._script_for(item):
            if self._interrupted.is_set():
                yield TurnFinished(reason="interrupted")
                return
            if isinstance(step, Text):
                yield TextDelta(step.text)
            elif isinstance(step, Think):
                yield ThinkingDelta(step.text)
            elif isinstance(step, Emit):
                yield step.event
            elif isinstance(step, Finish):
                yield Usage(input_tokens=1, output_tokens=1, cache_read_tokens=0,
                            cache_creation_tokens=0)
                yield TurnFinished(reason=step.reason, error=step.error)
                return
            elif isinstance(step, Wait):
                waiters = {asyncio.ensure_future(step.event.wait()),
                           asyncio.ensure_future(self._interrupted.wait())}
                _, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()
            elif isinstance(step, Tool):
                tool_id = f"fake-tool-{next(self._tool_ids)}"
                yield ToolUse(id=tool_id, name=sdk_tool_name(step.name), input=dict(step.args))
                if self._ctx_factory is None:
                    raise RuntimeError("FakeAgent Tool step needs ctx_factory")
                tools = {t.name: t for t in build_sdk_tool_list(self._specs, self._ctx_factory)}
                raw = await tools[step.name].handler(dict(step.args))
                yield mcp_result_to_event(tool_id, raw)
        if self._interrupted.is_set():
            yield TurnFinished(reason="interrupted")
            return
        yield Usage(input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_creation_tokens=0)
        yield TurnFinished(reason="completed")
```

Note: a `Wait` step followed by an interrupt makes the *next* loop iteration
yield `TurnFinished("interrupted")`; with `ignore_interrupt=True` the wait
never ends unless the event is set (used by the stuck-Stop test, Task 5.10).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/agent/test_fake.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent/fake.py backend/tests/unit/agent/test_fake.py
git commit -m "feat: scriptable FakeAgent backend" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.8: `Broadcaster` per-project fan-out

**Files:**
- Create: `backend/src/copperchat/agent/broadcaster.py`
- Test: `backend/tests/unit/agent/test_broadcaster.py`

**Interfaces:**
- Consumes: `ServerFrame` models.
- Produces: `Broadcaster(max_queue: int = 1000)` with `async publish(project_id, frame) -> None`, `subscribe(project_id) -> Subscription`, `subscriber_count(project_id) -> int`; `Subscription` (async iterator of frames) with `close()`, `dropped: bool`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/agent/test_broadcaster.py
import asyncio

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.protocol import RunStartedFrame

P, Q = "a1b2c3d4e5f6", "ffffffffffff"


def frame(n: int, pid: str = P) -> RunStartedFrame:
    return RunStartedFrame(project_id=pid, run_id=f"r{n}")


async def test_fan_out_only_to_same_project():
    b = Broadcaster()
    s1, s2, other = b.subscribe(P), b.subscribe(P), b.subscribe(Q)
    await b.publish(P, frame(1))
    assert (await asyncio.wait_for(s1.__anext__(), 0.1)).run_id == "r1"
    assert (await asyncio.wait_for(s2.__anext__(), 0.1)).run_id == "r1"
    other.close()
    assert [f async for f in other] == []


async def test_slow_subscriber_is_dropped_and_closed():
    b = Broadcaster(max_queue=2)
    slow, fast = b.subscribe(P), b.subscribe(P)
    got: list[str] = []

    async def drain() -> None:
        async for f in fast:
            got.append(f.run_id)  # type: ignore[union-attr]
            if len(got) == 3:
                return

    t = asyncio.create_task(drain())
    for n in range(3):
        await b.publish(P, frame(n))
        await asyncio.sleep(0)
    await asyncio.wait_for(t, 0.5)
    assert got == ["r0", "r1", "r2"]
    assert slow.dropped is True
    assert [f async for f in slow] == []
    assert b.subscriber_count(P) == 1


async def test_close_unsubscribes():
    b = Broadcaster()
    s = b.subscribe(P)
    s.close()
    assert b.subscriber_count(P) == 0
    await b.publish(P, frame(1))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/agent/test_broadcaster.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'copperchat.agent.broadcaster'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/agent/broadcaster.py
"""Per-project fan-out of server frames with bounded per-subscriber queues."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from copperchat.agent.protocol import Wire

log = logging.getLogger(__name__)


class Subscription:
    """Async iterator of frames. A subscriber that falls `max_queue` frames behind is
    dropped: its iterator ends and `dropped` is True (the client reconnects and replays)."""

    def __init__(self, max_queue: int, on_close: Callable[[Subscription], None]) -> None:
        self._q: asyncio.Queue[Wire | None] = asyncio.Queue(maxsize=max_queue + 1)
        self._max = max_queue
        self._on_close = on_close
        self._closed = False
        self.dropped = False

    def _offer(self, frame: Wire) -> None:
        if self._closed:
            return
        if self._q.qsize() >= self._max:
            self.dropped = True
            log.warning("dropping slow websocket subscriber")
            self.close()
            return
        self._q.put_nowait(frame)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        while not self._q.empty():
            self._q.get_nowait()
        self._q.put_nowait(None)  # end-of-stream sentinel (queue has room for it)
        self._on_close(self)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Wire:
        frame = await self._q.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


class Broadcaster:
    def __init__(self, max_queue: int = 1000) -> None:
        self._max = max_queue
        self._subs: dict[str, set[Subscription]] = {}

    def subscribe(self, project_id: str) -> Subscription:
        sub = Subscription(self._max, lambda s: self._subs.get(project_id, set()).discard(s))
        self._subs.setdefault(project_id, set()).add(sub)
        return sub

    def subscriber_count(self, project_id: str) -> int:
        return len(self._subs.get(project_id, ()))

    async def publish(self, project_id: str, frame: Wire) -> None:
        for sub in list(self._subs.get(project_id, ())):
            sub._offer(frame)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/agent/test_broadcaster.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent/broadcaster.py backend/tests/unit/agent/test_broadcaster.py
git commit -m "feat: per-project frame broadcaster with slow-subscriber drop" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.9: `RunEngine` core — queue-first submit, gated bursts, persistence, hooks

Owns the queue half of **Review Focus 1** (a retransmitted `clientMessageId`
produces one queue entry and one agent turn).

**Files:**
- Create: `backend/src/copperchat/agent/engine.py`
- Create: `backend/tests/unit/agent/conftest.py`
- Test: `backend/tests/unit/agent/test_engine.py`

**Interfaces:**
- Consumes: `MessageRepo`, `RunRepo`, `AgentSessionRepo`, `RateLimitRepo`, `Broadcaster`, `PushStream`, `AgentBackend`, events, protocol frames, `KicadSessionManager.current()`, `KicadUnavailable`, `ToolContext`, `agent_dir`.
- Produces: `UnknownAttachment(CopperChatError)`, `TurnInfo`, `TurnOutcome`, `RunHooks`, `BackendFactory = Callable[[Callable[[], ToolContext]], AgentBackend]`, `RunEngine(*, project, settings, db, broadcaster, sessions, backend_factory, hooks=None, flush_interval_s=0.5, flush_chars=2048, interrupt_grace_s=5.0)` with `submit`, `submit_ex`, `post_notice`, `post_error`, `add_agent_note`, `replay_and_subscribe`, `to_dto`, `start`, `close`, `is_busy`, `current_run_id`, `rate_limited_until`, `backend`, `broadcaster`, `hooks`, `attachment_resolver`. Images are stored at `agent_dir(project)/images/<32 hex>.<ext>` and referenced as `/api/projects/{id}/images/{name}` (served in Task 5.12).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/agent/conftest.py
import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from pydantic import SecretStr

from copperchat.agent.broadcaster import Broadcaster, Subscription
from copperchat.agent.engine import RunEngine
from copperchat.agent.fake import FakeAgent
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.projects import Project

PID = "a1b2c3d4e5f6"


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "demo"
    d.mkdir()
    return Project(id=PID, name="demo", rel_path="demo/demo.kicad_pro", dir=d,
                   pro_file=d / "demo.kicad_pro", sch_file=None, pcb_file=None)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return Settings(_env_file=None, projects_root=tmp_path, data_dir=tmp_path / "data",
                    claude_code_oauth_token=SecretStr("t"))


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    d = await Database.open(tmp_path / "t.db")
    yield d
    await d.close()


@pytest_asyncio.fixture
async def make_engine(project: Project, settings: Settings, db: Database):
    engines: list[RunEngine] = []

    async def make(script_for: Any = None, *, ignore_interrupt: bool = False, specs: Any = (),
                   with_session: bool = False, start: bool = True,
                   **kw: Any) -> tuple[RunEngine, FakeAgent]:
        box: dict[str, FakeAgent] = {}
        session = SimpleNamespace(project=project) if with_session else None

        def factory(ctx_factory: Callable[[], Any]) -> FakeAgent:
            box["agent"] = FakeAgent(script_for, specs=specs, ctx_factory=ctx_factory,
                                     ignore_interrupt=ignore_interrupt)
            return box["agent"]

        engine = RunEngine(project=project, settings=settings, db=db, broadcaster=Broadcaster(),
                           sessions=cast(Any, SimpleNamespace(current=lambda: session)),
                           backend_factory=factory, **kw)
        if start:
            await engine.start()
        engines.append(engine)
        return engine, box["agent"]

    yield make
    for e in engines:
        await e.close()


@pytest.fixture
def until() -> Callable[..., Any]:
    async def _until(pred: Callable[[], bool], timeout: float = 2.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not pred():
            if loop.time() > deadline:
                raise AssertionError("condition not reached in time")
            await asyncio.sleep(0.005)

    return _until


@pytest.fixture
def drain() -> Callable[..., Any]:
    async def _drain(sub: Subscription, quiet: float = 0.05) -> list[Any]:
        frames: list[Any] = []
        while True:
            try:
                frames.append(await asyncio.wait_for(sub.__anext__(), quiet))
            except (TimeoutError, StopAsyncIteration):
                return frames

    return _drain
```

```python
# backend/tests/unit/agent/test_engine.py
import asyncio
from typing import Any

from copperchat.agent.engine import RunHooks, TurnInfo, TurnOutcome
from copperchat.agent.fake import Text, Think, Tool, Wait
from copperchat.db.repos.agent_state import RunRepo
from copperchat.db.repos.messages import MessageRepo
from copperchat.projects import agent_dir
from copperchat.tools import ImageBlock, ToolContext, ToolResult, ToolSpec

PID = "a1b2c3d4e5f6"


async def _rows(db):  # type: ignore[no-untyped-def]
    rows, _ = await MessageRepo(db).page(PID, limit=100)
    return rows


async def test_submit_persists_queued_first_then_streams_and_finishes(make_engine, db, until, drain):
    engine, agent = await make_engine()
    sub = engine.broadcaster.subscribe(PID)
    dto, created = await engine.submit_ex("c1", "hi", [])
    assert created and dto.status == "queued" and dto.client_message_id == "c1"
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy)
    rows = await _rows(db)
    assert [(r.role, r.kind, r.status) for r in rows] == [
        ("user", "text", "done"), ("assistant", "text", "done")]
    assert rows[1].content == "You said: hi" and rows[0].run_id == rows[1].run_id
    frames = await drain(sub)
    types = [f.type for f in frames]
    assert types[0] == "message" and frames[0].message.status == "queued"
    assert types.index("run-started") < types.index("message-delta", types.index("run-started"))
    assert types[-1] == "run-finished" and frames[-1].reason == "completed"
    assert "usage" in types
    assert all(f.project_id == PID for f in frames)
    run = await RunRepo(db).get(frames[-1].run_id)
    assert run is not None and run["reason"] == "completed"


async def test_retransmitted_client_message_id_is_one_turn(make_engine, db, until):
    gate = asyncio.Event()
    engine, agent = await make_engine(lambda item: [Wait(gate), Text("ok")])
    results = await asyncio.gather(*(engine.submit_ex("same", "hello", []) for _ in range(3)))
    assert sorted(created for _, created in results) == [False, False, True]
    assert len({dto.id for dto, _ in results}) == 1
    gate.set()
    await until(lambda: not engine.is_busy)
    await asyncio.sleep(0.05)
    assert [p.client_message_id for p in agent.prompts] == ["same"]
    assert len([r for r in await _rows(db) if r.role == "user"]) == 1


async def test_next_message_is_released_only_after_turn_finished(make_engine, db, until):
    gate = asyncio.Event()
    engine, agent = await make_engine(
        lambda item: [Wait(gate), Text("one")] if item.text == "m1" else [Text("two")])
    m1, _ = await engine.submit_ex("a", "m1", [])
    m2, _ = await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 1)
    await asyncio.sleep(0.05)
    assert len(agent.prompts) == 1, "m2 must not reach the backend while m1 runs"
    row2 = await MessageRepo(db).get(m2.id)
    assert row2 is not None and row2.status == "queued"
    gate.set()
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    assert agent.resume_ids == [None], "both turns ran in one burst"
    users = [r for r in await _rows(db) if r.role == "user"]
    assert users[0].run_id != users[1].run_id


async def test_streamed_text_is_persisted_by_size_and_by_time(make_engine, db, until):
    gate = asyncio.Event()
    engine, _ = await make_engine(lambda item: [Text("a" * 3000), Text("b"), Wait(gate)],
                                  flush_interval_s=0.05)
    await engine.submit_ex("c", "go", [])
    repo = MessageRepo(db)

    async def assistant_content() -> str:
        rows = [r for r in await _rows(db) if r.role == "assistant"]
        return rows[0].content if rows else ""

    await until(lambda: engine.current_run_id is not None)
    await asyncio.sleep(0.2)
    assert await assistant_content() == "a" * 3000 + "b"
    [row] = [r for r in await _rows(db) if r.role == "assistant"]
    assert row.status == "streaming"
    assert engine.to_dto(row).content == "a" * 3000 + "b"
    gate.set()
    await until(lambda: not engine.is_busy)
    done = await repo.get(row.id)
    assert done is not None and done.status == "done"


async def test_thinking_tool_use_result_and_image_are_separate_rows(make_engine, db, until, project):
    async def probe(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(data={"ok": 1}, images=[ImageBlock(mime="image/png", data=b"PNG")])

    spec = ToolSpec(name="probe", description="d", input_schema={"type": "object"}, handler=probe)
    engine, _ = await make_engine(
        lambda item: [Think("hmm"), Text("x"), Tool("probe", {"a": 1}), Text("y")],
        specs=[spec], with_session=True)
    await engine.submit_ex("c", "go", [])
    await until(lambda: not engine.is_busy and engine.current_run_id is None)
    await asyncio.sleep(0.05)
    rows = await _rows(db)
    assert [r.kind for r in rows] == ["text", "thinking", "text", "tool_use", "tool_result",
                                      "image", "text"]
    tool_use, tool_result, image = rows[3], rows[4], rows[5]
    assert tool_use.content == "probe" and tool_use.metadata["input"] == {"a": 1}
    assert tool_result.metadata["toolUseId"] == tool_use.metadata["toolUseId"]
    url = image.metadata["url"]
    assert url.startswith(f"/api/projects/{PID}/images/")
    assert (agent_dir(project) / "images" / url.rsplit("/", 1)[1]).read_bytes() == b"PNG"
    assert tool_result.metadata["images"] == [{"url": url}]


async def test_hooks_and_agent_notes(make_engine, until):
    calls: list[Any] = []

    class Rec(RunHooks):
        async def on_engine_start(self) -> None:
            calls.append("engine")

        async def on_turn_start(self, turn: TurnInfo) -> None:
            calls.append(("start", turn.text))

        async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
            calls.append(("end", outcome.reason))

    engine, agent = await make_engine(hooks=Rec())
    engine.add_agent_note("history restored")
    await engine.submit_ex("c", "hi", [])
    await until(lambda: ("end", "completed") in calls)
    assert calls == ["engine", ("start", "hi"), ("end", "completed")]
    assert agent.prompts[0].text == "[CopperChat notice] history restored\n\nhi"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/agent/test_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'copperchat.agent.engine'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/agent/engine.py
"""Per-project run engine: queue-first persistence, gated input, streaming, hooks."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from copperchat.agent.broadcaster import Broadcaster, Subscription
from copperchat.agent.events import (
    AgentBackend, AgentEvent, QueuedItem, RateLimited, SessionId, TextDelta, ThinkingDelta,
    ToolResultEvt, ToolUse, TurnFinished, Usage,
)
from copperchat.agent.protocol import (
    AttachmentRef, FinishReason, MessageDeltaFrame, MessageDTO, MessageFrame, RateLimitPayload,
    RunFinishedFrame, RunStartedFrame, UsageFrame, UsagePayload, ViewUpdatedFrame, Wire,
)
from copperchat.agent.push_stream import PushStream
from copperchat.agent.sdk_tools import SERVER_NAME
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.agent_state import AgentSessionRepo, RateLimitRepo, RunRepo
from copperchat.db.repos.messages import MessageRepo, MessageRow
from copperchat.errors import CopperChatError
from copperchat.kicad_session import DocKind, KicadSessionManager, KicadUnavailable
from copperchat.projects import Project, agent_dir
from copperchat.tools import ImageBlock, ToolContext

log = logging.getLogger(__name__)
_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class UnknownAttachment(CopperChatError):
    pass


@dataclass(frozen=True)
class TurnInfo:
    run_id: str
    message_id: int
    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class TurnOutcome:
    reason: FinishReason
    changed_docs: frozenset[DocKind] = frozenset()
    mutated: bool = False
    kicad_failed: bool = False


class RunHooks:
    """No-op extension points; Phase 7 subclasses this."""

    async def on_engine_start(self) -> None:
        return None

    async def on_turn_start(self, turn: TurnInfo) -> None:
        return None

    async def on_first_mutation(self, turn: TurnInfo) -> None:
        return None

    async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
        return None


BackendFactory = Callable[[Callable[[], ToolContext]], AgentBackend]


@dataclass
class _Segment:
    id: int
    kind: str
    content: str = ""
    flushed_len: int = 0
    last_flush: float = 0.0


@dataclass
class _Turn:
    info: TurnInfo
    seg: _Segment | None = None
    usage: dict[str, Any] | None = None
    rate_limited: datetime | None = None
    changed: set[DocKind] = field(default_factory=set)
    mutated: bool = False
    kicad_failed: bool = False
    kicad_reported: bool = False


class RunEngine:
    def __init__(
        self,
        *,
        project: Project,
        settings: Settings,
        db: Database,
        broadcaster: Broadcaster,
        sessions: KicadSessionManager,
        backend_factory: BackendFactory,
        hooks: RunHooks | None = None,
        flush_interval_s: float = 0.5,
        flush_chars: int = 2048,
        interrupt_grace_s: float = 5.0,
    ) -> None:
        self.project = project
        self.broadcaster = broadcaster
        self.hooks = hooks or RunHooks()
        self.attachment_resolver: Callable[[str], AttachmentRef | None] = lambda _id: None
        self.rate_limited_until: datetime | None = None
        self._settings = settings
        self._sessions = sessions
        self._messages = MessageRepo(db)
        self._runs = RunRepo(db)
        self._agent_sessions = AgentSessionRepo(db)
        self._rate_limits = RateLimitRepo(db)
        self._flush_interval = flush_interval_s
        self._flush_chars = flush_chars
        self._grace = interrupt_grace_s
        self._lock = asyncio.Lock()  # serialises persist+publish against replay snapshots
        self._stream: PushStream[QueuedItem] | None = None
        self._burst: asyncio.Task[None] | None = None
        self._turn: _Turn | None = None
        self._inflight: int | None = None  # pulled from the stream, turn not finished
        self._live: dict[int, str] = {}  # streaming row id -> full content incl. unflushed
        self._notes: list[str] = []
        self._interrupting = False
        self._closed = False
        self._resume_timer: asyncio.TimerHandle | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self.backend: AgentBackend = backend_factory(self._make_ctx)

    # ---- public surface
    @property
    def pid(self) -> str:
        return self.project.id

    @property
    def is_busy(self) -> bool:
        return self._burst is not None and not self._burst.done()

    @property
    def current_run_id(self) -> str | None:
        return self._turn.info.run_id if self._turn else None

    async def start(self) -> None:
        await self._messages.finalize_streaming(self.pid)
        await self.hooks.on_engine_start()
        await self._restore()

    async def close(self) -> None:
        self._closed = True
        if self._resume_timer is not None:
            self._resume_timer.cancel()
        task = self._burst
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        for t in list(self._bg):
            t.cancel()

    async def submit(
        self, client_message_id: str, text: str, attachments: list[AttachmentRef]
    ) -> MessageDTO:
        dto, _ = await self.submit_ex(client_message_id, text, attachments)
        return dto

    async def submit_ex(
        self,
        client_message_id: str,
        text: str,
        attachments: list[AttachmentRef],
        metadata: dict[str, Any] | None = None,
    ) -> tuple[MessageDTO, bool]:
        for att in attachments:
            if self.attachment_resolver(att.id) is None:
                raise UnknownAttachment(f"unknown attachment: {att.id}")
        meta = dict(metadata or {})
        if attachments:
            meta["attachments"] = [a.model_dump(mode="json", by_alias=True) for a in attachments]
        async with self._lock:
            row, created = await self._messages.insert_user(
                self.pid, client_message_id, text, meta)
            dto = self.to_dto(row)
            await self._publish(MessageFrame(project_id=self.pid, message=dto))
        if created and self.rate_limited_until is None:
            self._enqueue(self._item(row))
        return dto, created

    async def post_notice(self, text: str, metadata: dict[str, Any] | None = None) -> MessageDTO:
        async with self._lock:
            row = await self._insert(role="system", kind="notice", content=text, metadata=metadata)
        return self.to_dto(row)

    async def post_error(self, text: str, metadata: dict[str, Any] | None = None) -> MessageDTO:
        async with self._lock:
            row = await self._insert(role="system", kind="error", content=text, metadata=metadata)
        return self.to_dto(row)

    def add_agent_note(self, text: str) -> None:
        self._notes.append(text)

    async def replay_and_subscribe(
        self, since_message_id: int
    ) -> tuple[list[MessageDTO], Subscription, str | None]:
        """Snapshot + subscribe atomically w.r.t. publishes: no gap, no duplicate delta."""
        async with self._lock:
            rows = await self._messages.since(self.pid, since_message_id)
            dtos = [self.to_dto(r) for r in rows]
            sub = self.broadcaster.subscribe(self.pid)
            return dtos, sub, self.current_run_id

    def to_dto(self, row: MessageRow) -> MessageDTO:
        wire = row.to_wire()
        wire["content"] = self._live.get(row.id, row.content)
        return MessageDTO.model_validate(wire)

    # ---- queue and bursts
    def _item(self, row: MessageRow) -> QueuedItem:
        atts = tuple(AttachmentRef.model_validate(a) for a in row.metadata.get("attachments", []))
        return QueuedItem(message_id=row.id, client_message_id=row.client_message_id or "",
                          text=row.content, attachments=atts)

    def _enqueue(self, item: QueuedItem) -> None:
        if self._stream is not None and self._stream.push(item):
            return
        stream: PushStream[QueuedItem] = PushStream()
        stream.push(item)
        self._stream = stream
        self._burst = asyncio.create_task(self._run_burst(stream), name=f"burst-{self.pid}")

    async def _restore(self) -> None:
        await self._drive_queued()

    async def _drive_queued(self) -> None:
        if self._closed or self.rate_limited_until is not None:
            return
        known = set(self._stream.pending_ids()) if self._stream else set()
        for row in await self._messages.queued_user_rows(self.pid):
            if row.id not in known and row.id != self._inflight:
                self._enqueue(self._item(row))

    async def _run_burst(self, stream: PushStream[QueuedItem]) -> None:
        resume = await self._agent_sessions.get(self.pid)  # read once per burst, never mid-burst
        flusher = asyncio.create_task(self._flush_loop())
        try:
            async for ev in self.backend.start(self.project, self._gated(stream), resume):
                await self._on_event(ev, stream)
        except asyncio.CancelledError:
            if not self._interrupting:
                raise
            log.warning("burst for %s cancelled by the interrupt backstop", self.pid)
        except Exception as exc:
            log.exception("agent backend failed for %s", self.pid)
            if self._turn is not None:
                async with self._lock:
                    await self._insert(role="system", kind="error",
                                       content=f"The agent stopped unexpectedly: {exc}")
                await self._finish_turn("error")
        finally:
            flusher.cancel()
            if self._turn is not None:
                await self._finish_turn(
                    "interrupted" if (self._interrupting or self._closed) else "error")
            if self._stream is stream:
                self._stream = None
            if self._burst is asyncio.current_task():
                self._burst = None
            self._interrupting = False
            await self._drive_queued()

    async def _gated(self, stream: PushStream[QueuedItem]) -> AsyncIterator[QueuedItem]:
        async for item in stream:
            self._inflight = item.message_id
            yield await self._begin_turn(item)

    async def _begin_turn(self, item: QueuedItem) -> QueuedItem:
        run_id = str(uuid.uuid4())
        row = await self._messages.get(item.message_id)
        info = TurnInfo(run_id=run_id, message_id=item.message_id, text=item.text,
                        metadata=row.metadata if row else {})
        async with self._lock:
            self._turn = _Turn(info)
            await self._runs.start(run_id, self.pid)
            await self._messages.assign_run(item.message_id, run_id, "done")
            await self._publish(MessageDeltaFrame(
                project_id=self.pid, message_id=item.message_id, status_patch="done",
                metadata_patch={"runId": run_id}))
            await self._publish(RunStartedFrame(project_id=self.pid, run_id=run_id))
        try:
            await self.hooks.on_turn_start(info)
        except Exception:
            log.exception("on_turn_start hook failed")
        notes, self._notes = self._notes, []
        if not notes:
            return item
        prefix = "\n".join(f"[CopperChat notice] {n}" for n in notes)
        return replace(item, text=f"{prefix}\n\n{item.text}")

    # ---- events
    async def _on_event(self, ev: AgentEvent, stream: PushStream[QueuedItem]) -> None:
        if isinstance(ev, SessionId):
            await self._agent_sessions.set(self.pid, ev.id)
            return
        turn = self._turn
        if turn is None:
            log.debug("ignoring %r outside a turn", ev)
            return
        if isinstance(ev, TurnFinished):
            await self._turn_finished(turn, ev, stream)
            return
        async with self._lock:
            if isinstance(ev, TextDelta):
                await self._append(turn, "text", ev.text)
            elif isinstance(ev, ThinkingDelta):
                await self._append(turn, "thinking", ev.text)
            elif isinstance(ev, ToolUse):
                await self._close_segment(turn)
                name = ev.name.removeprefix(f"mcp__{SERVER_NAME}__")
                await self._insert(role="assistant", kind="tool_use", content=name,
                                   metadata={"toolUseId": ev.id, "name": name, "input": ev.input})
            elif isinstance(ev, ToolResultEvt):
                await self._close_segment(turn)
                urls = [self._store_image(img) for img in ev.images]
                await self._insert(role="assistant", kind="tool_result", content=ev.content,
                                   metadata={"toolUseId": ev.id, "isError": ev.is_error,
                                             "images": [{"url": u} for u in urls]})
                for url in urls:
                    await self._insert(role="assistant", kind="image", content="",
                                       metadata={"toolUseId": ev.id, "url": url})
            elif isinstance(ev, Usage):
                payload = UsagePayload(
                    input_tokens=ev.input_tokens, output_tokens=ev.output_tokens,
                    cache_read_tokens=ev.cache_read_tokens,
                    cache_creation_tokens=ev.cache_creation_tokens, cost_usd=ev.cost_usd,
                    rate_limit=RateLimitPayload(
                        status=ev.rate_limit_status,
                        resets_at=ev.rate_limit_resets_at.isoformat()
                        if ev.rate_limit_resets_at else None,
                    ) if ev.rate_limit_status else None,
                )
                turn.usage = payload.model_dump(mode="json", by_alias=True)
                await self._publish(UsageFrame(project_id=self.pid, usage=payload))
            elif isinstance(ev, RateLimited):
                turn.rate_limited = ev.resets_at
        if turn.kicad_failed and not turn.kicad_reported:
            turn.kicad_reported = True
            async with self._lock:
                await self._insert(
                    role="system", kind="error",
                    content="KiCad stopped responding; the KiCad API server was restarted and "
                            "this turn ended. Send your message again to retry.",
                    metadata={"code": "kicad_unavailable"})
            await self.backend.interrupt()

    async def _turn_finished(
        self, turn: _Turn, ev: TurnFinished, stream: PushStream[QueuedItem]
    ) -> None:
        if ev.reason == "error" and ev.error:
            async with self._lock:
                await self._insert(role="system", kind="error", content=ev.error)
        await self._finish_turn("error" if turn.kicad_failed else ev.reason)
        stream.release_next()
        if stream.pending_count() == 0:
            stream.request_close()

    async def _finish_turn(self, reason: FinishReason) -> None:
        turn = self._turn
        if turn is None:
            return
        async with self._lock:
            await self._close_segment(turn)
            await self._runs.finish(turn.info.run_id, reason, turn.usage)
            await self._publish(RunFinishedFrame(project_id=self.pid, run_id=turn.info.run_id,
                                                 reason=reason))
            self._turn = None
            self._inflight = None
        outcome = TurnOutcome(reason=reason, changed_docs=frozenset(turn.changed),
                              mutated=turn.mutated, kicad_failed=turn.kicad_failed)
        try:
            await self.hooks.on_turn_end(turn.info, outcome)
        except Exception:
            log.exception("on_turn_end hook failed")

    # ---- persistence helpers (caller holds self._lock)
    async def _publish(self, frame: Wire) -> None:
        await self.broadcaster.publish(self.pid, frame)

    async def _insert(self, *, role: str, kind: str, content: str,
                      metadata: dict[str, Any] | None = None, status: str = "done") -> MessageRow:
        mid = await self._messages.insert(self.pid, role=role, kind=kind, content=content,
                                          status=status, run_id=self.current_run_id,
                                          metadata=metadata)
        row = await self._messages.get(mid)
        if row is None:
            raise RuntimeError(f"message {mid} vanished after insert")
        await self._publish(MessageFrame(project_id=self.pid, message=self.to_dto(row)))
        return row

    async def _append(self, turn: _Turn, kind: str, text: str) -> None:
        seg = turn.seg
        if seg is not None and seg.kind != kind:
            await self._close_segment(turn)
            seg = None
        if seg is None:
            row = await self._insert(role="assistant", kind=kind, content="", status="streaming")
            seg = turn.seg = _Segment(id=row.id, kind=kind, last_flush=time.monotonic())
        seg.content += text
        self._live[seg.id] = seg.content
        await self._publish(MessageDeltaFrame(project_id=self.pid, message_id=seg.id,
                                              content_delta=text))
        if (len(seg.content) - seg.flushed_len >= self._flush_chars
                or time.monotonic() - seg.last_flush >= self._flush_interval):
            await self._flush(seg)

    async def _flush(self, seg: _Segment) -> None:
        await self._messages.set_content(seg.id, seg.content)
        seg.flushed_len = len(seg.content)
        seg.last_flush = time.monotonic()

    async def _close_segment(self, turn: _Turn) -> None:
        seg = turn.seg
        if seg is None:
            return
        await self._flush(seg)
        await self._messages.set_status(seg.id, "done")
        self._live.pop(seg.id, None)
        turn.seg = None
        await self._publish(MessageDeltaFrame(project_id=self.pid, message_id=seg.id,
                                              status_patch="done"))

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            async with self._lock:
                turn = self._turn
                if turn and turn.seg and len(turn.seg.content) > turn.seg.flushed_len:
                    await self._flush(turn.seg)

    def _store_image(self, img: ImageBlock) -> str:
        name = f"{uuid.uuid4().hex}.{_IMAGE_EXT.get(img.mime, 'bin')}"
        d = agent_dir(self.project) / "images"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(img.data)
        return f"/api/projects/{self.pid}/images/{name}"

    # ---- tool context
    def _make_ctx(self) -> ToolContext:
        session = self._sessions.current()
        if session is None or session.project.id != self.pid:
            raise KicadUnavailable("the project is not open in KiCad")
        turn = self._turn
        return ToolContext(
            project=self.project, session=session, settings=self._settings,
            run_id=turn.info.run_id if turn else None, on_mutation=self._on_mutation,
            user_message=turn.info.text if turn else None,
            on_documents_changed=self._on_documents_changed,
            on_kicad_unavailable=self._on_kicad_unavailable,
        )

    async def _on_mutation(self) -> None:
        turn = self._turn
        if turn is not None and not turn.mutated:
            turn.mutated = True
            await self.hooks.on_first_mutation(turn.info)

    def _on_documents_changed(self, docs: set[DocKind]) -> None:
        if self._turn is not None:
            self._turn.changed |= docs
        self._spawn(self._publish(ViewUpdatedFrame(
            project_id=self.pid, docs=sorted(docs), version=int(time.time() * 1000))))

    def _on_kicad_unavailable(self, exc: Exception) -> None:
        log.warning("KiCad unavailable during turn: %s", exc)
        if self._turn is not None:
            self._turn.kicad_failed = True  # reported + interrupted after the tool result

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task
```

Task 5.10 adds `cancel_queued` / `interrupt`; Task 5.11 adds rate-limit
parking and replaces `_restore` / `_turn_finished`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/agent/test_engine.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent/engine.py backend/tests/unit/agent/conftest.py backend/tests/unit/agent/test_engine.py
git commit -m "feat: run engine with queue-first submit, gated bursts and streaming persistence" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.10: Cancel queued, interrupt, abort backstop

**Files:**
- Modify: `backend/src/copperchat/agent/engine.py`
- Test: `backend/tests/unit/agent/test_engine_interrupt.py`

**Interfaces:**
- Produces: `RunEngine.cancel_queued(message_id: int) -> bool`, `RunEngine.interrupt(run_id: str | None) -> None` (C7).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/agent/test_engine_interrupt.py
import asyncio

from copperchat.agent.fake import Text, Wait
from copperchat.db.repos.agent_state import RunRepo
from copperchat.db.repos.messages import MessageRepo

PID = "a1b2c3d4e5f6"


async def test_cancel_queued_before_pull(make_engine, db, until, drain):
    gate = asyncio.Event()
    engine, agent = await make_engine(lambda item: [Wait(gate), Text("x")])
    await engine.submit_ex("a", "m1", [])
    m2, _ = await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 1)
    sub = engine.broadcaster.subscribe(PID)
    assert await engine.cancel_queued(m2.id) is True
    frames = await drain(sub)
    assert any(f.type == "message-delta" and f.status_patch == "cancelled" for f in frames)
    gate.set()
    await until(lambda: not engine.is_busy)
    await asyncio.sleep(0.05)
    assert [p.text for p in agent.prompts] == ["m1"]
    row = await MessageRepo(db).get(m2.id)
    assert row is not None and row.status == "cancelled"


async def test_cancel_after_pull_returns_false_and_message_proceeds(make_engine, db, until):
    gate = asyncio.Event()
    engine, agent = await make_engine(lambda item: [Wait(gate), Text("x")])
    m1, _ = await engine.submit_ex("a", "m1", [])
    await until(lambda: len(agent.prompts) == 1)
    assert await engine.cancel_queued(m1.id) is False
    gate.set()
    await until(lambda: not engine.is_busy)
    rows, _ = await MessageRepo(db).page(PID, limit=10)
    assert rows[0].status == "done" and rows[-1].content == "x"


async def test_interrupt_cancels_queue_closes_stream_and_ends_run(make_engine, db, until):
    never = asyncio.Event()
    engine, agent = await make_engine(lambda item: [Text("a"), Wait(never)])
    await engine.submit_ex("a", "m1", [])
    m2, _ = await engine.submit_ex("b", "m2", [])
    await until(lambda: engine.current_run_id is not None)
    run_id = engine.current_run_id
    await engine.interrupt(run_id)
    await until(lambda: not engine.is_busy)
    assert agent.interrupts == 1
    run = await RunRepo(db).get(run_id)  # type: ignore[arg-type]
    assert run is not None and run["reason"] == "interrupted"
    row = await MessageRepo(db).get(m2.id)
    assert row is not None and row.status == "cancelled"
    assert [p.text for p in agent.prompts] == ["m1"]


async def test_stuck_stop_backstop_cancels_burst(make_engine, db, until, drain):
    never = asyncio.Event()
    engine, agent = await make_engine(lambda item: [Wait(never)], ignore_interrupt=True,
                                      interrupt_grace_s=0.1)
    await engine.submit_ex("a", "m1", [])
    await until(lambda: engine.current_run_id is not None)
    run_id = engine.current_run_id
    sub = engine.broadcaster.subscribe(PID)
    await engine.interrupt(None)
    await asyncio.sleep(0.05)
    assert engine.is_busy, "backend ignores interrupt; the backstop has not fired yet"
    await until(lambda: not engine.is_busy, timeout=2.0)
    frames = await drain(sub)
    assert any(f.type == "run-finished" and f.reason == "interrupted" for f in frames)
    run = await RunRepo(db).get(run_id)  # type: ignore[arg-type]
    assert run is not None and run["reason"] == "interrupted"
    # the engine is usable again afterwards (a fresh burst pulls the next message)
    await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 2)


async def test_interrupt_for_other_run_is_ignored(make_engine, until):
    gate = asyncio.Event()
    engine, agent = await make_engine(lambda item: [Wait(gate)])
    await engine.submit_ex("a", "m1", [])
    await until(lambda: engine.current_run_id is not None)
    await engine.interrupt("some-old-run")
    assert agent.interrupts == 0
    gate.set()
```

Note: in `test_stuck_stop_backstop_cancels_burst` the second message runs a
fresh burst whose script also waits forever; the fixture's `close()` cancels
it at teardown.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/agent/test_engine_interrupt.py -v`
Expected: FAIL with `AttributeError: 'RunEngine' object has no attribute 'cancel_queued'`.

- [ ] **Step 3: Write minimal implementation**

Add to `RunEngine` (public surface section):

```python
    async def cancel_queued(self, message_id: int) -> bool:
        """True if the message was still waiting; False once the engine pulled it."""
        async with self._lock:
            row = await self._messages.get(message_id)
            if (row is None or row.project_id != self.pid or row.role != "user"
                    or row.status != "queued" or message_id == self._inflight):
                return False
            stream = self._stream
            if stream is not None and not stream.drop(message_id):
                return False  # already pulled: the message proceeds
            await self._cancel_row(message_id)
            return True

    async def interrupt(self, run_id: str | None) -> None:
        current = self.current_run_id
        if run_id is not None and current is not None and run_id != current:
            return  # stale Stop for an older run
        async with self._lock:
            stream = self._stream
            if stream is not None:
                ids = stream.pending_ids()
                for mid in ids:
                    stream.drop(mid)
            else:
                ids = [r.id for r in await self._messages.queued_user_rows(self.pid)
                       if r.id != self._inflight]
            for mid in ids:
                await self._cancel_row(mid)
        task = self._burst
        if task is None or task.done():
            return
        self._interrupting = True
        if stream is not None:
            stream.request_close()
        try:
            await self.backend.interrupt()
        except Exception:
            log.exception("backend.interrupt() failed for %s", self.pid)
        self._spawn(self._backstop(task))

    async def _backstop(self, task: asyncio.Task[None]) -> None:
        done, _ = await asyncio.wait({task}, timeout=self._grace)
        if not done:
            log.warning("agent ignored interrupt for %.1fs; cancelling burst", self._grace)
            task.cancel()

    async def _cancel_row(self, message_id: int) -> None:
        await self._messages.set_status(message_id, "cancelled")
        await self._publish(MessageDeltaFrame(project_id=self.pid, message_id=message_id,
                                              status_patch="cancelled"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/agent/test_engine_interrupt.py tests/unit/agent/test_engine.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent/engine.py backend/tests/unit/agent/test_engine_interrupt.py
git commit -m "feat: queued-message cancel and interrupt with abort backstop" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.11: Session resume, rate-limit parking/auto-resume, KiCad failure mid-turn

**Files:**
- Modify: `backend/src/copperchat/agent/engine.py`
- Test: `backend/tests/unit/agent/test_engine_resume.py`

**Interfaces:**
- Consumes: `AgentSessionRepo`, `RateLimitRepo`, `RateLimited`, `RateLimitedFrame`.
- Produces: `RunEngine.rate_limited_until` semantics: set while parked (in memory and in `rate_limit_state`), cleared by the auto-resume timer; parked messages stay `queued` with `metadata.rateLimited = true` on the in-flight one.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/agent/test_engine_resume.py
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from copperchat.agent.events import RateLimited
from copperchat.agent.fake import Emit, Finish, Text, Tool, Wait
from copperchat.db.repos.agent_state import AgentSessionRepo, RateLimitRepo, RunRepo
from copperchat.db.repos.messages import MessageRepo
from copperchat.kicad_session import KicadUnavailable
from copperchat.tools import ToolContext, ToolResult, ToolSpec

PID = "a1b2c3d4e5f6"


async def test_session_id_stored_and_resume_only_at_burst_start(make_engine, db, until):
    gate = asyncio.Event()
    engine, agent = await make_engine(
        lambda item: [Wait(gate), Text("1")] if item.text == "m1" else [Text("n")])
    await engine.submit_ex("a", "m1", [])
    await until(lambda: len(agent.prompts) == 1)
    await engine.submit_ex("b", "m2", [])  # joins the running burst
    gate.set()
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    assert agent.resume_ids == [None], "no resume mid-burst"
    assert await AgentSessionRepo(db).get(PID) == "fake-session-1"
    await engine.submit_ex("c", "m3", [])  # new burst
    await until(lambda: len(agent.prompts) == 3 and not engine.is_busy)
    assert agent.resume_ids == [None, "fake-session-1"]
    assert await AgentSessionRepo(db).get(PID) == "fake-session-2"


async def test_rate_limit_parks_publishes_and_auto_resumes(make_engine, db, until, drain):
    gate = asyncio.Event()
    seen = {"m1": 0}

    def script(item: Any) -> list[Any]:
        if item.text == "m1":
            seen["m1"] += 1
            if seen["m1"] == 1:
                resets = datetime.now(UTC) + timedelta(seconds=0.4)
                return [Text("partial"), Wait(gate), Emit(RateLimited(resets_at=resets)),
                        Finish("error", "rate limited")]
        return [Text(f"done {item.text}")]

    engine, agent = await make_engine(script)
    sub = engine.broadcaster.subscribe(PID)
    m1, _ = await engine.submit_ex("a", "m1", [])
    await until(lambda: len(agent.prompts) == 1)
    m2, _ = await engine.submit_ex("b", "m2", [])
    gate.set()
    await until(lambda: engine.rate_limited_until is not None)
    repo = MessageRepo(db)
    r1, r2 = await repo.get(m1.id), await repo.get(m2.id)
    assert r1 is not None and r1.status == "queued" and r1.metadata["rateLimited"] is True
    assert r2 is not None and r2.status == "queued"
    assert await RateLimitRepo(db).get(PID) == engine.rate_limited_until
    m3, _ = await engine.submit_ex("c", "m3", [])  # stays queued while parked
    await asyncio.sleep(0.05)
    assert len(agent.prompts) == 1
    frames = await drain(sub)
    assert any(f.type == "rate-limited" for f in frames)
    assert any(f.type == "run-finished" and f.reason == "rate_limited" for f in frames)
    await until(lambda: len(agent.prompts) == 4 and not engine.is_busy, timeout=3.0)
    assert [p.text for p in agent.prompts] == ["m1", "m1", "m2", "m3"]
    assert engine.rate_limited_until is None and await RateLimitRepo(db).get(PID) is None


async def test_rate_limit_survives_restart(make_engine, db, until):
    await MessageRepo(db).insert_user(PID, "c1", "waiting")
    await RateLimitRepo(db).set(PID, datetime.now(UTC) + timedelta(seconds=0.3))
    engine, agent = await make_engine()
    assert engine.rate_limited_until is not None
    await asyncio.sleep(0.05)
    assert agent.prompts == []
    await until(lambda: len(agent.prompts) == 1 and not engine.is_busy, timeout=3.0)
    assert await RateLimitRepo(db).get(PID) is None


async def test_expired_rate_limit_at_start_runs_queue_immediately(make_engine, db, until):
    await MessageRepo(db).insert_user(PID, "c1", "waiting")
    await RateLimitRepo(db).set(PID, datetime.now(UTC) - timedelta(seconds=5))
    engine, agent = await make_engine()
    await until(lambda: len(agent.prompts) == 1)
    assert await RateLimitRepo(db).get(PID) is None


async def test_kicad_unavailable_ends_turn_with_error_row_and_next_runs(make_engine, db, until):
    async def boom(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        raise KicadUnavailable("api-server timed out")

    spec = ToolSpec(name="boom", description="d", input_schema={"type": "object"}, handler=boom)
    engine, agent = await make_engine(
        lambda item: [Tool("boom"), Text("after")] if item.text == "m1" else [Text("two")],
        specs=[spec], with_session=True)
    m1, _ = await engine.submit_ex("a", "m1", [])
    await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)
    rows, _ = await MessageRepo(db).page(PID, limit=50)
    errors = [r for r in rows if r.kind == "error"]
    assert len(errors) == 1 and "KiCad" in errors[0].content
    assert errors[0].metadata == {"code": "kicad_unavailable"}   # the only "KiCad stopped responding" row (Phase 7 does not post another)
    assert not any(r.kind == "text" and r.content == "after" for r in rows)
    assert any(r.kind == "text" and r.content == "two" for r in rows)
    first = await MessageRepo(db).get(m1.id)
    assert first is not None and first.run_id is not None
    run = await RunRepo(db).get(first.run_id)
    assert run is not None and run["reason"] == "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/agent/test_engine_resume.py -v`
Expected: `test_session_id_stored_and_resume_only_at_burst_start` and the KiCad test pass already (Task 5.9 behaviour, kept as regression guards); the three rate-limit tests FAIL (`rate_limited_until` stays `None`).

- [ ] **Step 3: Write minimal implementation**

In `engine.py` change the import to `from datetime import UTC, datetime` and add
`RateLimitedFrame` to the protocol import. Replace `_restore` and
`_turn_finished`, and add the parking helpers:

```python
    async def _restore(self) -> None:
        resets = await self._rate_limits.get(self.pid)
        if resets is not None and resets > datetime.now(UTC):
            self.rate_limited_until = resets  # parked across the restart
            self._arm_resume(resets)
            return
        if resets is not None:
            await self._rate_limits.set(self.pid, None)
        await self._drive_queued()

    async def _turn_finished(
        self, turn: _Turn, ev: TurnFinished, stream: PushStream[QueuedItem]
    ) -> None:
        if turn.rate_limited is not None:
            await self._park(turn, stream, turn.rate_limited)
            return
        if ev.reason == "error" and ev.error:
            async with self._lock:
                await self._insert(role="system", kind="error", content=ev.error)
        await self._finish_turn("error" if turn.kicad_failed else ev.reason)
        stream.release_next()
        if stream.pending_count() == 0:
            stream.request_close()

    async def _park(
        self, turn: _Turn, stream: PushStream[QueuedItem], resets_at: datetime
    ) -> None:
        self.rate_limited_until = resets_at  # set first: the burst's finally must not re-drive
        async with self._lock:
            for mid in stream.pending_ids():
                stream.drop(mid)  # they stay 'queued' in the DB
            mid = turn.info.message_id
            await self._messages.set_status(mid, "queued")
            await self._messages.merge_metadata(mid, {"rateLimited": True})
            await self._publish(MessageDeltaFrame(project_id=self.pid, message_id=mid,
                                                  status_patch="queued",
                                                  metadata_patch={"rateLimited": True}))
        stream.request_close()
        stream.release_next()
        await self._finish_turn("rate_limited")
        await self._rate_limits.set(self.pid, resets_at)
        await self._publish(RateLimitedFrame(project_id=self.pid, resets_at=resets_at.isoformat()))
        self._arm_resume(resets_at)

    def _arm_resume(self, resets_at: datetime) -> None:
        if self._resume_timer is not None:
            self._resume_timer.cancel()
        delay = max(0.0, (resets_at - datetime.now(UTC)).total_seconds())
        self._resume_timer = asyncio.get_running_loop().call_later(
            delay, lambda: self._spawn(self._resume_after_limit()))

    async def _resume_after_limit(self) -> None:
        self._resume_timer = None
        self.rate_limited_until = None
        await self._rate_limits.set(self.pid, None)
        await self._drive_queued()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/agent -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent/engine.py backend/tests/unit/agent/test_engine_resume.py
git commit -m "feat: session resume per burst, rate-limit parking with auto-resume, KiCad failure handling" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.11b: Sub-agent events (`parent_tool_use_id`) — mapper and engine

Owner decision 2026-10-16: Phase 8 runs `part-researcher` as a real SDK
sub-agent behind the built-in `Agent` tool. Its messages arrive in the same
SDK stream, tagged with `parent_tool_use_id` (the id of the main agent's
`Agent` tool call; verified by spike 1A h4c). Phase 5 makes the runtime carry
them generically, so Phase 8 only wires the sub-agent and Phase 6A renders a
nested card. Until Phase 8 the main agent has `tools=[]` and no sub-agent
messages occur; the code path is exercised by unit tests only.

Rules:
- Sub-agent `StreamEvent`s are ignored (no partial deltas for sub-agents;
  their text arrives as whole `AssistantMessage` blocks).
- Sub-agent `AssistantMessage`: `ToolUseBlock` → `ToolUse(..., parent_tool_use_id=p)`,
  `TextBlock` → `SubagentText(p, text)`, `ThinkingBlock` ignored.
- Sub-agent `UserMessage`: `ToolResultBlock` → `ToolResultEvt(..., parent_tool_use_id=p)`;
  string content (the delegation prompt echoed to the sub-agent) ignored.
- Engine: sub-agent events become **complete** rows (`status="done"`) with
  `metadata.parentToolUseId = p`; they never append to or close the main
  agent's streaming text segment. String values in a sub-agent `tool_use`
  input are truncated to 2000 characters, sub-agent `tool_result` content to
  4000 characters (a `Write` of a full `notes.md` or a fetched web page must
  not bloat the chat). Images in sub-agent tool results are dropped.
- The main agent's `Agent` tool call and its final `tool_result` (the
  sub-agent's summary) are ordinary main-agent rows (`parentToolUseId`
  absent); `metadata.name` is the bare SDK tool name (`"Agent"`, or `"Task"`
  on older CLIs per spike 1A).
- Interrupt needs no extra code: `backend.interrupt()` interrupts the whole
  SDK turn including a running sub-agent (spike 1A h5).
- Usage: if spike 1A h5 records that `ResultMessage.usage` does **not**
  include sub-agent tokens, `SdkEventMapper` sums `usage` of sub-agent
  `AssistantMessage`s since the last `ResultMessage` and adds them to the
  emitted `Usage` (cost stays `total_cost_usd`). If it does include them,
  nothing changes. Record which case applied in the done report.

**Files:**
- Modify: `backend/src/copperchat/agent/events.py`, `backend/src/copperchat/agent/sdk_backend.py`, `backend/src/copperchat/agent/engine.py`
- Modify: `protocol-fixtures/message.json` (one more valid example)
- Test: `backend/tests/unit/agent/test_sdk_backend.py`, `backend/tests/unit/agent/test_engine_subagent.py`

**Interfaces:**
- Produces: `ToolUse.parent_tool_use_id`, `ToolResultEvt.parent_tool_use_id` (default `None`), `SubagentText(parent_tool_use_id: str, text: str)` in the `AgentEvent` union; message metadata key `parentToolUseId` (C7a).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/unit/agent/test_sdk_backend.py`:

```python
from copperchat.agent.events import SubagentText


def test_mapper_maps_subagent_messages_with_parent_id():
    m = SdkEventMapper()
    out = []
    out += m.map(AssistantMessage(content=[ToolUseBlock(id="a1", name="Agent", input={
        "subagent_type": "part-researcher", "description": "TPS62133", "prompt": "q"})],
        model="m", message_id="msg_1"))
    out += m.map(StreamEvent(uuid="u", session_id="s1", parent_tool_use_id="a1",
                             event={"type": "content_block_delta",
                                    "delta": {"type": "text_delta", "text": "partial"}}))
    out += m.map(UserMessage(content="q", parent_tool_use_id="a1"))
    out += m.map(AssistantMessage(content=[TextBlock(text="Searching"),
                                           ToolUseBlock(id="w1", name="WebSearch", input={"query": "TPS62133"})],
                                  model="m", message_id="msg_2", parent_tool_use_id="a1"))
    out += m.map(UserMessage(content=[ToolResultBlock(tool_use_id="w1", content="hits", is_error=False)],
                             parent_tool_use_id="a1"))
    out += m.map(UserMessage(content=[ToolResultBlock(tool_use_id="a1", content="summary", is_error=False)]))
    assert out == [
        ToolUse(id="a1", name="Agent", input={"subagent_type": "part-researcher",
                                               "description": "TPS62133", "prompt": "q"}),
        SubagentText(parent_tool_use_id="a1", text="Searching"),
        ToolUse(id="w1", name="WebSearch", input={"query": "TPS62133"}, parent_tool_use_id="a1"),
        ToolResultEvt(id="w1", content="hits", parent_tool_use_id="a1"),
        ToolResultEvt(id="a1", content="summary"),
    ]
```

`backend/tests/unit/agent/test_engine_subagent.py`:

```python
from copperchat.agent.events import SubagentText, ToolResultEvt, ToolUse
from copperchat.agent.fake import Emit, Text
from copperchat.db.repos.messages import MessageRepo

PID = "a1b2c3d4e5f6"


async def test_subagent_rows_are_complete_and_tagged(make_engine, db, until):
    agent_input = {"subagent_type": "part-researcher", "description": "TPS62133", "prompt": "q"}
    engine, _ = await make_engine(lambda item: [
        Text("Let me research. "),
        Emit(ToolUse(id="a1", name="Agent", input=agent_input)),
        Emit(SubagentText(parent_tool_use_id="a1", text="Searching the web")),
        Emit(ToolUse(id="w1", name="Write", parent_tool_use_id="a1",
                     input={"file_path": "docs/parts/TPS62133/notes.md", "content": "x" * 5000})),
        Emit(ToolResultEvt(id="w1", content="y" * 9000, parent_tool_use_id="a1")),
        Emit(ToolResultEvt(id="a1", content="TPS62133 summary")),
        Text("Done."),
    ])
    await engine.submit_ex("c", "research it", [])
    await until(lambda: not engine.is_busy and engine.current_run_id is None)
    rows, _ = await MessageRepo(db).page(PID, limit=50)
    assistant = [r for r in rows if r.role == "assistant"]
    assert [(r.kind, r.metadata.get("parentToolUseId")) for r in assistant] == [
        ("text", None), ("tool_use", None), ("text", "a1"), ("tool_use", "a1"),
        ("tool_result", "a1"), ("tool_result", None), ("text", None)]
    assert all(r.status == "done" for r in assistant)
    assert assistant[1].metadata["name"] == "Agent" and assistant[1].metadata["input"] == agent_input
    assert len(assistant[3].metadata["input"]["content"]) == 2000
    assert len(assistant[4].content) == 4000
    assert assistant[0].content == "Let me research. " and assistant[6].content == "Done."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/unit/agent/test_sdk_backend.py tests/unit/agent/test_engine_subagent.py -v`
Expected: FAIL with `ImportError: cannot import name 'SubagentText'`.

- [ ] **Step 3: Write minimal implementation**

`events.py`: add `parent_tool_use_id: str | None = None` as the **last**
field of `ToolUse` and `ToolResultEvt`; add

```python
@dataclass(frozen=True)
class SubagentText:
    parent_tool_use_id: str
    text: str
```

and add `SubagentText` to the `AgentEvent` union.

`sdk_backend.py` (`SdkEventMapper.map`): replace the three
`if msg.parent_tool_use_id: return out` / `if msg.parent_tool_use_id or …`
early returns with:

```python
        parent = getattr(msg, "parent_tool_use_id", None)
        if parent:
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        out.append(ToolUse(id=block.id, name=block.name, input=dict(block.input),
                                           parent_tool_use_id=parent))
                    elif isinstance(block, TextBlock) and block.text.strip():
                        out.append(SubagentText(parent_tool_use_id=parent, text=block.text))
                self._add_subagent_usage(msg)   # no-op unless spike 1A h5 requires summing
            elif isinstance(msg, UserMessage) and not isinstance(msg.content, str):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        text, _images = _tool_result_content(block.content)
                        out.append(ToolResultEvt(id=block.tool_use_id, content=text,
                                                 is_error=bool(block.is_error),
                                                 parent_tool_use_id=parent))
            return out   # sub-agent StreamEvents and anything else: ignored
```

placed right after the `SessionId` handling and before the `StreamEvent`
branch. `_add_subagent_usage` accumulates into `self._sub_usage` (reset when a
`ResultMessage` is mapped) only if 1A h5 showed that the result usage excludes
sub-agent tokens; otherwise implement it as a no-op and delete the call.

`engine.py` (`_on_event`, inside the lock), before the existing `ToolUse`
branch:

```python
            if isinstance(ev, SubagentText) or (
                isinstance(ev, ToolUse | ToolResultEvt) and ev.parent_tool_use_id
            ):
                await self._insert_subagent(ev)
                return
```

(add `SubagentText` to the engine's `copperchat.agent.events` import) and the
helpers:

```python
# module level in engine.py
_SUB_INPUT_MAX = 2000
_SUB_RESULT_MAX = 4000


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value[:_SUB_INPUT_MAX]
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value]
    return value


# RunEngine method
    async def _insert_subagent(self, ev: SubagentText | ToolUse | ToolResultEvt) -> None:
        parent = ev.parent_tool_use_id
        if isinstance(ev, SubagentText):
            await self._insert(role="assistant", kind="text", content=ev.text,
                               metadata={"parentToolUseId": parent})
        elif isinstance(ev, ToolUse):
            await self._insert(role="assistant", kind="tool_use", content=ev.name,
                               metadata={"parentToolUseId": parent, "toolUseId": ev.id,
                                         "name": ev.name, "input": _clip(ev.input)})
        else:
            await self._insert(role="assistant", kind="tool_result",
                               content=ev.content[:_SUB_RESULT_MAX],
                               metadata={"parentToolUseId": parent, "toolUseId": ev.id,
                                         "isError": ev.is_error, "images": []})
```

(`_insert` defaults to `status="done"`; it does not touch the open segment.)

`protocol-fixtures/message.json`: add to `valid` a sub-agent row:

```json
{"type": "message", "projectId": "a1b2c3d4e5f6", "message": {"id": 46, "projectId": "a1b2c3d4e5f6", "clientMessageId": null, "runId": "r1", "role": "assistant", "kind": "tool_use", "content": "WebSearch", "status": "done", "metadata": {"parentToolUseId": "a1", "toolUseId": "w1", "name": "WebSearch", "input": {"query": "TPS62133 datasheet"}}, "createdAt": "2026-10-15T12:00:02.000Z"}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/agent -v`
Expected: all passed (including the existing mapper tests, which never set
`parent_tool_use_id`).

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent/events.py backend/src/copperchat/agent/sdk_backend.py backend/src/copperchat/agent/engine.py protocol-fixtures/message.json backend/tests/unit/agent/test_sdk_backend.py backend/tests/unit/agent/test_engine_subagent.py
git commit -m "feat: carry sub-agent messages (parent_tool_use_id) as tagged chat rows" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.12: Messages REST route and WebSocket endpoint

**Files:**
- Create: `backend/src/copperchat/web/routes_messages.py`, `backend/src/copperchat/web/ws.py`
- Modify: `backend/src/copperchat/web/auth.py` (add `authenticate_connection`)
- Test: `backend/tests/unit/web/test_ws.py`, `backend/tests/unit/web/test_routes_messages.py`, `backend/tests/unit/web/conftest.py` (fixture `build_chat_app`)

**Interfaces:**
- Consumes: `RunEngine`, `UnknownAttachment`, protocol models, `get_project`, `agent_dir`, `require_user`, `user_from_cookie`, `COOKIE_NAME`, `app.state.agent_runtime` (`get`, `ensure` — Task 5.13), `app.state.settings`, `app.state.db`.
- Produces: `GET /api/projects/{id}/messages?limit=&before=` → `{messages, hasMore}`; `GET /api/projects/{id}/images/{name}`; WebSocket `/api/ws`; `async def authenticate_connection(conn: HTTPConnection) -> str | None` (async because `user_from_cookie` is async); `copperchat.web.ws.HEARTBEAT_S = 25.0`. Close codes: 4401 unauthenticated, 4403 bad Origin, 4408 slow subscriber dropped (client reconnects with `sinceMessageId`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/web/conftest.py  (append if the file exists; fixture `build_chat_app`)
"""Minimal app with the chat routers and a FakeAgent runtime (Task 5.13 wires the real one)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.engine import RunEngine
from copperchat.agent.fake import FakeAgent, ScriptFor
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.projects import Project, project_id_for
from copperchat.web import routes_messages, ws
from copperchat.web.auth import User, require_user


class StubRuntime:
    def __init__(self, settings: Settings, db: Database, script_for: ScriptFor | None) -> None:
        self.settings, self.db, self.script_for = settings, db, script_for
        self.broadcaster = Broadcaster()
        self.engines: dict[str, RunEngine] = {}

    def get(self, project_id: str) -> RunEngine | None:
        return self.engines.get(project_id)

    async def ensure(self, project: Project) -> RunEngine:
        if project.id not in self.engines:
            engine = RunEngine(
                project=project, settings=self.settings, db=self.db, broadcaster=self.broadcaster,
                sessions=cast(Any, SimpleNamespace(current=lambda: None)),
                backend_factory=lambda ctx: FakeAgent(self.script_for, ctx_factory=ctx))
            await engine.start()
            self.engines[project.id] = engine
        return self.engines[project.id]


def _build_chat_app(tmp_path: Path, script_for: ScriptFor | None = None) -> tuple[FastAPI, str]:
    root = tmp_path / "projects"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "demo.kicad_pro").write_text("")  # discovery marker only
    settings = Settings(_env_file=None, projects_root=root, data_dir=tmp_path / "data",
                        claude_code_oauth_token=SecretStr("t"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = await Database.open(tmp_path / "t.db")
        runtime = StubRuntime(settings, db, script_for)
        app.state.settings, app.state.db, app.state.agent_runtime = settings, db, runtime
        yield
        for engine in runtime.engines.values():
            await engine.close()
        await db.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(routes_messages.router)
    app.include_router(ws.router)
    app.dependency_overrides[require_user] = lambda: User(id=1, username="admin")
    return app, project_id_for("demo/demo.kicad_pro")


@pytest.fixture
def build_chat_app(tmp_path: Path) -> Any:
    return lambda script_for=None: _build_chat_app(tmp_path, script_for)
```

```python
# backend/tests/unit/web/test_ws.py
import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from copperchat.agent.fake import Text, Wait
from copperchat.web import ws as ws_module



@pytest.fixture(autouse=True)
def fake_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(conn: Any) -> str:
        return "admin"

    monkeypatch.setattr(ws_module, "authenticate_connection", ok)


def recv_until(sock, pred, limit: int = 200) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    frames = []
    for _ in range(limit):
        frame = json.loads(sock.receive_text())
        if frame["type"] != "ping":
            frames.append(frame)
        if pred(frame):
            return frames
    raise AssertionError("expected frame not received")


def test_ping_pong_and_invalid_frames(build_chat_app):
    app, pid = build_chat_app()
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        sock.send_text(json.dumps({"type": "ping", "projectId": None}))
        assert json.loads(sock.receive_text()) == {"type": "pong", "projectId": None}
        sock.send_text("not json")
        err = json.loads(sock.receive_text())
        assert err["type"] == "error" and err["projectId"] is None and err["code"] == "invalid_frame"
        sock.send_text(json.dumps({"type": "send", "projectId": pid, "clientMessageId": "c",
                                   "text": "", "attachments": []}))
        err2 = json.loads(sock.receive_text())
        assert err2["projectId"] == pid and err2["code"] == "invalid_frame"
        assert err2["clientMessageId"] == "c"


def test_subscribe_send_streams_and_every_frame_has_project_id(build_chat_app):
    app, pid = build_chat_app()
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        sock.send_text(json.dumps({"type": "subscribe", "projectId": pid, "sinceMessageId": 0}))
        sock.send_text(json.dumps({"type": "send", "projectId": pid, "clientMessageId": "c1",
                                   "text": "hi", "attachments": []}))
        frames = recv_until(sock, lambda f: f["type"] == "run-finished")
        assert all(f["projectId"] == pid for f in frames)
        assert frames[0]["type"] == "message" and frames[0]["message"]["status"] == "queued"
        text = "".join(f.get("contentDelta", "") for f in frames if f["type"] == "message-delta")
        assert text == "You said: hi"
        assert any(f["type"] == "usage" for f in frames)


def test_unknown_attachment_is_an_error_frame(build_chat_app):
    app, pid = build_chat_app()
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        sock.send_text(json.dumps({"type": "send", "projectId": pid, "clientMessageId": "c9",
                                   "text": "x", "attachments": [
                                       {"id": "up-9", "kind": "image", "name": "a.png"}]}))
        err = json.loads(sock.receive_text())
        assert err == {"type": "error", "projectId": pid, "code": "unknown_attachment",
                       "message": "unknown attachment: up-9", "clientMessageId": "c9"}


def test_reconnect_mid_run_gets_half_streamed_row_then_deltas(build_chat_app):
    gate = asyncio.Event()
    app, pid = build_chat_app(lambda item: [Text("Hel"), Wait(gate), Text("lo")])
    with TestClient(app) as client:
        with client.websocket_connect("/api/ws") as first:
            first.send_text(json.dumps({"type": "subscribe", "projectId": pid, "sinceMessageId": 0}))
            first.send_text(json.dumps({"type": "send", "projectId": pid, "clientMessageId": "c1",
                                        "text": "hi", "attachments": []}))
            recv_until(first, lambda f: f.get("contentDelta") == "Hel")
        with client.websocket_connect("/api/ws") as second:
            second.send_text(json.dumps({"type": "subscribe", "projectId": pid, "sinceMessageId": 0}))
            replay = recv_until(second, lambda f: f["type"] == "run-started")
            rows = [f["message"] for f in replay if f["type"] == "message"]
            assistant = rows[-1]
            assert assistant["role"] == "assistant" and assistant["status"] == "streaming"
            assert assistant["content"] == "Hel", "missed deltas arrive as the full row"
            client.portal.call(gate.set)
            live = recv_until(second, lambda f: f["type"] == "run-finished")
            more = "".join(f.get("contentDelta", "") for f in live
                           if f["type"] == "message-delta" and f["messageId"] == assistant["id"])
            assert assistant["content"] + more == "Hello", "no gap, no duplicate"


def test_heartbeat_ping(build_chat_app, monkeypatch):
    monkeypatch.setattr(ws_module, "HEARTBEAT_S", 0.05)
    app, _ = build_chat_app()
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        assert json.loads(sock.receive_text()) == {"type": "ping", "projectId": None}


def test_rejects_unauthenticated_and_foreign_origin(build_chat_app, monkeypatch):
    app, _ = build_chat_app()
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as bad_origin, client.websocket_connect(
                "/api/ws", headers={"origin": "https://evil.example"}) as sock:
            sock.receive_text()
        assert bad_origin.value.code == 4403

        async def nobody(conn: Any) -> None:
            return None

        monkeypatch.setattr(ws_module, "authenticate_connection", nobody)
        with pytest.raises(WebSocketDisconnect) as unauth, client.websocket_connect("/api/ws") as sock:
            sock.receive_text()
        assert unauth.value.code == 4401
```

```python
# backend/tests/unit/web/test_routes_messages.py
import asyncio
import json

from fastapi.testclient import TestClient

from copperchat.agent.fake import Text, Wait
from copperchat.web import ws as ws_module



def test_messages_page_has_more_before_and_live_overlay(build_chat_app, monkeypatch):
    async def ok(conn: object) -> str:
        return "admin"

    monkeypatch.setattr(ws_module, "authenticate_connection", ok)
    gate = asyncio.Event()
    app, pid = build_chat_app(
        lambda item: [Text("Hel"), Wait(gate)] if item.text == "m3" else [Text("ok")])
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        sock.send_text(json.dumps({"type": "subscribe", "projectId": pid, "sinceMessageId": 0}))
        for i in (1, 2, 3):
            sock.send_text(json.dumps({"type": "send", "projectId": pid, "clientMessageId": f"c{i}",
                                       "text": f"m{i}", "attachments": []}))
        while json.loads(sock.receive_text()).get("contentDelta") != "Hel":
            pass
        page = client.get(f"/api/projects/{pid}/messages", params={"limit": 2}).json()
        assert page["hasMore"] is True
        ids = [m["id"] for m in page["messages"]]
        assert ids == sorted(ids) and len(ids) == 2
        assert page["messages"][-1]["content"] == "Hel"  # live overlay of the streaming row
        older = client.get(f"/api/projects/{pid}/messages",
                           params={"limit": 50, "before": ids[0]}).json()
        assert older["hasMore"] is False and all(m["id"] < ids[0] for m in older["messages"])
        client.portal.call(gate.set)
        assert client.get(f"/api/projects/{pid}/messages", params={"limit": 0}).status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/web/test_ws.py tests/unit/web/test_routes_messages.py -v`
Expected: FAIL with `ImportError: cannot import name 'routes_messages' from 'copperchat.web'`.

- [ ] **Step 3: Write minimal implementation**

Append to `backend/src/copperchat/web/auth.py` (add `from starlette.requests import HTTPConnection`):

```python
async def authenticate_connection(conn: HTTPConnection) -> str | None:
    """Cookie auth for WebSocket upgrades (no FastAPI dependency available there)."""
    user = await user_from_cookie(conn.app.state, conn.cookies.get(COOKIE_NAME))
    return user.username if user else None
```

```python
# backend/src/copperchat/web/routes_messages.py
"""Chat history paging and tool image download."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from copperchat.agent.protocol import MessageDTO
from copperchat.db.repos.messages import MessageRepo
from copperchat.projects import agent_dir, get_project
from copperchat.web.auth import User, require_user

router = APIRouter(prefix="/api/projects", tags=["messages"])
_IMAGE_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp|bin)$")


@router.get("/{project_id}/messages")
async def list_messages(
    project_id: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    before: int | None = Query(None, ge=1),
    _: User = Depends(require_user),
) -> dict[str, Any]:
    state = request.app.state
    project = get_project(state.settings.projects_root, project_id)
    rows, has_more = await MessageRepo(state.db).page(project.id, limit=limit, before=before)
    engine = state.agent_runtime.get(project.id)
    dtos = [engine.to_dto(r) if engine else MessageDTO.model_validate(r.to_wire()) for r in rows]
    return {"messages": [d.model_dump(mode="json", by_alias=True) for d in dtos],
            "hasMore": has_more}


@router.get("/{project_id}/images/{name}")
async def get_image(
    project_id: str, name: str, request: Request, _: User = Depends(require_user)
) -> FileResponse:
    if not _IMAGE_NAME.match(name):
        raise HTTPException(status_code=404, detail="no such image")
    project = get_project(request.app.state.settings.projects_root, project_id)
    path = agent_dir(project) / "images" / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such image")
    return FileResponse(path)
```

```python
# backend/src/copperchat/web/ws.py
"""WebSocket /api/ws: cookie auth, schema validation, replay+subscribe, heartbeat."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from copperchat.agent.broadcaster import Subscription
from copperchat.agent.engine import RunEngine, UnknownAttachment
from copperchat.agent.protocol import (
    CancelQueuedFrame, ErrorFrame, InterruptFrame, MessageFrame, PingFrame, PongFrame,
    RateLimitedFrame, RunStartedFrame, SendFrame, ServerPingFrame, SubscribeFrame, Wire,
    client_frame_adapter, dump_frame,
)
from copperchat.errors import CopperChatError
from copperchat.projects import get_project
from copperchat.web import auth

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
HEARTBEAT_S = 25.0
_PID = re.compile(r"^[0-9a-f]{12}$")
authenticate_connection = auth.authenticate_connection  # module attribute so tests can patch


def _origin_ok(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == ws.headers.get("host")


class _Connection:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.runtime: Any = ws.app.state.agent_runtime
        self.settings: Any = ws.app.state.settings
        self._send_lock = asyncio.Lock()
        self._sub: Subscription | None = None
        self._forwarder: asyncio.Task[None] | None = None

    async def send(self, frame: Wire) -> None:
        async with self._send_lock:
            await self.ws.send_text(dump_frame(frame))

    async def engine(self, project_id: str) -> RunEngine:
        engine: RunEngine | None = self.runtime.get(project_id)
        if engine is None:
            engine = await self.runtime.ensure(get_project(self.settings.projects_root, project_id))
        return engine

    def unsubscribe(self) -> None:
        if self._sub is not None:
            self._sub.close()
        if self._forwarder is not None:
            self._forwarder.cancel()
        self._sub = self._forwarder = None

    async def subscribe(self, frame: SubscribeFrame) -> None:
        self.unsubscribe()
        engine = await self.engine(frame.project_id)
        dtos, sub, run_id = await engine.replay_and_subscribe(frame.since_message_id)
        self._sub = sub
        for dto in dtos:
            await self.send(MessageFrame(project_id=frame.project_id, message=dto))
        if run_id is not None:
            await self.send(RunStartedFrame(project_id=frame.project_id, run_id=run_id))
        if engine.rate_limited_until is not None:
            await self.send(RateLimitedFrame(project_id=frame.project_id,
                                             resets_at=engine.rate_limited_until.isoformat()))
        self._forwarder = asyncio.create_task(self._forward(sub))

    async def _forward(self, sub: Subscription) -> None:
        async for frame in sub:
            await self.send(frame)
        if sub.dropped:
            await self.ws.close(code=4408)

    async def handle(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self.send(ErrorFrame(project_id=None, code="invalid_frame",
                                       message="frame is not valid JSON"))
            return
        try:
            frame = client_frame_adapter.validate_python(data)
        except ValidationError as exc:
            pid = data.get("projectId") if isinstance(data, dict) else None
            cmid = data.get("clientMessageId") if isinstance(data, dict) else None
            await self.send(ErrorFrame(
                project_id=pid if isinstance(pid, str) and _PID.match(pid) else None,
                code="invalid_frame", message=str(exc.errors()[0]["msg"]),
                client_message_id=cmid if isinstance(cmid, str) else None))
            return
        if isinstance(frame, PingFrame):
            await self.send(PongFrame(project_id=None))
            return
        try:
            if isinstance(frame, SubscribeFrame):
                await self.subscribe(frame)
            elif isinstance(frame, SendFrame):
                await (await self.engine(frame.project_id)).submit(
                    frame.client_message_id, frame.text, list(frame.attachments))
            elif isinstance(frame, InterruptFrame):
                await (await self.engine(frame.project_id)).interrupt(frame.run_id)
            elif isinstance(frame, CancelQueuedFrame):
                if not await (await self.engine(frame.project_id)).cancel_queued(frame.message_id):
                    await self.send(ErrorFrame(project_id=frame.project_id, code="not_cancellable",
                                               message="message already started or finished"))
        except UnknownAttachment as exc:
            await self.send(ErrorFrame(project_id=frame.project_id, code="unknown_attachment",
                                       message=str(exc),
                                       client_message_id=getattr(frame, "client_message_id", None)))
        except CopperChatError as exc:
            await self.send(ErrorFrame(project_id=frame.project_id,
                                       code=str(getattr(exc, "code", "error")), message=str(exc)))


async def _heartbeat(conn: _Connection) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        await conn.send(ServerPingFrame(project_id=None))


@router.websocket("/ws")
async def chat_ws(ws: WebSocket) -> None:
    if not _origin_ok(ws):
        await ws.close(code=4403)
        return
    if await authenticate_connection(ws) is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    conn = _Connection(ws)
    heartbeat = asyncio.create_task(_heartbeat(conn))
    try:
        while True:
            await conn.handle(await ws.receive_text())
    except WebSocketDisconnect:
        pass
    finally:
        heartbeat.cancel()
        conn.unsubscribe()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
```

Note: `chat_ws` reads `authenticate_connection` and `HEARTBEAT_S` as module
globals at call time, which is what the tests patch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/web/test_ws.py tests/unit/web/test_routes_messages.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/web/auth.py backend/src/copperchat/web/routes_messages.py backend/src/copperchat/web/ws.py backend/tests/unit/web/conftest.py backend/tests/unit/web/test_ws.py backend/tests/unit/web/test_routes_messages.py
git commit -m "feat: chat history route and websocket endpoint with replay, heartbeat and auth" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5.13: `AgentRuntime` and app wiring

**Files:**
- Create: `backend/src/copperchat/agent/runtime.py`
- Modify: `backend/src/copperchat/agent/sdk_backend.py` (add `SdkAgentBackend` alias), `backend/src/copperchat/web/app.py`, `backend/src/copperchat/web/routes_projects.py`
- Test: `backend/tests/unit/agent/test_runtime.py`, `backend/tests/unit/web/test_chat_e2e.py`

**Interfaces:**
- Consumes: `RunEngine`, `RunHooks`, `Broadcaster`, `FakeAgent`, `SdkStreamingBackend` (or `SdkPerTurnBackend` after 1A no-go), `registry.all_specs()`, `check_model_credentials`, `create_app`.
- Produces: `AgentRuntime(*, settings, db, broadcaster, sessions, backend_factory=None, hooks_factory=None)` at `app.state.agent_runtime` with `get`, `ensure`, `close_all`, `backend_factory`, `hooks_factory`; `default_backend_factory(settings, specs) -> BackendFactory`; `copperchat.agent.sdk_backend.SdkAgentBackend` (alias of the backend chosen by spike 1A). `POST /api/projects/{id}/open` now also creates the project's `RunEngine`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/agent/test_runtime.py
import asyncio
from typing import Any

from claude_agent_sdk import ResultMessage

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.engine import RunHooks
from copperchat.agent.fake import FakeAgent
from copperchat.agent.runtime import AgentRuntime, default_backend_factory
from copperchat.agent.sdk_backend import SdkAgentBackend, SdkStreamingBackend
from copperchat.config import Settings

PID = "a1b2c3d4e5f6"


class _Sessions:
    def current(self) -> None:
        return None


def test_backend_selection(settings: Settings):
    fake = settings.model_copy(update={"dev_fake_agent": True})
    assert isinstance(default_backend_factory(fake, [])(lambda: None), FakeAgent)  # type: ignore[arg-type,return-value]
    real = default_backend_factory(settings, [])(lambda: None)  # type: ignore[arg-type,return-value]
    assert isinstance(real, SdkAgentBackend)


async def test_ensure_creates_one_engine_per_project_with_hooks(settings, db, project):
    made: list[Any] = []

    class Hooks(RunHooks):
        pass

    def hooks_factory(p, engine):  # type: ignore[no-untyped-def]
        made.append((p.id, engine))
        return Hooks()

    rt = AgentRuntime(settings=settings, db=db, broadcaster=Broadcaster(), sessions=_Sessions(),  # type: ignore[arg-type]
                      backend_factory=lambda ctx: FakeAgent(ctx_factory=ctx),
                      hooks_factory=hooks_factory)
    a, b = await asyncio.gather(rt.ensure(project), rt.ensure(project))
    assert a is b and rt.get(PID) is a and isinstance(a.hooks, Hooks)
    assert len(made) == 1
    await rt.close_all()
    assert rt.get(PID) is None


async def test_usage_frame_from_sdk_result_message(settings, db, project, until, drain):
    class Client:
        def __init__(self, options: Any) -> None:
            self.out: asyncio.Queue[Any] = asyncio.Queue()

        async def connect(self, prompt: Any) -> None:
            async def pump() -> None:
                async for _ in prompt:
                    await self.out.put(ResultMessage(
                        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                        num_turns=1, session_id="s1", total_cost_usd=0.25,
                        usage={"input_tokens": 10, "output_tokens": 3,
                               "cache_read_input_tokens": 7, "cache_creation_input_tokens": 1}))
                await self.out.put(None)

            self.task = asyncio.create_task(pump())

        async def receive_messages(self):  # type: ignore[no-untyped-def]
            while (m := await self.out.get()) is not None:
                yield m

        async def interrupt(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    rt = AgentRuntime(
        settings=settings, db=db, broadcaster=Broadcaster(), sessions=_Sessions(),  # type: ignore[arg-type]
        backend_factory=lambda ctx: SdkStreamingBackend(settings=settings, specs=[],
                                                        ctx_factory=ctx, client_factory=Client))
    engine = await rt.ensure(project)
    sub = engine.broadcaster.subscribe(PID)
    await engine.submit_ex("c1", "hi", [])
    await until(lambda: not engine.is_busy)
    frames = await drain(sub)
    [usage] = [f for f in frames if f.type == "usage"]
    assert (usage.usage.input_tokens, usage.usage.output_tokens) == (10, 3)
    assert usage.usage.cache_read_tokens == 7 and usage.usage.cost_usd == 0.25
    await rt.close_all()
```

```python
# backend/tests/unit/web/test_chat_e2e.py
import json
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from copperchat.config import Settings
from copperchat.errors import MissingCredentialsError
from copperchat.projects import project_id_for
from copperchat.web.app import create_app
from copperchat.web.auth import COOKIE_NAME

GOOD = {"username": "admin", "password": "test-password"}


def test_startup_requires_credentials_unless_fake_agent(make_settings: Callable[..., Settings]):
    with pytest.raises(MissingCredentialsError), TestClient(
            create_app(make_settings(claude_code_oauth_token=None))):
        pass
    with TestClient(create_app(make_settings(claude_code_oauth_token=None,
                                             dev_fake_agent=True))) as client:
        assert client.app.state.agent_runtime is not None  # type: ignore[attr-defined]


def test_cookie_authenticated_chat_round_trip(make_settings: Callable[..., Settings]):
    settings = make_settings(dev_fake_agent=True)
    (settings.projects_root / "demo").mkdir()
    (settings.projects_root / "demo" / "demo.kicad_pro").write_text("")
    pid = project_id_for("demo/demo.kicad_pro")
    with TestClient(create_app(settings)) as client:
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/api/ws") as s:
            s.receive_text()
        assert exc.value.code == 4401
        assert client.post("/api/auth/login", json=GOOD).status_code == 204
        cookie = {"cookie": f"{COOKIE_NAME}={client.cookies.get(COOKIE_NAME)}"}
        with client.websocket_connect("/api/ws", headers=cookie) as sock:
            sock.send_text(json.dumps({"type": "subscribe", "projectId": pid, "sinceMessageId": 0}))
            sock.send_text(json.dumps({"type": "send", "projectId": pid, "clientMessageId": "c1",
                                       "text": "hello", "attachments": []}))
            seen: list[dict] = []
            while not seen or seen[-1]["type"] != "run-finished":
                frame = json.loads(sock.receive_text())
                if frame["type"] != "ping":
                    seen.append(frame)
        assert any(f["type"] == "usage" for f in seen)
        page = client.get(f"/api/projects/{pid}/messages").json()
        assert [m["role"] for m in page["messages"]] == ["user", "assistant"]
        assert page["messages"][1]["content"] == "You said: hello"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/agent/test_runtime.py tests/unit/web/test_chat_e2e.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'copperchat.agent.runtime'`.

- [ ] **Step 3: Write minimal implementation**

Append to `backend/src/copperchat/agent/sdk_backend.py`:

```python
# The backend chosen by spike 1A. If docs/spikes/1a-sdk-streaming.md records
# no-go, set this to SdkPerTurnBackend (Task 5.4b).
SdkAgentBackend = SdkStreamingBackend
```

```python
# backend/src/copperchat/agent/runtime.py
"""One RunEngine per project, created when the project is opened."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.engine import BackendFactory, RunEngine, RunHooks
from copperchat.agent.events import AgentBackend
from copperchat.agent.fake import FakeAgent
from copperchat.agent.sdk_backend import SdkAgentBackend
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.kicad_session import KicadSessionManager
from copperchat.projects import Project
from copperchat.tools import ToolContext, ToolSpec

HooksFactory = Callable[[Project, RunEngine], RunHooks]


def default_backend_factory(settings: Settings, specs: Sequence[ToolSpec]) -> BackendFactory:
    def make(ctx_factory: Callable[[], ToolContext]) -> AgentBackend:
        if settings.dev_fake_agent:
            return FakeAgent(specs=specs, ctx_factory=ctx_factory)
        return SdkAgentBackend(settings=settings, specs=specs, ctx_factory=ctx_factory)

    return make


def _no_hooks(project: Project, engine: RunEngine) -> RunHooks:
    return RunHooks()  # Phase 7 replaces hooks_factory


class AgentRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        broadcaster: Broadcaster,
        sessions: KicadSessionManager,
        backend_factory: BackendFactory | None = None,
        hooks_factory: HooksFactory | None = None,
    ) -> None:
        from copperchat.tools import registry

        self.settings = settings
        self.db = db
        self.broadcaster = broadcaster
        self.sessions = sessions
        self.backend_factory: BackendFactory = backend_factory or default_backend_factory(
            settings, registry.all_specs())
        self.hooks_factory: HooksFactory = hooks_factory or _no_hooks
        self._engines: dict[str, RunEngine] = {}
        self._lock = asyncio.Lock()

    def get(self, project_id: str) -> RunEngine | None:
        return self._engines.get(project_id)

    async def ensure(self, project: Project) -> RunEngine:
        async with self._lock:
            engine = self._engines.get(project.id)
            if engine is not None:
                return engine
            engine = RunEngine(project=project, settings=self.settings, db=self.db,
                               broadcaster=self.broadcaster, sessions=self.sessions,
                               backend_factory=self.backend_factory)
            engine.hooks = self.hooks_factory(project, engine)
            await engine.start()
            self._engines[project.id] = engine
            return engine

    async def close_all(self) -> None:
        engines, self._engines = list(self._engines.values()), {}
        for engine in engines:
            await engine.close()
```

(`registry` is imported inside `__init__` because the tool providers import
`copperchat.agent` helpers in later phases; a module-level import would cycle.)

`backend/src/copperchat/web/app.py` — in the lifespan, after `sessions` is
created and before `yield` (keep `check_model_credentials(settings)` as the
first lifespan statement; it already raises `MissingCredentialsError` unless
`dev_fake_agent=True`):

```python
from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.runtime import AgentRuntime
from copperchat.web import routes_messages, ws
...
            app.state.agent_runtime = AgentRuntime(
                settings=settings, db=db, broadcaster=Broadcaster(), sessions=sessions)
            yield
        finally:
            if getattr(app.state, "agent_runtime", None) is not None:
                await app.state.agent_runtime.close_all()
            await sessions.close_all()
            await db.close()
...
    app.include_router(routes_messages.router)
    app.include_router(ws.router)
```

(Register both routers before the SPA static-files fallback mount from Phase 3.)

`backend/src/copperchat/web/routes_projects.py` — in `open_project`, right
after `session = await sessions.open(project)`:

```python
    await state.agent_runtime.ensure(project)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest -m "not kicad" && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: all passed, no lint/type errors.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent/runtime.py backend/src/copperchat/agent/sdk_backend.py backend/src/copperchat/web/app.py backend/src/copperchat/web/routes_projects.py backend/tests/unit/agent/test_runtime.py backend/tests/unit/web/test_chat_e2e.py
git commit -m "feat: per-project agent runtime wired into the app" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Phase self-review

**Spec coverage:**
- §5 (agent runtime): queue-first persistence (5.2, 5.9), gated push stream (5.6, 5.9), one SDK session per burst with resume/fork only at burst start (5.4, 5.4b, 5.11), interrupt with backstop (5.10), cancel-queued race (5.10), hooks (5.9, 5.13), SDK adapter restrictions — `tools=[]`, only `mcp__copperchat__*`, `can_use_tool` guard, own system prompt, token via `env` (5.4, 5.5).
- §5.1 / §5.2 (protocol, client expectations): all C7 frames with fixtures (5.3), `projectId` on every frame, replay with live overlay, `run-started`/`rate-limited` re-send, heartbeat, reconnect-mid-run without gaps (5.12), paging with `hasMore` (5.2, 5.12).
- §10 (rate limit, usage): `rate_limit_state`, parking, auto-resume after restart, `rate-limited` and `usage` frames, `runs.usage` (5.1, 5.11, 5.13).
- §11 (errors): KiCad failure mid-turn ends the turn with an error row while the queue continues (5.5, 5.11); structured tool errors (5.5); invalid frames → `error` with `projectId: null` when unknown (5.12).
- Review Focus 1: Task 5.2 (repo) and Task 5.9 (`test_retransmitted_client_message_id_is_one_turn`).
- §8 (part research as SDK sub-agent, owner decision 2026-10-16): sub-agent messages with `parent_tool_use_id` become tagged complete rows; interrupt and usage cover them (5.11b). The `Agent` tool itself is enabled in Phase 8.
- KiCad failure mid-turn: the engine posts the **only** "KiCad stopped responding" error row (`metadata.code = "kicad_unavailable"`); Phase 7's recovery hook does not post a second one for `kicad_failed` turns.

**Deviations / decisions to confirm:**
- `authenticate_connection` is `async` (it must await `user_from_cookie`); the earlier cross-phase list wrote it without `async`.
- `ToolContext.on_documents_changed` / `on_kicad_unavailable` are synchronous callables (Task 5.5); the earlier cross-phase list typed them as returning `Awaitable[None]`. The engine schedules `view-updated` publishing itself.
- `SdkAgentBackend` is a module alias (`= SdkStreamingBackend`, or `SdkPerTurnBackend` on 1A no-go). Phase 8 assumes `SdkAgentBackend._build_options` / `_user_message`; those do not exist — Phase 8 should use the module functions `build_options(...)` and `user_prompt(item)` from `sdk_backend.py`.
- Tool images are stored under `agent_dir(project)/images/` and served by `GET /api/projects/{id}/images/{name}` (new route, not in C8). Image rows (`kind="image"`, `metadata.url`) and `tool_result.metadata.images[].url` both reference them.
- `view-updated.version` is a millisecond timestamp (matches the fixture), not `KicadSession.view_version`.
- WebSocket close codes 4401/4403/4408 and `error.code` values `invalid_frame`, `unknown_attachment`, `not_cancellable` are new names for Phase 6A.
- Subscribing to a project that is not open creates its `RunEngine` (history and queue work without KiCad; tools fail with `KicadUnavailable` until `POST /open`).
