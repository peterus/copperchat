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
    m = routed(walk((10, 10), 0, _pair_steps(0.25)))
    r = postprocess(m, kiids(m))
    assert r == PostResult(replace=(), rip=(), reasons=(), merged=0, chamfered=0)


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
        ends = (_in_end_pad(line[0], ZIG), _in_end_pad(line[-1], ZIG))
        assert fails(analyse_bends(line, w, pad_ends=ends)) == {}, line
    whole = analyse_tracks(out)
    assert fails(whole) == {} and whole.t_junctions == ()
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
    m = routed(pts, width, extra_parts=obst)
    ks = kiids(m)
    if len(m.tracks) > 1 and rng.random() < 0.25:  # one track stays untouched copper
        ks = ks - {rng.choice(sorted(ks))}
    return m, ks, pts, width


def test_output_passes_analyse_bends() -> None:
    ripped = 0
    n = 500
    for seed in range(n):
        m, ks, pts, width = _case(seed)
        msg = f"seed={seed} pts={pts} width={width} kiids={sorted(ks)}"
        r = postprocess(m, ks)
        for t in (t for _, new in r.replace for t in new):
            assert on_nm_grid(t), msg
        out = nm_model(apply(m, r))
        if routed_length(m, *_pads(m)) is not None and routed_length(out, *_pads(out)) is None:
            assert KEY in r.rip, msg
        if KEY in r.rip:
            ripped += 1
            continue
        for line, w in polylines(out, "N1", "F.Cu"):
            ends = (_in_end_pad(line[0], pts), _in_end_pad(line[-1], pts))
            rb = analyse_bends(line, w, pad_ends=ends)
            assert fails(rb) == {}, f"{msg} line={line}"
        whole = analyse_tracks(out)
        assert fails(whole) == {} and whole.t_junctions == (), msg
        assert routed_length(out, *_pads(out)) is not None, msg
        n2 = [q for p in m.parts for q in p.pads if q.net == "N2"]
        for d in chamfers(r):
            for q in n2:
                gap = _rect_dist(d.start, d.end, q) - width / 2
                assert gap >= 0.2 - 1e-6, f"{msg} diag={d} pad={q}"
    assert 0 < ripped < n


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
