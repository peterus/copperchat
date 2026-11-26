"""B-C5: the one bend definition (M4/M7) - analyse_bends, polylines, analyse_tracks."""

from __future__ import annotations

import itertools
import math
import random

from copperchat.board.geom import (
    ANGLE_TOL_DEG,
    CHAMFER_MIN,
    JOG_MIN,
    BendReport,
    Point,
    analyse_bends,
    analyse_tracks,
    polylines,
)
from copperchat.board.model import ArcTrack, Pad
from tests.unit.board.builders import board, part, track, via

FAILURES = ("non_octilinear", "ninety", "acute", "off_angle", "t_junctions", "micro_jogs")
ALL_FAILURES = (*FAILURES, "short_chamfers", "y_junctions")


def walk(start: Point, heading: float, steps: list[tuple[float, float]]) -> list[Point]:
    """Polyline from `start`: each step turns by the given degrees, then runs `length` mm.
    Board coordinates; the first step's turn is relative to `heading`."""
    pts = [start]
    h = heading
    for turn, length in steps:
        h += turn
        x, y = pts[-1]
        pts.append((x + length * math.cos(math.radians(h)), y + length * math.sin(math.radians(h))))
    return pts


def failures(r: BendReport) -> dict[str, int]:
    return {f: len(getattr(r, f)) for f in ALL_FAILURES if getattr(r, f)}


NO_PAD = (False, False)


def test_constants() -> None:
    assert ANGLE_TOL_DEG == 0.5
    assert CHAMFER_MIN(0.1) == 0.25
    assert CHAMFER_MIN(0.4) == 0.4
    assert JOG_MIN(0.2) == 0.4


def test_straight_ok() -> None:
    r = analyse_bends([(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)], 0.2, pad_ends=NO_PAD)
    assert failures(r) == {}
    assert r.bends == 0


def test_ninety_fails() -> None:
    r = analyse_bends([(0.0, 0.0), (5.0, 0.0), (5.0, 5.0)], 0.2, pad_ends=NO_PAD)
    assert len(r.ninety) == 1
    assert r.ninety[0] == (5.0, 0.0)
    assert r.bends == 0


def test_valid_chamfer_passes() -> None:
    r = analyse_bends(walk((0, 0), 0, [(0, 2), (45, 0.25), (45, 2)]), 0.2, pad_ends=NO_PAD)
    assert failures(r) == {}
    assert r.bends == 2
    r = analyse_bends(walk((0, 0), 0, [(0, 2), (45, 0.40), (45, 2)]), 0.4, pad_ends=NO_PAD)
    assert failures(r) == {}
    assert r.bends == 2


def test_short_chamfer_fails() -> None:
    r = analyse_bends(walk((0, 0), 0, [(0, 2), (45, 0.10), (45, 2)]), 0.2, pad_ends=NO_PAD)
    assert len(r.short_chamfers) == 1
    r = analyse_bends(walk((0, 0), 0, [(0, 2), (45, 0.39), (45, 2)]), 0.4, pad_ends=NO_PAD)
    assert len(r.short_chamfers) == 1


def test_micro_jog_fails() -> None:
    r = analyse_bends(walk((0, 0), 0, [(0, 2), (45, 0.30), (-45, 2)]), 0.2, pad_ends=NO_PAD)
    assert len(r.micro_jogs) == 1
    assert r.bends == 2
    r = analyse_bends(walk((0, 0), 0, [(0, 2), (45, 0.40), (-45, 2)]), 0.2, pad_ends=NO_PAD)
    assert len(r.micro_jogs) == 0


def test_valid_chamfer_never_micro_jog() -> None:
    for w in (0.1, 0.2, 0.3, 0.5):
        for piece in (0.01, 0.1, 0.3, CHAMFER_MIN(w), 1.0):
            pts = walk((0, 0), 0, [(0, 2), (45, piece), (45, 2)])
            r = analyse_bends(pts, w, pad_ends=NO_PAD)
            assert len(r.micro_jogs) == 0, (w, piece)


