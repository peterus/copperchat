"""kipy returns nanometres (spike 1C, Q3); every tool speaks millimetres."""

from __future__ import annotations

from typing import Any

NM_PER_MM = 1_000_000


def nm_to_mm(nm: float) -> float:
    return round(nm / NM_PER_MM, 4)


def mm_to_nm(mm: float) -> int:
    return round(mm * NM_PER_MM)


def pos_mm(v: Any) -> dict[str, float]:
    """`{"x", "y"}` in mm from anything with `.x` / `.y` in nm (kipy `Vector2`)."""
    return {"x": nm_to_mm(v.x), "y": nm_to_mm(v.y)}
