"""Pure board model (B-C2): frozen dataclasses in mm, no kipy import, plus the two
content hashes (`placement_hash`, `board_hash`). KIIDs never enter a hash."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from copperchat.board.geom import polygon_inside, rect_poly

Point = tuple[float, float]
Side = Literal["top", "bottom"]


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class TextItem:
    kind: str
    text: str
    layer: str
    at: Point
    size: Point
    thickness: float
    visible: bool


@dataclass(frozen=True)
class GraphicItem:
    layer: str
    kind: str
    points: tuple[Point, ...]
    width: float


@dataclass(frozen=True)
class Pad:
    ref: str
    number: str
    net: str
    x: float
    y: float
    w: float
    h: float
    rot: float
    shape: str
    layers: frozenset[str]
    smd: bool
    drill: float | None


@dataclass(frozen=True)
class Part:
    ref: str
    kiid: str
    lib_id: str
    value: str
    x: float
    y: float
    rot: float
    side: Side
    locked: bool
    courtyard: tuple[Point, ...]
    pads: tuple[Pad, ...]
    sheet: str
    symbol_path: str
    texts: tuple[TextItem, ...]
    group: str | None
    rule_areas: tuple[tuple[Point, ...], ...] = ()


@dataclass(frozen=True)
class Track:
    kiid: str
    net: str
    layer: str
    start: Point
    end: Point
    width: float
    locked: bool


@dataclass(frozen=True)
class ArcTrack:
    kiid: str
    net: str
    layer: str
    start: Point
    mid: Point
    end: Point
    width: float
    locked: bool


@dataclass(frozen=True)
class Via:
    kiid: str
    net: str
    at: Point
    diameter: float
    drill: float
    locked: bool


@dataclass(frozen=True)
class Zone:
    kiid: str
    name: str
    net: str
    layers: tuple[str, ...]
    outline: tuple[Point, ...]
    priority: int
    rule_area: bool
    keepout: frozenset[str]
    settings: tuple[tuple[str, str], ...]
    filled_area_mm2: float
    filled: tuple[tuple[Point, ...], ...] = ()


@dataclass(frozen=True)
class Outline:
    kind: Literal["rect", "other", "none"]
    x: float
    y: float
    w: float
    h: float
    corner_radius: float
    polygon: tuple[Point, ...]


@dataclass(frozen=True)
class NetClassInfo:
    name: str
    track_width: float
    clearance: float
    via_diameter: float
    via_drill: float
    diff_pair_width: float | None
    diff_pair_gap: float | None
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class BoardModel:
    parts: tuple[Part, ...]
    tracks: tuple[Track, ...]
    arcs: tuple[ArcTrack, ...]
    vias: tuple[Via, ...]
    zones: tuple[Zone, ...]
    graphics: tuple[GraphicItem, ...]
    outline: Outline
    copper_layers: tuple[str, ...]
    layer_types: tuple[tuple[str, str], ...]
    rules: tuple[tuple[str, float], ...]
    net_classes: tuple[NetClassInfo, ...]
    net_class_of: tuple[tuple[str, str], ...]
    groups: tuple[tuple[str, tuple[str, ...]], ...]
    dru_text: str
    stackup: tuple[tuple[str, str], ...]
    dru_marker: str = ""
    warnings: tuple[str, ...] = ()
    texts: tuple[TextItem, ...] = ()  # board-level texts and text boxes (not footprints')

    def part(self, ref: str) -> Part:
        for p in self.parts:
            if p.ref == ref:
                return p
        raise KeyError(ref)

    def placed(self, part: Part) -> bool:
        """The courtyard lies fully inside the board outline; False without an outline
        (or without a courtyard)."""
        o = self.outline
        if o.kind == "none" or len(part.courtyard) < 3:
            return False
        if o.kind == "rect" and o.corner_radius > 0:
            rounded = rect_poly(o.x, o.y, o.w, o.h, o.corner_radius)
            return polygon_inside(part.courtyard, rounded)
        if o.kind == "rect":
            return all(
                o.x <= px <= o.x + o.w and o.y <= py <= o.y + o.h for px, py in part.courtyard
            )
        return len(o.polygon) >= 3 and polygon_inside(part.courtyard, o.polygon)


# ---- hashing -----------------------------------------------------------------------------


def _r(v: float, nd: int) -> float:
    return round(v, nd) + 0.0  # "+ 0.0" folds -0.0 into 0.0


def _rot(v: float, nd: int) -> float:
    return round(v, nd) % 360 + 0.0  # round first: 359.99999 and -1e-9 both become 0.0


def _pt(p: Point, nd: int) -> list[float]:
    return [_r(p[0], nd), _r(p[1], nd)]


def _poly(pts: tuple[Point, ...], nd: int) -> list[list[float]]:
    return [_pt(p, nd) for p in pts]


def _digest(obj: Any) -> str:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canon(items: list[Any]) -> list[Any]:
    return sorted(items, key=lambda i: json.dumps(i, sort_keys=True, separators=(",", ":")))


def _rule_areas(m: BoardModel) -> list[Any]:
    out: list[Any] = [
        [z.name, list(z.layers), sorted(z.keepout), _poly(z.outline, 2)]
        for z in m.zones
        if z.rule_area
    ]
    for p in m.parts:
        out.extend([f"fp:{p.ref}", [], [], _poly(ra, 2)] for ra in p.rule_areas)
    return _canon(out)


def placement_hash(m: BoardModel) -> str:
    """sha256 of the placement: parts (ref, x, y, rot, side, locked), outline and rule areas."""
    o = m.outline
    # The full rows are sorted (not by ref alone): parts sharing a ref (REF**) hash the
    # same in any API order.
    parts = _canon(
        [[p.ref, _r(p.x, 2), _r(p.y, 2), _rot(p.rot, 1), p.side, p.locked] for p in m.parts]
    )
    outline = [
        o.kind,
        _r(o.x, 2),
        _r(o.y, 2),
        _r(o.w, 2),
        _r(o.h, 2),
        _r(o.corner_radius, 2),
        _poly(o.polygon, 2),
    ]
    return _digest({"parts": parts, "outline": outline, "rule_areas": _rule_areas(m)})


def _text(t: TextItem) -> list[Any]:
    return [t.kind, t.text, t.layer, _pt(t.at, 3), _pt(t.size, 3), _r(t.thickness, 3), t.visible]


def _pad(p: Pad) -> list[Any]:
    return [
        p.ref,
        p.number,
        p.net,
        _r(p.x, 3),
        _r(p.y, 3),
        _r(p.w, 3),
        _r(p.h, 3),
        _rot(p.rot, 1),
        p.shape,
        sorted(p.layers),
        p.smd,
        None if p.drill is None else _r(p.drill, 3),
    ]


def _opt(v: float | None) -> float | None:
    return None if v is None else _r(v, 3)


def board_hash(m: BoardModel) -> str:
    """sha256 of everything that makes up the board except fills, warnings and KIIDs."""
    o = m.outline
    parts = [
        [
            p.ref,
            p.lib_id,
            p.value,
            _r(p.x, 3),
            _r(p.y, 3),
            _rot(p.rot, 3),
            p.side,
            p.locked,
            _poly(p.courtyard, 3),
            _canon([_pad(pad) for pad in p.pads]),
            p.sheet,
            p.symbol_path,
            _canon([_text(t) for t in p.texts]),
            p.group,
            _canon([_poly(ra, 3) for ra in p.rule_areas]),
        ]
        for p in m.parts
    ]
    tracks = [
        [t.net, t.layer, _pt(t.start, 3), _pt(t.end, 3), _r(t.width, 3), t.locked] for t in m.tracks
    ]
    arcs = [
        [a.net, a.layer, _pt(a.start, 3), _pt(a.mid, 3), _pt(a.end, 3), _r(a.width, 3), a.locked]
        for a in m.arcs
    ]
    vias = [[v.net, _pt(v.at, 3), _r(v.diameter, 3), _r(v.drill, 3), v.locked] for v in m.vias]
    zones = [
        [
            z.name,
            z.net,
            list(z.layers),
            _poly(z.outline, 3),
            z.priority,
            z.rule_area,
            sorted(z.keepout),
            sorted([k, v] for k, v in z.settings),
        ]
        for z in m.zones
    ]
    graphics = [[g.layer, g.kind, _poly(g.points, 3), _r(g.width, 3)] for g in m.graphics]
    net_classes = [
        [
            c.name,
            _r(c.track_width, 3),
            _r(c.clearance, 3),
            _r(c.via_diameter, 3),
            _r(c.via_drill, 3),
            _opt(c.diff_pair_width),
            _opt(c.diff_pair_gap),
            sorted(c.patterns),
        ]
        for c in m.net_classes
    ]
    dru = m.dru_text.replace("\r\n", "\n").replace("\r", "\n")
    return _digest(
        {
            "parts": _canon(parts),  # full rows: duplicate refs hash order-free
            "texts": _canon([_text(t) for t in m.texts]),
            "tracks": _canon(tracks),
            "arcs": _canon(arcs),
            "vias": _canon(vias),
            "zones": _canon(zones),
            "graphics": _canon(graphics),
            "outline": [
                o.kind,
                _r(o.x, 3),
                _r(o.y, 3),
                _r(o.w, 3),
                _r(o.h, 3),
                _r(o.corner_radius, 3),
                _poly(o.polygon, 3),
            ],
            "rules": sorted([name, _r(v, 3)] for name, v in m.rules),
            "net_classes": _canon(net_classes),
            "net_class_of": sorted([n, c] for n, c in m.net_class_of),
            "groups": sorted([g, sorted(refs)] for g, refs in m.groups),
            "dru_text": dru,
            "dru_marker": m.dru_marker,
            "copper_layers": list(m.copper_layers),
            "layer_types": [list(t) for t in m.layer_types],
            "stackup": [list(s) for s in m.stackup],
        }
    )
