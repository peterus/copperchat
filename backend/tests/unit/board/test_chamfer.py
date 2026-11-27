"""P6a: Freerouting post-processor (merge, chamfer, rip list) - B-C12 `postprocess`."""

from __future__ import annotations

import itertools
import math
import random
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import replace

import pytest

from copperchat.board.geom import (
    CHAMFER_MIN,
    BendReport,
    Point,
    analyse_bends,
    analyse_tracks,
    pad_entry_ends,
    polylines,
)
from copperchat.board.metrics import routed_length
from copperchat.board.model import ArcTrack, BoardModel, Pad, Track
from copperchat.board.router import chamfer
from copperchat.board.router.chamfer import PostResult, postprocess
from tests.unit.board.builders import board, net_class, part, track, via

S2 = math.sqrt(2.0)
FAILS = ("non_octilinear", "ninety", "acute", "off_angle", "micro_jogs", "short_chamfers")
KEY = "N1:J1.1-J2.1"


def pad(ref: str, x: float, y: float, *, net: str = "N1", size: float = 0.5) -> Pad:
    return Pad(
        ref=ref,
        number="1",
        net=net,
        x=x,
        y=y,
        w=size,
        h=size,
        rot=0.0,
        shape="rect",
        layers=frozenset({"F.Cu", "F.Paste", "F.Mask"}),
        smd=True,
        drill=None,
    )


def pad_part(ref: str, x: float, y: float, *, net: str = "N1", size: float = 0.5) -> object:
    return part(ref, x, y, half=0.5, pads=(pad(ref, x, y, net=net, size=size),))


def path_tracks(
    pts: Sequence[Point], width: float = 0.2, *, net: str = "N1", prefix: str = "t"
) -> tuple[Track, ...]:
    return tuple(
        track(net, "F.Cu", a, b, width, kiid=f"{prefix}{i}")
        for i, (a, b) in enumerate(itertools.pairwise(pts))
    )


def routed(
    pts: Sequence[Point],
    width: float = 0.2,
    *,
    extra_parts: Sequence[object] = (),
    extra_tracks: Sequence[Track] = (),
    vias: Sequence[object] = (),
    pads: bool = True,
) -> BoardModel:
    """A board with net N1 routed along `pts` (pads J1.1 / J2.1 at the ends)."""
    parts = [pad_part("J1", *pts[0]), pad_part("J2", *pts[-1])] if pads else []
    return board(
        parts=(*parts, *extra_parts),
        tracks=(*path_tracks(pts, width), *extra_tracks),
        vias=tuple(vias),
        net_class_of=(("N1", "Default"), ("N2", "Default")),
    )


def kiids(m: BoardModel, prefix: str = "t") -> frozenset[str]:
    return frozenset(t.kiid for t in m.tracks if t.kiid.startswith(prefix))


def apply(m: BoardModel, r: PostResult) -> BoardModel:
    rep = dict(r.replace)
    out: list[Track] = []
    for t in m.tracks:
        out.extend(rep.get(t.kiid, (t,)))
    return replace(m, tracks=tuple(out))


def fails(rep: BendReport) -> dict[str, int]:
    return {f: len(getattr(rep, f)) for f in FAILS if getattr(rep, f)}


def walk(start: Point, heading: float, steps: Sequence[tuple[float, float]]) -> list[Point]:
    """Points from `start`: each step turns by degrees (board coords), then runs mm."""
    pts = [start]
    h = heading
    for turn, length in steps:
        h += turn
        x, y = pts[-1]
        pts.append((x + length * math.cos(math.radians(h)), y + length * math.sin(math.radians(h))))
    return pts


def nm(v: float) -> float:
    """`v` mm rounded to KiCad's integer nanometres."""
    return round(v * 1e6) / 1e6


def nm_pt(p: Point) -> Point:
    return (nm(p[0]), nm(p[1]))


def nm_model(m: BoardModel) -> BoardModel:
    """`m` with every track endpoint rounded to nanometres (as KiCad stores it)."""
    return replace(
        m, tracks=tuple(replace(t, start=nm_pt(t.start), end=nm_pt(t.end)) for t in m.tracks)
    )


def on_nm_grid(t: Track) -> bool:
    return all(abs(v * 1e6 - round(v * 1e6)) < 1e-6 for p in (t.start, t.end) for v in p)


def chamfers(r: PostResult) -> list[Track]:
    """The chamfers `r` adds (each follows the shortened track in its replace entry)."""
    return [t for _, new in r.replace for t in new[1:]]


def diagonals(r: PostResult) -> list[Track]:
    return [
        t
        for _, new in r.replace
        for t in new
        if abs(abs(t.end[0] - t.start[0]) - abs(t.end[1] - t.start[1])) < 1e-9 and t.start != t.end
    ]


L_PTS: list[Point] = [(10.0, 10.0), (15.0, 10.0), (15.0, 15.0)]


def test_ninety_corner_chamfered() -> None:
    m = routed(L_PTS)
    r = postprocess(m, kiids(m))
    assert r.chamfered == 1
    assert r.rip == ()
    rep = dict(r.replace)
    a = math.ceil(0.6 / S2 * 1e6) / 1e6  # the offset, rounded up to whole nanometres
    (t_in,) = rep["t0"]
    t_out, diag = rep["t1"]
    assert t_in.start == (10.0, 10.0)
    assert math.dist(t_in.end, (15.0 - a, 10.0)) < 1e-12
    assert math.dist(t_out.start, (15.0, 10.0 + a)) < 1e-12
    assert t_out.end == (15.0, 15.0)
    assert 0.6 <= math.dist(diag.start, diag.end) <= 0.6 + 2e-6
    assert on_nm_grid(diag)
    assert {diag.start, diag.end} == {t_in.end, t_out.start}
    for t in (t_in, t_out, diag):
        assert (t.kiid, t.net, t.layer, t.width, t.locked) == ("", "N1", "F.Cu", 0.2, False)
    heading = math.degrees(math.atan2(diag.end[1] - diag.start[1], diag.end[0] - diag.start[0]))
    assert math.isclose(abs(heading) % 90, 45.0, abs_tol=1e-6)
    out = apply(m, r)
    (pl,) = polylines(out, "N1", "F.Cu")
    rb = analyse_bends(pl[0], pl[1], pad_ends=(True, True))
    assert rb.ninety == rb.acute == rb.off_angle == rb.micro_jogs == rb.short_chamfers == ()
    assert rb.non_octilinear == ()
    assert fails(analyse_tracks(out)) == {}


def _corner_c(r: PostResult) -> float:
    (d,) = diagonals(r)
    return math.dist(d.start, d.end)


def test_chamfer_shrinks_near_obstacle() -> None:
    # N2 pad 0.5 x 0.5 inside the corner; its nearest corner lies on the corner bisector
    # 0.52 mm from the vertex. Inflated diagonal (w/2 + clearance 0.2) reaches c/2 + 0.3:
    # c = 0.6 -> 0.6, 0.5 -> 0.55 (overlap), 0.4 -> 0.5 (clear) => c = 2w.
    off = 0.52 / S2
    cx, cy = 15.0 - off - 0.25, 10.0 + off + 0.25
    m = routed(L_PTS, extra_parts=(pad_part("U9", cx, cy, net="N2"),))
    r = postprocess(m, kiids(m))
    assert r.rip == ()
    assert r.chamfered == 1
    assert 0.4 <= _corner_c(r) <= 0.4 + 2e-6


