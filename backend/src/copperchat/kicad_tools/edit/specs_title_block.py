"""`sch_set_title_block` (two-phase): set the title block of one sheet, or of every sheet
file. The title block is not an item, so the change set carries no stages; `apply` writes
it through the API inside one session commit (lock, modified flags, save bookkeeping),
reads it back and restores the old values on any failure."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from copperchat.changes.repo import ChangeEntry
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadUnavailable
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical, NetIntent, SheetRef
from copperchat.kicad_tools.edit.labels import no_change
from copperchat.kicad_tools.edit.scene import resolve_sheet, sheet_api, sheet_refs
from copperchat.kicad_tools.edit.title_block import (
    COMMENT_COUNT,
    MAX_COMMENT_LEN,
    MAX_LEN,
    TOOL,
    Values,
    changed_fields,
    merge,
    nonempty,
    parse_args,
    read_values,
    write_values,
)
from copperchat.tools.spec import ToolContext, ToolResult, ToolSpec, edit_spec

log = logging.getLogger(__name__)


@dataclass
class TitleBlockChange:
    sheet: SheetRef
    old: Values
    new: Values

    @property
    def changed(self) -> list[str]:
        return changed_fields(self.old, self.new)


@dataclass
class TitleBlockChangeSet(ChangeSet):
    plan: list[TitleBlockChange] = field(default_factory=list)  # only sheets that change


def _row(c: TitleBlockChange) -> dict[str, Any]:
    return {
        "sheet": c.sheet.as_dict(),
        "changed": c.changed,
        "before": nonempty(c.old),
        "after": nonempty(c.new),
    }


async def prepare_set_title_block(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    sheet_arg, all_sheets, updates = parse_args(args)

    def run(h: KicadHandle) -> ChangeSet:
        refs = sheet_refs(h)
        if all_sheets:
            files: dict[str, SheetRef] = {}
            for ref in refs:  # one title block per sheet file, however often it is instanced
                files.setdefault(ref.file, ref)
            targets = list(files.values())
        else:
            targets = [resolve_sheet(h, sheet_arg)]
        rows: list[TitleBlockChange] = []
        for ref in targets:
            old = read_values(sheet_api(h, ref))
            rows.append(TitleBlockChange(ref, old, merge(old, updates)))
        notes: list[str] = []
        for ref in targets:
            users = [r.human_path for r in refs if r.file == ref.file]
            if len(users) > 1:
                notes.append(f"{ref.file} is used by {len(users)} sheet instances "
                             f"({', '.join(users[:8])}): all of them show this title "
                             "block")  # fmt: skip
        plan = [c for c in rows if c.changed]
        summary: dict[str, Any] = {
            "title_blocks": [_row(c) for c in rows],
            "sheets_changed": len(plan),
        }
        if not plan:
            return no_change(TOOL, targets[0], summary,
                             [*notes, "every title block already has these values: "
                              "nothing to change"])  # fmt: skip
        return TitleBlockChangeSet(
            tool=TOOL,
            sheet=plan[0].sheet,
            stages=[],
            pre_images={},
            intent=NetIntent(),
            logical=Logical(1),
            summary=summary,
            notes=notes,
            plan=plan,
        )

    return await ctx.session.call(run)


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


def _restore(plan: list[TitleBlockChange]) -> Any:
    def run(h: KicadHandle) -> None:
        for c in plan:
            write_values(sheet_api(h, c.sheet), c.old)

    return run


def _write(plan: list[TitleBlockChange]) -> Any:
    """Writes and reads back every title block; undoes itself when anything fails."""

    def run(h: KicadHandle) -> None:
        done: list[TitleBlockChange] = []
        try:
            for c in plan:
                sch = sheet_api(h, c.sheet)
                done.append(c)
                write_values(sch, c.new)
                got = read_values(sch)
                bad = [f for f in c.changed if got[f] != c.new[f]]
                if bad:
                    raise StructuredToolError(
                        f"{TOOL}: KiCad did not keep the title block of {c.sheet.human_path}",
                        f"read back differently: {', '.join(bad)}",
                        "The change was undone; check the values and retry.",
                        code="verification_failed",
                    )
        except BaseException:
            for c in reversed(done):
                try:
                    write_values(sheet_api(h, c.sheet), c.old)
                except Exception as exc:  # keep restoring the others
                    log.warning("restoring the title block of %s failed: %s", c.sheet.file, exc)
            raise

    return run


def _entries(ctx: ToolContext, plan: list[TitleBlockChange]) -> list[ChangeEntry]:
    state = ctx.editing.turn(ctx.project.id, ctx.run_id) if ctx.editing is not None else None
    return [
        ChangeEntry(
            project_id=ctx.project.id,
            run_id=ctx.run_id or "",
            plan_step_id=state.active_step_id if state else None,
            call_seq=state.call_seq if state else 0,
            tool=TOOL,
            op="modify",
            kind="title_block",
            uuid=c.sheet.file,
            sheet_path=c.sheet.path,
            before={"kind": "title_block", **nonempty(c.old)},
            after={"kind": "title_block", **nonempty(c.new)},
        )
        for c in plan
    ]


async def apply_set_title_block(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    plan = cs.plan if isinstance(cs, TitleBlockChangeSet) else []
    data: dict[str, Any] = {
        **cs.summary,
        "logical_changes": cs.logical.changes,
        "budget": None,  # filled in by the adapter
        "notes": list(cs.notes),
    }
    if not plan:  # nothing differs: an empty, verified call
        return ToolResult(data=data)
    session = ctx.session
    async with session.applying():
        try:
            async with session.commit(f"{TOOL}: {len(plan)} sheet(s)"):
                await session.call(_write(plan))
        except KicadUnavailable:
            raise  # the server restarted from the last save: the write is gone with it
        except asyncio.CancelledError:
            # `_write` ran in a worker thread and may have completed before the cancel
            await _shielded(session.call(_restore(plan)))
            raise
    if ctx.editing is not None:
        try:
            await ctx.editing.record_changes(ctx, _entries(ctx, plan))
        except Exception:  # applied and verified: never fail it afterwards
            log.exception("recording changes of %s failed", TOOL)
    return ToolResult(data=data, changed_docs={"sch"})


_TEXT = {"type": "string"}

TOOL_SPECS: list[ToolSpec] = [
    edit_spec(
        TOOL,
        "Set the title block of a schematic sheet (the text block in the bottom-right "
        "corner of the drawing frame: title, date, revision, company and comments 1-9). "
        "Partial update: only the fields you pass change; an empty string clears a "
        'field. date also accepts "today" (becomes YYYY-MM-DD, the server\'s local date). '
        "Target: one sheet "
        "(default root; a sheet path like '/amp/', a name or a page number) or "
        "all_sheets=true for every sheet file of the project (each file written once; "
        "all instances of a file share one title block). Text is single-line, at most "
        f"{MAX_LEN['title']} characters ({MAX_LEN['date']} for date and rev); quotes and "
        "backslashes are fine. KiCad creates the title block when the sheet has none. "
        "Reports per sheet which fields changed with their before / after values (only "
        "non-empty ones); the current values are also in project_info "
        "(sheets[].title_block). Verified by read-back, else undone. Budget: 1 logical "
        "change.",
        {
            "type": "object",
            "properties": {
                "title": {**_TEXT, "maxLength": MAX_LEN["title"]},
                "date": {
                    **_TEXT,
                    "maxLength": MAX_LEN["date"],
                    "description": 'Free text, usually YYYY-MM-DD; "today" = today.',
                },
                "rev": {**_TEXT, "maxLength": MAX_LEN["rev"], "description": "Revision."},
                "company": {**_TEXT, "maxLength": MAX_LEN["company"]},
                "comments": {
                    "type": "object",
                    "description": 'Comment lines by index, e.g. {"1": "Designed by ..."}.',
                    "properties": {
                        str(i): {**_TEXT, "maxLength": MAX_COMMENT_LEN}
                        for i in range(1, COMMENT_COUNT + 1)
                    },
                    "additionalProperties": False,
                },
                "sheet": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "Sheet instance path like '/amp/', name or page; default root.",
                },
                "all_sheets": {
                    "type": "boolean",
                    "description": "true: set the same values on every sheet file.",
                },
            },
            "additionalProperties": False,
        },
        prepare_set_title_block,
        apply_set_title_block,
    ),
]
