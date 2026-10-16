from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Literal

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import DocKind, KicadHandle, KicadSession
from copperchat.kicad_tools.access import hierarchy
from copperchat.kicad_tools.checks import cached_violations
from copperchat.kicad_tools.filtering import not_found
from copperchat.kicad_tools.pcb import board_boxes, snapshot_with_connectivity
from copperchat.kicad_tools.schematic import symbol_boxes
from copperchat.projects import agent_dir
from copperchat.tools.spec import ImageBlock
from copperchat.views.agent_render import THEMES, overlay_grid, render_board
from copperchat.views.kicad_render import (
    board_layers,
    dpi_for,
    export_kicad_png,
    px_per_mm,
    to_png,
)
from copperchat.views.region import Box, Region, resolve_region

Mode = Literal["agent", "kicad"]
Theme = Literal["agent", "light", "dark"]
PAGE_DPI = 150  # whole A4 page ~1750 px wide: readable without being huge
BOARD_MARGIN_MM = 3.0


async def render_view(
    session: KicadSession,
    doc: DocKind,
    mode: Mode,
    region: Region | None = None,
    layers: list[str] | None = None,
    theme: Theme = "agent",
    *,
    sheet: str | None = None,
    persist: bool = True,
) -> ImageBlock:
    """PNG of the schematic or board; also written to `.kicad-agent/renders/<uuid>.png`
    unless `persist=False`. Check markers are cached per document state.

    agent + pcb: our renderer (grid, labels, net colours, airwires, DRC markers).
    kicad + pcb: KiCad's plot of `layers`, cropped to the board (or region).
    sch (both): KiCad's plot of one sheet (`sheet`, default root; `around_ref` picks
    the symbol's sheet); agent mode adds the mm grid and ERC markers.
    """
    if (session.project.sch_file if doc == "sch" else session.project.pcb_file) is None:
        raise StructuredToolError(
            "Nothing to render",
            f"the project has no {'schematic' if doc == 'sch' else 'board'}",
            "Render the document the project has",
        )
    if doc == "pcb":
        if sheet:
            raise StructuredToolError(
                "Invalid argument", "sheet applies to the schematic only", "Omit sheet for doc=pcb"
            )
        data = await (_agent_board if mode == "agent" else _kicad_board)(
            session, region, layers, theme
        )
    else:
        if layers:
            raise StructuredToolError(
                "Invalid argument", "layers apply to the board only", "Omit layers for doc=sch"
            )
        data = await _schematic(session, mode, region, sheet)
    if not persist:  # e.g. the viewer's /agent-view: served once, never referenced again
        return ImageBlock(mime="image/png", data=data, path=None)
    out_dir = agent_dir(session.project) / "renders"
    path = out_dir / f"{uuid.uuid4().hex}.png"

    def write() -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    await asyncio.to_thread(write)
    return ImageBlock(mime="image/png", data=data, path=path)


async def _agent_board(
    session: KicadSession, region: Region | None, layers: list[str] | None, theme: Theme
) -> bytes:
    if layers:
        board_layers(layers)  # validates the names
    snap = await snapshot_with_connectivity(session)
    box = resolve_region(region, snap)
    markers = await cached_violations(session, "pcb")
    return await asyncio.to_thread(
        render_board, snap, region_box=box, layers=layers, theme=theme, markers=markers
    )


async def _kicad_board(
    session: KicadSession, region: Region | None, layers: list[str] | None, theme: Theme
) -> bytes:
    boxes = await session.call(board_boxes)
    box: Box | None = resolve_region(region, boxes)
    if box is None and boxes["outline"] is not None:
        o, m = boxes["outline"], BOARD_MARGIN_MM
        box = (o["x"] - m, o["y"] - m, o["w"] + 2 * m, o["h"] + 2 * m)
    dpi = dpi_for(box[2]) if box else PAGE_DPI
    background = THEMES["light" if theme == "light" else "dark"]["bg"]
    page, _ = await export_kicad_png(
        session, "pcb", dpi=dpi, layers=layers, background=background, box=box
    )
    return await asyncio.to_thread(to_png, page)


def _sheet_path(sheet: str | None) -> Callable[[KicadHandle], str]:
    """Sheet path (`/`, `/amp/`) for a sheet given by path or name; root by default."""

    def run(h: KicadHandle) -> str:
        sheets = hierarchy(h.schematic())
        if not sheet:
            return str(sheets[0]["path"])
        for s in sheets:
            if sheet in (s["path"], s["name"], s["path"].strip("/")):
                return str(s["path"])
        raise not_found("Sheet", sheet, [s["path"] for s in sheets])

    return run


async def _schematic(
    session: KicadSession, mode: Mode, region: Region | None, sheet: str | None
) -> bytes:
    box: Box | None = None
    path = await session.call(_sheet_path(sheet)) if sheet else None
    if region is not None:
        symbols = await session.call(symbol_boxes)
        if region.around_ref and path is None:
            hits = [s for s in symbols if s["ref"] == region.around_ref]
            path = hits[0]["sheet"] if hits else None  # first sheet instance with the part
        if path is not None:
            symbols = [s for s in symbols if s["sheet"] == path]
        box = resolve_region(region, {"symbols": symbols})
    if path is None:
        path = await session.call(_sheet_path(None))
    dpi = dpi_for(box[2]) if box else PAGE_DPI
    page, origin = await export_kicad_png(session, "sch", dpi=dpi, sheet=path, box=box)
    if mode == "kicad":
        return await asyncio.to_thread(to_png, page)
    markers = [v for v in await cached_violations(session, "sch") if v.get("sheet") == path]
    return await asyncio.to_thread(overlay_grid, page, origin, px_per_mm(dpi), markers, "light")
