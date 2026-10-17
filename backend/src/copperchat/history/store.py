"""Per-turn snapshots of KiCad's own saved files.

Snapshots are byte-for-byte copies of files KiCad wrote (SaveDocument). CopperChat
never parses or edits their content; it only copies bytes and hashes them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from copperchat.db import Database, utcnow_iso
from copperchat.errors import CopperChatError
from copperchat.projects import SKIP_DIRS, Project, agent_dir

HISTORY_KEEP = 50
# CopperChat's caches inside `.kicad-agent/`; kept out of the user's git repository.
GITIGNORE_LINES = ("history/", "view/", "renders/", "uploads/", "images/")
LABEL_MAX = 200
_ID_RE = re.compile(r"[0-9a-f]{32}")


class InvalidSnapshotId(CopperChatError):
    code = "invalid_snapshot_id"
    http_status = 400


class SnapshotNotFound(CopperChatError):
    code = "snapshot_not_found"
    http_status = 404


class CorruptSnapshot(CopperChatError):
    """The snapshot on disk does not match its manifest (edited, truncated, escaping)."""

    code = "corrupt_snapshot"
    http_status = 422


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


def _is_document_name(name: str) -> bool:
    # KiCad GUI autosaves (`_autosave-x.kicad_sch`) and lock/backup names are not documents.
    return name.endswith(".kicad_sch") and not name.startswith(("_autosave-", "~", "."))


def document_files(project: Project) -> list[Path]:
    """The project file, every schematic sheet below the project dir, and the board.

    Pruned from the walk, never descended into: hidden directories (`.git`,
    `.kicad-agent` with its snapshots and view copies), the discovery skip list, and
    sub-directories that hold their own `.kicad_pro` (a separate project).
    """
    files: list[Path] = [project.pro_file]
    sheets: list[Path] = []
    for here, dirs, names in os.walk(project.dir):
        root = Path(here)
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and d not in SKIP_DIRS
            and not (root / d).is_symlink()
            and not any((root / d).glob("*.kicad_pro"))
        ]
        for name in names:
            path = root / name
            if _is_document_name(name) and path.is_file() and not path.is_symlink():
                sheets.append(path)
    files.extend(sorted(sheets))
    if project.pcb_file is not None and project.pcb_file.is_file():
        files.append(project.pcb_file)
    return files


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_hashes(project: Project) -> dict[str, str]:
    """rel posix path -> sha256 of every document file (blocking; see compute_hashes_async)."""
    return {p.relative_to(project.dir).as_posix(): sha256_file(p) for p in document_files(project)}


async def compute_hashes_async(project: Project) -> dict[str, str]:
    return await asyncio.to_thread(compute_hashes, project)


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

    def _copy_documents(self, tmp: Path) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for src in document_files(self.project):
            rel = src.relative_to(self.project.dir)
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            entries.append(
                {
                    "relPath": rel.as_posix(),
                    "sha256": sha256_file(target),
                    "size": target.stat().st_size,
                }
            )
        return entries

    def _sweep_orphans(self, known: set[str]) -> None:
        """Snapshot dirs without a DB row (killed mid-snapshot) and stale temp dirs."""
        if not self.root.is_dir():
            return
        for child in self.root.iterdir():
            if child.is_dir() and not child.is_symlink() and child.name not in known:
                shutil.rmtree(child, ignore_errors=True)

    async def snapshot(self, run_id: str, label: str, preview_png: bytes | None = None) -> Snapshot:
        ensure_gitignore(self.project)
        sid = uuid.uuid4().hex
        dest = self.snapshot_dir(sid)
        tmp = self.root / f".tmp-{sid}"
        label = label[:LABEL_MAX]
        try:
            entries = await asyncio.to_thread(self._copy_documents, tmp)
            created = utcnow_iso()
            if preview_png is not None:
                (tmp / "preview.png").write_bytes(preview_png)
            manifest = {
                "id": sid,
                "projectId": self.project.id,
                "runId": run_id,
                "createdAt": created,
                "label": label,
                "files": entries,
            }
            (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
            tmp.rename(dest)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        files = [e["relPath"] for e in entries]
        try:
            await self._db.execute(
                "INSERT INTO history_snapshots(id, project_id, run_id, created_at, label, files)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (sid, self.project.id, run_id, created, label, json.dumps(files)),
            )
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, dest, True)
            raise
        await self._prune()
        return Snapshot(
            sid, self.project.id, run_id, created, label, files, preview_png is not None
        )

    def _row(self, row: Any) -> Snapshot:
        sid = str(row["id"])
        return Snapshot(
            sid,
            row["project_id"],
            row["run_id"],
            row["created_at"],
            row["label"],
            json.loads(row["files"]),
            (self.root / sid / "preview.png").is_file(),
        )

    async def list(self) -> list[Snapshot]:
        """Newest first."""
        rows = await self._db.fetchall(
            "SELECT * FROM history_snapshots WHERE project_id = ?"
            " ORDER BY created_at DESC, rowid DESC",
            (self.project.id,),
        )
        return [self._row(r) for r in rows]

    async def get(self, snapshot_id: str) -> Snapshot:
        sid = validate_snapshot_id(snapshot_id)
        row = await self._db.fetchone(
            "SELECT * FROM history_snapshots WHERE id = ? AND project_id = ?",
            (sid, self.project.id),
        )
        if row is None or not (self.snapshot_dir(sid) / "manifest.json").is_file():
            raise SnapshotNotFound(f"snapshot {sid} not found")
        return self._row(row)

    async def latest_for_run(self, run_id: str) -> Snapshot | None:
        row = await self._db.fetchone(
            "SELECT * FROM history_snapshots WHERE project_id = ? AND run_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (self.project.id, run_id),
        )
        return None if row is None else self._row(row)

    async def _prune(self) -> None:
        rows = await self._db.fetchall(
            "SELECT id FROM history_snapshots WHERE project_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (self.project.id, HISTORY_KEEP),
        )
        for row in rows:
            await self._db.execute("DELETE FROM history_snapshots WHERE id = ?", (row["id"],))
            await asyncio.to_thread(shutil.rmtree, self.snapshot_dir(row["id"]), True)
        kept = await self._db.fetchall(
            "SELECT id FROM history_snapshots WHERE project_id = ?", (self.project.id,)
        )
        await asyncio.to_thread(self._sweep_orphans, {str(r["id"]) for r in kept})


class DocumentHashRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, project_id: str) -> dict[str, str]:
        rows = await self._db.fetchall(
            "SELECT rel_path, sha256 FROM document_hashes WHERE project_id = ?", (project_id,)
        )
        return {r["rel_path"]: r["sha256"] for r in rows}

    async def replace(self, project_id: str, hashes: dict[str, str]) -> None:
        async with self._db.transaction():
            await self._db.execute(
                "DELETE FROM document_hashes WHERE project_id = ?", (project_id,)
            )
            for rel, digest in sorted(hashes.items()):
                await self._db.execute(
                    "INSERT INTO document_hashes(project_id, rel_path, sha256) VALUES (?, ?, ?)",
                    (project_id, rel, digest),
                )
