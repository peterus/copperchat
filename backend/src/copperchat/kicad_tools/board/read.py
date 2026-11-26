"""The board model loader (B-C3): one `session.call` reads the live board into the pure
`BoardModel` (B-C2), plus the defensive `.kicad_dru` read.

kipy coordinates are nm; everything here is converted to mm (board coordinates, y down).
Footprint children (pads, shapes, texts) come in board coordinates; footprint-embedded
zones (rule areas) come in the footprint frame (relative to the footprint position,
unrotated - verified on the pinned server) and are transformed here.
"""

from __future__ import annotations

import functools
import math
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Concatenate

from kipy.board_types import ArcTrack as KipyArcTrack
from kipy.board_types import (
    BoardArc,
    BoardBezier,
    BoardCircle,
    BoardPolygon,
    BoardRectangle,
    BoardSegment,
    BoardShape,
    BoardText,
    BoardTextBox,
    Field,
    FootprintInstance,
)
from kipy.board_types import Pad as KipyPad
from kipy.board_types import Zone as KipyZone
from kipy.proto.board import board_types_pb2
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.util.board_layer import canonical_name

from copperchat.board.geom import convex_hull, polygon_area, rect_poly
from copperchat.board.model import (
    ArcTrack,
    BoardModel,
    GraphicItem,
    NetClassInfo,
    Outline,
    Pad,
    Part,
    Point,
    TextItem,
    Track,
    Via,
    Zone,
)
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_session.session import request_timeout
from copperchat.kicad_tools.access import sheet_path_key
from copperchat.kicad_tools.board.api import board_project, save_document_to_string
from copperchat.kicad_tools.units import nm_to_mm

if TYPE_CHECKING:
    from copperchat.projects import Project
    from copperchat.tools.spec import ToolContext

# A board model or snapshot read serialises the whole board (SaveDocumentToString for
# the layer types): seconds on a big board, so every such `session.call` passes this
# explicitly instead of the 30 s default (B-C4).
BOARD_READ_TIMEOUT_S = 120.0
# kipy bounds every single request by the client's transport timeout (30 s), whatever the
# outer `session.call` timeout is: a board-wide read or compensation raises it for its
# own requests (`request_timeout`), leaving 10 s of the call's budget as margin.
BOARD_REQUEST_TIMEOUT_S = BOARD_READ_TIMEOUT_S - 10.0


