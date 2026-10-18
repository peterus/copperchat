"""Library symbol records from KiCad API protos (port of spikes/2b-libraries/libdata.py).

Everything comes from `GetItemsFromLibrary` (or the flattened `definition` of a
dropped probe placement for derived symbols): no library file is ever read.
Coordinates are library-local, mm, y down, untransformed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from copperchat.kicad_tools.access import pin_type_name
from copperchat.library.index import Scope, SymbolRow, clip

Direction = Literal["left", "right", "up", "down"]
NM_PER_MM = 1_000_000
MAX_DIRECTION_PINS = 4  # pin_directions_by_rotation only for small parts (spike 2D)
_DIR_NAME: dict[tuple[int, int], Direction] = {
    (1, 0): "right",
    (-1, 0): "left",
    (0, -1): "up",
    (0, 1): "down",
}


@dataclass(frozen=True)
class LibPin:
    number: str
    name: str
    type: str
    unit: int  # 0 = common to all units
    body_style: int  # 0 = common to all body styles
    at_mm: tuple[float, float]  # connection point relative to the symbol origin
    direction: Direction | None  # outward (away from the body) at rotation 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "name": self.name,
            "type": self.type,
            "unit": self.unit,
            "offset_mm": [self.at_mm[0], self.at_mm[1]],
            "direction": self.direction,
        }


@dataclass(frozen=True)
class SymbolDetail:
    lib_id: str
    description: str
    keywords: str
    footprint: str
    footprint_filters: list[str]
    datasheet: str
    unit_count: int
    body_styles: int
    is_power: bool
    pins: list[LibPin]
    body_size_mm: tuple[float, float]
    pin_directions_by_rotation: dict[str, dict[str, str]] | None
    reference_prefix: str = ""
    value: str = ""
    # False: a derived symbol whose pins could not be probed (read-only project).
    pins_known: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "lib_id": self.lib_id,
            "description": self.description,
            "keywords": self.keywords,
            "reference_prefix": self.reference_prefix,
            "value": self.value,
            "footprint": self.footprint or None,
            "footprint_filters": self.footprint_filters,
            "datasheet": self.datasheet or None,
            "unit_count": self.unit_count,
            "body_styles": self.body_styles,
            "is_power": self.is_power,
            "pin_count": len({p.number for p in self.pins}) if self.pins_known else None,
            "pins": [p.to_dict() for p in self.pins],
            "body_size_mm": [self.body_size_mm[0], self.body_size_mm[1]],
            "pin_directions_by_rotation": self.pin_directions_by_rotation,
        }


def _mm(nm: int) -> float:
    return round(nm / NM_PER_MM, 4)


def _is_power(sy: Any) -> bool:
    from kipy.proto.schematic import schematic_types_pb2

    kinds = schematic_types_pb2.SchematicSymbolType
    return int(sy.type) in (kinds.SST_GLOBAL_POWER, kinds.SST_LOCAL_POWER)


def _tags(child: Any) -> tuple[int, int]:
    unit = child.unit.unit if child.HasField("unit") else 0
    style = child.body_style.style if child.HasField("body_style") else 0
    return int(unit), int(style)


def _kind(child: Any) -> str:
    return str(child.item.type_url).rsplit(".", 1)[-1]


def has_drawing(sy: Any) -> bool:
    """False for derived (`extends`) symbols: the API returns them without any child
    items (spike 2B, finding 3)."""
    return len(sy.items) > 0


def row_from_proto(sy: Any, scope: Scope) -> SymbolRow:
    pins = pins_from_proto(sy) if has_drawing(sy) else None
    return SymbolRow(
        lib_id=f"{sy.id.library_nickname}:{sy.id.entry_name}",
        description=clip(sy.description_field.text.text),
        keywords=clip(sy.keywords),
        unit_count=int(sy.unit_count) or 1,
        pin_count=None if pins is None else len({p.number for p in pins}),
        footprint=clip(sy.footprint_field.text.text, 256),
        footprint_filters=[clip(f, 100) for f in list(sy.footprint_filters)[:20]],
        scope=scope,
    )


def _outward(orientation: int) -> tuple[int, int] | None:
    """SchematicPinOrientation says where the pin *shape* extends from its connection
    point (towards the body); the outward direction is the opposite."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicPinOrientation as PO

    inward = {
        PO.SPO_RIGHT: (1, 0),
        PO.SPO_LEFT: (-1, 0),
        PO.SPO_UP: (0, -1),
        PO.SPO_DOWN: (0, 1),
    }.get(orientation)  # type: ignore[call-overload]
    return None if inward is None else (-inward[0], -inward[1])


def pins_from_proto(sy: Any) -> list[LibPin]:
    """Pins deduplicated per (unit, number): multi-body-style symbols repeat every pin
    per body style (spike 2B, finding 5); the first (lowest) body style is kept."""
    from kipy.proto.schematic import schematic_types_pb2

    found: dict[tuple[int, str], tuple[int, LibPin]] = {}
    for child in sy.items:
        if _kind(child) != "SchematicPin":
            continue
        pin = schematic_types_pb2.SchematicPin()
        child.item.Unpack(pin)
        unit, style = _tags(child)
        out = _outward(int(pin.orientation))
        lp = LibPin(
            number=clip(str(pin.number), 32),
            name=clip(str(pin.name), 100),
            type=pin_type_name(int(pin.electrical_type)),
            unit=unit,
            body_style=style,
            at_mm=(_mm(pin.position.x_nm), _mm(pin.position.y_nm)),
            direction=None if out is None else _DIR_NAME[out],
        )
        key = (unit, lp.number)
        rank = style or 0
        if key not in found or rank < found[key][0]:
            found[key] = (rank, lp)
    return sorted((lp for _, lp in found.values()), key=lambda p: (p.unit, _natural(p.number)))


