# Phase 7: History, Git and Recovery

> **Cross-phase corrections apply:** see index § "Cross-phase corrections" (X2, X4); they supersede conflicting code in this file.

**Goal:** Per-turn snapshots with undo, autosave + view refresh at turn end, external-change detection before every turn, crash recovery with a restore offer, read-only git tools for the agent, and the History/Git panels in the frontend.

**Depends on:** Phase 4 (`write_kicanvas_snapshot`, `render_view`, fixture projects), Phase 5 (`RunHooks`, `TurnInfo`, `TurnOutcome`, `AgentRuntime.hooks_factory`, `RunEngine.post_notice/post_error/add_agent_note`, `Broadcaster`, `ViewUpdatedFrame`, `ToolContext.user_message`). Frontend tasks also need Phase 6A merged (`registerPanel`, `useChat`, `@/api/http`).

**Parallel with:** 6A (backend tasks 7.1–7.7), 6B, 8.

**Read first:** index `docs/superpowers/plans/2026-10-15-foundation-plan.md` (Global Constraints, Review Focus 2–5, C2–C8); spec `docs/superpowers/specs/2026-10-15-foundation-design.md` §§ on save/undo/history, git, KiCad server lifecycle, external changes; `docs/spikes/1c-api-server.md` (SaveDocument, SaveCopyOfDocument, sub-sheet behaviour, crash/restart); Phase 5 file "Interfaces"; Phase 6A Task 6A.6 and the panels task.

**PR title:** `foundation phase 7: history, autosave, recovery and git tools`

**Files:**
- Create: `backend/src/copperchat/db/migrations/0003_history.sql`
- Create: `backend/src/copperchat/history/__init__.py`, `history/store.py`, `history/restore.py`, `history/hooks.py`, `history/service.py`
- Create: `backend/src/copperchat/git_tools/__init__.py`, `git_tools/specs.py`
- Create: `backend/src/copperchat/web/routes_history.py`, `backend/src/copperchat/web/routes_git.py`
- Modify: `backend/src/copperchat/web/app.py` (install `HistoryService`, include routers), `backend/src/copperchat/tools/registry.py` (`_PROVIDERS` += `"copperchat.git_tools.specs"`)
- Create tests: `backend/tests/unit/test_history_store.py`, `test_history_restore.py`, `test_history_hooks.py`, `test_external_change.py`, `test_crash_recovery.py`, `test_routes_history.py`, `test_git_tools.py`, `backend/tests/support/history.py`
- Create: `frontend/src/panels/HistoryPanel.tsx`, `HistoryPanel.test.tsx`, `GitPanel.tsx`, `GitPanel.test.tsx`; Modify: `frontend/src/panels/index.ts`

**Acceptance criteria:**
- The first mutating tool call of a turn creates exactly one snapshot under `<project>/.kicad-agent/history/<snapshotId>/` with `manifest.json`; files are byte-identical copies of what KiCad last saved; at most 50 snapshots are kept.
- Snapshot ids other than 32 lowercase hex chars (incl. `../x`, absolute paths) and unknown ids are rejected (400 / 404); nothing outside the history root is read or written (Review Focus 5).
- Restore closes the api-server session, copies KiCad's own saved files back byte-for-byte, reopens the session and publishes `view-updated`; read-only (locked) projects never autosave and never restore (Review Focus 3).
- A turn that changed documents ends with `session.save(doc)` for each changed doc, a fresh KiCanvas snapshot, `view_version += 1`, one `view-updated{docs, version}` frame and updated `document_hashes`.
- Before every turn, a hash mismatch against `document_hashes` reloads the session and posts a chat notice (Review Focus 4).
- `KicadUnavailable` during a turn ends it with an error row, the server is running again for the next turn, and a notice with `metadata={"action":"restore","snapshotId":…}` is posted when the run has a snapshot (Review Focus 2).
- `git_status`, `git_diff_summary`, `git_commit` work on temp repos, refuse outside a repo, are limited to the project dir minus `.kicad-agent/{history,view,renders,uploads}`, never push; `git_commit` refuses unless the user message contains the word "commit".
- History and Git panels are registered, list data, confirm before undo, and send the commit chat message.

**Verification commands:**
- `cd backend && uv sync && uv run pytest -m "not kicad" && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
- `cd frontend && npm ci && npm run typecheck && npm run lint && npm test -- --run && npm run build`

**CI expectations:** existing `backend` and `frontend` jobs green; no new KiCad-dependent tests (all Phase 7 tests use the Phase 3 fake launcher and KiCad-generated fixture files copied byte-for-byte), so the `kicad` job is unchanged.

---

## Interfaces

**Consumes:** C1 `Settings`; C2 `Database`; C3 `Project`, `agent_dir`, `lock_status`, `get_project`; C4 `KicadSession` (`save`, `close`, `read_only`, `view_version`), `KicadSessionManager` (`open`, `current`), `KicadUnavailable`, `DocKind`; C5 `ToolSpec`, `ToolContext` (incl. Phase 5 `user_message`), `ToolResult`, `StructuredToolError`, `registry._PROVIDERS`; C6 `write_kicanvas_snapshot`, `render_view`; Phase 5 `RunHooks`, `TurnInfo`, `TurnOutcome`, `RunEngine`, `AgentRuntime` (`hooks_factory`, `get`, `broadcaster`, `sessions`, `db`), `ViewUpdatedFrame`; Phase 3 `require_user`, `ReadOnlyProject`, `tests.support.fake_kicad`; Phase 6A `registerPanel`, `useChat`, `apiGet`, `apiPost`, `apiUrl`.

**Produces (NEW cross-phase names):**
```python
# copperchat.history.store
HISTORY_KEEP = 50
GITIGNORE_LINES = ("history/", "view/", "renders/", "uploads/")
class InvalidSnapshotId(CopperChatError)          # → HTTP 400
class SnapshotNotFound(CopperChatError)           # → HTTP 404
@dataclass(frozen=True) class Snapshot: id, project_id, run_id, created_at, label, files: list[str], has_preview: bool
def validate_snapshot_id(raw: str) -> str
def document_files(project: Project) -> list[Path]      # .kicad_pro, all .kicad_sch, .kicad_pcb
def compute_hashes(project: Project) -> dict[str, str]  # rel posix path -> sha256
def ensure_gitignore(project: Project) -> None
class HistoryStore:
    def __init__(self, db: Database, project: Project)
    async def snapshot(self, run_id: str, label: str, preview_png: bytes | None = None) -> Snapshot
    async def list(self) -> list[Snapshot]                       # newest first
    async def get(self, snapshot_id: str) -> Snapshot             # validates, raises SnapshotNotFound
    def snapshot_dir(self, snapshot_id: str) -> Path              # validated, inside history root
    async def latest_for_run(self, run_id: str) -> Snapshot | None
class DocumentHashRepo:
    def __init__(self, db: Database)
    async def get(self, project_id: str) -> dict[str, str]
    async def replace(self, project_id: str, hashes: dict[str, str]) -> None
# copperchat.history.restore
class AgentBusy(CopperChatError)                  # → HTTP 409
async def restore_snapshot(svc: HistoryService, project: Project, snapshot_id: str) -> Snapshot
# copperchat.history.hooks
class HistoryHooks(RunHooks): __init__(self, svc: HistoryService, project: Project, engine: RunEngine)
# copperchat.history.service
class HistoryService:   # app.state.history
    db, sessions, broadcaster, runtime
    write_view: Callable[[KicadSession, DocKind], Awaitable[Path]]      # default write_kicanvas_snapshot
    render_preview: Callable[[KicadSession, DocKind], Awaitable[bytes | None]]
    def store(self, project) -> HistoryStore; hashes: DocumentHashRepo
    async def publish_view(self, project, session, docs: set[DocKind]) -> int
    def hooks_factory(self, project, engine) -> HistoryHooks
def install_history(app: FastAPI) -> HistoryService
# copperchat.git_tools.specs
COMMIT_INTENT: re.Pattern[str]    # r"\bcommit\b", IGNORECASE
EXCLUDED_DIRS = (".kicad-agent/history", ".kicad-agent/view", ".kicad-agent/renders", ".kicad-agent/uploads")
async def git_status_data(project_dir: Path) -> dict[str, Any]
TOOL_SPECS: list[ToolSpec]        # git_status, git_diff_summary, git_commit
```
HTTP (C8 plus one NEW route): `GET /api/projects/{id}/history` → `[{id, runId, createdAt, label, files, previewUrl}]`; `POST /api/projects/{id}/history/{snapshotId}/restore` → `{id, files}`; NEW `GET /api/projects/{id}/history/{snapshotId}/preview` → PNG; `GET /api/projects/{id}/git/status` → `{isRepo, branch, clean, files:[{path,status}]}`.

Notice metadata keys (NEW): `{"action":"restore","snapshotId":str}`, `{"action":"external-change","files":[str]}`, `{"action":"restored","snapshotId":str}`.

---

### Task 7.1: History store, migration, snapshot-id validation (Review Focus 5)

**Files:**
- Create: `backend/src/copperchat/db/migrations/0003_history.sql`, `backend/src/copperchat/history/__init__.py` (empty), `backend/src/copperchat/history/store.py`, `backend/tests/support/history.py`
- Test: `backend/tests/unit/test_history_store.py`

**Interfaces:** Consumes C2, C3. Produces `copperchat.history.store` as listed above.

- [ ] **Step 1: Write the failing test**

`backend/tests/support/history.py` (shared helper; copies KiCad-generated fixture files byte-for-byte, never writes KiCad content):
```python
import shutil
from pathlib import Path

from copperchat.projects import Project, discover_projects

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "projects"
# Phase 4 fixture copied from KiCad demos. If Phase 4 named the folder differently, use that path.
ECC83 = FIXTURES / "ecc83"
HIERARCHY = FIXTURES / "complex_hierarchy"


def copy_fixture(src: Path, root: Path) -> Project:
    shutil.copytree(src, root / src.name)
    return discover_projects(root)[0]


def replace_with_kicad_file(target: Path, source: Path) -> None:
    """Simulate an external save: swap in another file that KiCad itself wrote."""
    tmp = target.with_suffix(target.suffix + ".tmp")
    shutil.copyfile(source, tmp)
    tmp.replace(target)
```

`backend/tests/unit/test_history_store.py`:
```python
import json
from pathlib import Path

import pytest

