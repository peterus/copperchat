# Phase 8: Skills, part researcher, uploads

> **Cross-phase corrections apply:** see index § "Cross-phase corrections" (X5); they supersede conflicting code in this file.

**Goal:** Three-layer skills (built-in / user / project) assembled per session
and passed to the SDK, with a Skills page; the `part-researcher` sub-agent
reachable through the `research_part` tool, with a path guard and a Parts
page; image/PDF attachments uploaded from the composer and passed to the
agent.

**Depends on:** Phase 5 (agent runtime, `RunEngine`, `sdk_backend.py`),
Phase 6A (frontend shell, panels, composer slot).

**Parallel with:** 6B, 7.

**Read first:**
- Index `docs/superpowers/plans/2026-10-15-foundation-plan.md` (Global
  Constraints, Review Focus 5, C2, C3, C5, C7, C8).
- Spec §4.2 (Skills page, Parts page, attachments), §6 (`research_part`), §7,
  §8.
- `docs/spikes/1a-sdk-streaming.md` (how the adapter builds options and user
  messages).
- SDK source for verification: `claude_agent_sdk/types.py` (`AgentDefinition`,
  `ClaudeAgentOptions.skills|plugins|setting_sources`, `ToolPermissionContext`),
  `_internal/transport/subprocess_cli.py` (`_apply_skills_defaults`).

**Surfaces from earlier phases this phase relies on** (verify the exact names
in the merged code before starting; if a name differs, use the merged name and
note it in the done report):
- Phase 3: `create_app(settings)` stores `app.state.settings` and
  `app.state.db`; `copperchat.web.auth.require_user` dependency;
  `copperchat.errors.CopperChatError`; `POST /api/auth/login` seeds from
  `admin_password`.
- Phase 4: at least one fixture project under `backend/tests/fixtures/projects/`.
- Phase 5: module functions in `copperchat.agent.sdk_backend`: `build_options(*, settings, project, mcp_server, tool_names, resume_session_id, fork_session) -> ClaudeAgentOptions`
  (sync) and `user_prompt(item: QueuedItem) -> dict` (sync), both called by `SdkStreamingBackend` (or `SdkPerTurnBackend`), aliased as `SdkAgentBackend`; `make_tool_guard(allowed: frozenset[str])`; `default_backend_factory(settings, specs)`; `QueuedItem.text`,
  `QueuedItem.attachments: list[AttachmentRef]`; `copperchat.agent.protocol.AttachmentRef`;
  `RunEngine` validates attachment ids through an injectable
  `attachment_exists: Callable[[str, str], Awaitable[bool]]`.
- Phase 6A: `apiGet/apiPost/apiPut/apiDelete` (`frontend/src/api/http.ts`),
  `registerPanel({id, title, icon, component})` where panel components receive
  `{ projectId: string }` (panel modules are imported from `frontend/src/panels/index.ts`),
  `composerAttachmentSlot.register(Component)` from `@/components/chat/composerSlots` where the slot
  component receives `{ projectId: string; attachments: AttachmentRef[]; setAttachments(next: AttachmentRef[]): void }`
  and the composer passes `attachments` to `useChat(projectId).send(text, attachments)`.

**PR title:** `foundation phase 8: skills, part researcher, uploads`

**Files:**
- Create: `backend/src/copperchat/db/migrations/0004_skills.sql`
- Create: `backend/src/copperchat/db/repos/skill_prefs.py`, `backend/src/copperchat/db/repos/uploads.py`
- Create: `backend/src/copperchat/skills/__init__.py`, `skills/store.py`, `skills/assembly.py`
- Create: `backend/src/copperchat/skills/builtin/kicad-schematic-style/SKILL.md`,
  `skills/builtin/kicad-review-discipline/SKILL.md`, `skills/builtin/copperchat-tools/SKILL.md`
- Create: `backend/src/copperchat/research/__init__.py`, `research/paths.py`, `research/agent.py`,
  `research/runner.py`, `research/tools.py`
- Create: `backend/src/copperchat/parts.py`, `backend/src/copperchat/uploads.py`
- Create: `backend/src/copperchat/web/routes_skills.py`, `web/routes_parts.py`, `web/routes_uploads.py`
- Create: `backend/src/copperchat/agent/guards.py` (`make_researcher_guard`; Phase 5 keeps `make_tool_guard` in `sdk_backend.py`)
- Modify: `backend/src/copperchat/agent/sdk_backend.py` (skills + attachments)
- Modify: `backend/src/copperchat/agent/run_engine.py` wiring in `web/app.py` (attachment resolver)
- Modify: `backend/src/copperchat/tools/registry.py` (add `research` provider)
- Modify: `backend/src/copperchat/web/app.py` (include routers)
- Modify: `backend/pyproject.toml` (`httpx` runtime dependency, `live_sdk` marker)
- Create: `frontend/src/api/skills.ts`, `frontend/src/api/parts.ts`, `frontend/src/api/uploads.ts`
- Create: `frontend/src/pages/skills/SkillsPanel.tsx`, `frontend/src/pages/parts/PartsPanel.tsx`,
  `frontend/src/components/chat/AttachmentPicker.tsx`
- Modify: `frontend/src/panels/index.ts` (register Skills/Parts panels and the composer attachment slot)
- Tests: listed per task.

**Acceptance criteria:**
- Skills from three layers are listed with origin badge; the most specific
  layer wins; customise copies a built-in into `/data/skills/<name>/` with a
  `.origin.json` sidecar; reset deletes the user copy; an "original updated"
  notice with a unified diff appears when the built-in changes after
  customising; per-project enable/disable persists in `skill_prefs`.
- Each session gets a generated plugin directory
  `/data/runtime/skills/<projectId>/` passed via `plugins=[…]`,
  `skills=["copperchat:<name>", …]`, `setting_sources=[]` (the user's
  `~/.claude` is never loaded). Main agent built-in tools are exactly
  `["Skill"]` (or `[]` when no skill is enabled); no `Agent`/`Task` tool, no
  `agents=` on the main session.
- `research_part(query, attachment_paths?)` runs a separate one-shot SDK
  query configured from the `part-researcher` `AgentDefinition`; its
  `can_use_tool` guard allows Write/Edit only under `<project>/docs/parts/`
  (not `_uploads/`), Read/Glob only under `docs/parts/` and
  `.kicad-agent/uploads/`, rejects `..`, foreign absolute paths and symlink
  escapes, and refuses to modify a `notes.md` whose front matter has
  `user_edited: true`.
- Parts page lists `docs/parts/<MPN>/`, edits `notes.md` (saving sets
  `user_edited: true`), opens `datasheet.pdf`.
- `POST /api/projects/{id}/uploads` accepts PNG/JPEG/WebP/GIF ≤ 15 MB and PDF
  ≤ 20 MB by magic bytes, sanitises filenames, stores images (re-encoded,
  ≤ 2048 px) in `.kicad-agent/uploads/` and PDFs in `docs/parts/_uploads/`,
  returns `AttachmentRef`; images reach the model as image content blocks,
  PDFs as a text note with the project-relative path.
- All Review Focus 5 inputs of this phase (skill names, researcher paths, MPN
  folder names, upload filenames) are rejected with 400 / tool denial.

**Verification commands:**
- `cd backend && uv sync && uv run pytest -m "not kicad" && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
- `cd frontend && npm ci && npm run typecheck && npm run lint && npm test -- --run && npm run build`
- Optional, with a token in the environment: `cd backend && uv run pytest -m live_sdk`

**CI expectations:** existing `backend`, `frontend` and `kicad` jobs green; no
new job. `live_sdk` tests are deselected in CI (`-m "not kicad"` plus the
`live_sdk` skip when no token is set).

---

### Task 8.1: Migration 0004 and repositories

**Files:**
- Create: `backend/src/copperchat/db/migrations/0004_skills.sql`
- Create: `backend/src/copperchat/db/repos/skill_prefs.py`
- Create: `backend/src/copperchat/db/repos/uploads.py`
- Test: `backend/tests/unit/test_repos_phase8.py`

**Interfaces:**
- Consumes: `Database` (C2).
- Produces:
  - table `skill_prefs(project_id, skill_name, enabled, PRIMARY KEY(project_id, skill_name))` (C2)
  - table `uploads(id TEXT PK, project_id TEXT, kind TEXT, name TEXT, rel_path TEXT, mime TEXT, size INTEGER, created_at TEXT)` (**new**, reported to index)
  - `async def set_skill_enabled(db, project_id: str, skill_name: str, enabled: bool) -> None`
  - `async def disabled_skills(db, project_id: str) -> set[str]`
  - `@dataclass(frozen=True) class UploadRow: id, project_id, kind, name, rel_path, mime, size, created_at`
  - `async def insert_upload(db, row: UploadRow) -> None`, `async def get_upload(db, project_id: str, upload_id: str) -> UploadRow | None`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_repos_phase8.py
from pathlib import Path

import pytest

from copperchat.db import Database
from copperchat.db.repos import skill_prefs, uploads


@pytest.fixture
async def db(tmp_path: Path):
    d = await Database.open(tmp_path / "t.db")
    yield d
    await d.close()


async def test_skill_prefs_default_enabled_and_toggle(db: Database) -> None:
    assert await skill_prefs.disabled_skills(db, "p1") == set()
    await skill_prefs.set_skill_enabled(db, "p1", "copperchat-tools", False)
    assert await skill_prefs.disabled_skills(db, "p1") == {"copperchat-tools"}
    assert await skill_prefs.disabled_skills(db, "p2") == set()
    await skill_prefs.set_skill_enabled(db, "p1", "copperchat-tools", True)
    assert await skill_prefs.disabled_skills(db, "p1") == set()


async def test_upload_roundtrip_is_project_scoped(db: Database) -> None:
    row = uploads.UploadRow(
        id="a" * 32, project_id="p1", kind="image", name="board.jpg",
        rel_path=".kicad-agent/uploads/" + "a" * 32 + ".jpg", mime="image/jpeg",
        size=123, created_at="2026-10-15T00:00:00Z",
    )
    await uploads.insert_upload(db, row)
    assert await uploads.get_upload(db, "p1", "a" * 32) == row
    assert await uploads.get_upload(db, "p2", "a" * 32) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_repos_phase8.py -v`
Expected: FAIL with `ImportError: cannot import name 'skill_prefs'`.

- [ ] **Step 3: Write minimal implementation**

```sql
-- backend/src/copperchat/db/migrations/0004_skills.sql
CREATE TABLE skill_prefs (
    project_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    PRIMARY KEY (project_id, skill_name)
);

CREATE TABLE uploads (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('image', 'pdf')),
    name TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_uploads_project ON uploads(project_id);
```

```python
# backend/src/copperchat/db/repos/skill_prefs.py
from copperchat.db import Database


async def set_skill_enabled(db: Database, project_id: str, skill_name: str, enabled: bool) -> None:
    await db.execute(
        "INSERT INTO skill_prefs(project_id, skill_name, enabled) VALUES (?, ?, ?) "
        "ON CONFLICT(project_id, skill_name) DO UPDATE SET enabled = excluded.enabled",
        (project_id, skill_name, 1 if enabled else 0),
    )


async def disabled_skills(db: Database, project_id: str) -> set[str]:
    rows = await db.fetchall(
        "SELECT skill_name FROM skill_prefs WHERE project_id = ? AND enabled = 0", (project_id,)
    )
    return {str(r["skill_name"]) for r in rows}
```

