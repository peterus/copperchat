"""The adapter's generic `library_row` pre-stage (spec §7.9, F4b Task 3): an edit that
assigns a footprint of the repo's local library in a project without the library's row
(`ChangeSet.library_row`, planned by `footprint_notes`) adds that row first.

Runs inside the adapter's library lock, after the turn snapshot and around `spec.apply`
(lock order: library lock -> `_apply_lock` of the schematic engine). The row is
journalled like S1 of `fp_create` (`row_add`, `lib_id` = nickname, `file_rel` = the
project's `fp-lib-table`), so History undo of the turn removes it again. It costs no
budget; the result names it in `side_effects` and the change log."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import partial

from copperchat.changes.repo import ChangeEntry
from copperchat.errors import DocumentInconsistent, StructuredToolError
from copperchat.history.library_lock import assert_library_locked
from copperchat.kicad_session import KicadUnavailable
from copperchat.kicad_session.session import _uncancellable
from copperchat.kicad_tools.edit.changeset import ChangeSet
from copperchat.kicad_tools.edit.engine import record_applied
from copperchat.kicad_tools.footprints.engine import (
    LibraryRowStage,
    _delete_row,
    _ensure_row,
    _finalise,
    _report,
    _RowProgress,
    _sha_or_absent,
)
from copperchat.kicad_tools.footprints.journal import Journal, scrub_repo, sha_of, table_rel
from copperchat.library.local import LocalLibrary
from copperchat.library.service import library_for
from copperchat.tools.spec import ToolContext, ToolResult

log = logging.getLogger(__name__)

ROW_ADDED = "fp_lib_table_row_added"


async def apply_with_library_row(
    ctx: ToolContext, cs: ChangeSet, apply: Callable[[], Awaitable[ToolResult]]
) -> ToolResult:
    """Add `cs.library_row`'s row (journal `row_add` pending -> `AddLibraryTableEntry` ->
    read-back -> the footprint must exist, else the row is removed and `not_found`
    raised -> journal finalised), then `apply()`. A failing `apply()` (or a cancel)
    removes the row again (`DeleteLibraryTableEntry`, run to completion) and re-raises;
    `KicadUnavailable` skips that (the restarted server reads the table from disk; the
    journal row lets History undo it). A row that was there already (same URI) adds
    nothing and is never removed. The caller holds the library lock (asserted)."""
    stage = cs.library_row
    assert stage is not None
    if stage.create_library:
        raise TypeError("the library_row pre-stage never creates the library directory")
    lib = stage.library
    assert_library_locked(lib.dir)
    session = ctx.session
    svc = library_for(session)
    journal = Journal(ctx)
    row = _RowProgress()
    row.journal_id = await journal.pending(
        "row_add", lib.nickname, table_rel(lib), await sha_of(lib.table, lib.repo_root)
    )
    try:
        if not svc.footprint_libraries_ready():  # e.g. a server restart since prepare
            await svc.activate_footprints()
        await _ensure_row(ctx, stage, row)
        if stage.footprint is not None and not await svc.has_footprint(stage.footprint):
            raise StructuredToolError(
                f"Footprint {stage.footprint} not found",
                f"the library row {lib.nickname} was added, but KiCad does not list "
                f"{stage.footprint}; the row was removed again",
                "Check the name with fp_list_local.",
                code="not_found",
            )
    except KicadUnavailable:
        raise  # the journal row stays pending (cleanup / History decide)
    except BaseException as exc:
        await _undo(ctx, cs, lib, row, journal, exc)
        raise
    await journal.finalise(
        row.journal_id, "done" if row.added else "compensated", await _sha_or_absent(lib.table)
    )
    row.final = True
    try:
        result = await apply()
    except KicadUnavailable:
        raise
    except BaseException as exc:
        await _undo(ctx, cs, lib, row, journal, exc)
        raise
    if row.added:
        _report_row(result, lib)
        await record_applied(ctx, cs.tool, [_change_entry(ctx, cs.tool, lib)])
    return result


def _report_row(result: ToolResult, lib: LocalLibrary) -> None:
    result.library_changed = {*result.library_changed, lib.nickname}
    if result.data is None:
        return
    effect = {
        "uuid": lib.nickname,
        "op": "add",
        "kind": ROW_ADDED,
        "before": None,
        "after": {"uri": lib.uri},
    }
    result.data["side_effects"] = [*result.data.get("side_effects", []), effect]


def _change_entry(ctx: ToolContext, tool: str, lib: LocalLibrary) -> ChangeEntry:
    """`fp_lib_table_row_added` as `fp_create` logs it (ref = nickname, after = URI),
    flagged as a side effect of the edit."""
    state = ctx.editing.turn(ctx.project.id, ctx.run_id) if ctx.editing is not None else None
    return ChangeEntry(
        project_id=ctx.project.id,
        run_id=ctx.run_id or "",
        plan_step_id=state.active_step_id if state else None,
        call_seq=state.call_seq if state else 0,
        tool=tool,
        op="add",
        kind=ROW_ADDED,
        uuid=lib.nickname,
        sheet_path="",
        ref=lib.nickname,
        after={"uri": lib.uri},
        side_effect=True,
    )


async def _undo(
    ctx: ToolContext,
    cs: ChangeSet,
    lib: LocalLibrary,
    row: _RowProgress,
    journal: Journal,
    cause: BaseException,
) -> None:
    """Remove the row this call added (run to completion; a Stop meanwhile wins
    afterwards). A failed removal leaves it (`leftover`): the error becomes
    `DocumentInconsistent` naming it (a schematic `DocumentInconsistent` gets it added
    to its leftovers); under a cancel the turn is told through `_report`."""
    try:
        await _uncancellable(_remove_row(ctx, lib, row, journal))
    except asyncio.CancelledError as again:
        failure = again.__cause__
        if isinstance(failure, DocumentInconsistent):
            _report(ctx, failure)
        raise
    except DocumentInconsistent as failure:
        if isinstance(cause, asyncio.CancelledError):
            _report(ctx, failure)
            return
        if isinstance(cause, DocumentInconsistent):
            cause.leftovers.extend(failure.leftovers)
            return
        raise failure from cause


async def _remove_row(
    ctx: ToolContext, lib: LocalLibrary, row: _RowProgress, journal: Journal
) -> None:
    session = ctx.session
    ctx.library_touched.append(lib.table)
    try:
        if row.added:
            await session.call(partial(_delete_row, lib.nickname))
        library_for(session).invalidate_footprints(lib.nickname)
    except Exception as exc:
        log.warning("removing the library row %s again failed: %s", lib.nickname, exc)
        await _finalise(journal, row.journal_id, "leftover", await _sha_or_absent(lib.table))
        reason = scrub_repo(str(exc), lib.repo_root)
        raise DocumentInconsistent(
            f"removing the footprint library row {lib.nickname} again failed: {reason}; "
            f"left behind: row {lib.nickname}",
            leftovers=[f"row {lib.nickname}"],
            footprint_only=True,
        ) from exc
    await _finalise(journal, row.journal_id, "compensated", await _sha_or_absent(lib.table))


__all__ = ["ROW_ADDED", "LibraryRowStage", "apply_with_library_row"]
