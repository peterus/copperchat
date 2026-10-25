"""Rigid transforms for `sch_move` (v0.5.0 #2 / #20): rotating and mirroring labels and
sheet symbols, and carrying a part's own stub chains with the part.

A `Frame` maps a point the way a moved owner (a symbol, a sheet symbol, a sheet pin)
moves: `old` goes to `new`, every offset from `old` turned by the matrix. A plain
translation is a frame with the identity matrix.

Sheets have no orientation in KiCad (kipy `SheetSymbol`: position, size, pins). They are
turned as KiCad's own `SCH_SHEET::Rotate` / `Mirror*` do it: the rectangle about its
centre, every pin rigidly with it onto its new edge (side and spin style from the
edge), the Sheetname / Sheetfile fields laid out again as `SCH_SHEET::AutoplaceFields`
does when they were autoplaced, else shifted with the corner. Labels turn about their
anchor; their spin style is the text direction, their fields follow as
`SCH_LABEL_BASE::AutoplaceFields` places them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from copperchat.kicad_tools.edit.geometry import (
    CANONICAL,
    EPS_MM,
    GRID_MM,
    Box,
    Matrix,
    Point,
    apply,
    mm_point,
    transform_matrix,
)

Mirror = Literal["", "x", "y"]
Dir = tuple[int, int]
IDENT: Matrix = (1, 0, 0, 1)
# SchematicLabelSpinStyle -> the direction the text runs (screen, y down)
SPIN_DIR: dict[int, Dir] = {1: (-1, 0), 2: (0, -1), 3: (1, 0), 4: (0, 1)}
DIR_SPIN: dict[Dir, int] = {d: s for s, d in SPIN_DIR.items()}
# the angle KiCad writes for a label of that text direction (degrees CCW)
DIR_ANGLE: dict[Dir, int] = {(1, 0): 0, (0, -1): 90, (-1, 0): 180, (0, 1): 270}
ANGLE_DIR: dict[int, Dir] = {a: d for d, a in DIR_ANGLE.items()}
SIDE_DIR: dict[str, str] = {"left": "left", "right": "right", "top": "up", "bottom": "down"}
HALF_GRID = GRID_MM / 2


def _r(v: float) -> float:
    return round(v, 4)


def matrix_of(rotation: int | None, mirror: Mirror | None) -> Matrix:
    """The relative transform `rotation` degrees CCW, then `mirror` ('x': upside down,
    'y': left-right) — the order KiCad's symbol transforms use."""
    return transform_matrix((rotation or 0) % 360, mirror == "x", mirror == "y")


def compose(a: Matrix, b: Matrix) -> Matrix:
    """a after b."""
    a1, b1, c1, d1 = a
    a2, b2, c2, d2 = b
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2)


def decompose(m: Matrix) -> tuple[int, Mirror]:
    """(rotation, mirror) as `sch_move` / `set_transform` take them for matrix `m`."""
    for deg, mx, my in CANONICAL:
        if transform_matrix(deg, mx, my) == m:
            return deg, "x" if mx else "y" if my else ""
    raise ValueError(f"not an orthogonal transform: {m}")


def turn(m: Matrix, d: Dir) -> Dir:
    x, y = apply(m, *d)
    return (round(x), round(y))


@dataclass(frozen=True)
class Frame:
    """How one moved owner maps points: `old` -> `new`, offsets turned by `m`."""

    old: Point
    new: Point
    m: Matrix = IDENT

    @property
    def rigid(self) -> bool:
        return self.m == IDENT

    def __call__(self, p: Point) -> Point:
        dx, dy = apply(self.m, p[0] - self.old[0], p[1] - self.old[1])
        return (_r(self.new[0] + dx), _r(self.new[1] + dy))

    def dir(self, d: Dir) -> Dir:
        return turn(self.m, d)


def translation(d: Point) -> Frame:
    return Frame((0.0, 0.0), (_r(d[0]), _r(d[1])))


# -- labels ------------------------------------------------------------------------------


def label_matrix(spin: int, rotation: int | None, mirror: Mirror | None) -> Matrix:
    """The relative transform a label with spin style `spin` gets for an absolute
    `rotation` (its text angle, as KiCad writes it) and a `mirror` flip of the text
    direction ('x': up <-> down, 'y': left <-> right). A label has no mirror state: a
    mirror always flips."""
    old = SPIN_DIR.get(spin, (1, 0))
    rel = 0 if rotation is None else (rotation - DIR_ANGLE[old]) % 360
    return matrix_of(rel, mirror)


