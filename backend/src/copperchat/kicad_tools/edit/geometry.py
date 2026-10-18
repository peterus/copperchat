"""Schematic geometry from API data (E-C7): grid, transforms, pin ends, boxes.

Port of spike 2A `geom.py` (verified against KiCad's live netlist for all 12
orientation/mirror requests). KiCad builds a symbol's TRANSFORM incrementally
(`SCH_SYMBOL::SetOrientation`): each rotation/mirror step acts on the already
transformed coordinates. Library coordinates as the API returns them are y-down, so
the default transform is the identity. Everything here is in mm, y down.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from copperchat.kicad_tools import access

GRID_MM = 1.27
EPS_MM = 0.01  # two points closer than this are the same connection point
NM = 1_000_000

Matrix = tuple[int, int, int, int]  # (x1, y1, x2, y2): x' = x1*x + y1*y ; y' = x2*x + y2*y
Direction = Literal["left", "right", "up", "down"]
Point = tuple[float, float]

_IDENT: Matrix = (1, 0, 0, 1)
_ROT_CCW: Matrix = (0, 1, -1, 0)
_ROT_CW: Matrix = (0, -1, 1, 0)
_MIR_Y: Matrix = (-1, 0, 0, 1)  # KiCad SYM_MIRROR_Y: mirror about the Y axis (x -> -x)
_MIR_X: Matrix = (1, 0, 0, -1)  # KiCad SYM_MIRROR_X: mirror about the X axis (y -> -y)
_ROT_STEPS: dict[int, tuple[Matrix, ...]] = {
    0: (),
    90: (_ROT_CCW,),
    180: (_ROT_CCW, _ROT_CCW),
    270: (_ROT_CW,),
}
# kiapi SchematicSymbolOrientation: SSO_UNKNOWN = 0, SSO_0 = 1 ... SSO_270 = 4.
SSO_DEG = {0: 0, 1: 0, 2: 90, 3: 180, 4: 270}
DEG_SSO = {0: 1, 90: 2, 180: 3, 270: 4}
# The 8 distinct transforms in the form KiCad stores without loss (spike 2A Q1: 180 +
# mirror_x collapses to the identity in SetMirrorX, so it is sent as 0 + mirror_y).
CANONICAL: tuple[tuple[int, bool, bool], ...] = (
    (0, False, False),
    (90, False, False),
    (180, False, False),
    (270, False, False),
    (0, True, False),
    (90, True, False),
    (270, True, False),
    (0, False, True),
)
# SchematicPinOrientation (SPO_RIGHT = 1 ... SPO_DOWN = 4): the direction the pin line
# extends from its connection point towards the body; outward is the opposite.
_PIN_INWARD = {1: (1, 0), 2: (-1, 0), 3: (0, -1), 4: (0, 1)}
_DIR_NAME: dict[tuple[int, int], Direction] = {
    (1, 0): "right",
    (-1, 0): "left",
    (0, -1): "up",
    (0, 1): "down",
}


def snap(v: float) -> float:
    return round(round(v / GRID_MM) * GRID_MM, 4)


def snap_point(p: Sequence[float]) -> Point:
    return (snap(p[0]), snap(p[1]))


def on_grid(p: Sequence[float]) -> bool:
    return all(abs(c / GRID_MM - round(c / GRID_MM)) < 1e-3 for c in p)


def same_point(a: Sequence[float], b: Sequence[float], eps: float = EPS_MM) -> bool:
    return abs(a[0] - b[0]) < eps and abs(a[1] - b[1]) < eps


def mm_point(v: Any) -> Point:
    """(x, y) in mm from a kipy Vector2 (nm)."""
    return (round(v.x / NM, 4), round(v.y / NM, 4))


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def around(cls, points: Iterable[Sequence[float]]) -> Box:
        pts = list(points)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return cls(min(xs), min(ys), max(xs), max(ys))

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def grow(self, margin: float) -> Box:
        return Box(self.x0 - margin, self.y0 - margin, self.x1 + margin, self.y1 + margin)

    def contains(self, p: Sequence[float], eps: float = 1e-6) -> bool:
        return self.x0 - eps <= p[0] <= self.x1 + eps and self.y0 - eps <= p[1] <= self.y1 + eps

    def overlaps(self, other: Box, margin: float = 0.0) -> bool:
        return (
            self.x0 - margin < other.x1
            and self.x1 + margin > other.x0
            and self.y0 - margin < other.y1
            and self.y1 + margin > other.y0
        )

    def hit_by_segment(self, a: Sequence[float], b: Sequence[float], eps: float = 0.05) -> bool:
        """An axis-aligned segment enters the box interior (touching its edge is not)."""
        x0, y0, x1, y1 = self.x0 + eps, self.y0 + eps, self.x1 - eps, self.y1 - eps
        return (
            min(a[0], b[0]) < x1
            and max(a[0], b[0]) > x0
            and min(a[1], b[1]) < y1
            and max(a[1], b[1]) > y0
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "x": round(self.x0, 4),
            "y": round(self.y0, 4),
            "w": round(self.width, 4),
            "h": round(self.height, 4),
        }


def _mul(old: Matrix, step: Matrix) -> Matrix:
    """Compose as SetOrientation does: `step * old` (the step acts on the result)."""
    a1, b1, c1, d1 = step
    a2, b2, c2, d2 = old
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2)


def transform_matrix(orientation_deg: int, mirror_x: bool, mirror_y: bool) -> Matrix:
    t = _IDENT
    for step in _ROT_STEPS[orientation_deg % 360]:
        t = _mul(t, step)
    if mirror_x:
        t = _mul(t, _MIR_X)
    if mirror_y:
        t = _mul(t, _MIR_Y)
    return t


def apply(t: Matrix, x: float, y: float) -> tuple[float, float]:
    x1, y1, x2, y2 = t
    return (x1 * x + y1 * y, x2 * x + y2 * y)


def canonical_transform(rotation: int, mirror: Literal["", "x", "y"]) -> tuple[int, bool, bool]:
    """One of the 8 distinct transforms as (orientation_deg, mirror_x, mirror_y), in a
    form KiCad stores as requested (spike 2A Q1)."""
    if rotation % 90 or mirror not in ("", "x", "y"):
        raise ValueError(f"unsupported transform rotation={rotation} mirror={mirror!r}")
    wanted = transform_matrix(rotation % 360, mirror == "x", mirror == "y")
    return next(c for c in CANONICAL if transform_matrix(*c) == wanted)


def symbol_matrix(sym: Any) -> Matrix:
    tr = sym.transform
    return transform_matrix(SSO_DEG[int(tr.orientation)], bool(tr.mirror_x), bool(tr.mirror_y))


def symbol_transform(sym: Any) -> tuple[int, Literal["", "x", "y"]]:
    """(rotation_deg, mirror) of a placed symbol as the agent sees it."""
    tr = sym.transform
    mirror: Literal["", "x", "y"] = "x" if tr.mirror_x else "y" if tr.mirror_y else ""
    return SSO_DEG[int(tr.orientation)], mirror


def _in_unit(child: Any, sym: Any) -> bool:
    """Library children of the instance's unit and body style (0 / unset = common)."""
    if (child.unit or 0) not in (0, int(sym.unit)):
        return False
    proto = child.proto
    style = proto.body_style.style if proto.HasField("body_style") else 0
    return style in (0, int(sym.body_style or 1))


