"""`fp_render` (footprints spec §7.5, §11): one footprint as a PNG for the model plus a
zoomable SVG for the user. The pipeline lives in `views.footprint_render`; this module
validates the input, writes the SVG download-name sidecar (index author call 3) and
shapes the result."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.footprints.read import parse_lib_id, require_enabled
from copperchat.tools.spec import ImageBlock, ToolContext, ToolResult
from copperchat.views.footprint_render import (
    GRID_LADDER,
    LAYER_RE,
    MAX_LAYERS,
    RenderOutput,
    render_footprint,
)

log = logging.getLogger(__name__)

RENDER_KEYS = frozenset({"lib_id", "layers", "grid_mm", "labels"})


def _invalid(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


def _layers(value: Any) -> list[str] | None:
    if value is None:
        return None
    ok = isinstance(value, list) and 0 < len(value) <= MAX_LAYERS
    if not ok or not all(isinstance(n, str) and LAYER_RE.fullmatch(n) for n in value):
        raise _invalid(
            "Invalid layers",
            f"1 to {MAX_LAYERS} names, each F./B. + Cu, Mask, Paste, SilkS, Fab, CrtYd "
            "or Edge.Cuts",
            "Omit layers for the default set (front layers, back layers when used)",
        )
    return list(value)


def _grid(value: Any) -> float | None:
    if value is None:
        return None
    ok = isinstance(value, int | float) and not isinstance(value, bool)
    try:
        v = float(value) if ok else math.nan
    except OverflowError:  # a huge JSON int such as 10**400
        v = math.nan
    if not math.isfinite(v) or v not in GRID_LADDER:
        raise _invalid(
            "Invalid grid_mm",
            f"grid_mm must be one of {', '.join(str(g) for g in GRID_LADDER)}",
            "Omit it for an automatic grid",
        )
    return v


def _labels(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, bool):
        raise _invalid("Invalid labels", "labels must be true or false", "Omit it for true")
    return value


def _write_sidecar(out: RenderOutput, nickname: str, name: str) -> None:
    """`<uuid>.svg.json` with the download name; unsanitised here (the renders route
    reduces it). Best effort: without it the route sends `<uuid>.svg`."""
    sidecar = out.svg.with_name(out.svg.name + ".json")
    try:
        sidecar.write_text(json.dumps({"download_name": f"{nickname}_{name}.svg"}))
    except OSError as exc:
        log.warning("fp_render: no download-name sidecar for %s (%s)", out.svg.name, exc)


def _mm(values: tuple[float, float]) -> list[float]:
    return [round(v, 4) for v in values]


async def fp_render(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """`{lib_id, image, svg, svg_url, scale_px_per_mm, extent_mm, origin_mm, grid_mm,
    geometry_hash}` (+ `notes` when non-empty) and one PNG ImageBlock; read-only."""
    unknown = sorted(set(args) - RENDER_KEYS)
    if unknown:
        raise _invalid("Invalid arguments", f"unknown key(s) {unknown}", "see the tool schema")
    lib_id, nickname, name = parse_lib_id(args.get("lib_id"))
    layers = _layers(args.get("layers"))
    grid_mm = _grid(args.get("grid_mm"))
    labels = _labels(args.get("labels"))
    require_enabled(ctx)
    notes: list[dict[str, Any]] = []
    out = await render_footprint(ctx, lib_id, layers, grid_mm, labels, notes=notes)
    png = await asyncio.to_thread(out.png.read_bytes)
    await asyncio.to_thread(_write_sidecar, out, nickname, name)
    data: dict[str, Any] = {
        "lib_id": lib_id,
        "image": out.png.name,
        "svg": out.svg.name,
        # same-origin path the chat markdown allows (web/security.py): link it for zooming
        "svg_url": f"/api/projects/{ctx.project.id}/renders/{out.svg.name}",
        "scale_px_per_mm": round(out.scale_px_per_mm, 4),
        "extent_mm": _mm(out.extent_mm),
        "origin_mm": _mm(out.origin_mm),
        "grid_mm": out.grid_mm,
        "geometry_hash": out.geometry_hash,
    }
    if notes:
        data["notes"] = notes
    return ToolResult(data=data, images=[ImageBlock(mime="image/png", data=png, path=out.png)])
