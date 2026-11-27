"""Obstacle grid of the octilinear router (P5a Task 1)."""

from __future__ import annotations

import math
import pickle
import tracemalloc
from typing import Any

import pytest

from copperchat.board.model import Pad
from copperchat.board.router import (
    MAX_CELLS_PER_LAYER,
    RouteRequest,
    RouteTooLarge,
    build_grid,
)
from copperchat.board.router.grid import BLOCKED, FREE, OWN

from .builders import board, outline, part, track, via, zone


def pad(
    net: str,
    x: float,
    y: float,
    w: float = 0.6,
    h: float = 1.0,
    *,
    rot: float = 0.0,
    layer: str = "F.Cu",
    smd: bool = True,
    ref: str = "U9",
    number: str = "1",
    shape: str = "rect",
) -> Pad:
    layers = frozenset({layer, "F.Paste", "F.Mask"}) if smd else frozenset({"*.Cu", "*.Mask"})
    return Pad(
        ref=ref,
        number=number,
        net=net,
        x=x,
        y=y,
        w=w,
        h=h,
        rot=rot,
        shape=shape,
        layers=layers,
        smd=smd,
        drill=None if smd else 0.8,
    )


def req(
    a: tuple[float, float] = (5.0, 10.0),
    b: tuple[float, float] = (15.0, 10.0),
    *,
    width: float = 0.2,
    clearance: float = 0.2,
    layers: tuple[str, ...] = ("F.Cu",),
    allow_vias: bool = False,
    net: str = "SIG",
) -> RouteRequest:
    return RouteRequest(
        net=net,
        a=pad(net, *a, 0.5, 0.5, ref="A"),
        b=pad(net, *b, 0.5, 0.5, ref="B"),
        width=width,
        clearance=clearance,
        layers=layers,
        allow_vias=allow_vias,
        via=(0.6, 0.3),
    )


def model_with(*pads: Pad, **kw: Any):
    parts = tuple(part(f"P{k}", p.x, p.y, pads=(p,)) for k, p in enumerate(pads))
    return board(parts=parts, outline=kw.pop("outline", outline(0, 0, 30, 20)), **kw)


def at(g, li: int, x: float, y: float) -> int:
    i, j = g.cell(x, y)
    assert math.isclose(g.xy(i, j)[0], x, abs_tol=1e-6), (g.xy(i, j), x)
    assert math.isclose(g.xy(i, j)[1], y, abs_tol=1e-6), (g.xy(i, j), y)
    return g.cells[li][j * g.w + i]


def test_pad_inflation_exact() -> None:
    # 0.6 x 1.0 pad of another net at (10, 10); r = clearance + width/2 = 0.3
    # (the plan text says 0.4; the B-C12 formula clearance + margin + width/2 gives 0.3)
    m = model_with(pad("OTHER", 10.0, 10.0))
    g = build_grid(m, req())
    assert at(g, 0, 10.6, 10.0) == BLOCKED  # 0.30 from the right edge
    assert at(g, 0, 10.65, 10.0) == FREE  # 0.35
    assert at(g, 0, 9.4, 10.0) == BLOCKED
    assert at(g, 0, 9.35, 10.0) == FREE
    assert at(g, 0, 10.0, 10.8) == BLOCKED  # 0.30 below the bottom edge (h = 1.0)
    assert at(g, 0, 10.0, 10.85) == FREE
    # rounded corner: (10.3 + 0.2, 10.5 + 0.2) is 0.283 from the corner -> blocked;
    # (10.3 + 0.25, 10.5 + 0.25) is 0.354 -> free
    assert at(g, 0, 10.5, 10.7) == BLOCKED
    assert at(g, 0, 10.55, 10.75) == FREE


