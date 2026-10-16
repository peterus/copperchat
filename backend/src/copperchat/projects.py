from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from copperchat.errors import InvalidProjectId, ProjectNotFound

PROJECT_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_SKIP_DIRS = frozenset({".kicad-agent", "node_modules", "__pycache__"})


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    rel_path: str
    dir: Path
    pro_file: Path
    sch_file: Path | None
    pcb_file: Path | None


@dataclass(frozen=True)
class LockStatus:
    locked: bool
    lock_files: tuple[Path, ...]


def project_id_for(rel_path: str) -> str:
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:12]


def _make_project(root: Path, pro: Path) -> Project:
    rel = pro.relative_to(root).as_posix()
    stem = pro.stem
    sch = pro.with_name(f"{stem}.kicad_sch")
    pcb = pro.with_name(f"{stem}.kicad_pcb")
    return Project(
        id=project_id_for(rel),
        name=stem,
        rel_path=rel,
        dir=pro.parent,
        pro_file=pro,
        sch_file=sch if sch.is_file() else None,
        pcb_file=pcb if pcb.is_file() else None,
    )


def discover_projects(root: Path) -> list[Project]:
    root = root.resolve()
    found: list[Project] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".") and d not in _SKIP_DIRS and not (here / d).is_symlink()
        )
        for name in sorted(filenames):
            path = here / name
            if name.endswith(".kicad_pro") and not name.startswith(".") and not path.is_symlink():
                found.append(_make_project(root, path))
    return sorted(found, key=lambda p: p.rel_path)


def get_project(root: Path, project_id: str) -> Project:
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise InvalidProjectId(f"invalid project id {project_id!r}")
    resolved_root = root.resolve()
    for project in discover_projects(resolved_root):
        if project.id == project_id:
            if not project.dir.resolve().is_relative_to(resolved_root):
                raise ProjectNotFound(f"project {project_id} is outside the projects root")
            return project
    raise ProjectNotFound(f"no project with id {project_id}")


def lock_file_for(path: Path) -> Path:
    """KiCad's lock file for `path`: `~<name>.lck` in the same directory."""
    return path.with_name(f"~{path.name}.lck")


def read_lock_token(lock: Path) -> str | None:
    """The `token` of a KiCad lock file (JSON `{hostname, token, username}`), if readable."""
    try:
        data = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = data.get("token") if isinstance(data, dict) else None
    return token if isinstance(token, str) and token else None


def lock_status(project: Project, ignore_tokens: Collection[str] = ()) -> LockStatus:
    """Lock files held by another KiCad instance.

    KiCad 11 writes `~STEM.kicad_pro.lck` (spike 1C); `~FILE.lck` next to the
    schematic and board are checked too. Locks whose token is in `ignore_tokens`
    (written by CopperChat's own api-server, possibly left behind by a crash) do
    not count.
    """
    candidates = [f for f in (project.pro_file, project.sch_file, project.pcb_file) if f]
    locks = tuple(
        lock.resolve()
        for f in candidates
        if (lock := lock_file_for(f)).exists()
        and (not ignore_tokens or read_lock_token(lock) not in ignore_tokens)
    )
    return LockStatus(locked=bool(locks), lock_files=locks)


def agent_dir(project: Project) -> Path:
    return project.dir / ".kicad-agent"
