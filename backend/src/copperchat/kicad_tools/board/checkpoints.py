"""The two server-enforced board checkpoints (spec §8.3, B-C10): which placement and which
board the user approved. Only the latest approval per (project, board file, stage) counts;
it is valid while its hash equals the current hash of the stage, so any later change
(and an undo back to an older approved state) invalidates it.

`require_placement_approved` guards the routing tools (P5b, P6b, P7), `require_board_approved`
the fab export (P8). Both fail closed without a database."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypedDict

from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.changes.repo import BOARD_KINDS
from copperchat.db.repos.board import BoardCheckpointRepo, BoardReviewRepo, board_file
from copperchat.errors import StructuredToolError

if TYPE_CHECKING:
    from copperchat.db import Database
    from copperchat.projects import Project
    from copperchat.tools.spec import ToolContext

STAGES = ("placement", "board")
# Change-log kinds that change the board (every board write); an approval changes nothing.
WRITE_KINDS = BOARD_KINDS - {"board_approve"}


class CheckpointState(TypedDict):
    approved: bool  # a latest approval row exists
    stale: bool  # it exists and its hash differs from the current hash of the stage
    hash: str | None  # the approved hash
    approved_at: str | None
    conversation_id: str | None


def _hashes(model: BoardModel) -> dict[str, str]:
    return {"placement": placement_hash(model), "board": board_hash(model)}


async def checkpoint_state(
    db: Database, project: Project, model: BoardModel, *, current: dict[str, str] | None = None
) -> dict[str, CheckpointState]:
    """`{"placement": ..., "board": ...}` for the project's board (B-C10); `current` = the
    model's hashes when the caller has them already (else computed here)."""
    if current is None:
        current = await asyncio.to_thread(_hashes, model)
    repo = BoardCheckpointRepo(db)
    out: dict[str, CheckpointState] = {}
    for stage in STAGES:
        row = None if project.pcb_file is None else await repo.latest(
            project.id, board_file(project), stage
        )  # fmt: skip
        out[stage] = CheckpointState(
            approved=row is not None,
            stale=row is not None and row.hash != current[stage],
            hash=None if row is None else row.hash,
            approved_at=None if row is None else row.approved_at,
            conversation_id=None if row is None else row.conversation_id,
        )
    return out


async def board_written_since(db: Database, project_id: str, since_iso: str) -> bool:
    """Whether the change log holds a board write (`WRITE_KINDS`) of the project at or
    after `since_iso` (ties count as later: fail closed)."""
    marks = ", ".join("?" for _ in WRITE_KINDS)
    row = await db.fetchone(
        f"SELECT 1 FROM changes WHERE project_id = ? AND created_at >= ? AND kind IN ({marks}) "
        "LIMIT 1",
        (project_id, since_iso, *sorted(WRITE_KINDS)),
    )
    return row is not None


async def record_unless_written(
    db: Database, project_id: str, since_iso: str, record: Callable[[], Awaitable[int]]
) -> bool:
    """Record a piece of evidence (`record`) unless a board write was logged since
    `since_iso` (the moment its model was read), atomically: the check and the insert share
    one transaction, so a write logged later has a `created_at` at or after the evidence
    row's and `pcb_approve` guard 4 sees it. Returns whether the row was written."""
    async with db.transaction():
        if await board_written_since(db, project_id, since_iso):
            return False
        await record()
        return True


async def require_placement_approved(ctx: ToolContext, model: BoardModel) -> None:
    """`placement_not_approved` unless the latest `placement` approval's hash equals the
    current `placement_hash` (details: `stale`, the current `placement_hash`)."""
    current = await asyncio.to_thread(placement_hash, model)
    row = None
    if ctx.editing is not None and ctx.project.pcb_file is not None:
        row = await BoardCheckpointRepo(ctx.editing.db).latest(
            ctx.project.id, board_file(ctx.project), "placement"
        )
    if row is not None and row.hash == current:
        return
    stale = row is not None
    raise StructuredToolError(
        "The placement is not approved",
        "the placement changed after the user approved it"
        if stale
        else "the user has not approved this placement",
        "Render the placement (pcb_render view=placement), run pcb_check quick, show both to "
        "the user and end the turn; after their OK call pcb_approve stage 'placement'.",
        code="placement_not_approved",
        details={"stale": stale, "placement_hash": current},
    )


async def require_board_approved(ctx: ToolContext, model: BoardModel) -> None:
    """`not_approved` unless the latest `board` approval's hash equals the current
    `board_hash` and the newest `pcb_check full` for it had no hard failure (details:
    `reason` missing | stale | check_failed, the current `board_hash`)."""
    current = await asyncio.to_thread(board_hash, model)
    reason: str | None = "missing"
    if ctx.editing is not None and ctx.project.pcb_file is not None:
        db = ctx.editing.db
        rel = board_file(ctx.project)
        row = await BoardCheckpointRepo(db).latest(ctx.project.id, rel, "board")
        if row is None:
            reason = "missing"
        elif row.hash != current:
            reason = "stale"
        else:
            check = await BoardReviewRepo(db).last_full_check(ctx.project.id, rel, current)
            reason = None if check is not None and check.hard_failures == 0 else "check_failed"
    if reason is None:
        return
    why = {
        "missing": "the user has not approved this board for manufacturing",
        "stale": "the board changed after the user approved it",
        "check_failed": "the last pcb_check full of this board has hard failures (or none ran)",
    }[reason]
    raise StructuredToolError(
        "The board is not approved",
        why,
        "Run pcb_check full, render top and bottom, show them to the user and end the turn; "
        "after their OK call pcb_approve stage 'board'.",
        code="not_approved",
        details={"reason": reason, "board_hash": current},
    )