def _align(d: Dir) -> tuple[int, int]:
    """(text angle, horizontal alignment) KiCad's label AutoplaceFields gives a field of
    a label whose text runs in `d`."""
    from kipy.proto.common.types.enums_pb2 import HA_LEFT, HA_RIGHT

    angle = 0 if d[0] else 90
    return angle, HA_LEFT if d in ((1, 0), (0, -1)) else HA_RIGHT


def label_fields(item: Any) -> list[Any]:
    out = list(item.fields)
    refs = getattr(type(item), "intersheet_refs_field", None)
    if refs is not None and item.proto.HasField("intersheet_refs_field"):
        out.append(item.intersheet_refs_field)
    return out


def turn_label(item: Any, frame: Frame, direction: Dir | None = None) -> Dir:
    """Move and turn kipy label `item` by `frame` (anchor mapped, text direction turned,
    fields following); `direction` overrides the turned text direction (a label on a pin
    reads away from it). Returns the new text direction."""
    from copperchat.kicad_tools.edit.labels import _v2

    old_at = mm_point(item.position)
    old_dir = SPIN_DIR.get(int(item.spin_style), (1, 0))
    at, new_dir = frame(old_at), direction or frame.dir(old_dir)
    item.position = _v2(at)
    item.text.position = _v2(frame(mm_point(item.text.position)))
    item.spin_style = DIR_SPIN[new_dir]
    autoplaced = bool(item.fields_autoplaced)
    for f in label_fields(item):
        off = mm_point(f.text.position)
        off = (off[0] - old_at[0], off[1] - old_at[1])
        if autoplaced:  # along the text direction, and the fixed +y / +x side across it
            along = off[0] * old_dir[0] + off[1] * old_dir[1]
            across = off[1] if old_dir[0] else off[0]
            new_off = (along * new_dir[0], along * new_dir[1])
            new_off = (
                (new_off[0], new_off[1] + across)
                if new_dir[0]
                else (new_off[0] + across, new_off[1])
            )
            angle, h_align = _align(new_dir)
            f.text.attributes.angle = angle
            f.text.attributes.horizontal_alignment = h_align
        else:
            new_off = apply(frame.m, *off)
            if frame.m[0] == 0:  # a quarter turn: the field turns with it
                f.text.attributes.angle = 90 - float(f.text.attributes.angle) % 180
        f.text.position = _v2((_r(at[0] + new_off[0]), _r(at[1] + new_off[1])))
    return new_dir


# -- sheet symbols -----------------------------------------------------------------------


def _half(v: float) -> int:
    return round(v / HALF_GRID)


def sheet_center(box: Box, m: Matrix) -> Point:
    """The rotation centre of a sheet: its box centre on the half grid (as KiCad's
    GetNearestHalfGridPosition), moved by half a grid step where a quarter turn about it
    would put grid points off the grid (both coordinates must then be whole or both half
    grid steps)."""
    cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    hx, hy = _half(cx), _half(cy)
    if m[0] == 0 and (hx - hy) % 2:  # a quarter turn about a mixed centre
        options = [(hx, hy + 1), (hx, hy - 1), (hx + 1, hy), (hx - 1, hy)]
        hx, hy = min(
            options, key=lambda o: (o[0] * HALF_GRID - cx) ** 2 + (o[1] * HALF_GRID - cy) ** 2
        )
    return (_r(hx * HALF_GRID), _r(hy * HALF_GRID))


def box_of(sym: Any) -> Box:
    x, y = mm_point(sym.position)
    w, h = mm_point(sym.size)
    return Box(x, y, _r(x + w), _r(y + h))


def edge_side(box: Box, p: Point) -> str | None:
    if abs(p[0] - box.x0) < EPS_MM:
        return "left"
    if abs(p[0] - box.x1) < EPS_MM:
        return "right"
    if abs(p[1] - box.y0) < EPS_MM:
        return "top"
    if abs(p[1] - box.y1) < EPS_MM:
        return "bottom"
    return None


_SIDES = {1: "left", 2: "right", 3: "top", 4: "bottom"}  # SheetSide


def _vertical(sides: Sequence[str]) -> bool:
    """SCH_SHEET::IsVerticalOrientation: pins only on the top / bottom edges."""
    return any(s in ("top", "bottom") for s in sides) and not any(
        s in ("left", "right") for s in sides
    )


