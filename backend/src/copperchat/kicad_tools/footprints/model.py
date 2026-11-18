"""The shared footprint geometry model (FP-C3, spec §7.1) and its canonical hash (§9.4).

Built from KiCad API protobuf: either a library `kiapi.board.types.Footprint`
(`GetItemsFromLibrary`) or a footprint document's `FootprintInstance` (`GetItemsById`).
The library read only carries what `FOOTPRINT::SerializeDefinition` packs: pads, shapes,
user texts, custom fields, 3D models, net ties, private layers and jumpers. The mandatory
fields (Reference / Value with their text geometry), the attributes (mounting, exclude
flags, DNP) and the footprint-level overrides come only with a `FootprintInstance`.

Units are mm, footprint coordinates (origin = anchor, x right, **y down**); angles in
degrees.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from kipy.proto.board import board_types_pb2 as bt
from kipy.proto.common.types import base_types_pb2 as base
from kipy.util.proto import unpack_any

Scope = Literal["local", "project", "global"]
Mounting = Literal["smd", "tht", "unspecified"]
ReviewState = Literal["approved", "needs_user_review", "unreviewed"]
Point = tuple[float, float]
Vec3 = tuple[float, float, float]

REVIEW_FIELD = "needs_user_review"
SOURCE_FIELD = "Footprint source"
APPROVAL_FIELD = "User review"
# casefolded; never accepted as fp_create extra fields (only fp_approve writes the review ones)
RESERVED_FIELDS = frozenset(
    {"reference", "value", "footprint source", "needs_user_review", "user review"}
)

NM_PER_MM = 1_000_000
HASH_VERSION = 1

_PAD_TYPES = {"PT_SMD": "smd", "PT_PTH": "tht", "PT_NPTH": "npth", "PT_EDGE_CONNECTOR": "connector"}
_PAD_SHAPES = {
    "PSS_CIRCLE": "circle",
    "PSS_RECTANGLE": "rect",
    "PSS_OVAL": "oval",
    "PSS_TRAPEZOID": "trapezoid",
    "PSS_ROUNDRECT": "roundrect",
    "PSS_CHAMFEREDRECT": "chamfered_rect",
    "PSS_CUSTOM": "custom",
}
_MOUNTING: dict[str, Mounting] = {"FMS_SMD": "smd", "FMS_THROUGH_HOLE": "tht"}
_ALL_COPPER = frozenset({"F.Cu", "B.Cu", *(f"In{i}.Cu" for i in range(1, 31))})
_UNFILLED = frozenset({"GFT_UNKNOWN", "GFT_UNFILLED"})


# -- dataclasses -------------------------------------------------------------------------


def _r(v: float) -> float:
    """Output rounding: 1 nm, and never -0.0."""
    return round(float(v), 6) + 0.0


def _pt(p: Sequence[float]) -> list[float]:
    return [_r(c) for c in p]


@dataclass(frozen=True)
class Drill:
    size: Point
    shape: str  # "circle" | "oval"

    def to_dict(self) -> dict[str, Any]:
        return {"size": _pt(self.size), "shape": self.shape}


@dataclass(frozen=True)
class Pad:
    number: str
    type: str  # smd | tht | npth | connector
    shape: str  # rect | roundrect | circle | oval | trapezoid | chamfered_rect | custom
    at: Point
    rotation: float
    size: Point
    roundrect_ratio: float | None
    drill: Drill | None
    layers: tuple[str, ...]
    # clearance, margins, zone/thermal, custom primitives, ...: hashed, not in to_dict
    extra: tuple[tuple[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "type": self.type,
            "shape": self.shape,
            "at": _pt(self.at),
            "rotation": _r(self.rotation),
            "size": _pt(self.size),
            "roundrect_ratio": self.roundrect_ratio,
            "drill": self.drill.to_dict() if self.drill else None,
            "layers": list(self.layers),
        }


@dataclass(frozen=True)
class Shape:
    kind: str  # segment | rect | arc | circle | polygon | polygon_hole | bezier | ellipse...
    layer: str
    points: tuple[Point, ...]  # segment: start, end; rect: corners; arc: start, mid, end
    center: Point | None
    size: Point | None
    width: float
    closed: bool
    filled: bool = False
    radius: float | None = None  # rect corner radius
    rotation: float | None = None  # ellipse / ellipse_arc, degrees
    arc_angles: tuple[float, float] | None = None  # ellipse_arc start, end, degrees

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "layer": self.layer}
        if self.points:
            d["points"] = [_pt(p) for p in self.points]
        if self.center is not None:
            d["center"] = _pt(self.center)
        if self.size is not None:
            d["size"] = _pt(self.size)
        if self.radius is not None:
            d["radius"] = _r(self.radius)
        if self.rotation is not None:
            d["rotation"] = _r(self.rotation)
        if self.arc_angles is not None:
            d["arc_angles"] = _pt(self.arc_angles)
        d.update(width=_r(self.width), closed=self.closed, filled=self.filled)
        return d


@dataclass(frozen=True)
class Text:
    kind: str  # reference | value | user
    layer: str
    at: Point
    size: Point
    rotation: float
    value: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "layer": self.layer,
            "at": _pt(self.at),
            "size": _pt(self.size),
            "rotation": _r(self.rotation),
            "value": self.value,
        }


@dataclass(frozen=True)
class Model3D:
    path: str
    offset: Vec3  # mm
    rotation: Vec3  # degrees
    scale: Vec3

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "offset": _pt(self.offset),
            "rotation": _pt(self.rotation),
            "scale": _pt(self.scale),
        }


@dataclass(frozen=True)
class FootprintGeometry:
    lib_id: str
    scope: Scope
    description: str
    keywords: str
    mounting: Mounting
    exclude_from_bom: bool
    exclude_from_pos: bool
    dnp: bool
    fields: dict[str, str]
    pads: tuple[Pad, ...]
    shapes: tuple[Shape, ...]
    texts: tuple[Text, ...]
    models: tuple[Model3D, ...]
    overrides: tuple[tuple[str, Any], ...]  # footprint-level (patch 0002) properties, hashed
    # items the model does not describe (zones, points, ...): (type, sha256 of the
    # deterministic protobuf without ids), hashed so a keepout change changes the hash
    other: tuple[tuple[str, str], ...] = ()

    def bbox(self) -> tuple[float, float, float, float]:
        """Extent of pads and shapes (texts excluded); (0, 0, 0, 0) when empty."""
        points = [p for pad in self.pads for p in _pad_extent(pad)]
        points += [p for s in self.shapes for p in _shape_extent(s)]
        return _bbox(points)

    def layer_summary(self) -> dict[str, dict[str, Any]]:
        """Per graphic layer: bbox, whether every outline is closed, and the shape count."""
        by_layer: dict[str, list[Shape]] = {}
        for s in self.shapes:
            by_layer.setdefault(s.layer, []).append(s)
        return {
            layer: {
                "bbox": list(_bbox([p for s in shapes for p in _shape_extent(s)])),
                "closed": _all_closed(shapes),
                "items": len(shapes),
            }
            for layer, shapes in sorted(by_layer.items())
        }

    def to_dict(self, detail: Literal["summary", "full"] = "summary") -> dict[str, Any]:
        d: dict[str, Any] = {
            "lib_id": self.lib_id,
            "scope": self.scope,
            "description": self.description,
            "keywords": self.keywords,
            "mounting": self.mounting,
            "exclude_from_bom": self.exclude_from_bom,
            "exclude_from_pos": self.exclude_from_pos,
            "dnp": self.dnp,
            "fields": dict(self.fields),
            "pads": [p.to_dict() for p in self.pads],
            "graphics": self.layer_summary(),
        }
        if detail == "full":
            d["shapes"] = [s.to_dict() for s in self.shapes]
        d.update(
            models=[m.to_dict() for m in self.models],
            texts=[t.to_dict() for t in self.texts],
            bbox=list(self.bbox()),
            geometry_hash=geometry_hash(self),
        )
        return d


# -- protobuf -> geometry ----------------------------------------------------------------


def _mm(nm: int) -> float:
    return nm / NM_PER_MM


def _vec(v: Any) -> Point:
    return (_mm(v.x_nm), _mm(v.y_nm))


def _vec3(v: Any) -> Vec3:
    # Footprint3DModel packs FP_3DMODEL's doubles as they are (mm / degrees / factor)
    return (float(v.x_nm), float(v.y_nm), float(v.z_nm))


def _layer(value: int) -> str:
    """`BL_F_Cu` -> `F.Cu`, `BL_In1_Cu` -> `In1.Cu`, `BL_User_1` -> `User.1`."""
    return str(bt.BoardLayer.Name(value)).removeprefix("BL_").replace("_", ".")


def _enum(enum: Any, value: int, prefix: str) -> str:
    return str(enum.Name(value)).removeprefix(prefix).lower()


def _pad_layers(values: Iterable[int]) -> tuple[str, ...]:
    layers = [_layer(v) for v in values]
    if not set(layers) >= _ALL_COPPER:
        return tuple(layers)
    out: list[str] = []
    for name in layers:
        if name not in _ALL_COPPER:
            out.append(name)
        elif "*.Cu" not in out:
            out.append("*.Cu")
    return tuple(out)


def _shapes_from(graphic: Any) -> list[Shape]:
    """`BoardGraphicShape` -> shapes (a polygon set gives one shape per outline/hole)."""
    s = graphic.shape
    layer = _layer(graphic.layer)
    width = _mm(s.attributes.stroke.width.value_nm)
    filled = base.GraphicFillType.Name(s.attributes.fill.fill_type) not in _UNFILLED
    kind = s.WhichOneof("geometry")
    pts: tuple[Point, ...]
    if kind == "segment":
        pts = (_vec(s.segment.start), _vec(s.segment.end))
        return [Shape("segment", layer, pts, None, None, width, False, filled)]
    if kind == "rectangle":
        (x0, y0), (x1, y1) = _vec(s.rectangle.top_left), _vec(s.rectangle.bottom_right)
        radius = _mm(s.rectangle.corner_radius.value_nm) or None
        return [
            Shape(
                "rect",
                layer,
                ((x0, y0), (x1, y1)),
                ((x0 + x1) / 2, (y0 + y1) / 2),
                (abs(x1 - x0), abs(y1 - y0)),
                width,
                True,
                filled,
                radius,
            )
        ]
    if kind == "arc":
        pts = (_vec(s.arc.start), _vec(s.arc.mid), _vec(s.arc.end))
        return [Shape("arc", layer, pts, None, None, width, False, filled)]
    if kind == "circle":
        c, rp = _vec(s.circle.center), _vec(s.circle.radius_point)
        d = 2 * math.hypot(rp[0] - c[0], rp[1] - c[1])
        return [Shape("circle", layer, (), c, (d, d), width, True, filled)]
    if kind == "bezier":
        b = s.bezier
        pts = (_vec(b.start), _vec(b.control1), _vec(b.control2), _vec(b.end))
        return [Shape("bezier", layer, pts, None, None, width, False, filled)]
    if kind == "polygon":
        out: list[Shape] = []
        for poly in s.polygon.polygons:
            for line, k in [(poly.outline, "polygon"), *((h, "polygon_hole") for h in poly.holes)]:
                out.append(Shape(k, layer, _polyline(line), None, None, width, line.closed, filled))
        return out
    if kind in ("ellipse", "ellipse_arc"):
        e = getattr(s, kind)
        major, minor = _mm(e.major_radius.value_nm), _mm(e.minor_radius.value_nm)
        arc_angles = None
        if kind == "ellipse_arc":
            arc_angles = (e.start_angle.value_degrees + 0.0, e.end_angle.value_degrees + 0.0)
        return [
            Shape(
                kind,
                layer,
                (),
                _vec(e.center),
                (2 * major, 2 * minor),
                width,
                kind == "ellipse",
                filled,
                rotation=e.rotation.value_degrees + 0.0,
                arc_angles=arc_angles,
            )
        ]
    return []


def _polyline(line: Any) -> tuple[Point, ...]:
    pts: list[Point] = []
    for node in line.nodes:
        if node.HasField("arc"):
            pts += [_vec(node.arc.start), _vec(node.arc.mid), _vec(node.arc.end)]
        else:
            pts.append(_vec(node.point))
    return tuple(pts)


def _outer_settings(stack: Any, extra: dict[str, Any]) -> None:
    for side, prefix in (("front_outer_layers", ""), ("back_outer_layers", "back_")):
        if not stack.HasField(side):
            continue
        outer = getattr(stack, side)
        mask, paste = outer.solder_mask_settings, outer.solder_paste_settings
        if mask.HasField("solder_mask_margin"):
            extra[f"{prefix}mask_margin"] = _mm(mask.solder_mask_margin.value_nm)
        if paste.HasField("solder_paste_margin"):
            extra[f"{prefix}paste_margin"] = _mm(paste.solder_paste_margin.value_nm)
        if paste.HasField("solder_paste_margin_ratio"):
            extra[f"{prefix}paste_margin_ratio"] = paste.solder_paste_margin_ratio.value
        for key, enum, value, pre in (
            ("mask_mode", bt.SolderMaskMode, outer.solder_mask_mode, "SMM_"),
            ("paste_mode", bt.SolderPasteMode, outer.solder_paste_mode, "SPM_"),
        ):
            name = _enum(enum, value, pre)
            if name not in ("unknown", "from_design_rules"):
                extra[f"{prefix}{key}"] = name
    # KiCad packs a pad's single margin into both sides; keep the back only if it differs
    for key in ("mask_margin", "paste_margin", "paste_margin_ratio", "mask_mode", "paste_mode"):
        if extra.get(f"back_{key}") == extra.get(key):
            extra.pop(f"back_{key}", None)


def _copper_layer(cl: Any) -> dict[str, Any]:
    shape = _PAD_SHAPES.get(bt.PadStackShape.Name(cl.shape), "unknown")
    d: dict[str, Any] = {"layer": _layer(cl.layer), "shape": shape, "size": _vec(cl.size)}
    if shape == "roundrect":
        d["roundrect_ratio"] = cl.corner_rounding_ratio
    if shape == "chamfered_rect":
        d["chamfer_ratio"] = cl.chamfer_ratio
        corners = cl.chamfered_corners
        d["chamfered_corners"] = tuple(
            c
            for c in ("top_left", "top_right", "bottom_left", "bottom_right")
            if getattr(corners, c)
        )
    if shape == "trapezoid":
        d["trapezoid_delta"] = _vec(cl.trapezoid_delta)
    if shape == "custom":
        d["anchor"] = _PAD_SHAPES.get(bt.PadStackShape.Name(cl.custom_anchor_shape), "unknown")
        d["primitives"] = tuple(s.to_dict() for g in cl.custom_shapes for s in _shapes_from(g))
    if cl.offset.x_nm or cl.offset.y_nm:
        d["offset"] = _vec(cl.offset)
    return d


def _pad_from(p: Any) -> Pad:
    stack = p.pad_stack
    copper = [_copper_layer(cl) for cl in stack.copper_layers]
    first: dict[str, Any] = copper[0] if copper else {"shape": "unknown", "size": (0.0, 0.0)}
    pad_type = _PAD_TYPES.get(bt.PadType.Name(p.type), "unknown")
    drill = None
    if stack.drill.diameter.x_nm > 0:
        oval = bt.DrillShape.Name(stack.drill.shape) == "DS_OBLONG"
        drill = Drill(_vec(stack.drill.diameter), "oval" if oval else "circle")

    extra: dict[str, Any] = {
        k: v for k, v in first.items() if k not in ("layer", "shape", "size", "roundrect_ratio")
    }
    if len(copper) > 1:
        extra["copper_layers"] = tuple(copper[1:])
    stack_type = _enum(bt.PadStackType, stack.type, "PST_")
    if stack_type not in ("normal", "unknown"):
        extra["padstack"] = stack_type
    if p.HasField("copper_clearance_override"):
        extra["clearance"] = _mm(p.copper_clearance_override.value_nm)
    _outer_settings(stack, extra)
    zone = stack.zone_settings
    extra["zone_connection"] = _enum(bt.ZoneConnectionStyle, zone.zone_connection, "ZCS_")
    spokes = zone.thermal_spokes
    if spokes.HasField("width"):
        extra["thermal_width"] = _mm(spokes.width.value_nm)
    if spokes.HasField("gap"):
        extra["thermal_gap"] = _mm(spokes.gap.value_nm)
    if spokes.HasField("angle"):
        extra["thermal_angle"] = spokes.angle.value_degrees
    removal = _enum(bt.UnconnectedLayerRemoval, stack.unconnected_layer_removal, "ULR_")
    if removal not in ("keep", "unknown"):
        extra["unconnected_layer_removal"] = removal
    fab = _enum(bt.PadFabricationProperty, p.fab_property, "PFP_")
    if fab not in ("none", "unknown"):
        extra["fab_property"] = fab
    return Pad(
        number=p.number,
        type=pad_type,
        shape=str(first["shape"]),
        at=_vec(p.position),
        rotation=stack.angle.value_degrees + 0.0,
        size=first["size"],
        roundrect_ratio=first.get("roundrect_ratio"),
        drill=drill,
        layers=_pad_layers(stack.layers),
        extra=tuple(sorted(extra.items())),
    )


def _text_from(board_text: Any, kind: str) -> Text:
    t = board_text.text
    return Text(
        kind=kind,
        layer=_layer(board_text.layer),
        at=_vec(t.position),
        size=_vec(t.attributes.size),
        rotation=t.attributes.angle.value_degrees + 0.0,
        value=t.text,
    )


def _textbox_from(box: Any) -> Text:
    t = box.textbox
    return Text(
        kind="user",
        layer=_layer(box.layer),
        at=_vec(t.top_left),
        size=_vec(t.attributes.size),
        rotation=t.attributes.angle.value_degrees + 0.0,
        value=t.text,
    )


def _overrides(definition: Any, fp: Any) -> tuple[tuple[str, Any], ...]:
    out: dict[str, Any] = {}
    ov = definition.overrides
    if ov.HasField("copper_clearance"):
        out["clearance"] = _mm(ov.copper_clearance.value_nm)
    if ov.solder_mask.HasField("solder_mask_margin"):
        out["mask_margin"] = _mm(ov.solder_mask.solder_mask_margin.value_nm)
    if ov.solder_paste.HasField("solder_paste_margin"):
        out["paste_margin"] = _mm(ov.solder_paste.solder_paste_margin.value_nm)
    if ov.solder_paste.HasField("solder_paste_margin_ratio"):
        out["paste_margin_ratio"] = ov.solder_paste.solder_paste_margin_ratio.value
    zone = _enum(bt.ZoneConnectionStyle, ov.zone_connection, "ZCS_")
    if zone not in ("unknown", "inherited"):
        out["zone_connection"] = zone
    attrs = definition.attributes
    for flag in ("exempt_from_courtyard_requirement", "allow_soldermask_bridges"):
        if getattr(attrs, flag):
            out[flag] = True
    if fp.net_ties:
        out["net_ties"] = tuple(sorted(tuple(sorted(t.pad_number)) for t in fp.net_ties))
    if fp.private_layers:
        out["private_layers"] = tuple(sorted(_layer(v) for v in fp.private_layers))
    if fp.jumpers.duplicate_names_are_jumpered:
        out["duplicate_pads_are_jumpers"] = True
    if fp.jumpers.groups:
        out["jumper_groups"] = tuple(sorted(tuple(sorted(g.pad_names)) for g in fp.jumpers.groups))
    if fp.HasField("height") and fp.height.value_nm > 0:
        out["height"] = _mm(fp.height.value_nm)
    return tuple(sorted(out.items()))


def _opaque_digest(item: Any) -> str:
    """Stable digest of an item the model does not describe: its protobuf without the
    per-load KIIDs (`id`, `parent`), serialised deterministically."""
    copy = type(item)()
    copy.CopyFrom(item)
    for name in ("id", "parent"):
        if name in copy.DESCRIPTOR.fields_by_name:
            copy.ClearField(name)
    return hashlib.sha256(copy.SerializeToString(deterministic=True)).hexdigest()


def geometry_from_proto(
    definition: Any, items: Sequence[Any], lib_id: str, scope: str
) -> FootprintGeometry:
    """`FootprintGeometry` from a `Footprint` (library read) or a `FootprintInstance`
    (footprint document) plus its items (packed `Any` or unpacked messages)."""
    instance = definition.DESCRIPTOR.name == "FootprintInstance"
    fp = definition.definition if instance else definition
    attrs = definition.attributes
    fields: dict[str, str] = {}
    texts: list[Text] = []
    if instance:
        for name, kind in (("reference_field", "reference"), ("value_field", "value")):
            if definition.HasField(name):
                field = getattr(definition, name)
                fields[field.name or kind.title()] = field.text.text.text
                texts.append(_text_from(field.text, kind))
        if definition.HasField("datasheet_field"):
            fields["Datasheet"] = definition.datasheet_field.text.text.text

    pads: list[Pad] = []
    shapes: list[Shape] = []
    models: list[Model3D] = []
    other: list[tuple[str, str]] = []
    for raw in items:
        item = unpack_any(raw) if hasattr(raw, "type_url") else raw
        name = item.DESCRIPTOR.name
        if name == "Pad":
            pads.append(_pad_from(item))
        elif name == "BoardGraphicShape":
            shapes += _shapes_from(item)
        elif name == "BoardText":
            texts.append(_text_from(item, "user"))
        elif name == "BoardTextBox":
            texts.append(_textbox_from(item))
        elif name == "Field":
            fields[item.name] = item.text.text.text
        elif name == "Footprint3DModel":
            models.append(
                Model3D(item.filename, _vec3(item.offset), _vec3(item.rotation), _vec3(item.scale))
            )
        else:
            other.append((name, _opaque_digest(item)))
    mounting = _MOUNTING.get(bt.FootprintMountingStyle.Name(attrs.mounting_style), "unspecified")
    return FootprintGeometry(
        lib_id=lib_id,
        scope=scope,  # type: ignore[arg-type]
        description=fp.attributes.description,
        keywords=fp.attributes.keywords,
        mounting=mounting,
        exclude_from_bom=attrs.exclude_from_bill_of_materials,
        exclude_from_pos=attrs.exclude_from_position_files,
        dnp=attrs.do_not_populate,
        fields=fields,
        pads=tuple(pads),
        shapes=tuple(shapes),
        texts=tuple(texts),
        models=tuple(models),
        overrides=_overrides(definition, fp),
        other=tuple(sorted(other)),
    )


# -- extents -----------------------------------------------------------------------------


def _bbox(points: Sequence[Point]) -> tuple[float, float, float, float]:
    if not points:
        return (0.0, 0.0, 0.0, 0.0)
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (_r(min(xs)), _r(min(ys)), _r(max(xs)), _r(max(ys)))


def _rotate(p: Point, degrees: float) -> Point:
    """KiCad's RotatePoint in y-down coordinates (positive = counter-clockwise on screen)."""
    if not degrees:
        return p
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return (p[0] * c + p[1] * s, -p[0] * s + p[1] * c)


