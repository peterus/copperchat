from __future__ import annotations

from typing import Any

from copperchat.kicad_tools import checks, pcb, project, schematic
from copperchat.tools.spec import ToolSpec

_FILTER = {
    "filter": {
        "type": "string",
        "description": "Case-insensitive glob on reference, value or footprint, e.g. 'R*', "
        "'*0805*'. Omit to list all.",
    }
}
_REF = {"ref": {"type": "string", "description": "Reference designator, e.g. 'U1'."}}
_NET = {"name": {"type": "string", "description": "Exact net name, e.g. 'GND'."}}
_CHECK = {
    "severity": {"enum": list(checks.SEVERITIES), "description": "Only this severity."},
    "type": {"type": "string", "description": "Glob on the violation type, e.g. 'clearance*'."},
}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "project_info",
        "Project overview: files, sheet hierarchy, board outline size, enabled layers, "
        "copper layer count, net/part counts, design-rule minimums and KiCad version. "
        "All lengths in mm. Call this first.",
        _obj({}),
        project.project_info,
    ),
    ToolSpec(
        "sch_list_symbols",
        "List schematic symbols: one entry per placed unit and sheet instance with ref, "
        "value, footprint, library id, fields, unit, sheet path, mm position and full UUID.",
        _obj(_FILTER),
        schematic.sch_list_symbols,
    ),
    ToolSpec(
        "sch_get_symbol",
        "One schematic part by reference: its placed units (UUID, sheet, mm position) and "
        "every pin with name, electrical type, mm position and the net it connects to.",
        _obj(_REF, ["ref"]),
        schematic.sch_get_symbol,
    ),
    ToolSpec(
        "sch_get_net",
        "All schematic pins on a net (ref, pin number, pin function and type, mm position, "
        "sheet, symbol UUID), from KiCad's own netlist.",
        _obj(_NET, ["name"]),
        schematic.sch_get_net,
    ),
    ToolSpec(
        "pcb_list_footprints",
        "List PCB footprints (ref, value, library footprint, mm position, rotation, side, "
        "sheet, full UUID), optionally filtered.",
        _obj(_FILTER),
        pcb.pcb_list_footprints,
    ),
    ToolSpec(
        "pcb_get_footprint",
        "One footprint by reference with its mm bounding box and all pads (number, net, "
        "mm position and size, full UUID).",
        _obj(_REF, ["ref"]),
        pcb.pcb_get_footprint,
    ),
    ToolSpec(
        "pcb_get_net",
        "Pads, tracks, vias and unrouted connections (airwires between copper islands) of a "
        "PCB net, with full UUIDs and mm coordinates.",
        _obj(_NET, ["name"]),
        pcb.pcb_get_net,
    ),
    ToolSpec(
        "run_erc",
        "Run KiCad's electrical rules check on the schematic as currently loaded; returns "
        "violations with a stable id (the one plan_step_done takes in erc_justifications), "
        "type, severity, sheet, mm position and item UUIDs. Filters: severity, type "
        "(glob), sheet (name or path). Paged: limit (default 50, max 200) and offset; "
        "total and counts by type / sheet cover everything that matches.",
        _obj(
            {
                **_CHECK,
                "sheet": {
                    "type": "string",
                    "description": "Only a sheet: name ('Power') or path ('/Power/'); '/' is root.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": checks.ERC_PAGE_MAX},
                "offset": {"type": "integer", "minimum": 0},
            }
        ),
        checks.run_erc,
    ),
    ToolSpec(
        "run_drc",
        "Run KiCad's design rules check on the board as currently loaded, including "
        "unconnected items and schematic parity; returns violations with mm positions.",
        _obj(_CHECK),
        checks.run_drc,
    ),
]
