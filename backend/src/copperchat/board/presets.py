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


@dataclass(frozen=True)
class StackupLayer:
    kind: Literal["copper", "prepreg", "core"]
    thickness: float  # mm
    material: str
    epsilon_r: float | None = None  # dielectrics only


@dataclass(frozen=True)
class StackupPreset:
    """A fab's layer build-up, top to bottom (copper and dielectric alternate). The
    solder mask is not part of it: KiCad keeps its own mask layers."""

    id: str
    copper_layers: int
    jlc_name: str | None
    layers: tuple[StackupLayer, ...]

    @property
    def total_mm(self) -> float:
        return sum(layer.thickness for layer in self.layers)


def _cu(thickness: float) -> StackupLayer:
    return StackupLayer("copper", thickness, "copper")


# Thicknesses and dielectric constants from jlcpcb.com/impedance (read 2026-11-25):
# prepreg 7628 Er 4.4, 3313 Er 4.1, 2116 Er 4.16, core Er 4.6. JLC publishes no loss
# tangent, so KiCad's value stays. The 2-layer build-up is not published by JLC: 1 oz
# copper on a 1.53 mm FR-4 core (1.6 mm copper + dielectric), Er 4.5
# (jlcpcb.com/capabilities, "4.5 (2-Layer PCB)"). KiCad's board thickness adds its solder
# mask layers on top of `total_mm`.
_P7628 = StackupLayer("prepreg", 0.2104, "7628", 4.4)
_P3313 = StackupLayer("prepreg", 0.0994, "3313", 4.1)
_P2116 = StackupLayer("prepreg", 0.1088, "2116", 4.16)
_CORE_055 = StackupLayer("core", 0.55, "FR4", 4.6)

STACKUPS: Mapping[str, StackupPreset] = MappingProxyType(
    {
        "jlc-2l-1.6": StackupPreset(
            "jlc-2l-1.6",
            2,
            None,
            (_cu(0.035), StackupLayer("core", 1.53, "FR4", 4.5), _cu(0.035)),
        ),
        "jlc-4l-1.6-7628": StackupPreset(
            "jlc-4l-1.6-7628",
            4,
            "JLC04161H-7628",
            (
                _cu(0.035),
                _P7628,
                _cu(0.0152),
                StackupLayer("core", 1.065, "FR4", 4.6),
                _cu(0.0152),
                _P7628,
                _cu(0.035),
            ),
        ),
        "jlc-6l-1.6": StackupPreset(
            "jlc-6l-1.6",
            6,
            "JLC06161H-3313",
            (
                _cu(0.035),
                _P3313,
                _cu(0.0152),
                _CORE_055,
                _cu(0.0152),
                _P2116,
                _cu(0.0152),
                _CORE_055,
                _cu(0.0152),
                _P3313,
                _cu(0.035),
            ),
        ),
    }
)
