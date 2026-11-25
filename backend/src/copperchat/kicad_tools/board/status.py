"""`pcb_status` (spec §7.1): the cheap board summary the agent reads at the start of every
turn. A read tool: one board-model read plus one DRC task (for the unrouted count), never
the edit lock, nothing written. Checkpoint and last-check fields come with P3b."""

from __future__ import annotations

from collections import Counter
from typing import Any

from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.kicad_tools.board import api
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
    return ToolResult(data=status_data(model, drc.unconnected))
