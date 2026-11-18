"""Undo = restore a snapshot.

This copies back files that KiCad itself wrote (SaveDocument) and that were
copied byte-for-byte into the snapshot. CopperChat never generates, parses or
modifies KiCad file content: this is a file-level version restore, not
hand-editing. The api-server session is closed first and reopened afterwards so
KiCad never holds a stale in-memory document and loads the restored files itself.

v2 snapshots also restore the repo's local footprint library, scoped to this project's
own journalled writes (spec §10, library_restore.py). The library lock is held only
around the library file part, never across the session close/reopen (index call 4);
this project's own library-table rows are removed through the API after the reopen
under the history lock only. A project that is not open keeps its table (call 14).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from copperchat.db.repos.footprints import ABSENT, FootprintWriteRepo
from copperchat.errors import CopperChatError, ReadOnlyProject
from copperchat.history.blobs import blob_lock
from copperchat.history.hooks import changed_rels, is_library_rel
from copperchat.history.library_lock import library_lock
from copperchat.history.library_restore import (
    LibraryRestorePlan,
    apply_library_plan,
    plan_library_restore,
)
from copperchat.history.service import HistoryService
from copperchat.history.store import (
    CorruptSnapshot,
    HistoryStore,
    LibraryLocation,
    Snapshot,
    sha256_file,
)
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.projects import Project

log = logging.getLogger(__name__)


class AgentBusy(CopperChatError):
    code = "agent_busy"
    http_status = 409


def _inside(base: Path, rel: object) -> Path:
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
        raise CorruptSnapshot(f"snapshot manifest has an invalid path: {rel!r}")
    root = base.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or target == root:
        raise CorruptSnapshot(f"snapshot manifest path escapes its root: {rel!r}")
    return target


def _read_manifest(sdir: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((sdir / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise CorruptSnapshot(f"snapshot manifest unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CorruptSnapshot("snapshot manifest is not an object")
    return manifest


def snapshot_plan(sdir: Path, project_dir: Path) -> list[tuple[Path, Path]]:
    """(snapshot file, target below `project_dir`) per manifest entry, each checked
    against its hash and kept inside both roots; CorruptSnapshot otherwise."""
    try:
        entries = _read_manifest(sdir)["files"]
    except KeyError as exc:
        raise CorruptSnapshot(f"snapshot manifest unreadable: {exc}") from exc
    if not isinstance(entries, list) or not entries:
        raise CorruptSnapshot("snapshot manifest lists no files")
    plan: list[tuple[Path, Path]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise CorruptSnapshot("snapshot manifest entry is not an object")
        rel = entry.get("relPath")
        src = _inside(sdir, rel)
        dst = _inside(project_dir, rel)
        if not src.is_file() or sha256_file(src) != entry.get("sha256"):
            raise CorruptSnapshot(f"snapshot file {rel} is missing or corrupted")
        plan.append((src, dst))
    return plan


def _copy_back(plan: list[tuple[Path, Path]]) -> None:
    for src, dst in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".copperchat-restore")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)


def _check_writable(svc: HistoryService, project: Project) -> None:
    session = svc.open_session(project)
    # The manager's lock_status ignores CopperChat's own api-server lock (C7a C3).
    if svc.sessions.lock_status(project).locked or (session is not None and session.read_only):
        raise ReadOnlyProject("project is open in KiCad; close it there before restoring")


async def _reload(
    svc: HistoryService, project: Project, engine: Any, conversation_id: str | None
) -> KicadSession | None:
    """Reopen the project on the files now on disk. A failure is reported, not raised:
    the restore itself already happened (or failed with its own error). Returns the
    fresh session, None if KiCad did not start."""
    try:
        fresh = await svc.sessions.reopen(project)
    except Exception as exc:
        log.warning("reopen after restore failed: %s", exc)
        if engine is not None and conversation_id is not None:
            await engine.post_error(
                conversation_id,
                f"KiCad could not load the restored files ({exc}). Reopen the project.",
                {"code": "kicad_restart_failed"},
            )
        return None
    try:
        await svc.publish_view(project, fresh, svc.docs_of(project))
    except Exception:  # the viewer catches up with the next publish
        log.warning("view refresh after restore failed", exc_info=True)
    return fresh


# --- the library table through the KiCad API (injectable for unit tests) ---------------


def _fp_project_table() -> tuple[int, int]:
    from kipy.proto.common.types import LibraryTableScope, LibraryType

    return int(LibraryType.LT_FOOTPRINT), int(LibraryTableScope.LTS_PROJECT)


async def _read_table_rows(session: KicadSession) -> list[tuple[str, str]] | None:
    """The project's footprint-table rows as (nickname, uri); None if unreadable."""

    def run(h: KicadHandle) -> list[tuple[str, str]]:
        kind, scope = _fp_project_table()
        table: Any = h.kicad.get_library_table(kind, scope)
        return [(str(r.nickname), str(r.uri)) for r in table.rows]

    try:
        return await session.call(run)
    except Exception:
        log.warning("reading the project's footprint table failed", exc_info=True)
        return None