def test_rotated_pad_45_circle() -> None:
    m = model_with(pad("OTHER", 10.0, 10.0, 0.6, 1.0, rot=45.0))
    g = build_grid(m, req())
    radius = math.hypot(0.6, 1.0) / 2 + 0.3  # circumscribed circle + r = 0.883
    assert at(g, 0, 10.0 + 0.85, 10.0) == BLOCKED
    assert at(g, 0, 10.0 + 0.9, 10.0) == FREE
    assert radius < 0.9


def test_rotated_pad_90_swaps_axes() -> None:
    m = model_with(pad("OTHER", 10.0, 10.0, 0.6, 1.0, rot=90.0))
    g = build_grid(m, req())
    assert at(g, 0, 10.8, 10.0) == BLOCKED  # long side now along x: edge 10.5 + 0.3
    assert at(g, 0, 10.85, 10.0) == FREE


def test_own_net_passable() -> None:
    m = model_with(pad("SIG", 10.0, 10.0))
    g = build_grid(m, req())
    assert at(g, 0, 10.0, 10.0) == OWN
    assert at(g, 0, 10.6, 10.0) == FREE  # no inflation around own copper
    assert g.free(0, *g.cell(10.0, 10.0))


def test_track_capsule() -> None:
    m = board(
        parts=(),
        outline=outline(0, 0, 30, 20),
        tracks=(track("OTHER", "F.Cu", (8.0, 10.0), (12.0, 10.0), 0.4),),
    )
    g = build_grid(m, req(width=0.2, clearance=0.2))
    need = 0.2 + 0.1 + 0.2  # clearance + width/2 + other half width = 0.5
    assert at(g, 0, 10.0, 10.5) == BLOCKED
    assert at(g, 0, 10.0, 10.55) == FREE
    assert at(g, 0, 12.5, 10.0) == BLOCKED  # round end
    assert at(g, 0, 12.55, 10.0) == FREE
    assert at(g, 0, 12.35, 10.35) == BLOCKED  # 0.495 from the end point
    assert at(g, 0, 12.4, 10.4) == FREE  # 0.566
    assert need == pytest.approx(0.5)


def test_track_other_layer_ignored_for_tracks() -> None:
    m = board(
        parts=(),
        outline=outline(0, 0, 30, 20),
        tracks=(track("OTHER", "B.Cu", (8.0, 10.0), (12.0, 10.0), 0.4),),
    )
    g = build_grid(m, req(layers=("F.Cu",), allow_vias=True))
    assert at(g, 0, 10.0, 10.0) == FREE
    # but a via there would touch it
    assert not g.via_ok(*g.cell(10.0, 10.0))


def test_via_blocks_all_layers() -> None:
    m = board(parts=(), outline=outline(0, 0, 30, 20), vias=(via("OTHER", (10.0, 10.0), 0.6),))
    g = build_grid(m, req(layers=("F.Cu", "B.Cu"), allow_vias=True))
    for li in (0, 1):
        assert at(g, li, 10.6, 10.0) == BLOCKED  # 0.3 + 0.3
        assert at(g, li, 10.65, 10.0) == FREE
    # via to via: 0.3 + 0.2 + 0.3 = 0.8
    assert not g.via_ok(*g.cell(10.8, 10.0))
    assert g.via_ok(*g.cell(10.85, 10.0))


def test_keepout_tracks_vs_vias() -> None:
    poly = ((9.0, 9.0), (11.0, 9.0), (11.0, 11.0), (9.0, 11.0))
    only_vias = zone("K", "", ("F.Cu", "B.Cu"), poly, rule_area=True, keepout=frozenset({"vias"}))
    g = build_grid(
        board(parts=(), outline=outline(0, 0, 30, 20), zones=(only_vias,)),
        req(layers=("F.Cu", "B.Cu"), allow_vias=True),
    )
    assert at(g, 0, 10.0, 10.0) == FREE
    assert not g.via_ok(*g.cell(10.0, 10.0))
    assert not g.via_ok(*g.cell(11.25, 10.0))  # 0.25 < via radius 0.3
    assert g.via_ok(*g.cell(11.35, 10.0))
    only_tracks = zone("K", "", ("F.Cu",), poly, rule_area=True, keepout=frozenset({"tracks"}))
    g = build_grid(
        board(parts=(), outline=outline(0, 0, 30, 20), zones=(only_tracks,)),
        req(layers=("F.Cu", "B.Cu"), allow_vias=True),
    )
    assert at(g, 0, 10.0, 10.0) == BLOCKED
    assert at(g, 0, 11.1, 10.0) == BLOCKED  # 0.1 = width/2 is the only need
    assert at(g, 0, 11.15, 10.0) == FREE
    assert at(g, 1, 10.0, 10.0) == FREE  # other layer
    assert g.via_ok(*g.cell(10.0, 10.0))