def long_requests[**P, R](
    fn: Callable[Concatenate[KicadHandle, P], R],
) -> Callable[Concatenate[KicadHandle, P], R]:
    """Run `fn(h, ...)` with kipy's per-request timeout raised to BOARD_REQUEST_TIMEOUT_S."""

    @functools.wraps(fn)
    def wrapper(h: KicadHandle, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with request_timeout(h.kicad, BOARD_REQUEST_TIMEOUT_S):
            return fn(h, *args, **kwargs)

    return wrapper


# -- .kicad_dru --------------------------------------------------------------------------

DRU_MAX_BYTES = 1024 * 1024
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY  # as design_docs.paths
DRU_NOT_REGULAR = ".kicad_dru ignored: not a regular file"
DRU_OVERSIZE = ".kicad_dru ignored: larger than 1 MiB"


def _oversize(st: os.stat_result) -> tuple[str, str, tuple[str, ...]]:
    return "", f"oversize:{st.st_size}:{st.st_mtime_ns}", (DRU_OVERSIZE,)


def read_dru(path: Path) -> tuple[str, str, tuple[str, ...]]:
    """(dru_text, dru_marker, warnings) of a project's `.kicad_dru`.

    Only a regular file is read: `lstat` first (a symlink, FIFO, directory or device
    counts as absent, with a warning, and is never opened), then `open` with
    O_NOFOLLOW | O_NONBLOCK and `fstat` again (a swap in between is refused the same
    way). Bigger than DRU_MAX_BYTES → no text, a warning and a size/mtime marker that
    still enters `board_hash`."""
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "", "", ()
    if not stat.S_ISREG(st.st_mode):
        return "", "", (DRU_NOT_REGULAR,)
    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        return "", "", ()
    except OSError:  # ELOOP: swapped for a symlink since the lstat
        return "", "", (DRU_NOT_REGULAR,)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "", "", (DRU_NOT_REGULAR,)
        if st.st_size > DRU_MAX_BYTES:
            return _oversize(st)
        chunks: list[bytes] = []
        size = 0
        while size <= DRU_MAX_BYTES:
            chunk = os.read(fd, DRU_MAX_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > DRU_MAX_BYTES:  # grew after the fstat
            return _oversize(os.fstat(fd))
        return b"".join(chunks).decode("utf-8", errors="replace"), "", ()
    finally:
        os.close(fd)


# -- geometry helpers ----------------------------------------------------------------------


def _pt(v: Any) -> Point:
    return (nm_to_mm(v.x), nm_to_mm(v.y))


def _layer(layer: Any) -> str:
    return str(canonical_name(layer))


def _arc_points(start: Point, mid: Point, end: Point, n: int = 8) -> list[Point]:
    """`n` + 1 points along the arc through start, mid, end (straight if collinear)."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [start, end]
    ux = (
        (ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)
    ) / d
    uy = (
        (ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)
    ) / d
    r = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a1 = math.atan2(cy - uy, cx - ux)
    sweep = (a1 - a0) % (2 * math.pi)
    if (am - a0) % (2 * math.pi) > sweep:  # mid lies on the other way round
        sweep -= 2 * math.pi
    pts = [
        (ux + r * math.cos(a0 + sweep * i / n), uy + r * math.sin(a0 + sweep * i / n))
        for i in range(n + 1)
    ]
    pts[0], pts[-1] = start, end
    return pts


def _circle_points(center: Point, radius: float, n: int = 32) -> list[Point]:
    cx, cy = center
    return [
        (cx + radius * math.cos(2 * math.pi * i / n), cy + radius * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def _polyline(line: Any) -> list[Point]:
    """Points of a kipy PolyLine; arc nodes are sampled."""
    out: list[Point] = []
    for node in line.nodes:
        if node.has_point:
            out.append(_pt(node.point))
        elif node.has_arc:
            arc = node.arc
            out.extend(_arc_points(_pt(arc.start), _pt(arc.mid), _pt(arc.end)))
    return out


def _shape_kind_points(shape: BoardShape) -> tuple[str, tuple[Point, ...]]:
    """(kind, defining points) of a board graphic - what `GraphicItem` stores."""
    if isinstance(shape, BoardSegment):
        return "segment", (_pt(shape.start), _pt(shape.end))
    if isinstance(shape, BoardArc):
        return "arc", (_pt(shape.start), _pt(shape.mid), _pt(shape.end))
    if isinstance(shape, BoardCircle):
        return "circle", (_pt(shape.center), _pt(shape.radius_point))
    if isinstance(shape, BoardRectangle):
        return "rect", (_pt(shape.top_left), _pt(shape.bottom_right))
    if isinstance(shape, BoardPolygon):
        pts: list[Point] = []
        for poly in shape.polygons:
            pts.extend(_polyline(poly.outline))
        return "polygon", tuple(pts)
    if isinstance(shape, BoardBezier):
        return "bezier", (
            _pt(shape.start),
            _pt(shape.control1),
            _pt(shape.control2),
            _pt(shape.end),
        )
    return "other", ()


def _dense(shape: BoardShape) -> tuple[list[Point], bool]:
    """Points along a shape's outline and whether it is closed by itself."""
    kind, pts = _shape_kind_points(shape)
    if kind == "arc":
        return _arc_points(pts[0], pts[1], pts[2]), False
    if kind == "circle":
        (cx, cy), (rx, ry) = pts
        return _circle_points((cx, cy), math.hypot(rx - cx, ry - cy)), True
    if kind == "rect":
        (x0, y0), (x1, y1) = pts
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], True
    if kind == "polygon":
        return list(pts), True
    return list(pts), False


def _stroke(shape: BoardShape) -> float:
    return nm_to_mm(shape.proto.shape.attributes.stroke.width.value_nm)


_CLOSE_MM = 1e-3


def _close(a: Point, b: Point) -> bool:
    return abs(a[0] - b[0]) < _CLOSE_MM and abs(a[1] - b[1]) < _CLOSE_MM


def _cell(p: Point) -> tuple[int, int]:
    return (math.floor(p[0] / _CLOSE_MM), math.floor(p[1] / _CLOSE_MM))


def _loops(shapes: list[BoardShape]) -> list[list[Point]]:
    """Closed loops from closed shapes and from open pieces chained end to end.

    Open pieces are chained in input order: each chain starts at the first unused
    piece and takes the lowest-index unused piece whose start (else end) is `_close`
    to its tail. An endpoint index on a `_CLOSE_MM` grid (two close points lie in the
    same or a neighbouring cell) keeps this near-linear on big outlines."""
    loops: list[list[Point]] = []
    pieces: list[list[Point]] = []
    for sh in shapes:
        pts, closed = _dense(sh)
        if closed:
            loops.append(pts)
        elif len(pts) >= 2:
            pieces.append(pts)
    index: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for i, piece in enumerate(pieces):
        for end, p in ((0, piece[0]), (1, piece[-1])):
            index.setdefault(_cell(p), []).append((i, end))
    used = [False] * len(pieces)

    def match(tail: Point) -> tuple[int, int] | None:
        cx, cy = _cell(tail)
        best: tuple[int, int] | None = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                found = index.get((cx + dx, cy + dy))
                if not found:
                    continue
                found[:] = [e for e in found if not used[e[0]]]  # drop used entries
                for i, end in found:
                    p = pieces[i][-1] if end else pieces[i][0]
                    if (best is None or (i, end) < best) and _close(tail, p):
                        best = (i, end)
        return best

    for first in range(len(pieces)):
        if used[first]:
            continue
        used[first] = True
        chain = list(pieces[first])
        while not (len(chain) > 2 and _close(chain[0], chain[-1])):
            found = match(chain[-1])
            if found is None:
                break
            i, end = found
            used[i] = True
            chain += pieces[i][-2::-1] if end else pieces[i][1:]
        if len(chain) > 2 and _close(chain[0], chain[-1]):
            loops.append(chain[:-1])
    return loops


def _outline(edge: list[BoardShape]) -> Outline:
    """Edge.Cuts → Outline: "rect" for exactly one rectangle, "other" for any other
    shape set (polygon = the largest closed loop, else the hull), "none" without any."""
    if not edge:
        return Outline("none", 0.0, 0.0, 0.0, 0.0, 0.0, ())
    if len(edge) == 1 and isinstance(edge[0], BoardRectangle):
        rect = edge[0]
        (ax, ay), (bx, by) = _pt(rect.top_left), _pt(rect.bottom_right)
        x, y, w, h = min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay)
        r = nm_to_mm(rect.proto.shape.rectangle.corner_radius.value_nm)
        return Outline("rect", x, y, w, h, r, tuple(rect_poly(x, y, w, h, r)))
    every = [p for sh in edge for p in _dense(sh)[0]]
    loops = _loops(edge)
    polygon = max(loops, key=lambda lp: abs(polygon_area(lp))) if loops else convex_hull(every)
    xs, ys = [p[0] for p in every], [p[1] for p in every]
    x, y = min(xs), min(ys)
    return Outline("other", x, y, max(xs) - x, max(ys) - y, 0.0, tuple(polygon))


