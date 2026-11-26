"""Specs of the own-router tools (P5b), appended to `board/specs.py`'s `TOOL_SPECS`:
`pcb_route` and `pcb_unroute` (two-phase board writes). Every schema is bounded (plan call
6): `additionalProperties: false` at every level, every list `maxItems`, every number
finite bounds. `pcb_unroute`'s "nets or region" rule is checked in code (a top-level
`anyOf` is not allowed in tool schemas)."""

from __future__ import annotations

from typing import Any

from copperchat.kicad_tools.board.route import (
    MAX_COORD_MM,
    MAX_LAYERS,
    MAX_NET_CHARS,
    MAX_NETS,
    MAX_REGION_MM,
    ROUTE_DEADLINE_S,
    apply_route,
    apply_unroute,
    prepare_route,
    prepare_unroute,
)
from copperchat.tools.spec import ToolSpec, edit_spec


def _obj(props: dict[str, Any], required: list[str], what: str = "") -> dict[str, Any]:
    o: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }
    if what:
        o["description"] = what
    return o


def _nets(lo: int, what: str) -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": lo,
        "maxItems": MAX_NETS,
        "items": {"type": "string", "minLength": 1, "maxLength": MAX_NET_CHARS},
        "description": what,
    }


def _coord(what: str) -> dict[str, Any]:
    return {
        "type": "number",
        "minimum": -MAX_COORD_MM,
        "maximum": MAX_COORD_MM,
        "description": what,
    }


def _size(what: str) -> dict[str, Any]:
    return {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_REGION_MM, "description": what}


PCB_ROUTE_SCHEMA = _obj(
    {
        "nets": _nets(
            1,
            "Nets to route (exact names, e.g. pcb_analyze's critical_nets; at most 50 per "
            "call, never ground nets).",
        ),
        "layers": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_LAYERS,
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
            "description": "Copper layers the router may use, e.g. ['F.Cu', 'B.Cu']; default: "
            "every copper layer except the GND plane layers (In1.Cu on 4 layers, In1.Cu + "
            "In4.Cu on 6). Power nets prefer the outer layers.",
        },
        "width_from": {
            "type": "string",
            "enum": ["netclass"],
            "description": "Where widths, clearance and via size come from (only "
            "'netclass': each net's class from pcb_set_rules).",
        },
        "lock": {
            "type": "boolean",
            "description": "Lock the routed tracks and vias (default true: Freerouting and "
            "later fills keep them).",
        },
        "allow_vias": {
            "type": "boolean",
            "description": "Allow layer changes through vias (default true).",
        },
    },
    ["nets"],
)

PCB_ROUTE_DESCRIPTION = (
    "Route nets with CopperChat's own octilinear router: straight segments with 45° turns "
    "only, connection by connection (shortest first), widths/clearance/vias from the net "
    "class; every routed connection is checked by KiCad's DRC before it is kept (rejected → "
    "retried with more clearance, then reported failed) and locked. Differential pairs whose "
    "both nets are requested are routed coupled; their length mismatch and gap are "
    "reported. Needs the approved placement (placement_not_approved otherwise: render the "
    "placement, pcb_check quick, ask the user, pcb_approve). Unknown nets/layers → "
    "not_found (closest names); ground nets → invalid_argument (planes and pcb_fanout "
    f"connect ground). Stops after {ROUTE_DEADLINE_S:g} s with the rest failed 'timeout' "
    "(what was routed stays). Returns per net status routed | partial | failed | "
    "already_routed, connections, routed, failed [{a, b, reason: no_path | too_large | "
    "timeout | drc_rejected: …}], length_mm, vias, locked, coupling; pairs (coupled / "
    "uncoupled connections, length_mismatch, gap_min/max, warning); totals; bends (M4, M7 "
    "of the new copper, expected 0); drc_errors; next. Cost ceil(nets / 5)."
)

PCB_UNROUTE_SCHEMA = _obj(
    {
        "nets": _nets(0, "Nets whose copper is selected."),
        "region": _obj(
            {
                "x": _coord("Region left (mm)."),
                "y": _coord("Region top (mm, y down)."),
                "w": _size("Region width (mm)."),
                "h": _size("Region height (mm)."),
            },
            ["x", "y", "w", "h"],
            "Copper touching this board rectangle is selected (whole connections).",
        ),
        "include_locked": {
            "type": "boolean",
            "description": "Also delete locked connections (default false: they are kept and "
            "reported as skipped_locked).",
        },
        "unlock_only": {
            "type": "boolean",
            "description": "Do not delete: only clear the locked flag of the selected locked "
            "tracks/vias (ladder step before a re-fill). Ground/plane-net items are only "
            "unlocked when their net is named in nets.",
        },
    },
    [],
)

PCB_UNROUTE_DESCRIPTION = (
    "Remove routed copper: every track and via of the selected connections (by nets and/or "
    "a region; a connection is removed whole, so nothing dangles). Needs nets or region "
    "(invalid_argument otherwise); unknown nets → not_found. Locked connections stay unless "
    "include_locked. With unlock_only: nothing is deleted, the locked flag of the selected "
    "items is cleared; returns unlocked, unlocked_nets (pass them to pcb_route_fill "
    "relock_nets) and skipped_plane_items. Otherwise returns removed_tracks, removed_vias, "
    "nets, skipped_locked, skipped_locked_nets. Nothing selected → nothing written, cost 0; "
    "else cost 1."
)

ROUTE_SPECS: list[ToolSpec] = [
    edit_spec("pcb_route", PCB_ROUTE_DESCRIPTION, PCB_ROUTE_SCHEMA, prepare_route, apply_route),
    edit_spec(
        "pcb_unroute",
        PCB_UNROUTE_DESCRIPTION,
        PCB_UNROUTE_SCHEMA,
        prepare_unroute,
        apply_unroute,
    ),
]