@dataclass(frozen=True)
class PinEnd:
    key: access.PinKey  # (sheet_path, symbol_uuid, pin_number)
    ref: str
    number: str
    name: str
    type: str
    at: Point
    direction: Direction  # outward, away from the body

    @property
    def label(self) -> str:
        return f"{self.ref}.{self.number}"


def pin_ends(sym: Any, unit_only: bool = True) -> list[PinEnd]:
    """Absolute connection points of a placed symbol's pins (deduplicated by number).

    `unit_only=False` also returns the pins of the symbol's other units, at the positions
    they would have in this instance's transform (rarely useful; mostly for probes).
    """
    t = symbol_matrix(sym)
    px, py = sym.position.x, sym.position.y
    path = access.sheet_path_key(sym.path)
    uuid = access.kiid(sym)
    ref = str(sym.reference)
    out: dict[str, PinEnd] = {}
    for child in sym.definition.items:
        if child.kind != "SchematicPin" or (unit_only and not _in_unit(child, sym)):
            continue
        pin = child.item
        number = str(pin.number)
        if number in out:
            continue
        dx, dy = apply(t, pin.position.x, pin.position.y)
        ix, iy = _PIN_INWARD.get(int(pin.orientation), (0, 0))
        ox, oy = apply(t, -ix, -iy)
        out[number] = PinEnd(
            key=(path, uuid, number),
            ref=ref,
            number=number,
            name=str(pin.name),
            type=access.pin_type_name(int(pin.electrical_type)),
            at=(round((px + dx) / NM, 4), round((py + dy) / NM, 4)),
            direction=_DIR_NAME.get((int(ox), int(oy)), "left"),
        )
    return list(out.values())


