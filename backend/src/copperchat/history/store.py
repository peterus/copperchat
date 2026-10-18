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
from copperchat.plans.repo import PlanRepo
from copperchat.projects import SKIP_DIRS, Project, agent_dir

HISTORY_KEEP = 50  # turn snapshots; step checkpoints are pruned with their turn (§5.4)
# Newest plan-step checkpoints kept per project (E8b: a long plan, revisions and
# rollbacks within a few turns would otherwise grow them without bound). Snapshots an
# open plan can still roll back to are never pruned.
STEP_KEEP = 100
SNAPSHOT_KINDS = ("turn", "step")
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
    kind: str = "turn"  # turn | step (plan-step checkpoint, E-C10)
    plan_step_id: str | None = None


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

    async def snapshot(
        self,
        run_id: str,
        label: str,
        preview_png: bytes | None = None,
        *,
        kind: str = "turn",
        plan_step_id: str | None = None,
    ) -> Snapshot:
        if kind not in SNAPSHOT_KINDS:
            raise ValueError(f"unknown snapshot kind {kind!r}")
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
                "kind": kind,
                "planStepId": plan_step_id,
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
                "INSERT INTO history_snapshots(id, project_id, run_id, created_at, label, files,"
                " kind, plan_step_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    self.project.id,
                    run_id,
                    created,
                    label,
                    json.dumps(files),
                    kind,
                    plan_step_id,
                ),
            )
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, dest, True)
            raise
        await self._prune()
        return Snapshot(
            sid,
            self.project.id,
            run_id,
            created,
            label,
            files,
            preview_png is not None,
            kind,
            plan_step_id,
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
            str(row["kind"]),
            row["plan_step_id"],
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

    async def _latest(self, run_id: str, kind: str) -> Snapshot | None:
        row = await self._db.fetchone(
            "SELECT * FROM history_snapshots WHERE project_id = ? AND run_id = ? AND kind = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (self.project.id, run_id, kind),
        )
        return None if row is None else self._row(row)

    async def latest_for_run(self, run_id: str) -> Snapshot | None:
        """The run's turn snapshot (its undo point); step checkpoints never count."""
        return await self._latest(run_id, "turn")

    async def latest_step_checkpoint(self, run_id: str) -> Snapshot | None:
        return await self._latest(run_id, "step")

    async def _prune(self) -> None:
        """HISTORY_KEEP turn snapshots; step checkpoints go with their turn (same run),
        and so do those of other runs older than the oldest kept turn snapshot (a run
        whose steps changed nothing has no turn snapshot of its own); at most STEP_KEEP
        step checkpoints. An open plan's base snapshot and step checkpoints are kept."""
        turns = await self._db.fetchall(
            "SELECT id, run_id, created_at, rowid AS rid FROM history_snapshots"
            " WHERE project_id = ? AND kind = 'turn'"
            " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (self.project.id, HISTORY_KEEP - 1),
        )
        if len(turns) > 1:
            oldest_kept, pruned = turns[0], turns[1:]
            runs = sorted({str(r["run_id"]) for r in pruned})
            marks = ", ".join("?" for _ in runs)
            rows = list(pruned) + list(
                await self._db.fetchall(
                    "SELECT id FROM history_snapshots WHERE project_id = ? AND kind = 'step'"
                    f" AND (run_id IN ({marks}) OR (run_id != ? AND (created_at < ?"
                    " OR (created_at = ? AND rowid < ?))))",
                    (
                        self.project.id,
                        *runs,
                        oldest_kept["run_id"],
                        oldest_kept["created_at"],
                        oldest_kept["created_at"],
                        oldest_kept["rid"],
                    ),
                )
            )
        else:
            rows = []
        rows += await self._db.fetchall(
            "SELECT id FROM history_snapshots WHERE project_id = ? AND kind = 'step'"
            " ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (self.project.id, STEP_KEEP),
        )
        protected = await PlanRepo(self._db).protected_snapshot_ids(self.project.id)
        doomed = list(dict.fromkeys(str(r["id"]) for r in rows if str(r["id"]) not in protected))
        for sid in doomed:
            await self._db.execute("DELETE FROM history_snapshots WHERE id = ?", (sid,))
            await asyncio.to_thread(shutil.rmtree, self.snapshot_dir(sid), True)
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