async def _remove_table_rows(session: KicadSession, nicks: list[str]) -> list[tuple[str, str]]:
    """DeleteLibraryTableEntry per nickname (KiCad saves the table); returns the
    (nickname, reason) of every row that could not be removed."""
    failed: list[tuple[str, str]] = []
    for nick in nicks:

        def run(h: KicadHandle, nick: str = nick) -> None:
            kind, scope = _fp_project_table()
            h.kicad.delete_library_table_entry(kind, scope, nick)

        try:
            await session.call(run)
        except Exception as exc:
            log.warning("removing library row %s failed: %s", nick, exc)
            failed.append((nick, str(exc) or type(exc).__name__))
    return failed


# --- the library part of a restore ----------------------------------------------------


@dataclass
class _LibraryOutcome:
    plan: LibraryRestorePlan
    notes: list[str] = field(default_factory=list)
    changed: list[Path] = field(default_factory=list)  # files written or removed


def _wants_library(manifest: dict[str, Any]) -> bool:
    """v1 manifests and a transient library at snapshot time never touch the library."""
    return manifest.get("version") not in (None, 1) and manifest.get("library") is not None


def _fresh_sha(path: Path) -> str:
    try:
        return sha256_file(path)
    except FileNotFoundError:
        return ABSENT


async def _restore_library(
    svc: HistoryService,
    project: Project,
    store: HistoryStore,
    manifest: dict[str, Any],
    location: LibraryLocation,
    table_rows: list[tuple[str, str]] | None,
) -> _LibraryOutcome:
    """Plan and apply the library file part under the library lock (session closed)."""
    max_id = manifest.get("journalMaxId")
    if not isinstance(max_id, int) or isinstance(max_id, bool) or max_id < 0:
        raise CorruptSnapshot(f"snapshot manifest has an invalid journalMaxId: {max_id!r}")
    repo = FootprintWriteRepo(svc.db)
    async with library_lock(location.dir):
        # Another project's library write takes this lock too: the journal and the
        # files are read consistently while it is held.
        journal = await repo.after_id(project.id, max_id)
        foreign = {
            rel: await repo.later_foreign(rel, project.id, max_id)
            for rel in {r.file_rel for r in journal if r.op != "row_add"}
        }

        def plan_and_apply() -> _LibraryOutcome:
            plan = plan_library_restore(
                manifest,
                location,
                project.dir,
                journal,
                lambda rel, _after: foreign.get(rel, False),
                _fresh_sha,
                table_rows,
            )
            notes = apply_library_plan(
                plan, store.blobs.path, location=location, project_dir=project.dir
            )
            changed = [t for _, t in plan.copy]
            changed += [p for p in plan.delete if not p.exists()]
            if plan.remove_table and not location.table.exists():
                changed.append(location.table)
            return _LibraryOutcome(plan, notes, changed)

        async with blob_lock(project.id):  # no blob GC while blobs are copied back
            return await asyncio.to_thread(plan_and_apply)


def _rel(path: Path, project: Project) -> str:
    return Path(os.path.relpath(path, project.dir)).as_posix()


async def _notify_listeners(svc: HistoryService, project: Project, rels: list[str]) -> None:
    for listener in list(svc.library_listeners):
        try:
            await listener(project, rels, "restore")
        except Exception:
            log.warning("library listener failed after restore", exc_info=True)


