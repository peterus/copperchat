"""Target geometry -> KiCad API items for S3 `rebuild` (spec §9.3): the inverse of
`model.geometry_from_proto` for what `build.build_target` produces (pads, segments,
rects, polygons, circles, arcs, texts, custom fields, 3D model links).

Every message starts from a copy of the seed's own item where one exists (pad, user
text, field), so KiCad's defaults (teardrop settings, text justification, ...) stay as
KiCad wrote them; only the modelled properties are set. Ported from `build_l201` in
`tests/kicad/test_kicad_api_patches.py` (reference implementation, not imported)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.common.types import base_types_pb2 as base
from kipy.proto.common.types import enums_pb2 as en

from copperchat.kicad_tools.footprints.build import TEXT_THICKNESS, user_text_thickness
from copperchat.kicad_tools.footprints.model import Model3D, Pad, Shape, Text

NM_PER_MM = 1_000_000

# The footprint's own fields (FOOTPRINT mandatory fields): never deleted, updated in
# place. Every other `Field` item is a custom field.
MANDATORY_FIELDS = frozenset({"Reference", "Value", "Datasheet", "Description", "Footprint"})

_PAD_TYPES = {
    "smd": bt.PadType.PT_SMD,
    "tht": bt.PadType.PT_PTH,
    "npth": bt.PadType.PT_NPTH,
    "connector": bt.PadType.PT_EDGE_CONNECTOR,
}
_PAD_SHAPES = {
    "rect": bt.PadStackShape.PSS_RECTANGLE,
    "roundrect": bt.PadStackShape.PSS_ROUNDRECT,
    "circle": bt.PadStackShape.PSS_CIRCLE,
    "oval": bt.PadStackShape.PSS_OVAL,
}
_COPPER = ("F.Cu", *(f"In{i}.Cu" for i in range(1, 31)), "B.Cu")
_WILDCARD = {
    "*.Cu": _COPPER,
    "*.Mask": ("F.Mask", "B.Mask"),
    "*.Paste": ("F.Paste", "B.Paste"),
}


def nm(mm: float) -> int:
    return round(mm * NM_PER_MM)


def kiid() -> base.KIID:
    return base.KIID(value=str(uuid.uuid4()))


def layer(name: str) -> int:
    """`F.Cu` -> `BL_F_Cu`, `User.1` -> `BL_User_1`."""
    return int(bt.BoardLayer.Value("BL_" + name.replace(".", "_")))


def pad_layers(names: Iterable[str]) -> list[int]:
    """Board layers of a pad; `*.Cu` / `*.Mask` / `*.Paste` expand to every layer of
    that kind."""
    out: list[int] = []
    for name in names:
        for one in _WILDCARD.get(name, (name,)):
            value = layer(one)
            if value not in out:
                out.append(value)
    return out


def _xy(vec: Any, p: tuple[float, float]) -> None:
    vec.x_nm, vec.y_nm = nm(p[0]), nm(p[1])


def pad_proto(template: Any, pad: Pad) -> Any:
    """`pad` as a `Pad` message, based on the seed's pad `template`."""
    p = bt.Pad()
    p.CopyFrom(template)
    p.id.CopyFrom(kiid())
    p.number = pad.number
    p.type = _PAD_TYPES[pad.type]
    _xy(p.position, pad.at)
    stack = p.pad_stack
    stack.type = bt.PadStackType.PST_NORMAL
    del stack.layers[:]
    stack.layers.extend(pad_layers(pad.layers))
    stack.angle.value_degrees = pad.rotation
    del stack.copper_layers[1:]
    cl = stack.copper_layers[0] if stack.copper_layers else stack.copper_layers.add()
    cl.layer = bt.BoardLayer.BL_F_Cu
    cl.shape = _PAD_SHAPES[pad.shape]
    _xy(cl.size, pad.size)
    if pad.roundrect_ratio is not None:
        cl.corner_rounding_ratio = pad.roundrect_ratio
    cl.offset.x_nm, cl.offset.y_nm = 0, 0
    zone = stack.zone_settings  # build.PAD_DEFAULT_EXTRA
    zone.zone_connection = bt.ZoneConnectionStyle.ZCS_INHERITED
    zone.thermal_spokes.angle.value_degrees = 90.0
    drill = stack.drill
    if pad.drill is None:
        drill.diameter.x_nm, drill.diameter.y_nm = 0, 0
        drill.shape = bt.DrillShape.DS_UNDEFINED
        drill.start_layer = drill.end_layer = bt.BoardLayer.BL_UNDEFINED
    else:
        _xy(drill.diameter, pad.drill.size)
        oval = pad.drill.shape == "oval"
        drill.shape = bt.DrillShape.DS_OBLONG if oval else bt.DrillShape.DS_CIRCLE
        drill.start_layer = bt.BoardLayer.BL_F_Cu
        drill.end_layer = bt.BoardLayer.BL_B_Cu
    return p