def _margins(sym: Any, box: Box, vertical: bool) -> tuple[float, float]:
    """The Sheetname / Sheetfile distances from the box of autoplaced fields (KiCad: half
    a pen width + 0.5 / 0.4 text heights); read back from the current layout."""
    name, file = mm_point(sym.name_field.text.position), mm_point(sym.filename_field.text.position)
    got = (box.x0 - name[0], file[0] - box.x1) if vertical else (box.y0 - name[1], file[1] - box.y1)
    size = max(mm_point(sym.name_field.text.attributes.size)) or 1.27
    default = (_r(0.0762 + 0.5 * size), _r(0.0762 + 0.4 * size))
    return tuple(g if 0 < g < 5 else d for g, d in zip(got, default, strict=True))  # type: ignore[return-value]


@dataclass
class TurnedSheet:
    item: Any  # the kipy SheetSymbol copy, turned
    box: Box
    frame: Frame
    pins: dict[str, tuple[Point, str]]  # pin uuid -> (new point, new side)


def turn_sheet(sym: Any, m: Matrix, d: Point, *, to: Point | None = None) -> TurnedSheet:
    """`sym` (a copy, changed in place) turned by `m` about its centre, then shifted by
    `d` — or, with `to`, so that the turned box's top-left corner lands there."""
    from copperchat.kicad_tools import access
    from copperchat.kicad_tools.edit.labels import _v2
    from copperchat.kicad_tools.edit.sheets import side_attrs

    box = box_of(sym)
    c = sheet_center(box, m)
    spin = Frame(c, c, m)
    a, b = spin((box.x0, box.y0)), spin((box.x1, box.y1))
    turned = Box(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
    if to is not None:
        d = (_r(to[0] - turned.x0), _r(to[1] - turned.y0))
    frame = Frame(c, (_r(c[0] + d[0]), _r(c[1] + d[1])), m)
    new = Box(turned.x0 + d[0], turned.y0 + d[1], turned.x1 + d[0], turned.y1 + d[1])
    new = Box(_r(new.x0), _r(new.y0), _r(new.x1), _r(new.y1))
    old_sides = [_SIDES.get(int(p.side), "left") for p in sym.pins]
    sym.position = _v2((new.x0, new.y0))
    sym.size = _v2((_r(new.width), _r(new.height)))
    pins = []
    moved: dict[str, tuple[Point, str]] = {}
    for pin in sym.pins:
        at = frame(mm_point(pin.position))
        side = edge_side(new, at) or _SIDES.get(int(pin.side), "left")
        pin.position = _v2(at)
        pin.text.position = _v2(at)
        pin.side, pin.spin_style = side_attrs(side)
        moved[access.kiid(pin)] = (at, side)
        pins.append(pin)
    sym.pins = pins
    _place_fields(sym, box, new, old_sides, [s for _p, s in moved.values()], m)
    return TurnedSheet(sym, new, frame, moved)


def _place_fields(
    sym: Any, old: Box, new: Box, old_sides: list[str], new_sides: list[str], m: Matrix
) -> None:
    from kipy.proto.common.types.enums_pb2 import HA_LEFT, VA_BOTTOM, VA_TOP

    from copperchat.kicad_tools.edit.labels import _v2

    quarter = m[0] == 0
    dx, dy = new.x0 - old.x0, new.y0 - old.y0
    autoplace = bool(sym.fields_autoplaced) and quarter
    for f in (*((sym.name_field, sym.filename_field) if not autoplace else ()), *sym.user_fields):
        p = mm_point(f.text.position)  # KiCad: these shift with the corner
        f.text.position = _v2((_r(p[0] + dx), _r(p[1] + dy)))
    if not autoplace:
        return
    name_m, file_m = _margins(sym, old, _vertical(old_sides))
    vertical = _vertical(new_sides)
    name, file = sym.name_field, sym.filename_field
    if vertical:
        name.text.position = _v2((_r(new.x0 - name_m), new.y1))
        file.text.position = _v2((_r(new.x1 + file_m), new.y1))
    else:
        name.text.position = _v2((new.x0, _r(new.y0 - name_m)))
        file.text.position = _v2((new.x0, _r(new.y1 + file_m)))
    for f, v_align in ((name, VA_BOTTOM), (file, VA_TOP)):
        f.text.attributes.angle = 90 if vertical else 0
        f.text.attributes.horizontal_alignment = HA_LEFT
        f.text.attributes.vertical_alignment = v_align


def side_direction(side: str) -> str:
    return SIDE_DIR[side]
