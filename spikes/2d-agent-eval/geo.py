"""Scene model + orthogonal router for the 2D prototype tools (throwaway).

Pin ends, pin directions and symbol boxes come from API data (spike 2A `geom.py`):
library-local pin positions transformed by the symbol transform. The router never lets a
wire touch a foreign connection point (pin end, wire end, label anchor, junction,
no-connect) and never overlaps an existing wire collinearly — both would connect (2A Q3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from geom import MM, _in_unit, apply, sym_transform, symbol_bbox
from harness import sid
from kipy.proto.common.types import ElectricalPinType

GRID = 1.27
EPS = 0.01
EPT = {v: n.removeprefix("EPT_").lower() for n, v in ElectricalPinType.items()}
# SchematicPinOrientation: direction the pin *shape* extends from its connection point
# (towards the body); the outward direction is the opposite.
_INWARD = {1: (1, 0), 2: (-1, 0), 3: (0, -1), 4: (0, 1)}
SPIN_DIR = {1: (-1, 0), 2: (0, -1), 3: (1, 0), 4: (0, 1)}  # SLSS_LEFT/UP/RIGHT/BOTTOM: text direction
DIR_NAME = {(1, 0): "right", (-1, 0): "left", (0, -1): "up", (0, 1): "down"}


def snap(v: float) -> float:
    return round(round(v / GRID) * GRID, 4)


def snap_pt(p) -> tuple[float, float]:
    return (snap(p[0]), snap(p[1]))


def on_grid(p) -> bool:
    return all(abs(c / GRID - round(c / GRID)) < 1e-3 for c in p)


def same(a, b) -> bool:
    return abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) < EPS


@dataclass
class Pin:
    sym_uuid: str
    ref: str
    number: str
    name: str
    etype: str
    pos: tuple[float, float]
    out: tuple[int, int]  # outward unit vector (away from the body)

    @property
    def key(self) -> tuple[str, str]:
        return (self.sym_uuid, self.number)

    @property
    def label(self) -> str:
        return f"{self.ref}.{self.number}"


@dataclass
class Sym:
    uuid: str
    ref: str
    lib_id: str
    value: str
    footprint: str
    pos: tuple[float, float]
    rotation: int
    mirror: str
    bbox: tuple[float, float, float, float] | None
    pins: dict[str, Pin]
    power: bool
    proto: object = field(repr=False)
    occupied: tuple[float, float, float, float] | None = None  # union of bbox + visible field text (reporting)
    boxes: list = field(default_factory=list)  # obstacles: body/pins box + one box per visible field


@dataclass
class Scene:
    syms: dict[str, Sym]  # uuid -> Sym
    wires: list[tuple[str, tuple, tuple]]  # (uuid, start, end)
    labels: list[tuple[str, str, tuple, str]]  # (uuid, text, pos, kind)
    junctions: list[tuple[str, tuple]]
    no_connects: list[tuple[str, tuple]]
    label_spins: list = field(default_factory=list)  # (uuid, text, pos, kind, spin)

    def text_boxes(self) -> list[tuple[float, float, float, float]]:
        """Everything new items must not cover: symbols incl. their field text + label text."""
        return [b for s in self.syms.values() for b in s.boxes] + self.label_boxes()

    def label_boxes(self) -> list[tuple[float, float, float, float]]:
        out = []
        for _u, t, p, _k, spin in self.label_spins:
            w = 1.1 * len(t) + 1.0
            dx, dy = SPIN_DIR.get(spin, (1, 0))
            if dx:
                x0, x1 = (p[0], p[0] + w) if dx > 0 else (p[0] - w, p[0])
                out.append((x0, p[1] - 0.9, x1, p[1] + 0.9))
            else:
                y0, y1 = (p[1], p[1] + w) if dy > 0 else (p[1] - w, p[1])
                out.append((p[0] - 0.9, y0, p[0] + 0.9, y1))
        return out

    def by_ref(self, ref: str) -> list[Sym]:
        return [s for s in self.syms.values() if s.ref == ref]

    def all_pins(self) -> list[Pin]:
        return [p for s in self.syms.values() for p in s.pins.values()]

    def connection_points(self) -> list[tuple[tuple, str]]:
        pts = [(p.pos, f"pin {p.label}") for p in self.all_pins()]
        for _u, a, b in self.wires:
            pts += [(a, "wire end"), (b, "wire end")]
        pts += [(pos, f"label {t}") for _u, t, pos, _k in self.labels]
        pts += [(pos, "junction") for _u, pos in self.junctions]
        pts += [(pos, "no-connect") for _u, pos in self.no_connects]
        return pts

    def items_at(self, pt) -> list[str]:
        return [what for p, what in self.connection_points() if same(p, pt)] + [
            "wire interior" for _u, a, b in self.wires if on_segment(pt, a, b) and not (same(pt, a) or same(pt, b))
        ]


def _mm(v) -> tuple[float, float]:
    return (round(v.x / MM, 4), round(v.y / MM, 4))


def rotation_of(sym) -> tuple[int, str]:
    from geom import SSO_DEG

    tr = sym.transform
    mirror = "x" if tr.mirror_x else "y" if tr.mirror_y else "none"
    return SSO_DEG[int(tr.orientation)], mirror


def pins_of(sym) -> dict[str, Pin]:
    t = sym_transform(sym)
    px, py = sym.position.x, sym.position.y
    ref = sym.reference_field.text.value
    out: dict[str, Pin] = {}
    for child in sym.definition.items:
        item = child.item
        if type(item).__name__ != "SchematicPin" or not _in_unit(child, sym):
            continue
        dx, dy = apply(t, item.position.x, item.position.y)
        ix, iy = _INWARD.get(int(item.orientation), (0, 0))
        ox, oy = apply(t, -ix, -iy)
        out.setdefault(
            item.number,
            Pin(
                sid(sym),
                ref,
                item.number,
                item.name,
                EPT.get(int(item.electrical_type), "?"),
                (round((px + dx) / MM, 4), round((py + dy) / MM, 4)),
                (int(ox), int(oy)),
            ),
        )
    return out


def _text_box(f) -> tuple[float, float, float, float] | None:
    """Approximate extent of a visible field (1.27 mm font: ~1.1 mm per character)."""
    from kipy.proto.common.types import HorizontalAlignment as H

    if not f.visible or not f.text.value:
        return None
    x, y = _mm(f.text.position)
    w = 1.1 * len(f.text.value)
    al = f.text.attributes.horizontal_alignment
    x0 = x if al == H.HA_LEFT else x - w if al == H.HA_RIGHT else x - w / 2
    return (x0, y - 0.8, x0 + w, y + 0.8)


def sym_of(s) -> Sym:
    rot, mir = rotation_of(s)
    bbox = symbol_bbox(s)
    occ = bbox
    boxes = [bbox] if bbox else []
    for f in (s.reference_field, s.value_field):
        tb = _text_box(f)
        if tb and occ:
            boxes.append(tb)
            occ = (min(occ[0], tb[0]), min(occ[1], tb[1]), max(occ[2], tb[2]), max(occ[3], tb[3]))
    return Sym(
        uuid=sid(s),
        ref=s.reference_field.text.value,
        lib_id=f"{s.definition.id.library}:{s.definition.id.name}",
        value=s.value_field.text.value,
        footprint=s.footprint_field.text.value,
        pos=_mm(s.position),
        rotation=rot,
        mirror=mir,
        bbox=bbox,
        pins=pins_of(s),
        power=s.reference_field.text.value.startswith("#"),
        proto=s,
        occupied=occ,
        boxes=boxes,
    )


def read_scene(sch) -> Scene:
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLineType

    syms = {}
    for s in sch.get_symbols():
        y = sym_of(s)
        syms[y.uuid] = y
    wires = [(sid(w), _mm(w.start), _mm(w.end)) for w in sch.get_lines() if int(w.type) == SchematicLineType.SLT_WIRE]
    raw_labels = list(sch.get_labels())
    labels = [(sid(lb), lb.text.value, _mm(lb.position), type(lb).__name__) for lb in raw_labels]
    spins = [(sid(lb), lb.text.value, _mm(lb.position), type(lb).__name__, int(lb.spin_style)) for lb in raw_labels]
    juncs = [(sid(j), _mm(j.position)) for j in sch.get_junctions()]
    ncs = [(sid(n), _mm(n.position)) for n in sch.get_no_connects()]
    return Scene(syms, wires, labels, juncs, ncs, spins)


# ------------------------------------------------------------------ connectivity


def partition(sch, scene: Scene) -> dict[tuple[str, str], str]:
    """(symbol uuid, pin number) -> net name from the live GetSchematicNetlist (2A Q3)."""
    owner: dict[str, tuple[str, str]] = {}
    for s in scene.syms.values():
        for child in s.proto.definition.pins:
            owner[sid(child)] = (s.uuid, child.number)
    out = {}
    for net in sch.get_netlist():
        for sheet in net.sheets:
            for item in sheet.items:
                key = owner.get(str(item.value))
                if key:
                    out[key] = net.name
    return out


def groups(nets: dict) -> dict[str, frozenset]:
    g: dict[str, set] = {}
    for key, name in nets.items():
        g.setdefault(name, set()).add(key)
    return {k: frozenset(v) for k, v in g.items()}


def net_key(name: str) -> str:
    return name.rsplit("/", 1)[-1]


# ------------------------------------------------------------------ geometry tests


def on_segment(p, a, b) -> bool:
    if abs(a[0] - b[0]) < EPS:  # vertical
        return abs(p[0] - a[0]) < EPS and min(a[1], b[1]) - EPS <= p[1] <= max(a[1], b[1]) + EPS
    if abs(a[1] - b[1]) < EPS:  # horizontal
        return abs(p[1] - a[1]) < EPS and min(a[0], b[0]) - EPS <= p[0] <= max(a[0], b[0]) + EPS
    return False


def collinear_overlap(a, b, c, d) -> bool:
    """Two axis-aligned segments share more than a point."""
    if abs(a[0] - b[0]) < EPS and abs(c[0] - d[0]) < EPS and abs(a[0] - c[0]) < EPS:
        lo, hi = max(min(a[1], b[1]), min(c[1], d[1])), min(max(a[1], b[1]), max(c[1], d[1]))
        return hi - lo > EPS
    if abs(a[1] - b[1]) < EPS and abs(c[1] - d[1]) < EPS and abs(a[1] - c[1]) < EPS:
        lo, hi = max(min(a[0], b[0]), min(c[0], d[0])), min(max(a[0], b[0]), max(c[0], d[0]))
        return hi - lo > EPS
    return False


def crosses(a, b, c, d) -> bool:
    """Perpendicular crossing of interiors (no connection in KiCad without a junction)."""
    if abs(a[0] - b[0]) < EPS and abs(c[1] - d[1]) < EPS:
        x, y = a[0], c[1]
        return min(c[0], d[0]) + EPS < x < max(c[0], d[0]) - EPS and min(a[1], b[1]) + EPS < y < max(a[1], b[1]) - EPS
    if abs(a[1] - b[1]) < EPS and abs(c[0] - d[0]) < EPS:
        return crosses(c, d, a, b)
    return False


def seg_hits_box(a, b, box, eps=0.05) -> bool:
    x0, y0, x1, y1 = box[0] + eps, box[1] + eps, box[2] - eps, box[3] - eps
    sx0, sx1 = min(a[0], b[0]), max(a[0], b[0])
    sy0, sy1 = min(a[1], b[1]), max(a[1], b[1])
    return sx0 < x1 and sx1 > x0 and sy0 < y1 and sy1 > y0


def any_overlap(boxes_a, boxes_b, margin=0.0) -> bool:
    return any(boxes_overlap(a, b, margin) for a in boxes_a for b in boxes_b)


def shift(box, dx: float, dy: float):
    return (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)


def pin_corridors(sym: Sym, length: float = 5.08, half: float = 1.27) -> list:
    """Room in front of each pin for a stub + power symbol / label (kept free on placement)."""
    out = []
    for p in sym.pins.values():
        tip = (p.pos[0] + p.out[0] * length, p.pos[1] + p.out[1] * length)
        out.append((min(p.pos[0], tip[0]) - half, min(p.pos[1], tip[1]) - half, max(p.pos[0], tip[0]) + half, max(p.pos[1], tip[1]) + half))
    return out


def boxes_overlap(a, b, margin=0.0) -> bool:
    return a[0] - margin < b[2] and a[2] + margin > b[0] and a[1] - margin < b[3] and a[3] + margin > b[1]


def simplify(points: list) -> list:
    pts = [points[0]]
    for p in points[1:]:
        if same(p, pts[-1]):
            continue
        if len(pts) >= 2:
            a, b = pts[-2], pts[-1]
            if (abs(a[0] - b[0]) < EPS and abs(b[0] - p[0]) < EPS) or (abs(a[1] - b[1]) < EPS and abs(b[1] - p[1]) < EPS):
                # collinear: drop the middle point unless the path reverses
                if (b[0] - a[0]) * (p[0] - b[0]) + (b[1] - a[1]) * (p[1] - b[1]) < 0:
                    return []
                pts[-1] = p
                continue
        pts.append(p)
    return pts


def path_problems(points, scene: Scene, terminals: list[Pin], own_syms: set[str], extra_points=()) -> list[str]:
    """Why a wire path would be unsafe (empty list = OK)."""
    probs: list[str] = []
    segs = list(zip(points, points[1:], strict=False))
    term_pts = [t.pos for t in terminals]
    for i, (a, b) in enumerate(segs):
        if not (abs(a[0] - b[0]) < EPS or abs(a[1] - b[1]) < EPS):
            probs.append("non-orthogonal segment")
        for p, what in [*scene.connection_points(), *extra_points]:
            if any(same(p, t) for t in term_pts):
                continue
            if on_segment(p, a, b):
                probs.append(f"touches {what} at {p}")
        for _u, c, d in scene.wires:
            if collinear_overlap(a, b, c, d):
                probs.append(f"overlaps wire {c}-{d}")
        for v in (a, b):
            if any(same(v, t) for t in term_pts):
                continue
            for _u, c, d in scene.wires:
                if on_segment(v, c, d):
                    probs.append(f"corner {v} on existing wire")
        for s in scene.syms.values():
            if s.bbox is None:
                continue
            first = i == 0 and s.uuid == terminals[0].sym_uuid
            last = i == len(segs) - 1 and len(terminals) > 1 and s.uuid == terminals[-1].sym_uuid
            if first or last:
                continue  # the stub leaving a pin outward may cross its own body box margin
            if seg_hits_box(a, b, s.bbox):
                probs.append(f"crosses body of {s.ref}")
    return sorted(set(probs))


def route(pa: Pin, pb: Pin, scene: Scene, max_segments: int = 4) -> tuple[list | None, list[str]]:
    """Cheapest clean orthogonal path pa -> pb; returns (points, rejected reasons)."""
    A, B = pa.pos, pb.pos
    da, db = pa.out, pb.out
    cands = [[A, B], [A, (B[0], A[1]), B], [A, (A[0], B[1]), B]]
    for s in (2.54, 5.08, 7.62, 10.16):
        sa = snap_pt((A[0] + da[0] * s, A[1] + da[1] * s))
        sb = snap_pt((B[0] + db[0] * s, B[1] + db[1] * s))
        cands += [
            [A, sa, (sb[0], sa[1]), sb, B],
            [A, sa, (sa[0], sb[1]), sb, B],
            [A, sa, (B[0], sa[1]), B],
            [A, sa, (sa[0], B[1]), B],
            [A, (sb[0], A[1]), sb, B],
            [A, (A[0], sb[1]), sb, B],
        ]
    xs = sorted({snap((A[0] + B[0]) / 2 + k * 2.54) for k in range(-4, 5)})
    ys = sorted({snap((A[1] + B[1]) / 2 + k * 2.54) for k in range(-4, 5)})
    cands += [[A, (x, A[1]), (x, B[1]), B] for x in xs] + [[A, (A[0], y), (B[0], y), B] for y in ys]
    best, best_score, reasons = None, None, []
    for c in cands:
        pts = simplify(c)
        if len(pts) < 2 or len(pts) - 1 > max_segments:
            continue
        # leave each pin outward, enter the other pin from its outward side
        v0 = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
        v1 = (pts[-2][0] - pts[-1][0], pts[-2][1] - pts[-1][1])
        if v0[0] * da[0] + v0[1] * da[1] <= 0 or v1[0] * db[0] + v1[1] * db[1] <= 0:
            continue
        probs = path_problems(pts, scene, [pa, pb], {pa.sym_uuid, pb.sym_uuid})
        if probs:
            reasons += probs
            continue
        length = sum(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p, q in zip(pts, pts[1:], strict=False))
        n_cross = sum(crosses(p, q, c_, d_) for p, q in zip(pts, pts[1:], strict=False) for _u, c_, d_ in scene.wires)
        score = (len(pts) - 1) * 5 + length + n_cross * 4
        if best_score is None or score < best_score:
            best, best_score = pts, score
    return best, sorted(set(reasons))[:6]
