"""Phase 7 helpers: fixture projects copied byte-for-byte, fakes for the engine/runtime.

Nothing here writes KiCad content: files are KiCad-generated fixtures copied verbatim.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from copperchat.projects import Project, discover_projects
from tests.support.conversations import TEST_CID
from tests.support.kicad_project import FIXTURES

ECC83 = FIXTURES / "ecc83"
HIERARCHY = FIXTURES / "complex_hierarchy"


def copy_project(src: Path, root: Path) -> Project:
    shutil.copytree(src, root / src.name)
    return next(p for p in discover_projects(root) if p.dir == root / src.name)


def replace_with_kicad_file(target: Path, source: Path) -> None:
    """Simulate an external save: swap in another file that KiCad itself wrote."""
    tmp = target.with_suffix(target.suffix + ".tmp")
    shutil.copyfile(source, tmp)
    tmp.replace(target)


def other_board() -> Path:
    return next(HIERARCHY.glob("*.kicad_pcb"))


class RecordingBroadcaster:
    def __init__(self) -> None:
        self.frames: list[Any] = []

    async def publish(self, project_id: str, frame: Any) -> None:
        self.frames.append(frame)


class FakeEngine:
    """Records notices, errors and notes; ``*_conversations`` keep where each went
    (None = broadcast note)."""

    def __init__(self, conversation_id: str = TEST_CID) -> None:
        self.is_busy = False
        self.current_conversation_id: str | None = conversation_id
        self.notices: list[tuple[str, dict[str, Any] | None]] = []
        self.errors: list[tuple[str, dict[str, Any] | None]] = []
        self.notes: list[str] = []
        self.notice_conversations: list[str] = []
        self.error_conversations: list[str] = []
        self.note_conversations: list[str | None] = []

    async def post_notice(
        self, conversation_id: str, text: str, metadata: dict[str, Any] | None = None
    ) -> None:
        self.notices.append((text, metadata))
        self.notice_conversations.append(conversation_id)

    async def post_error(
        self, conversation_id: str, text: str, metadata: dict[str, Any] | None = None
    ) -> None:
        self.errors.append((text, metadata))
        self.error_conversations.append(conversation_id)

    def add_agent_note(self, text: str, conversation_id: str | None = None) -> None:
        self.notes.append(text)
        self.note_conversations.append(conversation_id)


class FakeRuntime:
    def __init__(self, engine: FakeEngine | None) -> None:
        self.engine = engine

    def get(self, project_id: str) -> FakeEngine | None:
        return self.engine
