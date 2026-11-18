"""History wiring: snapshot store, document hashes, view publishing and run hooks.

Lock order, always: `HistoryService.lock` (restore / turn-start check / autosave / live
publish) -> `HistoryService.view_lock` (every write of the view directory) -> the
session's locks.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from copperchat.agent.protocol import ViewUpdatedFrame
from copperchat.db import Database
from copperchat.history.live import LiveViewPublisher
from copperchat.history.store import (
    DocumentHashRepo,
    HashRow,
    HistoryStore,
    LibraryLocation,
    LibraryLocator,
    TrackedFile,
    compute_tracked_hashes,
    document_files,
    tracked_files,
    with_presence_rows,
)
from copperchat.kicad_session import DocKind, KicadSession, KicadSessionManager
from copperchat.kicad_tools.footprints.engine import make_library_listener, make_library_locator
from copperchat.projects import Project
from copperchat.views import render_view, write_kicanvas_snapshot
from copperchat.views.snapshot import (
    VIEW_SUFFIXES,
    copy_disk_schematic,
    view_file_hashes,
    view_file_list,
    write_view_snapshots,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from copperchat.agent.engine import RunEngine
    from copperchat.history.hooks import HistoryHooks

log = logging.getLogger(__name__)


class ViewWriter(Protocol):
    """`write_kicanvas_snapshot`'s shape (tests substitute it)."""

    def __call__(
        self, session: KicadSession, doc: DocKind, *, save_modified: bool = ...
    ) -> Awaitable[Path]: ...


async def _default_preview(session: KicadSession, doc: DocKind) -> bytes | None:
    try:
        return (await render_view(session, doc, "agent", persist=False)).data
    except Exception:  # a missing thumbnail must never block a snapshot
        log.warning("history preview render failed", exc_info=True)
        return None