def _to_board(local: Point, origin: Point, degrees: float) -> Point:
    """Footprint frame → board (kipy `Vector2.rotate` convention, y down)."""
    a = math.radians(degrees)
    x, y = local
    return (
        round(origin[0] + y * math.sin(a) + x * math.cos(a), 4),
        round(origin[1] + y * math.cos(a) - x * math.sin(a), 4),
    )


# -- items ---------------------------------------------------------------------------------


def _text(kind: str, t: BoardText, visible: bool) -> TextItem:
    attrs = t.attributes
    return TextItem(
        kind=kind,
        text=str(t.value),
        layer=_layer(t.layer),
        at=_pt(t.position),
        size=_pt(attrs.size),
        thickness=nm_to_mm(attrs.stroke_width),
        visible=visible,
        angle=float(attrs.angle) % 360,
    )


def _texts(fp: FootprintInstance) -> tuple[TextItem, ...]:
    out = [
        _text("reference", fp.reference_field.text, fp.reference_field.visible),
        _text("value", fp.value_field.text, fp.value_field.visible),
    ]
    for item in fp.definition.items:
        if isinstance(item, Field):
            out.append(_text(f"field:{item.name}", item.text, item.visible))
        elif isinstance(item, BoardText):
            out.append(_text("text", item, True))  # free texts: always visible at the pin
        elif isinstance(item, BoardTextBox):
            out.append(_textbox(item))
    return tuple(out)


