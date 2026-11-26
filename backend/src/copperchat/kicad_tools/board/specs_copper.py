"""Specs of the copper tools (P7), appended to `board/specs.py`'s `TOOL_SPECS`: `pcb_zones`,
`pcb_fanout`, `pcb_stitch`, `pcb_tidy_silk` (two-phase board writes). Every schema is
bounded (plan call 6): `additionalProperties: false` at every level, every list
`maxItems`, every number finite bounds."""

from __future__ import annotations

from typing import Any

from copperchat.kicad_tools.board.fanout import (
    FANOUT_DEADLINE_S,
    MAX_STUB_MM,
    MAX_VIA_DIAMETER_MM,
    MAX_VIA_DRILL_MM,
    MIN_STUB_MM,
    apply_fanout,
    prepare_fanout,
)
from copperchat.kicad_tools.board.specs_setup import NAME_PATTERN
from copperchat.kicad_tools.board.stitch import (
    MAX_PITCH_MM,
    MAX_STITCH_VIAS,
    MIN_PITCH_MM,
    STITCH_DEADLINE_S,
    apply_stitch,
    prepare_stitch,
)
from copperchat.kicad_tools.board.zones import (
    ACTIONS,
    MAX_CLEARANCE_MM,
    MAX_COORD_MM,
    MAX_LAYER_CHARS,
    MAX_LAYERS,
    MAX_NET_CHARS,
    MAX_POLY_POINTS,
    MAX_PRIORITY,
    MAX_THERMAL_MM,
    MAX_ZONES,
    apply_zones,
    prepare_zones,
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


def _num(lo: float, hi: float, what: str, *, lo_open: bool = True) -> dict[str, Any]:
    bound = "exclusiveMinimum" if lo_open else "minimum"
    return {"type": "number", bound: lo, "maximum": hi, "description": what}


_NET = {"type": "string", "minLength": 1, "maxLength": MAX_NET_CHARS, "description": "Net name."}

_POINT = _obj(
    {
        "x": _num(-MAX_COORD_MM, MAX_COORD_MM, "x (mm).", lo_open=False),
        "y": _num(-MAX_COORD_MM, MAX_COORD_MM, "y (mm, y down).", lo_open=False),
    },
    ["x", "y"],
)

PCB_ZONES_SCHEMA = _obj(
    {
        "action": {
            "type": "string",
            "enum": list(ACTIONS),
            "description": "add new zones, update existing ones (by name), delete (by name).",
        },
        "zones": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_ZONES,
            "description": "The zones. Omit with action 'add' for the default GND stack: "
            "4 layers GND_In1 (solid plane on In1.Cu, priority 10) + GND_F / GND_B outer "
            "pours (priority 1); 6 layers also GND_In4; 2 layers GND_B (10) + GND_F (1).",
            "items": _obj(
                {
                    "name": {"type": "string", "pattern": NAME_PATTERN},
                    "net": _NET,
                    "layers": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_LAYERS,
                        "items": {"type": "string", "minLength": 1, "maxLength": MAX_LAYER_CHARS},
                        "description": "Enabled copper layers, e.g. ['In1.Cu'].",
                    },
                    "outline": {
                        "anyOf": [
                            {"type": "string", "enum": ["board"]},
                            _obj(
                                {
                                    "polygon": {
                                        "type": "array",
                                        "minItems": 3,
                                        "maxItems": MAX_POLY_POINTS,
                                        "items": _POINT,
                                    }
                                },
                                ["polygon"],
                            ),
                        ],
                        "description": "'board' (the whole rectangular outline; default for "
                        "add) or {polygon: [{x, y}, ...]}.",
                    },
                    "priority": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_PRIORITY,
                        "description": "Fill priority (higher fills first; default 0).",
                    },
                    "clearance": _num(
                        0,
                        MAX_CLEARANCE_MM,
                        "Clearance to other nets (mm; default: the net class clearance).",
                    ),
                    "thermal": _obj(
                        {
                            "gap": _num(0, MAX_THERMAL_MM, "Thermal relief gap (mm)."),
                            "spoke_width": _num(0, MAX_THERMAL_MM, "Spoke width (mm)."),
                        },
                        ["gap", "spoke_width"],
                        "Thermal reliefs for pads (default: inherited from the rules).",
                    ),
                },
                ["name"],
            ),
        },
    },
    ["action"],
)

