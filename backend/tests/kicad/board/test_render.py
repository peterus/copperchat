"""`pcb_render` on the real api-server and kicad-cli (SP4 P1c Task 5, B-C9).

One ecc83 api-server for the module (`ecc83_module_session`): the tests only read, except
the last one, which adds GND pours on both sides through the API and refills them (the
zone-filled board the board SVG limits are measured on). The measured numbers (KiCad's
SVG: bytes, elements, coordinate pairs, the largest element, line span; the rasteriser:
seconds and peak RSS) are printed and recorded with `record_property`.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import json
import math
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from kipy.board_types import Zone
from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode
from kipy.proto.board.board_types_pb2 import BoardLayer
from PIL import Image

from copperchat.board.model import BoardModel
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.read import load_board_model
from copperchat.kicad_tools.board.status import pcb_status
from copperchat.kicad_tools.board.views import pcb_render
from copperchat.kicad_tools.cli import run_kicad_cli
from copperchat.projects import agent_dir
from copperchat.views import board_render as br
from copperchat.views import footprint_render as fr
from tests.support.edit import edit_ctx

pytestmark = pytest.mark.kicad
module_loop = pytest.mark.asyncio(loop_scope="module")
UNLIMITED = fr.SvgLimits(10**12, 10**12, 10**12, 10**12, math.inf)


def _png_side(png: bytes) -> int:
    with Image.open(io.BytesIO(png)) as img:
        assert img.format == "PNG"
        return max(img.size)


def _svg_cost(data: bytes) -> dict[str, float]:
    """What KiCad's SVG costs by the sanitiser's measures (no cap applied)."""
    text = data.decode("utf-8-sig")
    match = fr._KICAD_PROLOG.match(text) or fr._XML_DECL.match(text)
    root = ET.fromstring(text[match.end() :] if match else text)
    tally = fr._Tally(max(fr._view_box(root)[2:]), UNLIMITED)
    fr._clean(root, 0, tally)
    return {
        "bytes": len(data),
        "elements": tally.elements,
        "coords": tally.coords,
        "max_element_coords": tally.peak,
        "span": round(tally.span, 1),
    }


async def _raw_svg(s: KicadSession, view: br.BoardView, tmp: Path) -> bytes:
    """KiCad's own export of `view` (the input of the sanitiser), like render_board."""
    board = tmp / "measure.kicad_pcb"
    out = tmp / "measure.svg"
    await s.save_copy("pcb", board)
    result = await run_kicad_cli(
        s.settings,
        br.svg_export_args(view, None, board, out),
        timeout=br.BOARD_SVG_TIMEOUT_S,
        cwd=tmp,
    )
    assert result.returncode == 0, result.stderr
    return out.read_bytes()


_RASTER_PROBE = """
import asyncio, json, resource, sys, time
from copperchat.views import board_render as br, footprint_render as fr
svg = open(sys.argv[1], "rb").read()
t0 = time.monotonic()
png, _ = asyncio.run(fr.rasterise(
    svg, (float(sys.argv[2]), float(sys.argv[3])), max_side_px=br.BOARD_MAX_SIDE_PX,
    timeout_s=br.BOARD_RASTER_TIMEOUT_S, memory_bytes=br.BOARD_RASTER_MEMORY_BYTES))
rss_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
seconds = round(time.monotonic() - t0, 2)
print(json.dumps({"seconds": seconds, "peak_rss_mb": round(rss_kb / 1024, 1)}))
"""


