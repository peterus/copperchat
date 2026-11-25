"""Circuit-level builders on top of `builders` (P3a classification and metrics tests).

Pad positions are explicit and predictable:

* `ic(ref, pins, at)` — the pins, in natural order of their numbers, form one row along +x:
  the k-th pin (0-based) sits at `(at[0] + k * pitch, at[1])` (default pitch 1 mm). The part
  origin is `at`; the courtyard is the pad row's bounding box grown by 1 mm.
* two-pad parts (`cap`, `res`, `ind`, `xtal`) — pad "1" at `(at[0] - 0.5, at[1])` on `net_a`,
  pad "2" at `(at[0] + 0.5, at[1])` on `net_b`; courtyard = 2 x 1 mm box around `at`.
* `conn(ref, pins, at)` — same row layout as `ic`.

Every builder accepts `sheet=` and any other `Part` field as a keyword override."""

from __future__ import annotations

from typing import Any

from copperchat.board.geom import rect_poly
from copperchat.board.model import BoardModel, Pad, Part, Point
from copperchat.kicad_tools.filtering import natural_key

from .builders import board, part


def pad(ref: str, number: str, net: str, at: Point, *, w: float = 0.6, h: float = 0.6) -> Pad:
    return Pad(
        ref=ref,
        number=number,
        net=net,
        x=at[0],
        y=at[1],
        w=w,
        h=h,
        rot=0.0,
        shape="rect",
        layers=frozenset({"F.Cu"}),
        smd=True,
        drill=None,
    )


def _bbox_courtyard(pads: tuple[Pad, ...], at: Point, grow: float) -> tuple[Point, ...]:
    xs = [p.x for p in pads] or [at[0]]
    ys = [p.y for p in pads] or [at[1]]
    x0, y0 = min(xs) - grow, min(ys) - grow
    return tuple(rect_poly(x0, y0, max(xs) - min(xs) + 2 * grow, max(ys) - min(ys) + 2 * grow))


def _row_part(
    ref: str, pins: dict[str, str], at: Point, lib_id: str, pitch: float, **kw: Any
) -> Part:
    numbers = sorted(pins, key=natural_key)
    pads = tuple(pad(ref, n, pins[n], (at[0] + k * pitch, at[1])) for k, n in enumerate(numbers))
    fields: dict[str, Any] = {
        "lib_id": lib_id,
        "pads": pads,
        "courtyard": _bbox_courtyard(pads, at, 1.0),
    }
    fields.update(kw)
    return part(ref, at[0], at[1], **fields)


def ic(
    ref: str, pins: dict[str, str], at: Point = (50.0, 40.0), *, pitch: float = 1.0, **kw: Any
) -> Part:
    """An IC: pins (pad number → net) in one row along +x, `pitch` mm apart, from `at`."""
    return _row_part(ref, pins, at, kw.pop("lib_id", "Package_SO:SOIC-8"), pitch, **kw)


def conn(
    ref: str, pins: dict[str, str], at: Point = (10.0, 40.0), *, pitch: float = 1.0, **kw: Any
) -> Part:
    """A connector, same row layout as `ic`."""
    return _row_part(ref, pins, at, kw.pop("lib_id", "Connector_Generic:Conn"), pitch, **kw)


def two_pad(ref: str, net_a: str, net_b: str, at: Point, lib_id: str, **kw: Any) -> Part:
    pads = (
        pad(ref, "1", net_a, (at[0] - 0.5, at[1])),
        pad(ref, "2", net_b, (at[0] + 0.5, at[1])),
    )
    fields: dict[str, Any] = {
        "lib_id": lib_id,
        "pads": pads,
        "courtyard": tuple(rect_poly(at[0] - 1.0, at[1] - 0.5, 2.0, 1.0)),
    }
    fields.update(kw)
    return part(ref, at[0], at[1], **fields)


def cap(ref: str, net_a: str, net_b: str, at: Point = (50.0, 50.0), **kw: Any) -> Part:
    return two_pad(ref, net_a, net_b, at, kw.pop("lib_id", "Device:C"), **kw)


def res(ref: str, net_a: str, net_b: str, at: Point = (50.0, 50.0), **kw: Any) -> Part:
    return two_pad(ref, net_a, net_b, at, kw.pop("lib_id", "Device:R"), **kw)


