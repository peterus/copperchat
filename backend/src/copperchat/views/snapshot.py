"""KiCanvas sources: copies of the open documents as the api-server holds them.

Written to `<project>/.kicad-agent/view/` under the documents' real file names
(hierarchical sub-sheets keep their relative paths), so KiCanvas resolves sheet
references between them (X3). Writing a snapshot never bumps
`KicadSession.view_version`; only the turn-end publish does (X2).
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from copperchat.kicad_session import DocKind, KicadHandle, KicadSession
from copperchat.projects import Project, agent_dir

VIEW_SUFFIXES: dict[DocKind, str] = {"sch": ".kicad_sch", "pcb": ".kicad_pcb"}


def view_dir(project: Project) -> Path:
    return agent_dir(project) / "view"


def snapshot_path(project: Project, doc: DocKind) -> Path:
    """The root document's snapshot (root sheet for `sch`)."""
    return view_dir(project) / f"{project.name}{VIEW_SUFFIXES[doc]}"


def _sheet_files(h: KicadHandle) -> list[str]:
    return [str(s.filename) for s in h.schematic().get_hierarchy() if str(s.filename)]


def _project_has(project: Project, doc: DocKind) -> bool:
    return (project.sch_file if doc == "sch" else project.pcb_file) is not None


async def _write(session: KicadSession, doc: DocKind) -> list[str]:
    await session.save_copy(doc, snapshot_path(session.project, doc))
    files = await view_files(session, doc)
    if doc == "sch":
        _remove_stale_sheets(session.project, files)
    return files


async def write_kicanvas_snapshot(session: KicadSession, doc: DocKind) -> Path:
    """SaveCopyOfDocument (+ sub-sheet copies) into the view directory; returns the root."""
    await _write(session, doc)
    return snapshot_path(session.project, doc)


async def view_files(session: KicadSession, doc: DocKind) -> list[str]:
    """File names KiCanvas loads for `doc`, relative to the view directory, root first."""
    project = session.project
    if not _project_has(project, doc):
        return []
    root = snapshot_path(project, doc).name
    if doc == "pcb":
        return [root]
    sheets = await session.call(_sheet_files)
    others = [s for s in dict.fromkeys(sheets) if s != (project.sch_file or Path()).name]
    return [root, *others]


async def write_view_snapshots(session: KicadSession) -> dict[DocKind, list[str]]:
    """Snapshots of every document the project has (X1); returns the open response's
    `viewFiles` (X3)."""
    out: dict[DocKind, list[str]] = {"sch": [], "pcb": []}
    for doc in ("sch", "pcb"):
        if _project_has(session.project, doc):
            out[doc] = await _write(session, doc)
    return out


def _remove_stale_sheets(project: Project, current: list[str]) -> None:
    """Sub-sheets that left the hierarchy must not linger in the view directory."""
    root = view_dir(project)
    keep = {(root / name).resolve() for name in current}
    for path in root.rglob("*.kicad_sch"):
        if path.resolve() not in keep:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def resolve_view_file(project: Project, name: str) -> Path | None:
    """A snapshot file by its view-relative name; None if it is not a safe, existing one."""
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or not name.endswith(tuple(VIEW_SUFFIXES.values()))
    ):
        return None
    root = view_dir(project).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path == root or not path.is_file():
        return None
    return path