def test_chamfer_min_when_3w_too_small() -> None:
    m = routed(L_PTS, width=0.05)
    r = postprocess(m, kiids(m))
    assert r.chamfered == 1
    assert 0.25 <= _corner_c(r) <= 0.25 + 2e-6
    assert CHAMFER_MIN(0.05) == 0.25


def test_chamfer_blocked_rips() -> None:
    # an N2 pad right inside the corner: even c = CHAMFER_MIN overlaps it
    m = routed(L_PTS, extra_parts=(pad_part("U9", 14.6, 10.4, net="N2", size=0.3),))
    r = postprocess(m, kiids(m))
    assert r.rip == (KEY,)
    assert (KEY, "ninety_unfixable") in r.reasons
    assert r.replace == ()
    assert r.chamfered == 0


def test_short_leg_rips() -> None:
    # (a) an L whose legs (0.15 mm) are shorter than CHAMFER_MIN(w)/sqrt2 = 0.177 mm
    m = routed([(10.0, 10.0), (10.15, 10.0), (10.15, 10.15)], pads=False)
    m = replace(
        m, parts=(pad_part("J1", 10.0, 10.0, size=0.05), pad_part("J2", 10.15, 10.15, size=0.05))
    )
    r = postprocess(m, kiids(m))
    assert r.rip == (KEY,)
    assert (KEY, "ninety_unfixable") in r.reasons
    assert r.replace == ()
    # (b) a 0.2 mm leg between two 90-degree corners (w = 0.2): two chamfers do not fit
    # on it (offsets >= 2 x 0.177 mm) and one alone leaves a 90-degree corner
    for pts in (
        [(10.0, 10.0), (15.0, 10.0), (15.0, 10.2), (20.0, 10.2)],  # Z (opposite sense)
        [(10.0, 10.0), (15.0, 10.0), (15.0, 10.2), (10.0, 10.2)],  # U (same sense)
    ):
        m = routed(pts)
        r = postprocess(m, kiids(m))
        assert r.rip == (KEY,), pts
        assert (KEY, "ninety_unfixable") in r.reasons
        assert r.replace == ()


def _reasons(pts: Sequence[Point], width: float = 0.2) -> set[str]:
    m = routed(pts, width)
    r = postprocess(m, kiids(m))
    assert r.rip == (KEY,)
    return {why for key, why in r.reasons if key == KEY}


def test_acute_detected() -> None:
    assert "acute" in _reasons(walk((10, 10), 0, [(0, 5), (135, 4)]))


def test_off_angle_detected() -> None:
    assert "off_angle" in _reasons(walk((10, 10), 0, [(0, 5), (60, 4)]))


def test_non_octilinear_detected() -> None:
    assert _reasons(walk((10, 10), 0, [(30, 5)])) == {"non_octilinear"}


def test_micro_jog_detected() -> None:
    # the pair sits between two other bends (bends next to a pad entry are exempt)
    steps = [(0, 5), (45, 2), (-45, 3), (45, 0.3), (-45, 3), (45, 2), (-45, 5)]
    assert _reasons(walk((10, 10), 0, steps)) == {"micro_jog"}


def _pair_steps(piece: float) -> list[tuple[float, float]]:
    """A same-sense 45-degree pair with `piece` mm between, between two other bends."""
    return [(0, 5), (45, 2), (-45, 3), (45, piece), (45, 3), (-45, 2), (-45, 5)]


def test_short_chamfer_detected() -> None:
    assert _reasons(walk((10, 10), 0, _pair_steps(0.15))) == {"short_chamfer"}


def test_valid_chamfer_untouched() -> None:
    m = routed(walk((10, 10), 0, _pair_steps(0.251)))
    r = postprocess(m, kiids(m))
    assert r == PostResult(replace=(), rip=(), reasons=(), merged=0, chamfered=0)


def test_short_chamfer_after_nm_rounding_rips() -> None:
    # a piece of exactly CHAMFER_MIN in floats is shorter once KiCad rounds it to nanometres
    m = routed(walk((10, 10), 0, _pair_steps(0.25)))
    r = postprocess(m, kiids(m))
    assert r.reasons == ((KEY, "short_chamfer"),)


def test_merge_collinear() -> None:
    pts = [(10.0, 10.0), (12.0, 10.0), (15.0, 10.0), (20.0, 10.0)]
    m = routed(pts)
    r = postprocess(m, kiids(m))
    assert r.merged == 2
    assert r.rip == ()
    rep = dict(r.replace)
    assert rep["t1"] == () and rep["t2"] == ()
    (merged,) = rep["t0"]
    assert (merged.start, merged.end) == ((10.0, 10.0), (20.0, 10.0))
    assert (merged.kiid, merged.width, merged.locked) == ("", 0.2, False)


def _xnet_board(*, both_ends: bool) -> BoardModel:
    """N1 F.Cu split at (5, 0); N2 B.Cu split at (5, 5) with its ends at (5, 0) and (5, 10)
    - on another net/layer than N1's joint (and N3's joint at (5, 10) when `both_ends`)."""
    tracks = [
        track("N1", "F.Cu", (0.0, 0.0), (5.0, 0.0), 0.2, kiid="a0"),
        track("N1", "F.Cu", (5.0, 0.0), (10.0, 0.0), 0.2, kiid="a1"),
        track("N2", "B.Cu", (5.0, 0.0), (5.0, 5.0), 0.2, kiid="b0"),
        track("N2", "B.Cu", (5.0, 5.0), (5.0, 10.0), 0.2, kiid="b1"),
    ]
    parts = [pad_part("J1", 0.0, 0.0), pad_part("J2", 10.0, 0.0)]
    if both_ends:
        tracks += [
            track("N3", "F.Cu", (0.0, 10.0), (5.0, 10.0), 0.2, kiid="c0"),
            track("N3", "F.Cu", (5.0, 10.0), (10.0, 10.0), 0.2, kiid="c1"),
        ]
        parts += [pad_part("K1", 0.0, 10.0, net="N3"), pad_part("K2", 10.0, 10.0, net="N3")]
    return board(
        parts=tuple(parts),
        tracks=tuple(tracks),
        net_class_of=(("N1", "Default"), ("N2", "Default"), ("N3", "Default")),
    )


@pytest.mark.parametrize("both_ends", [False, True])
def test_merge_joints_per_net_and_layer(both_ends: bool) -> None:
    # another net's/layer's merge joint at a chain's outer end must not drop that end
    m = _xnet_board(both_ends=both_ends)
    r = postprocess(m, frozenset(t.kiid for t in m.tracks))
    assert r.rip == ()
    rep = dict(r.replace)
    assert rep["a1"] == () and rep["b1"] == ()
    (a,) = rep["a0"]
    (b,) = rep["b0"]
    assert (a.net, a.layer, a.start, a.end) == ("N1", "F.Cu", (0.0, 0.0), (10.0, 0.0))
    assert (b.net, b.layer, b.start, b.end) == ("N2", "B.Cu", (5.0, 0.0), (5.0, 10.0))
    if both_ends:
        assert rep["c1"] == ()
        (c,) = rep["c0"]
        assert (c.net, c.layer, c.start, c.end) == ("N3", "F.Cu", (0.0, 10.0), (10.0, 10.0))
    assert r.merged == (3 if both_ends else 2)


