"""Undo = restore a snapshot.

This copies back files that KiCad itself wrote (SaveDocument) and that were
copied byte-for-byte into the snapshot. CopperChat never generates, parses or
modifies KiCad file content: this is a file-level version restore, not
hand-editing. The api-server session is closed first and reopened afterwards so
KiCad never holds a stale in-memory document and loads the restored files itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from copperchat.errors import CopperChatError, ReadOnlyProject
from copperchat.history.service import HistoryService
from copperchat.history.store import CorruptSnapshot, Snapshot, compute_hashes_async, sha256_file
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


def _plan(sdir: Path, project_dir: Path) -> list[tuple[Path, Path]]:
    try:
        manifest = json.loads((sdir / "manifest.json").read_text())
        entries = manifest["files"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
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


async def _reload(svc: HistoryService, project: Project, engine: Any) -> None:
    """Reopen the project on the files now on disk. A failure is reported, not raised:
    the restore itself already happened (or failed with its own error)."""
    try:
        fresh = await svc.sessions.reopen(project)
    except Exception as exc:
        log.warning("reopen after restore failed: %s", exc)
        if engine is not None:
            await engine.post_error(
                f"KiCad could not load the restored files ({exc}). Reopen the project.",
                {"code": "kicad_restart_failed"},
            )
        return
    try:
        await svc.publish_view(project, fresh, svc.docs_of(project))
    except Exception:  # the viewer catches up with the next publish
        log.warning("view refresh after restore failed", exc_info=True)


async def restore_snapshot(svc: HistoryService, project: Project, snapshot_id: str) -> Snapshot:
    store = svc.store(project)
    snap = await store.get(snapshot_id)
    engine = svc.runtime.get(project.id)
    async with svc.lock(project):
        _check_writable(svc, project)
        if engine is not None and engine.is_busy:
            raise AgentBusy("the agent is working; stop it or wait before restoring")
        plan = await asyncio.to_thread(_plan, store.snapshot_dir(snap.id), project.dir)
        session = svc.open_session(project)
        if session is not None:
            await session.close()  # KiCad must not keep the replaced documents in memory
        try:
            await asyncio.to_thread(_copy_back, plan)
            await svc.hashes.replace(project.id, await compute_hashes_async(project))
        except BaseException:
            if session is not None:  # never leave the closed session as the current one
                await _reload(svc, project, engine)
            raise
        # The files are restored from here on, even if KiCad fails to start below.
        if engine is not None:
            await engine.post_notice(
                f"Restored the design to the snapshot from {snap.created_at} ({snap.label}).",
                {"action": "restored", "snapshotId": snap.id},
            )
            engine.add_agent_note(
                f"The user restored snapshot {snap.id} ({snap.label}). Design data from "
                "earlier tool results in this conversation may be stale; read it again "
                "before relying on it."
            )
        if session is not None:
            # A project that is not open needs no reload: opening it reads the files.
            await _reload(svc, project, engine)
    return snap
