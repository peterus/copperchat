from __future__ import annotations

import dataclasses

import pytest

from copperchat.board.presets import PRESETS

# id: layers, min_track, min_space, pref_track, min_via_drill, min_via_diameter, min_drill,
#     min_npth, min_annular, copper_to_edge, hole_to_hole, silk h, silk line, thickness, oz
# min_drill 0.2 (plated holes): owner decision 2026-11-26 (spec §22.2); vias stay 0.3.
_TABLE = {
    "jlcpcb-2l": (2, 0.127, 0.127, 0.15, 0.3, 0.66, 0.2, 0.5, 0.18, 0.3, 0.45, 1.0, 0.15, 1.6, 1),
    "jlcpcb-4l": (4, 0.127, 0.127, 0.15, 0.3, 0.6, 0.2, 0.5, 0.15, 0.3, 0.45, 1.0, 0.15, 1.6, 1),
    "jlcpcb-6l": (6, 0.127, 0.127, 0.15, 0.3, 0.6, 0.2, 0.5, 0.15, 0.3, 0.45, 1.0, 0.15, 1.6, 1),
}


def test_preset_ids_and_layers() -> None:
    assert set(PRESETS) == {"jlcpcb-2l", "jlcpcb-4l", "jlcpcb-6l"}
    for key, p in PRESETS.items():
        assert p.id == key
    assert [PRESETS[k].layers for k in ("jlcpcb-2l", "jlcpcb-4l", "jlcpcb-6l")] == [2, 4, 6]


@pytest.mark.parametrize("key", sorted(_TABLE))
def test_preset_values_call_8(key: str) -> None:
    p = PRESETS[key]
    got = (
        p.layers,
        p.min_track,
        p.min_space,
        p.pref_track,
        p.min_via_drill,
        p.min_via_diameter,
        p.min_drill,
        p.min_npth,
        p.min_annular,
        p.copper_to_edge,
        p.hole_to_hole,
        p.silk_min_height,
        p.silk_min_line,
        p.thickness,
        p.outer_copper_oz,
    )
    assert got == _TABLE[key]


def test_via_annular_consistent() -> None:
    for p in PRESETS.values():
        assert (p.min_via_diameter - p.min_via_drill) / 2 >= p.min_annular - 1e-9
    p2 = PRESETS["jlcpcb-2l"]
    assert (p2.min_via_diameter - p2.min_via_drill) / 2 == pytest.approx(p2.min_annular)


def test_presets_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        PRESETS["jlcpcb-2l"].min_track = 0.1  # type: ignore[misc]
