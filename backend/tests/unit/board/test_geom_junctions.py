"""Round 2 (R1, B-C5): junction kinds, `BendReport.y_junctions`."""

from __future__ import annotations

import math
from dataclasses import replace

import pytest

from copperchat.board.geom import Point, analyse_tracks, junction_kind
from copperchat.board.model import BoardModel, Pad, Track
from tests.unit.board.builders import board, part, track, via


@pytest.mark.parametrize(
    ("headings", "kind"),
    [
        ((0, 180, 90), "t"),
        ((0, 90, 180, 270), "x"),
        ((0, 180, 45), "y"),
        ((0, 180, 135), "y"),
        ((0, 90, 225), "y"),
        ((0, 45, 180), "y"),
        ((0, 180, 90.4), "t"),
        ((0, 180, 45.6), "y"),
        ((0, 180, 89.6), "t"),
        ((-90, 90, 0), "t"),
        ((0, 180, -45), "y"),
    ],
)
def test_junction_kind_table(headings: tuple[float, ...], kind: str) -> None:
    assert junction_kind(headings) == kind


def test_junction_kind_needs_three() -> None:
    with pytest.raises(ValueError):
        junction_kind((0.0, 90.0))


NODE: Point = (15.0, 10.0)


def _out(heading: float, length: float = 4.0) -> Point:
    h = math.radians(heading)
    return (NODE[0] + length * math.cos(h), NODE[1] + length * math.sin(h))


def _branch_board(heading: float, *, reverse: bool = False, **kw: object) -> BoardModel:
    """A straight through-track (10,10)-(20,10) and a branch leaving NODE at `heading`."""
    end = _out(heading)
    seg = (end, NODE) if reverse else (NODE, end)
    tracks = (
        track(start=(10.0, 10.0), end=(20.0, 10.0), kiid="a"),
        track(start=seg[0], end=seg[1], kiid="b"),
    )
    return board(tracks=tracks, **kw)


def test_y_junction_reported() -> None:
    r = analyse_tracks(_branch_board(45.0))
    assert r.y_junctions == (NODE,)
    assert r.t_junctions == ()


def test_t_junction_not_y() -> None:
    r = analyse_tracks(_branch_board(90.0))
    assert r.t_junctions == (NODE,)
    assert r.y_junctions == ()


def test_junction_kind_orientation_free() -> None:
    for heading in (45.0, 90.0, 135.0, -45.0):
        fwd = analyse_tracks(_branch_board(heading))
        rev = analyse_tracks(_branch_board(heading, reverse=True))
        assert fwd == rev, heading
    # the through-track drawn backwards too
    m = _branch_board(45.0)
    flipped = tuple(
        Track(t.kiid, t.net, t.layer, t.end, t.start, t.width, t.locked) for t in m.tracks
    )
    assert analyse_tracks(replace(m, tracks=flipped)) == analyse_tracks(m)


def test_y_at_via_exempt() -> None:
    r = analyse_tracks(_branch_board(45.0, vias=(via(at=NODE),)))
    assert r.y_junctions == ()
    # a Y with a 90° pair at a via: exempt from both junction kinds, as T-junctions were
    m = board(
        tracks=(
            track(start=(10.0, 10.0), end=NODE, kiid="a"),
            track(start=NODE, end=_out(90.0), kiid="b"),
            track(start=NODE, end=_out(-45.0), kiid="c"),
        ),
        vias=(via(at=NODE),),
    )
    r = analyse_tracks(m)
    assert r.y_junctions == () and r.t_junctions == ()


def _pad(x: float, y: float) -> Pad:
    return Pad(
        ref="U1",
        number="1",
        net="N1",
        x=x,
        y=y,
        w=1.0,
        h=1.0,
        rot=0.0,
        shape="rect",
        layers=frozenset({"F.Cu"}),
        smd=True,
        drill=None,
    )


def test_y_inside_pad_exempt() -> None:
    r = analyse_tracks(_branch_board(45.0, parts=(part("U1", 50, 50, pads=(_pad(*NODE),)),)))
    assert r.y_junctions == ()


def test_t_at_bend_vertex_is_y() -> None:
    # through-track comes from the west and bends 45° at NODE (headings 180 and 45 from the
    # node, i.e. (0, 225)-style); a branch at 90° onto that vertex is a Y
    m = board(
        tracks=(
            track(start=(10.0, 10.0), end=NODE, kiid="a"),
            track(start=NODE, end=_out(-45.0), kiid="b"),
            track(start=NODE, end=_out(90.0), kiid="c"),
        )
    )
    r = analyse_tracks(m)
    assert r.y_junctions == (NODE,)


def test_branch_on_chamfer_is_y() -> None:
    # the main track runs east and steps down by a 45° chamfer (12,10)-(14,12); a branch
    # perpendicular to the main direction lands on the middle of the chamfer piece
    m = board(
        tracks=(
            track(start=(8.0, 10.0), end=(12.0, 10.0), kiid="a"),
            track(start=(12.0, 10.0), end=(14.0, 12.0), kiid="b"),
            track(start=(14.0, 12.0), end=(20.0, 12.0), kiid="c"),
            track(start=(13.0, 11.0), end=(13.0, 16.0), kiid="d"),
        )
    )
    r = analyse_tracks(m)
    assert r.y_junctions == ((13.0, 11.0),)


def test_x_junction_ok() -> None:
    m = board(
        tracks=(
            track(start=(10.0, 10.0), end=NODE, kiid="a"),
            track(start=NODE, end=(20.0, 10.0), kiid="b"),
            track(start=(15.0, 5.0), end=NODE, kiid="c"),
            track(start=NODE, end=(15.0, 15.0), kiid="d"),
        )
    )
    r = analyse_tracks(m)
    assert r.y_junctions == ()
    assert r.t_junctions == (NODE,)  # old semantics: any 90° pair