def _arc_extent(start: Point, mid: Point, end: Point) -> list[Point]:
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:  # collinear
        return [start, mid, end]
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    tau = 2 * math.pi

    def angle(p: Point) -> float:
        return math.atan2(p[1] - uy, p[0] - ux) % tau

    a0, am, a1 = angle(start), angle(mid), angle(end)
    ccw = (am - a0) % tau <= (a1 - a0) % tau
    sweep = (a1 - a0) % tau if ccw else (a0 - a1) % tau
    out = [start, end]
    for k in range(4):
        t = k * math.pi / 2
        if ((t - a0) % tau if ccw else (a0 - t) % tau) <= sweep:
            out.append((ux + r * math.cos(t), uy + r * math.sin(t)))
    return out


def _shape_extent(s: Shape) -> list[Point]:
    if s.kind == "arc" and len(s.points) == 3:
        return _arc_extent(*s.points)
    if s.points:
        return list(s.points)
    if s.center is not None and s.size is not None:
        (cx, cy), (w, h) = s.center, s.size
        return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy + h / 2)]
    return []


def _primitive_extent(d: dict[str, Any]) -> list[Point]:
    shape = Shape(
        d["kind"],
        d["layer"],
        tuple(tuple(p) for p in d.get("points", ())),
        tuple(d["center"]) if "center" in d else None,
        tuple(d["size"]) if "size" in d else None,
        d["width"],
        d["closed"],
    )
    return _shape_extent(shape)


