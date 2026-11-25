"""`pcb_status` (spec §7.1): the cheap board summary the agent reads at the start of every
turn. A read tool: one board-model read plus one DRC task (for the unrouted count), never
the edit lock, nothing written. With a database it adds the checkpoint states (P3b,
B-C10) and the newest check of this conversation."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.db.repos.board import BoardReviewRepo, board_file
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.checkpoints import checkpoint_state
from copperchat.kicad_tools.board.read import board_model
from copperchat.kicad_tools.filtering import natural_key
from copperchat.tools.spec import ToolContext, ToolResult

MAX_UNPLACED_REFS = 50
# The DRC task behind `unrouted` (B-C4: an explicit, longer session.call timeout).
STATUS_DRC_TIMEOUT_S = 60.0


def _outline(m: BoardModel) -> dict[str, Any] | None:
    o = m.outline
    if o.kind == "none":
        return None
    return {
        "kind": o.kind,
        "x": round(o.x, 4),
        "y": round(o.y, 4),
        "w": round(o.w, 4),
        "h": round(o.h, 4),
        "corner_radius": round(o.corner_radius, 4),
    }


def connection_count(m: BoardModel) -> int:
    """Pad-to-pad connections a fully routed board has: the sum of (pads - 1) over the
    nets with at least two pads (single-pad nets and netless pads need none)."""
    pads = Counter(pad.net for part in m.parts for pad in part.pads if pad.net)
    return sum(n - 1 for n in pads.values() if n >= 2)


def status_data(m: BoardModel, unrouted: int) -> dict[str, Any]:
    """The `pcb_status` result of a board (pure)."""
    unplaced = sorted((p.ref for p in m.parts if not m.placed(p)), key=natural_key)
    connections = connection_count(m)
    return {
        "exists": True,
        "outline": _outline(m),
        "copper_layers": len(m.copper_layers),
        "footprints": len(m.parts),
        "placed": len(m.parts) - len(unplaced),
        "unplaced": len(unplaced),
        "unplaced_refs": unplaced[:MAX_UNPLACED_REFS],
        "unplaced_omitted": max(0, len(unplaced) - MAX_UNPLACED_REFS),
        "connections": connections,
        "routed": max(0, connections - unrouted),
        "unrouted": unrouted,
        "zones": sum(1 for z in m.zones if not z.rule_area),  # copper zones; keepouts aside
        "placement_hash": placement_hash(m),
        "board_hash": board_hash(m),
    }


async def pcb_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """`{exists: false, suggestion}` without a board (no error); else the board summary
    (`status_data`). `unrouted` = the unconnected-items markers of KiCad's DRC task."""
    if ctx.project.pcb_file is None:
        return ToolResult(data={"exists": False, "suggestion": "run pcb_sync_from_schematic"})
    model = await board_model(ctx)
    drc = await ctx.session.call(
        lambda h: api.run_drc_task(h, timeout_s=STATUS_DRC_TIMEOUT_S),
        timeout=api.drc_call_timeout(STATUS_DRC_TIMEOUT_S),
    )
    # the hashes serialise the whole board: in a thread, not on the event loop
    data = await asyncio.to_thread(status_data, model, drc.unconnected)
    data["checkpoints"], data["last_check"] = await _review_fields(ctx, model, data)
    return ToolResult(data=data)


async def _review_fields(
    ctx: ToolContext, model: BoardModel, data: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(`checkpoints`, `last_check`): both stages' checkpoint state with stale flags, and
    the newest `pcb_check` of this board in this conversation (`for_current`: it checked
    the current state); None without a database (and `last_check` without a
    conversation)."""
    if ctx.editing is None:
        return None, None
    db = ctx.editing.db
    current = {"placement": data["placement_hash"], "board": data["board_hash"]}
    checkpoints = dict(await checkpoint_state(db, ctx.project, model, current=current))
    if ctx.conversation_id is None:
        return checkpoints, None
    row = await BoardReviewRepo(db).last_check(
        ctx.project.id, board_file(ctx.project), ctx.conversation_id
    )
    if row is None:
        return checkpoints, None
    last = {
        "level": row.check_level,
        "hard_failures": row.hard_failures,
        "m1_pass": row.m1_pass == 1,
        "created_at": row.created_at,
        "for_current": row.placement_hash == data["placement_hash"]
        and row.board_hash == data["board_hash"],
    }
    return checkpoints, last
