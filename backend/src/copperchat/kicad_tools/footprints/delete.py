"""`fp_delete` (footprints spec §7.8, F4b Task 2): delete a footprint of the repo's local
library through KiCad (`DeleteLibraryItem`, patch 0003).

`prepare` refuses `not_found` (also an unfinished seed copy the next write's cleanup
removes), `not_local` (another library, or this project has no row for the local one)
and `in_use` (a symbol of the open schematic names it in its Footprint field; the refs
are in `details.refs`). The other projects of the repo are byte-searched
(`usage.scan_other_projects`); a hit is only a `possibly_in_use_elsewhere` note. The own
project's board is byte-searched too (`usage.scan_own_boards`); a hit is an
`in_use_on_board` note.

`apply` (under the library lock the adapter takes): the path and project-row checks
again, `cleanup_pending`, journal `delete` (pre = the file's hash), `DeleteLibraryItem`,
read back that the footprint is gone (KiCad's listing and the file), finalise `done`
with `absent`.
`Logical(1)`, never counted as a deleted symbol (spec §12)."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from copperchat.changes.repo import ChangeEntry
from copperchat.errors import (
    DocumentInconsistent,
    StructuredToolError,
    attach_tool_notes,
    deleted_unconfirmed,
)
from copperchat.history.library_lock import assert_library_locked
from copperchat.kicad_session import KicadUnavailable
from copperchat.kicad_session.session import _uncancellable
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.kicad_tools.edit.engine import record_applied
from copperchat.kicad_tools.footprints.engine import (
    FootprintChangeSet,
    FpStage,
    _delete_item,
    _file_checks,
    _finalise,
    _lexists,
    _library_error,
    _recheck_row,
    _report,
    _sha_or_absent,
    cleanable_copy,
    cleanup_pending,
)
from copperchat.kicad_tools.footprints.journal import ABSENT, Journal, footprint_rel, sha_of
from copperchat.kicad_tools.footprints.model import geometry_hash
from copperchat.kicad_tools.footprints.read import (
    _footprint_users,
    parse_lib_id,
    require_enabled,
)
from copperchat.kicad_tools.footprints.usage import scan_other_projects, scan_own_boards
from copperchat.library.local import LocalLibrary
from copperchat.library.service import library_for, local_footprint_file_safe
from copperchat.tools.spec import ToolContext, ToolResult

log = logging.getLogger(__name__)

TOOL = "fp_delete"
LOGICAL_CHANGES = 1  # spec §12; never a deleted symbol (MAX_PREEXISTING_SYMBOL_DELETES)
DELETE_KEYS = frozenset({"lib_id"})
MAX_REFS_SHOWN = 20


def _error(what: str, why: str, suggestion: str, code: str, **kw: Any) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code=code, **kw)


def _not_local(lib_id: str, why: str) -> StructuredToolError:
    return _error(
        f"{lib_id} is not a footprint of the local library",
        why,
        "Only footprints of this repository's local library can be deleted (see "
        "fp_list_local); every other library is read-only.",
        "not_local",
    )


def _not_found(lib_id: str, why: str, suggestion: str) -> StructuredToolError:
    return _error(f"Footprint {lib_id} not found", why, suggestion, "not_found")


def _listed(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_REFS_SHOWN])
    return shown + (
        f" and {len(names) - MAX_REFS_SHOWN} more" if len(names) > MAX_REFS_SHOWN else ""
    )


def _elsewhere_note(hits: list[str], complete: bool) -> dict[str, Any] | None:
    """The `possibly_in_use_elsewhere` note of the sibling-project scan (spec §7.8):
    a warning, never a refusal; also when the search hit a cap."""
    if not hits and complete:
        return None
    incomplete = (
        "The search of the other projects was incomplete (too many or too large files), "
        "so they may still use it."
    )
    if hits:
        message = (
            f"Other projects of this repository may use this footprint: {_listed(hits)} "
            "name it, and would lose it. Tell the user."
        )
        if not complete:
            message += " " + incomplete
    else:
        message = incomplete
    return {
        "kind": "possibly_in_use_elsewhere",
        "files": hits,
        "complete": complete,
        "message": message,
    }


def _board_note(hits: list[str], complete: bool) -> dict[str, Any] | None:
    """The `in_use_on_board` note of the own board scan: `in_use` reads only the open
    schematic, so a footprint the board places (e.g. one assigned on the board only) is
    a warning, never a refusal."""
    if not hits and complete:
        return None
    if hits:
        message = (
            f"This project's board {_listed(hits)} places this footprint; its footprints "
            "keep their own copy, but lose their library source (updating them from the "
            "library fails). Tell the user."
        )
    else:
        message = (
            "The search of this project's board was incomplete (the file is too large), "
            "so it may still place this footprint."
        )
    return {"kind": "in_use_on_board", "files": hits, "complete": complete, "message": message}


async def prepare_delete(ctx: ToolContext, args: dict[str, Any]) -> FootprintChangeSet:
    """Plan an `fp_delete` (spec §7.8); refuses with a structured error, writes
    nothing. The library comes from `local_library(for_write=True)`
    (`library_conflict` for a symlinked path or a conflicting row), the file is checked
    with `local_footprint_file_safe`."""
    unknown = sorted(set(args) - DELETE_KEYS)
    if unknown:
        why = f"unknown key(s) {unknown}"
        raise _error("Invalid arguments", why, "see the tool schema", "invalid_argument")
    lib_id, nickname, name = parse_lib_id(args.get("lib_id"))
    require_enabled(ctx)
    svc = library_for(ctx.session)
    info = await svc.local_library(for_write=True)
    assert info is not None  # for_write raises repo_root_unknown instead
    if info.row_conflict:
        raise _error(
            "The local footprint library conflicts with the project's library table",
            info.row_conflict,
            "Ask the user to rename or remove the conflicting fp-lib-table row; nothing was "
            "written.",
            "library_conflict",
        )
    lib = info.lib
    if nickname != lib.nickname:
        raise _not_local(lib_id, f"{nickname} is not this repository's local library")
    if not info.row_present:
        why = f"this project has no library row for {lib.nickname} yet"
        raise _not_local(lib_id, why)
    await asyncio.to_thread(local_footprint_file_safe, lib, name)
    if await cleanable_copy(ctx, lib, name):
        raise _not_found(
            lib_id,
            f"{lib_id} is only an unfinished copy left by an earlier footprint call that "
            "stopped when the KiCad server died; the next footprint write removes it",
            "Nothing to delete; list the local footprints with fp_list_local.",
        )
    notes: list[dict[str, Any]] = []
    geometry = await svc.footprint_geometry(lib_id, notes)
    if geometry is None or geometry.scope != "local":
        raise _not_found(
            lib_id,
            f"{lib_id} is not in the local library",
            "List the local footprints with fp_list_local.",
        )
    users: dict[str, list[str]] = await ctx.session.call(_footprint_users(lib.nickname))
    refs = users.get(lib_id, [])
    if refs:
        raise _error(
            f"{lib_id} is in use",
            f"symbols of this project use it in their Footprint field: {_listed(refs)}",
            "Assign another footprint to these symbols first (sch_set_fields), or keep "
            "the footprint.",
            "in_use",
            details={"refs": refs},
        )
    board = await asyncio.to_thread(scan_own_boards, lib.repo_root, ctx.project.dir, lib_id)
    scan = await asyncio.to_thread(scan_other_projects, lib.repo_root, ctx.project.dir, lib_id)
    for note in (
        _board_note(board.hits, board.complete),
        _elsewhere_note(scan.hits, scan.complete),
    ):
        if note is not None:
            notes.append(note)
    return FootprintChangeSet(
        tool=TOOL,
        logical=Logical(LOGICAL_CHANGES),
        summary={"lib_id": lib_id, "geometry_hash": geometry_hash(geometry), "notes": notes},
        fp_stages=[FpStage("delete", lib_id)],
        library=lib,
    )


# -- apply -----------------------------------------------------------------------------


async def _settle(
    journal: Journal, row: int | None, lib: LocalLibrary, lib_id: str, pre: str
) -> None:
    """After a failure: the file still holds its earlier bytes -> nothing was deleted,
    `compensated`; anything else (gone without a verified read-back, changed) ->
    `leftover` and `DocumentInconsistent` (footprint only; History undo restores the
    footprint, so it is reported like a replaced one: never to be deleted again). A file
    that is gone is labelled `deleted_unconfirmed(lib_id)`, so the agent and the notice
    say "deleted, not confirmed" instead of "remains"."""
    file = lib.dir / f"{lib_id.split(':', 1)[1]}.kicad_mod"
    sha = await _sha_or_absent(file)
    if sha == pre:
        await _finalise(journal, row, "compensated", sha)
        return
    await _finalise(journal, row, "leftover", sha)
    gone = not await asyncio.to_thread(_lexists, file)
    label = deleted_unconfirmed(lib_id) if gone else lib_id
    raise DocumentInconsistent(
        f"fp_delete of {lib_id} changed its file but could not verify it; left behind: {label}",
        leftovers=[label],
        replaced=[label],
        footprint_only=True,
    )


async def _delete_footprint(ctx: ToolContext, lib: LocalLibrary, lib_id: str, digest: str) -> str:
    """The single stage (spec §9.3): journal `delete`, `DeleteLibraryItem`, read back
    that KiCad no longer lists it and the file is gone. `KicadUnavailable` (or a
    restart) skips the settling: the row stays pending. Returns the file's repo-relative
    path."""
    from kipy.errors import ApiError

    name = lib_id.split(":", 1)[1]
    file = lib.dir / f"{name}.kicad_mod"
    rel = footprint_rel(lib, name)
    session = ctx.session
    svc = library_for(session)
    journal = Journal(ctx)
    generation = session.generation

    def same_server() -> bool:
        return session.generation == generation and session.server_alive

    def ensure_same_server() -> None:
        if not same_server():
            raise KicadUnavailable(
                "the KiCad api-server restarted during fp_delete; the journal keeps the "
                "write pending"
            )

    # FP-C8: path, project table, its row and the file were checked again under the
    # library lock by the caller, before the cleanup (`_recheck`)
    pre = await sha_of(file, lib.repo_root)
    if pre == ABSENT:
        raise _not_found(
            lib_id,
            f"{lib_id} was removed after the call was planned",
            "List the local footprints with fp_list_local; nothing was written.",
        )
    row = await journal.pending("delete", lib_id, rel, pre, digest)

    async def settle() -> None:
        try:
            await _uncancellable(_settle(journal, row, lib, lib_id, pre))
        except asyncio.CancelledError as again:
            if isinstance(again.__cause__, Exception):
                _report(ctx, again.__cause__)
            raise

    try:
        ctx.library_touched.append(file)
        try:
            await session.call(partial(_delete_item, lib.nickname, name))
        except (ApiError, RuntimeError, ValueError) as exc:  # KicadUnavailable passes
            raise _library_error(
                f"Could not delete footprint {lib_id}", exc, lib.repo_root
            ) from exc
        finally:
            svc.invalidate_footprints(lib.nickname)
        ensure_same_server()
        names = await svc.footprint_names(lib.nickname)
        ensure_same_server()
        problem = None
        if name in names:
            problem = "KiCad still lists it"
        elif await asyncio.to_thread(_lexists, file):
            problem = f"{rel} is still on disk"
        if problem is not None:
            raise _error(
                f"fp_delete: {lib_id} is not gone after the delete",
                problem,
                "Tell the user; do not retry in this turn.",
                "verification_failed",
            )
        await journal.finalise(row, "done", ABSENT)
    except asyncio.CancelledError:
        # the turn is being stopped: settle the journal (to completion), keep cancelling
        if same_server():
            try:
                await settle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _report(ctx, exc)
        raise
    except KicadUnavailable:
        raise  # spec §9.3 server death: the journal row stays pending
    except BaseException as exc:
        if not same_server():
            raise KicadUnavailable(
                "the KiCad api-server restarted during fp_delete; the journal keeps the "
                "write pending"
            ) from exc
        await settle()
        raise
    finally:
        svc.invalidate_footprints(lib.nickname)
    return rel


async def _recheck(ctx: ToolContext, lib: LocalLibrary, lib_id: str) -> None:
    """FP-C8 under the library lock, before `cleanup_pending` (which may already delete
    a file): the library path, the project table, its row and the target file are
    checked again - `library_conflict` otherwise, nothing written."""
    await asyncio.to_thread(_file_checks, lib, lib_id.split(":", 1)[1])
    await _recheck_row(ctx, lib)


async def apply_delete(ctx: ToolContext, cs: Any) -> ToolResult:
    """`fp_delete.apply` inside `session.applying()`, the adapter holding the library
    lock: the FP-C8 re-checks, `cleanup_pending`, then the delete stage; change log
    `footprint_deleted` (before = the geometry hash prepare read)."""
    if not isinstance(cs, FootprintChangeSet) or cs.library is None or cs.tool != TOOL:
        raise TypeError("apply_delete needs a planned fp_delete change set")
    lib = cs.library
    assert_library_locked(lib.dir)
    lib_id: str = cs.summary["lib_id"]
    digest: str = cs.summary["geometry_hash"]
    async with ctx.session.applying():
        await _recheck(ctx, lib, lib_id)  # before the cleanup writes anything
        cleaned = await cleanup_pending(ctx, lib)
        try:
            rel = await _delete_footprint(ctx, lib, lib_id, digest)
        except BaseException as exc:
            attach_tool_notes(exc, cleaned)  # the cleanup's work stays: say so
            raise
    await record_applied(ctx, TOOL, [_change_entry(ctx, lib_id, digest)])
    data: dict[str, Any] = {
        "deleted": lib_id,
        "file": rel,
        "logical_changes": cs.logical.changes,
        "budget": None,
        "notes": [*cs.summary.get("notes", []), *cleaned],
    }
    # Library-only (spec §9.1): journalled inside apply, nothing for the adapter to save.
    return ToolResult(data=data, changed_docs=set(), library_changed={lib.nickname})


def _change_entry(ctx: ToolContext, lib_id: str, digest: str) -> ChangeEntry:
    state = ctx.editing.turn(ctx.project.id, ctx.run_id) if ctx.editing is not None else None
    return ChangeEntry(
        project_id=ctx.project.id,
        run_id=ctx.run_id or "",
        plan_step_id=state.active_step_id if state else None,
        call_seq=state.call_seq if state else 0,
        tool=TOOL,
        op="delete",
        kind="footprint_deleted",
        uuid=lib_id,
        sheet_path="",
        ref=lib_id,
        before={"geometry_hash": digest},
    )