def test_edge_inset() -> None:
    # width 0.2, edge clearance 0.3 (floor) -> inset 0.4 from the outline
    g = build_grid(model_with(), req(a=(5.04, 10.0), b=(15.04, 10.0)))
    assert at(g, 0, 0.39, 10.0) == BLOCKED
    g = build_grid(model_with(), req(a=(5.01, 10.0), b=(15.01, 10.0)))
    assert at(g, 0, 0.41, 10.0) == FREE
    # a larger board rule wins over the floor
    g = build_grid(
        model_with(rules=(("copper_edge_clearance", 0.5),)), req(a=(5.01, 10.0), b=(15.01, 10.0))
    )
    assert at(g, 0, 0.56, 10.0) == BLOCKED
    assert at(g, 0, 0.61, 10.0) == FREE


def test_window_clipped() -> None:
    g = build_grid(model_with(), req(a=(2.0, 3.0), b=(6.0, 4.0)))
    x0, y0 = g.xy(0, 0)
    x1, y1 = g.xy(g.w - 1, g.h - 1)
    assert x0 >= 0.0 - 1e-9 and y0 >= 0.0 - 1e-9  # clipped to the outline
    assert x1 == pytest.approx(12.0, abs=0.06)  # 6 + WINDOW_MM
    assert y1 == pytest.approx(10.0, abs=0.06)
    g12 = build_grid(model_with(), req(a=(2.0, 3.0), b=(6.0, 4.0)), window_mm=12.0)
    assert g12.xy(g12.w - 1, 0)[0] == pytest.approx(18.0, abs=0.06)


def test_lattice_aligned_to_first_pad() -> None:
    g = build_grid(model_with(), req(a=(5.013, 10.027), b=(15.0, 10.0)))
    i, j = g.cell(5.013, 10.027)
    assert g.xy(i, j) == pytest.approx((5.013, 10.027), abs=1e-9)


def test_grid_picklable() -> None:
    m = model_with(pad("OTHER", 10.0, 10.0))
    g = build_grid(m, req(layers=("F.Cu", "B.Cu"), allow_vias=True))
    g.segment_clear(0, (5.0, 10.0), (6.0, 10.0))  # builds the lazy index
    h = pickle.loads(pickle.dumps(g))
    assert (h.w, h.h, h.x0, h.y0, h.layers) == (g.w, g.h, g.x0, g.y0, g.layers)
    assert h.cells == g.cells and h.via_blocked == g.via_blocked
    assert h.shapes == g.shapes


def test_window_over_cap_raises() -> None:
    m = board(parts=(), outline=outline(0, 0, 120, 120))
    r = req(a=(0.5, 0.5), b=(119.5, 119.5))
    tracemalloc.start()
    try:
        with pytest.raises(RouteTooLarge) as exc:
            build_grid(m, r, pitch=0.05)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert exc.value.cells > MAX_CELLS_PER_LAYER
    assert peak < 50 * 1024 * 1024
    assert pickle.loads(pickle.dumps(exc.value)).cells == exc.value.cells


def test_window_at_cap_ok() -> None:
    # outline 0..99.95, lattice aligned to a = (1, 1): nodes 0.00 .. 99.95 -> 2000 x 2000
    m = board(parts=(), outline=outline(0, 0, 99.95, 99.95))
    g = build_grid(m, req(a=(1.0, 1.0), b=(98.95, 98.95)), window_mm=200.0)
    assert g.w * g.h == MAX_CELLS_PER_LAYER