from copperchat.db import Database
from copperchat.history.store import (
    HISTORY_KEEP, DocumentHashRepo, HistoryStore, InvalidSnapshotId, SnapshotNotFound,
    compute_hashes, document_files, ensure_gitignore, validate_snapshot_id,
)
from copperchat.projects import agent_dir
from tests.support.history import ECC83, HIERARCHY, copy_fixture


@pytest.fixture
async def db(tmp_path: Path):
    d = await Database.open(tmp_path / "db.sqlite")
    yield d
    await d.close()


async def test_snapshot_copies_documents_byte_for_byte(db: Database, tmp_path: Path) -> None:
    project = copy_fixture(ECC83, tmp_path / "projects")
    store = HistoryStore(db, project)
    snap = await store.snapshot("run-1", "add decoupling", preview_png=b"\x89PNG fake")
    sdir = store.snapshot_dir(snap.id)
    assert sdir.parent == agent_dir(project) / "history"
    for src in document_files(project):
        rel = src.relative_to(project.dir)
        assert (sdir / rel).read_bytes() == src.read_bytes()
    manifest = json.loads((sdir / "manifest.json").read_text())
    assert manifest["id"] == snap.id and manifest["runId"] == "run-1"
    assert {f["relPath"] for f in manifest["files"]} == set(snap.files)
    assert (sdir / "preview.png").exists() and snap.has_preview
    assert [s.id for s in await store.list()] == [snap.id]
    assert (await store.latest_for_run("run-1")).id == snap.id  # type: ignore[union-attr]


async def test_sub_sheets_are_included(db: Database, tmp_path: Path) -> None:
    project = copy_fixture(HIERARCHY, tmp_path / "projects")
    files = {p.name for p in document_files(project)}
    assert len([f for f in files if f.endswith(".kicad_sch")]) > 1
    snap = await HistoryStore(db, project).snapshot("r", "x")
    assert len([f for f in snap.files if f.endswith(".kicad_sch")]) > 1


async def test_retention_keeps_last_50(db: Database, tmp_path: Path) -> None:
    project = copy_fixture(ECC83, tmp_path / "projects")
    store = HistoryStore(db, project)
    ids = [(await store.snapshot(f"run-{i}", str(i))).id for i in range(HISTORY_KEEP + 3)]
    listed = [s.id for s in await store.list()]
    assert len(listed) == HISTORY_KEEP
    assert listed[0] == ids[-1] and ids[0] not in listed
    assert not (agent_dir(project) / "history" / ids[0]).exists()


@pytest.mark.parametrize("bad", ["../etc", "..", "/etc/passwd", "a" * 31, "A" * 32,
                                 "0" * 31 + "/", "", "x" * 32, "0" * 32 + "\n"])
def test_invalid_snapshot_ids_rejected(bad: str) -> None:
    with pytest.raises(InvalidSnapshotId):
        validate_snapshot_id(bad)


async def test_unknown_and_traversal_ids_rejected_by_store(db: Database, tmp_path: Path) -> None:
    project = copy_fixture(ECC83, tmp_path / "projects")
    store = HistoryStore(db, project)
    with pytest.raises(SnapshotNotFound):
        await store.get("0" * 32)
    for bad in ("../../x", str(tmp_path)):
        with pytest.raises(InvalidSnapshotId):
            await store.get(bad)
        with pytest.raises(InvalidSnapshotId):
            store.snapshot_dir(bad)


