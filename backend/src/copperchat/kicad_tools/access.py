"""The only module that touches kipy object attributes (names confirmed by spike 1C).

Everything returned here is plain JSON-ready data: full UUIDs, millimetres,
canonical KiCad layer names.
"""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kipy.proto.common.types.base_types_pb2 import ElectricalPinType
from kipy.proto.schematic.schematic_jobs_pb2 import SNF_KICAD_XML
from kipy.util.board_layer import canonical_name

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.units import nm_to_mm, pos_mm

# kiapi SchematicSymbolOrientation: SSO_0 = 1 ... SSO_270 = 4.
_SSO_0 = 1


def kiid(item: Any) -> str:
    return str(item.id.value)


def field_text(field: Any) -> str:
    """Text of a schematic `SchematicField` or a board `Field`."""
    text = field.text
    return str(text.value) if hasattr(text, "value") else str(text)


def layer_name(layer: Any) -> str:
    """Canonical KiCad layer name (`F.Cu`, `Edge.Cuts`), never the user's alias."""
    return str(canonical_name(layer))


# -- schematic -------------------------------------------------------------------


def sheet_label(sym_or_sheet: Any) -> str:
    """Human-readable sheet path of a symbol instance or sheet instance (`/`, `/amp/`)."""
    return str(sym_or_sheet.proto.path.path_human_readable) or "/"


def symbol_dict(sym: Any) -> dict[str, Any]:
    """One placed unit of a symbol in one sheet instance (spike 1C: get_symbols())."""
    fields = {"Datasheet": field_text(sym.datasheet_field)}
    fields["Description"] = field_text(sym.description_field)
    for f in sym.user_fields:
        fields[str(f.name)] = field_text(f)
    return {
        "uuid": kiid(sym),
        "ref": str(sym.reference),
        "value": str(sym.value),
        "footprint": field_text(sym.footprint_field),
        "libId": str(sym.definition.id),
        "unit": int(sym.unit),
        "position_mm": pos_mm(sym.position),
        "sheet": sheet_label(sym),
        "fields": {k: v for k, v in fields.items() if v},
    }


def _rotate(x: int, y: int, quarter_turns: int) -> tuple[int, int]:
    # Screen coordinates (y down); SSO_90 turns the symbol counter-clockwise on
    # screen, as KiCad's SYM_ORIENT_90 does.
    for _ in range(quarter_turns % 4):
        x, y = y, -x
    return x, y


def symbol_pins(sym: Any) -> list[dict[str, Any]]:
    """Pins of the placed unit with their absolute schematic position.

    The API reports a symbol definition's pins relative to the symbol anchor
    (already in schematic orientation, y down); the instance transform rotates and
    mirrors them. KiCad exposes no per-instance pin objects, so this is the only
    API-based way to place a pin.
    """
    transform = sym.transform.proto
    quarter_turns = max(0, int(transform.orientation) - _SSO_0)
    body_style = int(sym.body_style or 1)
    pins: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for child in sym.definition.items:
        if child.kind != "SchematicPin":
            continue
        # Untagged (or 0) unit / body style = common to all units / styles.
        unit = child.unit or 0
        style = child.proto.body_style.style if child.proto.HasField("body_style") else 0
        if unit not in (0, int(sym.unit)) or style not in (0, body_style):
            continue
        pin = child.item
        x, y = _rotate(int(pin.position.x), int(pin.position.y), quarter_turns)
        if transform.mirror_x:
            y = -y
        if transform.mirror_y:
            x = -x
        ax, ay = int(sym.position.x) + x, int(sym.position.y) + y
        key = (str(pin.number), ax, ay)
        if key in seen:
            continue
        seen.add(key)
        pins.append(
            {
                "pin": str(pin.number),
                "name": str(pin.name),
                "type": pin_type_name(int(pin.electrical_type)),
                "position_mm": {"x": nm_to_mm(ax), "y": nm_to_mm(ay)},
            }
        )
    return pins


def pin_type_name(value: Any) -> str:
    try:
        name = str(ElectricalPinType.Name(value))
    except ValueError:
        return "unknown"
    return name.removeprefix("EPT_").lower()


def hierarchy(sch: Any) -> list[dict[str, Any]]:
    """Every sheet instance, depth-first from the root."""
    return [
        {
            "name": str(s.name),
            "file": str(s.filename),
            "path": sheet_label(s),
            "page": str(s.page_number),
        }
        for s in sch.get_hierarchy()
    ]


