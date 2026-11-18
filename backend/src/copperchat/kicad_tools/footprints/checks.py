"""fp_check rules (FP-C4, spec §8): a pure function over `FootprintGeometry`.

No KiCad calls here; the geometry comes from `geom` (plain Python). Units mm, y down.

Conventions:
- A *copper pad* is a pad that is not NPTH and has a copper layer (`*.Cu` = all).
- *Bottom-only* means every copper pad is on B.Cu only; then the courtyard, fab and
  silk rules use the B.* layers. Mask/paste rules are side-aware per pad: an SMD pad on
  B.Cu needs B.Mask / B.Paste.
- Courtyard rules (enclosure, margin) consider every pad, also NPTH holes.
- `pad_gap_small` compares copper pads that share a copper layer, unless both carry the
  same non-empty number (an EP split or a multi-pad pin); unnumbered copper pads count
  as different from every other pad.
- Threshold comparisons allow 1 nm of float noise (`EPS`).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from copperchat.kicad_tools.footprints import geom
from copperchat.kicad_tools.footprints.geom import Box, Point, Polygon, Seg
from copperchat.kicad_tools.footprints.model import (
    APPROVAL_FIELD,
    SOURCE_FIELD,
    FootprintGeometry,
    Pad,
    Shape,
    Text,
    geometry_hash,
    review_state,
)

Severity = Literal["error", "warning", "info"]

EPS = 1e-6
PAD_MIN = 0.15
PAD_GAP_WARN = 0.15
PAD_GAP_ERROR = 0.1
SILK_CLEARANCE = 0.1
DRILL_ERROR = 0.2
DRILL_WARN = 0.3
ANNULAR_MIN = 0.15
MARGIN = 0.25
MARGIN_SMALL = 0.1
SMALL_EXTENT = 1.0  # 0402 and smaller
PIN1_REACH = 1.0  # mark within max(pad1.size) + this
PIN1_EDGE_KINDS = ("polygon", "polygon_hole", "rect")  # closed outlines: edges count alone
PIN1_AXIS_TOL_DEG = 1.0  # an edge within this of the x or y axis is not diagonal
TEXT_CHAR_WIDTH = 0.8  # text box: len(value) * 0.8 * size_x by size_y
MAX_PAIR_PADS = 2000
MAX_PAIR_SHAPES = 5000  # all shapes plus the silk texts
# a pad with more copper pieces (custom pad primitives, arcs as chords) or more copper
# vertices is left out of the pair rules, measured by its bounding box in the pin-1 rule
# and named in `too_complex_partial_check`: `geom.distance` is O(Va * Vb) per piece pair
MAX_PAD_PARTS = 256
MAX_PAD_VERTICES = 512
# One work budget per run, shared by every loop whose cost is a product of two input
# sizes: closed-outline building, expected-pad matching, courtyard containment (points x
# courtyard vertices), courtyard margin, the pair sweeps and distances, the pin-1 mark
# distances. A unit is about one point-to-segment distance (~0.7 us in CPython). Each
# such loop charges its work (`_Run.afford`) before it runs; a rule that cannot afford
# its next step stops there and is named in the one `too_complex_partial_check` info.
WORK_BUDGET = 1_500_000
PAIR_RULES = ("pad_gap_small", "silk_on_pad")
COURTYARD_RULES = ("courtyard_not_enclosing", "courtyard_margin", "silk_outside_courtyard")
# why a rule is partial, in message order
_TOO_MANY, _COMPLEX, _BBOX, _BUDGET = "too_many_items", "complex_pads", "bounding_box", "budget"
PER_RULE_CAP = 20
TOTAL_CAP = 200
DEFAULT_TOLERANCE = 0.01

_RANK = {"error": 0, "warning": 1, "info": 2}
_HASH8_RE = re.compile(r"geometry\s+([0-9a-fA-F]{8})\b")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Severity
    message: str
    pads: tuple[str, ...] = ()
    at: Point | None = None
    measured: Any = None
    expected: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"rule": self.rule, "severity": self.severity, "message": self.message}
        if self.pads:
            d["pads"] = list(self.pads)
        if self.at is not None:
            d["at"] = _jsonable(self.at)
        if self.measured is not None:
            d["measured"] = _jsonable(self.measured)
        if self.expected is not None:
            d["expected"] = _jsonable(self.expected)
        return d


@dataclass(frozen=True)
class ExpectedPad:
    number: str
    at: Point
    size: Point
    drill: float | None = None


@dataclass(frozen=True)
class Expected:
    pads: tuple[ExpectedPad, ...] = ()
    pitch: float | None = None
    courtyard: Point | None = None
    body: Point | None = None
    tolerance_mm: float = DEFAULT_TOLERANCE


@dataclass(frozen=True)
class CheckInput:
    geometry: FootprintGeometry
    pins: list[str] | None = None  # symbol pin numbers
    polarized: bool = False
    expected: Expected | None = None
    allow_unmatched: frozenset[str] = frozenset()
    local: bool = False
    pending: bool = False


@dataclass(frozen=True)
class CheckReport:
    passed: bool
    counts: dict[str, int]
    findings: tuple[Finding, ...]
    geometry_hash: str


# --- small helpers ------------------------------------------------------------------


def _jsonable(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, tuple | list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def _mm(v: float) -> str:
    return f"{v:.3f} mm"


def _xy(p: Sequence[float]) -> str:
    return f"({p[0]:.3f}, {p[1]:.3f})"


def natural_key(number: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key for pad numbers: `1 < 2 < 10 < A1 < A2`."""
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", number)
        if part
    )