def test_segment_clear_exact() -> None:
    m = model_with(pad("OTHER", 10.0, 10.0))
    g = build_grid(m, req())
    # horizontal line 0.3 above the pad's top edge (y = 9.5): exactly the need
    assert g.segment_clear(0, (8.0, 9.2), (12.0, 9.2))
    assert not g.segment_clear(0, (8.0, 9.21), (12.0, 9.21))
    assert not g.segment_clear(0, (0.2, 5.0), (3.0, 5.0))  # inside the edge inset


def test_build_grid_deadline_with_many_via_obstacles() -> None:
    import time

    from copperchat.board.router.grid import DeadlineExpired

    pads = tuple(
        Pad(
            f"U{k}",
            "1",
            f"N{k}",
            5.0 + (k % 200) * 0.1,
            5.0 + (k // 200) * 0.05,
            0.05,
            0.05,
            0.0,
            "rect",
            frozenset({"B.Cu"}),
            True,
            None,
        )
        for k in range(40_000)
    )
    m = board(parts=(part("U", 10, 10, pads=pads),), outline=outline(0, 0, 30, 20))
    r = req(a=(5.0, 3.0), b=(25.0, 3.0), layers=("F.Cu", "B.Cu"), allow_vias=True)
    t = time.monotonic()
    with pytest.raises(DeadlineExpired):
        build_grid(m, r, deadline=time.monotonic() + 0.2)
    assert time.monotonic() - t < 3.0


def test_escape_map_marks_unrouted_other_net_pads() -> None:
    """R4: the escape cost map covers the strip in front of another net's still unconnected
    pad (both ends of its long axis, every layer for an SMD pad), not the own net's pads,
    not a pad that already has copper, and nothing with `escape=False`."""
    from dataclasses import replace as _replace

    from copperchat.board.router.grid import ESCAPE_REACH_MM, _escape_rect

    from .builders import track
    from .builders_circuit import circuit, pad

    a = pad("U1", "1", "P", (10.0, 10.0))
    b = pad("U2", "1", "P", (16.0, 10.0))
    other = pad("U3", "1", "Q", (13.0, 12.0), w=1.2, h=0.3)  # long axis x
    done = pad("U4", "1", "R", (13.0, 8.0), w=1.2, h=0.3)
    m = circuit(
        part("U1", 10.0, 10.0, pads=(a,)),
        part("U2", 16.0, 10.0, pads=(b,)),
        part("U3", 13.0, 12.0, pads=(other,)),
        part("U4", 13.0, 8.0, pads=(done,)),
        tracks=(track("R", "F.Cu", (13.0, 8.0), (13.0, 6.0), 0.2),),
        copper_layers=("F.Cu", "B.Cu"),
    )
    req = RouteRequest("P", a, b, 0.2, 0.2, ("F.Cu", "B.Cu"), True, (0.6, 0.3))
    grid = build_grid(m, req)
    assert grid.escape and len(grid.escape) == 2

    def marked(p: tuple[float, float], k: int = 0) -> bool:
        i, j = grid.cell(*p)
        return bool(grid.escape[k][j * grid.w + i])

    end = 13.0 + 0.6 + ESCAPE_REACH_MM / 2  # in front of U3's right end
    assert marked((end, 12.0)) and marked((end, 12.0), k=1)  # SMD: every layer
    assert not marked((13.0 + 0.6 + ESCAPE_REACH_MM / 2, 8.0))  # U4 has copper
    assert not marked((10.8, 10.0))  # own net
    zone = _escape_rect(other, 0.2)
    xs = [q[0] for q in zone]
    assert max(xs) == pytest.approx(13.0 + 0.6 + ESCAPE_REACH_MM)
    assert build_grid(m, _replace(req, escape=False)).escape == ()