def test_merge_stops_at_via() -> None:
    pts = [(10.0, 10.0), (15.0, 10.0), (20.0, 10.0)]
    m = routed(pts, vias=(via("N1", (15.0, 10.0)),))
    r = postprocess(m, kiids(m))
    assert r == PostResult(replace=(), rip=(), reasons=(), merged=0, chamfered=0)
    # a third track at the joint stops it too
    stub = track("N1", "F.Cu", (15.0, 10.0), (15.0, 9.0), 0.2, kiid="x-stub")
    m = routed(pts, extra_tracks=(stub,))
    assert postprocess(m, kiids(m)).merged == 0


def test_zero_length_and_duplicate_dropped() -> None:
    pts = [(10.0, 10.0), (20.0, 10.0)]
    zero = track("N1", "F.Cu", (20.0, 10.0), (20.0, 10.0005), 0.2, kiid="tz")
    dup = track("N1", "F.Cu", (20.0, 10.0), (10.0, 10.0), 0.2, kiid="tdup")
    m = routed(pts, extra_tracks=(zero, dup))
    r = postprocess(m, kiids(m))
    assert dict(r.replace) == {"tz": (), "tdup": ()}
    assert r.rip == ()
    assert r.merged == 0


# ---- sub-join-tolerance pieces and gaps (finding: a hidden 90-degree corner) --------------


def _tiny_corner(piece: float) -> BoardModel:
    """L corner at (15, 10) whose first leg ends `piece` mm short of it, the rest a touched
    piece of that length on the leg (dropped when < ZERO_LEN, leaving a gap)."""
    x = 15.0 - piece
    return routed([(10.0, 10.0), (x, 10.0), (15.0, 10.0), (15.0, 15.0)])


@pytest.mark.parametrize("piece", [0.0015, 0.0019, 0.0005])
def test_tiny_corner_piece_dropped_and_chamfered(piece: float) -> None:
    m = _tiny_corner(piece)
    r = postprocess(m, kiids(m))
    rep = dict(r.replace)
    assert rep["t1"] == ()
    assert r.rip == ()
    assert r.chamfered == 1
    out = nm_model(apply(m, r))
    for line, w in polylines(out, "N1", "F.Cu"):
        ends = pad_entry_ends(line, lambda p: _in_end_pad(p, L_PTS))
        assert fails(analyse_bends(line, w, pad_ends=ends)) == {}, line
    assert routed_length(out, *_pads(out)) is not None


def test_tiny_diagonal_corner_piece_dropped() -> None:
    # 45-degree legs; a 0.0011 mm piece continues the first leg up to the corner
    c = (20.0, 20.0)
    a = (c[0] - 3.0, c[1] + 3.0)
    s = (c[0] - 0.000778, c[1] + 0.000778)
    b = (c[0] + 3.0, c[1] + 3.0)
    m = routed([a, s, c, b])
    r = postprocess(m, kiids(m))
    assert dict(r.replace)["t1"] == ()
    assert r.rip == () and r.chamfered == 1


def test_tiny_piece_at_untouched_end_rips() -> None:
    # the untouched stub ends 0.0015 mm short of the corner: closing the gap would move
    # untouched copper, so the piece stays and the hidden corner must be ripped
    stub = track("N1", "F.Cu", (12.0, 10.0), (14.9985, 10.0), 0.2, kiid="u0")
    head = track("N1", "F.Cu", (10.0, 10.0), (12.0, 10.0), 0.2, kiid="t9")
    rest = path_tracks([(14.9985, 10.0), (15.0, 10.0), (15.0, 10.1), (20.0, 10.1)])
    m = board(
        parts=(pad_part("J1", 10.0, 10.0), pad_part("J2", 20.0, 10.1)),
        tracks=(head, stub, *rest),
        net_class_of=(("N1", "Default"),),
    )
    r = postprocess(m, kiids(m))
    assert "t0" not in dict(r.replace)
    assert KEY in r.rip


def test_tiny_piece_safety_net_without_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    # step 3 alone: the polyline walked across the 0.0015 mm piece is checked as a whole
    monkeypatch.setattr(chamfer, "_close_gaps", lambda _m, _u, live, _s: live)
    m = routed([(10.0, 10.0), (14.9985, 10.0), (15.0, 10.0), (15.0, 10.1), (20.0, 10.1)])
    r = postprocess(m, kiids(m))
    assert KEY in r.rip


def test_detour_rips() -> None:
    # pads 10 mm apart, routed 26 mm (8 + 10 + 8) along octilinear-free 90-degree corners
    pts = [(10.0, 10.0), (10.0, 18.0), (20.0, 18.0), (20.0, 10.0)]
    m = routed(pts)
    r = postprocess(m, kiids(m))
    assert r.rip == (KEY,)
    assert set(r.reasons) == {(KEY, "detour")}
    assert r.chamfered == 2


def test_long_straight_not_ripped() -> None:
    # board diagonal 128 mm; pads 80 mm apart, routed 84.1 mm (ratio 1.05)
    pts = [(10.0, 40.0), (15.0, 45.0), (80.0, 45.0), (85.0, 40.0), (90.0, 40.0)]
    m = routed(pts)
    r = postprocess(m, kiids(m))
    assert r.rip == ()
    assert r.reasons == ()


def test_only_given_kiids_touched() -> None:
    corner = path_tracks([(30.0, 30.0), (35.0, 30.0), (35.0, 35.0)], net="N2", prefix="old")
    m = routed(
        [(10.0, 10.0), (20.0, 10.0)],
        extra_tracks=corner,
        extra_parts=(pad_part("U8", 30.0, 30.0, net="N2"), pad_part("U9", 35.0, 35.0, net="N2")),
    )
    r = postprocess(m, kiids(m))
    assert r == PostResult(replace=(), rip=(), reasons=(), merged=0, chamfered=0)
    assert analyse_tracks(apply(m, r)).ninety == ((35.0, 30.0),)


def test_pad_entry_not_chamfered() -> None:
    # the 90-degree corner lies exactly on a same-net pad centre (pad entry, polyline end)
    m = routed(L_PTS, extra_parts=(pad_part("U3", 15.0, 10.0),))
    r = postprocess(m, kiids(m))
    assert r == PostResult(replace=(), rip=(), reasons=(), merged=0, chamfered=0)


# ---- KiCad nanometre grid (finding: chamfer at c = CHAMFER_MIN rounded below the minimum) --

ZIG: list[Point] = [(10.0, 10.0), (15.0, 10.0), (15.0, 15.0), (20.0, 15.0), (20.0, 20.0)]


