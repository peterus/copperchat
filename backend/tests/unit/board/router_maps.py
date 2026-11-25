"""Seeded random maps for the router's property and performance tests (P5a Task 5), and an
independent exact clearance check (no grid, no router code)."""

from __future__ import annotations

import itertools
import math
import random
from collections.abc import Sequence
from dataclasses import replace

from copperchat.board.geom import point_in_polygon, rect_poly
from copperchat.board.model import BoardModel, Pad, Part, Point, Track, Via
from copperchat.board.router import RoutePath, RouteRequest

from .builders import board, outline, part, track, via, zone

EDGE = 0.3


# --- independent geometry ------------------------------------------------------------------


def _pt_seg(p: Point, a: Point, b: Point) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    ll = vx * vx + vy * vy
    t = 0.0 if ll == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / ll))
    return math.hypot(p[0] - a[0] - t * vx, p[1] - a[1] - t * vy)


def _cross(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    d1, d2, d3, d4 = _cross(c, d, a), _cross(c, d, b), _cross(a, b, c), _cross(a, b, d)
    return d1 * d2 < 0 and d3 * d4 < 0


def seg_dist(a: Point, b: Point, c: Point, d: Point) -> float:
    if _intersect(a, b, c, d):
        return 0.0
    return min(_pt_seg(a, c, d), _pt_seg(b, c, d), _pt_seg(c, a, b), _pt_seg(d, a, b))


def pad_corners(p: Pad) -> list[Point]:
    """The pad rectangle in board coordinates (rotation counter-clockwise on screen)."""
    t = math.radians(p.rot)
    c, s = math.cos(t), math.sin(t)
    out = []
    for lx, ly in (
        (-p.w / 2, -p.h / 2),
        (p.w / 2, -p.h / 2),
        (p.w / 2, p.h / 2),
        (-p.w / 2, p.h / 2),
    ):
        # inverse of geom._in_pad: lx = dx c - dy s, ly = dx s + dy c
        out.append((p.x + lx * c + ly * s, p.y - lx * s + ly * c))
    return out


def seg_poly_dist(a: Point, b: Point, poly: Sequence[Point]) -> float:
    if point_in_polygon(a, list(poly)) or point_in_polygon(b, list(poly)):
        return 0.0
    n = len(poly)
    return min(seg_dist(a, b, poly[k], poly[(k + 1) % n]) for k in range(n))


def _on(p: Pad, layer: str) -> bool:
    return layer in p.layers or "*.Cu" in p.layers


def exact_clearance_ok(
    model: BoardModel, path: RoutePath, req: RouteRequest, *, margin: float = 0.0
) -> bool:
    """Every segment keeps `clearance + width/2 (+ margin)` from other-net copper (exact pad
    rectangles, track capsules, via discs), `width/2` from `tracks` keepouts and stays
    inside the outline inset; every via keeps `clearance + diameter/2` from other-net copper
    on any layer (tolerance 1e-6)."""
    tol = 1e-6
    need = req.clearance + req.width / 2 + margin
    other_pads = [p for pt in model.parts for p in pt.pads if p.net != req.net]
    edge = max(dict(model.rules).get("copper_edge_clearance", 0.0), EDGE) + req.width / 2 + margin
    poly = list(model.outline.polygon)
    for layer, a, b in path.segments:
        for p in other_pads:
            if _on(p, layer):
                if p.shape == "circle":
                    if _pt_seg((p.x, p.y), a, b) - p.w / 2 < need - tol:
                        return False
                elif seg_poly_dist(a, b, pad_corners(p)) < need - tol:
                    return False
        for t in model.tracks:
            if (
                t.net != req.net
                and t.layer == layer
                and seg_dist(a, b, t.start, t.end) < need + t.width / 2 - tol
            ):
                return False
        for v in model.vias:
            if v.net != req.net and _pt_seg(v.at, a, b) < need + v.diameter / 2 - tol:
                return False
        for z in model.zones:
            keepout = z.rule_area and "tracks" in z.keepout and layer in z.layers
            if keepout and seg_poly_dist(a, b, z.outline) < req.width / 2 + margin - tol:
                return False
        if poly:
            if not (point_in_polygon(a, poly) and point_in_polygon(b, poly)):
                return False
            n = len(poly)
            if min(seg_dist(a, b, poly[k], poly[(k + 1) % n]) for k in range(n)) < edge - tol:
                return False
    vneed = req.clearance + req.via[0] / 2 + margin
    for at in path.vias:
        for p in other_pads:
            if seg_poly_dist(at, at, pad_corners(p)) < vneed - tol:
                return False
        for t in model.tracks:
            if t.net != req.net and _pt_seg(at, t.start, t.end) < vneed + t.width / 2 - tol:
                return False
        for v in model.vias:
            if v.net != req.net and math.dist(at, v.at) < vneed + v.diameter / 2 - tol:
                return False
    return True


# --- map generation ------------------------------------------------------------------------


def smd_pad(
    ref: str,
    number: str,
    net: str,
    x: float,
    y: float,
    w: float,
    h: float,
    rot: float = 0.0,
    layer: str = "F.Cu",
) -> Pad:
    return Pad(
        ref=ref,
        number=number,
        net=net,
        x=x,
        y=y,
        w=w,
        h=h,
        rot=rot,
        shape="rect",
        layers=frozenset({layer, "F.Paste" if layer == "F.Cu" else "B.Paste"}),
        smd=True,
        drill=None,
    )


def _pad_free(p: Pad, m_pads: list[Pad], tracks: list[Track], vias: list[Via], gap: float) -> bool:
    corners = pad_corners(p)
    for q in m_pads:
        if (
            math.dist((p.x, p.y), (q.x, q.y))
            < (math.hypot(p.w, p.h) + math.hypot(q.w, q.h)) / 2 + gap
        ):
            return False
    for t in tracks:
        if seg_poly_dist(t.start, t.end, corners) < t.width / 2 + gap:
            return False
    return all(seg_poly_dist(v.at, v.at, corners) >= v.diameter / 2 + gap for v in vias)


def random_map(rng: random.Random) -> tuple[BoardModel, list[RouteRequest]]:
    """Outline 10-30 mm, 1-2 copper layers, 2-20 obstacles of other nets (SMD pad rows,
    tracks at octilinear and other angles, vias, one keepout with p = 0.3), 1-3 two-pad
    requests with widths 0.15-0.5 mm and clearances 0.1-0.3 mm."""
    w, h = rng.uniform(10, 30), rng.uniform(10, 30)
    layers = ("F.Cu",) if rng.random() < 0.5 else ("F.Cu", "B.Cu")
    pads: list[Pad] = []
    tracks: list[Track] = []
    vias: list[Via] = []
    zones = []
    for k in range(rng.randint(2, 20)):
        kind = rng.random()
        net = f"O{k}"
        if kind < 0.4:
            n = rng.randint(2, 6)
            pitch = rng.choice((0.5, 0.65, 0.8, 1.27))
            pw, ph = pitch * rng.uniform(0.4, 0.6), rng.uniform(0.5, 1.5)
            x0, y0 = rng.uniform(1, w - 1 - n * pitch), rng.uniform(1, h - 1)
            rot = rng.choice((0.0, 90.0, 45.0, 30.0))
            for q in range(n):
                p = smd_pad(
                    f"X{k}",
                    str(q + 1),
                    f"{net}_{q}",
                    x0 + q * pitch,
                    y0,
                    pw,
                    ph,
                    rot,
                    rng.choice(layers),
                )
                pads.append(p)
        elif kind < 0.75:
            a = (rng.uniform(0, w), rng.uniform(0, h))
            ang = rng.choice((0, 45, 90, 135, rng.uniform(0, 180)))
            length = rng.uniform(2, 12)
            b = (
                a[0] + length * math.cos(math.radians(ang)),
                a[1] + length * math.sin(math.radians(ang)),
            )
            tracks.append(
                track(net, rng.choice(layers), a, b, rng.uniform(0.15, 0.5), kiid=f"t{k}")
            )
        elif kind < 0.9:
            vias.append(
                via(net, (rng.uniform(1, w - 1), rng.uniform(1, h - 1)), 0.6, 0.3, kiid=f"v{k}")
            )
        else:
            vias.append(
                via(net, (rng.uniform(1, w - 1), rng.uniform(1, h - 1)), 0.45, 0.2, kiid=f"v{k}")
            )
    if rng.random() < 0.3:
        x, y = rng.uniform(1, w - 4), rng.uniform(1, h - 4)
        keep = frozenset({rng.choice(("tracks", "vias"))})
        poly = tuple(rect_poly(x, y, rng.uniform(1, 3), rng.uniform(1, 3)))
        zones.append(zone("K", "", layers, poly, rule_area=True, keepout=keep, kiid="z"))
    reqs: list[RouteRequest] = []
    req_pads: list[Pad] = []
    for r in range(rng.randint(1, 3)):
        width, clearance = round(rng.uniform(0.15, 0.5), 3), round(rng.uniform(0.1, 0.3), 3)
        net = f"R{r}"
        ends: list[Pad] = []
        for e in range(2):
            for _try in range(200):
                pw = rng.uniform(max(width, 0.4), 1.2)
                ph = rng.uniform(max(width, 0.4), 1.2)
                x = rng.uniform(1 + pw, w - 1 - pw)
                y = rng.uniform(1 + ph, h - 1 - ph)
                rot = rng.choice((0.0, 0.0, 90.0, 45.0))
                p = smd_pad(f"R{r}{'AB'[e]}", "1", net, x, y, pw, ph, rot, layers[0])
                inside_keepout = any(
                    seg_poly_dist((p.x, p.y), (p.x, p.y), z.outline) < math.hypot(pw, ph) + 0.5
                    for z in zones
                )
                if not inside_keepout and _pad_free(
                    p, pads + req_pads + ends, tracks, vias, 0.3 + width + clearance
                ):
                    ends.append(p)
                    break
        if len(ends) < 2:
            continue
        req_pads.extend(ends)
        reqs.append(
            RouteRequest(
                net, ends[0], ends[1], width, clearance, layers, len(layers) > 1, (0.6, 0.3)
            )
        )
    parts = tuple(
        part(ref, sum(p.x for p in ps) / len(ps), sum(p.y for p in ps) / len(ps), pads=tuple(ps))
        for ref, ps in itertools.groupby(
            sorted(pads + req_pads, key=lambda p: p.ref), key=lambda p: p.ref
        )
        for ps in [list(ps)]
    )
    m = board(
        parts=parts,
        tracks=tuple(tracks),
        vias=tuple(vias),
        zones=tuple(zones),
        outline=outline(0, 0, w, h),
        copper_layers=layers,
        layer_types=tuple((name, "signal") for name in layers),
    )
    return m, reqs


def open_map(rng: random.Random) -> tuple[BoardModel, list[RouteRequest]]:
    """A random map without obstacles: only the request pads."""
    m, reqs = random_map(rng)
    keep = {r.net for r in reqs}
    parts = tuple(replace(p, pads=tuple(q for q in p.pads if q.net in keep)) for p in m.parts)
    m = replace(m, parts=tuple(p for p in parts if p.pads), tracks=(), vias=(), zones=())
    return m, reqs


def add_path(m: BoardModel, req: RouteRequest, path: RoutePath, tag: str) -> BoardModel:
    """`m` with the routed copper of `path` (as P5b will commit it)."""
    tracks = tuple(
        Track(f"{tag}-t{k}", req.net, layer, a, b, req.width, True)
        for k, (layer, a, b) in enumerate(path.segments)
    )
    vias = tuple(
        Via(f"{tag}-v{k}", req.net, at, req.via[0], req.via[1], True)
        for k, at in enumerate(path.vias)
    )
    return replace(m, tracks=m.tracks + tracks, vias=m.vias + vias)


# --- performance boards ----------------------------------------------------------------------

_FOOTPRINTS = (  # (pad count per side, pitch, pad w, pad h, row gap): 2-pin chips and SOIC-like
    (1, 0.0, 0.9, 1.0, 1.6),  # 0805
    (1, 0.0, 0.8, 0.9, 1.4),  # 0603
    (4, 1.27, 0.6, 1.5, 5.2),  # SOIC-8
    (3, 0.95, 0.6, 1.0, 2.2),  # SOT-23-6
)


def perf_board(
    rng: random.Random,
    *,
    size: float,
    n_parts: int,
    n_nets: int,
    layers: tuple[str, ...],
    net_len: tuple[float, float],
    width: float = 0.2,
    clearance: float = 0.2,
) -> tuple[BoardModel, list[RouteRequest]]:
    """`n_parts` SMD-like parts on a `size` mm square board (2-pin chips and SOIC/SOT rows,
    0 or 90 degrees, courtyards apart) and `n_nets` two-pin nets between pads of different
    parts whose centres are `net_len` apart; every other pad is its own net."""
    parts: list[Part] = []
    boxes: list[tuple[float, float, float, float]] = []
    tries = 0
    while len(parts) < n_parts and tries < 20_000:
        tries += 1
        n, pitch, pw, ph, gap = rng.choice(_FOOTPRINTS)
        rot = rng.choice((0.0, 90.0))
        span_x = (n - 1) * pitch + pw
        span_y = gap + ph
        hx, hy = (
            (span_x / 2 + 0.6, span_y / 2 + 0.6)
            if rot == 0.0
            else (span_y / 2 + 0.6, span_x / 2 + 0.6)
        )
        cx, cy = rng.uniform(2 + hx, size - 2 - hx), rng.uniform(2 + hy, size - 2 - hy)
        box = (cx - hx, cy - hy, cx + hx, cy + hy)
        if any(
            not (
                box[2] + 0.5 < b[0]
                or b[2] + 0.5 < box[0]
                or box[3] + 0.5 < b[1]
                or b[3] + 0.5 < box[1]
            )
            for b in boxes
        ):
            continue
        ref = f"U{len(parts) + 1}"
        pads = []
        num = 1
        for side in (-1, 1):
            for q in range(n):
                lx, ly = (q - (n - 1) / 2) * pitch, side * gap / 2
                x, y = (cx + lx, cy + ly) if rot == 0.0 else (cx + ly, cy - lx)
                pads.append(smd_pad(ref, str(num), f"{ref}-{num}", x, y, pw, ph, rot))
                num += 1
        boxes.append(box)
        parts.append(part(ref, cx, cy, pads=tuple(pads)))
    all_pads = [(pi, p) for pi, pt in enumerate(parts) for p in pt.pads]
    used: set[tuple[str, str]] = set()
    reqs: list[RouteRequest] = []
    tries = 0
    nets: dict[tuple[str, str], str] = {}
    while len(reqs) < n_nets and tries < 50_000:
        tries += 1
        (pa, a), (pb, b) = rng.sample(all_pads, 2)
        if pa == pb or (a.ref, a.number) in used or (b.ref, b.number) in used:
            continue
        if not (net_len[0] <= math.dist((a.x, a.y), (b.x, b.y)) <= net_len[1]):
            continue
        net = f"N{len(reqs) + 1}"
        used.update({(a.ref, a.number), (b.ref, b.number)})
        nets[(a.ref, a.number)] = nets[(b.ref, b.number)] = net
        reqs.append(
            RouteRequest(
                net,
                replace(a, net=net),
                replace(b, net=net),
                width,
                clearance,
                layers,
                len(layers) > 1,
                (0.6, 0.3),
            )
        )
    parts = [
        replace(pt, pads=tuple(replace(p, net=nets.get((p.ref, p.number), p.net)) for p in pt.pads))
        for pt in parts
    ]
    m = board(
        parts=tuple(parts),
        outline=outline(0, 0, size, size),
        copper_layers=layers,
        layer_types=tuple((name, "signal") for name in layers),
    )
    return m, reqs
