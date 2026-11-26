"""Spec of the Freerouting fill (P6b), appended to `board/specs.py`'s `TOOL_SPECS`:
`pcb_route_fill` (two-phase board write). Bounded schema (plan call 6):
`additionalProperties: false`, every list `maxItems`, every number finite bounds."""

from __future__ import annotations

from typing import Any

from copperchat.kicad_tools.board.fill import (
    FILL_COST,
    MAX_NET_CHARS,
    MAX_PLANE_LAYERS,
    MAX_RELOCK,
    PASSES_DEFAULT,
    PASSES_MAX,
    PASSES_MIN,
    POST_DEADLINE_S,
    RIP_RATIO_MAX,
    TIMEOUT_DEFAULT_S,
    TIMEOUT_MAX_S,
    TIMEOUT_MIN_S,
    apply_fill,
    prepare_fill,
)
from copperchat.tools.spec import ToolSpec, edit_spec

PCB_ROUTE_FILL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "timeout_s": {
            "type": "integer",
            "minimum": TIMEOUT_MIN_S,
            "maximum": TIMEOUT_MAX_S,
            "default": TIMEOUT_DEFAULT_S,
            "description": f"Freerouting's wall-clock limit in seconds (default "
            f"{TIMEOUT_DEFAULT_S}).",
        },
        "passes": {
            "type": "integer",
            "minimum": PASSES_MIN,
            "maximum": PASSES_MAX,
            "default": PASSES_DEFAULT,
            "description": f"Freerouting's autorouter passes (default {PASSES_DEFAULT}; more "
            "passes = ladder step (a)).",
        },
        "relock_nets": {
            "type": "array",
            "maxItems": MAX_RELOCK,
            "items": {"type": "string", "minLength": 1, "maxLength": MAX_NET_CHARS},
            "description": "Nets unlocked before with pcb_unroute {unlock_only: true} (ladder "
            "step (b)): Freerouting may re-route them; afterwards they are locked again if "
            "they pass M4/M7 (relocked / relock_failed). Never ground nets.",
        },
        "plane_layers": {
            "type": "array",
            "maxItems": MAX_PLANE_LAYERS,
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
            "description": "Inner copper layers the fill must keep free for the planes "
            "(default: the inner layers where ground or supply zones cover the board, plus "
            "In1.Cu on 4 layers / In1.Cu + In4.Cu on 6 while no ground plane exists; none "
            "on 2).",
        },
    },
    "required": [],
    "additionalProperties": False,
}

PCB_ROUTE_FILL_DESCRIPTION = (
    "Route every remaining connection with Freerouting, then make it meet the quality bar: "
    "locked work (pcb_route, fanout, stitching) is kept, the fill's ground copper is "
    "deleted (planes and pcb_fanout connect ground), 90° corners are chamfered to 45°, "
    "connections with acute or off-angle bends, micro-jogs, short chamfers or detours are "
    "ripped and re-routed by the own router; the plane layers stay free. Refused "
    f"(fill_not_acceptable, reason rip_ratio, worst_areas) when more than "
    f"{RIP_RATIO_MAX:.0%} of the fill's connections would need that - improve channels or "
    "the critical routing first. Any failure or a stop restores the board as it was. Needs "
    "the approved placement (placement_not_approved) and the Freerouting runtime "
    "(freerouting_unavailable). Takes Freerouting's timeout_s plus up to "
    f"{POST_DEADLINE_S:g} s post-processing. Returns completion (routed / all non-ground "
    "connections), fill_routed, ground_items_deleted, chamfered, merged, ripped, rerouted, "
    "rejected [{connection, reason, x, y}], relocked, relock_failed, open [{connection, a, "
    "b, x, y}] (≤ 50), positions_restored, freerouting {seconds, passes, timed_out}, metrics "
    "(M4/M5/M7/M12), next (the ladder: more passes → unlock + relock_nets → pcb_route → ask "
    f"the user; never done with open connections). Cost {FILL_COST}."
)

FILL_SPECS: list[ToolSpec] = [
    edit_spec(
        "pcb_route_fill",
        PCB_ROUTE_FILL_DESCRIPTION,
        PCB_ROUTE_FILL_SCHEMA,
        prepare_fill,
        apply_fill,
    ),
]
