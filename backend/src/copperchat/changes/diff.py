"""`kicad-cli sch diff` between a snapshot and the current design (E-C10, spike 2C).

Both sides are full project directories in a temporary directory: "before" is a copy
of the snapshot's files, "after" a fresh `save_copy` of the design as KiCad holds it
(saved first when modified, so sub-sheets and new sheets are current). The JSON is
compacted for the UI: one list per sheet instance path (the diff identifies an item
by sheet instance path + UUID, so an edit on a shared sheet appears once per
instance), boxes in mm, and the property lists of added/removed items cut to names.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import tempfile
import weakref
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from copperchat.changes.erc import copy_snapshot_files
from copperchat.changes.repo import ChangeEntry
from copperchat.errors import StructuredToolError
from copperchat.history.store import Snapshot
from copperchat.kicad_session import KicadUnavailable
from copperchat.kicad_session.launcher import kicad_cli_env, kicad_config_home
from copperchat.kicad_tools.checks import PROJECT_SUPPORT_FILES
from copperchat.projects import Project

if TYPE_CHECKING:
    from copperchat.history.service import HistoryService

DIFF_OK = (0, 5)  # 0 = identical, 5 = differences (spike 2C); anything else failed
_BOX_UNIT = 1e4  # diff boxes are in 100 nm units
_OP_KIND = {"add": "added", "delete": "removed", "modify": "modified"}


def _box_mm(raw: Any) -> dict[str, float] | None:
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    try:
        x, y, w, h = (round(float(v) / _BOX_UNIT, 4) for v in raw)
    except (TypeError, ValueError):
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def _properties(change: dict[str, Any]) -> list[dict[str, Any]]:
    props = [p for p in change.get("properties") or [] if isinstance(p, dict)]
    if change.get("kind") != "modified":
        # Added items list every property, removed ones most of them: noise for the UI.
        return [{"name": str(p.get("name", ""))} for p in props]
    return [
        {"name": str(p.get("name", "")), "before": p.get("before"), "after": p.get("after")}
        for p in props
    ]


def _logged(entries: Iterable[ChangeEntry]) -> dict[str, set[str]]:
    """item UUID -> the diff kinds the tools logged for it as their own change."""
    out: dict[str, set[str]] = {}
    for e in entries:
        kinds = out.setdefault(e.uuid, set())
        if not e.side_effect:
            kinds.add(_OP_KIND.get(e.op, e.op))
    return out


def compact_diff(raw: dict[str, Any], log: Iterable[ChangeEntry] | None = None) -> dict[str, Any]:
    """`{"sheets": {sheet_path: [entry]}, "count": n}`; entry = `{id, kind, type, uuid,
    ref, box_mm, properties, side_effect}`. `side_effect`: no change of that kind was
    logged by a tool for the item (KiCad's own cleanup, e.g. merged wires); always
    False without a `log`."""
    logged = _logged(log) if log is not None else None
    sheets: dict[str, list[dict[str, Any]]] = {}
    count = 0
    for change in raw.get("changes") or []:
        if not isinstance(change, dict) or not isinstance(change.get("id"), str):
            continue
        path, _, uuid = change["id"].rpartition("/")
        kind = str(change.get("kind", ""))
        entry = {
            "id": change["id"],
            "kind": kind,
            "type": str(change.get("typeName", "")),
            "uuid": uuid,
            "ref": change.get("refdes") or None,
            "box_mm": _box_mm(change.get("bbox")),
            "properties": _properties(change),
            "side_effect": logged is not None and kind not in logged.get(uuid, set()),
        }
        sheets.setdefault(path or "/", []).append(entry)
        count += 1
    return {"sheets": sheets, "count": count}


async def _after_copy(svc: HistoryService, project: Project, dest: Path) -> None:
    session = svc.open_session(project)
    if session is None:
        raise KicadUnavailable("the project is not open in KiCad")
    assert project.sch_file is not None
    await session.save_copy("sch", dest / project.sch_file.name)

    def support() -> None:
        root = project.dir.resolve()
        for source in (project.pro_file, *(project.dir / n for n in PROJECT_SUPPORT_FILES)):
            if source.is_file() and source.resolve().is_relative_to(root):
                shutil.copyfile(source, dest / source.name)

    await asyncio.to_thread(support)


async def run_sch_diff(svc: HistoryService, project: Project, snapshot: Snapshot) -> dict[str, Any]:
    """The raw `kicad-cli sch diff --format json` result of snapshot -> current."""
    session = svc.open_session(project)
    if session is None:
        raise KicadUnavailable("the project is not open in KiCad")
    if project.sch_file is None:
        raise StructuredToolError(
            "No schematic diff",
            "the project has no schematic",
            "Diff only projects with a schematic",
            code="invalid_argument",
        )
    settings = session.settings
    sdir = svc.store(project).snapshot_dir(snapshot.id)
    name = project.sch_file.name
    with tempfile.TemporaryDirectory(prefix="cc-diff-") as tmp:
        before, after = Path(tmp) / "before", Path(tmp) / "after"
        await asyncio.to_thread(copy_snapshot_files, sdir, project, before)
        await _after_copy(svc, project, after)
        proc = await asyncio.create_subprocess_exec(
            settings.kicad_cli,
            "sch",
            "diff",
            "--format",
            "json",
            f"before/{name}",
            f"after/{name}",
            cwd=tmp,
            env=kicad_cli_env(settings.kicad_cli, kicad_config_home(settings)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), settings.kicad_api_timeout_s * 4)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise StructuredToolError(
                "Schematic diff timed out",
                "kicad-cli did not finish in time",
                "Retry once",
            ) from None
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(proc.wait())
            raise
    if proc.returncode not in DIFF_OK:
        detail = err.decode(errors="replace").strip()[-500:]
        raise StructuredToolError(
            "Schematic diff failed",
            detail or f"kicad-cli exited with {proc.returncode}",
            "Check that both versions load in KiCad",
        )
    try:
        raw = json.loads(out.decode("utf-8"))
    except ValueError as exc:
        raise StructuredToolError(
            "Schematic diff unreadable",
            f"kicad-cli wrote unexpected output ({exc})",
            "Retry; if it persists the KiCad version changed its diff format",
        ) from exc
    if not isinstance(raw, dict):
        raise StructuredToolError(
            "Schematic diff unreadable",
            "kicad-cli did not write a JSON object",
            "Retry; if it persists the KiCad version changed its diff format",
        )
    return raw


async def diff_against_snapshot(
    svc: HistoryService,
    project: Project,
    snapshot: Snapshot,
    log: Iterable[ChangeEntry] | None = None,
) -> dict[str, Any]:
    """Compact diff of `snapshot` -> current design; `log`: the change rows written
    since the snapshot (entries without one are marked `side_effect`)."""
    return compact_diff(await run_sch_diff(svc, project, snapshot), log)


# One diff per project at a time (each spawns kicad-cli on two project copies) and the
# last result per project, reused while nothing changed: same snapshot, same session
# state (view version / restarts: reloads, restores, turn-end publishes) and no new
# change row (every edit tool writes rows).
_DIFF_LOCKS: dict[str, asyncio.Lock] = {}
# (session weakref, key, result): the session is compared by identity (a freed
# session's id() can be reused by a later one).
_DIFF_CACHE: dict[str, tuple[weakref.ref[Any], tuple[Any, ...], dict[str, Any]]] = {}


async def cached_diff(
    svc: HistoryService,
    project: Project,
    snapshot: Snapshot,
    log: list[ChangeEntry],
) -> dict[str, Any]:
    session = svc.open_session(project)
    if session is None:
        raise KicadUnavailable("the project is not open in KiCad")
    lock = _DIFF_LOCKS.setdefault(project.id, asyncio.Lock())
    async with lock:
        key = (
            snapshot.id,
            session.view_version,
            session.restart_count,
            max((e.id or 0 for e in log), default=0),
            len(log),
        )
        hit = _DIFF_CACHE.get(project.id)
        if hit is not None and hit[0]() is session and hit[1] == key:
            return hit[2]
        result = await diff_against_snapshot(svc, project, snapshot, log)
        _DIFF_CACHE[project.id] = (weakref.ref(session), key, result)
        return result