def test_acute_135_fails() -> None:
    r = analyse_bends(walk((0, 0), 0, [(0, 2), (135, 2)]), 0.2, pad_ends=NO_PAD)
    assert len(r.acute) == 1
    assert len(r.ninety) == 0
    assert len(r.off_angle) == 0


def test_reversal_is_acute() -> None:
    r = analyse_bends([(0.0, 0.0), (5.0, 0.0), (2.0, 0.0)], 0.2, pad_ends=NO_PAD)
    assert len(r.acute) == 1


def test_vertex_90_8_fails() -> None:
    r = analyse_bends(walk((0, 0), 0, [(0, 2), (90.8, 2)]), 0.2, pad_ends=NO_PAD)
    assert len(r.off_angle) == 1
    assert len(r.ninety) == 0


def test_vertex_60_fails() -> None:
    r = analyse_bends(walk((0, 0), 0, [(0, 2), (60, 2)]), 0.2, pad_ends=NO_PAD)
    assert len(r.off_angle) == 1


def test_vertex_46_fails() -> None:
    r = analyse_bends(walk((0, 0), 0, [(0, 2), (46, 2)]), 0.2, pad_ends=NO_PAD)
    assert len(r.off_angle) == 1
    assert r.bends == 0


def test_vertex_45_4_is_bend() -> None:
    r = analyse_bends(walk((0, 0), 0, [(0, 2), (45.4, 2)]), 0.2, pad_ends=NO_PAD)
    assert failures(r) == {}
    assert r.bends == 1


def test_off_by_0_6_deg_fails() -> None:
    r = analyse_bends(walk((0, 0), 45.6, [(0, 3)]), 0.2, pad_ends=NO_PAD)
    assert len(r.non_octilinear) == 1


def test_off_by_0_4_deg_passes() -> None:
    r = analyse_bends(walk((0, 0), 45.4, [(0, 3)]), 0.2, pad_ends=NO_PAD)
    assert len(r.non_octilinear) == 0


def _pad(x: float, y: float, w: float = 1.0, h: float = 1.0, **kw: object) -> Pad:
    fields: dict[str, object] = {
        "ref": "U1",
        "number": "1",
        "net": "N1",
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "rot": 0.0,
        "shape": "rect",
        "layers": frozenset({"F.Cu", "F.Mask", "F.Paste"}),
        "smd": True,
        "drill": None,
    }
    fields.update(kw)
    return Pad(**fields)  # type: ignore[arg-type]


def _t_board(branch_end: Point, pads: tuple[Pad, ...] = ()) -> object:
    parts = (part("U1", 50, 50, pads=pads),) if pads else ()
    return board(
        parts=parts,
        tracks=(
            track(start=(10.0, 10.0), end=(20.0, 10.0), kiid="a"),
            track(start=(15.0, 10.0), end=branch_end, kiid="b"),
        ),
    )


def test_t_junction_90_reported_not_m4() -> None:
    # still in t_junctions (P3a semantics for chamfer/route/fill); M4 ignores it (R1)
    r = analyse_tracks(_t_board((15.0, 15.0)))  # type: ignore[arg-type]
    assert len(r.t_junctions) == 1
    assert r.t_junctions[0] == (15.0, 10.0)
    assert r.y_junctions == ()
    assert len(r.ninety) == 0


def test_t_junction_45_is_y() -> None:
    r = analyse_tracks(_t_board((18.0, 13.0)))  # type: ignore[arg-type]
    assert len(r.t_junctions) == 0
    assert r.y_junctions == ((15.0, 10.0),)
    assert failures(r) == {"y_junctions": 1}