def _clean_after_nm(m: BoardModel, r: PostResult) -> BoardModel:
    out = nm_model(apply(m, r))
    for line, w in polylines(out, "N1", "F.Cu"):
        ends = pad_entry_ends(line, lambda p: _in_end_pad(p, ZIG))
        assert fails(analyse_bends(line, w, pad_ends=ends)) == {}, line
    whole = analyse_tracks(out)
    # round 2: a T is legal; a Y-junction is re-landed or ripped (M15)
    assert fails(whole) == {} and whole.y_junctions == ()
    return out


@pytest.mark.parametrize("width", [0.3, 0.4, 0.25, 0.127, 0.2, 0.15])
@pytest.mark.parametrize("blocked", [False, True])
def test_nm_rounded_output_clean(width: float, blocked: bool) -> None:
    # middle corner (15, 15) of a zig-zag (its chamfer lies between two non-exempt bends);
    # an N2 pad on the corner bisector forces c = CHAMFER_MIN(w) there when `blocked`
    extra: tuple[object, ...] = ()
    lo = CHAMFER_MIN(width)
    higher = [f * width for f in (1.5, 2.0, 2.5, 3.0) if f * width > lo]
    if blocked:
        d = (lo + min(higher)) / 4 + width / 2 + 0.2  # between the two inflated reaches
        size = 0.3
        cx, cy = 15.0 + d / S2 + size / 2, 15.0 - d / S2 - size / 2
        extra = (pad_part("U9", cx, cy, net="N2", size=size),)
    m = routed(ZIG, width, extra_parts=extra)
    r = postprocess(m, kiids(m))
    assert r.rip == ()
    assert r.chamfered == 3
    for t in (t for _, new in r.replace for t in new):
        assert on_nm_grid(t), t
    for d in diagonals(r):
        assert math.dist(d.start, d.end) >= lo
    if blocked:
        mid = [d for d in diagonals(r) if math.dist(nm_pt(d.start), (15.0, 15.0)) < 1.0]
        (d,) = mid
        assert lo <= math.dist(d.start, d.end) <= lo + 2e-6
    _clean_after_nm(m, r)


# ---- rip attribution (finding: defect point on untouched copper next to touched) ----------


def test_defect_on_untouched_piece_attributed() -> None:
    # the micro-jog's straight piece is an untouched stub; both bends are on touched tracks
    stub = track("N1", "F.Cu", (17.0, 12.0), (17.15, 12.0), 0.2, kiid="u-stub")
    t = path_tracks([(10.0, 10.0), (15.0, 10.0), (17.0, 12.0)])
    t2 = path_tracks([(17.15, 12.0), (19.15, 14.0), (24.15, 14.0)], prefix="s")
    m = board(
        parts=(pad_part("J1", 10.0, 10.0), pad_part("J2", 24.15, 14.0)),
        tracks=(*t, stub, *t2),
        net_class_of=(("N1", "Default"),),
    )
    ks = frozenset(x.kiid for x in m.tracks if not x.kiid.startswith("u"))
    assert analyse_tracks(m).micro_jogs == ((17.075, 12.0),)
    r = postprocess(m, ks)
    assert r.rip == (KEY,)
    assert (KEY, "micro_jog") in r.reasons


def test_untouched_defect_not_attributed() -> None:
    # an untouched 90-degree corner elsewhere on the same net stays not ours
    corner = path_tracks([(30.0, 30.0), (35.0, 30.0), (35.0, 35.0)], prefix="u")
    m = routed([(10.0, 10.0), (20.0, 10.0)], extra_tracks=corner)
    r = postprocess(m, kiids(m))
    assert r.rip == ()


# ---- untouched copper ends at a corner (finding: arc end left off copper) -----------------


def _arc(start: Point, mid: Point, end: Point, net: str = "N1") -> ArcTrack:
    return ArcTrack(
        kiid="arc", net=net, layer="F.Cu", start=start, mid=mid, end=end, width=0.2, locked=False
    )


def test_arc_end_at_corner_not_moved() -> None:
    m = routed(L_PTS)
    m = replace(m, arcs=(_arc((15.0, 10.0), (16.0, 9.0), (17.0, 10.0)),))
    r = postprocess(m, kiids(m))
    assert r.replace == ()
    assert r.rip == (KEY,)
    assert (KEY, "ninety_unfixable") in r.reasons


def test_arc_end_on_leg_limits_chamfer() -> None:
    # an arc ends on the incoming leg 0.3 mm before the corner: the offset must stay below it
    m = routed(L_PTS)
    m = replace(m, arcs=(_arc((14.7, 10.0), (14.2, 9.5), (13.7, 10.0)),))
    r = postprocess(m, kiids(m))
    assert r.rip == ()
    assert 0.4 <= _corner_c(r) <= 0.4 + 2e-6
    out = apply(m, r)
    assert any(_seg_dist((14.7, 10.0), (14.7, 10.0), t.start, t.end) < 1e-9 for t in out.tracks)


def test_lost_connectivity_rips(monkeypatch: pytest.MonkeyPatch) -> None:
    # J2 hangs off the corner by an arc only; with the corner guard disabled the chamfer
    # strands the arc - the connectivity safety net must still rip the connection
    m = board(
        parts=(pad_part("J1", 10.0, 10.0), pad_part("J2", 17.0, 10.0)),
        tracks=path_tracks(L_PTS),
        arcs=(_arc((15.0, 10.0), (16.0, 9.0), (17.0, 10.0)),),
        net_class_of=(("N1", "Default"),),
    )
    assert routed_length(m, *_pads(m)) is not None
    monkeypatch.setattr(chamfer, "_copper_ends", lambda *_a, **_k: [])
    r = postprocess(m, kiids(m))
    assert r.chamfered == 1
    assert routed_length(apply(m, r), *_pads(m)) is None
    assert (KEY, "disconnected") in r.reasons


def _pads(m: BoardModel) -> tuple[Pad, Pad]:
    a, b = (q for p in m.parts for q in p.pads if q.net == "N1")
    return a, b


def test_half_touched_corner_rips() -> None:
    m = routed(L_PTS)
    m = replace(m, tracks=tuple(replace(t, kiid="u1") if t.kiid == "t1" else t for t in m.tracks))
    r = postprocess(m, kiids(m))
    assert r.replace == ()
    assert r.rip == (KEY,)
    assert (KEY, "ninety_unfixable") in r.reasons


def _seg_dist(a: Point, b: Point, c: Point, d: Point) -> float:
    def pt(p: Point, s: Point, e: Point) -> float:
        vx, vy = e[0] - s[0], e[1] - s[1]
        ll = vx * vx + vy * vy
        t = 0.0 if ll == 0 else max(0.0, min(1.0, ((p[0] - s[0]) * vx + (p[1] - s[1]) * vy) / ll))
        return math.dist(p, (s[0] + t * vx, s[1] + t * vy))

    def cross(o: Point, p: Point, q: Point) -> float:
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    if (cross(a, b, c) > 0) != (cross(a, b, d) > 0) and (cross(c, d, a) > 0) != (
        cross(c, d, b) > 0
    ):
        return 0.0
    return min(pt(a, c, d), pt(b, c, d), pt(c, a, b), pt(d, a, b))


