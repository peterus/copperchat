"""Round 2 R3 Task 2: the own router narrows a wide class track to the pad-limited width
inside a fine-pitch part's escape zone (spec §22.4), and only there."""

from __future__ import annotations

from dataclasses import replace

import pytest

from copperchat.board.classify import classify
from copperchat.board.geom import escape_zone, point_in_polygon
from copperchat.board.metrics import compute_metrics
from copperchat.board.model import BoardModel, Pad, Track
from copperchat.board.router import RouteRequest, RouteTooLarge, build_grid, solve
from copperchat.board.router import grid as grid_mod
from copperchat.board.router.simplify import neck_pieces
from copperchat.kicad_tools.board import route

from .builders import net_class, outline, part
from .builders_circuit import circuit, pad

PITCH = 0.5  # QFN pitch
CLASS_W = 0.5


def _qfn(net_pins: dict[int, str] | None = None) -> tuple[list[Pad], object]:
    """A QFN-like part U1 at (10, 10): five 0.8 x 0.25 pads on its left side, 0.5 mm pitch
    (y 9.0 .. 11.0, x 8.6), courtyard 4 x 4 mm. Pin 3 (y = 10) is net P."""
    nets = net_pins or {}
    pads = [
        pad("U1", str(k), nets.get(k, f"S{k}"), (8.6, 9.0 + (k - 1) * PITCH), w=0.8, h=0.25)
        for k in range(1, 6)
    ]
    pads[2] = replace(pads[2], net="P")
    return pads, part("U1", 10.0, 10.0, pads=tuple(pads))


def _board(*, class_w: float = CLASS_W, wide_pad: bool = False) -> BoardModel:
    pads, u1 = _qfn()
    if wide_pad:  # an 0805-like pad instead of the fine-pitch one
        pads[2] = replace(pads[2], w=1.2, h=1.3)
        u1 = replace(u1, pads=tuple(pads))
    c1 = pad("C1", "1", "P", (4.0, 10.0), w=1.0, h=1.0)
    return circuit(
        u1,
        part("C1", 4.0, 10.0, half=0.8, pads=(c1,)),
        outline=outline(0, 0, 20, 20),
        net_classes=(net_class("Default", track_width=class_w, clearance=0.2),),
    )


def _req(m: BoardModel, *, neck: bool = True, min_track: float = 0.127) -> RouteRequest:
    plans = route.plan_connections(
        m, classify(m), ["P"], layers=("F.Cu",), allow_vias=False, min_track=min_track
    )
    (p,) = plans
    return p.req if neck else replace(p.req, neck=())


def _zone(m: BoardModel) -> tuple:
    return escape_zone(next(p for p in m.parts if p.ref == "U1"))


def test_neck_reaches_fine_pitch_pad() -> None:
    m = _board()
    req = _req(m)
    assert solve(m, replace(req, neck=())) is None  # 0.5 mm cannot reach the 0.25 mm pad
    path = solve(m, req)
    assert path is not None
    zone = _zone(m)
    widths = {w for _l, _p, _q, w in path.segments}
    assert widths == {0.25, CLASS_W}
    for _l, p, q, w in path.segments:
        mid = ((p[0] + q[0]) / 2, (p[1] + q[1]) / 2)
        assert (w == 0.25) == point_in_polygon(mid, list(zone)), (p, q, w)


def test_neck_only_in_zone() -> None:
    """No piece narrower than the class width has a point outside the escape zone (the
    width switches at every boundary crossing)."""
    m = _board()
    path = solve(m, _req(m))
    assert path is not None
    zone = list(_zone(m))
    for _l, p, q, w in path.segments:
        if w < CLASS_W:
            assert point_in_polygon(p, zone) and point_in_polygon(q, zone), (p, q, w)