PCB_ZONES_DESCRIPTION = (
    "Add, update or delete copper zones (GND planes and pours), then refill. "
    "{action: 'add'} alone pours the default GND stack (D1: solid GND plane on In1.Cu, GND "
    "pours on F.Cu/B.Cu below it). add needs name, net, layers; update needs name + the "
    "fields to change; delete needs name only. Needs the approved placement "
    "(placement_not_approved otherwise). Unknown net/layer/zone → not_found (closest "
    "names); add of an existing name → invalid_argument (use update). Returns zones [{name, "
    "action, kiid, net, layers, priority, filled_area_mm2}], m11 (plane fill per layer, "
    "void crossings), next. Cost 1 per zone."
)

_VIA = _obj(
    {
        "diameter": _num(0, MAX_VIA_DIAMETER_MM, "Via diameter (mm)."),
        "drill": _num(0, MAX_VIA_DRILL_MM, "Via drill (mm), smaller than the diameter."),
    },
    ["diameter", "drill"],
    "Via size (default: the net class via).",
)

PCB_FANOUT_SCHEMA = _obj(
    {
        "net": _NET | {"description": "The plane net, e.g. 'GND'."},
        "via": _VIA,
        "max_stub": _num(
            MIN_STUB_MM,
            MAX_STUB_MM,
            "Longest stub outside the pad (mm, default 1.0).",
            lo_open=False,
        ),
    },
    ["net"],
)

PCB_FANOUT_DESCRIPTION = (
    "Connect every SMD pad of a plane net (e.g. GND after pcb_zones) to the planes: a short "
    "straight stub at 0/45/90° + a through via next to the pad (never in the pad), stub "
    "width and via from the net class; both locked (fills keep them). Candidates in 8 "
    "directions x 2 distances are tried in DRC rounds (refill + KiCad DRC; a rejected via "
    "moves on to the next candidate). Pads without a legal via are reported: via_pour "
    "(connected by the pour on their layer) or unconnected {ref, pad, reason: no_candidate "
    "| all_rejected | timeout}. Needs the approved placement. Unknown net → not_found. "
    f"Stops starting rounds after {FANOUT_DEADLINE_S:g} s. Returns smd_pads, "
    "already_connected, fanned_out, via_pour, unconnected, rounds, drc_seconds, seconds, "
    "m11, next. Cost 2 (0 when no pad needs a via)."
)

PCB_STITCH_SCHEMA = _obj(
    {
        "net": _NET | {"description": "The plane net, e.g. 'GND'."},
        "pitch": _num(MIN_PITCH_MM, MAX_PITCH_MM, "Grid pitch (mm, default 5).", lo_open=False),
        "edge_ring": {
            "type": "boolean",
            "description": "Also a ring of vias along the board edge (default false).",
        },
        "via": _VIA,
    },
    ["net"],
)

PCB_STITCH_DESCRIPTION = (
    "Stitching vias of a plane net (e.g. GND after pcb_zones and pcb_fanout): a grid at "
    "`pitch` (default 5 mm), optionally an edge ring, only where zones of the net on two "
    "layers connect them, clear of pads, courtyards and other copper; all locked. KiCad's "
    "DRC legalises them (rejected and dangling vias removed). More than "
    f"{MAX_STITCH_VIAS} vias → invalid_argument (details.candidates; use a larger pitch). "
    "Needs the approved placement. Unknown net → not_found. Stops after "
    f"{STITCH_DEADLINE_S:g} s (unchecked vias removed, rejected_timeout). Returns "
    "candidates, kept, rejected_drc, removed_dangling, rejected_timeout, edge_ring, "
    "edge_ring_kept, drc_seconds, seconds, m11, next. Cost 2 (0 when there is nothing to "
    "place, with a note)."
)

COPPER_SPECS: list[ToolSpec] = [
    edit_spec("pcb_zones", PCB_ZONES_DESCRIPTION, PCB_ZONES_SCHEMA, prepare_zones, apply_zones),
    edit_spec(
        "pcb_fanout", PCB_FANOUT_DESCRIPTION, PCB_FANOUT_SCHEMA, prepare_fanout, apply_fanout
    ),
    edit_spec(
        "pcb_stitch", PCB_STITCH_DESCRIPTION, PCB_STITCH_SCHEMA, prepare_stitch, apply_stitch
    ),
]
