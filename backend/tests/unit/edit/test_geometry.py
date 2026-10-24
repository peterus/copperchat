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


class _P:
    def __init__(self, x: float, y: float) -> None:
        self.x, self.y = int(x * 1e6), int(y * 1e6)


class _Field:
    def __init__(
        self,
        value: str,
        angle: float = 0.0,
        halign: int = 2,
        valign: int = 2,
        visible: bool = True,
    ) -> None:
        from types import SimpleNamespace as NS

        attrs = NS(
            size=_P(1.27, 1.27), horizontal_alignment=halign, vertical_alignment=valign, angle=angle
        )
        self.text = NS(value=value, position=_P(10, 10), attributes=attrs)
        self.visible = visible


def test_field_box_matches_layout_extent() -> None:
    from copperchat.kicad_tools.edit.fields import text_width
    from copperchat.kicad_tools.edit.geometry import _field_box

    b = _field_box(_Field("10k"))
    assert b is not None
    assert b.x1 - b.x0 == pytest.approx(text_width("10k", 1.27))
    assert b.y1 - b.y0 == pytest.approx(1.6 * 1.27)
    r = _field_box(_Field("10k", angle=90))
    assert r is not None
    assert r.x1 - r.x0 == pytest.approx(1.6 * 1.27)
    assert r.y1 - r.y0 == pytest.approx(text_width("10k", 1.27))


@pytest.mark.parametrize(
    ("halign", "x0_off"),
    [(1, 0.0), (2, -0.5), (3, -1.0)],  # left, centre, right
)
@pytest.mark.parametrize(
    ("valign", "y0_off"), [(1, 0.0), (2, -0.5), (3, -1.0)]
)  # top, centre, bottom
def test_field_box_alignment_anchor(halign: int, x0_off: float, valign: int, y0_off: float) -> None:
    from copperchat.kicad_tools.edit.fields import text_width
    from copperchat.kicad_tools.edit.geometry import _field_box

    b = _field_box(_Field("10k", halign=halign, valign=valign))
    assert b is not None
    w, h = text_width("10k", 1.27), 1.6 * 1.27
    assert b.x0 == pytest.approx(10 + x0_off * w)
    assert b.y0 == pytest.approx(10 + y0_off * h)


def test_field_box_skips_invisible_and_empty() -> None:
    from copperchat.kicad_tools.edit.geometry import _field_box

    assert _field_box(_Field("10k", visible=False)) is None
    assert _field_box(_Field("")) is None