def _item_kiids(fp: FootprintInstance) -> tuple[tuple[str, str], ...]:
    """KIID -> role of the footprint's own items (P7): pads, fields, texts, shapes."""
    out = [(str(p.id.value), f"pad:{p.number}") for p in fp.definition.pads]
    out.append((str(fp.reference_field.text.id.value), "reference"))
    out.append((str(fp.value_field.text.id.value), "value"))
    for item in fp.definition.items:
        if isinstance(item, Field):
            out.append((str(item.text.id.value), f"field:{item.name}"))
        elif isinstance(item, BoardText):
            out.append((str(item.id.value), "text"))
        elif isinstance(item, BoardTextBox):
            out.append((str(item.id.value), "textbox"))
    out += [(str(sh.id.value), "shape") for sh in fp.definition.shapes]
    return tuple((k, role) for k, role in out if k)


_SILK_LAYERS = {"F.SilkS", "B.SilkS"}


def _silk(fp: FootprintInstance) -> tuple[GraphicItem, ...]:
    """The footprint's silkscreen graphics (board coordinates, like its courtyard)."""
    out = []
    for sh in fp.definition.shapes:
        layer = _layer(sh.layer)
        if layer in _SILK_LAYERS:
            kind, pts = _shape_kind_points(sh)
            out.append(GraphicItem(layer=layer, kind=kind, points=pts, width=_stroke(sh)))
    return tuple(out)


def _textbox(item: BoardTextBox) -> TextItem:
    attrs = item.attributes
    return TextItem(
        kind="textbox",
        text=str(item.value),
        layer=_layer(item.layer),
        at=_pt(item.top_left),
        size=_pt(attrs.size),
        thickness=nm_to_mm(attrs.stroke_width),
        visible=True,
    )


def _board_texts(board: Any) -> tuple[TextItem, ...]:
    """Board-level texts and text boxes (footprint texts are the parts' own)."""
    return tuple(
        _textbox(t) if isinstance(t, BoardTextBox) else _text("text", t, True)
        for t in board.get_text()
    )


_DRILLED = (board_types_pb2.PT_PTH, board_types_pb2.PT_NPTH)


def _pad(ref: str, pad: KipyPad) -> Pad:
    ps = pad.padstack
    copper = ps.copper_layers
    w = h = 0.0
    shape = "unknown"
    if len(copper):
        w, h = _pt(copper[0].size)
        name = board_types_pb2.PadStackShape.Name(copper[0].shape)
        shape = name.removeprefix("PSS_").lower()
    drill = nm_to_mm(ps.drill.diameter.x) if pad.pad_type in _DRILLED else None
    return Pad(
        ref=ref,
        number=str(pad.number),
        net=str(pad.net.name),
        x=nm_to_mm(pad.position.x),
        y=nm_to_mm(pad.position.y),
        w=w,
        h=h,
        rot=ps.angle.degrees % 360,
        shape=shape,
        layers=frozenset(_layer(layer) for layer in ps.layers),
        smd=pad.pad_type == board_types_pb2.PT_SMD,
        drill=drill,
    )