async def _no_library(project: Project) -> LibraryLocation | None:
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
        # The project's local footprint library, tracked with the documents (FP-C6);
        # replaced once the footprint tools are wired (F4a).
        self.locate_library: LibraryLocator = _no_library
        self.write_view: ViewWriter = write_kicanvas_snapshot
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
        # (project, changed rel paths, reason "own" | "external" | "restore") after the
        # local footprint library changed (FP-C6); F4a registers the cache invalidation /
        # ReloadLibrary reaction. Errors are logged, never raised.
        self.library_listeners: list[Callable[[Project, list[str], str], Awaitable[None]]] = []
        # Every CopperChat save rewrites the files: record their hashes right after it,
        # so the turn-start check never takes our own save for an external change.
        sessions.add_save_listener(self._on_saved)

    async def _on_saved(self, project: Project, doc: DocKind) -> None:
        """Re-record the saved document's files (+ the project file KiCad may rewrite);
        the other document's stored hashes stay, so an external change to it is still
        reported at the next turn start. Library, table and presence rows are never
        owned (no document suffix), so they stay as they are."""
        suffix = ".kicad_sch" if doc == "sch" else ".kicad_pcb"
        pro = project.pro_file.relative_to(project.dir).as_posix()

        def owned(rel: str) -> bool:
            return rel == pro or rel.endswith(suffix)

        def owned_hashes() -> dict[str, HashRow]:
            files = [
                TrackedFile(p, rel, False)
                for p in document_files(project)
                if owned(rel := p.relative_to(project.dir).as_posix())
            ]
            return compute_tracked_hashes(files, {})

        now = await asyncio.to_thread(owned_hashes)
        # Only the owned rows are written (one transaction): a row another path stored
        # meanwhile is never overwritten with a stale copy.
        stored = await self.hashes.get(project.id)
        await self.hashes.upsert(project.id, now, delete=[k for k in stored if owned(k)])

    async def on_library_changed(
        self, project: Project, files: Iterable[Path], reason: str = "own"
    ) -> None:
        """CopperChat itself wrote `files` (footprints, the project's fp-lib-table; FP-C6):
        re-record only their rows (fresh hash + stats), leaving every other row as it
        is like `_on_saved`, so the next turn start does not take the own write for an
        external change while changes other projects made to the rest of the library
        still show.
        A file that no longer exists, or is not tracked (no library located, a symlink),
        loses its row - except the project's fp-lib-table with a library located, which
        gets its ABSENT presence row (store.py). The library marker is never written
        here: a library not tracked before stays a silent baseline at the next turn start.
        Then the library listeners get `reason` ("own"; restore's failure path passes
        "restore"; errors logged).

        Never takes `HistoryService.lock` (FP-C6 lock rule): it runs in the `finally` of
        a tool call, and `svc.lock` is always taken first (restore, step checkpoints:
        `svc.lock` -> library lock -> ...), so a holder of `svc.lock` may be waiting for
        this very call; waiting for it here could deadlock. Safe without it: it writes
        the rows of `files` only, in one transaction (`DocumentHashRepo.upsert`; sha
        computed from the file as it is now), and the turn-start check, snapshot and
        restore hash the tracked files themselves. The worst a race with them can cause
        is one extra or one missed report of exactly these files at the next turn start,
        never a stale row for another file."""
        targets = {Path(os.path.relpath(f, project.dir)).as_posix() for f in files}
        if not targets:
            return
        location = await self.locate_library(project)

        def compute() -> dict[str, HashRow]:
            tracked = [f for f in tracked_files(project, location) if f.rel in targets]
            rows = with_presence_rows(compute_tracked_hashes(tracked, {}), project, location)
            return {k: v for k, v in rows.items() if k in targets}

        now = await asyncio.to_thread(compute)
        await self.hashes.upsert(project.id, now, delete=targets)
        await self.notify_library(project, sorted(targets), reason)

    async def notify_library(self, project: Project, rels: list[str], reason: str) -> None:
        """Call every library listener; a failing one is logged, never raised."""
        for listener in list(self.library_listeners):
            try:
                await listener(project, rels, reason)
            except Exception:
                log.exception("library listener failed (%s)", reason)

    async def current_hashes(self, project: Project) -> dict[str, HashRow]:
        """Hashes of every tracked file (documents, fp-lib-table, local library) plus,
        with a library located, its presence rows (marker, absent table; store.py);
        library files with unchanged stats reuse their stored sha (FP-C7)."""
        location = await self.locate_library(project)
        previous = await self.hashes.get_rows(project.id)

        def compute() -> dict[str, HashRow]:
            rows = compute_tracked_hashes(tracked_files(project, location), previous)
            return with_presence_rows(rows, project, location)

        return await asyncio.to_thread(compute)

    def store(self, project: Project) -> HistoryStore:
        return HistoryStore(self.db, project, locate_library=self.locate_library)

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
        self,
        project: Project,
        session: KicadSession,
        docs: set[DocKind],
        *,
        live: bool = False,
    ) -> int:
        """Fresh KiCanvas snapshots for `docs`, then ONE view-updated frame (X2) whose
        `changedFiles` lists the view files that differ from the previous frame (changed,
        added or removed sub-sheets) and whose `viewFiles` is the current list of view
        files per doc (root first, then sorted). `live` (the debounced mid-turn schematic refresh):
        no change -> no frame and no version bump; a schematic with unsaved edits (a
        failed partial edit the turn end throws away) is never saved for it: the write
        raises `DocumentModified` and nothing is published. Every other publish always
        sends, even unchanged - the turn end is the guaranteed final state."""
        docs = docs & self.docs_of(project)
        if not docs:
            return session.view_version

        async def write() -> None:
            for doc in sorted(docs):
                if live:
                    await self.write_view(session, doc, save_modified=False)
                else:
                    await self.write_view(session, doc)

        async with self.view_lock(project):
            return await self._publish_locked(project, session, docs, write, live=live)

    async def publish_disk_view(self, project: Project) -> int | None:
        """The turn end's catch-up after live frames: the schematic view files become
        byte copies of the project's files ON DISK (the last save - what the recovery
        notices say the design is back at), without KiCad (no call, no restart, never a
        save), then ONE non-live frame like `publish_view`. No open session (no version
        source): nothing is written or sent, returns None."""
        if project.sch_file is None:
            return None
        async with self.view_lock(project):
            session = self.open_session(project)
            if session is None:
                return None

            async def write() -> None:
                await asyncio.to_thread(copy_disk_schematic, project)

            return await self._publish_locked(project, session, {"sch"}, write, live=False)

    async def _publish_locked(
        self,
        project: Project,
        session: KicadSession,
        docs: set[DocKind],
        write: Callable[[], Awaitable[None]],
        *,
        live: bool,
    ) -> int:
        """`write` the view files of `docs`, then the frame (under `view_lock`). The
        frame's `view_files` is a full snapshot of every doc in `docs` as the view
        directory holds it after the write (stale sub-sheets are removed by then), sent
        on every frame, not only when the sheet set changed: a client that missed or
        deferred frames, or reconnected, just takes the latest one."""
        known = self._view_hashes.get(project.id)
        if known is None:  # nothing published yet: what is in the view directory now
            all_docs = self.docs_of(project)
            known_now = await asyncio.to_thread(view_file_hashes, project, all_docs)
        else:
            known_now = known
        before = {k: v for k, v in known_now.items() if _of_docs(k, docs)}
        try:
            await write()
        except BaseException:
            # Whatever was written is not what the clients have: the baseline
            # stays, so the next publish still lists it.
            self._view_hashes.setdefault(project.id, known_now)
            raise
        after = await asyncio.to_thread(view_file_hashes, project, docs)
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        if live and not changed:
            return session.view_version
        rest = {k: v for k, v in known_now.items() if not _of_docs(k, docs)}
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
                view_files={d: view_file_list(project, d, after) for d in sorted(docs)},
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
    # The local footprint library (F1 <-> F3 wiring, FP-C6): locator and listener.
    settings = app.state.settings
    svc.locate_library = make_library_locator(settings, app.state.sessions)
    svc.library_listeners.append(make_library_listener(settings, app.state.sessions))
    runtime.hooks_factory = svc.hooks_factory
    app.state.history = svc
    return svc
