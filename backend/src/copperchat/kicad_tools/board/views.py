"""`pcb_render` (spec §7.1, §13): the board as a PNG for the model plus a zoomable SVG
link for the user. The pipeline lives in `views.board_render`; this module validates the
input against the live board (layer, nets), runs the DRC task for markers and shapes the
result. Read-only: nothing on the board changes; in a conversation the render is recorded
as approval evidence (`board_reviews`, P3b)."""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import math
from typing import Any

from copperchat.board.model import BoardModel, Rect
from copperchat.board.render import Marker
from copperchat.db import utcnow_iso
from copperchat.db.repos.board import BoardReviewRepo, board_file
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.checkpoints import board_written_since
from copperchat.kicad_tools.board.read import board_model, require_board
from copperchat.tools.spec import ImageBlock, ToolContext, ToolResult
from copperchat.views.board_render import ANGLES, BOARD_VIEWS, BoardRender, render_board

log = logging.getLogger(__name__)

RENDER_KEYS = frozenset({"view", "layer", "region", "highlight_nets", "markers", "angle"})
MAX_LAYER_CHARS = 32
MAX_NET_CHARS = 256
MAX_HIGHLIGHT_NETS = 20
MAX_COORD_MM = 1000.0
MAX_REGION_MM = 1000.0
MAX_MARKERS = 100  # listed and drawn; the rest is counted (markers_omitted)
MAX_MARKER_TEXT = 200
MARKERS_DRC_TIMEOUT_S = 60.0


def _invalid(what: str, why: str, suggestion: str = "see the tool schema") -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


def _not_found(kind: str, name: str, pool: set[str], suggestion: str) -> StructuredToolError:
    closest = difflib.get_close_matches(name, sorted(pool), n=5, cutoff=0.4)
    hint = f"Closest: {', '.join(closest)}" if closest else suggestion
    return StructuredToolError(
        f"{kind} {name[:80]!r} not found",
        f"the board has no {kind.lower()} of that name",
        hint,
        code="not_found",
        details={"closest": closest},
    )


def _number(value: Any, name: str, lo: float, hi: float, *, open_lo: bool = False) -> float:
    ok = isinstance(value, int | float) and not isinstance(value, bool)
    try:
        v = float(value) if ok else math.nan
    except OverflowError:  # a huge JSON int
        v = math.nan
    if not math.isfinite(v) or v > hi or v < lo or (open_lo and v <= lo):
        bound = f"{'>' if open_lo else '>='} {lo:g} and <= {hi:g}"
        raise _invalid(f"Invalid region.{name}", f"region.{name} must be a number {bound}")
    return v


def _region(value: Any) -> Rect | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"x", "y", "w", "h"}:
        raise _invalid("Invalid region", "region must be {x, y, w, h} (mm, board coordinates)")
    return Rect(
        x=_number(value["x"], "x", -MAX_COORD_MM, MAX_COORD_MM),
        y=_number(value["y"], "y", -MAX_COORD_MM, MAX_COORD_MM),
        w=_number(value["w"], "w", 0.0, MAX_REGION_MM, open_lo=True),
        h=_number(value["h"], "h", 0.0, MAX_REGION_MM, open_lo=True),
    )


def _nets(value: Any) -> list[str]:
    if value is None:
        return []
    ok = isinstance(value, list) and len(value) <= MAX_HIGHLIGHT_NETS
    if not ok or not all(isinstance(n, str) and 0 < len(n) <= MAX_NET_CHARS for n in value):
        raise _invalid(
            "Invalid highlight_nets",
            f"up to {MAX_HIGHLIGHT_NETS} net names of 1 to {MAX_NET_CHARS} characters",
        )
    return list(dict.fromkeys(value))