def test_neck_pieces_switch_at_every_crossing() -> None:
    """A segment through a zone, out and in again: one width change per crossing."""
    z1 = (((1.0, -1.0), (2.0, -1.0), (2.0, 1.0), (1.0, 1.0)), 0.2, ("F.Cu",))
    z2 = (((3.0, -1.0), (4.0, -1.0), (4.0, 1.0), (3.0, 1.0)), 0.2, ("F.Cu",))
    got = neck_pieces((0.0, 0.0), (5.0, 0.0), (z1, z2), 0.5)
    assert [(p[0], q[0], w) for p, q, w in got] == [
        (0.0, 1.0, 0.5),
        (1.0, 2.0, 0.2),
        (2.0, 3.0, 0.5),
        (3.0, 4.0, 0.2),
        (4.0, 5.0, 0.5),
    ]
    assert neck_pieces((0.0, 0.0), (5.0, 0.0), (), 0.5) == [((0.0, 0.0), (5.0, 0.0), 0.5)]


def test_neck_never_below_min_track() -> None:
    m = _board()
    req = _req(m, min_track=0.3)
    assert {nw for _z, nw, _l in req.neck} == {0.3}
    path = solve(m, req)
    assert path is None or min(w for *_x, w in path.segments) >= 0.3


def test_no_neck_when_pad_wide_enough() -> None:
    m = _board(wide_pad=True)
    assert _req(m).neck == ()


def test_plan_sets_neck_for_fine_pitch_only() -> None:
    m = _board()
    req = _req(m)
    ((zone, nw, zone_layers),) = req.neck
    assert zone_layers == ("F.Cu",)
    assert nw == 0.25
    # the escape zone, inset by the 2 µm rounding margin
    ez = _zone(m)
    assert zone[0] == pytest.approx((ez[0][0] + 0.002, ez[0][1] + 0.002))
    # a narrow class needs no neck
    assert _req(_board(class_w=0.2)).neck == ()


def test_neck_result_passes_m12() -> None:
    """R1's M12 on the routed copper: the neck-down pieces are no offenders."""
    m = _board()
    req = _req(m)
    path = solve(m, req)
    assert path is not None
    tracks = tuple(
        Track(f"t{k}", "P", la, p, q, w, True) for k, (la, p, q, w) in enumerate(path.segments)
    )
    routed = replace(m, tracks=tracks)
    from copperchat.board.boardmd import BoardDoc

    rep = compute_metrics(
        routed,
        classify(routed),
        BoardDoc(),
        drc=None,
        parity=None,
        preset=None,
        only=frozenset({"M12"}),
    )
    m12 = next(x for x in rep.metrics if x.id == "M12")
    assert [o for o in m12.offenders if o.what.startswith("track_width")] == []


def test_neck_map_bounded() -> None:
    """The neck map covers the zones' node boxes only, and counts against the cell cap."""
    m = _board()
    req = _req(m)
    grid = build_grid(m, req)
    ((zone, _nw, _l),) = req.neck
    xs, ys = [p[0] for p in zone], [p[1] for p in zone]
    box = (round((max(xs) - min(xs)) / grid.pitch) + 2) * (
        round((max(ys) - min(ys)) / grid.pitch) + 2
    )
    assert 0 < grid.neck_cells <= box
    assert build_grid(m, replace(req, neck=())).neck_cells == 0


def test_neck_map_counts_against_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _board()
    req = _req(m)
    plain = build_grid(m, replace(req, neck=()))
    monkeypatch.setattr(grid_mod, "MAX_CELLS_PER_LAYER", plain.w * plain.h + 10)
    build_grid(m, replace(req, neck=()))
    with pytest.raises(RouteTooLarge):
        build_grid(m, req)


def test_neck_only_on_the_pads_layers() -> None:
    """ODH probe: a via inside the escape zone took the neck to B.Cu, which M12 fails (the
    pad is on F.Cu only). The neck applies on the end pad's layers only."""
    from copperchat.board.router.grid import neck_layers

    m = replace(_board(), copper_layers=("F.Cu", "B.Cu"))
    req = replace(_req(m), layers=("F.Cu", "B.Cu"), allow_vias=True)
    ((_zone, _nw, zone_layers),) = req.neck
    assert neck_layers(zone_layers, req.layers) == frozenset({0})
    plain = build_grid(m, replace(req, neck=()))
    necked = build_grid(m, req)
    assert necked.cells[1] == plain.cells[1]  # B.Cu untouched
    assert necked.cells[0] != plain.cells[0]


