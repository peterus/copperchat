"""Fab presets (B-C8, pure data, mm). Values verified against the JLCPCB capabilities
page (2026-11-25). `hole_to_hole` is the stricter pad value (0.45), applied globally because
KiCad has one `hole_to_hole_min`; the 0.2 mm via value is then implied. Hole-to-edge checks
use `copper_to_edge`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal


@dataclass(frozen=True)
class FabPreset:
    id: Literal["jlcpcb-2l", "jlcpcb-4l", "jlcpcb-6l"]
    layers: int
    min_track: float
    min_space: float
    pref_track: float
    min_via_drill: float
    min_via_diameter: float
    min_drill: float
    min_npth: float
    min_annular: float
    copper_to_edge: float
    hole_to_hole: float
    silk_min_height: float
    silk_min_line: float
    thickness: float
    outer_copper_oz: float


PRESETS: Mapping[str, FabPreset] = MappingProxyType(
    {
        "jlcpcb-2l": FabPreset(
            "jlcpcb-2l",
            2,
            0.127,
            0.127,
            0.15,
            0.3,
            0.66,
            0.3,
            0.5,
            0.18,
            0.3,
            0.45,
            1.0,
            0.15,
            1.6,
            1,
        ),
        "jlcpcb-4l": FabPreset(
            "jlcpcb-4l",
            4,
            0.127,
            0.127,
            0.15,
            0.3,
            0.6,
            0.3,
            0.5,
            0.15,
            0.3,
            0.45,
            1.0,
            0.15,
            1.6,
            1,
        ),
        "jlcpcb-6l": FabPreset(
            "jlcpcb-6l",
            6,
            0.127,
            0.127,
            0.15,
            0.3,
            0.6,
            0.3,
            0.5,
            0.15,
            0.3,
            0.45,
            1.0,
            0.15,
            1.6,
            1,
        ),
    }
)