async def restore_snapshot(
    svc: HistoryService,
    project: Project,
    snapshot_id: str,
    *,
    conversation_id: str | None,
) -> Snapshot:
    """The restore notice goes to `conversation_id` (the session the user restored from,
    already ownership-checked by the caller); the agent note is broadcast to every open
    session (spec §5.3)."""
    store = svc.store(project)
    snap = await store.get(snapshot_id)
    engine = svc.runtime.get(project.id)
    async with svc.lock(project):
        _check_writable(svc, project)
        if engine is not None and engine.is_busy:
            raise AgentBusy("the agent is working; stop it or wait before restoring")
        sdir = store.snapshot_dir(snap.id)
        plan = await asyncio.to_thread(snapshot_plan, sdir, project.dir)
        manifest = await asyncio.to_thread(_read_manifest, sdir)
        location = await svc.locate_library(project) if _wants_library(manifest) else None
        session = svc.open_session(project)
        table_rows: list[tuple[str, str]] | None = None
        if location is not None and session is not None:
            table_rows = await _read_table_rows(session)
        if session is not None:
            await session.close()  # KiCad must not keep the replaced documents in memory
        library: _LibraryOutcome | None = None
        try:
            if location is not None:
                library = await _restore_library(
                    svc, project, store, manifest, location, table_rows
                )
            await asyncio.to_thread(_copy_back, plan)
            stored = await svc.hashes.get(project.id)
            current = await svc.current_hashes(project)
            await svc.hashes.replace(project.id, current)
        except BaseException:
            if session is not None:  # never leave the closed session as the current one
                await _reload(svc, project, engine, conversation_id)
            raise
        # The files are restored from here on, even if KiCad fails to start below.
        fresh = None
        if session is not None:
            # A project that is not open needs no reload: opening it reads the files.
            fresh = await _reload(svc, project, engine, conversation_id)
        extras: list[str] = []
        if library is not None and location is not None:
            extras = await _finish_library(svc, project, library, location, fresh)
        # Re-recording every library hash above must not absorb a change another project
        # made since the last turn start to a footprint this restore left alone.
        touched = set() if library is None else {_rel(p, project) for p in library.changed}
        now = {k: r.sha256 for k, r in current.items()}
        external = [k for k in changed_rels(stored, now) if is_library_rel(k) and k not in touched]
        if external:
            stems = ", ".join(sorted({PurePosixPath(k).stem for k in external}))
            extras.append(f"Footprints changed outside this project: {stems}")
            await svc.notify_library(project, external, "external")
        if engine is not None and conversation_id is not None:
            text = f"Restored the design to the snapshot from {snap.created_at} ({snap.label})."
            if extras:
                text += "\n" + "\n".join(f"- {line}" for line in extras)
            await engine.post_notice(
                conversation_id, text, {"action": "restored", "snapshotId": snap.id}
            )
        if engine is not None:
            engine.add_agent_note(
                f"The user restored snapshot {snap.id} ({snap.label}). Design data from "
                "earlier tool results in this conversation may be stale; read it again "
                "before relying on it."
            )
    return snap


async def _finish_library(
    svc: HistoryService,
    project: Project,
    library: _LibraryOutcome,
    location: LibraryLocation,
    fresh: KicadSession | None,
) -> list[str]:
    """After the reopen (history lock only): remove this project's own table rows,
    re-record the hashes, tell the listeners. Returns the notice lines."""
    plan = library.plan
    lines: list[str] = []
    if plan.refused is not None:
        lines.append(plan.refused)
    lines += plan.skipped
    lines += library.notes
    changed = [_rel(p, project) for p in library.changed]
    if plan.remove_rows:
        if fresh is None:
            failed = [(n, "KiCad did not restart") for n in plan.remove_rows]
        else:
            failed = await _remove_table_rows(fresh, list(plan.remove_rows))
        lines += [f"library row {nick} not removed ({reason})" for nick, reason in failed]
        if len(failed) < len(plan.remove_rows):
            changed.append(_rel(location.table, project))
            # The table changed: never report our own row removal as an external change.
            await svc.hashes.replace(project.id, await svc.current_hashes(project))
    if changed:
        await _notify_listeners(svc, project, sorted(set(changed)))
    return lines