def _cu(pad: Pad) -> frozenset[str]:
    return frozenset(name for name in pad.layers if name.endswith(".Cu"))


def is_copper(pad: Pad) -> bool:
    return pad.type != "npth" and bool(_cu(pad))


def _sides(pad: Pad) -> set[str]:
    cu = _cu(pad)
    every = "*.Cu" in cu
    return {s for s in ("F", "B") if every or f"{s}.Cu" in cu}


def _has_layer(pad: Pad, name: str) -> bool:
    return name in pad.layers or "*." + name.split(".", 1)[1] in pad.layers


def _share_copper(a: Pad, b: Pad) -> bool:
    ca, cb = _cu(a), _cu(b)
    return bool(ca and cb and ("*.Cu" in ca or "*.Cu" in cb or ca & cb))


def _box(points: Iterable[Point]) -> Box:
    xs, ys = zip(*points, strict=True)
    return (min(xs), min(ys), max(xs), max(ys))


def _union(boxes: Iterable[Box]) -> Box | None:
    bs = list(boxes)
    if not bs:
        return None
    return (
        min(b[0] for b in bs),
        min(b[1] for b in bs),
        max(b[2] for b in bs),
        max(b[3] for b in bs),
    )


def _extent(b: Box) -> float:
    return max(b[2] - b[0], b[3] - b[1])


def _box_gap(a: Box, b: Box) -> float:
    dx = max(0.0, b[0] - a[2], a[0] - b[2])
    dy = max(0.0, b[1] - a[3], a[1] - b[3])
    return math.hypot(dx, dy)


def _geom_box(g: geom.Geometry) -> Box:
    if isinstance(g, Seg):
        h = g.half_width
        return (
            min(g.a[0], g.b[0]) - h,
            min(g.a[1], g.b[1]) - h,
            max(g.a[0], g.b[0]) + h,
            max(g.a[1], g.b[1]) + h,
        )
    if geom.is_circle(g):
        (cx, cy), r = g[1], g[2]
        return (cx - r, cy - r, cx + r, cy + r)
    return _box(g)


def _points(g: geom.Geometry) -> list[Point]:
    """Points to test for containment: polygon vertices, circle chords, and for a
    stroked segment its ends, plus with a stroke 8 points around each end at the half
    width (the stadium's extremes along both board axes and the diagonals)."""
    if isinstance(g, Seg):
        h = g.half_width
        if h <= 0.0:
            return [g.a, g.b]
        d = h * math.sqrt(0.5)
        ring = ((h, 0.0), (d, d), (0.0, h), (-d, d), (-h, 0.0), (-d, -d), (0.0, -h), (d, -d))
        return [(x + ox, y + oy) for x, y in (g.a, g.b) for ox, oy in ring]
    if geom.is_circle(g):
        (cx, cy), r = g[1], g[2]
        return [
            (cx + r * math.cos(i * math.pi / 8), cy + r * math.sin(i * math.pi / 8))
            for i in range(16)
        ]
    return list(g)


def _min_dist(
    xs: Sequence[geom.Geometry],
    ys: Sequence[geom.Geometry],
    below: float = math.inf,
    bx: Sequence[Box] | None = None,
    by: Sequence[Box] | None = None,
) -> float:
    """Shortest distance between two groups of pieces, exact when it is below `below`
    (else some value >= `below`). Lazy: piece pairs are visited in order with a running
    best and skipped when their bounding-box gap (a lower bound of their distance)
    already reaches it; nothing is materialised or sorted."""
    bx = bx if bx is not None else [_geom_box(x) for x in xs]
    by = by if by is not None else [_geom_box(y) for y in ys]
    best = below
    for i, a in enumerate(bx):
        for j, b in enumerate(by):
            if _box_gap(a, b) >= best:
                continue
            best = min(best, geom.distance(xs[i], ys[j]))
            if best <= 0.0:
                return 0.0
    return best


def _vertices(pieces: Iterable[geom.Geometry]) -> int:
    """Vertex count of pieces (a segment 2, a circle 1): the `geom.distance` cost unit."""
    total = 0
    for x in pieces:
        total += 2 if isinstance(x, Seg) else 1 if geom.is_circle(x) else len(x)
    return total


def _dist_cost(va: int, vb: int) -> int:
    """Work units of `geom.distance` / `_min_dist` between pieces of `va` and `vb`
    vertices: up to 4 point-segment distances per edge pair, plus the containment tests
    and the piece-box gaps."""
    return 4 * va * vb + va + vb