def _choice(value: Any, name: str, allowed: tuple[str, ...], default: str | None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise _invalid(f"Invalid {name}", f"{name} must be one of {', '.join(allowed)}")
    return value


def _board_nets(m: BoardModel) -> set[str]:
    nets = {pad.net for p in m.parts for pad in p.pads}
    nets.update(t.net for t in m.tracks)
    nets.update(a.net for a in m.arcs)
    nets.update(v.net for v in m.vias)
    nets.update(z.net for z in m.zones)
    nets.discard("")
    return nets


def _marker_rows(markers: tuple[api.DrcMarker, ...]) -> list[dict[str, Any]]:
    return [
        {
            "id": i,
            "type": mk.type,
            "severity": mk.severity,
            "text": mk.text[:MAX_MARKER_TEXT],
            "x": round(mk.pos[0], 4) if mk.pos is not None else None,
            "y": round(mk.pos[1], 4) if mk.pos is not None else None,
        }
        for i, mk in enumerate(markers[:MAX_MARKERS], start=1)
    ]


def _write_sidecar(out: BoardRender, name: str) -> None:
    """`<uuid>.svg.json` with the download name (best effort, as fp_render's)."""
    if out.svg is None:
        return
    sidecar = out.svg.with_name(out.svg.name + ".json")
    try:
        sidecar.write_text(json.dumps({"download_name": f"{name}.svg"}))
    except OSError as exc:
        log.warning("pcb_render: no download-name sidecar for %s (%s)", out.svg.name, exc)


async def _record_evidence(ctx: ToolContext, view: str, out: BoardRender, since: str) -> None:
    """The render as evidence for `pcb_approve` (P3b, index call 14): with a conversation
    and a database, one `render` row with both hashes of the rendered state - unless a
    board write was logged since the model was read (the image may show a newer state)."""
    if ctx.conversation_id is None or ctx.editing is None:
        return
    db = ctx.editing.db
    if await board_written_since(db, ctx.project.id, since):
        log.info("pcb_render: board written during the render; no evidence recorded")
        return
    await BoardReviewRepo(db).record_render(
        ctx.project.id,
        board_file(ctx.project),
        ctx.conversation_id,
        ctx.run_id,
        view=view,
        placement_hash=out.placement_hash,
        board_hash=out.board_hash,
    )


async def pcb_render(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """One PNG ImageBlock and `{view, svg_url, png_url, placement_hash, board_hash,
    markers}` (+ `markers_omitted` above 100 markers); read-only."""
    unknown = sorted(set(args) - RENDER_KEYS)
    if unknown:
        raise _invalid("Invalid arguments", f"unknown key(s) {unknown[:10]}")
    view = _choice(args.get("view"), "view", BOARD_VIEWS, None)
    angle = _choice(args.get("angle"), "angle", ANGLES, "top")
    layer = args.get("layer")
    if layer is not None and (not isinstance(layer, str) or not 0 < len(layer) <= MAX_LAYER_CHARS):
        raise _invalid("Invalid layer", f"layer must be a copper layer name (≤ {MAX_LAYER_CHARS})")
    if view == "layer" and layer is None:
        raise _invalid("Missing layer", "view 'layer' needs a copper layer, e.g. 'B.Cu'")
    if view != "layer" and layer is not None:
        raise _invalid("Unexpected layer", "layer is only used with view 'layer'")
    region = _region(args.get("region"))
    nets = _nets(args.get("highlight_nets"))
    want_markers = args.get("markers", False)
    if not isinstance(want_markers, bool):
        raise _invalid("Invalid markers", "markers must be true or false")
    if view == "3d" and (region is not None or nets or want_markers):
        raise _invalid(
            "Unexpected argument", "markers, highlight_nets and region are only used with 2D views"
        )
    require_board(ctx)
    since = utcnow_iso()
    m = await board_model(ctx)
    if layer is not None and layer not in m.copper_layers:
        raise _not_found("Layer", layer, set(m.copper_layers), "use a layer pcb_status counts")
    known = _board_nets(m)
    for net in nets:
        if net not in known:
            raise _not_found("Net", net, known, "use pcb_get_net or pcb_list_footprints")
    rows: list[dict[str, Any]] = []
    omitted = 0
    drawn: list[Marker] = []
    if want_markers:
        drc = await ctx.session.call(
            lambda h: api.run_drc_task(h, timeout_s=MARKERS_DRC_TIMEOUT_S),
            timeout=api.drc_call_timeout(MARKERS_DRC_TIMEOUT_S),
        )
        rows = _marker_rows(drc.markers)
        omitted = max(0, len(drc.markers) - MAX_MARKERS)
        drawn = [Marker(r["id"], r["x"], r["y"]) for r in rows if r["x"] is not None]
    out = await render_board(
        ctx,
        view,  # type: ignore[arg-type]  # validated against BOARD_VIEWS
        layer=layer,
        region=region,
        highlight_nets=nets,
        markers=drawn,
        angle=angle,  # type: ignore[arg-type]  # validated against ANGLES
        model=m,
    )
    png = await asyncio.to_thread(out.png.read_bytes)
    await asyncio.to_thread(_write_sidecar, out, f"{ctx.project.name}_board_{view}")
    base = f"/api/projects/{ctx.project.id}/renders"
    data: dict[str, Any] = {
        "view": view,
        "svg_url": f"{base}/{out.svg.name}" if out.svg is not None else None,
        "png_url": f"{base}/{out.png.name}",
        "placement_hash": out.placement_hash,
        "board_hash": out.board_hash,
        "markers": rows,
    }
    if omitted:
        data["markers_omitted"] = omitted
    if region is None:  # a zoomed render shows only part of the board: no evidence
        await _record_evidence(ctx, view, out, since)
    return ToolResult(data=data, images=[ImageBlock(mime="image/png", data=png, path=out.png)])
