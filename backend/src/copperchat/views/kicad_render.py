"""KiCad's own rendering via the api-server's PNG export jobs (spike 1C, Q5)."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from typing import Any

from kipy.board_jobs import PlotSettings
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.schematic.schematic_jobs_pb2 import SJSM_SINGLE_SHEET
from kipy.util.board_layer import CANONICAL_LAYER_NAMES, layer_from_canonical_name
from PIL import Image

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import DocKind, KicadHandle, KicadSession
from copperchat.kicad_tools.access import sheet_label
from copperchat.kicad_tools.filtering import not_found

# Spike 1C (Q5): both PNG export jobs exist at the pinned commit, and a PNG is the
# whole page with `mm = px * 25.4 / dpi` from the page's top-left corner (board:
# only with PlotSettings.scale = 1).
API_PNG_EXPORT = True
PIXEL_MAPPING_KNOWN = True
DEFAULT_BOARD_LAYERS = ("F.Cu", "B.Cu", "F.SilkS", "B.SilkS", "Edge.Cuts")
MIN_DPI, MAX_DPI = 72, 600
BOARD_LAYER_NAMES = tuple(
    name for layer, name in CANONICAL_LAYER_NAMES.items() if layer != BoardLayer.BL_UNKNOWN
)


def px_per_mm(dpi: int) -> float:
    return dpi / 25.4


def dpi_for(width_mm: float, target_px: int = 1600) -> int:
    """Resolution that renders `width_mm` about `target_px` wide."""
    return max(MIN_DPI, min(MAX_DPI, round(target_px * 25.4 / max(width_mm, 1.0))))


def board_layers(names: list[str] | None) -> list[str]:
    """Canonical layer names; unknown names are a tool error with suggestions."""
    chosen = list(names) if names else list(DEFAULT_BOARD_LAYERS)
    for name in chosen:
        if name not in BOARD_LAYER_NAMES:
            raise not_found("Layer", name, BOARD_LAYER_NAMES)
    return chosen


def _select_sheet(h: KicadHandle, sheet: str | None) -> Any:
    """The schematic bound to one sheet instance, by path (`/amp/`); root by default."""
    sch = h.schematic()
    sheets = list(sch.get_hierarchy())
    if not sheet:
        return sch.for_sheet(sheets[0])
    for s in sheets:
        if sheet_label(s) == sheet:
            return sch.for_sheet(s)
    raise not_found("Sheet", sheet, [sheet_label(s) for s in sheets])


def _export(doc: DocKind, out: Path, dpi: int, layers: list[str], sheet: str | None) -> Any:
    def run(h: KicadHandle) -> Any:
        if doc == "sch":
            return _select_sheet(h, sheet).export_png(
                str(out), dpi=dpi, sheet_mode=SJSM_SINGLE_SHEET
            )
        settings = PlotSettings()
        settings.layers = [layer_from_canonical_name(n) for n in layers]
        settings.scale = 1.0  # keeps the page mapping (spike 1C, Q5)
        return h.board().export_png(str(out), plot_settings=settings, dpi=dpi)

    return run


async def export_kicad_png(
    session: KicadSession,
    doc: DocKind,
    *,
    dpi: int,
    layers: list[str] | None = None,
    sheet: str | None = None,
    background: tuple[int, int, int] = (255, 255, 255),
) -> Image.Image:
    """The whole page as KiCad plots it, flattened onto `background` (RGB)."""
    chosen = board_layers(layers) if doc == "pcb" else []
    with tempfile.TemporaryDirectory(prefix="cc-render-") as tmp:
        out = Path(tmp) / "page.png"
        result = await session.call(_export(doc, out, dpi, chosen, sheet))
        written = [Path(p) for p in result.output_paths] or [out]
        if not result.succeeded or not written[0].is_file():
            raise StructuredToolError(
                "KiCad export failed",
                "the api-server's PNG export job did not produce an image",
                "Use render_view with mode=agent",
            )
        with Image.open(written[0]) as raw:
            page = raw.convert("RGBA")
    flat = Image.new("RGBA", page.size, (*background, 255))
    flat.alpha_composite(page)
    return flat.convert("RGB")


def crop_mm(
    img: Image.Image, dpi: int, box: tuple[float, float, float, float]
) -> tuple[Image.Image, tuple[float, float]]:
    """Crops a page image to a mm box; returns the crop and its top-left in mm."""
    scale = px_per_mm(dpi)
    x, y, w, h = box
    left, top = max(0, round(x * scale)), max(0, round(y * scale))
    right, bottom = min(img.width, round((x + w) * scale)), min(img.height, round((y + h) * scale))
    if right - left < 2 or bottom - top < 2:
        raise StructuredToolError(
            "Region outside the page",
            "the requested region does not overlap the plotted page",
            "Use mode=agent, or pick a region inside the drawing",
        )
    return img.crop((left, top, right, bottom)), (left / scale, top / scale)


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