def _natural(number: str) -> tuple[int, str]:
    return (int(number), "") if number.isdigit() else (1 << 30, number)


def _first_unit_items(sy: Any) -> list[Any]:
    """Children drawn for unit 1 / body style 1 (or common to all)."""
    return [c for c in sy.items if _tags(c)[0] in (0, 1) and _tags(c)[1] in (0, 1)]


def _points(msg: Any, out: list[tuple[int, int]]) -> None:
    """Every Vector2 inside a shape proto (rectangle corners, polyline nodes, arc points,
    bezier controls); circles are handled by the caller."""
    for desc, value in msg.ListFields():
        if desc.message_type is None:
            continue
        values = value if desc.label == desc.LABEL_REPEATED else [value]
        for v in values:
            if desc.message_type.name == "Vector2":
                out.append((int(v.x_nm), int(v.y_nm)))
            else:
                _points(v, out)


def body_size_mm(sy: Any) -> tuple[float, float]:
    """Width/height of unit 1: graphics bounding box plus pin connection points,
    untransformed (rotation 0). (0, 0) for a symbol without drawing."""
    from kipy.proto.schematic import schematic_types_pb2

    pts: list[tuple[int, int]] = []
    for child in _first_unit_items(sy):
        kind = _kind(child)
        if kind == "SchematicPin":
            pin = schematic_types_pb2.SchematicPin()
            child.item.Unpack(pin)
            pts.append((int(pin.position.x_nm), int(pin.position.y_nm)))
        elif kind == "SchematicGraphicShape":
            shape = schematic_types_pb2.SchematicGraphicShape()
            child.item.Unpack(shape)
            geometry = shape.shape
            if geometry.HasField("circle"):
                c, r = geometry.circle.center, geometry.circle.radius_point
                radius = int(((r.x_nm - c.x_nm) ** 2 + (r.y_nm - c.y_nm) ** 2) ** 0.5)
                pts += [(c.x_nm - radius, c.y_nm - radius), (c.x_nm + radius, c.y_nm + radius)]
            else:
                _points(geometry, pts)
    if not pts:
        return (0.0, 0.0)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (round(_mm(max(xs) - min(xs)), 2), round(_mm(max(ys) - min(ys)), 2))


def _rotate(v: tuple[int, int], quarter_turns: int) -> tuple[int, int]:
    # Screen coordinates (y down); KiCad's 90 deg turns the symbol counter-clockwise
    # on screen (same convention as kicad_tools.access._rotate).
    x, y = v
    for _ in range(quarter_turns % 4):
        x, y = y, -x
    return x, y


def pin_directions_by_rotation(pins: list[LibPin]) -> dict[str, dict[str, str]] | None:
    """Outward direction of every pin of unit 1 at rotation 0/90/180/270, for parts with
    at most 4 pins (LED / diode / transistor orientation, spike 2D); None otherwise."""
    if len({p.number for p in pins}) > MAX_DIRECTION_PINS:
        return None
    unit_pins = [p for p in pins if p.unit in (0, 1) and p.direction is not None]
    if not unit_pins:
        return None
    vectors = {name: vec for vec, name in _DIR_NAME.items()}
    out: dict[str, dict[str, str]] = {}
    for turns, rot in enumerate(("0", "90", "180", "270")):
        out[rot] = {
            p.number: _DIR_NAME[_rotate(vectors[p.direction], turns)]  # type: ignore[index]
            for p in unit_pins
        }
    return out


def detail_from_proto(sy: Any, drawing: Any | None = None, pins_known: bool = True) -> SymbolDetail:
    """`sy` = the library proto (fields); `drawing` = the proto that carries the pins and
    graphics (the flattened probe definition of a derived symbol; default `sy`)."""
    drawing = sy if drawing is None else drawing
    pins = pins_from_proto(drawing) if pins_known else []
    return SymbolDetail(
        lib_id=f"{sy.id.library_nickname}:{sy.id.entry_name}",
        description=clip(sy.description_field.text.text),
        keywords=clip(sy.keywords),
        footprint=clip(sy.footprint_field.text.text, 256),
        footprint_filters=[clip(f, 100) for f in list(sy.footprint_filters)[:20]],
        datasheet=clip(sy.datasheet_field.text.text),
        unit_count=int(sy.unit_count) or 1,
        body_styles=max(1, len(sy.body_style)),
        is_power=_is_power(sy),
        pins=pins,
        body_size_mm=body_size_mm(drawing) if pins_known else (0.0, 0.0),
        pin_directions_by_rotation=pin_directions_by_rotation(pins) if pins_known else None,
        reference_prefix=clip(sy.reference_field.text.text.rstrip("?"), 32),
        value=clip(sy.value_field.text.text, 256),
        pins_known=pins_known,
    )