def test_y_with_ninety_pair_in_both() -> None:
    # headings (0, 90, 225) from the node: a 90° pair (old T) and off the 90° lattice (Y)
    m = board(
        tracks=(
            track(start=(15.0, 10.0), end=(20.0, 10.0), kiid="a"),
            track(start=(15.0, 10.0), end=(15.0, 15.0), kiid="b"),
            track(start=(15.0, 10.0), end=(12.0, 7.0), kiid="c"),
        )
    )
    r = analyse_tracks(m)
    assert r.t_junctions == ((15.0, 10.0),)
    assert r.y_junctions == ((15.0, 10.0),)


def test_t_junction_inside_pad_ok() -> None:
    r = analyse_tracks(_t_board((15.0, 15.0), (_pad(15.0, 10.0),)))  # type: ignore[arg-type]
    assert len(r.t_junctions) == 0


def test_pad_entry_only_exempt_at_pad() -> None:
    pts = walk((0, 0), 0, [(0, 0.10), (45, 0.10), (-45, 2)])
    r = analyse_bends(pts, 0.2, pad_ends=(True, False))
    assert len(r.micro_jogs) == 0
    assert r.bends == 1
    r = analyse_bends(pts, 0.2, pad_ends=(False, False))
    assert len(r.micro_jogs) == 1
    assert r.bends == 2
    # a 90 degree change at the pad-entry vertex is still reported
    r = analyse_bends(walk((0, 0), 0, [(0, 0.10), (90, 2)]), 0.2, pad_ends=(True, False))
    assert len(r.ninety) == 1
    # a short chamfer at vertices 2-3 counted from the pad end is reported
    pts = walk((0, 0), 0, [(0, 0.10), (45, 1.0), (45, 0.10), (45, 2)])
    r = analyse_bends(pts, 0.2, pad_ends=(True, False))
    assert len(r.short_chamfers) == 1
    assert r.bends == 2
    # the exemption also applies from the far end
    r = analyse_bends(
        list(reversed(walk((0, 0), 0, [(0, 0.1), (45, 0.1), (-45, 2)]))),
        0.2,
        pad_ends=(False, True),
    )
    assert len(r.micro_jogs) == 0


def test_polylines_join_split() -> None:
    m = board(
        tracks=(
            track(start=(10.0, 10.0), end=(15.0, 10.0), kiid="a"),
            track(start=(15.0, 10.0), end=(20.0, 10.0), kiid="b"),
        )
    )
    assert polylines(m, "N1", "F.Cu") == [(((10.0, 10.0), (20.0, 10.0)), 0.2)]
    assert polylines(m, "N1", "B.Cu") == []
    assert polylines(m, "N2", "F.Cu") == []
    m2 = board(tracks=m.tracks, vias=(via(at=(15.0, 10.0)),))
    assert polylines(m2, "N1", "F.Cu") == [
        (((10.0, 10.0), (15.0, 10.0)), 0.2),
        (((15.0, 10.0), (20.0, 10.0)), 0.2),
    ]


def test_polylines_join_tolerance_and_bend() -> None:
    m = board(
        tracks=(
            track(start=(10.0, 10.0), end=(15.0, 10.0), kiid="a"),
            track(start=(15.001, 10.0), end=(17.0, 12.0), kiid="b"),
        )
    )
    out = polylines(m, "N1", "F.Cu")
    assert len(out) == 1
    assert len(out[0][0]) == 3


def test_polylines_split_on_width_change() -> None:
    m = board(
        tracks=(
            track(start=(10.0, 10.0), end=(15.0, 10.0), width=0.2, kiid="a"),
            track(start=(15.0, 10.0), end=(20.0, 10.0), width=0.4, kiid="b"),
        )
    )
    assert [w for _, w in polylines(m, "N1", "F.Cu")] == [0.2, 0.4]


