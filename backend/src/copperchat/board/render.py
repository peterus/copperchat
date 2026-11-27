"""Pure parts of the board render (B-C9, spec §13): zone-polygon simplification and the
wrapper SVG around KiCad's (sanitised) board drawing.

The wrapper draws in KiCad's own coordinates: `kicad-cli pcb export svg --page-size-mode
2` writes board millimetres as its viewBox (bottom view, `--mirror`: x negated), so
overlays only map board x to -x when mirrored. Every string the wrapper injects (titles,
block names, net names, numbers) goes through `_xml_text` (characters XML 1.0 forbids
dropped, then `xml.sax.saxutils.escape`); attribute values
are numbers or constants. No kipy, no I/O.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal
from xml.sax.saxutils import escape

from copperchat.board.geom import convex_hull
from copperchat.board.model import BoardModel, Point, Rect

SVG_NS = "http://www.w3.org/2000/svg"
BLOCK_PREFIX = "blk:"
SIMPLIFY_MIN_POINTS = 200  # paths with more points than this are simplified
# Point-distance evaluations one simplify_paths call may spend (RDP is O(n^2) on adversarial
# input; the sanitiser allows 2,000,000 coordinates). Past it the rest stays unsimplified.
SIMPLIFY_BUDGET = 5_000_000
SCALE_STEPS = (10.0, 5.0, 2.0, 1.0)
MAX_SCALE_COUNT = 10
HIGHLIGHT_MARGIN_MM = 0.3  # added to a highlighted track's width / pad radius
MIN_EXTENT_MM = 1.0  # a default view narrower than this (a lone marker) is grown around it

# Colours (dark background, like KiCad's board editor).
BG, TEXT, AXIS, HULL, MARK, HIGHLIGHT = (
    "#14181d",
    "#d0d7de",
    "#7d8a99",
    "#ffb000",
    "#ff3b30",
    "#00e5ff",
)
FONT = "DejaVu Sans, Arial, sans-serif"

_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


@dataclass(frozen=True)
class Hull:
    """A block's outline: the convex hull of its members' courtyards (board mm)."""

    name: str
    points: tuple[Point, ...]


@dataclass(frozen=True)
class Marker:
    """A numbered violation marker; `id` matches the tool result's marker list."""

    id: int
    x: float
    y: float


@dataclass(frozen=True)
class HighlightShape:
    """One highlighted copper item: a stroke along `points` (track) or a disc at
    `points[0]` (pad, via) of `width` mm."""

    net: str
    kind: Literal["line", "disc"]
    points: tuple[Point, ...]
    width: float


# --- model-derived overlays -------------------------------------------------------------


def block_hulls(m: BoardModel) -> list[Hull]:
    """One hull per `blk:<name>` group with at least one member courtyard, named
    `<name>`, in group order."""
    hulls: list[Hull] = []
    by_ref = {p.ref: p for p in m.parts}
    for group, refs in m.groups:
        if not group.startswith(BLOCK_PREFIX):
            continue
        pts = [pt for ref in refs if ref in by_ref for pt in by_ref[ref].courtyard]
        if len(pts) >= 3:
            hulls.append(Hull(group[len(BLOCK_PREFIX) :], tuple(convex_hull(pts))))
    return hulls


def highlight_shapes(
    m: BoardModel, nets: Sequence[str], layers: Collection[str] | None = None
) -> list[HighlightShape]:
    """The copper of `nets` from the model: tracks and arcs as thick strokes, pads and
    vias as discs. With `layers` (the view's copper layers) only what the view shows:
    tracks and arcs on one of `layers`, undrilled pads (SMD, edge connectors) with a layer
    in `layers` (so a top view skips bottom-side pads); vias and drilled pads span the
    board and are always kept. `None`: every layer."""
    wanted = set(nets)

    def shown(layer: str) -> bool:
        return layers is None or layer in layers

    out: list[HighlightShape] = []
    for t in m.tracks:
        if t.net in wanted and shown(t.layer):
            out.append(
                HighlightShape(t.net, "line", (t.start, t.end), t.width + HIGHLIGHT_MARGIN_MM)
            )
    for a in m.arcs:
        if a.net in wanted and shown(a.layer):
            out.append(
                HighlightShape(
                    a.net, "line", (a.start, a.mid, a.end), a.width + HIGHLIGHT_MARGIN_MM
                )
            )
    for v in m.vias:
        if v.net in wanted:
            out.append(HighlightShape(v.net, "disc", (v.at,), v.diameter + HIGHLIGHT_MARGIN_MM))
    for p in m.parts:
        for pad in p.pads:
            if pad.net not in wanted:
                continue
            if (
                pad.drill is None
                and layers is not None
                and not any(lay in layers for lay in pad.layers)
            ):
                continue
            size = max(pad.w, pad.h) + HIGHLIGHT_MARGIN_MM
            out.append(HighlightShape(pad.net, "disc", ((pad.x, pad.y),), size))
    return out