async def test_gitignore_and_hash_repo(db: Database, tmp_path: Path) -> None:
    project = copy_fixture(ECC83, tmp_path / "projects")
    ensure_gitignore(project)
    text = (agent_dir(project) / ".gitignore").read_text()
    assert text.splitlines() == ["history/", "view/", "renders/", "uploads/"]
    hashes = compute_hashes(project)
    assert all(len(h) == 64 for h in hashes.values())
    repo = DocumentHashRepo(db)
    assert await repo.get(project.id) == {}
    await repo.replace(project.id, hashes)
    assert await repo.get(project.id) == hashes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_history_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'copperchat.history'`.

- [ ] **Step 3: Write minimal implementation**

`backend/src/copperchat/db/migrations/0003_history.sql`:
```sql
CREATE TABLE IF NOT EXISTS history_snapshots (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    label       TEXT NOT NULL,
    files       TEXT NOT NULL            -- JSON array of rel posix paths
);
CREATE INDEX IF NOT EXISTS ix_history_project ON history_snapshots(project_id, created_at);
CREATE TABLE IF NOT EXISTS document_hashes (
    project_id  TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    PRIMARY KEY (project_id, rel_path)
);
```

`backend/src/copperchat/history/store.py`:
```python
"""Per-turn snapshots of KiCad's own saved files.

Snapshots are byte-for-byte copies of files KiCad wrote (SaveDocument). CopperChat
never parses or edits their content; it only copies bytes and hashes them.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from copperchat.db import Database
from copperchat.db.repos.messages import utcnow
from copperchat.errors import CopperChatError
from copperchat.projects import Project, agent_dir

HISTORY_KEEP = 50
GITIGNORE_LINES = ("history/", "view/", "renders/", "uploads/")
_ID_RE = re.compile(r"[0-9a-f]{32}")


class InvalidSnapshotId(CopperChatError):
    pass


class SnapshotNotFound(CopperChatError):
    pass


@dataclass(frozen=True)
class Snapshot:
    id: str
    project_id: str
    run_id: str
    created_at: str
    label: str
    files: list[str]
    has_preview: bool


def validate_snapshot_id(raw: str) -> str:
    if not isinstance(raw, str) or _ID_RE.fullmatch(raw) is None:
        raise InvalidSnapshotId(f"invalid snapshot id: {raw!r}")
    return raw


def _is_hidden_or_agent(rel: Path) -> bool:
    return any(part.startswith(".") for part in rel.parts[:-1])


def document_files(project: Project) -> list[Path]:
    """The project file, every schematic sheet below the project dir, and the board."""
    files: list[Path] = [project.pro_file]
    for sch in sorted(project.dir.rglob("*.kicad_sch")):
        if not _is_hidden_or_agent(sch.relative_to(project.dir)):
            files.append(sch)
    if project.pcb_file is not None and project.pcb_file.exists():
        files.append(project.pcb_file)
    return files


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_hashes(project: Project) -> dict[str, str]:
    return {p.relative_to(project.dir).as_posix(): _sha256(p) for p in document_files(project)}


def ensure_gitignore(project: Project) -> None:
    """CopperChat's own directory; keeps its caches out of the user's git repo."""
    path = agent_dir(project) / ".gitignore"
    content = "\n".join(GITIGNORE_LINES) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != content:
        path.write_text(content)


class HistoryStore:
    def __init__(self, db: Database, project: Project) -> None:
        self._db = db
        self.project = project
        self.root = agent_dir(project) / "history"

    def snapshot_dir(self, snapshot_id: str) -> Path:
        sid = validate_snapshot_id(snapshot_id)
        path = (self.root / sid).resolve()
        if path.parent != self.root.resolve():
            raise InvalidSnapshotId(f"invalid snapshot id: {snapshot_id!r}")
        return path

    async def snapshot(self, run_id: str, label: str, preview_png: bytes | None = None) -> Snapshot:
        ensure_gitignore(self.project)
        sid = uuid.uuid4().hex
        dest = self.snapshot_dir(sid)
        tmp = self.root / f".tmp-{sid}"
        entries = []
        for src in document_files(self.project):
            rel = src.relative_to(self.project.dir)
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            entries.append({"relPath": rel.as_posix(), "sha256": _sha256(target),
                            "size": target.stat().st_size})
        created = utcnow()
        if preview_png is not None:
            (tmp / "preview.png").write_bytes(preview_png)
        manifest = {"id": sid, "projectId": self.project.id, "runId": run_id,
                    "createdAt": created, "label": label[:200], "files": entries}
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
        tmp.rename(dest)
        files = [e["relPath"] for e in entries]
        await self._db.execute(
            "INSERT INTO history_snapshots(id, project_id, run_id, created_at, label, files)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (sid, self.project.id, run_id, created, label[:200], json.dumps(files)))
        await self._prune()
        return Snapshot(sid, self.project.id, run_id, created, label[:200], files,
                        preview_png is not None)

    def _row(self, row) -> Snapshot:  # type: ignore[no-untyped-def]
        sid = row["id"]
        return Snapshot(sid, row["project_id"], row["run_id"], row["created_at"], row["label"],
                        json.loads(row["files"]), (self.root / sid / "preview.png").exists())

    async def list(self) -> list[Snapshot]:
        rows = await self._db.fetchall(
            "SELECT * FROM history_snapshots WHERE project_id = ?"
            " ORDER BY created_at DESC, rowid DESC", (self.project.id,))
        return [self._row(r) for r in rows]

    async def get(self, snapshot_id: str) -> Snapshot:
        sid = validate_snapshot_id(snapshot_id)
        row = await self._db.fetchone(
            "SELECT * FROM history_snapshots WHERE id = ? AND project_id = ?", (sid, self.project.id))
        if row is None or not (self.snapshot_dir(sid) / "manifest.json").exists():
            raise SnapshotNotFound(f"snapshot {sid} not found")
        return self._row(row)

    async def latest_for_run(self, run_id: str) -> Snapshot | None:
        row = await self._db.fetchone(
            "SELECT * FROM history_snapshots WHERE project_id = ? AND run_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1", (self.project.id, run_id))
        return None if row is None else self._row(row)

    async def _prune(self) -> None:
        rows = await self._db.fetchall(
            "SELECT id FROM history_snapshots WHERE project_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (self.project.id, HISTORY_KEEP))
        for row in rows:
            shutil.rmtree(self.snapshot_dir(row["id"]), ignore_errors=True)
            await self._db.execute("DELETE FROM history_snapshots WHERE id = ?", (row["id"],))


class DocumentHashRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, project_id: str) -> dict[str, str]:
        rows = await self._db.fetchall(
            "SELECT rel_path, sha256 FROM document_hashes WHERE project_id = ?", (project_id,))
        return {r["rel_path"]: r["sha256"] for r in rows}

    async def replace(self, project_id: str, hashes: dict[str, str]) -> None:
        async with self._db.transaction():
            await self._db.execute("DELETE FROM document_hashes WHERE project_id = ?", (project_id,))
            for rel, digest in sorted(hashes.items()):
                await self._db.execute(
                    "INSERT INTO document_hashes(project_id, rel_path, sha256) VALUES (?, ?, ?)",
                    (project_id, rel, digest))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_history_store.py -q && uv run mypy src`
Expected: PASS (all tests), mypy clean.

- [ ] **Step 5: Commit**
```bash
git add backend/src/copperchat/db/migrations/0003_history.sql backend/src/copperchat/history backend/tests/support/history.py backend/tests/unit/test_history_store.py
git commit -m "feat: history snapshot store with retention and id validation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7.2: HistoryService and restore (Review Focus 3)

**Files:**
- Create: `backend/src/copperchat/history/service.py`, `backend/src/copperchat/history/restore.py`
- Test: `backend/tests/unit/test_history_restore.py`

**Interfaces:** Consumes `KicadSessionManager.open/current`, `KicadSession.close/read_only/view_version`, `lock_status`, `write_kicanvas_snapshot`, `render_view`, `ViewUpdatedFrame`, `Broadcaster.publish`, `AgentRuntime.get`, `RunEngine.is_busy/post_notice/add_agent_note`, `ReadOnlyProject`. Produces `HistoryService`, `restore_snapshot`, `AgentBusy`.

Restore semantics (state explicitly in the module docstring): restore copies back files that **KiCad itself wrote** (via SaveDocument) and that were copied byte-for-byte into the snapshot. CopperChat does not generate, parse or modify KiCad file content — this is file-level version restore, not hand-editing. The api-server session is closed first so KiCad never holds a stale in-memory document, then reopened on the restored files.

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/test_history_restore.py`:
```python
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.errors import ReadOnlyProject
from copperchat.history.restore import AgentBusy, restore_snapshot
from copperchat.history.service import HistoryService
from copperchat.history.store import compute_hashes
from copperchat.kicad_session import KicadSessionManager
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer
from tests.support.history import ECC83, HIERARCHY, copy_fixture, replace_with_kicad_file


class RecordingBroadcaster:
    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def publish(self, project_id: str, frame: Any) -> None:
        self.frames.append(frame)


class FakeEngine:
    is_busy = False

    def __init__(self) -> None:
        self.notices: list[tuple[str, dict | None]] = []
        self.errors: list[tuple[str, dict | None]] = []
        self.notes: list[str] = []

    async def post_notice(self, text: str, metadata: dict | None = None) -> None:
        self.notices.append((text, metadata))

    async def post_error(self, text: str, metadata: dict | None = None) -> None:
        self.errors.append((text, metadata))

    def add_agent_note(self, text: str) -> None:
        self.notes.append(text)


class FakeRuntime:
    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine

    def get(self, project_id: str) -> FakeEngine:
        return self.engine


@pytest.fixture
async def env(tmp_path: Path, make_settings: Callable[..., Settings]):
    server = FakeServer()
    settings = make_settings()
    sessions = KicadSessionManager(settings, FakeLauncher(server),
                                   client_factory=FakeClientFactory(server))
    db = await Database.open(tmp_path / "db.sqlite")
    engine = FakeEngine()
    svc = HistoryService(db=db, sessions=sessions, broadcaster=RecordingBroadcaster(),
                         runtime=FakeRuntime(engine))
    written: list[str] = []

    async def write_view(session: Any, doc: str) -> Path:
        written.append(doc)
        return tmp_path / doc

    svc.write_view = write_view  # type: ignore[assignment]
    yield svc, sessions, server, engine, written, settings
    await sessions.close_all()
    await db.close()
    server.release.set()


async def test_restore_brings_back_kicad_saved_bytes_and_reopens(env) -> None:  # type: ignore[no-untyped-def]
    svc, sessions, server, engine, written, settings = env
    project = copy_fixture(ECC83, settings.projects_root)
    before = compute_hashes(project)
    session = await sessions.open(project)
    session.view_version = 4
    snap = await svc.store(project).snapshot("run-1", "turn")
    replace_with_kicad_file(project.pcb_file, next(HIERARCHY.glob("*.kicad_pcb")))
    assert compute_hashes(project) != before

    restored = await restore_snapshot(svc, project, snap.id)

    assert restored.id == snap.id
    assert compute_hashes(project) == before
    assert await svc.hashes.get(project.id) == before
    assert server.starts == 2                          # closed + reopened
    new = sessions.current()
    assert new is not None and new is not session and new.view_version == 5
    frame = svc.broadcaster.frames[-1]
    assert frame.type == "view-updated" and frame.version == 5
    assert sorted(frame.docs) == ["pcb", "sch"] and sorted(written) == ["pcb", "sch"]
    assert engine.notices[-1][1] == {"action": "restored", "snapshotId": snap.id}
    assert engine.notes and snap.id in engine.notes[-1]


async def test_restore_refused_for_locked_project(env) -> None:  # type: ignore[no-untyped-def]
    svc, sessions, server, engine, written, settings = env
    project = copy_fixture(ECC83, settings.projects_root)
    snap = await svc.store(project).snapshot("run-1", "turn")
    (project.dir / f"~{project.pcb_file.name}.lck").touch()   # KiCad GUI lock marker
    pcb_before = project.pcb_file.read_bytes()
    replace_with_kicad_file(project.pcb_file, next(HIERARCHY.glob("*.kicad_pcb")))
    changed = project.pcb_file.read_bytes()
    with pytest.raises(ReadOnlyProject):
        await restore_snapshot(svc, project, snap.id)
    assert project.pcb_file.read_bytes() == changed != pcb_before
    assert svc.broadcaster.frames == [] and server.starts == 0


async def test_restore_refused_while_agent_busy(env) -> None:  # type: ignore[no-untyped-def]
    svc, sessions, server, engine, written, settings = env
    project = copy_fixture(ECC83, settings.projects_root)
    snap = await svc.store(project).snapshot("run-1", "turn")
    engine.is_busy = True
    with pytest.raises(AgentBusy):
        await restore_snapshot(svc, project, snap.id)


async def test_restore_rejects_tampered_manifest(env) -> None:  # type: ignore[no-untyped-def]
    svc, sessions, server, engine, written, settings = env
    project = copy_fixture(ECC83, settings.projects_root)
    store = svc.store(project)
    snap = await store.snapshot("run-1", "turn")
    manifest = store.snapshot_dir(snap.id) / "manifest.json"
    manifest.write_text(manifest.read_text().replace(snap.files[0], "../../escape"))
    with pytest.raises(ValueError):
        await restore_snapshot(svc, project, snap.id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_history_restore.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'copperchat.history.restore'`.

- [ ] **Step 3: Write minimal implementation**

`backend/src/copperchat/history/service.py`:
```python
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from copperchat.agent.protocol import ViewUpdatedFrame
from copperchat.db import Database
from copperchat.history.store import DocumentHashRepo, HistoryStore
from copperchat.kicad_session import DocKind, KicadSession, KicadSessionManager
from copperchat.projects import Project
from copperchat.views import render_view, write_kicanvas_snapshot

if TYPE_CHECKING:
    from fastapi import FastAPI

    from copperchat.agent.engine import RunEngine
    from copperchat.history.hooks import HistoryHooks

log = logging.getLogger(__name__)


async def _default_preview(session: KicadSession, doc: DocKind) -> bytes | None:
    try:
        return (await render_view(session, doc, "agent")).data
    except Exception:  # a missing thumbnail must never block a snapshot
        log.warning("history preview render failed", exc_info=True)
        return None


class HistoryService:
    def __init__(self, *, db: Database, sessions: KicadSessionManager, broadcaster: Any,
                 runtime: Any) -> None:
        self.db = db
        self.sessions = sessions
        self.broadcaster = broadcaster
        self.runtime = runtime
        self.hashes = DocumentHashRepo(db)
        self.write_view: Callable[[KicadSession, DocKind], Awaitable[Path]] = write_kicanvas_snapshot
        self.render_preview: Callable[[KicadSession, DocKind], Awaitable[bytes | None]] = _default_preview

    def store(self, project: Project) -> HistoryStore:
        return HistoryStore(self.db, project)

    @staticmethod
    def docs_of(project: Project) -> set[DocKind]:
        docs: set[DocKind] = set()
        if project.sch_file is not None:
            docs.add("sch")
        if project.pcb_file is not None:
            docs.add("pcb")
        return docs

    async def session_for(self, project: Project) -> KicadSession:
        current = self.sessions.current()
        if current is not None and current.project.id == project.id:
            return current
        return await self.sessions.open(project)

    async def publish_view(self, project: Project, session: KicadSession,
                           docs: set[DocKind]) -> int:
        for doc in sorted(docs):
            await self.write_view(session, doc)
        session.view_version += 1
        await self.broadcaster.publish(project.id, ViewUpdatedFrame(
            project_id=project.id, docs=sorted(docs), version=session.view_version))
        return session.view_version

    def hooks_factory(self, project: Project, engine: RunEngine) -> HistoryHooks:
        from copperchat.history.hooks import HistoryHooks

        return HistoryHooks(self, project, engine)


def install_history(app: FastAPI) -> HistoryService:
    runtime = app.state.agent_runtime
    svc = HistoryService(db=app.state.db, sessions=app.state.sessions,
                         broadcaster=runtime.broadcaster, runtime=runtime)
    runtime.hooks_factory = svc.hooks_factory
    app.state.history = svc
    return svc
```

`backend/src/copperchat/history/restore.py`:
```python
"""Undo = restore a snapshot.

This copies back files that KiCad itself wrote (SaveDocument) and that were
copied byte-for-byte into the snapshot. CopperChat never generates, parses or
modifies KiCad file content: this is a file-level version restore, not
hand-editing. The api-server session is closed first and reopened afterwards so
KiCad loads the restored files itself.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from copperchat.errors import CopperChatError, ReadOnlyProject
from copperchat.history.service import HistoryService
from copperchat.history.store import Snapshot, compute_hashes
from copperchat.projects import Project, lock_status


class AgentBusy(CopperChatError):
    pass