def _pad_extent(pad: Pad) -> list[Point]:
    extra = dict(pad.extra)
    ox, oy = extra.get("offset", (0.0, 0.0))
    w, h = pad.size
    if pad.shape == "circle":
        cx, cy = _rotate((ox, oy), pad.rotation)
        x, y = pad.at[0] + cx, pad.at[1] + cy
        return [(x - w / 2, y - w / 2), (x + w / 2, y + w / 2)]
    local = [(ox + sx * w / 2, oy + sy * h / 2) for sx in (-1, 1) for sy in (-1, 1)]
    for prim in extra.get("primitives", ()):
        local += _primitive_extent(prim)
    return [
        (pad.at[0] + rx, pad.at[1] + ry) for rx, ry in (_rotate(p, pad.rotation) for p in local)
    ]


def _nm_point(p: Sequence[float]) -> tuple[int, int]:
    return (round(p[0] * NM_PER_MM), round(p[1] * NM_PER_MM))


def _all_closed(shapes: Iterable[Shape]) -> bool:
    """Every open segment/arc/curve chains into a closed loop (each end meets an even
    number of other ends, at 1 nm)."""
    ends: Counter[tuple[int, int]] = Counter()
    for s in shapes:
        if s.closed or len(s.points) < 2:
            continue
        ends[_nm_point(s.points[0])] += 1
        ends[_nm_point(s.points[-1])] += 1
    return all(n % 2 == 0 for n in ends.values())