def _rect_dist(a: Point, b: Point, q: Pad) -> float:
    x0, x1, y0, y1 = q.x - q.w / 2, q.x + q.w / 2, q.y - q.h / 2, q.y + q.h / 2
    if any(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in (a, b)):
        return 0.0
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return min(_seg_dist(a, b, p, q2) for p, q2 in itertools.pairwise([*corners, corners[0]]))


def test_cross_net_diagonals_keep_clearance() -> None:
    # an N2 L wrapped around the N1 L, 0.45 mm apart (edge gap 0.25 >= 0.2 clearance): the
    # outer corner's chamfer cuts toward the inner one and must shrink to keep clearance
    outer = [(10.0, 9.55), (15.45, 9.55), (15.45, 15.0)]
    n2 = tuple(
        track("N2", "F.Cu", a, b, 0.2, kiid=f"v{i}")
        for i, (a, b) in enumerate(itertools.pairwise(outer))
    )
    m = routed(
        L_PTS,
        extra_tracks=n2,
        extra_parts=(pad_part("K1", *outer[0], net="N2"), pad_part("K2", *outer[-1], net="N2")),
    )
    r = postprocess(m, kiids(m) | kiids(m, "v"))
    assert r.rip == ()
    assert r.chamfered == 2
    by_net = {d.net: math.dist(d.start, d.end) for d in chamfers(r)}
    assert by_net["N1"] >= 0.6
    assert by_net["N2"] < 0.6  # shrunk by the N1 copper
    out = nm_model(apply(m, r))
    for d in chamfers(r):
        for t in out.tracks:
            if t.net != d.net:
                gap = _seg_dist(d.start, d.end, t.start, t.end) - 0.2
                assert gap >= 0.2 - 1e-6, (d, t, gap)


def test_chamfer_keeps_obstacle_net_class_clearance() -> None:
    # N2 (class "Wide", clearance 0.45 > N1's 0.2) has a pad whose nearest corner lies on
    # the corner bisector 0.8 mm from the vertex: c = 0.6 would leave an edge gap of
    # 0.8 - 0.3 - 0.1 = 0.4 mm (fine for 0.2, not for 0.45) - KiCad uses max(own, other)
    d = 0.8 / S2
    cx, cy = 15.0 - d - 0.25, 10.0 + d + 0.25
    m = routed(L_PTS, extra_parts=(pad_part("U9", cx, cy, net="N2"),))
    m = replace(
        m,
        net_classes=(*m.net_classes, net_class("Wide", clearance=0.45)),
        net_class_of=(("N1", "Default"), ("N2", "Wide")),
    )
    r = postprocess(m, kiids(m))
    assert r.rip == ()
    assert r.chamfered == 1
    (q,) = [q for p in m.parts for q in p.pads if q.net == "N2"]
    (diag,) = diagonals(r)
    assert math.dist(diag.start, diag.end) < 0.6
    assert _rect_dist(diag.start, diag.end, q) - 0.1 >= 0.45 - 1e-6


