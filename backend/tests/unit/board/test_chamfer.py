"""P6a: Freerouting post-processor (merge, chamfer, rip list) - B-C12 `postprocess`."""

from __future__ import annotations

import itertools
import math
import random
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace

from copperchat.board.geom import (
    CHAMFER_MIN,
    BendReport,
    Point,
    analyse_bends,
    analyse_tracks,
    polylines,
)
from copperchat.board.model import BoardModel, Pad, Track
from copperchat.board.router.chamfer import PostResult, postprocess
from tests.unit.board.builders import board, part, track, via

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
    a = 0.6 / S2
    (t_in,) = rep["t0"]
    t_out, diag = rep["t1"]
    assert t_in.start == (10.0, 10.0)
    assert math.dist(t_in.end, (15.0 - a, 10.0)) < 1e-9
    assert math.dist(t_out.start, (15.0, 10.0 + a)) < 1e-9
    assert t_out.end == (15.0, 15.0)
    assert math.isclose(math.dist(diag.start, diag.end), 0.6, abs_tol=1e-6)
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
    assert math.isclose(_corner_c(r), 0.4, abs_tol=1e-9)


def test_chamfer_min_when_3w_too_small() -> None:
    m = routed(L_PTS, width=0.05)
    r = postprocess(m, kiids(m))
    assert r.chamfered == 1
    assert math.isclose(_corner_c(r), 0.25, abs_tol=1e-9)
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


def _random_pts(rng: random.Random) -> list[Point]:
    pts: list[Point] = [(rng.uniform(20, 30), rng.uniform(20, 30))]
    heading = rng.choice((0, 90, 180, 270))
    for i in range(rng.randint(1, 7)):
        if i:
            heading += rng.choice((90, -90))
        length = rng.choice((0.1, 0.2, 0.3, 0.45, 0.6, 1.0, 2.0, 3.0, rng.uniform(0.05, 6.0)))
        x, y = pts[-1]
        h = math.radians(heading)
        pts.append((round(x + length * math.cos(h), 6), round(y + length * math.sin(h), 6)))
    return pts


def _in_end_pad(p: Point, pts: Sequence[Point]) -> bool:
    """`p` inside the 0.5 mm pad J1 (at pts[0]) or J2 (at pts[-1]), join tolerance included."""
    return any(abs(p[0] - c[0]) <= 0.252 and abs(p[1] - c[1]) <= 0.252 for c in (pts[0], pts[-1]))


def test_output_passes_analyse_bends() -> None:
    ripped = 0
    for seed in range(200):
        rng = random.Random(seed)
        pts = _random_pts(rng)
        width = rng.choice((0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.5))
        m = routed(pts, width)
        r = postprocess(m, kiids(m))
        if KEY in r.rip:
            ripped += 1
            continue
        out = apply(m, r)
        for line, w in polylines(out, "N1", "F.Cu"):
            ends = (_in_end_pad(line[0], pts), _in_end_pad(line[-1], pts))
            rb = analyse_bends(line, w, pad_ends=ends)
            assert fails(rb) == {}, f"seed={seed} pts={pts} width={width} line={line}"
        whole = analyse_tracks(out)
        assert fails(whole) == {} and whole.t_junctions == (), f"seed={seed} pts={pts}"
    assert 0 < ripped < 200


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