_CRTYD = {"F.CrtYd", "B.CrtYd"}


def _part(fp: FootprintInstance, group: str | None) -> Part:
    ref = str(fp.reference_field.text.value)
    origin = _pt(fp.position)
    rot = fp.orientation.degrees
    outline_pts: list[Point] = []
    for sh in fp.definition.shapes:
        if _layer(sh.layer) in _CRTYD:
            outline_pts.extend(_dense(sh)[0])
    rule_areas = tuple(
        tuple(_to_board(p, origin, rot) for p in _polyline(item.outline.outline))
        for item in fp.definition.items
        if isinstance(item, KipyZone) and item.is_rule_area()
    )
    symbol_path = fp.proto.symbol_path
    return Part(
        ref=ref,
        kiid=str(fp.id.value),
        lib_id=str(fp.definition.id),
        value=str(fp.value_field.text.value),
        x=origin[0],
        y=origin[1],
        rot=rot % 360,
        side="bottom" if fp.layer == BoardLayer.BL_B_Cu else "top",
        locked=fp.locked,
        courtyard=tuple(convex_hull(outline_pts)) if len(outline_pts) >= 3 else (),
        pads=tuple(_pad(ref, p) for p in fp.definition.pads),
        sheet=str(fp.symbol_sheet_name),
        symbol_path=sheet_path_key(symbol_path) if len(symbol_path.path) else "",
        texts=_texts(fp),
        group=group,
        rule_areas=rule_areas,
        item_kiids=_item_kiids(fp),
        silk=_silk(fp),
        exclude_from_bom=bool(fp.attributes.exclude_from_bill_of_materials),
        exclude_from_pos=bool(fp.attributes.exclude_from_position_files),
    )


def _enum_name(enum: Any, value: Any, prefix: str) -> str:
    try:
        return str(enum.Name(value)).removeprefix(prefix).lower()
    except ValueError:
        return str(value)


def _mm_or_blank(nm: int | None) -> str:
    return "" if nm is None else f"{nm_to_mm(nm):g}"


def _zone_settings(z: KipyZone) -> tuple[tuple[str, str], ...]:
    if z.is_rule_area():
        return ()
    settings = [
        ("clearance", _mm_or_blank(z.clearance)),
        ("min_thickness", _mm_or_blank(z.min_thickness)),
        (
            "fill_mode",
            ""
            if z.fill_mode is None
            else _enum_name(board_types_pb2.ZoneFillMode, z.fill_mode, "ZFM_"),
        ),
        (
            "island_mode",
            ""
            if z.island_mode is None
            else _enum_name(board_types_pb2.IslandRemovalMode, z.island_mode, "IRM_"),
        ),
    ]
    conn = z.connection
    if conn is not None:
        style = _enum_name(board_types_pb2.ZoneConnectionStyle, conn.zone_connection, "ZCS_")
        settings += [
            ("connection", style),
            ("thermal_gap", _mm_or_blank(conn.thermal_spokes.gap)),
            ("thermal_spoke_width", _mm_or_blank(conn.thermal_spokes.width)),
        ]
    return tuple(settings)


def _keepout(z: KipyZone) -> frozenset[str]:
    ra = z.rule_area_settings
    if not z.is_rule_area() or ra is None:
        return frozenset()
    flags = {
        "tracks": ra.keepout_tracks,
        "vias": ra.keepout_vias,
        "copper": ra.keepout_copper,
        "footprints": ra.keepout_footprints,
    }
    return frozenset(k for k, on in flags.items() if on)