def test_timing_step3_many_defects() -> None:
    """4000 touched non-octilinear tracks on one net (each a defect): step-3 attribution
    must not scan every track per defect."""
    tracks = tuple(
        track("N1", "F.Cu", (x, y), (x + 0.3, y + 0.1), 0.2, kiid=f"t{k}")
        for k, (x, y) in enumerate(
            (1.0 + 0.5 * (k % 80), 1.0 + 0.5 * (k // 80)) for k in range(4000)
        )
    )
    m = board(
        parts=(pad_part("J1", 0.0, 0.0), pad_part("J2", 50.0, 30.0)),
        tracks=tracks,
        net_class_of=(("N1", "Default"),),
    )
    t0 = time.perf_counter()
    r = postprocess(m, kiids(m))
    elapsed = time.perf_counter() - t0
    assert (KEY, "non_octilinear") in r.reasons
    print(f"postprocess 4000 defects: {elapsed:.3f} s")
    assert elapsed < 3.0, elapsed


def test_timing_merge_long_chain() -> None:
    """2000 collinear touched pieces merge into one track; finding the chain's outer ends
    must not compare every endpoint with every joint."""
    pts = [(1.0 + 0.01 * k, 10.0) for k in range(2001)]
    m = routed(pts, pads=False)  # end pads would stop the merge at the joints they cover
    t0 = time.perf_counter()
    r = postprocess(m, kiids(m))
    elapsed = time.perf_counter() - t0
    assert r.merged == 1999
    (merged,) = dict(r.replace)["t0"]
    assert (merged.start, merged.end) == (pts[0], pts[-1])
    print(f"postprocess 2000-piece chain: {elapsed:.3f} s")
    assert elapsed < 1.0, elapsed


def test_timing_pad_comb(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1600-pad GND comb (spine + one stub per pad, all touched): the polyline pad-end
    check must not test every polyline end against every pad of the net."""
    calls = 0
    in_pad = chamfer._in_pad

    def counted(p: Point, q: Pad) -> bool:
        nonlocal calls
        calls += 1
        return in_pad(p, q)

    monkeypatch.setattr(chamfer, "_in_pad", counted)
    xs = [1.0 + 0.5 * k for k in range(1600)]
    spine = [
        track("GND", "F.Cu", (a, 5.0), (b, 5.0), 0.2, kiid=f"s{k}")
        for k, (a, b) in enumerate(itertools.pairwise(xs))
    ]
    stubs = [track("GND", "F.Cu", (x, 5.0), (x, 6.0), 0.2, kiid=f"p{k}") for k, x in enumerate(xs)]
    parts = [
        part(f"R{k}", x, 6.0, half=0.2, pads=(pad(f"R{k}", x, 6.0, net="GND", size=0.2),))
        for k, x in enumerate(xs)
    ]
    m = board(parts=tuple(parts), tracks=(*spine, *stubs), net_class_of=(("GND", "Default"),))
    t0 = time.perf_counter()
    r = postprocess(m, frozenset(t.kiid for t in m.tracks))
    elapsed = time.perf_counter() - t0
    assert r.merged == 0
    print(f"postprocess 1600-pad comb: {elapsed:.3f} s, {calls} _in_pad calls")
    assert calls < 100_000, calls  # was 2.56 M (every polyline end x every pad)
    assert elapsed < 3.0, elapsed


def test_timing_step3_large_baseline() -> None:
    """An untouched staircase with 4000 90-degree corners plus one touched track on the same
    net/layer: suppressing the baseline defects must not compare every new defect with
    every pre-existing one."""
    pts = [(1.0, 1.0)]
    for k in range(4001):
        x, y = pts[-1]
        pts.append((x + 0.5, y) if k % 2 == 0 else (x, y + 0.5))
    stair = path_tracks(pts, prefix="u")
    m = board(
        parts=(pad_part("J1", 0.0, 0.0), pad_part("J2", 20.0, 0.0)),
        tracks=(*stair, track("N1", "F.Cu", (0.0, 0.0), (20.0, 0.0), 0.2, kiid="t0")),
        net_class_of=(("N1", "Default"),),
    )
    assert len(analyse_tracks(m).ninety) == 4000
    t0 = time.perf_counter()
    r = postprocess(m, kiids(m))
    elapsed = time.perf_counter() - t0
    assert r.rip == ()
    print(f"postprocess 4000 baseline defects: {elapsed:.3f} s")
    assert elapsed < 1.0, elapsed


# ---- property test ------------------------------------------------------------------------

_DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]


def _random_pts(rng: random.Random) -> list[Point]:
    """A random polyline on the nm grid with 90-degree turns: axis legs, or (rotated by 45
    degrees) exact diagonal legs."""
    p = (round(rng.uniform(20, 30) * 1e6), round(rng.uniform(20, 30) * 1e6))
    pts = [p]
    k = rng.choice((0, 2, 4, 6)) + rng.choice((0, 0, 1))  # odd -> diagonal legs
    for i in range(rng.randint(1, 7)):
        if i:
            k = (k + rng.choice((2, -2))) % 8
        length = rng.choice(
            (0.1, 0.2, 0.3, 0.45, 0.6, 1.0, 1.0, 2.0, 2.0, 3.0, rng.uniform(0.05, 6.0))
        )
        dx, dy = _DIRS[k]
        step = round(length * 1e6 / (S2 if dx and dy else 1.0))
        p = (p[0] + dx * step, p[1] + dy * step)
        pts.append(p)
    return [(x / 1e6, y / 1e6) for x, y in pts]


def _in_end_pad(p: Point, pts: Sequence[Point]) -> bool:
    """`p` inside the 0.5 mm pad J1 (at pts[0]) or J2 (at pts[-1]), join tolerance included."""
    return any(abs(p[0] - c[0]) <= 0.252 and abs(p[1] - c[1]) <= 0.252 for c in (pts[0], pts[-1]))


def _case(seed: int) -> tuple[BoardModel, frozenset[str], list[Point], float]:
    rng = random.Random(seed)
    pts = _random_pts(rng)
    width = rng.choice((0.05, 0.1, 0.127, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5))
    obst = []
    for j, c in enumerate(pts[1:-1]):
        if rng.random() < 0.3:  # an N2 pad somewhere near the corner
            size = rng.choice((0.2, 0.3, 0.5))
            ang = rng.uniform(0, 2 * math.pi)
            d = rng.uniform(0.4, 1.5)
            x, y = c[0] + d * math.cos(ang), c[1] + d * math.sin(ang)
            obst.append(pad_part(f"U{j + 10}", nm(x), nm(y), net="N2", size=size))
    # a separate stream, so the polylines / obstacles of every seed stay as they were
    rng2 = random.Random(seed + 1_000_000)
    split = _split_legs(rng2, pts)
    xnet = _xnet_tracks(rng2, split, width)
    m = routed(split, width, extra_parts=obst, extra_tracks=xnet)
    ks = kiids(m) | kiids(m, "n3-")
    n1 = sorted(k for k in ks if k.startswith("t"))
    if len(n1) > 1 and rng.random() < 0.25:  # one track stays untouched copper
        ks = ks - {rng.choice(n1)}
    return m, ks, pts, width


def _nm_ints(p: Point) -> tuple[int, int]:
    return (round(p[0] * 1e6), round(p[1] * 1e6))


def _split_legs(rng: random.Random, pts: Sequence[Point]) -> list[Point]:
    """`pts` with some straight legs split into 2-3 collinear touched pieces (nm grid)."""
    out = [pts[0]]
    for a, b in itertools.pairwise(pts):
        (ax, ay), (bx, by) = _nm_ints(a), _nm_ints(b)
        steps = max(abs(bx - ax), abs(by - ay))
        if steps >= 4 and rng.random() < 0.4:
            dx, dy = (bx > ax) - (bx < ax), (by > ay) - (by < ay)
            for k in sorted(rng.sample(range(1, steps), rng.choice((1, 2)))):
                out.append(((ax + dx * k) / 1e6, (ay + dy * k) / 1e6))
        out.append(b)
    return out


def _xnet_tracks(rng: random.Random, pts: Sequence[Point], width: float) -> list[Track]:
    """Sometimes a split straight N3 run on B.Cu with its end or its joint on a vertex /
    split point of N1 (another net and layer sharing coordinates)."""
    if rng.random() < 0.5:
        return []
    ax, ay = _nm_ints(rng.choice(list(pts)))
    dx, dy = _DIRS[rng.randrange(8)]
    k1 = rng.randint(200, 3000) * 1000  # nm steps
    k2 = rng.randint(200, 3000) * 1000
    if rng.random() < 0.5:  # the anchor is N3's outer end
        run = [(ax, ay), (ax + dx * k1, ay + dy * k1), (ax + dx * (k1 + k2), ay + dy * (k1 + k2))]
    else:  # the anchor is N3's joint
        run = [(ax - dx * k1, ay - dy * k1), (ax, ay), (ax + dx * k2, ay + dy * k2)]
    return [
        track(
            "N3", "B.Cu", (a[0] / 1e6, a[1] / 1e6), (b[0] / 1e6, b[1] / 1e6), width, kiid=f"n3-{i}"
        )
        for i, (a, b) in enumerate(itertools.pairwise(run))
    ]


def _check_case(seed: int) -> bool:
    """postprocess the property case `seed`; assert its output is clean (nm grid, N3 merged,
    no silent disconnection, every non-ripped N1 polyline passes `analyse_bends` (M4 / M7)
    after nm rounding, clearance kept). Returns whether the N1 connection was ripped."""
    m, ks, pts, width = _case(seed)
    msg = f"seed={seed} pts={pts} width={width} kiids={sorted(ks)}"
    r = postprocess(m, ks)
    for t in (t for _, new in r.replace for t in new):
        assert on_nm_grid(t), msg
    out = nm_model(apply(m, r))
    n3_in = [t for t in m.tracks if t.net == "N3"]
    if n3_in:  # N3 (no pads, nothing at its joint): one merged track, same outer ends
        (n3,) = [t for t in out.tracks if t.net == "N3"]
        assert {n3.start, n3.end} == {n3_in[0].start, n3_in[-1].end}, msg
        assert not any(k.startswith("N3") for k in r.rip), msg
    if routed_length(m, *_pads(m)) is not None and routed_length(out, *_pads(out)) is None:
        assert KEY in r.rip, msg
    if KEY in r.rip:
        return True
    for line, w in polylines(out, "N1", "F.Cu"):
        ends = pad_entry_ends(line, lambda p: _in_end_pad(p, pts))
        rb = analyse_bends(line, w, pad_ends=ends)
        assert fails(rb) == {}, f"{msg} line={line}"
    whole = analyse_tracks(out)
    # round 2: a T is legal; a Y-junction is re-landed or ripped (M15)
    assert fails(whole) == {} and whole.y_junctions == (), msg
    assert routed_length(out, *_pads(out)) is not None, msg
    n2 = [q for p in m.parts for q in p.pads if q.net == "N2"]
    for d in chamfers(r):
        for q in n2:
            gap = _rect_dist(d.start, d.end, q) - width / 2
            assert gap >= 0.2 - 1e-6, f"{msg} diag={d} pad={q}"
    return False


# seeds whose sub-join-tolerance pieces / gaps hid a 90-degree corner (finding: a polyline
# walked across a < 0.002 mm piece was non-octilinear and slipped through unripped)
TINY_SEEDS = (1670, 2650, 3077, 3369, 4757, 4879, 5190)


@pytest.mark.parametrize("seed", TINY_SEEDS)
def test_tiny_piece_seeds_clean_or_ripped(seed: int) -> None:
    _check_case(seed)


@pytest.mark.parametrize("seed", TINY_SEEDS)
def test_polyline_safety_net_rips(seed: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # without the step-1 repair the step-3 polyline check alone must still catch the defect
    monkeypatch.setattr(chamfer, "_close_gaps", lambda _m, _u, live, _s: live)
    _check_case(seed)


@pytest.mark.parametrize("seed", (2595, 4816))
def test_harmless_sub_tolerance_gap_not_ripped(seed: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # an untouched < 0.001 mm piece next to touched ends: a collinear gap / one node that
    # turns no track noticeably in any track order - not a defect. (Seed 2595's random path
    # also U-turns at J1, an M16 pad loop the R4 step rips - not what this test is about.)
    monkeypatch.setattr(chamfer, "m16_defects", lambda *_a, **_k: [])
    assert _check_case(seed) is False


PROPERTY_SEEDS = 4000  # ~9 s; a one-off 10000-seed run passed too


def test_output_passes_analyse_bends() -> None:
    t0 = time.perf_counter()
    ripped = sum(_check_case(seed) for seed in range(PROPERTY_SEEDS))
    print(f"{PROPERTY_SEEDS} seeds: {time.perf_counter() - t0:.1f} s, {ripped} ripped")
    assert 0 < ripped < PROPERTY_SEEDS


# ---- T-split pieces: the step-3 polyline check selects polylines by touched copper ----------


def _in_any_pad(p: Point, m: BoardModel, net: str) -> bool:
    return any(
        abs(p[0] - q.x) <= q.w / 2 + 0.002 and abs(p[1] - q.y) <= q.h / 2 + 0.002
        for part_ in m.parts
        for q in part_.pads
        if q.net == net
    )


def _touched_lines_clean_or_ripped(m: BoardModel, ks: frozenset[str], r: PostResult) -> None:
    """Every nm-rounded output polyline holding touched copper (a segment whose midpoint
    lies on a touched output track) passes `analyse_bends`, or its net was ripped."""
    rep = dict(r.replace)
    touched = [
        nm_model(replace(m, tracks=(t,))).tracks[0]
        for k in sorted(ks)
        for t in rep.get(k, [tt for tt in m.tracks if tt.kiid == k])
    ]
    out = nm_model(apply(m, r))
    for net, layer in sorted({(t.net, t.layer) for t in touched}):
        if any(k.startswith(f"{net}:") for k in r.rip):
            continue
        mine = [t for t in touched if (t.net, t.layer) == (net, layer)]
        for line, w in polylines(out, net, layer):
            if not any(
                chamfer._pt_seg(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), t.start, t.end) <= 0.003
                for a, b in itertools.pairwise(line)
                for t in mine
            ):
                continue
            ends = pad_entry_ends(line, lambda p, net=net: _in_any_pad(p, m, net))
            assert fails(analyse_bends(line, w, pad_ends=ends)) == {}, (line, r)


def _t_split_board(
    t0: tuple[Point, Point], t1: tuple[Point, Point], pads: Sequence[Point]
) -> BoardModel:
    return board(
        parts=tuple(pad_part(f"J{i + 1}", *p) for i, p in enumerate(pads)),
        tracks=(
            track("N1", "F.Cu", *t0, 0.2, kiid="t0"),
            track("N1", "F.Cu", *t1, 0.2, kiid="t1"),
        ),
        net_class_of=(("N1", "Default"),),
    )


def test_t_split_piece_without_touched_midpoint_rips() -> None:
    # t1 ends 0.0003 mm off t0's interior (a T): geom T-splits t0 there, so its 0.0024 mm
    # overshoot is a polyline of its own holding no touched track's midpoint - and it is
    # non-octilinear after nm rounding
    m = _t_split_board(
        ((10.0, 10.0), (13.0024, 10.0)),
        ((13.0, 10.0003), (15.0, 12.0003)),
        [(10, 10), (15, 12.0003)],
    )
    r = postprocess(m, kiids(m))
    assert KEY in r.rip
    _touched_lines_clean_or_ripped(m, kiids(m), r)


def test_t_split_piece_inside_pad_rips() -> None:
    # the same T inside pad J2 (both ends of the overshoot piece are pad ends)
    m = _t_split_board(
        ((10.0, 10.0), (15.0024, 10.0)),
        ((15.0, 10.0003), (17.0, 12.0003)),
        [(10, 10), (15, 10), (17, 12.0003)],
    )
    r = postprocess(m, kiids(m))
    assert r.rip
    _touched_lines_clean_or_ripped(m, kiids(m), r)


def _perturbed_case(seed: int) -> tuple[BoardModel, frozenset[str]]:
    """A random octilinear N1 route (all touched) whose joints get sub-2.5 um defects: a
    tiny piece, a gap, or a T (one track overshoots the joint, the next starts beside it)."""
    rng = random.Random(seed)
    pts = _random_pts(rng)
    width = rng.choice((0.1, 0.15, 0.2, 0.25, 0.3))

    def jitter() -> tuple[int, int]:
        return (rng.randint(-2500, 2500), rng.randint(-2500, 2500))

    ints = [_nm_ints(p) for p in pts]
    segs: list[tuple[tuple[int, int], tuple[int, int]]] = []
    start = ints[0]
    for i in range(1, len(ints)):
        v = ints[i]
        if i == len(ints) - 1 or rng.random() < 0.4:
            segs.append((start, v))
            start = v
            continue
        kind = rng.choice(("piece", "gap", "t"))
        dx, dy = jitter()
        if kind == "piece":
            segs.append((start, v))
            segs.append((v, (v[0] + dx, v[1] + dy)))
            start = (v[0] + dx, v[1] + dy)
        elif kind == "gap":
            segs.append((start, v))
            start = (v[0] + dx, v[1] + dy)
        else:  # overshoot along the leg, the next leg starts just off the joint
            ux, uy = (v[0] > start[0]) - (v[0] < start[0]), (v[1] > start[1]) - (v[1] < start[1])
            over = rng.randint(1, 2500)
            segs.append((start, (v[0] + ux * over, v[1] + uy * over)))
            off = rng.randint(-500, 500)
            start = (v[0] - uy * off, v[1] + ux * off)
    tracks = tuple(
        track("N1", "F.Cu", (a[0] / 1e6, a[1] / 1e6), (b[0] / 1e6, b[1] / 1e6), width, kiid=f"t{k}")
        for k, (a, b) in enumerate(segs)
        if a != b
    )
    m = board(
        parts=(pad_part("J1", *pts[0]), pad_part("J2", *pts[-1])),
        tracks=tracks,
        net_class_of=(("N1", "Default"),),
    )
    return m, kiids(m)


PERTURBED_SEEDS = 1000


def test_perturbed_joints_clean_or_ripped() -> None:
    t0 = time.perf_counter()
    ripped = 0
    for seed in range(PERTURBED_SEEDS):
        m, ks = _perturbed_case(seed)
        r = postprocess(m, ks)
        ripped += bool(r.rip)
        _touched_lines_clean_or_ripped(m, ks, r)
    print(f"{PERTURBED_SEEDS} perturbed seeds: {time.perf_counter() - t0:.1f} s, {ripped} ripped")


def test_deterministic() -> None:
    pts = [(10.0, 10.0), (15.0, 10.0), (17.0, 10.0), (17.0, 15.0), (22.0, 15.0)]
    m = routed(pts)
    a = postprocess(m, kiids(m))
    b = postprocess(replace(m, tracks=tuple(reversed(m.tracks))), kiids(m))
    assert a == b
    assert a.merged == 1 and a.chamfered == 2


def test_no_kipy_import() -> None:
    code = (
        "import sys; import copperchat.board.router.chamfer; "
        "assert 'kipy' not in sys.modules, 'kipy imported'"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)


# ---- R3 (owner rule a): a T-junction is legal; a Y with a 90-degree pair is still ripped ----


def test_t_junction_kept_y_with_ninety_pair_ripped() -> None:
    """A perpendicular branch off a straight N1 track (a T) is not ripped any more; a node
    that has a 90-degree pair AND a 45-degree branch (a Y, M15) still is."""
    branch = track("N1", "F.Cu", (15.0, 10.0), (15.0, 14.0), 0.2, kiid="t8")
    m = routed(
        [(10.0, 10.0), (15.0, 10.0), (20.0, 10.0)],
        extra_parts=(pad_part("J3", 15.0, 14.0),),
        extra_tracks=(branch,),
    )
    r = postprocess(m, kiids(m))
    assert r.rip == () and r.reasons == ()
    y = track("N1", "F.Cu", (15.0, 10.0), (17.0, 12.0), 0.2, kiid="t9")
    m2 = replace(m, parts=(*m.parts, pad_part("J4", 17.0, 12.0)), tracks=(*m.tracks, y))
    r2 = postprocess(m2, kiids(m2))
    # R4: its branch starts in a pad (no vertex to re-land from) -> ripped as a Y (M15)
    assert r2.rip and all(why == "y_junction" for _k, why in r2.reasons), r2


# ---- R4: Y-junctions to T, M16 rips ------------------------------------------------------


def _y_board(branch: Sequence[Point], through: tuple[Point, Point] = ((10.0, 10.0), (20.0, 10.0))):
    """N1 through-track J1-J2 along `through`, a branch polyline from pad J3 (its first
    point) ending on the through-track at its last point - all touched."""
    m = routed(
        list(through),
        extra_parts=(pad_part("J3", *branch[0]),),
        extra_tracks=path_tracks(branch, prefix="b"),
    )
    return m


def test_t_junction_kept() -> None:
    m = _y_board([(14.0, 16.0), (14.0, 10.0)])
    r = postprocess(m, kiids(m) | kiids(m, "b"))
    assert r.reasons == () and r.relanded == 0


def test_y_to_t_relands() -> None:
    """J3 → (14, 13) straight down, then 45° onto the through-track at (17, 10): a Y. The
    branch's last piece becomes the perpendicular (14, 13) → (14, 10) - a T, the vertex at
    (14, 13) now collinear."""
    m = _y_board([(14.0, 16.0), (14.0, 13.0), (17.0, 10.0)])
    assert analyse_tracks(m).y_junctions
    r = postprocess(m, kiids(m) | kiids(m, "b"))
    assert r.relanded == 1 and r.reasons == (), r
    out = apply(m, r)
    rep = analyse_tracks(out)
    assert rep.y_junctions == () and fails(rep) == {}
    new = dict(r.replace)["b1"][0]
    assert {nm_pt(new.start), nm_pt(new.end)} == {(14.0, 13.0), (14.0, 10.0)}


def test_y_to_t_rejects_ninety_on_branch() -> None:
    """The branch comes in horizontally at (14, 13): the perpendicular would turn it by 90°
    there - not re-landed, ripped `y_junction`."""
    m = _y_board([(11.0, 13.0), (14.0, 13.0), (17.0, 10.0)])
    r = postprocess(m, kiids(m) | kiids(m, "b"))
    assert r.relanded == 0
    assert ("N1:J1.1-J3.1", "y_junction") in r.reasons, r


def test_y_to_t_projection_outside() -> None:
    """The through-track starts at x = 15: the foot at x = 14 lies outside it - ripped."""
    m = _y_board([(14.0, 16.0), (14.0, 13.0), (17.0, 10.0)], through=((15.0, 10.0), (20.0, 10.0)))
    r = postprocess(m, kiids(m) | kiids(m, "b"))
    assert r.relanded == 0 and any(why == "y_junction" for _k, why in r.reasons), r


def test_y_unfixable_ripped_with_reason() -> None:
    """A branch straight from a pad at 45° (no vertex to re-land from): ripped as a Y."""
    m = _y_board([(14.0, 13.0), (17.0, 10.0)])
    r = postprocess(m, kiids(m) | kiids(m, "b"))
    assert r.relanded == 0 and any(why == "y_junction" for _k, why in r.reasons), r


def test_dogleg_ripped() -> None:
    """J1 (10, 10) and J2 (12.5, 10) are adjacent with a free straight corridor; the
    touched route jogs off it (M16 a)."""
    pts = [(10.0, 10.0), (10.5, 10.0), (10.9, 10.4), (11.6, 10.4), (12.0, 10.0), (12.5, 10.0)]
    m = routed(pts)
    r = postprocess(m, kiids(m))
    assert (KEY, "dogleg") in r.reasons, r


def test_pad_loop_ripped() -> None:
    """The route leaves J1 east and turns back by 135° inside its entry zone (M16 b)."""
    pts = walk((50.0, 50.0), 0.0, [(0, 0.8), (45, 0.4), (45, 0.4), (45, 0.4), (0, 6.0)])
    pts = [nm_pt(p) for p in pts]
    m = routed(pts)
    r = postprocess(m, kiids(m))
    assert (KEY, "pad_loop") in r.reasons, r


def test_pad_jog_ripped() -> None:
    """An S-offset (two opposite 45° bends) right at J1: an error like a loop (owner)."""
    pts = walk((50.0, 50.0), 0.0, [(0, 0.6), (45, 0.4), (-45, 6.0)])
    pts = [nm_pt(p) for p in pts]
    m = routed(pts)
    r = postprocess(m, kiids(m))
    assert (KEY, "pad_jog") in r.reasons, r


def test_y_reland_ignores_zero_length_untouched_track() -> None:
    """Review round 1: an untouched zero-length piece at the junction or at V must not
    crash the re-landing (it has no direction)."""
    m = _y_board([(14.0, 16.0), (14.0, 13.0), (17.0, 10.0)])
    stub = track("N1", "F.Cu", (14.0, 13.0), (14.0, 13.0), 0.2, kiid="z0")
    stub2 = track("N1", "F.Cu", (17.0, 10.0), (17.0, 10.0), 0.2, kiid="z1")
    m = replace(m, tracks=(*m.tracks, stub, stub2))
    r = postprocess(m, kiids(m) | kiids(m, "b"))
    assert isinstance(r, PostResult)