def test_neck_not_on_the_wide_pads_layer() -> None:
    """Verify-pass finding: a wide decap pad on B.Cu under the F.Cu QFN (centre inside the
    QFN's escape zone) must not take the neck to B.Cu - M12 would fail it there."""
    from copperchat.board.router.grid import neck_layers

    m = replace(_board(), copper_layers=("F.Cu", "B.Cu"))
    req = replace(_req(m), layers=("F.Cu", "B.Cu"), allow_vias=True)
    wide = req.a if req.a.ref == "C1" else req.b
    decap = replace(wide, x=10.0, y=10.0, layers=frozenset({"B.Cu"}))  # wide, under U1
    req = replace(req, a=decap) if req.a.ref == "C1" else replace(req, b=decap)
    ((_zone, _nw, zone_layers),) = req.neck
    assert neck_layers(zone_layers, req.layers) == frozenset({0})


def test_neck_entry_carries_its_own_pads_layers() -> None:
    """Verify-2 finding: a NARROW 0.3 mm pad of another part on B.Cu under the F.Cu QFN
    (centre inside the QFN's escape zone) must not take the QFN zone's neck to B.Cu: each
    neck entry (built by `neck_zones`) applies only on the layers of its own pad. A foreign
    B.Cu track inside the QFN zone shows it: its B.Cu blockage stays at class width."""
    from .builders import track

    pads, u1 = _qfn()
    qfn_pad = pads[2]  # U1.3, net P, F.Cu
    narrow = pad("C9", "1", "P", (10.0, 10.0), w=0.3, h=0.3)
    narrow = replace(narrow, layers=frozenset({"B.Cu"}))
    c9 = part("C9", 10.0, 10.0, half=0.3, pads=(narrow,))
    foreign = track("Q", "B.Cu", (8.0, 11.6), (9.2, 11.6), 0.2)  # in U1's zone, not C9's
    m = circuit(
        u1,
        c9,
        outline=outline(0, 0, 20, 20),
        tracks=(foreign,),
        copper_layers=("F.Cu", "B.Cu"),
        net_classes=(net_class("Default", track_width=CLASS_W, clearance=0.2),),
    )
    neck = route.neck_zones(m, (qfn_pad, narrow), CLASS_W, 0.127)
    assert sorted(z[2] for z in neck) == [("B.Cu",), ("F.Cu",)]
    req = RouteRequest(
        "P", qfn_pad, narrow, CLASS_W, 0.2, ("F.Cu", "B.Cu"), True, (0.6, 0.3), neck=neck
    )
    plain = build_grid(m, replace(req, neck=()))
    necked = build_grid(m, req)
    (c9_zone,) = [z[0] for z in neck if z[2] == ("B.Cu",)]
    xs, ys = [q[0] for q in c9_zone], [q[1] for q in c9_zone]
    changed = 0
    for j in range(necked.h):
        for i in range(necked.w):
            x, y = necked.xy(i, j)
            n = j * necked.w + i
            if necked.cells[1][n] != plain.cells[1][n]:
                changed += 1
                assert min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys), (x, y)
    # the QFN's F.Cu cells did neck down (the entry is not dead)
    assert necked.cells[0] != plain.cells[0]


def test_neck_zones_merge_layers_of_one_part() -> None:
    """Two narrow pads of one part (same zone, same neck) share one entry with both pads'
    layers."""
    m = replace(_board(), copper_layers=("F.Cu", "B.Cu"))
    u1 = next(p for p in m.parts if p.ref == "U1")
    a, b = u1.pads[0], replace(u1.pads[1], layers=frozenset({"B.Cu"}))
    ((_z, _nw, on),) = route.neck_zones(m, [a, b], CLASS_W, 0.127)
    assert on == ("F.Cu", "B.Cu")