def _inside(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    if not target.is_relative_to(base.resolve()) or Path(rel).is_absolute():
        raise ValueError(f"snapshot manifest path escapes its root: {rel!r}")
    return target


async def restore_snapshot(svc: HistoryService, project: Project, snapshot_id: str) -> Snapshot:
    store = svc.store(project)
    snap = await store.get(snapshot_id)
    current = svc.sessions.current()
    if lock_status(project).locked or (
        current is not None and current.project.id == project.id and current.read_only
    ):
        raise ReadOnlyProject("project is open in KiCad; close it there before restoring")
    engine = svc.runtime.get(project.id)
    if engine is not None and engine.is_busy:
        raise AgentBusy("the agent is working; stop it or wait before restoring")

    sdir = store.snapshot_dir(snap.id)
    manifest = json.loads((sdir / "manifest.json").read_text())
    plan: list[tuple[Path, Path]] = []
    for entry in manifest["files"]:
        src = _inside(sdir, entry["relPath"])
        dst = _inside(project.dir, entry["relPath"])
        if hashlib.sha256(src.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"snapshot file {entry['relPath']} is corrupted")
        plan.append((src, dst))

    version = 0
    if current is not None and current.project.id == project.id:
        version = current.view_version
        await current.close()
    for src, dst in plan:
        tmp = dst.with_name(dst.name + ".copperchat-restore")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
    session = await svc.sessions.open(project)
    session.view_version = max(session.view_version, version)
    await svc.hashes.replace(project.id, compute_hashes(project))
    await svc.publish_view(project, session, svc.docs_of(project))
    if engine is not None:
        await engine.post_notice(
            f"Restored the design to the snapshot from {snap.created_at} ({snap.label}).",
            {"action": "restored", "snapshotId": snap.id})
        engine.add_agent_note(
            f"The user restored snapshot {snap.id} ({snap.label}). Design data from earlier "
            "tool results in this conversation may be stale; read it again before relying on it.")
    return snap
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_history_restore.py tests/unit/test_history_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add backend/src/copperchat/history/service.py backend/src/copperchat/history/restore.py backend/tests/unit/test_history_restore.py
git commit -m "feat: snapshot restore with session reopen and read-only guard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7.3: HistoryHooks — snapshot on first mutation, autosave + view refresh at turn end

**Files:**
- Create: `backend/src/copperchat/history/hooks.py`
- Modify: `backend/src/copperchat/web/app.py`
- Test: `backend/tests/unit/test_history_hooks.py`

**Interfaces:** Consumes `RunHooks`, `TurnInfo(run_id, message_id, text, metadata)`, `TurnOutcome(reason, changed_docs, mutated, kicad_failed)`, `KicadSession.save`. Produces `HistoryHooks` and wires `AgentRuntime.hooks_factory` through `install_history(app)`.

The foundation has no mutating tool, so the test defines a test-only `ToolSpec(mutating=True, changes_documents=True)` and drives it through Phase 5's `build_sdk_tool_list`, with the `ToolContext.on_mutation` callback bound to `hooks.on_first_mutation` the way the Phase 5 engine binds it (once per turn).

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/test_history_hooks.py`:
```python
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from copperchat.agent.engine import TurnInfo, TurnOutcome
from copperchat.agent.sdk_tools import build_sdk_tool_list
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.history.service import HistoryService
from copperchat.history.store import compute_hashes
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.tools import ToolContext, ToolResult, ToolSpec
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer
from tests.support.history import ECC83, copy_fixture
from tests.unit.test_history_restore import FakeEngine, FakeRuntime, RecordingBroadcaster


@pytest.fixture
async def env(tmp_path: Path, make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch):
    server = FakeServer()
    settings = make_settings()
    sessions = KicadSessionManager(settings, FakeLauncher(server),
                                   client_factory=FakeClientFactory(server))
    db = await Database.open(tmp_path / "db.sqlite")
    engine = FakeEngine()
    svc = HistoryService(db=db, sessions=sessions, broadcaster=RecordingBroadcaster(),
                         runtime=FakeRuntime(engine))
    saved: list[str] = []

    async def fake_save(self: KicadSession, doc: str) -> None:
        if self.read_only:
            raise AssertionError("save on read-only session")
        saved.append(doc)

    async def write_view(session: Any, doc: str) -> Path:
        return tmp_path / doc

    async def preview(session: Any, doc: str) -> bytes:
        return b"\x89PNG"

    monkeypatch.setattr(KicadSession, "save", fake_save)
    svc.write_view = write_view  # type: ignore[assignment]
    svc.render_preview = preview  # type: ignore[assignment]
    svc.fake_server = server  # type: ignore[attr-defined]  # crash tests reach the fake server
    project = copy_fixture(ECC83, settings.projects_root)
    session = await sessions.open(project)
    hooks = svc.hooks_factory(project, engine)  # type: ignore[arg-type]
    await hooks.on_engine_start()
    yield svc, hooks, project, session, saved, engine
    await sessions.close_all()
    await db.close()
    server.release.set()


def turn(run_id: str = "run-1") -> TurnInfo:
    return TurnInfo(run_id=run_id, message_id=1, text="move R1 next to U1", metadata={})


async def test_test_only_mutating_tool_triggers_one_snapshot(env) -> None:  # type: ignore[no-untyped-def]
    svc, hooks, project, session, saved, engine = env
    t = turn()
    await hooks.on_turn_start(t)
    fired = False

    async def on_mutation() -> None:          # Phase 5 engine semantics: first call per turn only
        nonlocal fired
        if not fired:
            fired = True
            await hooks.on_first_mutation(t)

    async def mutate(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(data={"ok": True}, changed_docs={"pcb"})

    spec = ToolSpec(name="test_mutate", description="test only", input_schema={"type": "object"},
                    handler=mutate, mutating=True, changes_documents=True)
    changed: set[str] = set()
    [tool] = build_sdk_tool_list([spec], lambda: ToolContext(
        project=project, session=session, settings=None, run_id="run-1",  # type: ignore[arg-type]
        on_mutation=on_mutation, on_documents_changed=changed.update))
    await tool.handler({})
    await tool.handler({})
    snaps = await svc.store(project).list()
    assert len(snaps) == 1 and snaps[0].run_id == "run-1" and snaps[0].has_preview
    assert snaps[0].label == "move R1 next to U1" and changed == {"pcb"}


async def test_turn_end_autosaves_changed_docs_and_publishes_view(env) -> None:  # type: ignore[no-untyped-def]
    svc, hooks, project, session, saved, engine = env
    session.view_version = 2
    await hooks.on_turn_end(turn(), TurnOutcome(reason="completed", changed_docs=frozenset({"pcb"}),
                                                mutated=True, kicad_failed=False))
    assert saved == ["pcb"]
    frame = svc.broadcaster.frames[-1]
    assert frame.type == "view-updated" and frame.docs == ["pcb"] and frame.version == 3
    assert session.view_version == 3
    assert await svc.hashes.get(project.id) == compute_hashes(project)


async def test_turn_without_changes_does_nothing(env) -> None:  # type: ignore[no-untyped-def]
    svc, hooks, project, session, saved, engine = env
    await hooks.on_turn_end(turn(), TurnOutcome(reason="completed", changed_docs=frozenset(),
                                                mutated=False, kicad_failed=False))
    assert saved == [] and svc.broadcaster.frames == []


async def test_read_only_session_never_autosaves_or_snapshots(env) -> None:  # type: ignore[no-untyped-def]
    svc, hooks, project, session, saved, engine = env
    session.read_only = True
    await hooks.on_first_mutation(turn())
    await hooks.on_turn_end(turn(), TurnOutcome(reason="completed", changed_docs=frozenset({"sch"}),
                                                mutated=True, kicad_failed=False))
    assert saved == [] and await svc.store(project).list() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_history_hooks.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'copperchat.history.hooks'`.

- [ ] **Step 3: Write minimal implementation**

`backend/src/copperchat/history/hooks.py` (Tasks 7.4 and 7.5 extend `on_turn_start` and `on_turn_end`; this step writes the full file so later tasks only add tests):
```python
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from copperchat.agent.engine import RunHooks, TurnInfo, TurnOutcome
from copperchat.history.store import compute_hashes, ensure_gitignore
from copperchat.kicad_session import DocKind, KicadSession, KicadUnavailable
from copperchat.projects import Project

if TYPE_CHECKING:
    from copperchat.agent.engine import RunEngine
    from copperchat.history.service import HistoryService

log = logging.getLogger(__name__)


class HistoryHooks(RunHooks):
    def __init__(self, svc: HistoryService, project: Project, engine: RunEngine) -> None:
        self.svc = svc
        self.project = project
        self.engine = engine

    async def _session(self) -> KicadSession:
        return await self.svc.session_for(self.project)

    async def _record_hashes(self) -> None:
        await self.svc.hashes.replace(self.project.id, compute_hashes(self.project))

    async def on_engine_start(self) -> None:
        ensure_gitignore(self.project)
        if not await self.svc.hashes.get(self.project.id):
            await self._record_hashes()

    async def on_turn_start(self, turn: TurnInfo) -> None:
        stored = await self.svc.hashes.get(self.project.id)
        now = compute_hashes(self.project)
        if not stored:
            await self.svc.hashes.replace(self.project.id, now)
            return
        changed = sorted(k for k in stored.keys() | now.keys() if stored.get(k) != now.get(k))
        if not changed:
            return
        current = self.svc.sessions.current()
        version = 0
        if current is not None and current.project.id == self.project.id:
            version = current.view_version
            await current.close()
        session = await self.svc.sessions.open(self.project)
        session.view_version = max(session.view_version, version)
        await self.svc.hashes.replace(self.project.id, now)
        await self.svc.publish_view(self.project, session, self.svc.docs_of(self.project))
        files = ", ".join(changed)
        await self.engine.post_notice(
            f"Files changed outside CopperChat since the last turn ({files}). "
            "CopperChat reloaded them; the agent works on the new state.",
            {"action": "external-change", "files": changed})
        self.engine.add_agent_note(
            f"The design files {files} were changed outside CopperChat and reloaded. "
            "Re-read any design data you rely on.")

    async def on_first_mutation(self, turn: TurnInfo) -> None:
        session = await self._session()
        if session.read_only:
            return
        docs = self.svc.docs_of(self.project)
        preview = await self.svc.render_preview(session, "pcb" if "pcb" in docs else "sch")
        await self.svc.store(self.project).snapshot(turn.run_id, turn.text.strip()[:80], preview)

    async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
        if outcome.kicad_failed:
            await self._recover(turn)
            return
        docs: set[DocKind] = set(outcome.changed_docs)
        if not docs:
            return
        session = await self._session()
        if session.read_only:
            return
        try:
            for doc in sorted(docs):
                await session.save(doc)
            await self._record_hashes()
            await self.svc.publish_view(self.project, session, docs)
        except KicadUnavailable as exc:
            log.warning("autosave failed: %s", exc)
            await self._recover(turn)

    async def _recover(self, turn: TurnInfo) -> None:
        await self.engine.post_error(
            "KiCad stopped responding during this turn. The KiCad API server was restarted; "
            "the design is at its last saved state.", {"code": "kicad_unavailable"})
        try:
            await self._session()
        except KicadUnavailable as exc:
            await self.engine.post_error(f"KiCad could not be restarted: {exc}",
                                         {"code": "kicad_unavailable"})
        snap = await self.svc.store(self.project).latest_for_run(turn.run_id)
        if snap is not None:
            await self.engine.post_notice(
                "A snapshot from before this turn exists. Restore it to undo this turn's changes.",
                {"action": "restore", "snapshotId": snap.id})
```

Modify `backend/src/copperchat/web/app.py` — in `create_app`, directly after `app.state.agent_runtime` is created (Phase 5) add:
```python
    from copperchat.history.service import install_history
    from copperchat.web import routes_git, routes_history

    install_history(app)
    app.include_router(routes_history.router, prefix="/api")
    app.include_router(routes_git.router, prefix="/api")
```
(The two route modules are created in Tasks 7.6 and 7.7; in this task add only the `install_history(app)` line and the import of `install_history`. Tasks 7.6 and 7.7 each add their own router line.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_history_hooks.py -q && uv run pytest -m "not kicad" -q`
Expected: PASS; existing app tests still green (hooks are installed but no-op without turns).

- [ ] **Step 5: Commit**
```bash
git add backend/src/copperchat/history/hooks.py backend/src/copperchat/web/app.py backend/tests/unit/test_history_hooks.py
git commit -m "feat: history run hooks for snapshot, autosave and view refresh

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7.4: External change detection before each turn (Review Focus 4)

**Files:**
- Test: `backend/tests/unit/test_external_change.py` (implementation already in `HistoryHooks.on_turn_start`, Task 7.3; if a test here fails, fix `hooks.py`)

**Interfaces:** Consumes `HistoryHooks.on_turn_start`, `DocumentHashRepo`. Produces notice metadata `{"action":"external-change","files":[...]}`.

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/test_external_change.py`:
```python
from tests.support.history import HIERARCHY, replace_with_kicad_file
from tests.unit.test_history_hooks import env, turn  # noqa: F401  (fixture reuse)


async def test_external_change_reloads_session_and_posts_notice(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    svc, hooks, project, session, saved, engine = env
    stored = await svc.hashes.get(project.id)
    assert stored
    replace_with_kicad_file(project.pcb_file, next(HIERARCHY.glob("*.kicad_pcb")))

    await hooks.on_turn_start(turn("run-2"))

    rel = project.pcb_file.relative_to(project.dir).as_posix()
    text, meta = engine.notices[-1]
    assert meta == {"action": "external-change", "files": [rel]}
    assert "outside CopperChat" in text
    assert engine.notes and rel in engine.notes[-1]
    new = svc.sessions.current()
    assert new is not None and new is not session            # stale session closed, fresh one opened
    assert (await svc.hashes.get(project.id))[rel] != stored[rel]
    assert svc.broadcaster.frames[-1].type == "view-updated"


async def test_unchanged_files_do_not_reload(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    svc, hooks, project, session, saved, engine = env
    await hooks.on_turn_start(turn("run-2"))
    assert engine.notices == [] and svc.sessions.current() is session


async def test_own_autosave_is_not_reported_as_external(env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from copperchat.agent.engine import TurnOutcome

    svc, hooks, project, session, saved, engine = env
    await hooks.on_turn_end(turn(), TurnOutcome(reason="completed", changed_docs=frozenset({"pcb"}),
                                                mutated=True, kicad_failed=False))
    await hooks.on_turn_start(turn("run-2"))
    assert engine.notices == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_external_change.py -q`
Expected (if Task 7.3 was implemented first, as written): PASS immediately. To see the test fail, temporarily replace the body of `on_turn_start` with `return`; expected FAIL `IndexError: list index out of range` on `engine.notices[-1]`. Restore the body.

- [ ] **Step 3: Write minimal implementation**

No new code: the behaviour is `HistoryHooks.on_turn_start` from Task 7.3 (hash compare → close + reopen session → `hashes.replace` → `publish_view` → `post_notice(..., {"action":"external-change","files":changed})` → `add_agent_note`). Phase 5's engine awaits `on_turn_start` before releasing the user message to the backend, so the note reaches the same turn's prompt and the agent never reads the stale session.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_external_change.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add backend/tests/unit/test_external_change.py
git commit -m "test: external change detection reloads session and notifies chat

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7.5: Crash recovery with restore offer (Review Focus 2)

**Files:**
- Test: `backend/tests/unit/test_crash_recovery.py` (implementation: `HistoryHooks._recover` from Task 7.3)

**Interfaces:** Consumes `FakeServer.crash()`, `KicadSession.call` restart behaviour (Phase 3 Task 3.7), `TurnOutcome.kicad_failed`, `RunEngine.post_error/post_notice`. Produces notice metadata `{"action":"restore","snapshotId":...}`.

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/test_crash_recovery.py`:
```python
import pytest

from copperchat.agent.engine import TurnOutcome
from copperchat.kicad_session import KicadUnavailable
from tests.unit.test_history_hooks import env, turn  # noqa: F401


async def _crash_turn(hooks, session, server, t):  # type: ignore[no-untyped-def]
    await hooks.on_turn_start(t)
    await hooks.on_first_mutation(t)
    server.crash()
    with pytest.raises(KicadUnavailable):
        await session.call(lambda h: h.kicad.get_version())   # fake server is dead mid-turn
    await hooks.on_turn_end(t, TurnOutcome(reason="error", changed_docs=frozenset({"pcb"}),
                                           mutated=True, kicad_failed=True))


async def test_crash_mid_turn_posts_error_and_restore_offer(env, request) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    svc, hooks, project, session, saved, engine = env
    server = session_server(request)
    t = turn("run-9")
    await _crash_turn(hooks, session, server, t)

    assert engine.errors and engine.errors[0][1] == {"code": "kicad_unavailable"}
    snap = (await svc.store(project).list())[0]
    assert engine.notices[-1][1] == {"action": "restore", "snapshotId": snap.id}
    assert saved == []                                       # no autosave of a broken turn
    version = await svc.sessions.current().call(lambda h: h.kicad.get_version().full_version)
    assert version == "11.0.0-fake"                          # next turn works


async def test_crash_without_snapshot_posts_only_error(env, request) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    svc, hooks, project, session, saved, engine = env
    server = session_server(request)
    t = turn("run-10")
    await hooks.on_turn_start(t)
    server.crash()
    await hooks.on_turn_end(t, TurnOutcome(reason="error", changed_docs=frozenset(),
                                           mutated=False, kicad_failed=True))
    assert engine.errors and not any(m and m.get("action") == "restore" for _, m in engine.notices)


def session_server(request):  # type: ignore[no-untyped-def]
    """The FakeServer behind the env fixture (exposed by the fake launcher)."""
    return request.getfixturevalue("env")[0].fake_server
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_crash_recovery.py -q`
Expected (Task 7.3 implemented): PASS. To confirm the test has teeth, temporarily make `_recover` return immediately; expected FAIL `assert engine.errors` (empty list). Restore it.

- [ ] **Step 3: Write minimal implementation**

No new code beyond Task 7.3's `_recover`: it posts the error row (`post_error`, code `kicad_unavailable`), ensures a session is running (`svc.session_for` reopens if the restart inside `KicadSession.call` left none), and posts the restore offer when `latest_for_run(run_id)` finds a snapshot. Autosave is skipped for `kicad_failed` turns, so the next turn starts from the last autosaved state.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_crash_recovery.py tests/unit/test_history_hooks.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add backend/tests/unit/test_crash_recovery.py
git commit -m "test: crash recovery posts error and restore offer

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7.6: History HTTP routes

**Files:**
- Create: `backend/src/copperchat/web/routes_history.py`
- Modify: `backend/src/copperchat/web/app.py` (include router)
- Test: `backend/tests/unit/test_routes_history.py`

**Interfaces:** Consumes `require_user`, `get_project`, `app.state.history`, `restore_snapshot`. Produces C8 history routes plus `GET /api/projects/{id}/history/{snapshotId}/preview`.

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/test_routes_history.py`:
```python
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from copperchat.config import Settings
from copperchat.projects import discover_projects
from copperchat.web.app import create_app
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer
from tests.support.history import ECC83, copy_fixture


@pytest.fixture
def client(tmp_path: Path):
    server = FakeServer()
    copy_fixture(ECC83, tmp_path / "projects")
    settings = Settings(projects_root=tmp_path / "projects", data_dir=tmp_path / "data",
                        admin_username="admin", admin_password=SecretStr("pw"),
                        dev_fake_agent=True, cookie_secure=False)
    app = create_app(settings, launcher=FakeLauncher(server), client_factory=FakeClientFactory(server))
    with TestClient(app) as c:
        async def write_view(session: Any, doc: str) -> Path:
            return tmp_path / doc
        app.state.history.write_view = write_view
        assert c.post("/api/auth/login", json={"username": "admin", "password": "pw"}).status_code == 204
        project = discover_projects(settings.projects_root)[0]
        c.app_ = app  # type: ignore[attr-defined]
        c.project = project  # type: ignore[attr-defined]
        yield c
    server.release.set()


def test_list_restore_and_preview(client: TestClient) -> None:
    app, project = client.app_, client.project  # type: ignore[attr-defined]
    pid = project.id
    assert client.post(f"/api/projects/{pid}/open").status_code == 200
    snap = client.portal.call(app.state.history.store(project).snapshot, "run-1", "turn one", b"\x89PNG")  # type: ignore[union-attr]
    listed = client.get(f"/api/projects/{pid}/history").json()
    assert listed[0]["id"] == snap.id and listed[0]["label"] == "turn one"
    assert listed[0]["previewUrl"] == f"/api/projects/{pid}/history/{snap.id}/preview"
    prev = client.get(listed[0]["previewUrl"])
    assert prev.status_code == 200 and prev.headers["content-type"] == "image/png"
    res = client.post(f"/api/projects/{pid}/history/{snap.id}/restore")
    assert res.status_code == 200 and res.json()["id"] == snap.id


@pytest.mark.parametrize("bad", ["..%2F..%2Fetc", "%2Fetc%2Fpasswd", "ZZZ", "0" * 31])
def test_invalid_ids_are_400(client: TestClient, bad: str) -> None:
    pid = client.project.id  # type: ignore[attr-defined]
    assert client.post(f"/api/projects/{pid}/history/{bad}/restore").status_code in (400, 404)
    assert client.get(f"/api/projects/{pid}/history/{bad}/preview").status_code in (400, 404)


def test_unknown_id_is_404_and_locked_is_409(client: TestClient) -> None:
    project = client.project  # type: ignore[attr-defined]
    assert client.post(f"/api/projects/{project.id}/history/{'0' * 32}/restore").status_code == 404
    snap = client.portal.call(client.app_.state.history.store(project).snapshot, "r", "x")  # type: ignore[attr-defined, union-attr]
    (project.dir / f"~{project.pcb_file.name}.lck").touch()
    assert client.post(f"/api/projects/{project.id}/history/{snap.id}/restore").status_code == 409


def test_requires_login(tmp_path: Path, client: TestClient) -> None:
    client.cookies.clear()
    assert client.get(f"/api/projects/{client.project.id}/history").status_code == 401  # type: ignore[attr-defined]
```

Note on the `400 | 404` assertion: an encoded `/` may be rejected by the router (404) before reaching the handler; the handler itself returns 400 for any malformed id (asserted strictly by `"ZZZ"` and `"0"*31` in `test_history_store.py`). Both outcomes satisfy Review Focus 5: nothing is read or written.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_routes_history.py -q`
Expected: FAIL — 404 on `/api/projects/{id}/history` (router missing).

- [ ] **Step 3: Write minimal implementation**

`backend/src/copperchat/web/routes_history.py`:
```python
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from copperchat.errors import ReadOnlyProject
from copperchat.history.restore import AgentBusy, restore_snapshot
from copperchat.history.service import HistoryService
from copperchat.history.store import InvalidSnapshotId, SnapshotNotFound
from copperchat.projects import Project, get_project
from copperchat.web.auth import require_user

router = APIRouter(dependencies=[Depends(require_user)])


def _ctx(request: Request, project_id: str) -> tuple[HistoryService, Project]:
    project = get_project(request.app.state.settings.projects_root, project_id)
    return request.app.state.history, project


@router.get("/projects/{project_id}/history")
async def list_history(project_id: str, request: Request) -> list[dict[str, Any]]:
    svc, project = _ctx(request, project_id)
    return [
        {"id": s.id, "runId": s.run_id, "createdAt": s.created_at, "label": s.label,
         "files": s.files,
         "previewUrl": f"/api/projects/{project.id}/history/{s.id}/preview" if s.has_preview else None}
        for s in await svc.store(project).list()
    ]


@router.get("/projects/{project_id}/history/{snapshot_id}/preview")
async def preview(project_id: str, snapshot_id: str, request: Request) -> FileResponse:
    svc, project = _ctx(request, project_id)
    store = svc.store(project)
    try:
        await store.get(snapshot_id)
        path = store.snapshot_dir(snapshot_id) / "preview.png"
    except InvalidSnapshotId as exc:
        raise HTTPException(400, str(exc)) from exc
    except SnapshotNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, "no preview for this snapshot")
    return FileResponse(path, media_type="image/png")


@router.post("/projects/{project_id}/history/{snapshot_id}/restore")
async def restore(project_id: str, snapshot_id: str, request: Request) -> dict[str, Any]:
    svc, project = _ctx(request, project_id)
    try:
        snap = await restore_snapshot(svc, project, snapshot_id)
    except InvalidSnapshotId as exc:
        raise HTTPException(400, str(exc)) from exc
    except SnapshotNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ReadOnlyProject, AgentBusy) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": snap.id, "files": snap.files}
