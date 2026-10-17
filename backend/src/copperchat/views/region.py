from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.filtering import not_found

Box = tuple[float, float, float, float]  # x, y, w, h in mm


@dataclass(frozen=True)
class Region:
    """Either a mm box (`x_mm`, `y_mm`, `w_mm`, `h_mm`) or "around REF" (+ margin)."""

    x_mm: float | None = None
    y_mm: float | None = None
    w_mm: float | None = None
    h_mm: float | None = None
    around_ref: str | None = None
    margin_mm: float = 5.0


def resolve_region(region: Region | None, snap: dict[str, Any]) -> Box | None:
    """mm box for `region`. `snap` lists `footprints` (board) or `symbols` (schematic),
    each with `ref` and `bbox_mm` `{x, y, w, h}`."""
    if region is None:
        return None
    if region.margin_mm < 0:
        raise StructuredToolError(
            "Invalid region", "margin_mm must not be negative", "Pass margin_mm >= 0"
        )
    if region.around_ref:
        kind, items = (
            ("Symbol", snap["symbols"]) if "symbols" in snap else ("Footprint", snap["footprints"])
        )
        hit = next((f for f in items if f["ref"] == region.around_ref and f["bbox_mm"]), None)
        if hit is None:
            raise not_found(kind, region.around_ref, (f["ref"] for f in items))
        b, m = hit["bbox_mm"], region.margin_mm
        return (b["x"] - m, b["y"] - m, b["w"] + 2 * m, b["h"] + 2 * m)
    x, y, w, h = region.x_mm, region.y_mm, region.w_mm, region.h_mm
    if x is None or y is None or w is None or h is None or w <= 0 or h <= 0:
        raise StructuredToolError(
            "Invalid region",
            "a mm box needs x_mm, y_mm, w_mm > 0 and h_mm > 0",
            "Pass x_mm, y_mm, w_mm, h_mm or around_ref",
        )
    return (float(x), float(y), float(w), float(h))
