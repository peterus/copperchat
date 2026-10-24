"""Sheet tools (E7): `sch_add_sheet` and `sch_add_sheet_pin` (two-phase)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from copperchat.errors import DocumentInconsistent, StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit import sheets
from copperchat.kicad_tools.edit.changeset import ChangeSet
from copperchat.kicad_tools.edit.engine import apply_change_set, edit_result
from copperchat.kicad_tools.edit.scene import sheet_api
from copperchat.tools.spec import ToolContext, ToolResult, ToolSpec, edit_spec

log = logging.getLogger(__name__)


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


_XY = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}


# -- sch_add_sheet ------------------------------------------------------------------------


async def prepare_add_sheet(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    return await ctx.session.call(sheets.plan_add_sheet(ctx.project, args))


async def _shielded(coro: Any) -> None:
    """Run `coro` to completion even when the caller is cancelled meanwhile; a pending
    cancellation is re-raised afterwards."""
    task = asyncio.ensure_future(coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    if cancelled and not task.cancelled() and task.exception() is None:
        raise asyncio.CancelledError
    await task


async def _remove_failed_sheet(ctx: ToolContext, cs: ChangeSet, uuid: str, new_file: Path,
                               made_dir: bool) -> None:  # fmt: skip
    """The save failed after the sheet was verified: take the sheet symbol out again, drop
    the file this call may have created, and save the root once more (a failed save can
    leave the root on disk naming a sheet without a file)."""
    session = ctx.session
    sheet = cs.sheet

    def remove(h: KicadHandle) -> None:
        from kipy.proto.common.types import KIID

        k = KIID()
        k.value = uuid
        sheet_api(h, sheet).remove_items_by_id([k])

    try:
        async with session.commit("sch_add_sheet: undo"):
            await session.call(remove)
        new_file.unlink(missing_ok=True)
        if made_dir:
            with contextlib.suppress(OSError):
                new_file.parent.rmdir()
        await session.save("sch")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("undoing sch_add_sheet after a failed save failed: %s", exc)
        raise DocumentInconsistent(
            "sch_add_sheet could not be undone after its save failed"
        ) from exc


async def apply_add_sheet(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    session = ctx.session
    path = Path(ctx.project.dir) / str(cs.summary["file"])
    if path.exists() or path.is_symlink() or path.parent.is_symlink():
        # Appeared since prepare: never write over it, and the rollback must only ever
        # remove a file this call created.
        raise StructuredToolError(
            "Sheet file already exists",
            f"{cs.summary['file']} appeared after the check; it would not be overwritten",
            "Choose a new file name.",
            code="invalid_argument",
        )
    made_dir = not path.parent.exists()
    # Record the change rows only after the file is on disk: a failed save is rolled back
    # and must not leave an "added sheet" row.
    applied = await apply_change_set(ctx, cs, record=False)
    sheet_symbol = applied.created[0]
    uuid = access.kiid(sheet_symbol)
    try:
        await session.save("sch")
    except BaseException as exc:
        await _shielded(_remove_failed_sheet(ctx, cs, uuid, path, made_dir))
        if isinstance(exc, Exception):
            log.warning("sch_add_sheet: saving the new sheet failed: %s", exc)
            raise StructuredToolError(
                "sch_add_sheet failed: KiCad could not save the new sheet",
                "KiCad refused to save the schematic with the new sheet",
                "Nothing was changed; check the file name and retry.",
                code="invalid_argument",
            ) from exc
        raise
    if ctx.editing is not None:
        try:
            await ctx.editing.record_changes(ctx, applied.changes)
        except Exception:  # applied and saved: never fail it afterwards
            log.exception("recording changes of sch_add_sheet failed")

    name = str(cs.summary["name"])
    parent_human = str(cs.summary["parent"])
    human = f"{parent_human}{name}/"

    def info(h: KicadHandle) -> dict[str, str]:
        inst = next(
            (i for i in h.schematic().get_hierarchy() if str(i.path.path_human_readable) == human),
            None,
        )
        return {"page": str(inst.page_number) if inst is not None else ""}

    page = (await session.call(info))["page"]
    summary = {
        "name": name,
        "file": cs.summary["file"],
        "path": human,
        "page": page,
        "uuid": uuid,
    }
    cs.summary = {"sheet": summary}
    return edit_result(cs, applied)


# -- sch_add_sheet_pin --------------------------------------------------------------------


async def prepare_add_sheet_pin(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    return await ctx.session.call(sheets.plan_add_sheet_pin(args))


async def apply_add_sheet_pin(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    applied = await apply_change_set(ctx, cs)
    pin = dict(cs.summary["pin"])
    label = cs.summary["label"]
    sheet_symbol = applied.modified[0]
    found = next((p for p in sheet_symbol.pins if str(p.text.value) == pin["name"]), None)
    if found is not None:
        pin["uuid"] = access.kiid(found)
    out: dict[str, Any] = {"pin": pin, "label": None}
    if label is not None:
        created = [c for c in applied.created if type(c).__name__ == "HierarchicalLabel"]
        out["label"] = {**label, "uuid": access.kiid(created[0]) if created else None}
    cs.summary = out
    return edit_result(cs, applied)


TOOL_SPECS: list[ToolSpec] = [
    edit_spec(
        "sch_add_sheet",
        "Create a hierarchical sub-sheet: a sheet symbol on `parent` (default root) and its "
        "new file `file` (project-relative '*.kicad_sch', at most one directory level; the "
        "file must not exist yet). The new sheet gets the next page number. `at` is its top-left "
        "corner in mm (snapped to the grid), `size` [w, h] defaults to [50.8, 38.1]. Refused "
        "when the area is occupied or the name is taken. The file is written when the tool "
        "returns. Budget: 1 logical change. Then add pins with sch_add_sheet_pin and place parts "
        "on the new sheet by passing its path as `sheet`.",
        _obj(
            {
                "name": {"type": "string", "maxLength": 64, "description": "Sheet name."},
                "file": {
                    "type": "string",
                    "maxLength": 100,
                    "description": "Relative file like 'power.kicad_sch' or 'sub/power.kicad_sch'.",
                },
                "at": {**_XY, "description": "Top-left corner [x, y] in mm."},
                "size": {**_XY, "description": "[width, height] in mm, default [50.8, 38.1]."},
                "parent": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "Sheet to put the symbol on: path like '/amp/' or page; "
                    "default root.",
                },
            },
            ["name", "file", "at"],
        ),
        prepare_add_sheet,
        apply_add_sheet,
    ),
    edit_spec(
        "sch_add_sheet_pin",
        "Add a pin to a sheet symbol together with the matching hierarchical label inside "
        "the sub-sheet (same name and direction), so a wire on the pin connects to the label's "
        "net. `sheet` is the sub-sheet (name, path like '/Power/' or page). The pin goes on the "
        "sheet edge (`side`, default left; outputs right), stacked 2.54 mm apart on the grid. "
        "Refused when a pin of that name exists. On a sheet file used twice the label exists "
        "once; the pin is added to the chosen sheet symbol only. Budget: 1 logical change.",
        _obj(
            {
                "sheet": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "The sub-sheet: name, path like '/Power/', or page number.",
                },
                "name": {"type": "string", "maxLength": 64, "description": "Pin / net name."},
                "direction": {"enum": list(sheets.DIRECTIONS)},
                "side": {"enum": ["left", "right"]},
            },
            ["sheet", "name", "direction"],
        ),
        prepare_add_sheet_pin,
        apply_add_sheet_pin,
    ),
]
