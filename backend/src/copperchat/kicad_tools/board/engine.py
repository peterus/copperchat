"""The board write engine (B-C4, spec §6.3): a `BoardChangeSet` is applied stage by
stage, one board commit per stage, inside `session.applying()`.

A tool error leaves nothing written: any failure after the first stage started
(cancellation included) runs the KIID-diff compensation (`compensate.py`) under
`_uncancellable`, then the `undo` of the stage that raised and of every completed
stage, in reverse (undo restores pre-call values, so it is safe for a partially run
stage); what cannot be
brought back raises `DocumentInconsistent`. The one exception is a stage with
`compensable=False` (a real netlist import): a later failure raises `sync_failed`
("undo the turn from the History panel"). `creates_board` lets KiCad create the board
of a schematic-only project first (and removes it again when the call fails).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

from copperchat.board.model import BoardModel
from copperchat.changes.repo import ChangeEntry
from copperchat.errors import DocumentInconsistent, StructuredToolError
from copperchat.history.restore import remove_created
from copperchat.kicad_session import KicadHandle, KicadUnavailable
from copperchat.kicad_session.session import _uncancellable
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.api import REFILL_TIMEOUT_S, DrcResult
from copperchat.kicad_tools.board.compensate import (
    CopperSnapshot,
    counts,
    leftovers,
    restore,
    take_snapshot,
)
from copperchat.kicad_tools.board.read import BOARD_READ_TIMEOUT_S, board_model
from copperchat.kicad_tools.edit.changeset import ChangeSet, NetIntent, SheetRef, Stage
from copperchat.kicad_tools.edit.engine import record_applied
from copperchat.tools.spec import ToolContext, ToolResult

log = logging.getLogger(__name__)

T = TypeVar("T")

BOARD_SHEET = SheetRef(path="", page="", file="", name="<board>")

# CreateDocument + save of an empty board: seconds; explicit, like every long call.
CREATE_BOARD_TIMEOUT_S = 120.0

SYNC_FAILED_SUGGESTION = (
    "Undo this turn from the History panel; the board import cannot be reverted item by item."
)


@dataclass
class BoardStageContext:
    """What a stage's `run` / `undo` gets. Always goes through `ctx.session` (a board
    creation replaces it)."""

    ctx: ToolContext
    report: dict[str, Any]
    created: set[str] = field(default_factory=set)  # KIIDs created by this call

    async def call(self, fn: Callable[[KicadHandle], T], *, timeout: float | None = None) -> T:
        """`session.call`; a long call passes its own `timeout` (B-C4: every long KiCad
        call passes an explicit timeout, e.g. a DSN export or a SES import)."""
        return await self.ctx.session.call(fn, timeout=timeout)

    async def drc(self, *, timeout_s: float = 60.0) -> DrcResult:
        """DRC with the long `session.call` timeout (B-C4): never the 30 s default,
        which would restart the api-server under a running DRC."""
        result: DrcResult = await self.ctx.session.call(
            lambda h: api.run_drc_task(h, timeout_s=timeout_s),
            timeout=api.drc_call_timeout(timeout_s),
        )
        return result

    async def refill(self, *, timeout_s: float = REFILL_TIMEOUT_S) -> None:
        """The only way a stage refills zones (B-C4)."""
        await self.ctx.session.call(
            lambda h: api.refill(h, timeout_s=timeout_s),
            timeout=api.refill_call_timeout(timeout_s),
        )

    async def model(self) -> BoardModel:
        return await board_model(self.ctx)


@dataclass(frozen=True)
class BoardStage:
    label: str
    run: Callable[[BoardStageContext], Awaitable[None]]
    commit: bool = True  # wrap in session.commit(f"copperchat: {tool}", doc="pcb")
    # False: not revertible item by item (netlist import) -> sync_failed + "undo via History"
    compensable: bool = True
    # non-item state (rules, net classes, layer count, stackup): restores the pre-call values
    undo: Callable[[BoardStageContext], Awaitable[None]] | None = None


@dataclass(kw_only=True)
class BoardChangeSet(ChangeSet):
    """A board write (B-C4). The schematic fields of `ChangeSet` are defaulted;
    `logical` carries the budget."""

    sheet: SheetRef = BOARD_SHEET
    stages: list[Stage] = field(default_factory=list)
    pre_images: dict[str, Any] = field(default_factory=dict)
    intent: NetIntent = field(default_factory=NetIntent)
    kind: str  # change-log kind, member of BOARD_KINDS
    board_stages: list[BoardStage]
    verify: Callable[[BoardModel, dict[str, Any]], list[str]] | None = None
    snapshot: Literal["ids", "full"] = "ids"
    creates_board: bool = False  # P2 sync on a boardless project
    saves_project: bool = False  # rules/net classes (P2 verifies)
    unrouted_by_call: list[str] = field(default_factory=list)
    fail_after: int | None = None  # test hook: raise after stage n (1-based)
    temp_dirs: list[Path] = field(default_factory=list)
    # What the stages report (`BoardStageContext.report`); the result's `data`.
    report: dict[str, Any] = field(default_factory=dict)


class _Mismatch(StructuredToolError):
    pass


async def apply_board_change_set(ctx: ToolContext, cs: BoardChangeSet) -> ToolResult:
    """Apply `cs` (B-C4): board creation (when planned), then the stages under
    `session.applying()`, verify, change log. `cs.temp_dirs` are removed on every path."""
    if not isinstance(cs, BoardChangeSet):
        raise TypeError("apply_board_change_set needs a BoardChangeSet")
    try:
        created: str | None = None
        if cs.creates_board and ctx.project.pcb_file is None:
            created = await _create_board(ctx)
        try:
            return await _apply(ctx, cs)
        except BaseException as exc:
            if created is not None:
                await _cleanup_board(ctx, created, exc)
            raise
    finally:
        await _uncancellable(_remove_temp_dirs(cs.temp_dirs))


async def _remove_temp_dirs(dirs: list[Path]) -> None:
    def remove() -> None:
        for d in dirs:
            if os.path.lexists(d):
                shutil.rmtree(d, ignore_errors=True)

    if dirs:
        await asyncio.to_thread(remove)


# -- board creation ------------------------------------------------------------------------


def _board_rel(ctx: ToolContext) -> str:
    return f"{ctx.project.pro_file.stem}.kicad_pcb"


async def _create_board(ctx: ToolContext) -> str:
    """KiCad creates `<project dir>/<stem>.kicad_pcb`, then the session is reopened on
    the re-discovered project. Returns the created file's rel path. Anything at the
    target (a symlink, a directory, a file the project does not know) is refused before
    any write."""
    reload = ctx.reload_project
    if reload is None:
        raise RuntimeError("creates_board needs ctx.reload_project (wired by the run engine)")
    project_dir = ctx.project.pro_file.parent
    rel = _board_rel(ctx)
    target = project_dir / rel
    _refuse_occupied(target, rel)

    def create(h: KicadHandle) -> None:
        # Again under the session lock, right before CreateDocument: something that
        # appeared since the first check is refused, never overwritten or removed.
        _refuse_occupied(target, rel)
        api.create_board_document(h, target)

    try:
        await ctx.session.call(create, timeout=CREATE_BOARD_TIMEOUT_S)
        await _reload(ctx)
        if ctx.project.pcb_file is None:
            raise RuntimeError(f"the board KiCad created ({rel}) was not found afterwards")
    except _PathOccupied:
        raise  # refused before CreateDocument: nothing there is this call's
    except BaseException as exc:
        # The path was absent before the call: whatever is there now is this call's.
        await _cleanup_board(ctx, rel, exc)
        raise
    return rel


class _PathOccupied(StructuredToolError):
    pass


def _refuse_occupied(target: Path, rel: str) -> None:
    """`output_path_unsafe` when anything (a symlink, a directory, a file the project
    does not know) is at the board path; lstat, never followed."""
    try:
        os.lstat(target)
    except FileNotFoundError:
        return
    raise _PathOccupied(
        "Cannot create the board",
        f"{rel} already exists in the project directory but is not the project's board "
        "(a symlink, a directory or an unknown file)",
        "Ask the user to move it away, then retry.",
        code="output_path_unsafe",
        details={"path": rel},
    )


async def _reload(ctx: ToolContext) -> None:
    assert ctx.reload_project is not None
    project, session, generation = await ctx.reload_project()
    ctx.project, ctx.session = project, session
    ctx.reloaded_generation = generation


async def _cleanup_board(ctx: ToolContext, rel: str, cause: BaseException) -> None:
    """`_remove_board` shielded, for an `except` block that re-raises `cause`. When
    `cause` is a cancellation (Stop), a failed removal never replaces it: it is
    reported to the turn (`on_document_inconsistent`) and the cancel propagates."""
    try:
        await _uncancellable(_remove_board(ctx, rel, cause))
    except asyncio.CancelledError as again:
        if isinstance(again.__cause__, Exception):
            _report(ctx, again.__cause__)
        raise
    except Exception as failed:
        if not isinstance(cause, asyncio.CancelledError):
            raise
        _report(ctx, failed)


async def _remove_board(ctx: ToolContext, rel: str, cause: BaseException) -> None:
    """Compensation of a board creation: delete the file this call created
    (`remove_created`: a regular file only, never through a symlink) and reload, so
    the session is a schematic-only one again. Anything still at the path afterwards
    (`remove_created` skips non-regular files) is a leftover."""
    project_dir = ctx.project.pro_file.parent
    try:
        await asyncio.to_thread(remove_created, project_dir, [rel])
        await _reload(ctx)
    except Exception as exc:
        log.warning("removing the board created by a failed call failed: %s", exc)
        raise DocumentInconsistent(
            f"the board this call created ({rel}) could not be removed again",
            leftovers=[rel],
        ) from cause
    if os.path.lexists(project_dir / rel):
        log.warning("the board created by a failed call is still there: %s", rel)
        raise DocumentInconsistent(
            f"the board this call created ({rel}) is still in the project directory",
            leftovers=[rel],
        ) from cause


# -- stages ------------------------------------------------------------------------------


async def _apply(ctx: ToolContext, cs: BoardChangeSet) -> ToolResult:
    session = ctx.session
    async with session.applying():
        generation = session.generation
        snap: CopperSnapshot = await session.call(
            lambda h: take_snapshot(h, cs.snapshot), timeout=BOARD_READ_TIMEOUT_S
        )
        sctx = BoardStageContext(ctx, cs.report)
        step_report: list[dict[str, Any]] = []
        started: list[BoardStage] = []

        def same_server() -> bool:
            return session.generation == generation and session.server_alive

        try:
            for n, stage in enumerate(cs.board_stages, 1):
                started.append(stage)
                if stage.commit:
                    async with session.commit(f"copperchat: {cs.tool}", doc="pcb"):
                        await stage.run(sctx)
                else:
                    await stage.run(sctx)
                step_report.append({"stage": stage.label, "status": "done"})
                if cs.fail_after == n:
                    raise RuntimeError("fail_after")
            if cs.board_stages and not same_server():
                raise KicadUnavailable(
                    "the KiCad api-server restarted during the board edit; it holds the "
                    "last saved state"
                )
            if cs.verify is not None:
                problems = cs.verify(await sctx.model(), cs.report)
                if problems:
                    raise _Mismatch(
                        f"{cs.tool} did not produce the planned board; it was undone",
                        "; ".join(problems[:6]),
                        "Nothing was changed. Re-read the board (pcb_status) and retry.",
                        code="verification_failed",
                        details={"found": problems},
                    )
            # The change-log counts: inside the protected region, so a cancel or an
            # error here still compensates (nothing written on any tool error).
            after = await session.call(
                lambda h: take_snapshot(h, "ids"), timeout=BOARD_READ_TIMEOUT_S
            )
        except BaseException as exc:
            if not started:
                raise
            await _after_failure(ctx, cs, sctx, snap, started, exc, same_server)
            raise
    await record_applied(ctx, cs.tool, [_entry(ctx, cs, counts(snap, after))])
    return ToolResult(data=cs.report | {"step_report": step_report}, changed_docs={"pcb"})


async def _after_failure(
    ctx: ToolContext,
    cs: BoardChangeSet,
    sctx: BoardStageContext,
    snap: CopperSnapshot,
    started: list[BoardStage],
    exc: BaseException,
    same_server: Callable[[], bool],
) -> None:
    """Undo what the started stages wrote; returns when the original error should be
    re-raised, raises otherwise (sync_failed, DocumentInconsistent, KicadUnavailable)."""
    cancelled = isinstance(exc, asyncio.CancelledError)
    if isinstance(exc, KicadUnavailable):
        return  # the server restarted from the last save: the pushed stages are gone
    if not same_server():
        if cancelled:
            return  # a killed server holds the last save; nothing to compensate
        raise KicadUnavailable(
            "the KiCad api-server restarted during the board edit; it holds the last saved state"
        ) from exc
    if not all(s.compensable for s in started):
        if cancelled:
            # The Stop wins, but the turn must learn that the board is half-synced:
            # the DocumentInconsistent path ends the turn without autosave and reopens
            # the last save (the state before this call); the text names the History
            # panel like sync_failed.
            _report(
                ctx,
                DocumentInconsistent(
                    f"{cs.tool} was stopped after a step that cannot be reverted item by "
                    f"item. {SYNC_FAILED_SUGGESTION}"
                ),
            )
            return
        raise StructuredToolError(
            f"{cs.tool} failed after a step that cannot be reverted item by item",
            str(exc) or type(exc).__name__,
            SYNC_FAILED_SUGGESTION,
            code="sync_failed",
        ) from exc
    try:
        await _uncancellable(_compensate(ctx, cs, sctx, snap, started))
    except asyncio.CancelledError as again:
        if isinstance(again.__cause__, Exception):
            _report(ctx, again.__cause__)
        raise
    except Exception as failed:
        if not cancelled:
            raise
        _report(ctx, failed)  # the Stop wins; the turn still learns of the failure


async def _compensate(
    ctx: ToolContext,
    cs: BoardChangeSet,
    sctx: BoardStageContext,
    snap: CopperSnapshot,
    started: list[BoardStage],
) -> None:
    session = ctx.session
    try:
        # One board commit; KiCad stages its changes until the push, so the read-back
        # (`leftovers`) runs after it.
        async with session.commit(f"copperchat: {cs.tool}: undo", doc="pcb"):
            renamed = await session.call(lambda h: restore(h, snap), timeout=BOARD_READ_TIMEOUT_S)
        # `started` = the completed stages plus, on a failure inside a stage, the one
        # that raised: its undo runs first (it restores pre-call values, so a partial
        # run is fine), then the completed ones in reverse. Before the read-back (P7):
        # an undo may re-create an item a stage deleted (a zone, with its KIID).
        for stage in reversed(started):
            if stage.undo is not None:
                await stage.undo(sctx)
        left: list[str] = await session.call(
            lambda h: leftovers(h, snap, renamed), timeout=BOARD_READ_TIMEOUT_S
        )
    except (asyncio.CancelledError, KicadUnavailable):
        raise
    except Exception as exc:
        log.warning("compensating %s failed: %s", cs.tool, exc)
        raise DocumentInconsistent(f"undoing the partial {cs.tool} change failed") from exc
    if left:
        log.warning("compensating %s left %s", cs.tool, left)
        raise DocumentInconsistent(
            f"undoing the partial {cs.tool} change left the board different", leftovers=left
        )


def _report(ctx: ToolContext, exc: BaseException) -> None:
    """A compensation that failed while the call was being cancelled (the adapter only
    sees the CancelledError, so the turn is told here)."""
    if isinstance(exc, DocumentInconsistent) and ctx.on_document_inconsistent is not None:
        ctx.on_document_inconsistent(exc)
    elif isinstance(exc, KicadUnavailable) and ctx.on_kicad_unavailable is not None:
        ctx.on_kicad_unavailable(exc)
    else:
        log.error("undoing a board edit after a cancellation failed: %s", exc)


def _entry(ctx: ToolContext, cs: BoardChangeSet, after: dict[str, int]) -> ChangeEntry:
    state = ctx.editing.turn(ctx.project.id, ctx.run_id) if ctx.editing is not None else None
    return ChangeEntry(
        project_id=ctx.project.id,
        run_id=ctx.run_id or "",
        plan_step_id=state.active_step_id if state else None,
        call_seq=state.call_seq if state else 0,
        tool=cs.tool,
        op="modify",
        kind=cs.kind,
        uuid="",
        sheet_path="",
        after=after,
    )
