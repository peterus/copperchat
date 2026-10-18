"""Pure geometry (E-C7) without KiCad."""

from __future__ import annotations

import itertools

import pytest

from copperchat.kicad_tools.edit.geometry import (
    CANONICAL,
    Box,
    canonical_transform,
    in_segment_interior,
    on_grid,
    on_segment,
    snap,
    snap_point,
    transform_matrix,
)


def test_snap() -> None:
    assert snap(10.16) == 10.16
    assert snap(10.2) == 10.16
    assert snap(-0.7) == -1.27
    assert snap_point((0.3, 1.0)) == (0.0, 1.27)
    assert on_grid((2.54, 101.6)) and not on_grid((2.54, 101.61))


def test_canonical_transform_8_forms() -> None:
    assert canonical_transform(180, "x") == (0, False, True)
    assert len({transform_matrix(*c) for c in CANONICAL}) == 8
    requests = list(itertools.product((0, 90, 180, 270), ("", "x", "y")))
    assert len(requests) == 12
    results = {canonical_transform(r, m) for r, m in requests}
    assert results == set(CANONICAL)
    for r, m in requests:  # same matrix as requested
        assert transform_matrix(*canonical_transform(r, m)) == transform_matrix(
            r, m == "x", m == "y"
        )


def test_canonical_transform_rejects_odd_angles() -> None:
    with pytest.raises(ValueError):
        canonical_transform(45, "")


def test_box_and_segments() -> None:
    b = Box(0, 0, 10, 5)
    assert b.contains((10, 5)) and not b.contains((10.1, 5))
    assert b.grow(1) == Box(-1, -1, 11, 6)
    assert b.hit_by_segment((5, -3), (5, 8))
    assert not b.hit_by_segment((10, -3), (10, 8))  # along the edge
    assert on_segment((5, 0), (0, 0), (10, 0))
    assert in_segment_interior((5, 0), (0, 0), (10, 0))
    assert not in_segment_interior((10, 0), (0, 0), (10, 0))
    assert not on_segment((5, 0.02), (0, 0), (10, 0))
