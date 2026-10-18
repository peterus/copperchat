"""History wiring: snapshot store, document hashes, view publishing and run hooks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from copperchat.agent.protocol import ViewUpdatedFrame
from copperchat.db import Database
from copperchat.history.store import DocumentHashRepo, HistoryStore, document_files, sha256_file
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
        return (await render_view(session, doc, "agent", persist=False)).data
    except Exception:  # a missing thumbnail must never block a snapshot
        log.warning("history preview render failed", exc_info=True)
        return None


class HistoryService:
    def __init__(
        self, *, db: Database, sessions: KicadSessionManager, broadcaster: Any, runtime: Any
    ) -> None:
        self.db = db
        self.sessions = sessions
        self.broadcaster = broadcaster
        self.runtime = runtime
        self.hashes = DocumentHashRepo(db)
        self.write_view: Callable[[KicadSession, DocKind], Awaitable[Path]] = (
            write_kicanvas_snapshot
        )
        self.render_preview: Callable[[KicadSession, DocKind], Awaitable[bytes | None]] = (
            _default_preview
        )
        self._locks: dict[str, asyncio.Lock] = {}
        # Every CopperChat save rewrites the files: record their hashes right after it,
        # so the turn-start check never takes our own save for an external change.
        sessions.add_save_listener(self._on_saved)

    async def _on_saved(self, project: Project, doc: DocKind) -> None:
        """Re-record the saved document's files (+ the project file KiCad may rewrite);
        the other document's stored hashes stay, so an external change to it is still
        reported at the next turn start."""
        suffix = ".kicad_sch" if doc == "sch" else ".kicad_pcb"
        pro = project.pro_file.relative_to(project.dir).as_posix()

        def owned(rel: str) -> bool:
            return rel == pro or rel.endswith(suffix)

        def owned_hashes() -> dict[str, str]:
            rels = {p.relative_to(project.dir).as_posix(): p for p in document_files(project)}
            return {rel: sha256_file(path) for rel, path in rels.items() if owned(rel)}

        now = await asyncio.to_thread(owned_hashes)
        stored = await self.hashes.get(project.id)
        merged = {k: v for k, v in stored.items() if not owned(k)}
        merged |= now
        await self.hashes.replace(project.id, merged)

    def store(self, project: Project) -> HistoryStore:
        return HistoryStore(self.db, project)

    def lock(self, project: Project) -> asyncio.Lock:
        """Serialises restore against the turn-start hash check and the turn-end autosave."""
        return self._locks.setdefault(project.id, asyncio.Lock())

    @staticmethod
    def docs_of(project: Project) -> set[DocKind]:
        docs: set[DocKind] = set()
        if project.sch_file is not None:
            docs.add("sch")
        if project.pcb_file is not None:
            docs.add("pcb")
        return docs

    def open_session(self, project: Project) -> KicadSession | None:
        """The project's live session, or None when another (or no) project is open."""
        current = self.sessions.current()
        if current is None or current.closed or current.project.id != project.id:
            return None
        return current

    async def publish_view(
        self, project: Project, session: KicadSession, docs: set[DocKind]
    ) -> int:
        """Fresh KiCanvas snapshots for `docs`, then ONE view-updated frame (X2)."""
        docs = docs & self.docs_of(project)
        if not docs:
            return session.view_version
        for doc in sorted(docs):
            await self.write_view(session, doc)
        session.view_version += 1
        await self.broadcaster.publish(
            project.id,
            ViewUpdatedFrame(
                project_id=project.id, docs=sorted(docs), version=session.view_version
            ),
        )
        return session.view_version

    def hooks_factory(self, project: Project, engine: RunEngine) -> HistoryHooks:
        from copperchat.history.hooks import HistoryHooks

        return HistoryHooks(self, project, engine)


def install_history(app: FastAPI) -> HistoryService:
    """Called in the lifespan after AgentRuntime exists and BEFORE resume_pending():
    engines keep the hooks they were created with (C7a, X17)."""
    runtime = app.state.agent_runtime
    svc = HistoryService(
        db=app.state.db,
        sessions=app.state.sessions,
        broadcaster=runtime.broadcaster,
        runtime=runtime,
    )
    runtime.hooks_factory = svc.hooks_factory
    app.state.history = svc
    return svc
