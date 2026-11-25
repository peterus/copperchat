"""`pcb_approve` (spec §7.2, §8.3; P3b Task 3, index call 7): record the user's approval of
the placement or of the whole board, but only with server-side evidence that they saw
exactly this state and answered in their own words.

`prepare` checks the guards, in this order (each refusal is `approval_not_allowed` with
`details.guard`):

1. `hash` is the current hash of `stage` (`placement_hash` / `board_hash`); the other
   stage's hash is refused with `details.other_stage`;
2. this conversation has the stage's evidence for that hash: placement = a `placement`
   render and a check (quick or full); board = a `full` check and `top` and `bottom`
   renders;
3. the evidence passes: placement = the newest check has M1 passing; board = the newest
   full check has no hard failure;
4. no board write after the evidence: the change log holds no board write (any
   `BOARD_KINDS` kind but `board_approve`) since the OLDEST evidence row used - a move away
   and back to the same hash means the user never saw the board as it is now (ABA);
5. the current run processed a message the user typed after the NEWEST evidence row;
6. `user_quote` is in one of those messages (`copperchat.approvals.quote_in_message`);
7. no plan of this conversation is running.

`apply` writes no KiCad data: it re-reads the board, then in ONE database transaction
re-checks guards 1 and 4 (a write may have landed in between), inserts the checkpoint row
and its `board_approve` change entry - both or neither. Cost 1, plan-exempt (spec §7.3).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from copperchat.approvals import (
    MIN_QUOTE_CHARS,
    MIN_QUOTE_WORDS,
    human_messages_after,
    plan_running,
    quote_in_message,
)
from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.changes.repo import ChangeEntry
from copperchat.db.repos.board import BoardCheckpointRepo, BoardReviewRepo, Review, board_file
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board.checkpoints import STAGES, board_written_since
from copperchat.kicad_tools.board.engine import BoardChangeSet
from copperchat.kicad_tools.board.read import board_model, require_board
from copperchat.kicad_tools.board.setup import require_board_tools
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.tools.spec import ToolContext, ToolResult

log = logging.getLogger(__name__)

TOOL = "pcb_approve"
KIND = "board_approve"
LOGICAL_CHANGES = 1  # spec §7.2
APPROVE_KEYS = frozenset({"stage", "hash", "user_quote"})
HASH_RE = re.compile(r"^[0-9a-f]{64}\Z")
MAX_QUOTE_CHARS = 300

Stage = Literal["placement", "board"]

_NEXT = {
    "placement": "The placement is approved: route the board (pcb_route for the critical "
    "nets first). Any later move or outline change invalidates this approval.",
    "board": "The board is approved for manufacturing: export the fab files with "
    "pcb_fab_export. Any later change invalidates this approval.",
}
_SHOW = {
    "placement": "Render the placement (pcb_render view=placement) and run pcb_check quick, "
    "show both to the user and end the turn",
    "board": "Run pcb_check full and render the top and bottom views, show them to the user "
    "and end the turn",
}


@dataclass(frozen=True)
class ApproveArgs:
    stage: Stage
    hash: str
    user_quote: str


def _invalid(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


def parse_approve_args(args: dict[str, Any]) -> ApproveArgs:
    """The schema, again on the server."""
    unknown = sorted(set(args) - APPROVE_KEYS)
    if unknown:
        raise _invalid("Invalid arguments", f"unknown key(s) {unknown[:10]}", "see the tool schema")
    stage = args.get("stage")
    if not isinstance(stage, str) or stage not in STAGES:
        raise _invalid("Invalid stage", "stage must be 'placement' or 'board'", "see the schema")
    digest = args.get("hash")
    if not isinstance(digest, str) or not HASH_RE.match(digest):
        raise _invalid(
            "Invalid hash",
            "hash must be the 64 lower-case hex characters pcb_status / pcb_render report",
            "Copy the stage's hash (placement_hash or board_hash) from the result the user saw.",
        )
    quote = args.get("user_quote")
    if not isinstance(quote, str) or not 1 <= len(quote) <= MAX_QUOTE_CHARS:
        raise _invalid(
            "Invalid user_quote",
            f"user_quote must be a string of 1 to {MAX_QUOTE_CHARS} characters",
            "Copy the user's own words of approval from their message.",
        )
    return ApproveArgs(stage, digest, quote)  # type: ignore[arg-type]


def refused(
    guard: int, what: str, why: str, suggestion: str, **details: Any
) -> StructuredToolError:
    return StructuredToolError(
        what, why, suggestion, code="approval_not_allowed", details={"guard": guard, **details}
    )


def _hashes(model: BoardModel) -> dict[str, str]:
    return {"placement": placement_hash(model), "board": board_hash(model)}


async def current_hashes(ctx: ToolContext) -> dict[str, str]:
    """`{"placement": ..., "board": ...}` of the live board (hashing in a thread)."""
    model = await board_model(ctx)
    return await asyncio.to_thread(_hashes, model)


# -- guards ---------------------------------------------------------------------------------


def guard_hash(a: ApproveArgs, current: dict[str, str]) -> None:
    """Guard 1."""
    if a.hash == current[a.stage]:
        return
    other = "board" if a.stage == "placement" else "placement"
    if a.hash == current[other]:
        raise refused(
            1,
            f"That is the {other} hash, not the {a.stage} hash",
            f"a {a.stage} approval names the current {a.stage}_hash",
            f"Use the {a.stage}_hash of the state the user saw.",
            other_stage=True,
        )
    raise refused(
        1,
        f"The {a.stage} changed since the user saw it",
        f"its current {a.stage}_hash is {current[a.stage][:8]}, the approval names {a.hash[:8]}",
        f"{_SHOW[a.stage]}; approve only after the user's OK in their next message.",
    )


def _newest(rows: list[Review], kind: str, *, view: str | None = None,
            level: str | None = None) -> Review | None:  # fmt: skip
    found = [
        r
        for r in rows
        if r.kind == kind
        and (view is None or r.view == view)
        and (level is None or r.check_level == level)
    ]
    return found[-1] if found else None  # rows are ordered by id


async def guard_evidence(ctx: ToolContext, a: ApproveArgs) -> list[Review]:
    """Guards 2 and 3; returns the evidence rows used (one per required kind, the newest
    for the hash)."""
    assert ctx.editing is not None and ctx.conversation_id is not None
    repo = BoardReviewRepo(ctx.editing.db)
    key = {"placement_hash": a.hash} if a.stage == "placement" else {"board_hash": a.hash}
    rows = await repo.evidence(ctx.project.id, board_file(ctx.project), ctx.conversation_id, **key)
    if a.stage == "placement":
        need: dict[str, Review | None] = {
            "pcb_render view=placement": _newest(rows, "render", view="placement"),
            "pcb_check": _newest(rows, "check"),
        }
    else:
        need = {
            "pcb_check level=full": _newest(rows, "check", level="full"),
            "pcb_render view=top": _newest(rows, "render", view="top"),
            "pcb_render view=bottom": _newest(rows, "render", view="bottom"),
        }
    missing = [name for name, row in need.items() if row is None]
    if missing:
        raise refused(
            2,
            f"The {a.stage} was not shown to the user at this state",
            f"this session has no {' and no '.join(missing)} for {a.stage}_hash {a.hash[:8]}",
            f"{_SHOW[a.stage]}; approve only after the user's OK in their next message.",
            missing=missing,
        )
    used = [r for r in need.values() if r is not None]
    check = need["pcb_check level=full" if a.stage == "board" else "pcb_check"]
    assert check is not None  # not missing
    if a.stage == "placement" and check.m1_pass != 1:
        raise refused(
            3,
            "The placement does not pass M1",
            "the newest pcb_check of this placement has M1 failing (parts outside the "
            "outline, overlapping courtyards or SMD parts on a side board.md does not allow)",
            "Fix the placement, check it again and show it to the user.",
        )
    if a.stage == "board" and check.hard_failures != 0:
        raise refused(
            3,
            "The board fails its full check",
            f"the newest pcb_check full of this board has {check.hard_failures} hard failure(s)",
            "Fix the hard failures, run pcb_check full again and show the result to the user.",
        )
    return used


async def guard_no_write_after(ctx: ToolContext, a: ApproveArgs, since: str) -> None:
    """Guard 4."""
    assert ctx.editing is not None
    if await board_written_since(ctx.editing.db, ctx.project.id, since):
        raise refused(
            4,
            f"The {a.stage} cannot be approved in this turn",
            "the board was changed after its render or check (the hash may be the same "
            "again, but the user has not seen the board as it is now)",
            f"{_SHOW[a.stage]}; approve only after the user's OK in their next message.",
        )


async def guard_user(ctx: ToolContext, a: ApproveArgs, shown_at: str) -> None:
    """Guards 5, 6 and 7."""
    later = await human_messages_after(ctx, shown_at)
    if not later:
        why = (
            "this turn was not started by a message the user typed after the render and the "
            "check (a message from before them, plan execution, a session prompt or a notice "
            "do not count)"
        )
        raise refused(
            5,
            f"The {a.stage} cannot be approved in this turn",
            why,
            f"{_SHOW[a.stage]}; approve only after the user's OK in their next message.",
        )
    if not any(quote_in_message(a.user_quote, m.content) for m in later):
        raise refused(
            6,
            f"The quote is not the user's approval of the {a.stage}",
            "user_quote must be the user's own words copied from their message of this turn "
            f"(at least {MIN_QUOTE_WORDS} words and {MIN_QUOTE_CHARS} characters on word "
            "boundaries, or their whole message)",
            "Quote the user's approval verbatim; if they did not approve clearly, ask them.",
        )
    if await plan_running(ctx):
        raise refused(
            7,
            f"The {a.stage} cannot be approved while a plan runs",
            "a plan of this session is running; an approval is a separate turn after the "
            "user's answer",
            "Finish the plan first, then ask the user for the approval.",
        )


# -- prepare / apply ------------------------------------------------------------------------


async def prepare_approve(ctx: ToolContext, args: dict[str, Any]) -> BoardChangeSet:
    """Check guards 1-7; nothing is written."""
    a = parse_approve_args(args)
    require_board_tools(ctx)
    require_board(ctx)
    if ctx.editing is None or ctx.conversation_id is None:
        raise refused(
            2,
            f"The {a.stage} cannot be approved here",
            "approvals are only recorded in a chat session, where CopperChat tracks what the "
            "user was shown",
            "Ask the user to approve it in a CopperChat chat session.",
        )
    guard_hash(a, await current_hashes(ctx))
    used = await guard_evidence(ctx, a)
    oldest = min(r.created_at for r in used)
    await guard_no_write_after(ctx, a, oldest)
    await guard_user(ctx, a, max(r.created_at for r in used))
    return BoardChangeSet(
        tool=TOOL,
        kind=KIND,
        board_stages=[],
        logical=Logical(LOGICAL_CHANGES),
        summary={
            "stage": a.stage,
            "hash": a.hash,
            "user_quote": a.user_quote,
            "evidence_since": oldest,
        },
    )


def _entry(ctx: ToolContext, checkpoint_id: int, s: dict[str, Any]) -> ChangeEntry:
    state = ctx.editing.turn(ctx.project.id, ctx.run_id) if ctx.editing is not None else None
    return ChangeEntry(
        project_id=ctx.project.id,
        run_id=ctx.run_id or "",
        plan_step_id=state.active_step_id if state else None,
        call_seq=state.call_seq if state else 0,
        tool=TOOL,
        op="add",
        kind=KIND,
        uuid=f"board-checkpoint-{checkpoint_id}",
        sheet_path="",
        after={"stage": s["stage"], "hash": s["hash"], "user_quote": s["user_quote"]},
    )


async def apply_approve(ctx: ToolContext, cs: Any) -> ToolResult:
    """Re-check guards 1 and 4 and write the checkpoint row and its change entry in one
    transaction (both or neither); no KiCad data is written."""
    if not isinstance(cs, BoardChangeSet) or cs.tool != TOOL or cs.kind != KIND:
        raise TypeError("apply_approve needs a planned pcb_approve change set")
    if ctx.editing is None or ctx.conversation_id is None:
        raise TypeError("pcb_approve needs a conversation and a database")
    s = cs.summary
    a = ApproveArgs(s["stage"], s["hash"], s["user_quote"])
    current = await current_hashes(ctx)  # a KiCad read: before the transaction
    editing = ctx.editing
    db = editing.db
    async with db.transaction():
        guard_hash(a, current)  # TOCTOU
        await guard_no_write_after(ctx, a, s["evidence_since"])  # a write since prepare
        repo = BoardCheckpointRepo(db)
        rel = board_file(ctx.project)
        cp_id = await repo.approve(
            ctx.project.id, rel, a.stage, a.hash, ctx.conversation_id, ctx.run_id or "",
            a.user_quote,
        )  # fmt: skip
        row = await repo.latest(ctx.project.id, rel, a.stage)
        if row is None or row.id != cp_id:  # never: the insert is in this transaction
            raise RuntimeError("the board checkpoint row could not be read back")
        await editing.changes.add_many_in_transaction([_entry(ctx, cp_id, s)])
    step = editing.turn(ctx.project.id, ctx.run_id).active_step_id
    for cb in list(editing.on_changes_recorded):
        try:
            await cb(ctx.project.id, ctx.run_id or "", step, 1)
        except Exception:
            log.exception("on_changes_recorded callback failed")
    data = {
        "stage": a.stage,
        "hash": a.hash,
        "approved_at": row.approved_at,
        "next": _NEXT[a.stage],
        "logical_changes": cs.logical.changes,
        "budget": None,
    }
    return ToolResult(data=data, changed_docs=set())