def _box_polygon(b: Box) -> Polygon:
    return [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]


def _diagonal(seg: Seg) -> bool:
    """True when the segment is not parallel to x or y within `PIN1_AXIS_TOL_DEG`."""
    dx, dy = seg.b[0] - seg.a[0], seg.b[1] - seg.a[1]
    if dx == 0.0 and dy == 0.0:
        return False
    angle = math.degrees(math.atan2(abs(dy), abs(dx)))  # 0..90
    return PIN1_AXIS_TOL_DEG < angle < 90.0 - PIN1_AXIS_TOL_DEG


def _inside(poly: Polygon) -> tuple[Callable[[Point], bool], int]:
    """Containment test for `poly` (boundary = inside) and its work units per point; a
    bounds check for an axis-aligned rectangle, the usual courtyard."""
    if len(poly) == 4:
        (ax, ay), (bx, by), (cx, cy), (dx, dy) = poly
        if (ay == by and bx == cx and cy == dy and dx == ax) or (
            ax == bx and by == cy and cx == dx and dy == ay
        ):
            x0, y0, x1, y1 = _box(poly)
            return (
                lambda q: x0 - 1e-12 <= q[0] <= x1 + 1e-12 and y0 - 1e-12 <= q[1] <= y1 + 1e-12
            ), 1
    return (lambda q: geom.point_in_polygon(q, poly)), len(poly)


def text_box(t: Text) -> list[Point]:
    """A text as a box `len(value) * 0.8 * size_x` by `size_y`, centred at `at`, turned
    by the text rotation (KiCad sense, as pads)."""
    hw = len(t.value) * TEXT_CHAR_WIDTH * t.size[0] / 2
    hh = t.size[1] / 2
    a = math.radians(t.rotation)
    c, s = math.cos(a), math.sin(a)
    cx, cy = t.at
    return [
        (cx + x * c + y * s, cy - x * s + y * c)
        for x, y in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    ]


def _shape_geoms(shape: Shape) -> list[geom.Geometry]:
    out: list[geom.Geometry] = list(geom.stroke_segments(shape))
    if shape.filled and shape.kind in ("polygon", "rect") and len(shape.points) >= 2:
        if shape.kind == "rect":
            (x0, y0), (x1, y1) = shape.points[0], shape.points[1]
            out.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
        elif len(shape.points) >= 3:
            out.append(list(shape.points))
    return out


def pitch_pad_pair(g: FootprintGeometry) -> tuple[Pad, Pad] | None:
    """Pad "1" and the first pad with the next number in natural order (the pitch both
    `fp_check` and `fp_render` use); None without a pad "1" or a second number."""
    numbers = sorted({p.number for p in g.pads if p.number}, key=natural_key)
    if "1" not in numbers:
        return None
    i = numbers.index("1")
    if i + 1 >= len(numbers):
        return None
    first = next(p for p in g.pads if p.number == "1")
    second = next(p for p in g.pads if p.number == numbers[i + 1])
    return first, second


# --- the checks ---------------------------------------------------------------------


class _OutOfBudget(Exception):
    """Raised inside a rule when `_Run.afford` refuses its next step."""


