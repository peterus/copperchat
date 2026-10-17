import json
import re
from pathlib import Path
from typing import Any

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools import pcb
from copperchat.tools.spec import ToolContext

pytestmark = pytest.mark.kicad
GOLDEN = Path(__file__).resolve().parent.parent / "fixtures" / "golden"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def ctx(s: KicadSession) -> ToolContext:
    return ToolContext(project=s.project, session=s, settings=s.settings)


def golden(name: str) -> Any:
    return json.loads((GOLDEN / f"{name}.json").read_text())


async def test_list_and_get_footprint(ecc83_session: KicadSession) -> None:
    lst = await pcb.pcb_list_footprints(ctx(ecc83_session), {"filter": "R*"})
    assert lst.data == golden("pcb_list_footprints_R")
    fp = await pcb.pcb_get_footprint(ctx(ecc83_session), {"ref": "U1"})
    assert fp.data == golden("pcb_get_footprint_U1")
    assert fp.data is not None and all(UUID.match(p["uuid"]) for p in fp.data["pads"])


async def test_get_net_has_pads_tracks_vias(ecc83_session: KicadSession) -> None:
    net = await pcb.pcb_get_net(ctx(ecc83_session), {"name": "GND"})
    assert net.data == golden("pcb_get_net_GND")
    assert net.data is not None and net.data["pads"]


async def test_unknown_net(ecc83_session: KicadSession) -> None:
    with pytest.raises(StructuredToolError) as info:
        await pcb.pcb_get_net(ctx(ecc83_session), {"name": "GNDD"})
    assert "GND" in info.value.suggestion


async def test_board_snapshot_shape(ecc83_session: KicadSession) -> None:
    snap = await ecc83_session.call(pcb.board_snapshot)
    assert {"footprints", "tracks", "vias", "outline", "unconnected", "nets"} <= set(snap)
    assert all(f["bbox_mm"] and f["bbox_mm"]["w"] > 0 for f in snap["footprints"])
    assert {t["layer"] for t in snap["tracks"]} <= {"F.Cu", "B.Cu"}