# --- path simplification ----------------------------------------------------------------


class _Budget:
    """Work left for one simplify_paths call (point-distance evaluations)."""

    def __init__(self, left: int) -> None:
        self.left = left


def _rdp(pts: list[Point], tol: float, budget: _Budget | None = None) -> list[Point]:
    """Ramer-Douglas-Peucker of an open polyline (iterative: no recursion limit). Once
    `budget` is spent, every point of the intervals still pending is kept (shape exact)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if budget is not None:
            if budget.left < j - i:
                for k in range(i + 1, j):
                    keep[k] = True
                budget.left = 0
                continue
            budget.left -= j - i
        (ax, ay), (bx, by) = pts[i], pts[j]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        worst, idx = -1.0, -1
        for k in range(i + 1, j):
            px, py = pts[k]
            if norm == 0.0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * (px - ax) - dx * (py - ay)) / norm
            if d > worst:
                worst, idx = d, k
        if idx > 0 and worst > tol:
            keep[idx] = True
            stack.extend(((i, idx), (idx, j)))
    return [p for p, k in zip(pts, keep, strict=True) if k]


def _simplify_ring(pts: list[Point], tol: float, budget: _Budget | None = None) -> list[Point]:
    """A closed ring: split at the point farthest from the first, simplify both halves."""
    if len(pts) < 4:
        return pts
    x0, y0 = pts[0]
    far = max(range(len(pts)), key=lambda k: math.hypot(pts[k][0] - x0, pts[k][1] - y0))
    if far == 0:
        return pts[:1]
    first = _rdp(pts[: far + 1], tol, budget)
    second = _rdp([*pts[far:], pts[0]], tol, budget)
    return first[:-1] + second[:-1]


def _subpaths(d: str) -> list[tuple[list[Point], bool]] | None:
    """(points, closed) per subpath of an absolute M/L/Z path; None for anything else
    (curves, relative commands, odd numbers)."""
    out: list[tuple[list[Point], bool]] = []
    nums: list[float] = []
    current: list[Point] | None = None

    def flush() -> bool:
        if len(nums) % 2:
            return False
        if current is not None:
            current.extend(zip(nums[0::2], nums[1::2], strict=True))
        nums.clear()
        return True

    for tok in _TOKEN.findall(d):
        if tok in ("M", "L", "Z"):
            if not flush():
                return None
            if tok == "M":
                current = []
                out.append((current, False))
            elif tok == "Z":
                if current is None or not out:
                    return None
                out[-1] = (current, True)
                current = None
            elif current is None:
                return None
        elif tok.isalpha():
            return None
        else:
            if current is None:
                return None
            nums.append(float(tok))
    if not flush():
        return None
    return out


def _fmt(v: float) -> str:
    text = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def simplify_paths(root: ET.Element, tol_mm: float = 0.02, *, budget: int = SIMPLIFY_BUDGET) -> int:
    """Simplify every `path` (in place) whose data is absolute `M`/`L`/`Z` only with more
    than 200 points (KiCad's zone fills), within `tol_mm` (Ramer-Douglas-Peucker; closed
    subpaths stay closed). Curves, relative commands and short paths are untouched. At
    most `budget` point-distance evaluations are spent; what is left stays as it is.
    Returns the number of points removed."""
    work = _Budget(budget)
    removed = 0
    for el in root.iter():
        if not isinstance(el.tag, str) or el.tag.rsplit("}", 1)[-1] != "path":
            continue
        d = el.get("d")
        if not d:
            continue
        subs = _subpaths(d)
        if subs is None or sum(len(p) for p, _ in subs) <= SIMPLIFY_MIN_POINTS:
            continue
        parts: list[str] = []
        for pts, closed in subs:
            simple = _simplify_ring(pts, tol_mm, work) if closed else _rdp(pts, tol_mm, work)
            removed += len(pts) - len(simple)
            if not simple:
                continue
            head, *rest = simple
            parts.append(f"M {_fmt(head[0])},{_fmt(head[1])}")
            parts.extend(f"L {_fmt(x)},{_fmt(y)}" for x, y in rest)
            if closed:
                parts.append("Z")
        el.set("d", "\n".join(parts))
    return removed


# --- wrapper ----------------------------------------------------------------------------


def scale_bar(width_mm: float) -> tuple[float, int]:
    """(step, count): a bar of `count` steps of 10/5/2/1 mm spanning 10-25 % of
    `width_mm` (the largest step that fits; one 1 mm step below 4 mm, at least the
    10 % for very wide drawings)."""
    lo, hi = 0.10 * width_mm, 0.25 * width_mm
    for step in SCALE_STEPS:
        count = max(1, math.ceil(lo / step - 1e-9))
        if count * step <= hi + 1e-9 and count <= MAX_SCALE_COUNT:
            return step, count
    if hi < 1.0:
        return 1.0, 1
    return SCALE_STEPS[0], max(1, math.ceil(lo / SCALE_STEPS[0] - 1e-9))


def _n(v: float) -> str:
    text = f"{round(v, 4):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _parse_view_box(root: ET.Element) -> Rect:
    try:
        x, y, w, h = (float(p) for p in (root.get("viewBox") or "").replace(",", " ").split())
    except ValueError:
        raise ValueError("the board SVG has no usable viewBox") from None
    if not all(map(math.isfinite, (x, y, w, h))) or w <= 0 or h <= 0:
        raise ValueError("the board SVG has no usable viewBox")
    return Rect(x, y, w, h)


@dataclass(frozen=True)
class _Frame:
    """Board → drawing coordinates (x negated in a mirrored drawing)."""

    mirror: bool

    def pt(self, p: Point) -> Point:
        return (-p[0], p[1]) if self.mirror else p

    def rect(self, r: Rect) -> Rect:
        return Rect(-(r.x + r.w), r.y, r.w, r.h) if self.mirror else r

    def board_x(self, x: float) -> float:
        return -x if self.mirror else x


# characters outside XML 1.0's Char production (C0 controls but \t \n \r, surrogates,
# U+FFFE/U+FFFF): one in a net or group name would make the wrapper unparseable
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _xml_text(text: str) -> str:
    """`text` as XML character data: illegal characters dropped, then escaped."""
    return escape(_XML_ILLEGAL.sub("", text))


def _text(x: float, y: float, size: float, text: str, anchor: str = "start") -> str:
    return (
        f'<text x="{_n(x)}" y="{_n(y)}" font-size="{_n(size)}" text-anchor="{anchor}">'
        f"{_xml_text(text)}</text>"
    )


def _points(pts: Sequence[Point]) -> str:
    return " ".join(f"{_n(x)},{_n(y)}" for x, y in pts)


def default_extent(model: BoardModel, markers: Sequence[Marker] = ()) -> Rect | None:
    """The default 2D view (spec §22.6, board mm): the outline's bounding box + every
    footprint's courtyard (staging parts outside the outline included; a part without a
    courtyard counts with its origin and its pads' circumscribed extents) + the marker
    points. Tracks, vias and zones are not included: copper off the outline is a DRC
    finding, shown with `markers`. Not KiCad's "board area", which
    any stray drawing far off the board stretches. `None` when there is nothing (the
    caller then keeps KiCad's whole drawing); a side below `MIN_EXTENT_MM` is grown
    around its centre."""
    xs: list[float] = []
    ys: list[float] = []
    o = model.outline
    if o.kind != "none" and o.w > 0 and o.h > 0:
        xs += [o.x, o.x + o.w]
        ys += [o.y, o.y + o.h]
    for p in model.parts:
        if p.courtyard:
            xs += [q[0] for q in p.courtyard]
            ys += [q[1] for q in p.courtyard]
            continue
        xs.append(p.x)
        ys.append(p.y)
        for q in p.pads:  # no courtyard: its pads' (axis-aligned, rotation-safe) extents
            r = math.hypot(q.w, q.h) / 2
            xs += [q.x - r, q.x + r]
            ys += [q.y - r, q.y + r]
    xs += [mk.x for mk in markers]
    ys += [mk.y for mk in markers]
    pairs = [(x, y) for x, y in zip(xs, ys, strict=True) if math.isfinite(x) and math.isfinite(y)]
    if not pairs:
        return None
    x0, x1 = min(x for x, _ in pairs), max(x for x, _ in pairs)
    y0, y1 = min(y for _, y in pairs), max(y for _, y in pairs)
    if x1 - x0 < MIN_EXTENT_MM:
        c = (x0 + x1) / 2
        x0, x1 = c - MIN_EXTENT_MM / 2, c + MIN_EXTENT_MM / 2
    if y1 - y0 < MIN_EXTENT_MM:
        c = (y0 + y1) / 2
        y0, y1 = c - MIN_EXTENT_MM / 2, c + MIN_EXTENT_MM / 2
    return Rect(x0, y0, x1 - x0, y1 - y0)


def wrap_svg(
    svg_text: str,
    *,
    extent: Rect | None = None,
    mirror: bool = False,
    scale_bar_on: bool = True,
    hulls: Sequence[Hull] = (),
    markers: Sequence[Marker] = (),
    highlight: Sequence[HighlightShape] = (),
    highlight_nets: Sequence[str] = (),
    title: str = "",
    board_size: tuple[float, float] | None = None,
) -> str:
    """CopperChat's SVG around KiCad's sanitised board drawing `svg_text` (B-C9).

    The view is `extent` (board mm; default the whole drawing) plus a margin and a
    bottom band with the scale bar, the `"<w> x <h> mm"` board size label (a
    multiplication sign) and the
    highlighted net names. Overlays in board coordinates: block hulls (dashed, name
    label), highlighted copper (thick translucent strokes), numbered violation markers,
    and the view corner's board coordinates (origin cross). `mirror`: the bottom view
    (KiCad negated x). Every injected string is XML-escaped."""
    kicad = ET.fromstring(svg_text)
    box = _parse_view_box(kicad)
    frame = _Frame(mirror)
    view = frame.rect(extent) if extent is not None else box
    size = max(view.w, view.h)
    font = 0.022 * size
    pad = 0.03 * size
    band = 3.8 * font if (scale_bar_on or board_size or highlight_nets) else 0.0
    ox, oy = view.x - pad, view.y - pad
    ow, oh = view.w + 2 * pad, view.h + 2 * pad + band
    stroke = 0.002 * size

    # KiCad's drawing: its own viewBox, placed at the same user coordinates
    for key in ("width", "height", "x", "y"):
        kicad.attrib.pop(key, None)
    kicad.set("x", _n(box.x))
    kicad.set("y", _n(box.y))
    kicad.set("width", _n(box.w))
    kicad.set("height", _n(box.h))
    kicad.attrib.pop("xmlns", None)
    drawing = ET.tostring(kicad, encoding="unicode")

    out: list[str] = [
        f'<svg xmlns="{SVG_NS}" version="1.1" width="{_n(ow)}mm" height="{_n(oh)}mm" '
        f'viewBox="{_n(ox)} {_n(oy)} {_n(ow)} {_n(oh)}">',
    ]
    if title:
        out.append(f"<title>{_xml_text(title)}</title>")
    out.append(f'<rect x="{_n(ox)}" y="{_n(oy)}" width="{_n(ow)}" height="{_n(oh)}" fill="{BG}"/>')
    out.append(drawing)

    if highlight:
        out.append(
            f'<g id="highlight" fill="none" stroke="{HIGHLIGHT}" stroke-opacity="0.75" '
            'stroke-linecap="round" stroke-linejoin="round">'
        )
        for s in highlight:
            pts = [frame.pt(p) for p in s.points]
            if s.kind == "line":
                out.append(f'<polyline points="{_points(pts)}" stroke-width="{_n(s.width)}"/>')
            else:
                (cx, cy) = pts[0]
                out.append(
                    f'<circle cx="{_n(cx)}" cy="{_n(cy)}" r="{_n(s.width / 2)}" '
                    f'fill="{HIGHLIGHT}" fill-opacity="0.45" stroke="none"/>'
                )
        out.append("</g>")

    if hulls:
        out.append(
            f'<g id="hulls" fill="none" stroke="{HULL}" stroke-width="{_n(2 * stroke)}" '
            f'stroke-dasharray="{_n(4 * font / 3)} {_n(2 * font / 3)}" '
            f'font-family="{FONT}">'
        )
        for h in hulls:
            pts = [frame.pt(p) for p in h.points]
            out.append(f'<polygon points="{_points(pts)}"/>')
            lx = min(x for x, _ in pts)
            ly = min(y for _, y in pts)
            out.append(
                f'<g fill="{HULL}" stroke="none">{_text(lx, ly - 0.3 * font, font, h.name)}</g>'
            )
        out.append("</g>")

    if markers:
        r = 0.6 * font
        out.append(
            f'<g id="markers" fill="none" stroke="{MARK}" stroke-width="{_n(2 * stroke)}" '
            f'font-family="{FONT}">'
        )
        for mk in markers:
            mx, my = frame.pt((mk.x, mk.y))
            out.append(f'<circle cx="{_n(mx)}" cy="{_n(my)}" r="{_n(r)}"/>')
            out.append(
                f'<g fill="{MARK}" stroke="none">'
                f"{_text(mx + r * 1.1, my - r * 1.1, font, str(mk.id))}</g>"
            )
        out.append("</g>")

    # origin cross: the view's top-left corner with its board coordinates
    arm = 1.2 * font
    out.append(
        f'<g id="origin" stroke="{AXIS}" stroke-width="{_n(2 * stroke)}" fill="{TEXT}" '
        f'font-family="{FONT}">'
        f'<line x1="{_n(view.x - arm / 2)}" y1="{_n(view.y)}" '
        f'x2="{_n(view.x + arm)}" y2="{_n(view.y)}"/>'
        f'<line x1="{_n(view.x)}" y1="{_n(view.y - arm / 2)}" '
        f'x2="{_n(view.x)}" y2="{_n(view.y + arm)}"/>'
        f'<g stroke="none">'
        + _text(
            view.x + 0.3 * font,
            view.y + 1.1 * font,
            0.8 * font,
            f"({_n(round(frame.board_x(view.x), 2))}, {_n(round(view.y, 2))})",
        )
        + "</g></g>"
    )

    base = view.y + view.h + pad + 1.6 * font
    legend = f'<g id="legend" fill="{TEXT}" font-family="{FONT}">'
    items: list[str] = []
    if scale_bar_on:
        step, count = scale_bar(view.w)
        x0, bar = view.x, step * count
        tick = 0.4 * font
        items.append(
            f'<g id="scale-bar" stroke="{TEXT}" stroke-width="{_n(2 * stroke)}">'
            f'<line x1="{_n(x0)}" y1="{_n(base)}" x2="{_n(x0 + bar)}" y2="{_n(base)}"/>'
            + "".join(
                f'<line x1="{_n(x0 + i * step)}" y1="{_n(base - tick)}" '
                f'x2="{_n(x0 + i * step)}" y2="{_n(base + tick)}"/>'
                for i in range(count + 1)
            )
            + "</g>"
        )
        items.append(_text(x0 + bar + 0.5 * font, base + 0.35 * font, font, f"{_n(bar)} mm"))
    if board_size is not None:
        bw, bh = board_size
        items.append(
            _text(
                view.x + view.w,
                base + 0.35 * font,
                font,
                f"{_n(round(bw, 2))} \N{MULTIPLICATION SIGN} {_n(round(bh, 2))} mm",
                "end",
            )
        )
    if highlight_nets:
        items.append(
            _text(view.x, base + 1.5 * font, 0.8 * font, "highlight: " + ", ".join(highlight_nets))
        )
    if items:
        out.append(legend + "".join(items) + "</g>")
    out.append("</svg>")
    return "".join(out)