def test_pad_end_rotated_and_wildcard_layer() -> None:
    # a 0.1 mm stub, then a micro jog: exempt only when the end AND the first bend lie in
    # the pad (strict pad-entry rule, P6b): here the bend at x = 0.1 is outside the rotated
    # pad (x -1.95..0.05), so the jog is visible copper and reported
    pts = walk((0, 0), 0, [(0, 0.10), (45, 0.10), (-45, 2)])
    tracks = tuple(
        track(start=a, end=b, kiid=f"t{i}") for i, (a, b) in enumerate(itertools.pairwise(pts))
    )
    th = _pad(-0.95, 0.0, 0.4, 2.0, rot=90.0, layers=frozenset({"*.Cu"}), smd=False, drill=0.3)
    r = analyse_tracks(board(parts=(part("U1", 50, 50, pads=(th,)),), tracks=tracks))
    assert len(r.micro_jogs) == 1
    # starting 0.2 mm deeper, both bends lie under the pad copper: no jog (nodes inside a
    # pad end the polylines, so the bends are not counted at all)
    inner = walk((-0.2, 0), 0, [(0, 0.10), (45, 0.10), (-45, 2)])
    deep = tuple(
        track(start=a, end=b, kiid=f"d{i}") for i, (a, b) in enumerate(itertools.pairwise(inner))
    )
    r = analyse_tracks(board(parts=(part("U1", 50, 50, pads=(th,)),), tracks=deep))
    assert len(r.micro_jogs) == 0 and r.bends == 0
    # rotated it spans x -1.95..0.05; unrotated the 0.4 mm wide pad does not reach x = 0
    th0 = _pad(-0.95, 0.0, 0.4, 2.0, rot=0.0, layers=frozenset({"*.Cu"}), smd=False, drill=0.3)
    r = analyse_tracks(board(parts=(part("U1", 50, 50, pads=(th0,)),), tracks=tracks))
    assert len(r.micro_jogs) == 1
    # a pad of another net does not exempt
    other = _pad(0.0, 0.0, net="N2")
    r = analyse_tracks(board(parts=(part("U1", 50, 50, pads=(other,)),), tracks=tracks))
    assert len(r.micro_jogs) == 1


def test_arc_reported() -> None:
    arc = ArcTrack(
        kiid="arc",
        net="N1",
        layer="F.Cu",
        start=(0.0, 0.0),
        mid=(1.0, 1.0),
        end=(2.0, 0.0),
        width=0.2,
        locked=False,
    )
    r = analyse_tracks(board(arcs=(arc,)))
    assert r.non_octilinear == ((1.0, 1.0),)


def test_random_octilinear_45_only_passes() -> None:
    seed = 7
    rng = random.Random(seed)
    for case in range(300):
        w = rng.choice((0.1, 0.15, 0.2, 0.25, 0.4, 0.6))
        heading = 45.0 * rng.randrange(8)
        steps: list[tuple[float, float]] = [(0.0, rng.uniform(0.0, 5.0))]
        for _ in range(rng.randrange(1, 12)):
            steps.append((rng.choice((-45.0, 45.0)), rng.uniform(JOG_MIN(w) + 0.01, 5.0)))
        pts = walk((rng.uniform(-50, 50), rng.uniform(-50, 50)), heading, steps)
        pad_ends = (rng.random() < 0.5, rng.random() < 0.5)
        r = analyse_bends(pts, w, pad_ends=pad_ends)
        assert failures(r) == {}, f"seed={seed} case={case} w={w} pts={pts}"
        assert r.bends <= len(steps) - 1, f"seed={seed} case={case}"


def _tracks(*pts_widths: tuple[Point, Point, float]) -> tuple[object, ...]:
    return tuple(
        track(start=a, end=b, width=w, kiid=f"t{i}") for i, (a, b, w) in enumerate(pts_widths)
    )