```

Modify `backend/src/copperchat/web/app.py` (after `install_history(app)`):
```python
    from copperchat.web import routes_history

    app.include_router(routes_history.router, prefix="/api")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_routes_history.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add backend/src/copperchat/web/routes_history.py backend/src/copperchat/web/app.py backend/tests/unit/test_routes_history.py
git commit -m "feat: history list, preview and restore routes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7.7: Git tools, commit guard, git status route

**Files:**
- Create: `backend/src/copperchat/git_tools/__init__.py` (re-exports `TOOL_SPECS`), `backend/src/copperchat/git_tools/specs.py`, `backend/src/copperchat/web/routes_git.py`
- Modify: `backend/src/copperchat/tools/registry.py`, `backend/src/copperchat/web/app.py`
- Test: `backend/tests/unit/test_git_tools.py`

**Interfaces:** Consumes `ToolSpec`, `ToolContext.user_message`, `StructuredToolError`. Produces `COMMIT_INTENT`, `EXCLUDED_DIRS`, `git_status_data`, `TOOL_SPECS`, `GET /api/projects/{id}/git/status`. Registry: `_PROVIDERS` gains `"copperchat.git_tools.specs"`.

Rules: every git call is `git -C <project.dir> …` with env `GIT_TERMINAL_PROMPT=0`, `LANG=C`; pathspec `-- . :(exclude).kicad-agent/history :(exclude).kicad-agent/view :(exclude).kicad-agent/renders :(exclude).kicad-agent/uploads`; never `push`, `reset`, `rebase`, `--amend`, `checkout`. Commit test fixtures in temp repos use empty marker files (`.touch()`) and non-KiCad text files only.

