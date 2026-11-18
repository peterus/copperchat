"""Footprint render pipeline on a real api-server and kicad-cli (F2 Task 5, spec §7.5).
Task 6 adds the `fp_render` tool tests."""

from __future__ import annotations

import io
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

from copperchat.kicad_session import KicadSession
from copperchat.kicad_session.footprint_doc import SEED_LIB_ID
from copperchat.kicad_tools.footprints.model import geometry_hash
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