def test_corner_at_width_change_checked() -> None:
    """A polyline split by a width change (neck-down) still has its corner checked."""
    m = board(tracks=_tracks(((0, 0), (10, 0), 0.2), ((10, 0), (10, 10), 0.3)))
    r = analyse_tracks(m)
    assert failures(r) == {"ninety": 1}
    assert r.ninety == ((10.0, 0.0),)
    acute = board(tracks=_tracks(((0, 0), (10, 0), 0.2), ((10, 0), (5, 5), 0.3)))
    assert failures(analyse_tracks(acute)) == {"acute": 1}
    off = board(tracks=_tracks(((0, 0), (10, 0), 0.2), ((10, 0), (15, 3), 0.3)))
    assert failures(analyse_tracks(off)) == {"off_angle": 1, "non_octilinear": 1}
    # a 45-degree neck-down is fine and counts as a bend
    ok = board(tracks=_tracks(((0, 0), (10, 0), 0.2), ((10, 0), (15, 5), 0.3)))
    r = analyse_tracks(ok)
    assert failures(r) == {}
    assert r.bends == 1
    # straight neck-down: no bend
    assert (
        analyse_tracks(board(tracks=_tracks(((0, 0), (10, 0), 0.2), ((10, 0), (20, 0), 0.3)))).bends
        == 0
    )


def test_corner_at_same_layer_via_checked() -> None:
    m = board(
        tracks=_tracks(((0, 0), (10, 0), 0.2), ((10, 0), (10, 10), 0.2)),
        vias=(via(at=(10.0, 0.0)),),
    )
    r = analyse_tracks(m)
    assert failures(r) == {"ninety": 1}
    # the corner inside a same-net pad is a pad entry: not checked
    pad = _pad(10.0, 0.0)
    m = board(
        parts=(part("U1", 50, 50, pads=(pad,)),),
        tracks=_tracks(((0, 0), (10, 0), 0.2), ((10, 0), (10, 10), 0.3)),
    )
    assert failures(analyse_tracks(m)) == {}


def test_off_grid_raw_segments_reported() -> None:
    """Segments 0.3 deg then 0.7 deg off: the 0.4 deg vertex merges them for bend analysis,
    but the 0.7 deg raw segment is still non-octilinear (once)."""
    pts = walk((0, 0), 0.3, [(0, 20), (0.4, 3)])
    r = analyse_bends(pts, 0.2, pad_ends=NO_PAD)
    assert failures(r) == {"non_octilinear": 1}
    tracks = tuple(
        track(start=a, end=b, kiid=f"t{i}") for i, (a, b) in enumerate(itertools.pairwise(pts))
    )
    r = analyse_tracks(board(tracks=tracks))
    assert failures(r) == {"non_octilinear": 1}
    assert r.non_octilinear == (((pts[1][0] + pts[2][0]) / 2, (pts[1][1] + pts[2][1]) / 2),)
    # two raw segments each off by 0.7 deg: both reported, no double count
    pts = walk((0, 0), 0.7, [(0, 5), (0, 5)])
    tracks = tuple(
        track(start=a, end=b, kiid=f"t{i}") for i, (a, b) in enumerate(itertools.pairwise(pts))
    )
    assert failures(analyse_tracks(board(tracks=tracks))) == {"non_octilinear": 2}


def test_closed_loop_every_corner_checked() -> None:
    sq = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
    tracks = tuple(
        track(start=a, end=b, kiid=f"t{i}") for i, (a, b) in enumerate(itertools.pairwise(sq))
    )
    r = analyse_tracks(board(tracks=tracks))
    assert failures(r) == {"ninety": 4}
    # an octagon closes with eight 45-degree bends and no failure
    octo = walk((0, 0), 0, [(0, 4), *[(45, 4)] * 7])
    octo[-1] = octo[0]
    tracks = tuple(
        track(start=a, end=b, kiid=f"t{i}") for i, (a, b) in enumerate(itertools.pairwise(octo))
    )
    r = analyse_tracks(board(tracks=tracks))
    assert failures(r) == {}
    assert r.bends == 8