def netlist_nodes(sch: Any) -> dict[str, list[dict[str, str]]]:
    """Net name -> pins `{ref, pin, function, type}`, from KiCad's own netlist.

    The API's `GetSchematicNetlist` lists net members only as bare KIIDs that no
    other call resolves to a symbol pin, so connectivity comes from KiCad's
    netlist export job (RunSchematicJobExportNetlist, KiCad XML) run by the same
    api-server - KiCad output, like the ERC/DRC JSON, never a design-file parse.
    """
    with tempfile.TemporaryDirectory(prefix="cc-netlist-") as tmp:
        out = Path(tmp) / "netlist.xml"
        result = sch.export_netlist(str(out), format=SNF_KICAD_XML)
        if not result.succeeded or not out.is_file():
            raise StructuredToolError(
                "Netlist export failed",
                "KiCad could not export the schematic netlist",
                "Run run_erc to look for schematic errors, then retry",
            )
        return parse_netlist_xml(out.read_text(encoding="utf-8"))


def parse_netlist_xml(text: str) -> dict[str, list[dict[str, str]]]:
    # KiCad's netlist never carries a DTD; refusing one rules out entity expansion.
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise StructuredToolError(
            "Netlist export unreadable",
            "the exported netlist contains a DTD",
            "Report this; KiCad's netlist export changed",
        )
    root = ET.fromstring(text)
    nets: dict[str, list[dict[str, str]]] = {}
    for net in root.iterfind("./nets/net"):
        nets[net.get("name", "")] = [
            {
                "ref": node.get("ref", ""),
                "pin": node.get("pin", ""),
                "function": node.get("pinfunction", ""),
                "type": node.get("pintype", ""),
            }
            for node in net.iterfind("node")
        ]
    return nets


# -- board -----------------------------------------------------------------------


def footprint_dict(fp: Any) -> dict[str, Any]:
    layer = layer_name(fp.layer)
    return {
        "uuid": kiid(fp),
        "ref": field_text(fp.reference_field),
        "value": field_text(fp.value_field),
        "footprint": str(fp.definition.id),
        "position_mm": pos_mm(fp.position),
        "rotation_deg": round(float(fp.orientation.degrees), 3),
        "side": "back" if layer.startswith("B.") else "front",
        "sheet": str(fp.symbol_sheet_name) if fp.symbol_sheet_filename else None,
    }


def pad_dict(pad: Any) -> dict[str, Any]:
    layers = pad.padstack.copper_layers
    size = pos_mm(layers[0].size) if layers else {"x": 0.0, "y": 0.0}
    return {
        "uuid": kiid(pad),
        "number": str(pad.number),
        "net": str(pad.net.name),
        "position_mm": pos_mm(pad.position),
        # Copper size in the pad's own orientation (before footprint rotation).
        "size_mm": size,
    }


def track_dict(track: Any) -> dict[str, Any]:
    out = {
        "uuid": kiid(track),
        "start_mm": pos_mm(track.start),
        "end_mm": pos_mm(track.end),
        "width_mm": nm_to_mm(track.width),
        "layer": layer_name(track.layer),
        "net": str(track.net.name),
    }
    if hasattr(track, "mid"):  # ArcTrack
        out["mid_mm"] = pos_mm(track.mid)
    return out


def via_dict(via: Any) -> dict[str, Any]:
    return {"uuid": kiid(via), "position_mm": pos_mm(via.position), "net": str(via.net.name)}


def box_mm(box: Any) -> dict[str, float]:
    return {
        "x": nm_to_mm(box.pos.x),
        "y": nm_to_mm(box.pos.y),
        "w": nm_to_mm(box.size.x),
        "h": nm_to_mm(box.size.y),
    }


def bounding_boxes(board: Any, items: Sequence[Any]) -> list[dict[str, float] | None]:
    """KiCad-calculated boxes for `items` in one API call."""
    if not items:
        return []
    boxes = board.get_item_bounding_box(list(items))
    return [box_mm(b) if b is not None else None for b in boxes]


def outline_segments(board: Any) -> tuple[list[Any], list[dict[str, Any]]]:
    """Edge.Cuts shapes and the line segments the renderer draws for them."""
    shapes = [s for s in board.get_shapes() if layer_name(s.layer) == "Edge.Cuts"]
    segments = [
        {"start_mm": pos_mm(s.start), "end_mm": pos_mm(s.end)}
        for s in shapes
        if hasattr(s, "start") and hasattr(s, "end")
    ]
    return shapes, segments


def design_rules(board: Any) -> dict[str, float | int]:
    """Board-level minimum constraints (clearance, track width, via size ...) in mm."""
    constraints = board.get_design_rules().proto.rules.constraints
    out: dict[str, float | int] = {}
    for fd in constraints.DESCRIPTOR.fields:
        value = getattr(constraints, fd.name)
        if hasattr(value, "value_nm"):
            out[f"{fd.name}_mm"] = nm_to_mm(value.value_nm)
        elif isinstance(value, int):
            out[fd.name] = value
    return out
