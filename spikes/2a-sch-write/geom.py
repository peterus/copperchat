"""Pin-end and bounding-box geometry from API data (what a future geometry.py would do).

KiCad (sch_symbol.cpp, SCH_SYMBOL::GetPinPhysicalPosition): physical = T * lib_pos + pos,
where T is the symbol's TRANSFORM built by SetOrientation(): rotations/mirrors are applied
incrementally (each step applied to the already transformed coordinates). Library coordinates as returned by the API are y-down
(schematic screen orientation), so the default transform is the identity.
"""

from __future__ import annotations

from harness import MM

# (x1, y1, x2, y2): x' = x1*x + y1*y ; y' = x2*x + y2*y
IDENT = (1, 0, 0, 1)
ROT_CCW = (0, 1, -1, 0)
ROT_CW = (0, -1, 1, 0)
MIR_Y = (-1, 0, 0, 1)  # KiCad SYM_MIRROR_Y: mirror about the Y axis (x -> -x)
MIR_X = (1, 0, 0, -1)  # KiCad SYM_MIRROR_X: mirror about the X axis (y -> -y)


def _mul(old, temp):
    """Compose as SetOrientation does: the incremental step acts on the already
    transformed coordinates (matrix product temp * old; verified by Q1's netlist check)."""
    a1, b1, c1, d1 = temp
    a2, b2, c2, d2 = old
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2)


def transform(orientation_deg: int, mirror_x: bool, mirror_y: bool):
    t = IDENT
    steps = {0: [], 90: [ROT_CCW], 180: [ROT_CCW, ROT_CCW], 270: [ROT_CW]}[orientation_deg]
    for s in steps:
        t = _mul(t, s)
    if mirror_x:
        t = _mul(t, MIR_X)
    if mirror_y:
        t = _mul(t, MIR_Y)
    return t


def apply(t, x, y):
    x1, y1, x2, y2 = t
    return (x1 * x + y1 * y, x2 * x + y2 * y)


SSO_DEG = {0: 0, 1: 0, 2: 90, 3: 180, 4: 270}  # SSO_UNKNOWN, SSO_0..SSO_270


def sym_transform(sym):
    tr = sym.transform
    return transform(SSO_DEG[int(tr.orientation)], tr.mirror_x, tr.mirror_y)


def pin_ends_abs(sym) -> dict[str, tuple[float, float]]:
    """Absolute connection points (mm) of a placed symbol's pins of its own unit."""
    t = sym_transform(sym)
    px, py = sym.position.x, sym.position.y
    out = {}
    for child in sym.definition.items:
        item = child.item
        if type(item).__name__ != "SchematicPin":
            continue
        if not _in_unit(child, sym):
            continue
        dx, dy = apply(t, item.position.x, item.position.y)
        out[item.number] = (round((px + dx) / MM, 4), round((py + dy) / MM, 4))
    return out


# --------------------------------------------------------------------- bounding boxes

_PIN_DIR = {  # SchematicPinOrientation -> unit vector from the connection point inward
    1: (1, 0),  # SPO_RIGHT: pin points right, body is to the right of the end
    2: (-1, 0),  # SPO_LEFT
    3: (0, -1),  # SPO_UP
    4: (0, 1),  # SPO_DOWN
}


def _shape_points(shape) -> list[tuple[int, int]]:
    """Corners of one library graphic's bounding box (nm, library coords)."""
    b = shape.bounding_box()
    return [(b.pos.x, b.pos.y), (b.pos.x + b.size.x, b.pos.y + b.size.y)]


def _in_unit(child, sym) -> bool:
    if child.unit not in (None, 0, sym.unit):
        return False
    style = child._proto.body_style.style if child._proto.HasField("body_style") else 0
    return style in (0, sym.body_style or 1)


def symbol_bbox(sym, with_pins: bool = True, stroke_nm: int = 0):
    """Body graphics + pins of the placed unit, transformed; returns (x0, y0, x1, y1) mm."""
    from kipy.common_types import to_concrete_shape

    t = sym_transform(sym)
    pts: list[tuple[int, int]] = []
    for child in sym.definition.items:
        if not _in_unit(child, sym):
            continue
        item = child.item
        name = type(item).__name__
        if name == "SchematicGraphicShape" and item.shape is not None:
            shape = to_concrete_shape(item.shape) or item.shape
            pts += _shape_points(shape)
        elif name == "SchematicPin" and with_pins:
            x, y = item.position.x, item.position.y
            dx, dy = _PIN_DIR.get(int(item.orientation), (0, 0))
            pts += [(x, y), (x + dx * item.length, y + dy * item.length)]
    if not pts:
        return None
    abs_pts = [apply(t, x, y) for x, y in pts]
    xs = [sym.position.x + p[0] for p in abs_pts]
    ys = [sym.position.y + p[1] for p in abs_pts]
    h = stroke_nm / 2
    return tuple(round(v / MM, 3) for v in (min(xs) - h, min(ys) - h, max(xs) + h, max(ys) + h))