# -- canonical content, hash and equality -------------------------------------------------


def _pad_content(p: Pad) -> dict[str, Any]:
    return asdict(p) | {"extra": dict(p.extra)}


def _text_content(t: Text) -> dict[str, Any]:
    # Reference / Value: their geometry is hashed, their value is a field value (excluded)
    d = asdict(t)
    if t.kind != "user":
        del d["value"]
    return d


def _canonical(v: Any) -> Any:
    """Numbers -> integer millionths (1 nm for mm), containers recursively."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, (int, float)):
        return round(float(v) * NM_PER_MM) + 0
    if isinstance(v, dict):
        return {str(k): _canonical(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_canonical(x) for x in v]
    raise TypeError(f"cannot hash {type(v).__name__}")


def _dumps(v: Any) -> str:
    return json.dumps(_canonical(v), sort_keys=True, separators=(",", ":"))


def _content(g: FootprintGeometry) -> dict[str, Any]:
    """Everything manufacturing-relevant (§9.4), lists in canonical order. Left out:
    lib_id, scope, description, keywords and field values."""
    return {
        "v": HASH_VERSION,
        "mounting": g.mounting,
        "exclude_from_bom": g.exclude_from_bom,
        "exclude_from_pos": g.exclude_from_pos,
        "dnp": g.dnp,
        "overrides": dict(g.overrides),
        "pads": sorted(
            (_pad_content(p) for p in g.pads),
            key=lambda d: (d["number"], _canonical(d["at"]), _dumps(d)),
        ),
        "shapes": sorted(
            (asdict(s) for s in g.shapes),
            key=lambda d: (d["layer"], d["kind"], _dumps(d["points"]), _dumps(d)),
        ),
        "texts": sorted((_text_content(t) for t in g.texts), key=_dumps),
        "models": sorted((asdict(m) for m in g.models), key=_dumps),
        "other": sorted([list(o) for o in g.other]),
    }


def geometry_hash(g: FootprintGeometry) -> str:
    """sha256 hex over the canonical JSON of the manufacturing-relevant content (§9.4):
    coordinates rounded to 1 nm, lists sorted."""
    return hashlib.sha256(_dumps(_content(g)).encode()).hexdigest()


# Numbers that are not lengths (degrees, ratios, scale factors) compare with this
# tolerance instead of `tol_mm`.
_NON_LENGTH = frozenset(
    {
        "rotation",
        "roundrect_ratio",
        "chamfer_ratio",
        "paste_margin_ratio",
        "back_paste_margin_ratio",
        "thermal_angle",
        "scale",
        "arc_angles",
    }
)
_NON_LENGTH_TOL = 1e-6
_FAR = 1e9  # distance of structurally different values


def _close(a: Any, b: Any, tol: float, key: str | None = None) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        limit = _NON_LENGTH_TOL if key in _NON_LENGTH else tol
        return abs(float(a) - float(b)) <= limit + 1e-12
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k], tol, k) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_close(x, y, tol, key) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def _distance(a: Any, b: Any) -> float:
    """How far apart two items are (sum of numeric differences), for pairing."""
    if isinstance(a, bool) or isinstance(b, bool):
        return 0.0 if a is b else _FAR
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b))
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return _FAR
        return sum(_distance(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return _FAR
        return sum(_distance(x, y) for x, y in zip(a, b, strict=True))
    return 0.0 if a == b else _FAR


def _item_diffs(
    xs: list[dict[str, Any]],
    ys: list[dict[str, Any]],
    tol: float,
    group: Callable[[dict[str, Any]], tuple[str, ...]],
    name: Callable[[dict[str, Any]], str],
) -> list[str]:
    """Pair the items of `xs` and `ys` within each group (pad number, shape layer and
    kind, ...): first every item with a match within tolerance, then the rest by
    nearest distance. A 1 nm shift that reorders the canonical sort gives no diff."""
    groups: dict[tuple[str, ...], tuple[list[Any], list[Any]]] = {}
    for side, items in ((0, xs), (1, ys)):
        for item in items:
            groups.setdefault(group(item), ([], []))[side].append(item)
    out: list[str] = []
    for _key, (gx, gy) in sorted(groups.items()):
        label = name(gx[0] if gx else gy[0])
        if len(gx) != len(gy):
            out.append(f"{label} count: {len(gx)} != {len(gy)}")
            continue
        left, right = list(gx), list(gy)
        for x in list(left):
            close = [y for y in right if _close(x, y, tol)]
            if close:
                right.remove(min(close, key=lambda y: _distance(x, y)))
                left.remove(x)
        for x in left:
            y = min(right, key=lambda c: _distance(x, c))
            right.remove(y)
            keys = [
                k for k in sorted(x.keys() | y.keys()) if not _close(x.get(k), y.get(k), tol, k)
            ]
            parts = "; ".join(f"{k} {x.get(k)!r} != {y.get(k)!r}" for k in keys)
            out.append(f"{label}: {parts}")
    return out


def geometry_equal(a: FootprintGeometry, b: FootprintGeometry, tol_mm: float = 1e-3) -> list[str]:
    """Differences between two geometries (empty = equal): lengths within `tol_mm`,
    angles / ratios / scale within 1e-6, plus description, keywords and every field
    value exactly (read-back, §9.2). Items are paired tolerance-aware, not by index."""
    ca, cb = _content(a), _content(b)
    diffs: list[str] = []
    for key in ("mounting", "exclude_from_bom", "exclude_from_pos", "dnp", "overrides", "other"):
        if not _close(ca[key], cb[key], tol_mm):
            diffs.append(f"{key}: {ca[key]!r} != {cb[key]!r}")
    diffs += _item_diffs(
        ca["pads"],
        cb["pads"],
        tol_mm,
        lambda d: (d["number"],),
        lambda d: f"pad {d['number'] or '(unnumbered)'}",
    )
    diffs += _item_diffs(
        ca["shapes"],
        cb["shapes"],
        tol_mm,
        lambda d: (d["layer"], d["kind"]),
        lambda d: f"shape {d['kind']} on {d['layer']}",
    )
    diffs += _item_diffs(
        ca["texts"],
        cb["texts"],
        tol_mm,
        lambda d: (d["kind"], d["layer"]),
        lambda d: f"text {d['kind']} on {d['layer']}",
    )
    diffs += _item_diffs(
        ca["models"],
        cb["models"],
        tol_mm,
        lambda d: (d["path"],),
        lambda d: f"model {d['path']}",
    )
    for key in ("description", "keywords"):
        if getattr(a, key) != getattr(b, key):
            diffs.append(f"{key}: {getattr(a, key)!r} != {getattr(b, key)!r}")
    for name in sorted(a.fields.keys() | b.fields.keys()):
        if a.fields.get(name) != b.fields.get(name):
            diffs.append(f"field {name!r}: {a.fields.get(name)!r} != {b.fields.get(name)!r}")
    return diffs


# -- review state ------------------------------------------------------------------------


def review_state(g: FootprintGeometry, *, pending: bool) -> ReviewState:
    """Review state of a **local** footprint; callers pass only local geometries (for
    project/global ones the result means nothing). `unreviewed` when `Footprint source`
    or `needs_user_review` is missing or a `pending` journal row exists;
    `needs_user_review` for `yes`; `approved` for `no`; any other value: unreviewed."""
    source = g.fields.get(SOURCE_FIELD, "").strip()
    flag = g.fields.get(REVIEW_FIELD)
    if pending or not source or flag is None:
        return "unreviewed"
    value = flag.strip().casefold()
    if value == "yes":
        return "needs_user_review"
    if value == "no":
        return "approved"
    return "unreviewed"
