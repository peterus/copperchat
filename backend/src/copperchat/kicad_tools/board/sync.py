"""`pcb_sync_from_schematic` (spec §7.2 "Board from schematic", §6.3): the schematic's
netlist into the board, new footprints into the staging area.

The netlist comes from a fresh `kicad-cli sch export netlist` of the saved schematic
(spike G2: the api-server's own netlist job reads a stale copy). A dry run on the board
comes first; only a clean one is imported. The real import is the one board write that
cannot be reverted item by item (`compensable=False`): a failure after it raises
`sync_failed` naming the History panel. A boardless project gets its board created by
the engine first (`creates_board`), which removes it again on any failure.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from kipy.geometry import Vector2

from copperchat.board.model import BoardModel
from copperchat.board.staging import Move, natural_key, part_box, staging_positions
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.board.read import BOARD_READ_TIMEOUT_S, board_model
from copperchat.kicad_tools.board.setup import require_board_tools
from copperchat.kicad_tools.cli import run_kicad_cli
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical
from copperchat.kicad_tools.units import mm_to_nm
from copperchat.projects import agent_dir
from copperchat.tools.spec import ToolContext, ToolResult

NETLIST_TIMEOUT_S = 120.0
# ImportNetlist resolves every footprint from its library: seconds on a big board.
IMPORT_TIMEOUT_S = 300.0
MAX_WARNINGS = 50
STDERR_TAIL = 2000

SYNC_COST = 3


# -- report ----------------------------------------------------------------------------

_PAD_LINE = re.compile(r"\bpad\b|\bpin\b", re.I)
_REF_IN_LINE = re.compile(r"\b([A-Z]+[A-Za-z_]*\d+)\b")


def _severity_lines(report: str, word: str) -> list[str]:
    return [ln.strip() for ln in report.splitlines() if ln.strip().lower().startswith(word)]


def warning_rows(report: str, refs: set[str]) -> list[dict[str, Any]]:
    """KiCad's warning lines of an import report; a line about a pad or pin that names a
    footprint of the board gets the fp_check suggestion (a footprint that does not fit
    its symbol)."""
    rows: list[dict[str, Any]] = []
    for line in _severity_lines(report, "warning"):
        row: dict[str, Any] = {"text": line}
        if _PAD_LINE.search(line):
            named = [r for r in _REF_IN_LINE.findall(line) if r in refs]
            if named:
                row["ref"] = named[0]
                row["suggestion"] = f"check the footprint of {named[0]} with fp_check"
        rows.append(row)
    return rows


def _nets(m: BoardModel | None) -> set[str]:
    return {n for n, _ in m.net_class_of} if m is not None else set()


def diff_report(before: BoardModel | None, after: BoardModel) -> dict[str, Any]:
    """added / removed / changed refs (changed = value or lib_id differs) and the nets
    that appeared or went away."""
    old = {p.ref: p for p in before.parts} if before is not None else {}
    new = {p.ref: p for p in after.parts}

    def refs(keys: set[str]) -> list[str]:
        return sorted(keys, key=natural_key)

    changed = {
        r
        for r in old.keys() & new.keys()
        if (old[r].value, old[r].lib_id) != (new[r].value, new[r].lib_id)
    }
    return {
        "added": refs(new.keys() - old.keys()),
        "removed": refs(old.keys() - new.keys()),
        "changed": refs(changed),
        "nets_added": sorted(_nets(after) - _nets(before)),
        "nets_removed": sorted(_nets(before) - _nets(after)),
    }


def staging_box(model: BoardModel, moves: tuple[Move, ...]) -> dict[str, float] | None:
    """The box of the staged parts' courtyards (their pads/origin without one)."""
    boxes = []
    by_ref = {p.ref: p for p in model.parts}
    for mv in moves:
        p = by_ref[mv.ref]
        x0, y0, x1, y1 = part_box(p)
        dx, dy = mv.x - p.x, mv.y - p.y
        boxes.append((x0 + dx, y0 + dy, x1 + dx, y1 + dy))
    if not boxes:
        return None
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    return {"x": round(x0, 3), "y": round(y0, 3), "w": round(x1 - x0, 3), "h": round(y1 - y0, 3)}


# -- netlist ---------------------------------------------------------------------------


