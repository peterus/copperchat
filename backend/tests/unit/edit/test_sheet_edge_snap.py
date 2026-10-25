"""`sheets.snap_to_sheet_edge` (v0.5.0 #1, spec §5.2): the edge a moved sheet pin lands on."""

from __future__ import annotations

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.geometry import Box
from copperchat.kicad_tools.edit.sheets import sheet_edges, snap_to_sheet_edge

BOX = Box(50.8, 50.8, 101.6, 88.9)


def test_edges_keep_one_pitch_from_the_corners() -> None:
    assert sheet_edges(BOX) == {
        "left": {"x": 50.8, "y": [53.34, 86.36]},
        "right": {"x": 101.6, "y": [53.34, 86.36]},
        "top": {"y": 50.8, "x": [53.34, 99.06]},
        "bottom": {"y": 88.9, "x": [53.34, 99.06]},
    }


def test_too_short_edge_is_left_out() -> None:
    assert set(sheet_edges(Box(0, 0, 50.8, 3.81))) == {"top", "bottom"}


@pytest.mark.parametrize(
    ("point", "side", "at"),
    [
        ((49.53, 60.96), "left", (50.8, 60.96)),  # outside, 1.27 mm off
        ((52.07, 60.96), "left", (50.8, 60.96)),  # inside
        ((102.87, 63.5), "right", (101.6, 63.5)),
        ((76.2, 49.53), "top", (76.2, 50.8)),
        ((76.2, 90.17), "bottom", (76.2, 88.9)),
        ((50.8, 88.9), "left", (50.8, 86.36)),  # the corner: clamped, current side wins
        ((50.8, 60.33), "left", (50.8, 60.96)),  # snapped to the grid
    ],
)
def test_nearest_edge(point: tuple[float, float], side: str, at: tuple[float, float]) -> None:
    assert snap_to_sheet_edge("S", BOX, point, [], current="left") == (side, at)


def test_corner_tie_without_current_side_is_deterministic() -> None:
    assert snap_to_sheet_edge("S", BOX, (50.8, 88.9), [], current="bottom")[0] == "bottom"


def test_far_from_every_edge_refused_with_extents() -> None:
    with pytest.raises(StructuredToolError) as exc:
        snap_to_sheet_edge("S", BOX, (76.2, 69.85), [])
    assert exc.value.code == "invalid_argument"
    assert exc.value.details["edges"]["top"] == {"y": 50.8, "x": [53.34, 99.06]}


def test_beyond_the_corner_refused() -> None:
    with pytest.raises(StructuredToolError):
        snap_to_sheet_edge("S", BOX, (50.8, 95.25), [])  # on the left line, 6.35 mm below


def test_onto_another_pin_refused_other_edge_not() -> None:
    others = [("GND", "left", (50.8, 60.96))]
    with pytest.raises(StructuredToolError) as exc:
        snap_to_sheet_edge("S", BOX, (50.8, 62.23), others)
    assert exc.value.code == "overlap" and "S.GND" in exc.value.why
    assert snap_to_sheet_edge("S", BOX, (50.8, 63.5), others) == ("left", (50.8, 63.5))
    assert snap_to_sheet_edge("S", BOX, (101.6, 60.96), others) == ("right", (101.6, 60.96))
