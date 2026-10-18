"""The only module that touches kipy object attributes (names confirmed by spike 1C).

Everything returned here is plain JSON-ready data: full UUIDs, millimetres,
canonical KiCad layer names.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kipy.proto.common.types.base_types_pb2 import ElectricalPinType
from kipy.util.board_layer import canonical_name

from copperchat.kicad_tools.units import nm_to_mm, pos_mm

if TYPE_CHECKING:
    from copperchat.kicad_session import KicadHandle

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


# KiCad's canonical names (files, netlists, ERC) where the API enum spells them otherwise.
_PIN_TYPE_NAMES = {"tristate": "tri_state", "power_input": "power_in", "power_output": "power_out"}


def pin_type_name(value: Any) -> str:
    try:
        name = str(ElectricalPinType.Name(value)).removeprefix("EPT_").lower()
    except ValueError:
        return "unknown"
    return _PIN_TYPE_NAMES.get(name, name)


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


PinKey = tuple[str, str, str]  # (sheet_path, symbol_uuid, pin_number)


def sheet_path_key(path: Any) -> str:
    """`/`-joined KIID path of a sheet instance, root = `/<root uuid>` - the string
    `kicad-cli sch diff` and KiCanvas use (spike 2C). Takes a kipy SheetPath or its proto."""
    proto = getattr(path, "proto", path)
    return "/" + "/".join(str(p.value) for p in proto.path)


@dataclass(frozen=True)
class Netlist:
    """Net name -> the symbol pins on it, live from the api-server (unsaved edits too)."""

    nets: dict[str, frozenset[PinKey]]
    _by_pin: dict[PinKey, str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        by_pin = {key: name for name, keys in self.nets.items() for key in keys}
        object.__setattr__(self, "_by_pin", by_pin)

    def net_of(self, key: PinKey) -> str | None:
        return self._by_pin.get(key)

    def partition(self) -> frozenset[frozenset[PinKey]]:
        """The pin groups, without net names (compares connectivity across renames)."""
        return frozenset(self.nets.values())


def live_netlist(h: KicadHandle, symbols: Sequence[Any] | None = None) -> Netlist:
    """KiCad's live connectivity (`GetSchematicNetlist`), resolved to symbol pins.

    The netlist lists members as pin KIIDs per sheet path; a pin KIID repeats in every
    instance of a shared sub-sheet, so each is resolved per (sheet path, pin KIID)
    through the placed symbols' definition pins (spike 2A, Q8: identical to
    kicad-cli's netlist on a saved copy, without saving). `symbols`: the caller's
    `get_symbols()` result of this same call, to avoid fetching them twice.
    """
    sch = h.schematic()
    owner: dict[tuple[str, str], PinKey] = {}
    for sym in sch.get_symbols() if symbols is None else symbols:
        path = sheet_path_key(sym.path)
        uuid = kiid(sym)
        for pin in sym.definition.pins:
            owner[(path, kiid(pin))] = (path, uuid, str(pin.number))
    members: dict[str, set[PinKey]] = {}
    for net in sch.get_netlist():
        keys = members.setdefault(str(net.name), set())
        for sheet in net.sheets:
            path = sheet_path_key(sheet.path)
            for item in sheet.items:
                key = owner.get((path, str(item.value)))
                if key is not None:
                    keys.add(key)
    return Netlist({name: frozenset(keys) for name, keys in members.items() if keys})


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