def shape_proto(shape: Shape) -> Any:
    """`shape` (unfilled, solid stroke) as a `BoardGraphicShape`."""
    g = bt.BoardGraphicShape()
    g.id.CopyFrom(kiid())
    g.layer = layer(shape.layer)  # type: ignore[assignment]
    attrs = g.shape.attributes
    attrs.stroke.width.value_nm = nm(shape.width)
    attrs.stroke.style = en.StrokeLineStyle.SLS_SOLID
    attrs.fill.fill_type = base.GraphicFillType.GFT_UNFILLED
    pts = shape.points
    if shape.kind == "segment":
        _xy(g.shape.segment.start, pts[0])
        _xy(g.shape.segment.end, pts[1])
    elif shape.kind == "rect":
        _xy(g.shape.rectangle.top_left, pts[0])
        _xy(g.shape.rectangle.bottom_right, pts[1])
    elif shape.kind == "polygon":
        poly = g.shape.polygon.polygons.add()
        poly.outline.closed = shape.closed
        for pt in pts:
            _xy(poly.outline.nodes.add().point, pt)
    elif shape.kind == "arc":
        _xy(g.shape.arc.start, pts[0])
        _xy(g.shape.arc.mid, pts[1])
        _xy(g.shape.arc.end, pts[2])
    elif shape.kind == "circle" and shape.center is not None and shape.size is not None:
        cx, cy = shape.center
        _xy(g.shape.circle.center, shape.center)
        _xy(g.shape.circle.radius_point, (cx + shape.size[0] / 2, cy))
    else:
        raise ValueError(f"cannot write a {shape.kind} shape")
    return g


def _set_text(board_text: Any, text: Text, thickness: float) -> None:
    board_text.layer = layer(text.layer)
    t = board_text.text
    _xy(t.position, text.at)
    _xy(t.attributes.size, text.size)
    t.attributes.angle.value_degrees = text.rotation
    t.attributes.stroke_width.value_nm = nm(thickness)


def user_text_proto(template: Any | None, text: Text) -> Any:
    """A user `BoardText` (e.g. `${REFERENCE}` on F.Fab), based on the seed's own."""
    t = bt.BoardText()
    if template is not None:
        t.CopyFrom(template)
    else:
        a = t.text.attributes
        a.horizontal_alignment = en.HorizontalAlignment.HA_CENTER
        a.vertical_alignment = en.VerticalAlignment.VA_CENTER
        a.visible = True
        a.keep_upright = True
    t.id.CopyFrom(kiid())
    t.text.text = text.value
    _set_text(t, text, user_text_thickness(text.size[0]))
    return t


def field_text(field: Any, text: Text | None, value: str) -> None:
    """Set a mandatory field (Reference / Value / Datasheet) in place: its value and,
    for Reference / Value, the text geometry."""
    field.text.text.text = value
    if text is not None:
        _set_text(field.text, text, TEXT_THICKNESS)


def custom_field_proto(template: Any, name: str, value: str) -> Any:
    """A hidden custom field, based on a mandatory field of the seed (`custom_field` of
    the reference implementation)."""
    f = bt.Field()
    f.CopyFrom(template)
    f.ClearField("id")
    f.name = name
    f.text.id.CopyFrom(kiid())
    f.text.text.text = value
    f.visible = False
    return f


def model_proto(model: Model3D) -> Any:
    m = bt.Footprint3DModel()
    m.filename = model.path
    for vec, value in ((m.offset, model.offset), (m.rotation, model.rotation),
                       (m.scale, model.scale)):  # fmt: skip
        vec.x_nm, vec.y_nm, vec.z_nm = value
    m.visible = True
    m.opacity = 1.0
    return m


MOUNTING = {
    "smd": bt.FootprintMountingStyle.FMS_SMD,
    "tht": bt.FootprintMountingStyle.FMS_THROUGH_HOLE,
    "unspecified": bt.FootprintMountingStyle.FMS_UNSPECIFIED,
}