```python
# backend/src/copperchat/db/repos/uploads.py
from dataclasses import astuple, dataclass

from copperchat.db import Database


@dataclass(frozen=True)
class UploadRow:
    id: str
    project_id: str
    kind: str
    name: str
    rel_path: str
    mime: str
    size: int
    created_at: str


async def insert_upload(db: Database, row: UploadRow) -> None:
    await db.execute(
        "INSERT INTO uploads(id, project_id, kind, name, rel_path, mime, size, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        astuple(row),
    )


async def get_upload(db: Database, project_id: str, upload_id: str) -> UploadRow | None:
    r = await db.fetchone(
        "SELECT id, project_id, kind, name, rel_path, mime, size, created_at "
        "FROM uploads WHERE project_id = ? AND id = ?",
        (project_id, upload_id),
    )
    return None if r is None else UploadRow(*tuple(r))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_repos_phase8.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/db/migrations/0004_skills.sql backend/src/copperchat/db/repos/skill_prefs.py backend/src/copperchat/db/repos/uploads.py backend/tests/unit/test_repos_phase8.py
git commit -m "feat: add skill_prefs and uploads tables

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8.2: Skill layers, name validation, customise / reset / origin diff

**Files:**
- Create: `backend/src/copperchat/skills/__init__.py` (empty), `backend/src/copperchat/skills/store.py`
- Test: `backend/tests/unit/test_skills_store.py`

**Interfaces:**
- Consumes: `Settings.data_dir` (C1), `agent_dir(project)` (C3), `CopperChatError`.
- Produces (`copperchat.skills.store`):
  - `Origin = Literal["builtin", "user", "project"]`
  - `class InvalidSkillName(CopperChatError)`, `class InvalidSkill(CopperChatError)`, `class SkillNotFound(CopperChatError)`, `class SkillConflict(CopperChatError)`
  - `def validate_skill_name(name: str) -> str` — `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`
  - `def parse_front_matter(text: str) -> tuple[dict[str, str], str]`
  - `@dataclass(frozen=True) class SkillLayers(builtin_root: Path, user_root: Path, project_root: Path | None)` with `@classmethod for_project(cls, settings, project: Project | None)`
  - `@dataclass(frozen=True) class EffectiveSkill(name, origin, description, dir: Path, shadowed: tuple[Origin, ...], has_builtin: bool, origin_updated: bool)`
  - `def effective_skills(layers) -> list[EffectiveSkill]`, `def get_skill(layers, name) -> EffectiveSkill`
  - `def customise(layers, name) -> Path`, `def write_user_skill(layers, name, content: str) -> Path`, `def reset(layers, name) -> None`
  - `def origin_diff(layers, name) -> str | None`, `def acknowledge_update(layers, name) -> None`

Rules: a layer entry counts only if `<root>/<name>/` is a real directory (not
a symlink), its name passes validation, and `SKILL.md` is a regular file (not a
symlink) ≤ 256 KiB with front matter `name` == directory name and non-empty
`description`. Invalid entries are skipped silently in listings (they never
reach the SDK).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_skills_store.py
import json
from pathlib import Path

import pytest

from copperchat.skills import store
from copperchat.skills.store import SkillLayers


def skill_md(name: str, desc: str = "Test skill", body: str = "Body\n") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}"


def put(root: Path, name: str, text: str) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(text)


@pytest.fixture
def layers(tmp_path: Path) -> SkillLayers:
    b, u, p = tmp_path / "builtin", tmp_path / "user", tmp_path / "proj"
    for d in (b, u, p):
        d.mkdir()
    put(b, "alpha", skill_md("alpha", "builtin alpha"))
    put(b, "beta", skill_md("beta", "builtin beta"))
    return SkillLayers(builtin_root=b, user_root=u, project_root=p)


@pytest.mark.parametrize(
    "bad", ["", "../x", "a/b", "a\\b", ".hidden", "Upper", "x" * 65, "-lead", "trail-", "a b", "a:b", "..", "a\x00b"]
)
def test_invalid_names_rejected(bad: str) -> None:
    with pytest.raises(store.InvalidSkillName):
        store.validate_skill_name(bad)


def test_most_specific_layer_wins(layers: SkillLayers) -> None:
    put(layers.user_root, "alpha", skill_md("alpha", "user alpha"))
    assert layers.project_root is not None
    put(layers.project_root, "alpha", skill_md("alpha", "project alpha"))
    by_name = {s.name: s for s in store.effective_skills(layers)}
    assert by_name["alpha"].origin == "project"
    assert by_name["alpha"].description == "project alpha"
    assert by_name["alpha"].shadowed == ("user", "builtin")
    assert by_name["beta"].origin == "builtin"


def test_symlinked_and_mismatched_entries_skipped(layers: SkillLayers, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    put(outside, "evil", skill_md("evil"))
    assert layers.project_root is not None
    (layers.project_root / "evil").symlink_to(outside / "evil", target_is_directory=True)
    (layers.user_root / "gamma").mkdir()
    (layers.user_root / "gamma" / "SKILL.md").symlink_to(outside / "evil" / "SKILL.md")
    put(layers.user_root, "delta", skill_md("not-delta"))
    put(layers.user_root, "Bad_Name", skill_md("Bad_Name"))
    names = {s.name for s in store.effective_skills(layers)}
    assert names == {"alpha", "beta"}


def test_customise_copies_and_writes_sidecar(layers: SkillLayers) -> None:
    dest = store.customise(layers, "alpha")
    assert dest == layers.user_root / "alpha"
    assert (dest / "SKILL.md").read_text() == skill_md("alpha", "builtin alpha")
    sidecar = json.loads((dest / ".origin.json").read_text())
    assert set(sidecar) == {"builtinSha256", "builtinContent"}
    with pytest.raises(store.SkillConflict):
        store.customise(layers, "alpha")
    with pytest.raises(store.SkillNotFound):
        store.customise(layers, "nope")


def test_origin_updated_diff_and_acknowledge(layers: SkillLayers) -> None:
    store.customise(layers, "alpha")
    assert store.get_skill(layers, "alpha").origin_updated is False
    assert store.origin_diff(layers, "alpha") is None
    put(layers.builtin_root, "alpha", skill_md("alpha", "builtin alpha", "Body\nNew rule\n"))
    assert store.get_skill(layers, "alpha").origin_updated is True
    diff = store.origin_diff(layers, "alpha")
    assert diff is not None and "+New rule" in diff
    store.acknowledge_update(layers, "alpha")
    assert store.get_skill(layers, "alpha").origin_updated is False


def test_write_user_skill_validates_front_matter(layers: SkillLayers) -> None:
    with pytest.raises(store.InvalidSkill):
        store.write_user_skill(layers, "mine", skill_md("other"))
    with pytest.raises(store.InvalidSkill):
        store.write_user_skill(layers, "mine", "no front matter")
    with pytest.raises(store.InvalidSkillName):
        store.write_user_skill(layers, "../escape", skill_md("escape"))
    path = store.write_user_skill(layers, "mine", skill_md("mine"))
    assert path == layers.user_root / "mine" / "SKILL.md"
    assert not (layers.user_root.parent / "escape").exists()


def test_reset_removes_user_layer(layers: SkillLayers) -> None:
    store.customise(layers, "alpha")
    store.reset(layers, "alpha")
    assert store.get_skill(layers, "alpha").origin == "builtin"
    with pytest.raises(store.SkillNotFound):
        store.reset(layers, "alpha")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_skills_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'copperchat.skills'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/skills/store.py
"""Three-layer skill storage: built-in (image), user (/data/skills), project."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from copperchat.config import Settings
from copperchat.errors import CopperChatError
from copperchat.projects import Project, agent_dir

Origin = Literal["builtin", "user", "project"]
BUILTIN_ROOT = Path(__file__).parent / "builtin"
MAX_SKILL_BYTES = 256 * 1024
SIDECAR = ".origin.json"
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")


class InvalidSkillName(CopperChatError):
    pass


class InvalidSkill(CopperChatError):
    pass


class SkillNotFound(CopperChatError):
    pass


class SkillConflict(CopperChatError):
    pass


def validate_skill_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise InvalidSkillName(f"invalid skill name: {name!r}")
    return name


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "".join(lines[i + 1 :])
        key, sep, value = line.partition(":")
        if sep and key.strip():
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return {}, text


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SkillLayers:
    builtin_root: Path
    user_root: Path
    project_root: Path | None

    @classmethod
    def for_project(cls, settings: Settings, project: Project | None) -> SkillLayers:
        return cls(
            builtin_root=BUILTIN_ROOT,
            user_root=settings.data_dir / "skills",
            project_root=None if project is None else agent_dir(project) / "skills",
        )


@dataclass(frozen=True)
class EffectiveSkill:
    name: str
    origin: Origin
    description: str
    dir: Path
    shadowed: tuple[Origin, ...]
    has_builtin: bool
    origin_updated: bool


def _load(root: Path | None, name: str) -> tuple[Path, str, str] | None:
    """Return (dir, description, content) if root/name is a valid skill entry."""
    if root is None:
        return None
    d = root / name
    md = d / "SKILL.md"
    if d.is_symlink() or not d.is_dir() or md.is_symlink() or not md.is_file():
        return None
    if md.stat().st_size > MAX_SKILL_BYTES:
        return None
    content = md.read_text(encoding="utf-8", errors="replace")
    meta, _ = parse_front_matter(content)
    if meta.get("name") != name or not meta.get("description"):
        return None
    return d, meta["description"], content


def _names(root: Path | None) -> set[str]:
    if root is None or not root.is_dir():
        return set()
    return {p.name for p in root.iterdir() if _NAME_RE.fullmatch(p.name)}


def _origin_updated(layers: SkillLayers, name: str, user_dir: Path) -> bool:
    sidecar = user_dir / SIDECAR
    builtin = _load(layers.builtin_root, name)
    if builtin is None or not sidecar.is_file():
        return False
    data = json.loads(sidecar.read_text())
    return bool(data.get("builtinSha256") != _sha(builtin[2]))


def effective_skills(layers: SkillLayers) -> list[EffectiveSkill]:
    order: list[tuple[Origin, Path | None]] = [
        ("project", layers.project_root),
        ("user", layers.user_root),
        ("builtin", layers.builtin_root),
    ]
    names = set().union(*(_names(root) for _, root in order))
    result: list[EffectiveSkill] = []
    for name in sorted(names):
        found = [(origin, _load(root, name)) for origin, root in order]
        present = [(o, v) for o, v in found if v is not None]
        if not present:
            continue
        origin, (d, desc, _content) = present[0]
        result.append(
            EffectiveSkill(
                name=name,
                origin=origin,
                description=desc,
                dir=d,
                shadowed=tuple(o for o, _ in present[1:]),
                has_builtin=any(o == "builtin" for o, _ in present),
                origin_updated=origin == "user" and _origin_updated(layers, name, d),
            )
        )
    return result


def get_skill(layers: SkillLayers, name: str) -> EffectiveSkill:
    validate_skill_name(name)
    for s in effective_skills(layers):
        if s.name == name:
            return s
    raise SkillNotFound(f"skill not found: {name}")


def _user_dir(layers: SkillLayers, name: str) -> Path:
    validate_skill_name(name)
    d = layers.user_root / name
    if not d.resolve().is_relative_to(layers.user_root.resolve()):
        raise InvalidSkillName(f"invalid skill name: {name!r}")
    return d


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def customise(layers: SkillLayers, name: str) -> Path:
    dest = _user_dir(layers, name)
    builtin = _load(layers.builtin_root, name)
    if builtin is None:
        raise SkillNotFound(f"no built-in skill named {name}")
    if dest.exists():
        raise SkillConflict(f"skill {name} is already customised")
    src_dir, _desc, content = builtin
    layers.user_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, dest, symlinks=False)
    _atomic_write(dest / SIDECAR, json.dumps({"builtinSha256": _sha(content), "builtinContent": content}))
    return dest


def write_user_skill(layers: SkillLayers, name: str, content: str) -> Path:
    dest = _user_dir(layers, name)
    if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
        raise InvalidSkill("SKILL.md exceeds 256 KiB")
    meta, _ = parse_front_matter(content)
    if meta.get("name") != name or not meta.get("description"):
        raise InvalidSkill("front matter must contain 'name: <skill name>' and a non-empty 'description'")
    dest.mkdir(parents=True, exist_ok=True)
    _atomic_write(dest / "SKILL.md", content)
    return dest / "SKILL.md"


def reset(layers: SkillLayers, name: str) -> None:
    d = _user_dir(layers, name)
    if d.is_symlink() or not d.is_dir():
        raise SkillNotFound(f"no user copy of {name}")
    shutil.rmtree(d)


def origin_diff(layers: SkillLayers, name: str) -> str | None:
    d = _user_dir(layers, name)
    builtin = _load(layers.builtin_root, name)
    if builtin is None or not _origin_updated(layers, name, d):
        return None
    old = json.loads((d / SIDECAR).read_text())["builtinContent"]
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            builtin[2].splitlines(keepends=True),
            fromfile=f"{name}/SKILL.md (original when customised)",
            tofile=f"{name}/SKILL.md (original now)",
        )
    )


def acknowledge_update(layers: SkillLayers, name: str) -> None:
    d = _user_dir(layers, name)
    builtin = _load(layers.builtin_root, name)
    if builtin is None or not (d / SIDECAR).is_file():
        raise SkillNotFound(f"{name} is not a customised built-in")
    _atomic_write(d / SIDECAR, json.dumps({"builtinSha256": _sha(builtin[2]), "builtinContent": builtin[2]}))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_skills_store.py -v`
Expected: all passed (13 parametrised name cases + 6).

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/skills/__init__.py backend/src/copperchat/skills/store.py backend/tests/unit/test_skills_store.py
git commit -m "feat: three-layer skill store with customise, reset and origin diff

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8.3: Built-in skill content, per-session assembly, SDK wiring

**Decision (skills transport).** The SDK offers three relevant knobs:
`setting_sources` (which settings/skill dirs the CLI discovers), `plugins`
(`SdkPluginConfig{type:"local", path}` → `--plugin-dir`) and `skills`
(context filter; adds `Skill(<name>)` to `allowed_tools`, and — only when
`setting_sources is None` — defaults `setting_sources` to `["user","project"]`,
see `subprocess_cli._apply_skills_defaults`). CopperChat generates **one local
plugin per session** at `/data/runtime/skills/<projectId>/` named `copperchat`
(`.claude-plugin/plugin.json` + `skills/<name>/SKILL.md`), passes it via
`plugins`, lists `skills=["copperchat:<name>", …]`, and sets
`setting_sources=[]` explicitly so neither `~/.claude` nor a `.claude/` folder
in the user's repository is loaded. The `Skill` tool is the only built-in tool
the main agent gets; it loads instructions, it cannot read files, run commands
or reach the web. A `.claude/skills` dir under a CopperChat-controlled cwd was
rejected because it requires `setting_sources=["project"]`, which also loads
`CLAUDE.md`/settings from that cwd and ties the agent's cwd to a generated
directory. If the `live_sdk` test below shows that plugin skills are not
discovered with `setting_sources=[]`, use that rejected alternative: cwd =
the generated dir, skills under `<dir>/.claude/skills/<name>/`,
`setting_sources=["project"]`, `skills=[<name>, …]` (unqualified), and record
it in the done report.

**Files:**
- Create: `backend/src/copperchat/skills/builtin/kicad-schematic-style/SKILL.md`
- Create: `backend/src/copperchat/skills/builtin/kicad-review-discipline/SKILL.md`
- Create: `backend/src/copperchat/skills/builtin/copperchat-tools/SKILL.md`
- Create: `backend/src/copperchat/skills/assembly.py`
- Modify: `backend/src/copperchat/agent/sdk_backend.py`
- Modify: `backend/pyproject.toml` (package data for `skills/builtin/**`, marker `live_sdk`)
- Test: `backend/tests/unit/test_skills_assembly.py`, `backend/tests/unit/test_builtin_skills.py`, `backend/tests/live/test_skills_live.py`

**Interfaces:**
- Consumes: `store.effective_skills`, `SkillLayers.for_project`, `skill_prefs.disabled_skills`.
- Produces (`copperchat.skills.assembly`):
  - `PLUGIN_NAME = "copperchat"`
  - `def sdk_skill_name(name: str) -> str` → `"copperchat:<name>"`
  - `@dataclass(frozen=True) class SkillBundle(plugin_dir: Path, names: list[str], sdk_names: list[str])`
  - `async def assemble_skills(settings: Settings, project: Project, db: Database) -> SkillBundle`
  - `def apply_skills(options: ClaudeAgentOptions, bundle: SkillBundle) -> ClaudeAgentOptions`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/unit/test_builtin_skills.py
from copperchat.skills.store import BUILTIN_ROOT, parse_front_matter

EXPECTED = {
    "kicad-schematic-style": ["left-to-right", "labels", "bus"],
    "kicad-review-discipline": ["decoupling", "pull-up", "run_erc", "render_view"],
    "copperchat-tools": ["mm", "full UUID", "region", "research_part"],
}


def test_builtin_skills_present_and_valid() -> None:
    for name, phrases in EXPECTED.items():
        text = (BUILTIN_ROOT / name / "SKILL.md").read_text()
        meta, body = parse_front_matter(text)
        assert meta["name"] == name
        assert len(meta["description"]) > 40
        for phrase in phrases:
            assert phrase.lower() in body.lower(), (name, phrase)
```

```python
# backend/tests/unit/test_skills_assembly.py
import json
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from pydantic import SecretStr

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos import skill_prefs
from copperchat.projects import Project
from copperchat.skills.assembly import apply_skills, assemble_skills, sdk_skill_name


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "projects" / "demo"
    d.mkdir(parents=True)
    return Project(id="0123456789ab", name="demo", rel_path="demo/demo.kicad_pro", dir=d,
                   pro_file=d / "demo.kicad_pro", sch_file=None, pcb_file=None)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(projects_root=tmp_path / "projects", data_dir=tmp_path / "data",
                    admin_password=SecretStr("pw"), dev_fake_agent=True)


@pytest.fixture
async def db(tmp_path: Path):
    d = await Database.open(tmp_path / "t.db")
    yield d
    await d.close()


