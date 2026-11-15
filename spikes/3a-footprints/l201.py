"""Acceptance case L201 = Bourns SRN3012BTA-2R2M as API protobuf items, and a geometry
read-back that turns the footprint document's items into plain numbers (mm)."""

from __future__ import annotations

import uuid

from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.common.types import base_types_pb2 as base, enums_pb2 as en

from fpdoc import NM, kind, mm

NAME = "L_Bourns_SRN3012BTA"
DESCRIPTION = "Bourns SRN3012BTA SMD power inductor, 3.0x3.0x1.2 mm, https://www.bourns.com/docs/product-datasheets/srn3012bta.pdf"
KEYWORDS = "Bourns SRN3012BTA inductor SMD"
SOURCE = "Bourns SRN3012BTA datasheet, recommended land pattern (CopperChat spike 3A)"

PAD_SIZE = (1.3, 3.5)
PAD_X = 1.1
CRTYD = (4.0, 4.0)
FAB = (3.0, 3.0)


def _kiid() -> base.KIID:
    return base.KIID(value=str(uuid.uuid4()))


def pad(template: bt.Pad, number: str, x: float) -> bt.Pad:
    """An SMD rect pad cloned from the seed's pad (keeps the SMD layer set F.Cu/F.Paste/F.Mask)."""
    p = bt.Pad()
    p.CopyFrom(template)
    p.id.CopyFrom(_kiid())
    p.number = number
    p.type = bt.PadType.PT_SMD
    p.position.x_nm, p.position.y_nm = mm(x), 0
    del p.pad_stack.copper_layers[1:]
    cl = p.pad_stack.copper_layers[0]
    cl.layer = bt.BoardLayer.BL_F_Cu
    cl.shape = bt.PadStackShape.PSS_RECTANGLE
    cl.size.x_nm, cl.size.y_nm = mm(PAD_SIZE[0]), mm(PAD_SIZE[1])
    cl.corner_rounding_ratio = 0
    p.pad_stack.angle.value_degrees = 0
    return p


def rect(layer: int, w: float, h: float, stroke: float) -> bt.BoardGraphicShape:
    s = bt.BoardGraphicShape()
    s.id.CopyFrom(_kiid())
    s.layer = layer
    s.shape.attributes.stroke.width.value_nm = mm(stroke)
    s.shape.attributes.stroke.style = en.StrokeLineStyle.SLS_SOLID
    s.shape.rectangle.top_left.x_nm, s.shape.rectangle.top_left.y_nm = mm(-w / 2), mm(-h / 2)
    s.shape.rectangle.bottom_right.x_nm, s.shape.rectangle.bottom_right.y_nm = mm(w / 2), mm(h / 2)
    return s


def segment(layer: int, a: tuple[float, float], b: tuple[float, float], stroke: float) -> bt.BoardGraphicShape:
    s = bt.BoardGraphicShape()
    s.id.CopyFrom(_kiid())
    s.layer = layer
    s.shape.attributes.stroke.width.value_nm = mm(stroke)
    s.shape.attributes.stroke.style = en.StrokeLineStyle.SLS_SOLID
    s.shape.segment.start.x_nm, s.shape.segment.start.y_nm = mm(a[0]), mm(a[1])
    s.shape.segment.end.x_nm, s.shape.segment.end.y_nm = mm(b[0]), mm(b[1])
    return s


def fab_ref_text() -> bt.BoardText:
    t = bt.BoardText()
    t.id.CopyFrom(_kiid())
    t.layer = bt.BoardLayer.BL_F_Fab
    t.text.text = "${REFERENCE}"
    t.text.attributes.size.x_nm = t.text.attributes.size.y_nm = mm(0.6)
    t.text.attributes.stroke_width.value_nm = mm(0.09)
    t.text.attributes.visible = True
    t.text.attributes.horizontal_alignment = en.HorizontalAlignment.HA_CENTER
    t.text.attributes.vertical_alignment = en.VerticalAlignment.VA_CENTER
    return t


def custom_field(name: str, value: str, template: bt.Field) -> bt.Field:
    f = bt.Field()
    f.CopyFrom(template)
    f.ClearField("id")
    f.name = name
    f.text.id.CopyFrom(_kiid())
    f.text.text.text = value
    f.visible = False
    f.text.text.attributes.visible = False
    return f


def silk_items() -> list[bt.BoardGraphicShape]:
    """Pin-1 marker left of pad 1 (pad 1 copper spans x -1.75..-0.45, y -1.75..1.75) plus two
    body lines between the pads; all inside the 4x4 courtyard and outside the copper."""
    s = bt.BoardLayer.BL_F_SilkS
    return [
        segment(s, (-1.95, -1.75), (-1.95, 1.0), 0.12),  # pin-1 side bar, 0.14 mm from pad copper
        segment(s, (-0.25, -1.6), (0.25, -1.6), 0.12),
        segment(s, (-0.25, 1.6), (0.25, 1.6), 0.12),
    ]


def geometry(items: list) -> dict:
    """Plain-number read-back (mm, KiCad y-down) of a footprint document's items."""
    out: dict = {"pads": [], "shapes": [], "fields": {}, "texts": []}
    for it in items:
        k = kind(it)
        if k == "Pad":
            cl = it.pad_stack.copper_layers[0]
            out["pads"].append({
                "number": it.number,
                "type": bt.PadType.Name(it.type),
                "shape": bt.PadStackShape.Name(cl.shape),
                "pos": (it.position.x_nm / NM, it.position.y_nm / NM),
                "size": (cl.size.x_nm / NM, cl.size.y_nm / NM),
                "drill": (it.pad_stack.drill.diameter.x_nm / NM, it.pad_stack.drill.diameter.y_nm / NM),
                "layers": sorted(bt.BoardLayer.Name(x) for x in it.pad_stack.layers),
            })
        elif k == "BoardGraphicShape":
            g = it.shape
            geo = g.WhichOneof("geometry")
            d = {"layer": bt.BoardLayer.Name(it.layer), "geometry": geo,
                 "stroke": g.attributes.stroke.width.value_nm / NM}
            if geo == "rectangle":
                tl, br = g.rectangle.top_left, g.rectangle.bottom_right
                d["size"] = (round((br.x_nm - tl.x_nm) / NM, 4), round((br.y_nm - tl.y_nm) / NM, 4))
                d["center"] = ((br.x_nm + tl.x_nm) / 2 / NM, (br.y_nm + tl.y_nm) / 2 / NM)
            elif geo == "segment":
                d["start"] = (g.segment.start.x_nm / NM, g.segment.start.y_nm / NM)
                d["end"] = (g.segment.end.x_nm / NM, g.segment.end.y_nm / NM)
            out["shapes"].append(d)
        elif k == "Field":
            out["fields"][it.name] = {"text": it.text.text.text, "layer": bt.BoardLayer.Name(it.text.layer),
                                      "visible": it.visible, "id": it.id.id}
        elif k == "BoardText":
            out["texts"].append({"text": it.text.text, "layer": bt.BoardLayer.Name(it.layer)})
    out["pads"].sort(key=lambda p: p["number"])
    return out