@dataclass
class _Run:
    inp: CheckInput
    findings: list[Finding] = field(default_factory=list)
    work: int = 0  # units spent of WORK_BUDGET
    partial: dict[str, list[str]] = field(default_factory=dict)  # reason -> rules

    def __post_init__(self) -> None:
        g = self.inp.geometry
        self.g = g
        self._geoms: dict[int, list[geom.Geometry]] = {}
        self.parts = [geom.pad_parts(p) for p in g.pads]
        self.part_boxes = [[_geom_box(x) for x in parts] for parts in self.parts]
        self.boxes = [_union(b) or (0, 0, 0, 0) for b in self.part_boxes]
        self.copper = [i for i, p in enumerate(g.pads) if is_copper(p)]
        self.vertices = [_vertices(parts) for parts in self.parts]
        self.complex_pads = {
            i
            for i in self.copper
            if len(self.parts[i]) > MAX_PAD_PARTS or self.vertices[i] > MAX_PAD_VERTICES
        }
        bottom = bool(self.copper) and all(_sides(g.pads[i]) == {"B"} for i in self.copper)
        self.side = "B" if bottom else "F"
        self.fab = [s for s in g.shapes if s.layer == f"{self.side}.Fab"]
        self.fab_segs = [sg for s in self.fab for sg in geom.stroke_segments(s)]
        self.silk = [s for s in g.shapes if s.layer == f"{self.side}.SilkS"]
        silk_texts = sum(1 for t in g.texts if t.layer in ("F.SilkS", "B.SilkS"))
        self.pair_shapes = len(g.shapes) + silk_texts
        crtyd = [s for s in g.shapes if s.layer == f"{self.side}.CrtYd"]
        self.crtyd_shapes = crtyd
        afford = self.charger("courtyard_missing", *COURTYARD_RULES)
        self.outline = geom.closed_outline(crtyd, afford) if crtyd else None
        self.outline_unknown = self.outline is None and _BUDGET in self.partial

    # -- the shared work budget ----------------------------------------------------------

    def afford(self, cost: int, *rules: str) -> bool:
        """Charge `cost` units of `WORK_BUDGET`; False (and `rules` noted as partial)
        when they do not fit, then the caller skips the rest of its rule."""
        if self.work + cost > WORK_BUDGET:
            self.note(_BUDGET, *rules)
            return False
        self.work += cost
        return True

    def charger(self, *rules: str) -> Callable[[int], bool]:
        return lambda cost: self.afford(cost, *rules)

    def note(self, reason: str, *rules: str) -> None:
        got = self.partial.setdefault(reason, [])
        got += [r for r in rules if r not in got]

    def finish(self) -> None:
        """The single `too_complex_partial_check` info: every rule that was skipped or
        only partly run, grouped by reason."""
        if not self.partial:
            return
        g = self.g
        cx = sorted(self.complex_pads)
        names = [g.pads[i].number or "(unnumbered)" for i in cx]
        shown = ", ".join(names[:10]) + (", ..." if len(names) > 10 else "")
        why = {
            _TOO_MANY: f"skipped: {len(g.pads)} pads / {self.pair_shapes} shapes and silk "
            f"texts exceed {MAX_PAIR_PADS} / {MAX_PAIR_SHAPES}",
            _COMPLEX: f"skipped for pad(s) {shown} (more than {MAX_PAD_PARTS} copper pieces "
            f"or {MAX_PAD_VERTICES} vertices)",
            _BBOX: f"measured pad(s) {shown} by their bounding box",
            _BUDGET: f"stopped early: the work budget of {WORK_BUDGET} units is used up",
        }
        measured: dict[str, Any] = {"skipped": {r: list(v) for r, v in self.partial.items()}}
        expected: dict[str, Any] = {}
        if _TOO_MANY in self.partial:
            measured |= {"pads": len(g.pads), "shapes": self.pair_shapes}
            expected |= {"pads": MAX_PAIR_PADS, "shapes": MAX_PAIR_SHAPES}
        if _COMPLEX in self.partial or _BBOX in self.partial:
            measured |= {
                "pieces": max(len(self.parts[i]) for i in cx),
                "vertices": max(self.vertices[i] for i in cx),
            }
            expected |= {"pieces": MAX_PAD_PARTS, "vertices": MAX_PAD_VERTICES}
        if _BUDGET in self.partial:
            measured["work"] = self.work
            expected["work"] = WORK_BUDGET
        parts = [
            f"{', '.join(self.partial[r])} {why[r]}"
            for r in (_TOO_MANY, _COMPLEX, _BBOX, _BUDGET)
            if r in self.partial
        ]
        complex_named = _COMPLEX in self.partial or _BBOX in self.partial
        self.add(
            "too_complex_partial_check",
            "info",
            "partial check: " + "; ".join(parts),
            pads=[g.pads[i].number for i in cx if g.pads[i].number] if complex_named else (),
            measured=measured,
            expected=expected,
        )

    def geoms(self, shape: Shape) -> list[geom.Geometry]:
        key = id(shape)
        if key not in self._geoms:
            self._geoms[key] = _shape_geoms(shape)
        return self._geoms[key]

    def add(
        self,
        rule: str,
        severity: Severity,
        message: str,
        *,
        pads: Iterable[str] = (),
        at: Point | None = None,
        measured: Any = None,
        expected: Any = None,
    ) -> None:
        self.findings.append(Finding(rule, severity, message, tuple(pads), at, measured, expected))

    # -- pins and numbers --------------------------------------------------------------

    def pins(self) -> None:
        pads = self.g.pads
        if self.inp.pins is not None:
            pins = list(dict.fromkeys(self.inp.pins))
            numbers = {p.number for p in pads if p.number}
            for pin in pins:
                if pin not in numbers:
                    self.add("pin_without_pad", "error", f"symbol pin {pin} has no pad", pads=[pin])
            have = set(pins)
            copper = {pads[i].number for i in self.copper if pads[i].number}
            for num in sorted(copper - have, key=natural_key):
                allowed = num in self.inp.allow_unmatched
                self.add(
                    "pad_without_pin",
                    "info" if allowed else "warning",
                    f"pad {num} has no symbol pin" + (" (allowed)" if allowed else ""),
                    pads=[num],
                )
            k = sum(1 for p in pads if not p.number)
            self.add(
                "pad_count_summary",
                "info",
                f"{len(pins)} pins / {len(pads)} pads / {k} unnumbered",
                measured={"pins": len(pins), "pads": len(pads), "unnumbered": k},
            )
        counts = Counter(p.number for p in pads if p.number)
        for num in sorted(counts, key=natural_key):
            if counts[num] > 1:
                self.add(
                    "duplicate_pad_number",
                    "info",
                    f"{counts[num]} pads share number {num}",
                    pads=[num],
                    measured=counts[num],
                )

    # -- expected land pattern ---------------------------------------------------------

    def expected(self) -> None:
        exp = self.inp.expected
        if exp is None:
            return
        tol = exp.tolerance_mm

        def off(a: Sequence[float] | None, b: Sequence[float]) -> bool:
            return a is None or any(abs(x - y) > tol + EPS for x, y in zip(a, b, strict=True))

        def mismatch(what: str, measured: Any, want: Any, pads: Iterable[str] = ()) -> None:
            shown = "none" if measured is None else _fmt(measured)
            self.add(
                "expected_mismatch",
                "error",
                f"{what} is {shown}, expected {_fmt(want)} (tolerance {_mm(tol)})",
                pads=pads,
                measured=measured,
                expected=want,
            )

        by_num: dict[str, list[Pad]] = {}
        for p in self.g.pads:
            if p.number:
                by_num.setdefault(p.number, []).append(p)
        for ep in exp.pads:
            cands = by_num.get(ep.number)
            if not self.afford(len(cands or ()) + 1, "expected_mismatch", "expected_missing_pad"):
                break
            if not cands:
                self.add(
                    "expected_missing_pad",
                    "error",
                    f"expected pad {ep.number} is missing",
                    pads=[ep.number],
                    expected=ep.at,
                )
                continue
            pad = min(cands, key=lambda p: math.dist(p.at, ep.at))
            n = ep.number
            if off(pad.at, ep.at):
                mismatch(f"pad {n} position", pad.at, ep.at, [n])
            size = geom.board_size(pad)
            if off(size, ep.size):
                mismatch(f"pad {n} size", size, ep.size, [n])
            if ep.drill is not None:
                drill = min(pad.drill.size) if pad.drill else None
                if drill is None or abs(drill - ep.drill) > tol + EPS:
                    mismatch(f"pad {n} drill", drill, ep.drill, [n])
        if exp.pads:
            want = {ep.number for ep in exp.pads}
            for num in sorted(set(by_num) - want, key=natural_key):
                self.add(
                    "expected_missing_pad",
                    "error",
                    f"pad {num} is not in the expected land pattern",
                    pads=[num],
                )
        if exp.pitch is not None:
            pair = pitch_pad_pair(self.g)
            measured = math.dist(pair[0].at, pair[1].at) if pair else None
            if measured is None or abs(measured - exp.pitch) > tol + EPS:
                names = [pair[0].number, pair[1].number] if pair else []
                mismatch("pitch", measured, exp.pitch, names)
        if exp.courtyard is not None and self.outline_unknown:
            self.note(_BUDGET, "expected_mismatch")
        elif exp.courtyard is not None:
            b = _box(self.outline) if self.outline else None
            cy = (b[2] - b[0], b[3] - b[1]) if b else None
            if off(cy, exp.courtyard):
                mismatch("courtyard size", cy, exp.courtyard)
        if exp.body is not None:
            fb = _union(_geom_box(Seg(s.a, s.b)) for s in self.fab_segs)
            body = (fb[2] - fb[0], fb[3] - fb[1]) if fb else None
            if off(body, exp.body):
                mismatch("body size", body, exp.body)

    # -- courtyard, fab, silk outside ----------------------------------------------------

    def courtyard(self) -> None:
        layer = f"{self.side}.CrtYd"
        if not self.fab:
            self.add("fab_missing", "warning", f"no {self.side}.Fab outline")
        poly = self.outline
        if poly is None:
            if self.outline_unknown:  # building it ran out of budget: noted there
                return
            why = "is not one closed outline" if self.crtyd_shapes else "is missing"
            self.add("courtyard_missing", "error", f"{layer} {why}")
            return
        rule = "courtyard_not_enclosing"
        enclosing, complete = True, True
        inside, cost = _inside(poly)
        for i, pad in enumerate(self.g.pads):
            pts = [q for part in self.parts[i] for q in _points(part)]
            if not self.afford(len(pts) * cost, rule):
                complete = False
                break
            if not all(inside(q) for q in pts):
                enclosing = False
                self.add(
                    rule,
                    "error",
                    f"pad {pad.number or '(unnumbered)'} extends outside the courtyard",
                    pads=[pad.number] if pad.number else (),
                    at=pad.at,
                )
        if complete and self.afford(2 * len(self.fab_segs) * cost, rule):
            outside = [q for s in self.fab_segs for q in (s.a, s.b) if not inside(q)]
            if outside:
                enclosing = False
                self.add(
                    rule,
                    "error",
                    f"the body ({self.side}.Fab) extends outside the courtyard at "
                    f"{_xy(outside[0])}",
                    at=outside[0],
                )
        else:
            complete = False
        if not complete:  # the margin needs to know the outline encloses everything
            self.note(_BUDGET, "courtyard_margin")
        elif enclosing:
            self._margin(poly)
        self._silk_outside(inside, cost)

    def _margin(self, poly: Polygon) -> None:
        copper = _union(self.boxes[i] for i in self.copper)
        body = _union(_geom_box(Seg(s.a, s.b)) for s in self.fab_segs)
        small = any(b is not None and _extent(b) <= SMALL_EXTENT + EPS for b in (copper, body))
        limit = MARGIN_SMALL if small else MARGIN
        edges = [Seg(poly[k], poly[(k + 1) % len(poly)]) for k in range(len(poly))]
        items: list[tuple[int, geom.Geometry]] = [
            (i, part) for i in range(len(self.g.pads)) for part in self.parts[i]
        ] + [(-1, s) for s in self.fab_segs]
        edge_boxes = [_geom_box(e) for e in edges]
        item_boxes = [_geom_box(g) for _, g in items]
        afford = self.charger("courtyard_margin")
        best, near_k = math.inf, -1
        for e, k in geom.cross_pairs(edge_boxes, item_boxes, limit, afford):
            if not afford(_dist_cost(2, _vertices([items[k][1]]))):
                break  # a gap found so far is still real: reported below
            d = geom.distance(edges[e], items[k][1])
            if d < best or (d == best and k < near_k):  # ties: the first item
                best, near_k = d, k
        near = items[near_k][0] if near_k >= 0 else -1
        if best < limit - EPS:
            pad = self.g.pads[near] if near >= 0 else None
            what = f"pad {pad.number}" if pad and pad.number else "pad" if pad else "body"
            self.add(
                "courtyard_margin",
                "warning",
                f"courtyard gap to {what} is {_mm(best)}, below {_mm(limit)}",
                pads=[pad.number] if pad and pad.number else (),
                measured=best,
                expected=limit,
            )

    def _silk_outside(self, inside: Callable[[Point], bool], cost: int) -> None:
        items: list[tuple[str, list[Point]]] = [
            (s.kind, [q for x in self.geoms(s) if isinstance(x, Seg) for q in (x.a, x.b)])
            for s in self.silk
        ]
        items += [
            (f"text {t.value!r}", text_box(t))
            for t in self.g.texts
            if t.layer == f"{self.side}.SilkS" and t.kind not in ("reference", "value")
        ]
        for what, pts in items:
            if not self.afford(len(pts) * cost, "silk_outside_courtyard"):
                return
            out = next((q for q in pts if not inside(q)), None)
            if out is not None:
                self.add(
                    "silk_outside_courtyard",
                    "warning",
                    f"silk {what} lies outside the courtyard at {_xy(out)}",
                    at=out,
                )

    # -- pair checks (sweep) -------------------------------------------------------------

    def pairs(self) -> None:
        g = self.g
        if len(g.pads) > MAX_PAIR_PADS or self.pair_shapes > MAX_PAIR_SHAPES:
            self.note(_TOO_MANY, *PAIR_RULES)
            return
        if self.complex_pads:
            self.note(_COMPLEX, *PAIR_RULES)
        # complex pads stay out; silk farther than the clearance from all copper too
        pads = [i for i in self.copper if i not in self.complex_pads]
        raw: list[tuple[str, list[geom.Geometry]]] = [
            (s.layer[0], self.geoms(s)) for s in g.shapes if s.layer in ("F.SilkS", "B.SilkS")
        ]
        raw += [(t.layer[0], [text_box(t)]) for t in g.texts if t.layer in ("F.SilkS", "B.SilkS")]
        reach = _union(self.boxes[i] for i in pads)
        silk: list[tuple[str, list[geom.Geometry], list[Box], Box, int]] = []
        for side, pieces in raw:
            pboxes = [_geom_box(x) for x in pieces]
            box = _union(pboxes)
            if box and reach and _box_gap(box, reach) <= SILK_CLEARANCE:
                silk.append((side, pieces, pboxes, box, _vertices(pieces)))
        pad_boxes = [self.boxes[i] for i in pads]

        gaps: list[tuple[int, int, float]] = []
        afford = self.charger("pad_gap_small")
        for a, b in geom.candidate_pairs(pad_boxes, PAD_GAP_WARN, afford):
            i, j = pads[a], pads[b]
            pi, pj = g.pads[i], g.pads[j]
            if (pi.number and pi.number == pj.number) or not _share_copper(pi, pj):
                continue
            if not afford(_dist_cost(self.vertices[i], self.vertices[j])):
                break
            d = _min_dist(
                self.parts[i], self.parts[j], PAD_GAP_WARN, self.part_boxes[i], self.part_boxes[j]
            )
            if d < PAD_GAP_WARN - EPS:
                gaps.append((min(i, j), max(i, j), d))

        silk_min: dict[int, float] = {}
        afford = self.charger("silk_on_pad")
        silk_boxes = [s[3] for s in silk]
        for a, b in geom.cross_pairs(pad_boxes, silk_boxes, SILK_CLEARANCE, afford):
            i = pads[a]
            side, pieces, pboxes, _, verts = silk[b]
            if side not in _sides(g.pads[i]):
                continue
            if not afford(_dist_cost(verts, self.vertices[i])):
                break
            d = _min_dist(pieces, self.parts[i], SILK_CLEARANCE, pboxes, self.part_boxes[i])
            if d < SILK_CLEARANCE - EPS and d < silk_min.get(i, math.inf):
                silk_min[i] = d

        for i, j, d in sorted(gaps):
            pi, pj = g.pads[i], g.pads[j]
            error = d < PAD_GAP_ERROR - EPS
            limit = PAD_GAP_ERROR if error else PAD_GAP_WARN
            self.add(
                "pad_gap_small",
                "error" if error else "warning",
                f"gap between pads {pi.number or '?'} and {pj.number or '?'} is {_mm(d)}, "
                f"below {_mm(limit)}",
                pads=[x for x in (pi.number, pj.number) if x],
                at=pi.at,
                measured=d,
                expected=limit,
            )
        for i in sorted(silk_min):
            pad = g.pads[i]
            self.add(
                "silk_on_pad",
                "warning",
                f"silk is {_mm(silk_min[i])} from pad {pad.number or '?'}, "
                f"below {_mm(SILK_CLEARANCE)}",
                pads=[pad.number] if pad.number else (),
                at=pad.at,
                measured=silk_min[i],
                expected=SILK_CLEARANCE,
            )

    # -- pin 1 mark ----------------------------------------------------------------------

    def pin1(self) -> None:
        """A silk/fab piece marks pin 1 when it lies within `max(pad1.size) + 1` mm of
        pad 1 and strictly nearer to pad 1 than to any other pad. Pieces are whole
        shapes, plus the single diagonal edges of polygons and rects: a pin-1 chamfer
        drawn into one closed body outline is as near to the other corner pads as a
        whole, but its chamfer edge alone is not. A too-complex pad (see `pairs`) is
        measured by its bounding box; every distance is charged to the work budget, and
        when that runs out the rule ends without a verdict."""
        rule = "pin1_marker_missing"
        g = self.g
        n = len(self.copper)
        if not (n > 2 or (n == 2 and self.inp.polarized)):
            return
        ones = [i for i in self.copper if g.pads[i].number == "1"]
        p1_box = _union(self.boxes[i] for i in ones)
        if p1_box is None:  # no copper pad "1"
            return
        others = [i for i in self.copper if g.pads[i].number != "1"]

        def parts(i: int) -> list[geom.Geometry]:
            if i in self.complex_pads:
                self.note(_BBOX, rule)
                return [_box_polygon(self.boxes[i])]
            return self.parts[i]

        def verts(i: int) -> int:
            return 4 if i in self.complex_pads else self.vertices[i]

        p1_parts: list[geom.Geometry] = []  # built on first use (may note _BBOX)
        p1_verts = sum(verts(i) for i in ones)
        reach = max(max(g.pads[i].size) for i in ones) + PIN1_REACH

        def charge(cost: int) -> None:
            if not self.afford(cost, rule):
                raise _OutOfBudget

        def marks(pieces: Sequence[geom.Geometry]) -> bool:
            sbox = _union(_geom_box(x) for x in pieces)
            if sbox is None or _box_gap(sbox, p1_box) > reach + EPS:
                return False
            sv = _vertices(pieces)
            charge(_dist_cost(sv, p1_verts) + len(others))
            if not p1_parts:
                p1_parts.extend(part for i in ones for part in parts(i))
            d1 = _min_dist(pieces, p1_parts)
            if d1 > reach + EPS:
                return False
            for j in others:
                if _box_gap(sbox, self.boxes[j]) > d1 + EPS:
                    continue
                charge(_dist_cost(sv, verts(j)))
                if _min_dist(pieces, parts(j), d1 + EPS) <= d1 + EPS:
                    return False
            return True

        shapes = [*self.silk, *self.fab]
        try:
            if any(marks(self.geoms(s)) for s in shapes):
                return
            for s in shapes:
                if s.kind not in PIN1_EDGE_KINDS:
                    continue
                if any(_diagonal(e) and marks([e]) for e in geom.stroke_segments(s)):
                    return
        except _OutOfBudget:
            return  # no verdict: noted by `afford`
        pad = g.pads[ones[0]]
        self.add(
            rule,
            "warning",
            f"no silk or fab mark next to pad 1 (within {_mm(reach)})",
            pads=["1"],
            at=pad.at,
        )

    # -- pads: layers, mounting, sizes, drills -------------------------------------------

    def pads(self) -> None:
        g = self.g
        for i in self.copper:
            pad = g.pads[i]
            name = pad.number or "(unnumbered)"
            num = [pad.number] if pad.number else []
            if pad.type == "smd":
                rules: tuple[tuple[str, str, Severity], ...] = (
                    ("Mask", "mask_missing", "error"),
                    ("Paste", "paste_missing", "warning"),
                )
                for kind, rule, sev in rules:
                    want = [f"{s}.{kind}" for s in sorted(_sides(pad), reverse=True)]
                    lacking = [x for x in want if not _has_layer(pad, x)]
                    if lacking:
                        msg = f"SMD pad {name} has no {', '.join(lacking)}"
                        self.add(rule, sev, msg, pads=num, at=pad.at)
            elif pad.type == "tht" and any(_has_layer(pad, f"{s}.Paste") for s in "FB"):
                msg = f"THT pad {name} has paste"
                self.add("paste_on_tht", "warning", msg, pads=num, at=pad.at)
            if pad.shape != "custom" and min(pad.size) < PAD_MIN - EPS:
                self.add(
                    "pad_too_small",
                    "warning",
                    f"pad {name} is {_mm(min(pad.size))} wide, below {_mm(PAD_MIN)}",
                    pads=num,
                    at=pad.at,
                    measured=min(pad.size),
                    expected=PAD_MIN,
                )
            if pad.type == "tht" and pad.drill and pad.shape != "custom":
                (w, h), (dx, dy) = pad.size, pad.drill.size
                ring = min(w - dx, h - dy) / 2
                if ring < ANNULAR_MIN - EPS:
                    self.add(
                        "annular_ring_small",
                        "warning",
                        f"annular ring of pad {name} is {_mm(ring)}, below {_mm(ANNULAR_MIN)}",
                        pads=num,
                        at=pad.at,
                        measured=ring,
                        expected=ANNULAR_MIN,
                    )
        for pad in g.pads:
            if pad.drill is None:
                continue
            d = min(pad.drill.size)
            if d < DRILL_WARN - EPS:
                error = d < DRILL_ERROR - EPS
                limit = DRILL_ERROR if error else DRILL_WARN
                self.add(
                    "drill_small",
                    "error" if error else "warning",
                    f"drill of pad {pad.number or '(unnumbered)'} is {_mm(d)}, below {_mm(limit)}",
                    pads=[pad.number] if pad.number else (),
                    at=pad.at,
                    measured=d,
                    expected=limit,
                )
        types = Counter(g.pads[i].type for i in self.copper)
        if g.mounting == "smd" and types["tht"] and not types["smd"]:
            self.add("mounting_mismatch", "error", "mounting is SMD but all pads are THT")
        elif g.mounting == "tht" and types["smd"] and not types["tht"]:
            self.add("mounting_mismatch", "error", "mounting is THT but all pads are SMD")

    # -- fields --------------------------------------------------------------------------

    def fields(self, digest: str) -> None:
        g, inp = self.g, self.inp
        if inp.local and not g.fields.get(SOURCE_FIELD, "").strip():
            self.add("source_missing", "error", f"local footprint has no '{SOURCE_FIELD}' field")
        if inp.local:
            state = review_state(g, pending=inp.pending)
            if state != "approved":
                self.add(
                    "needs_user_review",
                    "info",
                    f"not approved by the user yet ({state})",
                    measured=state,
                )
        review = g.fields.get(APPROVAL_FIELD)
        if review is not None and review.strip():
            m = _HASH8_RE.search(review)
            recorded = m.group(1).lower() if m else None
            if recorded != digest[:8]:
                self.add(
                    "approved_geometry_changed",
                    "warning",
                    f"approved geometry {recorded or '(unreadable)'} differs from the "
                    f"current {digest[:8]}",
                    measured=recorded,
                    expected=digest[:8],
                )