def _raster_cost(svg: Path) -> dict[str, float]:
    """Seconds and peak RSS of the board rasteriser worker on the wrapper SVG `svg`, in a
    fresh interpreter (its only child is the worker: RUSAGE_CHILDREN is the worker's)."""
    root = ET.fromstring(svg.read_bytes())
    _, _, w, h = (float(v) for v in root.get("viewBox", "").split())
    out = subprocess.run(
        [sys.executable, "-c", _RASTER_PROBE, str(svg), str(w), str(h)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _record(record_property: Any, name: str, values: dict[str, float]) -> None:
    print(f"{name}: {values}")
    for key, value in values.items():
        record_property(f"{name}.{key}", value)


async def _model(s: KicadSession) -> BoardModel:
    model: BoardModel = await s.call(lambda h: load_board_model(h, s.project), timeout=120)
    return model


@module_loop
async def test_render_each_view(
    ecc83_module_session: KicadSession, tmp_path: Path, record_property: Any
) -> None:
    s = ecc83_module_session
    ctx = edit_ctx(s)
    status = (await pcb_status(ctx, {})).data
    assert status is not None
    renders = agent_dir(s.project) / "renders"
    for args in (
        {"view": "top"},
        {"view": "bottom"},
        {"view": "layer", "layer": "B.Cu"},
        {"view": "placement"},
        {"view": "3d", "angle": "iso"},
    ):
        started = time.monotonic()
        result = await pcb_render(ctx, args)
        seconds = round(time.monotonic() - started, 2)
        data = result.data
        assert data is not None, args
        print(args["view"], f"{seconds} s", _png_side(result.images[0].data), "px")
        assert len(result.images) == 1
        assert _png_side(result.images[0].data) <= br.BOARD_MAX_SIDE_PX
        assert data["view"] == args["view"]
        assert data["placement_hash"] == status["placement_hash"]
        assert data["board_hash"] == status["board_hash"]
        assert data["markers"] == []
        png_name = data["png_url"].rsplit("/", 1)[1]
        assert (renders / png_name).is_file()
        if args["view"] == "3d":
            assert data["svg_url"] is None
        else:
            svg = renders / data["svg_url"].rsplit("/", 1)[1]
            ET.fromstring(svg.read_bytes())  # well-formed
            assert (svg.parent / f"{svg.name}.json").is_file()
            if args["view"] == "top":
                _record(
                    record_property, "ecc83_top_svg", _svg_cost(await _raw_svg(s, "top", tmp_path))
                )
                _record(record_property, "ecc83_top_raster", _raster_cost(svg))
    assert not list((renders / "tmp").glob("*")), "temporary board copies left behind"
    listed = json.loads((renders / br.BOARD_SVGS_FILE).read_text())
    assert len(listed) == 4  # four 2D views; 3d has no SVG


@module_loop
async def test_render_unknown_layer(ecc83_module_session: KicadSession) -> None:
    ctx = edit_ctx(ecc83_module_session)
    with pytest.raises(StructuredToolError) as exc:
        await pcb_render(ctx, {"view": "layer", "layer": "In5.Cu"})
    assert exc.value.code == "not_found"
    assert exc.value.details is not None and "closest" in exc.value.details
    with pytest.raises(StructuredToolError) as exc:
        await pcb_render(ctx, {"view": "top", "highlight_nets": ["NO_SUCH_NET_XYZ"]})
    assert exc.value.code == "not_found" and "closest" in (exc.value.details or {})


@module_loop
async def test_render_highlight_and_markers(
    ecc83_module_session: KicadSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = ecc83_module_session
    m = await _model(s)
    net = sorted({pad.net for p in m.parts for pad in p.pads if pad.net})[0]
    current: dict[str, Any] = {}
    drc_timeouts: list[float | None] = []
    original_call = s.call
    original_drc = api.run_drc_task

    async def call(fn: Any, *, timeout: float | None = None) -> Any:
        current["timeout"] = timeout
        return await original_call(fn, timeout=timeout)

    def drc(h: KicadHandle, *, timeout_s: float = 60.0) -> api.DrcResult:
        drc_timeouts.append(current.get("timeout"))
        return original_drc(h, timeout_s=timeout_s)

    monkeypatch.setattr(s, "call", call)
    monkeypatch.setattr(api, "run_drc_task", drc)
    ctx = edit_ctx(s)
    result = await pcb_render(
        ctx, {"view": "top", "highlight_nets": [net], "markers": True, "region": m_region(m)}
    )
    data = result.data
    assert data is not None
    assert drc_timeouts == [api.drc_call_timeout(60)]
    assert data["markers"], "the ecc83 demo board has DRC markers"
    assert [mk["id"] for mk in data["markers"]] == list(range(1, len(data["markers"]) + 1))
    svg = (agent_dir(s.project) / "renders" / data["svg_url"].rsplit("/", 1)[1]).read_text()
    assert 'id="highlight"' in svg and 'id="markers"' in svg


def m_region(m: BoardModel) -> dict[str, float]:
    """The board outline's bounding box as a pcb_render region."""
    o = m.outline
    return {"x": o.x, "y": o.y, "w": o.w, "h": o.h}


async def test_render_boardless(blank_nopcb_session: KicadSession) -> None:
    with pytest.raises(StructuredToolError) as exc:
        await pcb_render(edit_ctx(blank_nopcb_session), {"view": "top"})
    assert exc.value.code == "no_board"


@module_loop
async def test_board_cli_serialised(
    ecc83_module_session: KicadSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, float, float, float]] = []
    original = br.run_kicad_cli

    async def spy(
        settings: Any, args: list[str], *, timeout: float, cwd: Path | None = None
    ) -> Any:
        entered = time.monotonic()
        try:
            return await original(settings, args, timeout=timeout, cwd=cwd)
        finally:
            calls.append((" ".join(args[:3]), timeout, entered, time.monotonic()))

    monkeypatch.setattr(br, "run_kicad_cli", spy)
    ctx = edit_ctx(ecc83_module_session)
    await asyncio.gather(
        pcb_render(ctx, {"view": "top"}),
        pcb_render(ctx, {"view": "3d", "angle": "top"}),
        pcb_render(ctx, {"view": "bottom"}),
    )
    assert len(calls) == 3
    spans = sorted((enter, leave) for _, _, enter, leave in calls)
    for (_, leave), (enter, _) in itertools.pairwise(spans):
        assert leave <= enter, "two board kicad-cli calls overlapped"
    for what, timeout, _, _ in calls:
        if what == "pcb export svg":
            assert timeout == 60
        else:
            assert what.startswith("pcb render") and timeout == 120


@module_loop
async def test_render_cli_timeout(
    ecc83_module_session: KicadSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hang(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError

    monkeypatch.setattr(br, "run_kicad_cli", hang)
    ctx = edit_ctx(ecc83_module_session)
    for args in ({"view": "top"}, {"view": "3d"}):
        with pytest.raises(StructuredToolError) as exc:
            await pcb_render(ctx, args)
        assert exc.value.code == "render_failed"
        assert "took longer than" in exc.value.why
        assert not br.BOARD_CLI_LOCK.locked()
    tmp = agent_dir(ecc83_module_session.project) / "renders" / "tmp"
    assert not list(tmp.glob("*")), "the temporary board copy was not deleted"


def _gnd_pours(h: KicadHandle, outline: tuple[float, float, float, float]) -> None:
    """GND zones over the whole outline box on F.Cu and B.Cu."""
    board = h.board()
    (gnd,) = [n for n in board.get_nets() if n.name == "GND"]
    x, y, w, hh = outline
    zones = []
    for layer in (BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu):
        z = Zone()
        z.layers = [layer]
        z.net = gnd
        line = PolyLine()
        for px, py in ((x, y), (x + w, y), (x + w, y + hh), (x, y + hh)):
            line.append(PolyLineNode.from_xy(round(px * 1e6), round(py * 1e6)))
        line.closed = True
        poly = PolygonWithHoles()
        poly.outline = line
        z.outline = poly
        zones.append(z)
    board.create_items(zones)


@module_loop
async def test_render_zone_filled(
    ecc83_module_session: KicadSession, tmp_path: Path, record_property: Any
) -> None:
    """Last test of the module: it changes the board."""
    s = ecc83_module_session
    o = (await _model(s)).outline
    async with s.commit("copperchat: test GND pours", doc="pcb"):
        await s.call(lambda h: _gnd_pours(h, (o.x, o.y, o.w, o.h)))
    await s.call(lambda h: api.refill(h, timeout_s=60), timeout=api.refill_call_timeout(60))
    m = await _model(s)
    assert sum(len(z.filled) for z in m.zones) >= 2, "the pours were not filled"
    ctx = edit_ctx(s)
    for view in ("top", "bottom"):
        raw = await _raw_svg(s, view, tmp_path)
        cost = _svg_cost(raw)
        _record(record_property, f"zone_{view}_svg", cost)
        fr.load_kicad_svg(raw, limits=br.BOARD_SVG_LIMITS)  # passes the board limits
        result = await pcb_render(ctx, {"view": view})
        data = result.data
        assert data is not None
        assert _png_side(result.images[0].data) <= br.BOARD_MAX_SIDE_PX
        svg = agent_dir(s.project) / "renders" / data["svg_url"].rsplit("/", 1)[1]
        _record(record_property, f"zone_{view}_raster", _raster_cost(svg))
        _record(record_property, f"zone_{view}_wrapped", {"bytes": svg.stat().st_size})
