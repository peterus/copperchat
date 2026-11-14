"""History wiring: snapshot store, document hashes, view publishing and run hooks.

Lock order, always: `HistoryService.lock` (restore / turn-start check / autosave / live
publish) -> `HistoryService.view_lock` (every write of the view directory) -> the
session's locks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from copperchat.agent.protocol import ViewUpdatedFrame
from copperchat.db import Database
from copperchat.history.live import LiveViewPublisher
from copperchat.history.store import DocumentHashRepo, HistoryStore, document_files, sha256_file
from copperchat.kicad_session import DocKind, KicadSession, KicadSessionManager
from copperchat.projects import Project
from copperchat.views import render_view, write_kicanvas_snapshot
from copperchat.views.snapshot import VIEW_SUFFIXES, view_file_hashes, write_view_snapshots

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
        self._view_locks: dict[str, asyncio.Lock] = {}
        # Per project: hashes of the view files as of the last view-updated frame, i.e.
        # what the clients have loaded ("" = unknown, listed by the next publish).
        self._view_hashes: dict[str, dict[str, str]] = {}
        self.live = LiveViewPublisher(self)
        # (project, changed files) after the turn-start check found an external change
        # (E8b: a running/approved/paused plan is paused). Errors are logged.
        self.on_external_change: list[Callable[[Project, list[str]], Awaitable[None]]] = []
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

    def view_lock(self, project: Project) -> asyncio.Lock:
        """Serialises every write of the project's view directory (publishes and the
        open snapshots), so the hashes before/after a write belong to that write. Taken
        inside `lock` when both are needed, never the other way round."""
        return self._view_locks.setdefault(project.id, asyncio.Lock())

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
        self, project: Project, session: KicadSession, docs: set[DocKind], *, live: bool = False
    ) -> int:
        """Fresh KiCanvas snapshots for `docs`, then ONE view-updated frame (X2) whose
        `changedFiles` lists the view files that differ from the previous frame (changed,
        added or removed sub-sheets). `live` (the debounced mid-turn schematic refresh):
        no change -> no frame and no version bump. Every other publish always sends,
        even unchanged - the turn end is the guaranteed final state."""
        docs = docs & self.docs_of(project)
        if not docs:
            return session.view_version
        async with self.view_lock(project):
            known = self._view_hashes.get(project.id)
            if known is not None:
                before = {k: v for k, v in known.items() if _of_docs(k, docs)}
            else:  # nothing published yet: what is in the view directory now
                before = await asyncio.to_thread(view_file_hashes, project, docs)
            for doc in sorted(docs):
                await self.write_view(session, doc)
            after = await asyncio.to_thread(view_file_hashes, project, docs)
            changed = sorted(
                k for k in before.keys() | after.keys() if before.get(k) != after.get(k)
            )
            if live and not changed:
                return session.view_version
            rest = {k: v for k, v in (known or {}).items() if not _of_docs(k, docs)}
            self._view_hashes[project.id] = rest | after
            session.view_version += 1
            version = session.view_version
            await self.broadcaster.publish(
                project.id,
                ViewUpdatedFrame(
                    project_id=project.id,
                    docs=sorted(docs),
                    version=version,
                    changed_files=changed,
                    live=True if live else None,
                ),
            )
            return version

    async def write_open_snapshots(self, session: KicadSession) -> dict[DocKind, list[str]]:
        """`write_view_snapshots` for the open response (X1/X3), under the view lock. It
        sends no frame: before the first publish what it wrote is the baseline; after
        it, every file that now differs from what the clients have is marked unknown,
        so the next publish lists it (also a live one that changed nothing itself)."""
        project = session.project
        async with self.view_lock(project):
            try:
                return await write_view_snapshots(session)
            finally:
                now = await asyncio.to_thread(view_file_hashes, project, self.docs_of(project))
                known = self._view_hashes.get(project.id)
                if known is None:
                    self._view_hashes[project.id] = now
                else:
                    for name in known.keys() | now.keys():
                        if known.get(name) != now.get(name):
                            known[name] = ""

    def hooks_factory(self, project: Project, engine: RunEngine) -> HistoryHooks:
        from copperchat.history.hooks import HistoryHooks

        return HistoryHooks(self, project, engine)


def _of_docs(name: str, docs: set[DocKind]) -> bool:
    return name.endswith(tuple(VIEW_SUFFIXES[d] for d in docs))


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