- [ ] **Step 1: Write the failing test**

`backend/tests/unit/test_git_tools.py`:
```python
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from copperchat.git_tools.specs import COMMIT_INTENT, TOOL_SPECS, git_status_data
from copperchat.projects import discover_projects
from copperchat.tools import StructuredToolError, ToolContext
from copperchat.tools.registry import all_specs

SPECS = {s.name: s for s in TOOL_SPECS}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    proj = root / "boards" / "demo"
    proj.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "Test")
    for suffix in ("kicad_pro", "kicad_sch"):
        (proj / f"demo.{suffix}").touch()
    (root / "README.md").write_text("outside project\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


def ctx(project_dir: Path, user_message: str | None) -> ToolContext:
    project = discover_projects(project_dir.parent)[0]
    return ToolContext(project=project, session=cast(Any, None), settings=cast(Any, None),
                       user_message=user_message)


def make_changes(repo: Path) -> Path:
    proj = repo / "boards" / "demo"
    (proj / "notes.txt").write_text("rev B\n")
    for sub in ("history", "view", "renders", "uploads"):
        (proj / ".kicad-agent" / sub).mkdir(parents=True)
        (proj / ".kicad-agent" / sub / "x.bin").write_bytes(b"x")
    (repo / "README.md").write_text("changed outside\n")
    return proj


def test_registered_in_registry() -> None:
    names = {s.name for s in all_specs()}
    assert {"git_status", "git_diff_summary", "git_commit"} <= names
    assert all(not s.mutating for s in TOOL_SPECS)


@pytest.mark.parametrize("text,ok", [
    ("Please commit the current changes. Suggested message: rev B", True),
    ("COMMIT this", True), ("commit", True),
    ("what did I commit to?", True),
    ("I'm committed to this design", False), ("recommit", False), ("show git status", False),
])
def test_commit_intent(text: str, ok: bool) -> None:
    assert bool(COMMIT_INTENT.search(text)) is ok


async def test_status_and_diff_limited_to_project(repo: Path) -> None:
    proj = make_changes(repo)
    data = (await SPECS["git_status"].handler(ctx(proj, None), {})).data
    assert data is not None and data["branch"] == "main" and data["clean"] is False
    paths = [f["path"] for f in data["files"]]
    assert paths == ["boards/demo/notes.txt"]
    diff = (await SPECS["git_diff_summary"].handler(ctx(proj, None), {})).data
    assert diff is not None and [f["path"] for f in diff["files"]] == ["boards/demo/notes.txt"]


async def test_commit_requires_explicit_user_request(repo: Path) -> None:
    proj = make_changes(repo)
    with pytest.raises(StructuredToolError) as err:
        await SPECS["git_commit"].handler(ctx(proj, "looks good, thanks"), {"message": "rev B"})
    assert "commit" in err.value.to_dict()["why"]
    assert git(repo, "rev-list", "--count", "HEAD").strip() == "1"


async def test_commit_only_project_files_never_push(repo: Path) -> None:
    proj = make_changes(repo)
    res = (await SPECS["git_commit"].handler(
        ctx(proj, "Please commit the current changes. Suggested message: rev B"),
        {"message": "rev B"})).data
    assert res is not None and len(res["commit"]) == 40
    committed = git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["boards/demo/notes.txt"]
    assert "README.md" in git(repo, "status", "--porcelain")      # outside dir untouched
    assert git(repo, "log", "-1", "--format=%s").strip() == "rev B"
    assert git(repo, "remote").strip() == ""


async def test_commit_with_nothing_to_commit_is_error(repo: Path) -> None:
    proj = repo / "boards" / "demo"
    with pytest.raises(StructuredToolError):
        await SPECS["git_commit"].handler(ctx(proj, "commit please"), {"message": "x"})


async def test_outside_git_repo_refused(tmp_path: Path) -> None:
    proj = tmp_path / "plain" / "demo"
    proj.mkdir(parents=True)
    (proj / "demo.kicad_pro").touch()
    with pytest.raises(StructuredToolError) as err:
        await SPECS["git_status"].handler(ctx(proj, None), {})
    assert "git repository" in err.value.to_dict()["what"]
    assert (await git_status_data(proj))["isRepo"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_git_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'copperchat.git_tools'`.

- [ ] **Step 3: Write minimal implementation**