def _zone(z: KipyZone) -> Zone:
    filled: list[tuple[Point, ...]] = []
    holes: list[tuple[tuple[Point, ...], ...]] = []
    filled_layers: list[str] = []
    area = 0.0
    for layer, polys in z.filled_polygons.items():
        for poly in polys:
            outline = _polyline(poly.outline)
            filled.append(tuple(outline))
            filled_layers.append(_layer(layer))
            poly_holes = tuple(tuple(_polyline(hole)) for hole in poly.holes)
            holes.append(poly_holes)
            area += abs(polygon_area(outline))
            area -= sum(abs(polygon_area(hole)) for hole in poly_holes)
    net = z.net
    return Zone(
        kiid=str(z.id.value),
        name=str(z.name),
        net=str(net.name) if net is not None and not z.is_rule_area() else "",
        layers=tuple(_layer(layer) for layer in z.layers),
        outline=tuple(_polyline(z.outline.outline)),
        priority=int(z.priority),
        rule_area=z.is_rule_area(),
        keepout=_keepout(z),
        settings=_zone_settings(z),
        filled_area_mm2=round(area, 4),
        filled=tuple(filled),
        filled_holes=tuple(holes),
        filled_layers=tuple(filled_layers),
    )


def _rules(board: Any) -> tuple[tuple[str, float], ...]:
    constraints = board.get_design_rules().rules.proto.constraints
    out: list[tuple[str, float]] = []
    for field in constraints.DESCRIPTOR.fields:
        value = getattr(constraints, field.name)
        if field.message_type is not None:
            out.append((field.name, nm_to_mm(value.value_nm)))
        else:
            out.append((field.name, float(value)))
    return tuple(out)


def _stackup(board: Any) -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    dielectric = 0
    for layer in board.get_stackup().layers:
        if not layer.enabled:
            continue
        material = str(layer.material_name)
        if layer.layer == BoardLayer.BL_UNDEFINED:
            dielectric += 1
            name = f"dielectric {dielectric}"
            props = layer.dielectric.layers if layer.dielectric is not None else []
            material = material or (str(props[0].material_name) if props else "")
        else:
            name = _layer(layer.layer)
        out.append((name, f"{nm_to_mm(layer.thickness):g}/{material}"))
    return tuple(out)


_LAYER_LINE = re.compile(r'^\s*\(\d+\s+"([^"]+)"\s+(\w+)', re.M)