async def test_bundle_contains_builtins_and_project_override(settings, project, db) -> None:
    skill = project.dir / ".kicad-agent" / "skills" / "copperchat-tools"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: copperchat-tools\ndescription: project override of tools\n---\nP\n")
    (skill / "secret-link").symlink_to("/etc/hostname")
    bundle = await assemble_skills(settings, project, db)
    assert bundle.plugin_dir == settings.data_dir / "runtime" / "skills" / project.id
    manifest = json.loads((bundle.plugin_dir / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "copperchat"
    assert set(bundle.names) >= {"kicad-schematic-style", "kicad-review-discipline", "copperchat-tools"}
    assert bundle.sdk_names == [sdk_skill_name(n) for n in bundle.names]
    copied = bundle.plugin_dir / "skills" / "copperchat-tools"
    assert "project override" in (copied / "SKILL.md").read_text()
    assert not (copied / "secret-link").exists()


async def test_disabled_skill_removed_and_stale_dirs_wiped(settings, project, db) -> None:
    first = await assemble_skills(settings, project, db)
    assert (first.plugin_dir / "skills" / "copperchat-tools").is_dir()
    await skill_prefs.set_skill_enabled(db, project.id, "copperchat-tools", False)
    second = await assemble_skills(settings, project, db)
    assert "copperchat-tools" not in second.names
    assert not (second.plugin_dir / "skills" / "copperchat-tools").exists()


async def test_apply_skills_keeps_main_agent_surface_minimal(settings, project, db) -> None:
    bundle = await assemble_skills(settings, project, db)
    opts = apply_skills(ClaudeAgentOptions(tools=[]), bundle)
    assert opts.tools == ["Skill"]
    assert opts.setting_sources == []
    assert opts.skills == bundle.sdk_names
    assert opts.plugins == [{"type": "local", "path": str(bundle.plugin_dir)}]
    assert opts.agents is None


async def test_no_enabled_skills_means_no_tools(settings, project, db) -> None:
    from copperchat.skills.store import BUILTIN_ROOT

    for d in BUILTIN_ROOT.iterdir():
        await skill_prefs.set_skill_enabled(db, project.id, d.name, False)
    bundle = await assemble_skills(settings, project, db)
    opts = apply_skills(ClaudeAgentOptions(tools=[]), bundle)
    assert opts.tools == [] and opts.skills == []
```

```python
# backend/tests/live/test_skills_live.py
"""Opt-in: verifies plugin-qualified skills are discovered with setting_sources=[]."""

import os
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions, SystemMessage, query

from copperchat.skills.assembly import SkillBundle, apply_skills

pytestmark = [
    pytest.mark.live_sdk,
    pytest.mark.skipif(
        not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")),
        reason="needs model credentials",
    ),
]


async def test_plugin_skill_listed_in_init(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "copperchat", "version": "1.0.0"}')
    (plugin / "skills" / "probe-skill").mkdir(parents=True)
    (plugin / "skills" / "probe-skill" / "SKILL.md").write_text(
        "---\nname: probe-skill\ndescription: Probe skill used by the CopperChat live test.\n---\nSay PROBE.\n"
    )
    bundle = SkillBundle(plugin_dir=plugin, names=["probe-skill"], sdk_names=["copperchat:probe-skill"])
    opts = apply_skills(ClaudeAgentOptions(tools=[], max_turns=1, cwd=str(tmp_path)), bundle)
    async for msg in query(prompt="Reply with OK.", options=opts):
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            assert "copperchat:probe-skill" in msg.data.get("skills", [])
            assert "Skill" in msg.data.get("tools", [])
            assert not any(t in msg.data.get("tools", []) for t in ("Read", "Bash", "WebFetch"))
            return
    pytest.fail("no init message")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/unit/test_builtin_skills.py tests/unit/test_skills_assembly.py -v`
Expected: FAIL — `FileNotFoundError` for the built-in SKILL.md and
`ModuleNotFoundError: No module named 'copperchat.skills.assembly'`.

- [ ] **Step 3: Write minimal implementation**

Built-in content (adapted from the OpenDriveHub `kicad-hardware` skill: its
validation gate, decoupling/pull-up red flags and library policy; KiCad-file
hand-editing rules and kicad-cli commands are replaced by CopperChat's tools).

````markdown
<!-- backend/src/copperchat/skills/builtin/kicad-schematic-style/SKILL.md -->
---
name: kicad-schematic-style
description: Schematic readability conventions by circuit type. Use when judging, explaining or proposing the layout of a KiCad schematic, or when the user asks whether a schematic is readable or well structured.
---

# Schematic style by circuit type

There is no single global rule. Pick the style per section of the schematic,
based on what the circuit does. When you review, name the section, the style
you expect and the concrete deviation (reference, sheet, position in mm).

## MCU, digital and bus-heavy sections → labels

- Connect MCU pins to their peripherals with **net labels**, not long wires.
  A pin stub plus a label with the same name is the expected pattern.
- Buses (SPI, I2C, UART, parallel data, JTAG/SWD) use consistent label names:
  `SPI1_SCK`, `SPI1_MOSI`, `I2C1_SDA`, `UART2_TX`. The name says the bus
  instance and the signal; the direction is clear from the MCU side.
- Group labels of one bus together at the MCU and at the peripheral.
- Use hierarchical sheets for blocks that repeat or that have a clean
  interface (power supply, each sensor, connectors).
- Wires that cross unrelated pins are a defect: KiCad connects by geometry,
  and a wire passing over a pin end creates a silent short.

## Analog signal paths → wired, left-to-right

- Draw the signal path **left-to-right**: input on the left, output on the
  right, in the order the signal passes the stages (filter → gain →
  buffer → ADC).
- Use **wires** inside the path so the topology is visible at a glance;
  labels inside an analog path hide feedback loops and are a smell.
- Feedback networks sit above or below their amplifier, close to it.
- Reference and bias networks stay near the stage they bias.

## Power

- Supplies flow top-to-bottom or left-to-right from the source (connector,
  battery, USB) through protection and regulation to the rails.
- Use power symbols for rails (`+3V3`, `+5V`, `GND`); never a net label for
  a supply that already has a power symbol.
- Each regulator shows its input and output capacitors next to it.
- Power flags (`PWR_FLAG`) sit at the source of each externally supplied net.

## General

- Decoupling capacitors are drawn next to the pin they decouple, one per
  supply pin, not collected in a corner.
- Reference designators and values must not overlap other text or wires.
- Unused pins carry a no-connect marker; floating inputs are not "unused".
- Title block, sheet names and label names are English and consistent.
````

````markdown
<!-- backend/src/copperchat/skills/builtin/kicad-review-discipline/SKILL.md -->
---
name: kicad-review-discipline
description: Electrical review checklist and validation discipline for KiCad designs. Use for any design review, any question about whether a circuit or board is correct, and before stating that something is fine or fixed.
---

# Review discipline

## Prime directive: verify before you claim

Never state that a schematic or board is correct, clean or fixed without
evidence from this turn:

1. `run_erc` on the schematic — read every violation. Zero violations, or
   each one explained (what it means, why it is acceptable or how to fix it).
2. `run_drc` on the board, if a board exists — same standard.
3. `render_view` of the area you are talking about — look at the image and
   describe what you actually see. "It looks correct" without a render in
   this turn is not acceptable.

Warnings are not decoration: either explain why one is harmless or report it
as a finding. If a tool fails, say so; do not guess the result.

## Checklist

- **Decoupling:** 100 nF ceramic per IC supply pin, placed at the pin, plus
  a bulk capacitor (1–10 µF) per IC or per rail segment. On the board, the
  capacitor sits next to the pin with the shortest loop to ground.
- **Regulators:** input and output capacitors per datasheet values and
  types (some LDOs need specific ESR); feedback dividers checked against the
  datasheet formula.
- **Pull-ups / pull-downs:** I2C needs one pull-up pair per bus (typically
  4.7 kΩ to the bus voltage), not one per device. Open-drain outputs, reset
  and enable pins, boot-strap pins need a defined level.
- **Floating inputs:** unused logic inputs tied high or low; unused op-amp
  sections configured as followers to a defined voltage.
- **Power flags:** every externally driven supply net has a `PWR_FLAG` at its
  source, otherwise ERC reports "input power pin not driven".
- **Crystals:** load capacitors computed from the crystal's load capacitance
  and stray capacitance.
- **Connectors:** pin 1 orientation, current rating, ESD protection on
  externally exposed lines.
- **Footprints:** every symbol has a footprint; IC pad count and pin mapping
  checked against the datasheet, not only the symbol.

## Library policy

1. KiCad's bundled libraries first.
2. Manufacturer-provided KiCad libraries next.
3. Third-party libraries (SnapEDA, Ultra Librarian) into a project-local
   library, never into the global tables.
4. Hand-made symbols/footprints only as the last resort, checked against the
   datasheet dimension by dimension.

Project-local libraries live next to the `.kicad_pro` and are registered in
the project's own library tables so the project stays self-contained.

## Unknown parts

When a part's wiring or layout rules matter and you do not know them for
certain, call `research_part` with the manufacturer part number instead of
answering from memory.
````

````markdown
<!-- backend/src/copperchat/skills/builtin/copperchat-tools/SKILL.md -->
---
name: copperchat-tools
description: How to use CopperChat's KiCad inspection tools correctly (units, identifiers, renders, part research). Use whenever you inspect the schematic or board, run ERC/DRC or render a view.
---

# Using the CopperChat tools

You see the design only through these tools. They read live data from the
running KiCad API server; there is no file access, shell or web for you.

## Units and identifiers

- All coordinates and sizes are in **mm**. Quote positions as
  `(x, y) mm` and never convert to mils unless the user asks.
- Items are identified by their **full UUID** as returned by the tools. Never
  shorten a UUID; copy it exactly when you refer to an item.
- References (`R12`, `U3`) are for talking to the user; UUIDs are for exact
  identification between tool calls.

## Querying

- Start with `project_info` to learn files, sheets, board size and layers.
- Use the `filter` arguments of `sch_list_symbols` / `pcb_list_footprints`
  instead of listing everything; there is no pagination.
- `sch_get_net` / `pcb_get_net` answer "what is connected to X".

## Seeing the design

- `render_view(doc, mode="agent")` draws a mm grid, references, stable net
  colours and ERC/DRC markers — use it to check what you claim.
- Dense boards: pass a **region** (`around_ref` with a margin, or an
  `x_mm/y_mm/w_mm/h_mm` box) instead of rendering the whole board; small
  parts are unreadable in a full-board render.
- `mode="kicad"` shows KiCad's own look when the user wants to see that.
- Describe what you see in the image; do not infer it from tool data alone.

## Errors

Tool errors come as `{what, why, suggestion}`. Follow the suggestion once;
if it fails again, report the error to the user instead of retrying in a loop.

## Part research

For datasheet questions (pinout, reference circuit, layout rules) call
`research_part` with the manufacturer part number and the question. Attached
PDFs appear as a path under `docs/parts/_uploads/`; pass that path in
`attachment_paths` so the researcher reads it. The researcher stores notes in
`docs/parts/<MPN>/notes.md`; later questions reuse them.

## Git

Use `git_commit` only when the user explicitly asks for a commit.
````

```python
# backend/src/copperchat/skills/assembly.py
"""Assemble the effective skill set of a project into a per-session SDK plugin."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.skill_prefs import disabled_skills
from copperchat.projects import Project
from copperchat.skills.store import MAX_SKILL_BYTES, SkillLayers, effective_skills

PLUGIN_NAME = "copperchat"


def sdk_skill_name(name: str) -> str:
    return f"{PLUGIN_NAME}:{name}"


@dataclass(frozen=True)
class SkillBundle:
    plugin_dir: Path
    names: list[str]
    sdk_names: list[str]


def _copy_regular_files(src: Path, dest: Path) -> None:
    """Copy regular, non-hidden files only; symlinks and dotfiles never leave their layer."""
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part.startswith(".") for part in rel.parts) or path.is_symlink():
            continue
        if path.is_file() and path.stat().st_size <= MAX_SKILL_BYTES:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


async def assemble_skills(settings: Settings, project: Project, db: Database) -> SkillBundle:
    plugin_dir = settings.data_dir / "runtime" / "skills" / project.id
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    (plugin_dir / ".claude-plugin").mkdir(parents=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": PLUGIN_NAME, "version": "1.0.0", "description": "CopperChat effective skills"})
    )
    disabled = await disabled_skills(db, project.id)
    names: list[str] = []
    for skill in effective_skills(SkillLayers.for_project(settings, project)):
        if skill.name in disabled:
            continue
        _copy_regular_files(skill.dir, plugin_dir / "skills" / skill.name)
        names.append(skill.name)
    return SkillBundle(plugin_dir=plugin_dir, names=names, sdk_names=[sdk_skill_name(n) for n in names])


def apply_skills(options: ClaudeAgentOptions, bundle: SkillBundle) -> ClaudeAgentOptions:
    return replace(
        options,
        tools=["Skill"] if bundle.sdk_names else [],
        plugins=[{"type": "local", "path": str(bundle.plugin_dir)}],
        skills=list(bundle.sdk_names),
        setting_sources=[],
    )
```

`tools=["Skill"]` for the main agent (only when a skill is enabled) is an
**owner review requested** decision: `Skill` is not a file/shell/web tool.

Modify `sdk_backend.py`: add a keyword parameter `skills: SkillBundle | None = None`
to Phase 5's (synchronous) `build_options(...)`; at its end, after `options`
was constructed with `tools=[]`, the MCP server, `allowed_tools` and
`can_use_tool`:

```python
from copperchat.skills.assembly import SkillBundle, apply_skills, assemble_skills

    if skills is not None:
        options = apply_skills(options, skills)
    return options
```

The backend's `start()` is an async generator, so it assembles the bundle
before each `build_options(...)` call site (`SdkStreamingBackend`, and
`SdkPerTurnBackend` if 1A chose it) and passes it in:

```python
        skills = await assemble_skills(self._settings, project, self._db)
        options = build_options(..., skills=skills)   # other arguments unchanged
```

The backend gets the `Database` through `default_backend_factory(settings, specs, db)`
(add the `db` parameter; `web/app.py` passes `app.state.db`). The guard built by
`make_tool_guard(frozenset(allowed))` inside `build_options` must additionally
contain `"Skill"` when `skills` enables any skill, so a `Skill` call is
evaluated by the allowlist rather than denied as unknown.

`backend/pyproject.toml`: add to `[tool.pytest.ini_options].markers`:
`"live_sdk: needs real model credentials, opt-in"` (registration only; do not
change `addopts` — the test's `skipif` on a missing token keeps it out of CI). Ensure
`skills/builtin/**/*.md` is shipped: with hatchling add
`[tool.hatch.build.targets.wheel] artifacts = ["src/copperchat/skills/builtin/**"]`
(uv installs the project editable in dev and as wheel in the image).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_builtin_skills.py tests/unit/test_skills_assembly.py -v && uv run pytest -m "not kicad" -q`
Expected: new tests pass; Phase 5 adapter tests still pass.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/skills backend/src/copperchat/agent/sdk_backend.py backend/pyproject.toml backend/tests/unit/test_builtin_skills.py backend/tests/unit/test_skills_assembly.py backend/tests/live/test_skills_live.py
git commit -m "feat: built-in skills and per-session skill plugin for the SDK

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8.4: part-researcher definition and path guard

**Decision (how the main agent reaches the researcher) — owner review requested.** `research_part` is
a normal CopperChat `ToolSpec` whose handler runs a **separate one-shot SDK
`query()`** configured from the `part-researcher` `AgentDefinition`
(prompt, tools, model, max turns). The alternative — registering
`agents={"part-researcher": …}` on the main session and letting the model call
the SDK `Agent`/`Task` tool — was rejected: (a) the main session has
`tools=["Skill"]`, and sub-agents can only use tools available to the session,
so WebSearch/WebFetch/Read/Write/Edit would have to be enabled for the main
session too and then filtered per call; (b) it would add the `Agent` tool to
the main agent's surface. With a separate query the main agent's surface stays
`Skill` + CopperChat MCP tools, and the researcher's `can_use_tool` guard
applies to every call of that query, so no main/sub-agent distinction is
needed. (`ToolPermissionContext.agent_id` exists and is `None` for the main
agent, but it identifies an instance, not a definition name, so it would only
work while exactly one sub-agent type exists.)

**Files:**
- Create: `backend/src/copperchat/research/__init__.py` (empty), `research/paths.py`, `research/agent.py`
- Create: `backend/src/copperchat/agent/guards.py`
- Test: `backend/tests/unit/test_researcher_guard.py`

**Interfaces:**
- Consumes: `agent_dir(project)` (C3), SDK `PermissionResultAllow`, `PermissionResultDeny`, `ToolPermissionContext`, `AgentDefinition`.
- Produces:
  - `copperchat.research.paths`: `parts_root(project) -> Path` (`<dir>/docs/parts`), `uploads_pdf_root(project) -> Path` (`docs/parts/_uploads`), `uploads_image_root(project) -> Path` (`.kicad-agent/uploads`), `def resolve_inside(raw: str, base: Path, roots: Sequence[Path]) -> Path | None`
  - `copperchat.research.agent`: `RESEARCHER_TOOLS: list[str]`, `PART_RESEARCHER: AgentDefinition`, `DOWNLOAD_TOOL = "mcp__parts__download_datasheet"`
  - `copperchat.agent.guards.make_researcher_guard(project: Project) -> CanUseTool`
  - `copperchat.parts.notes_user_edited(path: Path) -> bool` (created here, extended in 8.5)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_researcher_guard.py
from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from copperchat.agent.guards import make_researcher_guard
from copperchat.projects import Project
from copperchat.research.agent import PART_RESEARCHER, RESEARCHER_TOOLS


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "proj"
    (d / "docs" / "parts" / "TPS62133").mkdir(parents=True)
    (d / "docs" / "parts" / "_uploads").mkdir()
    (d / ".kicad-agent" / "uploads").mkdir(parents=True)
    return Project(id="0123456789ab", name="proj", rel_path="proj/proj.kicad_pro", dir=d,
                   pro_file=d / "proj.kicad_pro", sch_file=None, pcb_file=None)


async def decide(project: Project, tool: str, inp: dict) -> bool:
    guard = make_researcher_guard(project)
    res = await guard(tool, inp, ToolPermissionContext(tool_use_id="t1"))
    assert isinstance(res, PermissionResultAllow | PermissionResultDeny)
    return isinstance(res, PermissionResultAllow)


def test_definition_tools() -> None:
    assert RESEARCHER_TOOLS == ["WebSearch", "WebFetch", "Read", "Write", "Edit", "Glob"]
    assert PART_RESEARCHER.tools == RESEARCHER_TOOLS + ["mcp__parts__download_datasheet"]
    assert "docs/parts/<MPN>/notes.md" in PART_RESEARCHER.prompt


async def test_allowed_paths(project: Project) -> None:
    notes = project.dir / "docs/parts/TPS62133/notes.md"
    assert await decide(project, "Write", {"file_path": str(notes), "content": "x"})
    assert await decide(project, "Edit", {"file_path": "docs/parts/NEW/notes.md", "old_string": "a", "new_string": "b"})
    assert await decide(project, "Read", {"file_path": str(project.dir / "docs/parts/_uploads/a.pdf")})
    assert await decide(project, "Read", {"file_path": str(project.dir / ".kicad-agent/uploads/x.png")})
    assert await decide(project, "Glob", {"pattern": "**/*", "path": str(project.dir / "docs/parts")})
    assert await decide(project, "WebSearch", {"query": "TPS62133 datasheet"})
    assert await decide(project, "WebFetch", {"url": "https://www.ti.com/x", "prompt": "p"})
    assert await decide(project, "mcp__parts__download_datasheet", {"mpn": "TPS62133", "url": "https://x"})


@pytest.mark.parametrize(
    ("tool", "inp"),
    [
        ("Write", {"file_path": "docs/parts/../../evil.md", "content": "x"}),
        ("Write", {"file_path": "/etc/passwd", "content": "x"}),
        ("Write", {"file_path": "proj.kicad_sch", "content": "x"}),
        ("Write", {"file_path": "docs/parts/_uploads/x.pdf", "content": "x"}),
        ("Edit", {"file_path": ".kicad-agent/uploads/x.png", "old_string": "a", "new_string": "b"}),
        ("Read", {"file_path": "/data/vapid.json"}),
        ("Read", {"file_path": "proj.kicad_pcb"}),
        ("Read", {"file_path": ""}),
        ("Glob", {"pattern": "**/*"}),
        ("Bash", {"command": "ls"}),
        ("NotebookEdit", {"notebook_path": "docs/parts/a.ipynb"}),
        ("Agent", {"prompt": "x"}),
    ],
)
async def test_denied(project: Project, tool: str, inp: dict) -> None:
    assert not await decide(project, tool, inp)


async def test_symlink_escape_denied(project: Project, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (project.dir / "docs/parts/LINK").symlink_to(outside, target_is_directory=True)
    assert not await decide(project, "Write", {"file_path": "docs/parts/LINK/notes.md", "content": "x"})


async def test_parts_root_symlinked_outside_denied(tmp_path: Path) -> None:
    d = tmp_path / "p2"
    (d / "docs").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (d / "docs" / "parts").symlink_to(outside, target_is_directory=True)
    project = Project(id="ba9876543210", name="p2", rel_path="p2/p2.kicad_pro", dir=d,
                      pro_file=d / "p2.kicad_pro", sch_file=None, pcb_file=None)
    assert not await decide(project, "Write", {"file_path": "docs/parts/X/notes.md", "content": "x"})


async def test_user_edited_notes_are_protected(project: Project) -> None:
    notes = project.dir / "docs/parts/TPS62133/notes.md"
    notes.write_text("---\nuser_edited: true\n---\nMine\n")
    assert not await decide(project, "Write", {"file_path": str(notes), "content": "x"})
    assert not await decide(project, "Edit", {"file_path": str(notes), "old_string": "Mine", "new_string": "x"})
    assert await decide(project, "Read", {"file_path": str(notes)})
    notes.write_text("---\nuser_edited: false\n---\nResearcher\n")
    assert await decide(project, "Write", {"file_path": str(notes), "content": "x"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_researcher_guard.py -v`
Expected: FAIL with `ImportError: cannot import name 'make_researcher_guard'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/research/paths.py
from collections.abc import Sequence
from pathlib import Path

from copperchat.projects import Project, agent_dir


def parts_root(project: Project) -> Path:
    return project.dir / "docs" / "parts"


def uploads_pdf_root(project: Project) -> Path:
    return parts_root(project) / "_uploads"


def uploads_image_root(project: Project) -> Path:
    return agent_dir(project) / "uploads"


def resolve_inside(raw: str, base: Path, roots: Sequence[Path]) -> Path | None:
    """Resolve raw (absolute or relative to base) and return it if it lies inside one of roots.

    Rejects empty paths, NUL bytes and any '..' component before resolving; resolves symlinks
    of both the candidate and the roots, and requires every root to resolve inside base.
    """
    if not raw or "\x00" in raw:
        return None
    candidate = Path(raw)
    if ".." in candidate.parts:
        return None
    if not candidate.is_absolute():
        candidate = base / candidate
    real = candidate.resolve(strict=False)
    real_base = base.resolve(strict=False)
    for root in roots:
        real_root = root.resolve(strict=False)
        if not real_root.is_relative_to(real_base):
            continue
        if real == real_root or real.is_relative_to(real_root):
            return real
    return None
```

```python
# backend/src/copperchat/parts.py  (first part; Task 8.5 extends this module)
from pathlib import Path

from copperchat.skills.store import parse_front_matter


def notes_user_edited(path: Path) -> bool:
    if not path.is_file():
        return False
    meta, _ = parse_front_matter(path.read_text(encoding="utf-8", errors="replace"))
    return meta.get("user_edited", "").lower() == "true"
```

```python
# backend/src/copperchat/research/agent.py
from claude_agent_sdk import AgentDefinition

RESEARCHER_TOOLS = ["WebSearch", "WebFetch", "Read", "Write", "Edit", "Glob"]
DOWNLOAD_TOOL = "mcp__parts__download_datasheet"

RESEARCHER_PROMPT = """\
You are part-researcher, a sub-agent of CopperChat. You answer one question about one
electronic part for the main design agent and keep project-local notes about it.

Working directory: the KiCad project. You may only read docs/parts/ and .kicad-agent/uploads/,
and only write inside docs/parts/<MPN>/. Other paths are denied.

Steps:
1. Determine the manufacturer part number (MPN) from the question. Folder name rule:
   letters, digits, '.', '_', '+', '-' only; replace '/' and spaces with '_'.
2. Look in docs/parts/<MPN>/ (Glob with path="docs/parts") for datasheet.pdf and notes.md.
   If notes.md answers the question, answer from it (and datasheet.pdf if needed).
3. Otherwise search the web for the official datasheet (manufacturer site preferred), call
   download_datasheet(mpn, url) to store it as docs/parts/<MPN>/datasheet.pdf, read it, and
   write docs/parts/<MPN>/notes.md with these sections: Summary, Pinout, Reference circuit
   (component values), Layout recommendations, Pitfalls, Sources (URLs with access date).
   Start notes.md with front matter:
   ---
   mpn: <MPN>
   user_edited: false
   ---
4. If notes.md has `user_edited: true` in its front matter, the user owns it: never modify it.
   Put any new findings only into your answer.
5. If the question names attached PDFs (paths under docs/parts/_uploads/), read them first.

Answer: at most 250 words, concrete values (mm, ohms, farads, volts), the path of notes.md,
and which source each key statement comes from. No speculation; say what you could not verify.
"""

PART_RESEARCHER = AgentDefinition(
    description="Researches an electronic part (datasheet, pinout, reference circuit, layout) "
    "and maintains docs/parts/<MPN>/notes.md.",
    prompt=RESEARCHER_PROMPT,
    tools=[*RESEARCHER_TOOLS, DOWNLOAD_TOOL],
    model="inherit",
    maxTurns=40,
)
```

The prompt contains the literal `docs/parts/<MPN>/notes.md` (test assertion).

Create `backend/src/copperchat/agent/guards.py` (Phase 5 keeps its
`make_tool_guard` in `sdk_backend.py`, unchanged):

```python
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext
from claude_agent_sdk.types import CanUseTool, PermissionResult

from copperchat.parts import notes_user_edited
from copperchat.projects import Project
from copperchat.research.agent import DOWNLOAD_TOOL
from copperchat.research.paths import (
    parts_root,
    resolve_inside,
    uploads_image_root,
    uploads_pdf_root,
)

_PATH_KEY = {"Read": "file_path", "Write": "file_path", "Edit": "file_path", "Glob": "path"}


def make_researcher_guard(project: Project) -> CanUseTool:
    """Permission callback for the part-researcher query (every call of that query)."""
    base = project.dir
    read_roots = [parts_root(project), uploads_image_root(project)]
    write_roots = [parts_root(project)]
    pdf_uploads = uploads_pdf_root(project)

    def deny(msg: str) -> PermissionResult:
        return PermissionResultDeny(message=msg)

    async def guard(tool: str, inp: dict[str, Any], _ctx: ToolPermissionContext) -> PermissionResult:
        if tool in ("WebSearch", "WebFetch", DOWNLOAD_TOOL):
            return PermissionResultAllow()
        key = _PATH_KEY.get(tool)
        if key is None:
            return deny(f"{tool} is not available to part-researcher")
        raw = str(inp.get(key) or "")
        if tool in ("Read", "Glob"):
            if resolve_inside(raw, base, read_roots) is None:
                return deny("Read access is limited to docs/parts/ and .kicad-agent/uploads/")
            return PermissionResultAllow()
        target = resolve_inside(raw, base, write_roots)
        if target is None:
            return deny("Writes are limited to docs/parts/<MPN>/")
        real_uploads = pdf_uploads.resolve(strict=False)
        if target == real_uploads or target.is_relative_to(real_uploads):
            return deny("docs/parts/_uploads/ holds user uploads and is read-only")
        if target.name == "notes.md" and notes_user_edited(target):
            return deny("notes.md is user-edited (user_edited: true); do not modify it")
        return PermissionResultAllow()

    return guard
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_researcher_guard.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/research/__init__.py backend/src/copperchat/research/paths.py backend/src/copperchat/research/agent.py backend/src/copperchat/parts.py backend/src/copperchat/agent/guards.py backend/tests/unit/test_researcher_guard.py
git commit -m "feat: part-researcher definition and path guard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8.5: Parts storage (MPN sanitising) and upload storage (sniffing, filenames)

**Files:**
- Modify: `backend/src/copperchat/parts.py`
- Create: `backend/src/copperchat/uploads.py`
- Test: `backend/tests/unit/test_parts_storage.py`, `backend/tests/unit/test_uploads_storage.py`

**Interfaces:**
- Produces (`copperchat.parts`):
  - `class InvalidMpn(CopperChatError)`
  - `def mpn_dirname(mpn: str) -> str` — strip, `/`, `\` and whitespace → `_`, then require `^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$` and no `..`
  - `@dataclass(frozen=True) class PartInfo(mpn: str, has_notes: bool, has_datasheet: bool, user_edited: bool, updated_at: str | None)`
  - `def list_parts(project) -> list[PartInfo]` (skips `_uploads`, symlinks, invalid names)
  - `def read_notes(project, mpn) -> str` (`""` if absent), `def write_user_notes(project, mpn, text) -> None` (forces `user_edited: true`)
  - `def datasheet_path(project, mpn) -> Path | None`
- Produces (`copperchat.uploads`):
  - `class UploadRejected(CopperChatError)` with `.status: int` (400/413/415)
  - `MAX_IMAGE_BYTES = 15 MiB`, `MAX_PDF_BYTES = 20 MiB`, `MAX_IMAGE_EDGE = 2048`
  - `def sniff(head: bytes) -> tuple[Literal["image","pdf"], str] | None` (kind, mime)
  - `def safe_filename(name: str | None, default: str = "upload") -> str`
  - `@dataclass(frozen=True) class StoredUpload(id, kind, name, rel_path, mime, size, abs_path: Path)`
  - `def store_upload(project, data: bytes, filename: str | None) -> StoredUpload`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/unit/test_parts_storage.py
from pathlib import Path

import pytest

from copperchat import parts
from copperchat.projects import Project


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "proj"
    d.mkdir()
    return Project(id="0123456789ab", name="proj", rel_path="proj/proj.kicad_pro", dir=d,
                   pro_file=d / "proj.kicad_pro", sch_file=None, pcb_file=None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("TPS62133", "TPS62133"), (" LM358/NOPB ", "LM358_NOPB"), ("ESP32-S3-WROOM-1 N8R8", "ESP32-S3-WROOM-1_N8R8"),
     ("BAT54C.215", "BAT54C.215")],
)
def test_mpn_dirname_ok(raw: str, expected: str) -> None:
    assert parts.mpn_dirname(raw) == expected


@pytest.mark.parametrize("bad", ["", "..", "../etc", "a/../b", ".hidden", "_uploads", "x" * 65, "a\x00b", "ä-part", "a;rm"])
def test_mpn_dirname_rejects(bad: str) -> None:
    with pytest.raises(parts.InvalidMpn):
        parts.mpn_dirname(bad)


def test_user_notes_roundtrip_sets_flag(project: Project) -> None:
    parts.write_user_notes(project, "TPS62133", "# Notes\nmine\n")
    text = parts.read_notes(project, "TPS62133")
    assert text.startswith("---\n") and "user_edited: true" in text and "mine" in text
    parts.write_user_notes(project, "TPS62133", "---\nmpn: TPS62133\nuser_edited: false\n---\nedited\n")
    text = parts.read_notes(project, "TPS62133")
    assert "user_edited: true" in text and "user_edited: false" not in text and "mpn: TPS62133" in text


def test_list_parts_skips_uploads_and_symlinks(project: Project, tmp_path: Path) -> None:
    root = project.dir / "docs" / "parts"
    (root / "_uploads").mkdir(parents=True)
    (root / "LM358").mkdir()
    (root / "LM358" / "datasheet.pdf").write_bytes(b"%PDF-1.7\n")
    (root / "evil").symlink_to(tmp_path, target_is_directory=True)
    infos = parts.list_parts(project)
    assert [p.mpn for p in infos] == ["LM358"]
    assert infos[0].has_datasheet and not infos[0].has_notes
    assert parts.datasheet_path(project, "LM358") == root / "LM358" / "datasheet.pdf"
    assert parts.datasheet_path(project, "evil") is None
```

```python
# backend/tests/unit/test_uploads_storage.py
import io
from pathlib import Path

import pytest
from PIL import Image

from copperchat import uploads
from copperchat.projects import Project


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "proj"
    d.mkdir()
    return Project(id="0123456789ab", name="proj", rel_path="proj/proj.kicad_pro", dir=d,
                   pro_file=d / "proj.kicad_pro", sch_file=None, pcb_file=None)


def png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 50, 50)).save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("../../etc/passwd", "passwd"), ("C:\\Users\\x\\board photo.JPG", "board_photo.JPG"),
     ("..", "upload"), (".bashrc", "bashrc"), (None, "upload"), ("a\x00b.pdf", "a_b.pdf"),
     ("x" * 300 + ".pdf", "x" * 96 + ".pdf")],
)
def test_safe_filename(raw: str | None, expected: str) -> None:
    assert uploads.safe_filename(raw) == expected


def test_sniff() -> None:
    assert uploads.sniff(b"%PDF-1.7\n") == ("pdf", "application/pdf")
    assert uploads.sniff(png(1, 1)[:16]) == ("image", "image/png")
    assert uploads.sniff(b"\xff\xd8\xff\xe0abc") == ("image", "image/jpeg")
    assert uploads.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ("image", "image/webp")
    assert uploads.sniff(b"GIF89a....") == ("image", "image/gif")
    assert uploads.sniff(b"<html>") is None
    assert uploads.sniff(b"\x00\x00\x00\x18ftypheic") is None


def test_pdf_stored_under_parts_uploads(project: Project) -> None:
    up = uploads.store_upload(project, b"%PDF-1.7\nbody", "../TPS62133 datasheet.pdf")
    assert up.kind == "pdf" and up.name == "TPS62133_datasheet.pdf"
    assert up.rel_path == f"docs/parts/_uploads/{up.id[:8]}-TPS62133_datasheet.pdf"
    assert (project.dir / up.rel_path).read_bytes() == b"%PDF-1.7\nbody"


def test_image_reencoded_and_downscaled(project: Project) -> None:
    up = uploads.store_upload(project, png(4000, 1000), "photo.png")
    assert up.kind == "image" and up.rel_path.startswith(".kicad-agent/uploads/")
    with Image.open(project.dir / up.rel_path) as im:
        assert max(im.size) == uploads.MAX_IMAGE_EDGE


def test_rejections(project: Project) -> None:
    with pytest.raises(uploads.UploadRejected) as e:
        uploads.store_upload(project, b"#!/bin/sh\n", "x.pdf")
    assert e.value.status == 415
    with pytest.raises(uploads.UploadRejected) as e:
        uploads.store_upload(project, b"%PDF-" + b"0" * uploads.MAX_PDF_BYTES, "big.pdf")
    assert e.value.status == 413
    with pytest.raises(uploads.UploadRejected) as e:
        uploads.store_upload(project, b"\x89PNG\r\n\x1a\nbroken", "x.png")
    assert e.value.status == 415


def test_parts_dir_symlinked_outside_rejected(project: Project, tmp_path: Path) -> None:
    (project.dir / "docs").mkdir()
    (project.dir / "docs" / "parts").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(uploads.UploadRejected) as e:
        uploads.store_upload(project, b"%PDF-1.7\n", "a.pdf")
    assert e.value.status == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/unit/test_parts_storage.py tests/unit/test_uploads_storage.py -v`
Expected: FAIL with `AttributeError: module 'copperchat.parts' has no attribute 'mpn_dirname'` and `ModuleNotFoundError: No module named 'copperchat.uploads'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/parts.py  (full file after this task)
"""docs/parts/<MPN>/ storage shared by the Parts page and part-researcher."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from copperchat.errors import CopperChatError
from copperchat.projects import Project
from copperchat.skills.store import parse_front_matter

_MPN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_SEP_RE = re.compile(r"[\s/\\]+")


class InvalidMpn(CopperChatError):
    pass


def mpn_dirname(mpn: str) -> str:
    name = _SEP_RE.sub("_", mpn.strip())
    if not _MPN_RE.fullmatch(name) or ".." in name:
        raise InvalidMpn(f"invalid part number: {mpn!r}")
    return name


def _root(project: Project) -> Path:
    return project.dir / "docs" / "parts"


def _part_dir(project: Project, mpn: str) -> Path:
    d = _root(project) / mpn_dirname(mpn)
    real_root = _root(project).resolve(strict=False)
    if not real_root.is_relative_to(project.dir.resolve()) or not d.resolve(strict=False).is_relative_to(real_root):
        raise InvalidMpn(f"part folder escapes docs/parts: {mpn!r}")
    if d.is_symlink():
        raise InvalidMpn(f"part folder is a symlink: {mpn!r}")
    return d


def notes_user_edited(path: Path) -> bool:
    if not path.is_file():
        return False
    meta, _ = parse_front_matter(path.read_text(encoding="utf-8", errors="replace"))
    return meta.get("user_edited", "").lower() == "true"


@dataclass(frozen=True)
class PartInfo:
    mpn: str
    has_notes: bool
    has_datasheet: bool
    user_edited: bool
    updated_at: str | None


def list_parts(project: Project) -> list[PartInfo]:
    root = _root(project)
    if not root.is_dir() or not root.resolve().is_relative_to(project.dir.resolve()):
        return []
    result: list[PartInfo] = []
    for d in sorted(root.iterdir()):
        if d.is_symlink() or not d.is_dir() or d.name.startswith("_") or not _MPN_RE.fullmatch(d.name):
            continue
        notes, sheet = d / "notes.md", d / "datasheet.pdf"
        mtimes = [p.stat().st_mtime for p in (notes, sheet) if p.is_file()]
        result.append(
            PartInfo(
                mpn=d.name,
                has_notes=notes.is_file(),
                has_datasheet=sheet.is_file() and not sheet.is_symlink(),
                user_edited=notes_user_edited(notes),
                updated_at=datetime.fromtimestamp(max(mtimes), UTC).isoformat() if mtimes else None,
            )
        )
    return result


def read_notes(project: Project, mpn: str) -> str:
    notes = _part_dir(project, mpn) / "notes.md"
    if notes.is_symlink() or not notes.is_file():
        return ""
    return notes.read_text(encoding="utf-8", errors="replace")


def write_user_notes(project: Project, mpn: str, text: str) -> None:
    meta, body = parse_front_matter(text)
    meta.pop("user_edited", None)
    meta.setdefault("mpn", mpn_dirname(mpn))
    header = "".join(f"{k}: {v}\n" for k, v in meta.items())
    content = f"---\n{header}user_edited: true\n---\n{body.lstrip(chr(10))}"
    d = _part_dir(project, mpn)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".notes.md.tmp"
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, d / "notes.md")


def datasheet_path(project: Project, mpn: str) -> Path | None:
    try:
        p = _part_dir(project, mpn) / "datasheet.pdf"
    except InvalidMpn:
        return None
    return p if p.is_file() and not p.is_symlink() else None
```

Note: `_uploads` fails `_MPN_RE` (leading `_`), so `mpn_dirname("_uploads")`
raises — the upload folder can never be addressed as a part.

```python
# backend/src/copperchat/uploads.py
"""Chat attachments: images → .kicad-agent/uploads/, PDFs → docs/parts/_uploads/."""

from __future__ import annotations

import io
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps, UnidentifiedImageError

from copperchat.errors import CopperChatError
from copperchat.projects import Project
from copperchat.research.paths import uploads_image_root, uploads_pdf_root

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_IMAGE_EDGE = 2048
MAX_IMAGE_PIXELS = 50_000_000
Kind = Literal["image", "pdf"]
_UNSAFE = re.compile(r"[^A-Za-z0-9._+-]+")


class UploadRejected(CopperChatError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def sniff(head: bytes) -> tuple[Kind, str] | None:
    if head.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    return None


def safe_filename(name: str | None, default: str = "upload") -> str:
    base = re.split(r"[/\\]", name or "")[-1]
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = _UNSAFE.sub("_", base.replace("\x00", "_")).strip("._-")
    if not base:
        return default
    stem, dot, ext = base.rpartition(".")
    if dot and stem and len(ext) <= 8:
        return f"{stem[:96]}.{ext}"
    return base[:100]


@dataclass(frozen=True)
class StoredUpload:
    id: str
    kind: Kind
    name: str
    rel_path: str
    mime: str
    size: int
    abs_path: Path


def _checked_dir(project: Project, d: Path) -> Path:
    if not d.resolve(strict=False).is_relative_to(project.dir.resolve()):
        raise UploadRejected("upload directory resolves outside the project", 400)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _normalise_image(data: bytes) -> tuple[bytes, str, str]:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.width * probe.height > MAX_IMAGE_PIXELS:
                raise UploadRejected("image has too many pixels", 413)
            probe.verify()
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
            out = io.BytesIO()
            if im.mode in ("RGBA", "LA", "P"):
                im.convert("RGBA").save(out, "PNG", optimize=True)
                return out.getvalue(), "image/png", "png"
            im.convert("RGB").save(out, "JPEG", quality=90)
            return out.getvalue(), "image/jpeg", "jpg"
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise UploadRejected(f"image could not be decoded: {exc}", 415) from exc


def store_upload(project: Project, data: bytes, filename: str | None) -> StoredUpload:
    detected = sniff(data[:16])
    if detected is None:
        raise UploadRejected("only PNG, JPEG, WebP, GIF images and PDF files are accepted", 415)
    kind, mime = detected
    limit = MAX_PDF_BYTES if kind == "pdf" else MAX_IMAGE_BYTES
    if len(data) > limit:
        raise UploadRejected(f"{kind} exceeds {limit // (1024 * 1024)} MB", 413)
    upload_id = uuid.uuid4().hex
    if kind == "pdf":
        name = safe_filename(filename, "document.pdf")
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        d = _checked_dir(project, uploads_pdf_root(project))
        path = d / f"{upload_id[:8]}-{name}"
        payload = data
    else:
        payload, mime, ext = _normalise_image(data)
        name = safe_filename(filename, f"image.{ext}")
        d = _checked_dir(project, uploads_image_root(project))
        path = d / f"{upload_id}.{ext}"
    _write(path, payload)
    return StoredUpload(
        id=upload_id, kind=kind, name=name, rel_path=path.relative_to(project.dir).as_posix(),
        mime=mime, size=len(payload), abs_path=path,
    )
```

Also replace the guard-local import in `agent/guards.py`: it already imports
`notes_user_edited` from `copperchat.parts` (unchanged signature).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_parts_storage.py tests/unit/test_uploads_storage.py tests/unit/test_researcher_guard.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/parts.py backend/src/copperchat/uploads.py backend/tests/unit/test_parts_storage.py backend/tests/unit/test_uploads_storage.py
git commit -m "feat: parts and upload storage with MPN and filename sanitising

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8.6: Research runner and `research_part` tool

**Files:**
- Create: `backend/src/copperchat/research/runner.py`, `backend/src/copperchat/research/tools.py`
- Modify: `backend/src/copperchat/tools/registry.py` (append `research` provider)
- Modify: `backend/pyproject.toml` (`httpx>=0.28` in `[project].dependencies` if not present)
- Test: `backend/tests/unit/test_research_runner.py`

**Interfaces:**
- Consumes: `PART_RESEARCHER`, `make_researcher_guard`, `ToolSpec`/`ToolContext`/`ToolResult`/`StructuredToolError` (C5), `mpn_dirname`.
- Produces:
  - `QueryFn = Callable[[str, ClaudeAgentOptions], AsyncIterator[Any]]`
  - `def researcher_options(project, settings) -> ClaudeAgentOptions`
  - `async def run_research(project, settings, query: str, attachment_paths: list[str], *, query_fn: QueryFn | None = None, timeout_s: float = 900.0) -> str`
  - `async def download_datasheet(project, mpn: str, url: str, *, client: httpx.AsyncClient | None = None) -> Path`
  - `TOOL_SPECS = [research_part]` in `copperchat.research.tools`; input schema `{query: string (required), attachment_paths: string[] }`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_research_runner.py
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, ToolPermissionContext
from pydantic import SecretStr

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.projects import Project
from copperchat.research import runner
from copperchat.research.tools import TOOL_SPECS


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "proj"
    (d / "docs/parts/_uploads").mkdir(parents=True)
    return Project(id="0123456789ab", name="proj", rel_path="proj/proj.kicad_pro", dir=d,
                   pro_file=d / "proj.kicad_pro", sch_file=None, pcb_file=None)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(projects_root=tmp_path, data_dir=tmp_path / "data",
                    claude_code_oauth_token=SecretStr("tok"), admin_password=SecretStr("pw"))


def result(text: str, is_error: bool = False) -> ResultMessage:
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id="s", result=text)


def test_options_are_isolated(project: Project, settings: Settings) -> None:
    opts = runner.researcher_options(project, settings)
    assert opts.tools == ["WebSearch", "WebFetch", "Read", "Write", "Edit", "Glob"]
    assert opts.allowed_tools == []
    assert opts.setting_sources == []
    assert opts.cwd == str(project.dir)
    assert opts.can_use_tool is not None
    assert set(opts.mcp_servers) == {"parts"}
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert opts.max_turns == 40


async def test_run_research_returns_summary_and_guard_is_live(project: Project, settings: Settings) -> None:
    seen: dict[str, Any] = {}

    async def fake(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        seen["prompt"] = prompt
        assert options.can_use_tool is not None
        denied = await options.can_use_tool("Write", {"file_path": "/etc/x", "content": ""},
                                            ToolPermissionContext(tool_use_id="1"))
        seen["denied"] = denied.behavior
        yield result("TPS62133: 3 MHz buck. Notes: docs/parts/TPS62133/notes.md")

    text = await runner.run_research(project, settings, "How is TPS62133 laid out?",
                                     ["docs/parts/_uploads/abcd1234-ds.pdf"], query_fn=fake)
    assert text.startswith("TPS62133")
    assert seen["denied"] == "deny"
    assert "docs/parts/_uploads/abcd1234-ds.pdf" in seen["prompt"]


async def test_attachment_paths_validated(project: Project, settings: Settings) -> None:
    async def never(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        raise AssertionError("must not run")
        yield

    with pytest.raises(StructuredToolError):
        await runner.run_research(project, settings, "q", ["../../etc/passwd"], query_fn=never)


async def test_error_result_raises_structured_error(project: Project, settings: Settings) -> None:
    async def failing(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
        yield result("rate limited", is_error=True)

    with pytest.raises(StructuredToolError) as e:
        await runner.run_research(project, settings, "q", [], query_fn=failing)
    assert e.value.to_dict()["what"] == "Part research failed"


async def test_download_datasheet(project: Project) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok.pdf":
            return httpx.Response(200, content=b"%PDF-1.7\nx")
        return httpx.Response(200, content=b"<html>login</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        path = await runner.download_datasheet(project, "LM358/NOPB", "https://ti.com/ok.pdf", client=client)
        assert path == project.dir / "docs/parts/LM358_NOPB/datasheet.pdf"
        with pytest.raises(StructuredToolError):
            await runner.download_datasheet(project, "LM358", "https://ti.com/page", client=client)
        with pytest.raises(StructuredToolError):
            await runner.download_datasheet(project, "LM358", "file:///etc/passwd", client=client)
        with pytest.raises(StructuredToolError):
            await runner.download_datasheet(project, "../x", "https://ti.com/ok.pdf", client=client)


def test_tool_spec_registered() -> None:
    spec = next(s for s in TOOL_SPECS if s.name == "research_part")
    assert spec.input_schema["required"] == ["query"]
    assert not spec.mutating and not spec.changes_documents
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_research_runner.py -v`
Expected: FAIL with `ImportError: cannot import name 'runner'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/research/runner.py
"""One-shot SDK query that executes the part-researcher definition."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query, tool

from copperchat.agent.guards import make_researcher_guard
from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.parts import InvalidMpn, mpn_dirname
from copperchat.projects import Project
from copperchat.research.agent import PART_RESEARCHER, RESEARCHER_TOOLS
from copperchat.research.paths import parts_root, resolve_inside, uploads_image_root

QueryFn = Callable[[str, ClaudeAgentOptions], AsyncIterator[Any]]
MAX_DATASHEET_BYTES = 30 * 1024 * 1024
_locks: dict[str, asyncio.Lock] = {}


async def download_datasheet(
    project: Project, mpn: str, url: str, *, client: httpx.AsyncClient | None = None
) -> Path:
    try:
        folder = mpn_dirname(mpn)
    except InvalidMpn as exc:
        raise StructuredToolError("Invalid part number", str(exc), "Use the plain MPN, e.g. TPS62133") from exc
    if not url.startswith(("https://", "http://")):
        raise StructuredToolError("Download refused", "only http(s) URLs are allowed", "Pass the datasheet URL")
    own = client is None
    c = client or httpx.AsyncClient(follow_redirects=True, timeout=60.0)
    try:
        async with c.stream("GET", url) as resp:
            resp.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_DATASHEET_BYTES:
                    raise StructuredToolError("Datasheet too large", "over 30 MB", "Find a smaller PDF")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise StructuredToolError("Download failed", str(exc), "Try another source URL") from exc
    finally:
        if own:
            await c.aclose()
    data = b"".join(chunks)
    if not data.startswith(b"%PDF-"):
        raise StructuredToolError("Not a PDF", "the URL did not return a PDF file", "Use a direct PDF link")
    dest_dir = parts_root(project) / folder
    if resolve_inside(str(dest_dir), project.dir, [parts_root(project)]) is None:
        raise StructuredToolError("Download refused", "part folder escapes docs/parts", "Check the MPN")
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / ".datasheet.pdf.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, dest_dir / "datasheet.pdf")
    return dest_dir / "datasheet.pdf"


def _parts_server(project: Project) -> Any:
    @tool("download_datasheet", "Download a datasheet PDF into docs/parts/<MPN>/datasheet.pdf",
          {"mpn": str, "url": str})
    async def _download(args: dict[str, Any]) -> dict[str, Any]:
        try:
            path = await download_datasheet(project, args["mpn"], args["url"])
        except StructuredToolError as err:
            return {"content": [{"type": "text", "text": str(err.to_dict())}], "is_error": True}
        return {"content": [{"type": "text", "text": f"saved {path.relative_to(project.dir).as_posix()}"}]}

    return create_sdk_mcp_server("parts", tools=[_download])


def _credentials_env(settings: Settings) -> dict[str, str]:
    if settings.claude_code_oauth_token is not None:
        return {"CLAUDE_CODE_OAUTH_TOKEN": settings.claude_code_oauth_token.get_secret_value()}
    if settings.anthropic_api_key is not None:
        return {"ANTHROPIC_API_KEY": settings.anthropic_api_key.get_secret_value()}
    return {}


def researcher_options(project: Project, settings: Settings) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=PART_RESEARCHER.prompt,
        tools=list(RESEARCHER_TOOLS),
        allowed_tools=[],  # every call goes through the guard
        permission_mode="default",
        can_use_tool=make_researcher_guard(project),
        mcp_servers={"parts": _parts_server(project)},
        setting_sources=[],
        cwd=str(project.dir),
        env=_credentials_env(settings),
        max_turns=PART_RESEARCHER.maxTurns,
    )


async def _sdk_query(prompt: str, options: ClaudeAgentOptions) -> AsyncIterator[Any]:
    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "user", "message": {"role": "user", "content": prompt},
               "parent_tool_use_id": None, "session_id": "part-researcher"}

    async for msg in query(prompt=stream(), options=options):
        yield msg


async def run_research(
    project: Project, settings: Settings, question: str, attachment_paths: list[str],
    *, query_fn: QueryFn | None = None, timeout_s: float = 900.0,
) -> str:
    readable = [parts_root(project), uploads_image_root(project)]
    for p in attachment_paths:
        if resolve_inside(p, project.dir, readable) is None:
            raise StructuredToolError(
                "Attachment path rejected", f"{p!r} is not under docs/parts/ or .kicad-agent/uploads/",
                "Pass the path exactly as shown in the attachment note",
            )
    prompt = question
    if attachment_paths:
        prompt += "\n\nAttached files (read these first):\n" + "\n".join(f"- {p}" for p in attachment_paths)
    fn = query_fn or _sdk_query
    lock = _locks.setdefault(project.id, asyncio.Lock())

    async def collect() -> str:
        final: ResultMessage | None = None
        async for msg in fn(prompt, researcher_options(project, settings)):
            if isinstance(msg, ResultMessage):
                final = msg
        if final is None or final.is_error or not final.result:
            why = (final.result or final.subtype) if final else "no result"
            raise StructuredToolError("Part research failed", str(why), "Retry later or ask the user for a datasheet")
        return final.result

    async with lock:
        try:
            return await asyncio.wait_for(collect(), timeout=timeout_s)
        except TimeoutError as exc:
            raise StructuredToolError("Part research timed out", f"exceeded {int(timeout_s)} s",
                                      "Ask a narrower question") from exc
```

```python
# backend/src/copperchat/research/tools.py
from typing import Any

from copperchat.research.runner import run_research
from copperchat.tools import ToolContext, ToolResult, ToolSpec


async def _research_part(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    summary = await run_research(ctx.project, ctx.settings, str(args["query"]),
                                 [str(p) for p in args.get("attachment_paths", [])])
    return ToolResult(data={"summary": summary})


TOOL_SPECS = [
    ToolSpec(
        name="research_part",
        description=(
            "Delegate a question about an electronic part (pinout, reference circuit, layout rules) to the "
            "part-researcher, which uses docs/parts/<MPN>/notes.md or the web and returns a concise summary. "
            "Pass attached PDF paths in attachment_paths."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "MPN plus the question"},
                "attachment_paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_research_part,
    )
]
```

`backend/src/copperchat/tools/registry.py`: append the provider import
`from copperchat.research import tools as research_tools` and add
`research_tools` to the provider list (C5).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_research_runner.py -v && uv run mypy src`
Expected: all passed; mypy clean.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/research backend/src/copperchat/tools/registry.py backend/pyproject.toml backend/uv.lock backend/tests/unit/test_research_runner.py
git commit -m "feat: research_part tool running the part-researcher as a one-shot query

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8.7: REST routes for skills, parts and uploads

**Files:**
- Create: `backend/src/copperchat/web/routes_skills.py`, `web/routes_parts.py`, `web/routes_uploads.py`
- Modify: `backend/src/copperchat/web/app.py` (`app.include_router(...)` ×3)
- Test: `backend/tests/unit/test_routes_phase8.py`

**Interfaces:**
- Consumes: `require_user`, `app.state.settings`, `app.state.db`, `get_project`, store/parts/uploads modules.
- Produces (C8 plus these **additions**, reported to the index):
  - `GET /api/skills?projectId=` → `[{name, origin, description, shadowed, hasBuiltin, originUpdated, enabled}]`
  - `GET /api/skills/{name}?projectId=` → `{…list fields, content, originDiff: string|null, editable: bool}` (**new**)
  - `POST /api/skills/{name}/customise` → 201 skill detail; 409 if customised
  - `PUT /api/skills/user/{name}` `{content}` → 204
  - `POST /api/skills/{name}/reset` → 204 (deletes the user copy)
  - `POST /api/skills/{name}/acknowledge-update` → 204 (**new**)
  - `PUT /api/projects/{id}/skills/{name}` `{enabled}` → 204
  - `GET /api/projects/{id}/parts` → `[{mpn, hasNotes, hasDatasheet, userEdited, updatedAt}]`
  - `GET /api/projects/{id}/parts/{mpn}/notes` → `{mpn, content, userEdited}`; `PUT` `{content}` → 204
  - `GET /api/projects/{id}/parts/{mpn}/datasheet` → `application/pdf`, `Content-Disposition: inline`
  - `POST /api/projects/{id}/uploads` (multipart field `file`) → 201 `AttachmentRef`
  - `GET /api/projects/{id}/uploads/{uploadId}` → the stored file (**new**, for composer/chat thumbnails)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_routes_phase8.py
import io
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr

from copperchat.config import Settings
from copperchat.projects import discover_projects
from copperchat.web.app import create_app

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "projects"


@pytest.fixture
def client(tmp_path: Path):
    pro = sorted(FIXTURES.rglob("*.kicad_pro"))[0]
    shutil.copytree(pro.parent, tmp_path / "projects" / pro.parent.name)
    settings = Settings(projects_root=tmp_path / "projects", data_dir=tmp_path / "data",
                        admin_username="admin", admin_password=SecretStr("pw"), dev_fake_agent=True)
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "pw"}).status_code == 204
        c.project_id = discover_projects(settings.projects_root)[0].id  # type: ignore[attr-defined]
        c.data_dir = settings.data_dir  # type: ignore[attr-defined]
        yield c


def test_skills_listing_customise_reset_and_toggle(client: TestClient) -> None:
    pid = client.project_id  # type: ignore[attr-defined]
    listed = client.get(f"/api/skills?projectId={pid}").json()
    tools = next(s for s in listed if s["name"] == "copperchat-tools")
    assert tools["origin"] == "builtin" and tools["enabled"] is True
    assert client.post("/api/skills/copperchat-tools/customise").status_code == 201
    assert client.post("/api/skills/copperchat-tools/customise").status_code == 409
    detail = client.get(f"/api/skills/copperchat-tools?projectId={pid}").json()
    assert detail["origin"] == "user" and detail["editable"] is True and detail["originDiff"] is None
    assert client.put(f"/api/projects/{pid}/skills/copperchat-tools", json={"enabled": False}).status_code == 204
    listed = client.get(f"/api/skills?projectId={pid}").json()
    assert next(s for s in listed if s["name"] == "copperchat-tools")["enabled"] is False
    assert client.post("/api/skills/copperchat-tools/reset").status_code == 204
    assert client.get(f"/api/skills/copperchat-tools?projectId={pid}").json()["origin"] == "builtin"


@pytest.mark.parametrize("bad", ["..%2F..%2Fetc", "UPPER", "a.b", "-x"])
def test_skill_name_traversal_rejected(client: TestClient, bad: str) -> None:
    r = client.put(f"/api/skills/user/{bad}", json={"content": "---\nname: x\ndescription: y\n---\n"})
    assert r.status_code in (400, 404, 405)
    skills_dir = client.data_dir / "skills"  # type: ignore[attr-defined]
    assert list(skills_dir.glob("**/SKILL.md")) == []


def test_parts_notes_and_mpn_traversal(client: TestClient) -> None:
    pid = client.project_id  # type: ignore[attr-defined]
    assert client.put(f"/api/projects/{pid}/parts/TPS62133/notes", json={"content": "# n\n"}).status_code == 204
    got = client.get(f"/api/projects/{pid}/parts/TPS62133/notes").json()
    assert got["userEdited"] is True
    assert [p["mpn"] for p in client.get(f"/api/projects/{pid}/parts").json()] == ["TPS62133"]
    for bad in ("..", "_uploads", "..%2F..%2Fx", ".git"):
        assert client.put(f"/api/projects/{pid}/parts/{bad}/notes", json={"content": "x"}).status_code in (400, 404, 405)  # httpx collapses ".."
    assert client.get(f"/api/projects/{pid}/parts/TPS62133/datasheet").status_code == 404


def test_upload_image_and_pdf(client: TestClient) -> None:
    pid = client.project_id  # type: ignore[attr-defined]
    buf = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buf, "PNG")
    r = client.post(f"/api/projects/{pid}/uploads", files={"file": ("../../x.png", buf.getvalue(), "text/plain")})
    assert r.status_code == 201
    ref = r.json()
    assert ref["kind"] == "image" and ref["name"] == "x.png" and len(ref["id"]) == 32
    assert client.get(f"/api/projects/{pid}/uploads/{ref['id']}").status_code == 200
    r = client.post(f"/api/projects/{pid}/uploads", files={"file": ("ds.pdf", b"%PDF-1.7\n", "application/pdf")})
    assert r.status_code == 201 and r.json()["kind"] == "pdf"
    r = client.post(f"/api/projects/{pid}/uploads", files={"file": ("x.pdf", b"MZ\x90\x00", "application/pdf")})
    assert r.status_code == 415
    assert client.get(f"/api/projects/{pid}/uploads/{'0' * 32}").status_code == 404


def test_requires_login(tmp_path: Path) -> None:
    settings = Settings(projects_root=tmp_path, data_dir=tmp_path / "d", admin_password=SecretStr("pw"),
                        dev_fake_agent=True)
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/skills").status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_routes_phase8.py -v`
Expected: FAIL — `GET /api/skills` returns 404.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/web/routes_skills.py
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from copperchat.db.repos.skill_prefs import disabled_skills, set_skill_enabled
from copperchat.projects import ProjectNotFound, get_project
from copperchat.skills import store
from copperchat.skills.store import EffectiveSkill, SkillLayers
from copperchat.web.auth import require_user

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


class ContentBody(BaseModel):
    content: str


class EnabledBody(BaseModel):
    enabled: bool


def _layers(request: Request, project_id: str | None) -> SkillLayers:
    settings = request.app.state.settings
    project = None
    if project_id:
        try:
            project = get_project(settings.projects_root, project_id)
        except ProjectNotFound as exc:
            raise HTTPException(404, "project not found") from exc
    return SkillLayers.for_project(settings, project)


def _dto(s: EffectiveSkill, disabled: set[str]) -> dict[str, Any]:
    return {"name": s.name, "origin": s.origin, "description": s.description, "shadowed": list(s.shadowed),
            "hasBuiltin": s.has_builtin, "originUpdated": s.origin_updated, "enabled": s.name not in disabled}


def _guard(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except (store.InvalidSkillName, store.InvalidSkill) as exc:
        raise HTTPException(400, str(exc)) from exc
    except store.SkillNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except store.SkillConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/skills")
async def list_skills(request: Request, projectId: str | None = None) -> list[dict[str, Any]]:  # noqa: N803
    disabled = await disabled_skills(request.app.state.db, projectId) if projectId else set()
    return [_dto(s, disabled) for s in store.effective_skills(_layers(request, projectId))]


@router.get("/skills/{name}")
async def skill_detail(request: Request, name: str, projectId: str | None = None) -> dict[str, Any]:  # noqa: N803
    layers = _layers(request, projectId)
    skill: EffectiveSkill = _guard(store.get_skill, layers, name)
    disabled = await disabled_skills(request.app.state.db, projectId) if projectId else set()
    return {
        **_dto(skill, disabled),
        "content": (skill.dir / "SKILL.md").read_text(encoding="utf-8", errors="replace"),
        "originDiff": _guard(store.origin_diff, layers, name) if skill.origin == "user" else None,
        "editable": skill.origin == "user",
    }


@router.post("/skills/{name}/customise", status_code=201)
async def customise(request: Request, name: str) -> dict[str, Any]:
    layers = _layers(request, None)
    _guard(store.customise, layers, name)
    return _dto(_guard(store.get_skill, layers, name), set())


@router.put("/skills/user/{name}", status_code=204)
async def put_user_skill(request: Request, name: str, body: ContentBody) -> Response:
    _guard(store.write_user_skill, _layers(request, None), name, body.content)
    return Response(status_code=204)


@router.post("/skills/{name}/reset", status_code=204)
async def reset(request: Request, name: str) -> Response:
    _guard(store.reset, _layers(request, None), name)
    return Response(status_code=204)


@router.post("/skills/{name}/acknowledge-update", status_code=204)
async def acknowledge(request: Request, name: str) -> Response:
    _guard(store.acknowledge_update, _layers(request, None), name)
    return Response(status_code=204)


@router.put("/projects/{project_id}/skills/{name}", status_code=204)
async def set_enabled(request: Request, project_id: str, name: str, body: EnabledBody) -> Response:
    _guard(store.validate_skill_name, name)
    _layers(request, project_id)  # 404 for unknown project
    await set_skill_enabled(request.app.state.db, project_id, name, body.enabled)
    return Response(status_code=204)
```

```python
# backend/src/copperchat/web/routes_parts.py
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from copperchat import parts
from copperchat.projects import Project, ProjectNotFound, get_project
from copperchat.web.auth import require_user

router = APIRouter(prefix="/api/projects/{project_id}/parts", dependencies=[Depends(require_user)])


class NotesBody(BaseModel):
    content: str


def _project(request: Request, project_id: str) -> Project:
    try:
        return get_project(request.app.state.settings.projects_root, project_id)
    except ProjectNotFound as exc:
        raise HTTPException(404, "project not found") from exc


def _mpn(mpn: str) -> str:
    try:
        return parts.mpn_dirname(mpn)
    except parts.InvalidMpn as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("")
async def list_parts(request: Request, project_id: str) -> list[dict[str, Any]]:
    return [
        {"mpn": p.mpn, "hasNotes": p.has_notes, "hasDatasheet": p.has_datasheet,
         "userEdited": p.user_edited, "updatedAt": p.updated_at}
        for p in parts.list_parts(_project(request, project_id))
    ]


@router.get("/{mpn}/notes")
async def get_notes(request: Request, project_id: str, mpn: str) -> dict[str, Any]:
    project, name = _project(request, project_id), _mpn(mpn)
    try:
        content = parts.read_notes(project, name)
    except parts.InvalidMpn as exc:
        raise HTTPException(400, str(exc)) from exc
    notes = project.dir / "docs" / "parts" / name / "notes.md"
    return {"mpn": name, "content": content, "userEdited": parts.notes_user_edited(notes)}


@router.put("/{mpn}/notes", status_code=204)
async def put_notes(request: Request, project_id: str, mpn: str, body: NotesBody) -> Response:
    if len(body.content.encode("utf-8")) > 512 * 1024:
        raise HTTPException(413, "notes exceed 512 KiB")
    try:
        parts.write_user_notes(_project(request, project_id), _mpn(mpn), body.content)
    except parts.InvalidMpn as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(status_code=204)


@router.get("/{mpn}/datasheet")
async def datasheet(request: Request, project_id: str, mpn: str) -> FileResponse:
    path = parts.datasheet_path(_project(request, project_id), _mpn(mpn))
    if path is None:
        raise HTTPException(404, "no datasheet")
    return FileResponse(path, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{path.parent.name}.pdf"'})
```

```python
# backend/src/copperchat/web/routes_uploads.py
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from copperchat.db.repos.uploads import UploadRow, get_upload, insert_upload
from copperchat.projects import Project, ProjectNotFound, get_project
from copperchat.uploads import MAX_PDF_BYTES, UploadRejected, store_upload
from copperchat.web.auth import require_user

router = APIRouter(prefix="/api/projects/{project_id}/uploads", dependencies=[Depends(require_user)])
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _project(request: Request, project_id: str) -> Project:
    try:
        return get_project(request.app.state.settings.projects_root, project_id)
    except ProjectNotFound as exc:
        raise HTTPException(404, "project not found") from exc


@router.post("", status_code=201)
async def upload(request: Request, project_id: str, file: UploadFile) -> dict[str, str]:
    project = _project(request, project_id)
    data = await file.read(MAX_PDF_BYTES + 1)
    try:
        stored = store_upload(project, data, file.filename)
    except UploadRejected as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    await insert_upload(
        request.app.state.db,
        UploadRow(id=stored.id, project_id=project.id, kind=stored.kind, name=stored.name,
                  rel_path=stored.rel_path, mime=stored.mime, size=stored.size,
                  created_at=datetime.now(UTC).isoformat()),
    )
    return {"id": stored.id, "kind": stored.kind, "name": stored.name}


@router.get("/{upload_id}")
async def get_file(request: Request, project_id: str, upload_id: str) -> FileResponse:
    project = _project(request, project_id)
    row = await get_upload(request.app.state.db, project.id, upload_id) if _ID_RE.fullmatch(upload_id) else None
    path = None if row is None else project.dir / row.rel_path
    if row is None or path is None or not path.is_file() or path.is_symlink():
        raise HTTPException(404, "upload not found")
    return FileResponse(path, media_type=row.mime)
```

`web/app.py`: `from copperchat.web import routes_parts, routes_skills, routes_uploads`
and `app.include_router(routes_skills.router)`, `…routes_parts.router`,
`…routes_uploads.router`. `python-multipart` is required by `UploadFile`;
add `python-multipart>=0.0.20` (Apache-2.0) to dependencies if Phase 3 did not.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_routes_phase8.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/web backend/pyproject.toml backend/uv.lock backend/tests/unit/test_routes_phase8.py
git commit -m "feat: REST routes for skills, parts and uploads

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8.8: Attachments into the agent turn

**Files:**
- Create: `backend/src/copperchat/agent/attachments.py`
- Modify: `backend/src/copperchat/agent/sdk_backend.py` (`user_prompt` call sites)
- Modify: `backend/src/copperchat/web/app.py` (inject resolver into `RunEngine`)
- Test: `backend/tests/unit/test_attachment_content.py`

**Interfaces:**
- Consumes: `AttachmentRef` (C7), `get_upload`, `Project`.
- Produces (`copperchat.agent.attachments`):
  - `async def attachment_exists(db, project_id: str, attachment_id: str) -> bool`
  - `async def build_user_content(db, project: Project, text: str, attachments: list[AttachmentRef]) -> str | list[dict[str, Any]]` — returns plain `text` when no attachments (unchanged Phase 5 behaviour), otherwise `[{"type":"text",…}, image blocks…, PDF notes…]`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/unit/test_attachment_content.py
import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from copperchat.agent.attachments import attachment_exists, build_user_content
from copperchat.agent.protocol import AttachmentRef
from copperchat.db import Database
from copperchat.db.repos.uploads import UploadRow, insert_upload
from copperchat.projects import Project
from copperchat.uploads import store_upload


@pytest.fixture
async def env(tmp_path: Path):
    d = tmp_path / "proj"
    d.mkdir()
    project = Project(id="0123456789ab", name="proj", rel_path="proj/proj.kicad_pro", dir=d,
                      pro_file=d / "proj.kicad_pro", sch_file=None, pcb_file=None)
    db = await Database.open(tmp_path / "t.db")
    yield project, db
    await db.close()


async def _store(db: Database, project: Project, data: bytes, name: str) -> AttachmentRef:
    up = store_upload(project, data, name)
    await insert_upload(db, UploadRow(up.id, project.id, up.kind, up.name, up.rel_path, up.mime, up.size, "t"))
    return AttachmentRef(id=up.id, kind=up.kind, name=up.name)


async def test_plain_text_unchanged(env) -> None:
    project, db = env
    assert await build_user_content(db, project, "hi", []) == "hi"


async def test_image_and_pdf_blocks(env) -> None:
    project, db = env
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    img = await _store(db, project, buf.getvalue(), "photo.png")
    pdf = await _store(db, project, b"%PDF-1.7\n", "ds.pdf")
    content = await build_user_content(db, project, "look", [img, pdf])
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look"}
    image_block = content[1]
    assert image_block["type"] == "image" and image_block["source"]["type"] == "base64"
    data = base64.b64decode(image_block["source"]["data"])
    assert data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8\xff")
    note = content[2]
    assert note["type"] == "text" and "docs/parts/_uploads/" in note["text"] and "research_part" in note["text"]
    assert await attachment_exists(db, project.id, img.id)
    assert not await attachment_exists(db, "other", img.id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_attachment_content.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'copperchat.agent.attachments'`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/src/copperchat/agent/attachments.py
from __future__ import annotations

import base64
from typing import Any

from copperchat.agent.protocol import AttachmentRef
from copperchat.db import Database
from copperchat.db.repos.uploads import get_upload
from copperchat.projects import Project


async def attachment_exists(db: Database, project_id: str, attachment_id: str) -> bool:
    return await get_upload(db, project_id, attachment_id) is not None


async def build_user_content(
    db: Database, project: Project, text: str, attachments: list[AttachmentRef]
) -> str | list[dict[str, Any]]:
    if not attachments:
        return text
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for ref in attachments:
        row = await get_upload(db, project.id, ref.id)
        if row is None:
            blocks.append({"type": "text", "text": f"[Attachment {ref.name} is no longer available.]"})
            continue
        path = project.dir / row.rel_path
        if row.kind == "image" and path.is_file():
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": row.mime,
                                                        "data": base64.b64encode(path.read_bytes()).decode()}})
        else:
            blocks.append({"type": "text", "text": (
                f"[Attached PDF \"{row.name}\" stored at {row.rel_path}. You cannot open files yourself; "
                f"call research_part with attachment_paths=[\"{row.rel_path}\"] to have it read.]")})
    return blocks
```

Modify `sdk_backend.py`: add

```python
async def user_prompt_with_attachments(item: QueuedItem, db: Database, project: Project) -> dict[str, Any]:
    prompt = user_prompt(item)
    prompt["message"]["content"] = await build_user_content(db, project, item.text, item.attachments)
    return prompt
```

and replace each `yield user_prompt(item)` in the backend's input generator
with `yield await user_prompt_with_attachments(item, self._db, project)`. Modify `web/app.py` where the
`RunEngine` is constructed: pass
`attachment_exists=lambda pid, aid: attachment_exists(db, pid, aid)` replacing
Phase 5's always-false default, so `send` with an uploaded id is accepted and
unknown ids still produce `error{code:"unknown_attachment"}`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/unit/test_attachment_content.py -v && uv run pytest -m "not kicad" -q`
Expected: new tests pass; Phase 5 engine tests (unknown attachment) still pass.

- [ ] **Step 5: Commit**

```bash
git add backend/src/copperchat/agent backend/src/copperchat/web/app.py backend/tests/unit/test_attachment_content.py
git commit -m "feat: pass image and PDF attachments to the agent turn

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8.9: Frontend API clients and Skills panel

**Files:**
- Create: `frontend/src/api/skills.ts`, `frontend/src/pages/skills/SkillsPanel.tsx`
- Test: `frontend/src/pages/skills/SkillsPanel.test.tsx`

**Interfaces:**
- Consumes: `apiGet`, `apiPost`, `apiPut` from `frontend/src/api/http.ts`.
- Produces: `SkillSummary`, `SkillDetail` zod schemas/types; `listSkills(projectId)`, `getSkill(name, projectId)`, `customiseSkill(name)`, `saveUserSkill(name, content)`, `resetSkill(name)`, `acknowledgeSkillUpdate(name)`, `setSkillEnabled(projectId, name, enabled)`; component `SkillsPanel({projectId})`.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/pages/skills/SkillsPanel.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const http = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn(), apiPut: vi.fn(), apiDelete: vi.fn() }));
vi.mock("../../api/http", () => http);

import { SkillsPanel } from "./SkillsPanel";

const summary = {
  name: "copperchat-tools", origin: "user", description: "Tools", shadowed: ["builtin"],
  hasBuiltin: true, originUpdated: true, enabled: true,
};

describe("SkillsPanel", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    http.apiGet.mockImplementation(async (path: string) =>
      path.startsWith("/skills?")
        ? [summary]
        : { ...summary, content: "---\nname: copperchat-tools\n---\nbody", originDiff: "+New rule", editable: true },
    );
    http.apiPut.mockResolvedValue(undefined);
    http.apiPost.mockResolvedValue(undefined);
  });

  it("shows origin badge, update notice with diff, and toggles enable", async () => {
    render(<SkillsPanel projectId="p1" />);
    expect(await screen.findByText("copperchat-tools")).toBeInTheDocument();
    expect(screen.getByText("user")).toBeInTheDocument();
    await userEvent.click(screen.getByText("copperchat-tools"));
    expect(await screen.findByText(/original updated/i)).toBeInTheDocument();
    expect(screen.getByText(/\+New rule/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox", { name: /enabled in this project/i }));
    await waitFor(() =>
      expect(http.apiPut).toHaveBeenCalledWith("/projects/p1/skills/copperchat-tools", { enabled: false }),
    );
  });

  it("saves an edited user skill", async () => {
    render(<SkillsPanel projectId="p1" />);
    await userEvent.click(await screen.findByText("copperchat-tools"));
    const editor = await screen.findByRole("textbox", { name: /skill content/i });
    await userEvent.type(editor, "!");
    await userEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() =>
      expect(http.apiPut).toHaveBeenCalledWith("/skills/user/copperchat-tools", {
        content: "---\nname: copperchat-tools\n---\nbody!",
      }),
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/skills/SkillsPanel.test.tsx`
Expected: FAIL — `Failed to resolve import "./SkillsPanel"`.

- [ ] **Step 3: Write minimal implementation**

```ts
// frontend/src/api/skills.ts
import { z } from "zod";
import { apiGet, apiPost, apiPut } from "./http";

export const SkillSummary = z.object({
  name: z.string(),
  origin: z.enum(["builtin", "user", "project"]),
  description: z.string(),
  shadowed: z.array(z.enum(["builtin", "user", "project"])),
  hasBuiltin: z.boolean(),
  originUpdated: z.boolean(),
  enabled: z.boolean(),
});
export type SkillSummary = z.infer<typeof SkillSummary>;
export const SkillDetail = SkillSummary.extend({
  content: z.string(),
  originDiff: z.string().nullable(),
  editable: z.boolean(),
});
export type SkillDetail = z.infer<typeof SkillDetail>;

const q = (projectId: string) => `projectId=${encodeURIComponent(projectId)}`;
const n = encodeURIComponent;

export async function listSkills(projectId: string): Promise<SkillSummary[]> {
  return z.array(SkillSummary).parse(await apiGet(`/skills?${q(projectId)}`));
}
export async function getSkill(name: string, projectId: string): Promise<SkillDetail> {
  return SkillDetail.parse(await apiGet(`/skills/${n(name)}?${q(projectId)}`));
}
export const customiseSkill = (name: string) => apiPost(`/skills/${n(name)}/customise`, {});
export const saveUserSkill = (name: string, content: string) => apiPut(`/skills/user/${n(name)}`, { content });
export const resetSkill = (name: string) => apiPost(`/skills/${n(name)}/reset`, {});
export const acknowledgeSkillUpdate = (name: string) => apiPost(`/skills/${n(name)}/acknowledge-update`, {});
export const setSkillEnabled = (projectId: string, name: string, enabled: boolean) =>
  apiPut(`/projects/${n(projectId)}/skills/${n(name)}`, { enabled });
```

```tsx
// frontend/src/pages/skills/SkillsPanel.tsx
import { useCallback, useEffect, useState } from "react";
import {
  acknowledgeSkillUpdate, customiseSkill, getSkill, listSkills, resetSkill, saveUserSkill, setSkillEnabled,
  type SkillDetail, type SkillSummary,
} from "../../api/skills";

const badgeClass: Record<SkillSummary["origin"], string> = {
  builtin: "bg-[var(--color-muted)] text-[var(--color-muted-foreground)]",
  user: "bg-[var(--color-accent)] text-[var(--color-accent-foreground)]",
  project: "bg-[var(--color-primary)] text-[var(--color-primary-foreground)]",
};

export function SkillsPanel({ projectId }: { projectId: string }) {
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [selected, setSelected] = useState<SkillDetail | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setSkills(await listSkills(projectId));
    } catch (e) {
      setError(String(e));
    }
  }, [projectId]);

  const open = useCallback(
    async (name: string) => {
      const detail = await getSkill(name, projectId);
      setSelected(detail);
      setDraft(detail.content);
    },
    [projectId],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const run = async (action: () => Promise<unknown>, reopen = true) => {
    setError(null);
    try {
      await action();
      await refresh();
      if (reopen && selected) await open(selected.name);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <div className="flex h-full flex-col gap-3 overflow-auto p-3">
      {error && <p role="alert" className="text-[var(--color-destructive)]">{error}</p>}
      <ul className="flex flex-col gap-1">
        {skills.map((s) => (
          <li key={s.name}>
            <button type="button" className="flex w-full items-center gap-2 text-left" onClick={() => void open(s.name)}>
              <span className="font-medium">{s.name}</span>
              <span className={`rounded px-1.5 text-xs ${badgeClass[s.origin]}`}>{s.origin}</span>
              {s.originUpdated && <span className="text-xs">●</span>}
              {!s.enabled && <span className="text-xs opacity-60">disabled</span>}
            </button>
            <p className="text-xs opacity-70">{s.description}</p>
          </li>
        ))}
      </ul>
      {selected && (
        <section className="flex flex-col gap-2 border-t border-[var(--color-border)] pt-3">
          <h3 className="font-semibold">{selected.name}</h3>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={selected.enabled}
              onChange={(e) => void run(() => setSkillEnabled(projectId, selected.name, e.target.checked))}
            />
            Enabled in this project
          </label>
          {selected.originUpdated && selected.originDiff && (
            <div className="rounded border border-[var(--color-border)] p-2">
              <p className="text-sm font-medium">The original updated since you customised this skill.</p>
              <pre className="max-h-64 overflow-auto text-xs">{selected.originDiff}</pre>
              <button type="button" onClick={() => void run(() => acknowledgeSkillUpdate(selected.name))}>
                Mark as reviewed
              </button>
            </div>
          )}
          <textarea
            aria-label="Skill content"
            className="min-h-64 w-full rounded border border-[var(--color-border)] bg-transparent p-2 font-mono text-xs"
            value={draft}
            readOnly={!selected.editable}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="flex flex-wrap gap-2">
            {selected.editable && (
              <button type="button" onClick={() => void run(() => saveUserSkill(selected.name, draft))}>
                Save
              </button>
            )}
            {selected.origin === "builtin" && (
              <button type="button" onClick={() => void run(() => customiseSkill(selected.name))}>
                Customise
              </button>
            )}
            {selected.origin === "user" && (
              <button
                type="button"
                onClick={() =>
                  void run(async () => {
                    await resetSkill(selected.name);
                    if (!selected.hasBuiltin) setSelected(null);
                  }, selected.hasBuiltin)
                }
              >
                {selected.hasBuiltin ? "Reset to original" : "Delete"}
              </button>
            )}
            {selected.origin === "project" && (
              <p className="text-xs opacity-70">Project skills live in .kicad-agent/skills/ and are edited in the repository.</p>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/skills/SkillsPanel.test.tsx && npm run typecheck`
Expected: 2 passed; typecheck clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/skills.ts frontend/src/pages/skills
git commit -m "feat: skills panel with origin badges, customise, reset and update diff

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8.10: Parts panel

**Files:**
- Create: `frontend/src/api/parts.ts`, `frontend/src/pages/parts/PartsPanel.tsx`
- Test: `frontend/src/pages/parts/PartsPanel.test.tsx`

**Interfaces:**
- Consumes: `apiGet`, `apiPut`; `react-markdown` (MIT; add `react-markdown@^10` to `dependencies` if Phase 6A did not).
- Produces: `listParts(projectId)`, `getNotes(projectId, mpn)`, `saveNotes(projectId, mpn, content)`, `datasheetUrl(projectId, mpn)`; component `PartsPanel({projectId})`.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/pages/parts/PartsPanel.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

const http = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn(), apiPut: vi.fn(), apiDelete: vi.fn() }));
vi.mock("../../api/http", () => http);

