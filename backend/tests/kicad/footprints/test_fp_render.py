"""Footprint render pipeline and the `fp_render` tool on a real api-server and kicad-cli
(F2 Tasks 5 and 6, spec §7.5, §16.2)."""

from __future__ import annotations

import io
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

from copperchat.kicad_session import KicadSession
from copperchat.kicad_session.footprint_doc import SEED_LIB_ID
from copperchat.kicad_tools.footprints.model import geometry_hash
from copperchat.kicad_tools.footprints.read import fp_get
from copperchat.kicad_tools.footprints.render import fp_render
from copperchat.library.service import library_for
from copperchat.tools.spec import ToolContext
from copperchat.views.footprint_render import render_footprint

pytestmark = pytest.mark.kicad


async def test_render_seed(blank_nopcb_session: KicadSession) -> None:
    session = blank_nopcb_session
    ctx = ToolContext(project=session.project, session=session, settings=session.settings)
    out = await render_footprint(ctx, SEED_LIB_ID, None, None, True)
    with Image.open(io.BytesIO(out.png.read_bytes())) as img:
        assert img.format == "PNG"
        assert img.size == (
            round(out.extent_mm[0] * out.scale_px_per_mm),
            round(out.extent_mm[1] * out.scale_px_per_mm),
        )
        assert max(img.size) <= 2000 and out.scale_px_per_mm >= 40
    root = ET.fromstring(out.svg.read_bytes())
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert out.png.parent == session.project.dir / ".kicad-agent" / "renders"
    geometry = await library_for(session).footprint_geometry(SEED_LIB_ID)
    assert geometry is not None and out.geometry_hash == geometry_hash(geometry)
    keep = os.environ.get("COPPERCHAT_KEEP_RENDER")
    if keep:  # manual inspection
        Path(keep).mkdir(parents=True, exist_ok=True)
        (Path(keep) / "seed.png").write_bytes(out.png.read_bytes())
        (Path(keep) / "seed.svg").write_bytes(out.svg.read_bytes())


SVG = "{http://www.w3.org/2000/svg}"


def _ctx(session: KicadSession) -> ToolContext:
    return ToolContext(project=session.project, session=session, settings=session.settings)


async def test_fp_render_tool_seed(blank_nopcb_session: KicadSession) -> None:
    """The tool: PNG ImageBlock that decodes, filenames only, the sidecar, and the
    geometry_hash fp_get reports."""
    ctx = _ctx(blank_nopcb_session)
    result = await fp_render(ctx, {"lib_id": SEED_LIB_ID})
    data = result.data
    assert data is not None
    renders = blank_nopcb_session.project.dir / ".kicad-agent" / "renders"
    (img,) = result.images
    assert img.mime == "image/png" and img.path == renders / data["image"]
    with Image.open(io.BytesIO(img.data)) as png:
        assert png.format == "PNG" and max(png.size) <= 2000
    assert "/" not in data["image"] and "/" not in data["svg"]
    assert (renders / data["svg"]).is_file()
    sidecar = json.loads((renders / f"{data['svg']}.json").read_text())
    assert sidecar == {"download_name": "Inductor_SMD_L_Bourns-SRN4018.svg"}
    got = await fp_get(ctx, {"lib_id": SEED_LIB_ID})
    assert got.data is not None and data["geometry_hash"] == got.data["geometry_hash"]
    assert data["scale_px_per_mm"] >= 40


# a path command letter, or a number (exponent notation included)
_PATH_TOKEN = re.compile(r"([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)|([A-Za-z])")


async def test_crtyd_extent_matches_geometry(blank_nopcb_session: KicadSession) -> None:
    """Only F.CrtYd: the extent of KiCad's drawing (its path centrelines) is the
    courtyard bbox fp_get reports (spike Q8: "CrtYd 4.0 x 4.0 ... exact").

    KiCad's `viewBox` is NOT asserted: kicad-cli sizes the page to every item of the
    footprint incl. the texts on layers it does not draw (probed: 15.01 x 7.90 mm page
    for this 5.06 x 4.5 mm courtyard with `--layers F.CrtYd`), so it only has to
    contain the drawing; full exports are not asserted at all (spec §16.2)."""
    ctx = _ctx(blank_nopcb_session)
    result = await fp_render(ctx, {"lib_id": SEED_LIB_ID, "layers": ["F.CrtYd"]})
    assert result.data is not None
    renders = blank_nopcb_session.project.dir / ".kicad-agent" / "renders"
    root = ET.fromstring((renders / result.data["svg"]).read_bytes())
    (nested,) = root.findall(f"{SVG}svg")
    xs: list[float] = []
    ys: list[float] = []
    commands: set[str] = set()
    for path in nested.iter(f"{SVG}path"):
        nums: list[float] = []
        for num, cmd in _PATH_TOKEN.findall(path.get("d", "")):
            if cmd:
                commands.add(cmd.upper())
            else:
                nums.append(float(num))
        xs += nums[0::2]
        ys += nums[1::2]
    # the pairing above assumes absolute move/line pairs only: fail loudly otherwise
    assert commands <= {"M", "L", "Z"}, commands
    assert xs and ys
    got = await fp_get(ctx, {"lib_id": SEED_LIB_ID})
    assert got.data is not None
    x0, y0, x1, y1 = got.data["graphics"]["F.CrtYd"]["bbox"]
    assert [min(xs), min(ys), max(xs), max(ys)] == pytest.approx([x0, y0, x1, y1], abs=0.01)
    assert max(xs) - min(xs) == pytest.approx(x1 - x0, abs=0.01)
    assert max(ys) - min(ys) == pytest.approx(y1 - y0, abs=0.01)
    vx, vy, vw, vh = (float(v) for v in nested.get("viewBox", "").split())
    assert vx <= x0 and vy <= y0 and vx + vw >= x1 and vy + vh >= y1


def test_cairosvg_in_image() -> None:
    """CairoSVG >= 2.7.0 (CVE-2023-27586) with a working libcairo2 in the image."""
    import cairosvg.surface  # type: ignore[import-untyped]

    version = tuple(int(p) for p in cairosvg.__version__.split(".")[:2])
    assert version >= (2, 7), cairosvg.__version__
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="1mm" height="1mm" viewBox="0 0 1 1">'
        b'<rect width="1" height="1" fill="#000"/></svg>'
    )
    png = cairosvg.surface.PNGSurface.convert(bytestring=svg, url_fetcher=_refuse, unsafe=False)
    with Image.open(io.BytesIO(png)) as img:
        assert img.format == "PNG" and img.size[0] >= 1 and img.size[1] >= 1


def _refuse(url: str, resource_type: str) -> bytes:
    raise ValueError(f"external resource refused: {url}")
