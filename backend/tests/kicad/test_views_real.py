import io

import pytest
from PIL import Image

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession
from copperchat.views import Region, render_view, write_view_snapshots
from copperchat.views.snapshot import resolve_view_file, snapshot_path, write_kicanvas_snapshot

pytestmark = pytest.mark.kicad


def size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "PNG"
        return img.size


async def test_snapshot_written_without_version_bump(ecc83_session: KicadSession) -> None:
    before = ecc83_session.view_version
    path = await write_kicanvas_snapshot(ecc83_session, "pcb")
    assert path == snapshot_path(ecc83_session.project, "pcb")
    assert path.is_file() and path.stat().st_size > 1000
    assert ".kicad-agent/view" in path.as_posix()
    assert ecc83_session.view_version == before  # X2: only the turn-end publish bumps


async def test_hierarchy_view_files(hier_session: KicadSession) -> None:
    files = await write_view_snapshots(hier_session)
    assert files == {
        "sch": ["complex_hierarchy.kicad_sch", "ampli_ht.kicad_sch"],
        "pcb": ["complex_hierarchy.kicad_pcb"],
    }
    for name in files["sch"] + files["pcb"]:
        assert resolve_view_file(hier_session.project, name) is not None
    # The sub-sheet is a byte copy of the project's own file (spike 1C, Q4).
    project = hier_session.project
    sub = resolve_view_file(project, "ampli_ht.kicad_sch")
    assert sub is not None and sub.read_bytes() == (project.dir / "ampli_ht.kicad_sch").read_bytes()


async def test_agent_board_render_written(ecc83_session: KicadSession) -> None:
    img = await render_view(ecc83_session, "pcb", "agent", theme="agent")
    assert img.mime == "image/png" and img.path is not None and img.path.is_file()
    assert img.path.parent.name == "renders" and len(img.path.stem) == 32
    assert img.path.read_bytes() == img.data
    assert size(img.data)[0] > 100


async def test_agent_region_around_ref(ecc83_session: KicadSession) -> None:
    full = await render_view(ecc83_session, "pcb", "agent")
    crop = await render_view(ecc83_session, "pcb", "agent", region=Region(around_ref="U1"))
    assert size(crop.data) != size(full.data)


async def test_kicad_mode_pcb_cropped_to_board(ecc83_session: KicadSession) -> None:
    img = await render_view(ecc83_session, "pcb", "kicad", theme="light")
    w, h = size(img.data)
    # ecc83-pp's board is 52.2 x 46.5 mm; + 3 mm margins -> aspect ~1.11.
    assert 1.0 < w / h < 1.25
    region = await render_view(
        ecc83_session, "pcb", "kicad", region=Region(around_ref="U1"), layers=["F.Cu", "Edge.Cuts"]
    )
    assert size(region.data) != (w, h)


async def test_schematic_modes(hier_session: KicadSession) -> None:
    page = await render_view(hier_session, "sch", "kicad")
    agent = await render_view(hier_session, "sch", "agent")
    assert size(page.data) == size(agent.data)  # same page, grid drawn on top
    assert page.data != agent.data
    sub = await render_view(hier_session, "sch", "agent", sheet="ampli_ht_horizontal")
    assert sub.data != agent.data
    around = await render_view(hier_session, "sch", "agent", region=Region(around_ref="U301"))
    assert size(around.data) != size(agent.data)


async def test_render_errors(ecc83_session: KicadSession) -> None:
    with pytest.raises(StructuredToolError) as info:
        await render_view(ecc83_session, "pcb", "agent", layers=["F.Cuu"])
    assert "F.Cu" in info.value.suggestion
    with pytest.raises(StructuredToolError):
        await render_view(ecc83_session, "sch", "agent", layers=["F.Cu"])
    with pytest.raises(StructuredToolError):
        await render_view(ecc83_session, "sch", "kicad", sheet="/nope/")
    with pytest.raises(StructuredToolError):
        await render_view(ecc83_session, "pcb", "kicad", region=Region(around_ref="U99"))