def body_box(sym: Any, margin: float = 2.0) -> Box:
    """Library graphics + pins of the placed unit, transformed, plus `margin` mm (2 mm
    contains KiCad's plot incl. pin texts, spike 2A Q9)."""
    from kipy.common_types import to_concrete_shape

    t = symbol_matrix(sym)
    pts: list[tuple[float, float]] = []
    for child in sym.definition.items:
        if not _in_unit(child, sym):
            continue
        item = child.item
        if child.kind == "SchematicGraphicShape" and item.shape is not None:
            shape = to_concrete_shape(item.shape) or item.shape
            try:
                b = shape.bounding_box()
            except Exception:  # a shape kind without a box contributes nothing
                continue
            pts += [(b.pos.x, b.pos.y), (b.pos.x + b.size.x, b.pos.y + b.size.y)]
        elif child.kind == "SchematicPin":
            x, y = item.position.x, item.position.y
            dx, dy = _PIN_INWARD.get(int(item.orientation), (0, 0))
            pts += [(x, y), (x + dx * item.length, y + dy * item.length)]
    if not pts:
        pts = [(0, 0)]
    px, py = sym.position.x, sym.position.y
    abs_pts = [apply(t, x, y) for x, y in pts]
    box = Box.around(((px + x) / NM, (py + y) / NM) for x, y in abs_pts)
    return box.grow(margin) if margin else box


TextExtents = Callable[[Any], Box | None]


def _field_box(field: Any) -> Box | None:
    """Approximate extent of a visible field: 0.6 x size per character (KiCad's stroke
    font averages ~0.6 em), one line high; honours alignment and 90/270 degree angles."""
    text = field.text
    value = str(text.value)
    if not field.visible or not value:
        return None
    attrs = text.attributes
    size = attrs.size
    w = 0.6 * (size.x / NM) * len(value)
    h = size.y / NM
    x, y = mm_point(text.position)
    halign = int(attrs.horizontal_alignment)  # HA_LEFT = 1, HA_CENTER = 2, HA_RIGHT = 3
    valign = int(attrs.vertical_alignment)  # VA_TOP = 1, VA_CENTER = 2, VA_BOTTOM = 3
    along0 = 0.0 if halign == 1 else -w if halign == 3 else -w / 2
    across0 = 0.0 if valign == 1 else -h if valign == 3 else -h / 2
    angle = round(float(attrs.angle)) % 180
    if angle == 90:  # vertical text runs bottom to top
        return Box(x + across0, y - along0 - w, x + across0 + h, y - along0)
    return Box(x + along0, y + across0, x + along0 + w, y + across0 + h)


def symbol_fields(sym: Any) -> list[Any]:
    return [
        sym.reference_field,
        sym.value_field,
        sym.footprint_field,
        sym.datasheet_field,
        sym.description_field,
        *sym.user_fields,
    ]


def field_boxes(sym: Any, extents: TextExtents | None = None) -> list[Box]:
    """One box per visible, non-empty field (never a union box, spike 2D failure mode 2).
    `extents` (e.g. KiCad's `get_text_extents`) replaces the estimate where it answers."""
    out: list[Box] = []
    for f in symbol_fields(sym):
        box = _field_box(f)
        if box is None:
            continue
        if extents is not None:
            box = extents(f.text) or box
        out.append(box)
    return out


# -- axis-aligned segment tests ---------------------------------------------------------


def on_segment(p: Sequence[float], a: Sequence[float], b: Sequence[float]) -> bool:
    """`p` lies on the axis-aligned segment a-b (end points included)."""
    if abs(a[0] - b[0]) < EPS_MM:
        return abs(p[0] - a[0]) < EPS_MM and (
            min(a[1], b[1]) - EPS_MM <= p[1] <= max(a[1], b[1]) + EPS_MM
        )
    if abs(a[1] - b[1]) < EPS_MM:
        return abs(p[1] - a[1]) < EPS_MM and (
            min(a[0], b[0]) - EPS_MM <= p[0] <= max(a[0], b[0]) + EPS_MM
        )
    return False


def in_segment_interior(p: Sequence[float], a: Sequence[float], b: Sequence[float]) -> bool:
    return on_segment(p, a, b) and not (same_point(p, a) or same_point(p, b))


def orthogonal(a: Sequence[float], b: Sequence[float]) -> bool:
    return abs(a[0] - b[0]) < EPS_MM or abs(a[1] - b[1]) < EPS_MM