def ind(ref: str, net_a: str, net_b: str, at: Point = (50.0, 50.0), **kw: Any) -> Part:
    return two_pad(ref, net_a, net_b, at, kw.pop("lib_id", "Device:L"), **kw)


def xtal(ref: str, net_a: str, net_b: str, at: Point = (50.0, 50.0), **kw: Any) -> Part:
    return two_pad(ref, net_a, net_b, at, kw.pop("lib_id", "Crystal:Crystal_SMD"), **kw)


def circuit(*parts: Part, **kw: Any) -> BoardModel:
    """A board made of exactly `parts` (other fields from `builders.board`)."""
    return board(parts=tuple(parts), **kw)


# ---- P3a metrics helpers (appended) --------------------------------------------------------


def with_layers(p: Pad, *layers: str, smd: bool | None = None, drill: float | None = None) -> Pad:
    """`p` with exactly `layers` (and optionally `smd` / `drill` changed)."""
    from dataclasses import replace

    return replace(
        p,
        layers=frozenset(layers),
        smd=p.smd if smd is None else smd,
        drill=p.drill if drill is None else drill,
    )


def route(
    net: str, pts: list[Point], *, layer: str = "F.Cu", width: float = 0.2
) -> tuple[Any, ...]:
    """Tracks along the polyline `pts` (one `Track` per segment, unique KIIDs)."""
    import itertools

    from .builders import track

    return tuple(
        track(net, layer, a, b, width, kiid=f"t-{net}-{layer}-{i}-{a}-{b}")
        for i, (a, b) in enumerate(itertools.pairwise(pts))
    )


def routed_board(
    seed: int, n_ics: int = 20, n_small: int = 180, max_segments: int = 2000
) -> BoardModel:
    """A seeded 200-part board with ~`max_segments` octilinear track segments (timing tests):
    random parts on a 100 x 80 mm board, each signal net's pads chained by 3-segment
    (x run, 45 degree, y run) routes alternating F.Cu / B.Cu, a via where the layer changes."""
    import itertools
    import random

    from .builders import via

    rnd = random.Random(seed)
    nets = [f"N{i}" for i in range(70)] + ["GND", "+3V3"]
    parts: list[Part] = []
    for i in range(n_ics):
        pins = {str(k): rnd.choice(nets) for k in range(1, 17)}
        parts.append(ic(f"U{i + 1}", pins, (rnd.uniform(5, 80), rnd.uniform(5, 75))))
    for i in range(n_small):
        mk = rnd.choice([cap, res, ind])
        prefix = {cap: "C", res: "R", ind: "L"}[mk]
        parts.append(
            mk(
                f"{prefix}{i + 1}",
                rnd.choice(nets),
                rnd.choice(nets),
                (rnd.uniform(5, 95), rnd.uniform(5, 75)),
            )
        )
    by_net: dict[str, list[Pad]] = {}
    for p in parts:
        for q in p.pads:
            by_net.setdefault(q.net, []).append(q)
    tracks: list[Any] = []
    vias: list[Any] = []
    for net in sorted(by_net):
        if net == "GND":
            continue
        pads = sorted(by_net[net], key=lambda q: (q.x, q.y))
        for k, (a, b) in enumerate(itertools.pairwise(pads)):
            if len(tracks) + 3 > max_segments:
                break
            dx, dy = b.x - a.x, b.y - a.y
            d = min(abs(dx), abs(dy))
            sx = 1.0 if dx >= 0 else -1.0
            sy = 1.0 if dy >= 0 else -1.0
            if abs(dx) >= abs(dy):
                run = (abs(dx) - d) / 2
                p1 = (a.x + sx * run, a.y)
            else:
                run = (abs(dy) - d) / 2
                p1 = (a.x, a.y + sy * run)
            p2 = (p1[0] + sx * d, p1[1] + sy * d)
            pts = [(a.x, a.y), p1, p2, (b.x, b.y)]
            layer = "F.Cu" if k % 2 == 0 else "B.Cu"
            tracks.extend(route(net, pts, layer=layer))
            if layer == "B.Cu":
                vias.append(via(net, (a.x, a.y), kiid=f"v-{net}-{k}"))
    return circuit(*parts, tracks=tuple(tracks), vias=tuple(vias))