`backend/src/copperchat/git_tools/specs.py`:
```python
"""Agent git tools. Never push, never rewrite history, commit only on explicit request."""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from copperchat.tools import StructuredToolError, ToolContext, ToolResult, ToolSpec

COMMIT_INTENT = re.compile(r"\bcommit\b", re.IGNORECASE)
EXCLUDED_DIRS = (".kicad-agent/history", ".kicad-agent/view",
                 ".kicad-agent/renders", ".kicad-agent/uploads")
PATHSPEC = ["--", ".", *(f":(exclude){d}" for d in EXCLUDED_DIRS)]
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LANG": "C", "LC_ALL": "C"}


async def _git(project_dir: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(project_dir), *args, env=_ENV,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def _is_repo(project_dir: Path) -> bool:
    code, out, _ = await _git(project_dir, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


async def _require_repo(project_dir: Path) -> None:
    if not await _is_repo(project_dir):
        raise StructuredToolError(
            "The project is not inside a git repository",
            f"{project_dir.name} has no enclosing .git directory",
            "Tell the user to run `git init` in the project folder if they want version control.")


async def _branch(project_dir: Path) -> str | None:
    code, out, _ = await _git(project_dir, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out.strip() if code == 0 else None


async def _status_files(project_dir: Path) -> list[dict[str, str]]:
    code, out, err = await _git(project_dir, "status", "--porcelain=v1", "-z",
                                "--untracked-files=all", *PATHSPEC)
    if code != 0:
        raise StructuredToolError("git status failed", err.strip(), "Check the repository state.")
    files = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in "RC":
            i += 1  # skip the rename source
        files.append({"path": path, "status": status.strip() or status})
    return files


async def git_status_data(project_dir: Path) -> dict[str, Any]:
    if not await _is_repo(project_dir):
        return {"isRepo": False, "branch": None, "clean": True, "files": []}
    files = await _status_files(project_dir)
    return {"isRepo": True, "branch": await _branch(project_dir), "clean": not files,
            "files": files}


async def git_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    await _require_repo(ctx.project.dir)
    return ToolResult(data=await git_status_data(ctx.project.dir))


async def git_diff_summary(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    d = ctx.project.dir
    await _require_repo(d)
    code, _, _ = await _git(d, "rev-parse", "--verify", "--quiet", "HEAD")
    numstat: dict[str, tuple[int | None, int | None]] = {}
    if code == 0:
        _, out, _ = await _git(d, "diff", "HEAD", "--numstat", "--relative=", *PATHSPEC)
        for line in out.splitlines():
            added, deleted, path = line.split("\t", 2)
            numstat[path] = (None if added == "-" else int(added),
                             None if deleted == "-" else int(deleted))
    files = []
    for f in await _status_files(d):
        added, deleted = numstat.get(f["path"], (None, None))
        files.append({"path": f["path"], "status": f["status"], "added": added, "deleted": deleted})
    return ToolResult(data={"files": files, "count": len(files)})


async def git_commit(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    d = ctx.project.dir
    if not ctx.user_message or not COMMIT_INTENT.search(ctx.user_message):
        raise StructuredToolError(
            "Commit refused",
            "git_commit is only allowed when the user's message explicitly asks to commit",
            "Ask the user whether they want to commit, or tell them to use the Commit button.")
    message = str(args.get("message", "")).strip()
    if not message or len(message) > 2000:
        raise StructuredToolError("Invalid commit message", "message must be 1–2000 characters",
                                  "Write a short summary of the design change.")
    await _require_repo(d)
    if not await _status_files(d):
        raise StructuredToolError("Nothing to commit", "the project has no changes",
                                  "Tell the user the working tree is clean.")
    code, _, err = await _git(d, "add", "-A", *PATHSPEC)
    if code != 0:
        raise StructuredToolError("git add failed", err.strip(), "Check the repository state.")
    code, _, err = await _git(d, "commit", "--no-verify", "-q", "-m", message, *PATHSPEC)
    if code != 0:
        raise StructuredToolError("git commit failed", err.strip(),
                                  "Ask the user to configure git user.name/user.email.")
    _, sha, _ = await _git(d, "rev-parse", "HEAD")
    _, names, _ = await _git(d, "show", "--name-only", "--format=", "HEAD")
    return ToolResult(data={"commit": sha.strip(), "files": names.split()})


_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("git_status", "Branch and changed files of the project in its git repository "
             "(CopperChat caches excluded).", _EMPTY, git_status),
    ToolSpec("git_diff_summary", "Changed project files with added/deleted line counts "
             "against HEAD.", _EMPTY, git_diff_summary),
    ToolSpec("git_commit", "Commit the project's changes. Only when the user explicitly asked "
             "to commit in their message. Never pushes.",
             {"type": "object", "properties": {"message": {"type": "string", "minLength": 1,
                                                           "maxLength": 2000}},
              "required": ["message"], "additionalProperties": False}, git_commit),
]
```

`backend/src/copperchat/git_tools/__init__.py`:
```python
from copperchat.git_tools.specs import TOOL_SPECS

__all__ = ["TOOL_SPECS"]
```

Modify `backend/src/copperchat/tools/registry.py` — append to `_PROVIDERS`:
```python
    "copperchat.git_tools.specs",
```

`backend/src/copperchat/web/routes_git.py`:
```python
from typing import Any

from fastapi import APIRouter, Depends, Request

from copperchat.git_tools.specs import git_status_data
from copperchat.projects import get_project
from copperchat.web.auth import require_user

router = APIRouter(dependencies=[Depends(require_user)])


@router.get("/projects/{project_id}/git/status")
async def git_status(project_id: str, request: Request) -> dict[str, Any]:
    project = get_project(request.app.state.settings.projects_root, project_id)
    return await git_status_data(project.dir)
```

Modify `backend/src/copperchat/web/app.py` (next to the history router):
```python
    from copperchat.web import routes_git

    app.include_router(routes_git.router, prefix="/api")
```

Add to `test_git_tools.py`:
```python
def test_git_status_route(tmp_path: Path, repo: Path) -> None:
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from copperchat.config import Settings
    from copperchat.web.app import create_app

    settings = Settings(projects_root=repo, data_dir=tmp_path / "data", admin_username="admin",
                        admin_password=SecretStr("pw"), dev_fake_agent=True, cookie_secure=False)
    make_changes(repo)
    with TestClient(create_app(settings)) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "pw"})
        pid = discover_projects(repo)[0].id
        body = c.get(f"/api/projects/{pid}/git/status").json()
    assert body["isRepo"] is True and body["files"] == [{"path": "boards/demo/notes.txt", "status": "??"}]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_git_tools.py -q && uv run ruff check . && uv run mypy src`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add backend/src/copperchat/git_tools backend/src/copperchat/web/routes_git.py backend/src/copperchat/tools/registry.py backend/src/copperchat/web/app.py backend/tests/unit/test_git_tools.py
git commit -m "feat: git status/diff/commit tools with explicit-commit guard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7.8: Frontend History panel

**Files:**
- Create: `frontend/src/panels/HistoryPanel.tsx`, `frontend/src/panels/HistoryPanel.test.tsx`
- Modify: `frontend/src/panels/index.ts` (add `import './HistoryPanel';`)

**Interfaces:** Consumes `registerPanel`, `usePanels` (`@/layouts/panels`), `apiGet`, `apiPost`, `apiUrl` (`@/api/http`), `useChat(projectId).runActive`. Produces panel id `"history"`, exported `HistoryPanel({projectId})`, type `HistoryEntry`.

- [ ] **Step 1: Write the failing test**

`frontend/src/panels/HistoryPanel.test.tsx`:
```tsx
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiGet = vi.fn();
const apiPost = vi.fn();
vi.mock('@/api/http', () => ({
  apiGet: (...a: unknown[]) => apiGet(...a),
  apiPost: (...a: unknown[]) => apiPost(...a),
  apiUrl: (p: string) => `http://test${p}`,
}));
vi.mock('@/hooks/useChat', () => ({ useChat: () => ({ runActive: false }) }));

import { usePanels } from '@/layouts/panels';
import { HistoryPanel } from './HistoryPanel';

const entries = [
  { id: 'b'.repeat(32), runId: 'r2', createdAt: '2026-10-16T08:00:00Z', label: 'route USB',
    files: ['demo.kicad_pcb'], previewUrl: `/api/projects/p1/history/${'b'.repeat(32)}/preview` },
  { id: 'a'.repeat(32), runId: 'r1', createdAt: '2026-10-16T07:00:00Z', label: 'place R1',
    files: ['demo.kicad_pcb'], previewUrl: null },
];