def _fmt(v: Any) -> str:
    if isinstance(v, tuple | list):
        return "(" + ", ".join(f"{x:.3f}" for x in v) + ")"
    return _mm(float(v))


def run_checks(inp: CheckInput) -> CheckReport:
    """Every spec §8 rule on `inp.geometry` (pure; no KiCad)."""
    digest = geometry_hash(inp.geometry)
    run = _Run(inp)
    run.pins()
    run.expected()
    run.courtyard()
    run.pairs()
    run.pin1()
    run.pads()
    run.fields(digest)
    run.finish()
    counts = Counter(f.severity for f in run.findings)
    exact = {s: counts.get(s, 0) for s in ("error", "warning", "info")}
    return CheckReport(exact["error"] == 0, exact, tuple(run.findings), digest)


def to_result(report: CheckReport) -> dict[str, Any]:
    """JSON result: exact counts; findings (errors first) capped at 20 per rule and 200
    overall, with `truncated` and, when truncated, `omitted: {rule: n}`."""
    kept: list[Finding] = []
    per_rule: Counter[str] = Counter()
    omitted: Counter[str] = Counter()
    for f in sorted(report.findings, key=lambda f: _RANK[f.severity]):
        if per_rule[f.rule] >= PER_RULE_CAP or len(kept) >= TOTAL_CAP:
            omitted[f.rule] += 1
            continue
        per_rule[f.rule] += 1
        kept.append(f)
    out: dict[str, Any] = {
        "passed": report.passed,
        "counts": dict(report.counts),
        "findings": [f.to_dict() for f in kept],
        "geometry_hash": report.geometry_hash,
        "truncated": bool(omitted),
    }
    if omitted:
        out["omitted"] = dict(omitted)
    return out