def _layer_types(text: str, copper: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """(copper layer, KiCad layer type) from the board header `(layers …)` block - the
    API has no layer-type query at the pin (P2 probes setting it)."""
    start = text.find("\n\t(layers")
    if start < 0:
        return ()
    end = text.find("\n\t)", start)
    types = dict(_LAYER_LINE.findall(text[start : end if end > 0 else None]))
    return tuple((name, types[name]) for name in copper if name in types)


def _net_classes(
    h: KicadHandle, board: Any, nets: list[Any]
) -> tuple[tuple[NetClassInfo, ...], tuple[tuple[str, str], ...]]:
    project = board_project(h)
    classes = list(project.get_net_classes())
    patterns = project.get_net_class_assignments().pattern_assignments
    default = next((c for c in classes if c.name == "Default"), None)

    def val(c: Any, attr: str) -> float:
        v = getattr(c, attr)
        if v is None and default is not None:
            v = getattr(default, attr)
        return 0.0 if v is None else nm_to_mm(v)

    def opt(c: Any, attr: str) -> float | None:
        v = getattr(c, attr)
        return None if v is None else nm_to_mm(v)

    infos = tuple(
        NetClassInfo(
            name=str(c.name),
            track_width=val(c, "track_width"),
            clearance=val(c, "clearance"),
            via_diameter=val(c, "via_diameter"),
            via_drill=val(c, "via_drill"),
            diff_pair_width=opt(c, "diff_pair_track_width"),
            diff_pair_gap=opt(c, "diff_pair_gap"),
            patterns=tuple(str(p.pattern) for p in patterns if p.netclass == c.name),
        )
        for c in classes
    )
    named = [n for n in nets if n.name]
    effective = board.get_netclass_for_nets(named) if named else {}
    net_class_of = tuple(
        sorted(
            (str(net), str(nc.name) if nc.name else ",".join(nc.constituents or ()))
            for net, nc in effective.items()
        )
    )
    return infos, net_class_of


# -- loader --------------------------------------------------------------------------------


@long_requests
def load_board_model(h: KicadHandle, project: Project) -> BoardModel:
    """The live board as a `BoardModel` (run inside one `session.call`)."""
    board = h.board()
    footprints = list(board.get_footprints())
    ref_of = {str(fp.id.value): str(fp.reference_field.text.value) for fp in footprints}
    groups: list[tuple[str, tuple[str, ...]]] = []
    group_of: dict[str, str] = {}
    for g in board.get_groups():
        members = [str(k.value) for k in g.item_ids]
        refs = tuple(sorted(ref_of[k] for k in members if k in ref_of))
        groups.append((str(g.name), refs))
        for k in members:
            group_of.setdefault(k, str(g.name))

    tracks: list[Track] = []
    arcs: list[ArcTrack] = []
    for t in board.get_tracks():
        kiid, net, layer = str(t.id.value), str(t.net.name), _layer(t.layer)
        start, end, width = _pt(t.start), _pt(t.end), nm_to_mm(t.width)
        if isinstance(t, KipyArcTrack):
            arcs.append(ArcTrack(kiid, net, layer, start, _pt(t.mid), end, width, t.locked))
        else:
            tracks.append(Track(kiid, net, layer, start, end, width, t.locked))
    vias = tuple(
        Via(
            kiid=str(v.id.value),
            net=str(v.net.name),
            at=_pt(v.position),
            diameter=nm_to_mm(v.diameter),
            drill=nm_to_mm(v.drill_diameter),
            locked=v.locked,
        )
        for v in board.get_vias()
    )
    zones = tuple(_zone(z) for z in board.get_zones())
    shapes = list(board.get_shapes())
    graphics = tuple(
        GraphicItem(layer=_layer(sh.layer), kind=kind, points=pts, width=_stroke(sh))
        for sh in shapes
        for kind, pts in [_shape_kind_points(sh)]
    )
    edge = [sh for sh in shapes if sh.layer == BoardLayer.BL_Edge_Cuts]
    copper = tuple(
        _layer(layer)
        for layer in sorted(board.get_enabled_layers())
        if _layer(layer).endswith(".Cu")
    )
    net_classes, net_class_of = _net_classes(h, board, list(board.get_nets()))
    dru_text, dru_marker, warnings = read_dru(project.pro_file.with_suffix(".kicad_dru"))
    return BoardModel(
        parts=tuple(_part(fp, group_of.get(str(fp.id.value))) for fp in footprints),
        tracks=tuple(tracks),
        arcs=tuple(arcs),
        vias=vias,
        zones=zones,
        graphics=graphics,
        outline=_outline(edge),
        copper_layers=copper,
        layer_types=_layer_types(save_document_to_string(h), copper),
        rules=_rules(board),
        net_classes=net_classes,
        net_class_of=net_class_of,
        groups=tuple(sorted(groups)),
        dru_text=dru_text,
        stackup=_stackup(board),
        dru_marker=dru_marker,
        warnings=warnings,
        texts=_board_texts(board),
    )


def require_board(ctx: ToolContext) -> None:
    """Refuse a board tool on a project without a board (B-C14 `no_board`)."""
    if ctx.project.pcb_file is None:
        raise StructuredToolError(
            "No board",
            f"the project {ctx.project.name} has no .kicad_pcb yet",
            "run pcb_sync_from_schematic",
            code="no_board",
        )


async def board_model(ctx: ToolContext) -> BoardModel:
    """The live board of the tool call's project as a `BoardModel`."""
    require_board(ctx)
    project = ctx.project
    model: BoardModel = await ctx.session.call(
        lambda h: load_board_model(h, project), timeout=BOARD_READ_TIMEOUT_S
    )
    return model