describe('HistoryPanel', () => {
  beforeEach(() => {
    apiGet.mockReset().mockResolvedValue(entries);
    apiPost.mockReset().mockResolvedValue({ id: entries[0]!.id, files: [] });
  });

  it('is registered under id "history"', async () => {
    await import('./index');
    const { result } = await import('@testing-library/react').then(({ renderHook }) =>
      renderHook(() => usePanels()));
    expect(result.current.map((p) => p.id)).toContain('history');
  });

  it('lists turns with preview thumbnails', async () => {
    render(<HistoryPanel projectId="p1" />);
    expect(await screen.findByText('route USB')).toBeInTheDocument();
    expect(apiGet).toHaveBeenCalledWith('/projects/p1/history');
    const img = screen.getByAltText('Preview of route USB') as HTMLImageElement;
    expect(img.src).toBe(`http://test${entries[0]!.previewUrl}`);
    expect(screen.queryByAltText('Preview of place R1')).toBeNull();
  });

  it('asks for confirmation before undo and restores on confirm', async () => {
    render(<HistoryPanel projectId="p1" />);
    fireEvent.click((await screen.findAllByRole('button', { name: /undo/i }))[0]!);
    expect(screen.getByRole('dialog')).toHaveTextContent('route USB');
    expect(apiPost).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }));
    expect(screen.queryByRole('dialog')).toBeNull();

    fireEvent.click(screen.getAllByRole('button', { name: /undo/i })[0]!);
    fireEvent.click(screen.getByRole('button', { name: /restore/i }));
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith(`/projects/p1/history/${entries[0]!.id}/restore`, {}));
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2));
  });

  it('shows the server error when restore is refused', async () => {
    apiPost.mockRejectedValue(new Error('project is open in KiCad'));
    render(<HistoryPanel projectId="p1" />);
    fireEvent.click((await screen.findAllByRole('button', { name: /undo/i }))[0]!);
    fireEvent.click(screen.getByRole('button', { name: /restore/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent('project is open in KiCad');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/panels/HistoryPanel.test.tsx`
Expected: FAIL — `Failed to resolve import "./HistoryPanel"`.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/panels/HistoryPanel.tsx`:
```tsx
import { History } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { apiGet, apiPost, apiUrl } from '@/api/http';
import { useChat } from '@/hooks/useChat';
import { registerPanel } from '@/layouts/panels';

export interface HistoryEntry {
  id: string; runId: string; createdAt: string; label: string; files: string[];
  previewUrl: string | null;
}

export function HistoryPanel({ projectId }: { projectId: string }) {
  const { runActive } = useChat(projectId);
  const [entries, setEntries] = useState<HistoryEntry[] | null>(null);
  const [confirm, setConfirm] = useState<HistoryEntry | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setEntries(await apiGet<HistoryEntry[]>(`/projects/${projectId}/history`));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    if (!runActive) void load();     // refresh after every finished turn
  }, [load, runActive]);

  async function restore(entry: HistoryEntry) {
    setBusy(true);
    setError(null);
    try {
      await apiPost(`/projects/${projectId}/history/${entry.id}/restore`, {});
      setConfirm(null);
      await load();
    } catch (e) {
      setConfirm(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full flex-col gap-2 overflow-y-auto p-3 text-fg">
      {error && <p role="alert" className="rounded bg-danger/10 p-2 text-danger">{error}</p>}
      {entries === null && <p className="text-muted">Loading…</p>}
      {entries?.length === 0 && (
        <p className="text-muted">No snapshots yet. One is taken before each turn that changes the design.</p>
      )}
      <ul className="flex flex-col gap-2">
        {entries?.map((e) => (
          <li key={e.id} className="flex items-center gap-3 rounded border border-border bg-surface p-2">
            {e.previewUrl ? (
              <img src={apiUrl(e.previewUrl)} alt={`Preview of ${e.label}`}
                   className="h-14 w-20 flex-none rounded bg-surface-2 object-contain" />
            ) : (
              <div className="h-14 w-20 flex-none rounded bg-surface-2" aria-hidden />
            )}
            <div className="min-w-0 flex-1">
              <p className="truncate font-medium">{e.label || 'Agent turn'}</p>
              <p className="text-sm text-muted">{new Date(e.createdAt).toLocaleString()}</p>
            </div>
            <button type="button" disabled={runActive || busy} onClick={() => setConfirm(e)}
                    className="rounded border border-border px-3 py-1 disabled:opacity-50">
              Undo
            </button>
          </li>
        ))}
      </ul>
      {confirm && (
        <div role="dialog" aria-modal="true" aria-labelledby="history-confirm-title"
             className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-sm rounded bg-surface p-4 shadow-lg">
            <h2 id="history-confirm-title" className="mb-2 font-semibold">Restore snapshot?</h2>
            <p className="mb-4 text-sm">
              Restore the design to its state before “{confirm.label}”
              ({new Date(confirm.createdAt).toLocaleString()}). Later changes are replaced.
            </p>
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setConfirm(null)}
                      className="rounded border border-border px-3 py-1">Cancel</button>
              <button type="button" disabled={busy} onClick={() => void restore(confirm)}
                      className="rounded bg-danger px-3 py-1 text-white disabled:opacity-50">Restore</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

registerPanel({ id: 'history', title: 'History', icon: History, component: HistoryPanel, order: 10 });
```

Modify `frontend/src/panels/index.ts` — add:
```ts
import './HistoryPanel';
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/panels/HistoryPanel.test.tsx && npm run typecheck && npm run lint`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/panels/HistoryPanel.tsx frontend/src/panels/HistoryPanel.test.tsx frontend/src/panels/index.ts
git commit -m "feat: history panel with previews and confirmed undo

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7.9: Frontend Git panel

**Files:**
- Create: `frontend/src/panels/GitPanel.tsx`, `frontend/src/panels/GitPanel.test.tsx`
- Modify: `frontend/src/panels/index.ts` (add `import './GitPanel';`)

**Interfaces:** Consumes `registerPanel`, `apiGet`, `useChat(projectId).send/runActive`. Produces panel id `"git"`, exported `GitPanel({projectId})`, `commitRequestText(message: string): string`.

- [ ] **Step 1: Write the failing test**

`frontend/src/panels/GitPanel.test.tsx`:
```tsx
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiGet = vi.fn();
const send = vi.fn();
vi.mock('@/api/http', () => ({ apiGet: (...a: unknown[]) => apiGet(...a) }));
vi.mock('@/hooks/useChat', () => ({ useChat: () => ({ runActive: false, send }) }));

import { GitPanel, commitRequestText } from './GitPanel';

describe('GitPanel', () => {
  beforeEach(() => {
    send.mockReset();
    apiGet.mockReset().mockResolvedValue({
      isRepo: true, branch: 'main', clean: false,
      files: [{ path: 'boards/demo/demo.kicad_pcb', status: 'M' }],
    });
  });

  it('commit text always contains the word commit (backend guard)', () => {
    expect(commitRequestText('rev B')).toBe('Please commit the current changes. Suggested message: rev B');
    expect(commitRequestText('  ')).toBe('Please commit the current changes.');
    expect(/\bcommit\b/i.test(commitRequestText('x'))).toBe(true);
  });

  it('shows branch and changed files', async () => {
    render(<GitPanel projectId="p1" />);
    expect(await screen.findByText('boards/demo/demo.kicad_pcb')).toBeInTheDocument();
    expect(screen.getByText(/main/)).toBeInTheDocument();
    expect(apiGet).toHaveBeenCalledWith('/projects/p1/git/status');
  });

  it('Commit sends a chat message instead of committing directly', async () => {
    render(<GitPanel projectId="p1" />);
    await screen.findByText('boards/demo/demo.kicad_pcb');
    fireEvent.change(screen.getByLabelText(/suggested message/i), { target: { value: 'rev B' } });
    fireEvent.click(screen.getByRole('button', { name: /^commit/i }));
    expect(send).toHaveBeenCalledWith('Please commit the current changes. Suggested message: rev B');
  });

  it('disables Commit when clean and explains when not a repo', async () => {
    apiGet.mockResolvedValueOnce({ isRepo: true, branch: 'main', clean: true, files: [] });
    const { unmount } = render(<GitPanel projectId="p1" />);
    await waitFor(() => expect(screen.getByRole('button', { name: /^commit/i })).toBeDisabled());
    unmount();
    apiGet.mockResolvedValueOnce({ isRepo: false, branch: null, clean: true, files: [] });
    render(<GitPanel projectId="p1" />);
    expect(await screen.findByText(/not in a git repository/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/panels/GitPanel.test.tsx`
Expected: FAIL — `Failed to resolve import "./GitPanel"`.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/panels/GitPanel.tsx`:
```tsx
import { GitBranch } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { apiGet } from '@/api/http';
import { useChat } from '@/hooks/useChat';
import { registerPanel } from '@/layouts/panels';

interface GitStatus {
  isRepo: boolean; branch: string | null; clean: boolean;
  files: { path: string; status: string }[];
}

export function commitRequestText(message: string): string {
  const m = message.trim();
  return m ? `Please commit the current changes. Suggested message: ${m}` : 'Please commit the current changes.';
}

export function GitPanel({ projectId }: { projectId: string }) {
  const { runActive, send } = useChat(projectId);
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState('');

  const load = useCallback(async () => {
    try {
      setStatus(await apiGet<GitStatus>(`/projects/${projectId}/git/status`));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [projectId]);

  useEffect(() => {
    if (!runActive) void load();
  }, [load, runActive]);

  if (error) return <p role="alert" className="p-3 text-danger">{error}</p>;
  if (!status) return <p className="p-3 text-muted">Loading…</p>;
  if (!status.isRepo)
    return <p className="p-3 text-muted">This project is not in a git repository.</p>;

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-3 text-fg">
      <p className="text-sm">Branch <span className="font-mono">{status.branch ?? '(detached)'}</span></p>
      {status.clean ? (
        <p className="text-muted">No uncommitted changes.</p>
      ) : (
        <ul className="flex flex-col gap-1 font-mono text-sm">
          {status.files.map((f) => (
            <li key={f.path} className="flex gap-2">
              <span className="w-6 text-warning">{f.status}</span><span className="truncate">{f.path}</span>
            </li>
          ))}
        </ul>
      )}
      <label className="flex flex-col gap-1 text-sm">
        Suggested message
        <input value={message} onChange={(e) => setMessage(e.target.value)}
               className="rounded border border-border bg-surface px-2 py-1" />
      </label>
      <button type="button" disabled={status.clean || runActive}
              onClick={() => { send(commitRequestText(message)); setMessage(''); }}
              className="self-start rounded bg-accent px-3 py-1 text-accent-fg disabled:opacity-50">
        Commit via agent
      </button>
      <p className="text-xs text-muted">The agent commits only project files and never pushes.</p>
    </div>
  );
}

registerPanel({ id: 'git', title: 'Git', icon: GitBranch, component: GitPanel, order: 20 });
```

Modify `frontend/src/panels/index.ts` — add:
```ts
import './GitPanel';
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npm test -- --run && npm run typecheck && npm run lint && npm run build`
Expected: PASS.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/panels/GitPanel.tsx frontend/src/panels/GitPanel.test.tsx frontend/src/panels/index.ts
git commit -m "feat: git panel with commit-via-chat button

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Phase self-review

**Spec coverage:** save/autosave after every agent turn that changed a document (Task 7.3); undo via own per-turn snapshots under `.kicad-agent/history/` (7.1, 7.2, 7.6, 7.8); external-change detection (7.4); api-server crash/hang recovery (7.5, building on Phase 3 Task 3.7 restart); git tools with commit-only-on-request and no push/rewrite (7.7, 7.9); read-only projects with lock file (7.2, 7.3); path-traversal rejection for snapshot ids (7.1, 7.6). Global constraint "never hand-edit KiCad files": snapshots and restore copy bytes KiCad wrote; tests only copy KiCad-generated fixtures or touch empty marker files.

**Deviations / notes for the coordinator:**
- Task numbering vs index Review Focus: index lists RF2 → Task 7.4, RF4 → Task 7.3. In this file, external change is Task 7.4 (RF4) and crash recovery is Task 7.5 (RF2); RF3 → 7.2, RF5 → 7.1 match. Please update the index lines.
- Registry provider is `"copperchat.git_tools.specs"` (index C5 text says `git_tools`; `git_tools/__init__.py` re-exports `TOOL_SPECS`, so either name works).
- NEW route `GET /api/projects/{id}/history/{snapshotId}/preview` and NEW `app.state.history` (`HistoryService`); thumbnails are agent-view renders of the pre-turn state taken at snapshot time.
- Assumes Phase 5's engine calls `hooks.on_first_mutation` once per turn from `ToolContext.on_mutation`, sets `TurnOutcome.kicad_failed` from `on_kicad_unavailable`, and awaits `on_turn_start` before releasing the message so `add_agent_note` reaches the same turn.
- Tasks 7.4 and 7.5 are test-only tasks because their logic lives in `hooks.py` (Task 7.3); each documents how to see the test fail.