def _netlist_dir(ctx: ToolContext) -> Path:
    """A fresh scratch dir for this call's netlist (removed by the engine / prepare)."""
    base = agent_dir(ctx.project) / "board"
    base.mkdir(parents=True, exist_ok=True)
    d = base / f"sync-{uuid.uuid4().hex}"
    d.mkdir()
    return d


async def export_netlist(ctx: ToolContext, dest: Path) -> None:
    """`kicad-cli sch export netlist` of the saved root schematic (G2: never the API job)."""
    sch = ctx.project.sch_file
    if sch is None:
        raise StructuredToolError(
            "Cannot sync the board",
            f"the project {ctx.project.name} has no schematic",
            "Open a project with a schematic.",
            code="sync_failed",
        )
    args = ["sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(dest), str(sch)]
    try:
        res = await run_kicad_cli(ctx.settings, args, timeout=NETLIST_TIMEOUT_S)
    except TimeoutError as exc:
        raise StructuredToolError(
            "Cannot sync the board",
            f"the netlist export did not finish within {NETLIST_TIMEOUT_S:.0f} s",
            "Retry; if it repeats, tell the user.",
            code="sync_failed",
        ) from exc
    if res.returncode != 0 or not dest.is_file():
        tail = (res.stderr or res.stdout or "")[-STDERR_TAIL:]
        raise StructuredToolError(
            "Cannot sync the board",
            f"kicad-cli could not export the netlist (exit {res.returncode}): {tail}",
            "Fix the schematic (run ERC), save it, then retry.",
            code="sync_failed",
            details={"stderr": tail},
        )


def _netlist_refs(path: Path) -> list[str]:
    refs = set(re.findall(r'\(comp\s+\(ref\s+"([^"]+)"\)', path.read_text(errors="replace")))
    return sorted(refs, key=natural_key)


# -- import ----------------------------------------------------------------------------


def _import(h: KicadHandle, netlist: Path, *, dry_run: bool, delete_extra: bool) -> dict[str, Any]:
    res = h.board().import_netlist(
        str(netlist), dry_run=dry_run, delete_extra_footprints=delete_extra
    )
    return {
        "errors": int(res.error_count),
        "warnings": int(res.warning_count),
        "new": int(res.new_footprint_count),
        "report": str(res.report),
    }


def _dry_run_failed(result: dict[str, Any]) -> StructuredToolError:
    lines = _severity_lines(result["report"], "error")[:MAX_WARNINGS]
    return StructuredToolError(
        "Cannot sync the board",
        f"KiCad's dry run of the netlist import reports {result['errors']} error(s): "
        + "; ".join(lines[:6]),
        "Fix the schematic (missing or unknown footprints), save it, then retry. "
        "Nothing was written.",
        code="sync_failed",
        details={"errors": lines},
    )


async def _dry_run(ctx: ToolContext, netlist: Path, delete_extra: bool) -> dict[str, Any]:
    result: dict[str, Any] = await ctx.session.call(
        lambda h: _import(h, netlist, dry_run=True, delete_extra=delete_extra),
        timeout=IMPORT_TIMEOUT_S,
    )
    if result["errors"] > 0:
        raise _dry_run_failed(result)
    return result


def _move_footprints(h: KicadHandle, moves: dict[str, Move], kiids: set[str]) -> None:
    board = h.board()
    changed = []
    for fp in board.get_footprints():
        kiid = str(fp.id.value)
        mv = moves.get(str(fp.reference_field.text.value))
        if kiid in kiids and mv is not None:
            fp.position = Vector2.from_xy(mm_to_nm(mv.x), mm_to_nm(mv.y))
            changed.append(fp)
    if changed:
        board.update_items(changed)


# -- tool --------------------------------------------------------------------------------


async def _schematic_unsaved(ctx: ToolContext) -> bool:
    if ctx.session.modified:
        return True
    return bool(await ctx.session.call(lambda h: h.schematic().is_document_modified()))


async def prepare_sync(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    require_board_tools(ctx)
    delete_extra = bool(args.get("delete_extra", True))
    dry_run = bool(args.get("dry_run", False))
    if await _schematic_unsaved(ctx):
        raise StructuredToolError(
            "Cannot sync the board",
            "the schematic has unsaved changes; the netlist is exported from the saved files",
            "Save the schematic first (the turn's edits are saved after each edit; retry "
            "after the current edit finished).",
            code="schematic_unsaved",
        )
    work = await asyncio.to_thread(_netlist_dir, ctx)
    try:
        netlist = work / "netlist.net"
        await export_netlist(ctx, netlist)
        has_board = ctx.project.pcb_file is not None
        before = await board_model(ctx) if has_board else None
        dry = await _dry_run(ctx, netlist, delete_extra) if has_board else None
        if dry_run:
            report = await _dry_run_report(ctx, netlist, before, dry)
            await asyncio.to_thread(shutil.rmtree, work, True)
            return BoardChangeSet(
                tool="pcb_sync_from_schematic",
                logical=Logical(0),
                kind="board_sync_from_schematic",
                board_stages=[],
                report=report,
            )
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, work, True)
        raise
    return _plan(ctx, netlist, work, before, delete_extra)


async def _dry_run_report(
    ctx: ToolContext, netlist: Path, before: BoardModel | None, dry: dict[str, Any] | None
) -> dict[str, Any]:
    if dry is None:  # no board yet: what the sync would create
        new_refs = await asyncio.to_thread(_netlist_refs, netlist)
        return {
            "dry_run": True,
            "creates_board": True,
            "added": new_refs,
            "footprints": len(new_refs),
            "warnings": [],
        }
    refs = {p.ref for p in before.parts} if before is not None else set()
    return {
        "dry_run": True,
        "creates_board": False,
        "new_footprints": dry["new"],
        "error_count": dry["errors"],
        "warning_count": dry["warnings"],
        "warnings": warning_rows(dry["report"], refs)[:MAX_WARNINGS],
        "report": dry["report"].splitlines()[:200],
    }


def _plan(
    ctx: ToolContext, netlist: Path, work: Path, before: BoardModel | None, delete_extra: bool
) -> BoardChangeSet:
    creates = ctx.project.pcb_file is None
    pre_kiids = {p.kiid for p in before.parts} if before is not None else set()
    state: dict[str, Any] = {}

    async def dry_run_new_board(sctx: BoardStageContext) -> None:
        await _dry_run(sctx.ctx, netlist, delete_extra)

    async def real_import(sctx: BoardStageContext) -> None:
        state["import"] = await sctx.call(
            lambda h: _import(h, netlist, dry_run=False, delete_extra=delete_extra),
            timeout=IMPORT_TIMEOUT_S,
        )
        sctx.ctx.session.modified_docs.add("pcb")

    async def stage_new(sctx: BoardStageContext) -> None:
        model = await sctx.model()
        new = {p.kiid for p in model.parts} - pre_kiids
        refs = [p.ref for p in model.parts if p.kiid in new]
        moves = staging_positions(model, refs)
        by_ref = {mv.ref: mv for mv in moves}
        if moves:
            async with sctx.ctx.session.commit("copperchat: pcb_sync_from_schematic", doc="pcb"):
                await sctx.call(
                    lambda h: _move_footprints(h, by_ref, new), timeout=BOARD_READ_TIMEOUT_S
                )
        after = await sctx.model()
        imp = state.get("import", {"report": ""})
        sctx.report.update(diff_report(before, after))
        sctx.report.update(
            {
                "warnings": warning_rows(imp["report"], {p.ref for p in after.parts})[
                    :MAX_WARNINGS
                ],
                "footprints": len(after.parts),
                "unplaced": sum(1 for p in after.parts if not after.placed(p)),
                "staging_box": staging_box(model, moves),
                "board_created": creates,
                "next": "pcb_set_outline",
            }
        )

    stages = []
    if creates:
        stages.append(BoardStage("dry run on the new board", dry_run_new_board, commit=False))
    stages += [
        BoardStage("import netlist", real_import, commit=False, compensable=False),
        BoardStage("stage new footprints", stage_new, commit=False),
    ]
    return BoardChangeSet(
        tool="pcb_sync_from_schematic",
        logical=Logical(SYNC_COST),
        kind="board_sync_from_schematic",
        board_stages=stages,
        creates_board=creates,
        temp_dirs=[work],
    )


async def apply_sync(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    assert isinstance(cs, BoardChangeSet)
    if cs.report.get("dry_run"):
        return ToolResult(data=dict(cs.report))  # read-only: nothing written, cost 0
    return await apply_board_change_set(ctx, cs)