import { PartsPanel } from "./PartsPanel";

beforeEach(() => {
  vi.resetAllMocks();
  http.apiGet.mockImplementation(async (path: string) =>
    path.endsWith("/parts")
      ? [{ mpn: "TPS62133", hasNotes: true, hasDatasheet: true, userEdited: false, updatedAt: null }]
      : { mpn: "TPS62133", content: "---\nuser_edited: false\n---\n# Pinout\nVIN on pin 1", userEdited: false },
  );
  http.apiPut.mockResolvedValue(undefined);
});

it("renders notes, links the datasheet and saves edits", async () => {
  render(<PartsPanel projectId="p1" />);
  await userEvent.click(await screen.findByText("TPS62133"));
  expect(await screen.findByRole("heading", { name: "Pinout" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /datasheet/i })).toHaveAttribute(
    "href", "/api/projects/p1/parts/TPS62133/datasheet",
  );
  await userEvent.click(screen.getByRole("button", { name: /edit/i }));
  const box = screen.getByRole("textbox", { name: /notes/i });
  await userEvent.clear(box);
  await userEvent.type(box, "mine");
  await userEvent.click(screen.getByRole("button", { name: /save/i }));
  await waitFor(() =>
    expect(http.apiPut).toHaveBeenCalledWith("/projects/p1/parts/TPS62133/notes", { content: "mine" }),
  );
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/parts/PartsPanel.test.tsx`
Expected: FAIL — `Failed to resolve import "./PartsPanel"`.

- [ ] **Step 3: Write minimal implementation**

```ts
// frontend/src/api/parts.ts
import { z } from "zod";
import { apiGet, apiPut } from "./http";

export const PartInfo = z.object({
  mpn: z.string(), hasNotes: z.boolean(), hasDatasheet: z.boolean(),
  userEdited: z.boolean(), updatedAt: z.string().nullable(),
});
export type PartInfo = z.infer<typeof PartInfo>;
export const PartNotes = z.object({ mpn: z.string(), content: z.string(), userEdited: z.boolean() });
export type PartNotes = z.infer<typeof PartNotes>;

const e = encodeURIComponent;
export async function listParts(projectId: string): Promise<PartInfo[]> {
  return z.array(PartInfo).parse(await apiGet(`/projects/${e(projectId)}/parts`));
}
export async function getNotes(projectId: string, mpn: string): Promise<PartNotes> {
  return PartNotes.parse(await apiGet(`/projects/${e(projectId)}/parts/${e(mpn)}/notes`));
}
export const saveNotes = (projectId: string, mpn: string, content: string) =>
  apiPut(`/projects/${e(projectId)}/parts/${e(mpn)}/notes`, { content });
export const datasheetUrl = (projectId: string, mpn: string) =>
  `/api/projects/${e(projectId)}/parts/${e(mpn)}/datasheet`;

export function stripFrontMatter(text: string): string {
  const m = /^---\n[\s\S]*?\n---\n?/.exec(text);
  return m ? text.slice(m[0].length) : text;
}
```

```tsx
// frontend/src/pages/parts/PartsPanel.tsx
import { useCallback, useEffect, useState } from "react";
import Markdown from "react-markdown";
import { datasheetUrl, getNotes, listParts, saveNotes, stripFrontMatter, type PartInfo, type PartNotes } from "../../api/parts";

export function PartsPanel({ projectId }: { projectId: string }) {
  const [parts, setParts] = useState<PartInfo[]>([]);
  const [notes, setNotes] = useState<PartNotes | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setParts(await listParts(projectId));
    } catch (e) {
      setError(String(e));
    }
  }, [projectId]);
  useEffect(() => {
    void refresh();
  }, [refresh]);

  const open = async (mpn: string) => {
    setDraft(null);
    setNotes(await getNotes(projectId, mpn));
  };
  const save = async () => {
    if (!notes || draft === null) return;
    try {
      await saveNotes(projectId, notes.mpn, draft);
      await open(notes.mpn);
      await refresh();
    } catch (e) {
      setError(String(e));
    }
  };
  const current = parts.find((p) => p.mpn === notes?.mpn);

  return (
    <div className="flex h-full flex-col gap-3 overflow-auto p-3">
      {error && <p role="alert" className="text-[var(--color-destructive)]">{error}</p>}
      {parts.length === 0 && <p className="text-sm opacity-70">No researched parts yet. Ask the agent about a part number.</p>}
      <ul className="flex flex-col gap-1">
        {parts.map((p) => (
          <li key={p.mpn}>
            <button type="button" onClick={() => void open(p.mpn)} className="flex gap-2">
              <span className="font-medium">{p.mpn}</span>
              {p.userEdited && <span className="text-xs opacity-70">edited by you</span>}
            </button>
          </li>
        ))}
      </ul>
      {notes && (
        <section className="flex flex-col gap-2 border-t border-[var(--color-border)] pt-3">
          <div className="flex items-center gap-3">
            <h3 className="font-semibold">{notes.mpn}</h3>
            {current?.hasDatasheet && (
              <a href={datasheetUrl(projectId, notes.mpn)} target="_blank" rel="noreferrer">Open datasheet</a>
            )}
            {draft === null ? (
              <button type="button" onClick={() => setDraft(notes.content)}>Edit</button>
            ) : (
              <>
                <button type="button" onClick={() => void save()}>Save</button>
                <button type="button" onClick={() => setDraft(null)}>Cancel</button>
              </>
            )}
          </div>
          {draft === null ? (
            <article className="prose max-w-none text-sm">
              <Markdown>{stripFrontMatter(notes.content)}</Markdown>
            </article>
          ) : (
            <>
              <textarea
                aria-label="Notes"
                className="min-h-80 w-full rounded border border-[var(--color-border)] bg-transparent p-2 font-mono text-xs"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
              />
              <p className="text-xs opacity-70">Saved notes are marked as yours; the researcher will not overwrite them.</p>
            </>
          )}
        </section>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/parts/PartsPanel.test.tsx && npm run typecheck`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/parts.ts frontend/src/pages/parts frontend/package.json frontend/package-lock.json
git commit -m "feat: parts panel with notes editor and datasheet link

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8.11: Composer attachment picker and panel registration

**Files:**
- Create: `frontend/src/api/uploads.ts`, `frontend/src/components/chat/AttachmentPicker.tsx`
- Modify: `frontend/src/panels/index.ts` (Phase 6A's panel registration module, already imported by `main.tsx`)
- Test: `frontend/src/components/chat/AttachmentPicker.test.tsx`

**Interfaces:**
- Consumes: `composerAttachmentSlot.register(Component)` from `@/components/chat/composerSlots`, `registerPanel` from `@/layouts/panels`, `AttachmentRef` type from `frontend/src/protocol/frames.ts`.
- Produces: `uploadAttachment(projectId, file): Promise<AttachmentRef>`, `AttachmentPicker` (slot component).

Upload uses `fetch` directly (multipart; `apiPost` JSON-encodes), with
`credentials: "same-origin"`. On iOS, the "Photo" input uses
`accept="image/*" capture="environment"` (camera), the "File" input accepts
`image/*,application/pdf` (photo library / Files); Safari transcodes HEIC to
JPEG for `image/*` inputs, and the server rejects anything it cannot sniff with
a clear 415 message shown in the chip row.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/components/chat/AttachmentPicker.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { AttachmentPicker } from "./AttachmentPicker";

afterEach(() => vi.unstubAllGlobals());

it("uploads a picked file and reports the AttachmentRef; remove clears it", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ id: "a".repeat(32), kind: "image", name: "p.png" }), { status: 201 }),
  );
  vi.stubGlobal("fetch", fetchMock);
  const setAttachments = vi.fn();
  const { rerender } = render(<AttachmentPicker projectId="p1" attachments={[]} setAttachments={setAttachments} />);
  const input = screen.getByLabelText(/attach file/i) as HTMLInputElement;
  expect(input.accept).toBe("image/*,application/pdf");
  expect(screen.getByLabelText(/take photo/i)).toHaveAttribute("capture", "environment");
  await userEvent.upload(input, new File([new Uint8Array([137, 80, 78, 71])], "p.png", { type: "image/png" }));
  await waitFor(() => expect(setAttachments).toHaveBeenCalledWith([{ id: "a".repeat(32), kind: "image", name: "p.png" }]));
  expect(fetchMock.mock.calls[0][0]).toBe("/api/projects/p1/uploads");
  const ref = { id: "a".repeat(32), kind: "image" as const, name: "p.png" };
  rerender(<AttachmentPicker projectId="p1" attachments={[ref]} setAttachments={setAttachments} />);
  await userEvent.click(screen.getByRole("button", { name: /remove p.png/i }));
  expect(setAttachments).toHaveBeenLastCalledWith([]);
});

it("shows the server error on rejection", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "only PNG…" }), { status: 415 })));
  render(<AttachmentPicker projectId="p1" attachments={[]} setAttachments={vi.fn()} />);
  await userEvent.upload(screen.getByLabelText(/attach file/i), new File(["x"], "x.heic", { type: "image/heic" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("only PNG");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/chat/AttachmentPicker.test.tsx`
Expected: FAIL — `Failed to resolve import "./AttachmentPicker"`.

- [ ] **Step 3: Write minimal implementation**

```ts
// frontend/src/api/uploads.ts
import type { AttachmentRef } from "../protocol/frames";

export async function uploadAttachment(projectId: string, file: File): Promise<AttachmentRef> {
  const body = new FormData();
  body.append("file", file, file.name);
  const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}/uploads`, {
    method: "POST", body, credentials: "same-origin",
  });
  const json: unknown = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (json as { detail?: unknown }).detail;
    throw new Error(typeof detail === "string" ? detail : `Upload failed (${res.status})`);
  }
  return json as AttachmentRef;
}
```

```tsx
// frontend/src/components/chat/AttachmentPicker.tsx
import { useRef, useState, type ChangeEvent } from "react";
import { uploadAttachment } from "../../api/uploads";
import type { AttachmentRef } from "../../protocol/frames";

export interface AttachmentPickerProps {
  projectId: string;
  attachments: AttachmentRef[];
  setAttachments: (next: AttachmentRef[]) => void;
}

export function AttachmentPicker({ projectId, attachments, setAttachments }: AttachmentPickerProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const latest = useRef(attachments);
  latest.current = attachments;

  const onFiles = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (files.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      for (const file of files) {
        const ref = await uploadAttachment(projectId, file);
        latest.current = [...latest.current, ref];
        setAttachments(latest.current);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <label className="cursor-pointer text-sm" aria-busy={busy}>
        <span aria-hidden>📎</span>
        <input aria-label="Attach file" type="file" multiple accept="image/*,application/pdf" className="sr-only"
          onChange={(e) => void onFiles(e)} disabled={busy} />
      </label>
      <label className="cursor-pointer text-sm">
        <span aria-hidden>📷</span>
        <input aria-label="Take photo" type="file" accept="image/*" capture="environment" className="sr-only"
          onChange={(e) => void onFiles(e)} disabled={busy} />
      </label>
      {attachments.map((a) => (
        <span key={a.id} className="flex items-center gap-1 rounded bg-[var(--color-muted)] px-2 text-xs">
          {a.kind === "image" ? (
            <img src={`/api/projects/${encodeURIComponent(projectId)}/uploads/${a.id}`} alt="" className="h-6 w-6 object-cover" />
          ) : (
            <span>PDF</span>
          )}
          {a.name}
          <button type="button" aria-label={`Remove ${a.name}`}
            onClick={() => setAttachments(attachments.filter((x) => x.id !== a.id))}>×</button>
        </span>
      ))}
      {busy && <span className="text-xs opacity-70">Uploading…</span>}
      {error && <span role="alert" className="text-xs text-[var(--color-destructive)]">{error}</span>}
    </div>
  );
}
```

(The two icon glyphs are decorative; if Phase 6A uses an icon library for the
composer, swap them for its paperclip/camera icons.)

```ts
// frontend/src/panels/index.ts — append to Phase 6A's module
import { BookOpen, Cpu } from "lucide-react";
import { composerAttachmentSlot } from "@/components/chat/composerSlots";
import { AttachmentPicker } from "@/components/chat/AttachmentPicker";
import { registerPanel } from "@/layouts/panels";
import { PartsPanel } from "@/pages/parts/PartsPanel";
import { SkillsPanel } from "@/pages/skills/SkillsPanel";

composerAttachmentSlot.register(AttachmentPicker);
registerPanel({ id: "skills", title: "Skills", icon: BookOpen, component: SkillsPanel });
registerPanel({ id: "parts", title: "Parts", icon: Cpu, component: PartsPanel });
```

(`icon` is a `lucide-react` component, as declared by Phase 6A.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npm run typecheck && npm run lint && npm test -- --run && npm run build`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/uploads.ts frontend/src/components/chat/AttachmentPicker.tsx frontend/src/components/chat/AttachmentPicker.test.tsx frontend/src/panels/index.ts
git commit -m "feat: composer attachments and skills/parts panels registration

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Phase self-review

Spec coverage:
- §7 Skills: three layers + precedence (8.2), assembly per session and SDK
  hand-off (8.3), GUI list/badge/customise/per-project enable/reset/"original
  updated" diff (8.2, 8.7, 8.9), schematic style by circuit type and
  OpenDriveHub `kicad-hardware` starting material (8.3).
- §8 Part researcher: steps 1–3 in the prompt (8.4), web + write access only
  for the researcher, path guard (8.4), concise summary returned (8.6),
  `docs/parts/` storage + Parts page with user-edit precedence (8.5, 8.7,
  8.10), PDFs under `docs/parts/`, images under `.kicad-agent/uploads/` (8.5).
- §6 `research_part(query)` (8.6), §4.2 Skills page, Parts page, image/PDF
  attachments (8.9–8.11), §5 step 1 attachments in `send` (8.8).
- Review Focus 5: skill names (8.2, 8.7), researcher write paths (8.4), MPN
  folder names + upload filenames (8.5, 8.7).

Deviations / decisions:
- `part-researcher` runs as a **separate one-shot SDK query** built from its
  `AgentDefinition`, not as an `agents=` entry on the main session (keeps the
  main agent's surface at `Skill` + CopperChat MCP tools; the main agent has no
  `Agent` tool and no `agents=`) — **owner review requested**. The main session
  gets `tools=["Skill"]` instead of `[]` when at least one skill is enabled
  (`Skill` is not a file/shell/web tool) — **owner review requested**.
- Datasheet download needs binary-safe storage, so the researcher additionally
  gets an in-process MCP tool `download_datasheet(mpn, url)`.
- User-edit precedence is front-matter based (`user_edited: true`, set
  automatically by the Parts page save), not mtime based.
- New cross-phase names: table `uploads`; routes `GET /skills/{name}`,
  `POST /skills/{name}/acknowledge-update`, `GET /projects/{id}/uploads/{uploadId}`;
  `copperchat.agent.attachments.attachment_exists/build_user_content`;
  `copperchat.skills.assembly.SkillBundle/assemble_skills/apply_skills`.
