import json
import re
from pathlib import Path
from typing import Any

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access, project, schematic
from copperchat.tools.spec import ToolContext

pytestmark = pytest.mark.kicad
GOLDEN = Path(__file__).resolve().parent.parent / "fixtures" / "golden"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def ctx(session: KicadSession) -> ToolContext:
    return ToolContext(project=session.project, session=session, settings=session.settings)


def golden(name: str) -> Any:
    return json.loads((GOLDEN / f"{name}.json").read_text())


async def test_project_info(ecc83_session: KicadSession) -> None:
    r = await project.project_info(ctx(ecc83_session), {})
    assert r.data is not None and r.data["kicadVersion"]
    assert {k: v for k, v in r.data.items() if k != "kicadVersion"} == golden("project_info")


async def test_list_symbols_filter_and_uuids(ecc83_session: KicadSession) -> None:
    r = await schematic.sch_list_symbols(ctx(ecc83_session), {"filter": "R*"})
    assert r.data is not None
    assert r.data == golden("sch_list_symbols_R")
    assert all(UUID.match(s["uuid"]) for s in r.data["symbols"])
    assert all(s["ref"].startswith("R") for s in r.data["symbols"])


async def test_get_symbol_and_net(ecc83_session: KicadSession) -> None:
    sym = await schematic.sch_get_symbol(ctx(ecc83_session), {"ref": "U1"})
    assert sym.data == golden("sch_get_symbol_U1")
    net = await schematic.sch_get_net(ctx(ecc83_session), {"name": "GND"})
    assert net.data == golden("sch_get_net_GND")


async def test_unknown_ref_and_net_suggest(ecc83_session: KicadSession) -> None:
    with pytest.raises(StructuredToolError) as info:
        await schematic.sch_get_symbol(ctx(ecc83_session), {"ref": "U9"})
    assert "U1" in info.value.suggestion
    with pytest.raises(StructuredToolError) as info:
        await schematic.sch_get_net(ctx(ecc83_session), {"name": "GNDD"})
    assert "GND" in info.value.suggestion


async def test_hierarchy_sheets_and_instances(hier_session: KicadSession) -> None:
    info = await project.project_info(ctx(hier_session), {})
    assert info.data is not None
    assert [s["file"] for s in info.data["sheets"]] == [
        "complex_hierarchy.kicad_sch",
        "ampli_ht.kicad_sch",
        "ampli_ht.kicad_sch",
    ]
    # The same sub-sheet symbol in two sheet instances gets two references.
    syms = await schematic.sch_list_symbols(ctx(hier_session), {"filter": "U?01"})
    assert syms.data is not None
    assert {(s["ref"], s["sheet"]) for s in syms.data["symbols"]} >= {
        ("U201", "/ampli_ht_vertical/"),
        ("U301", "/ampli_ht_horizontal/"),
    }


def _pin_hit_ratio(h: KicadHandle) -> float:
    """Share of computed pin positions that touch a wire end, label or another pin."""
    sch = h.schematic()
    anchors = set()
    for line in sch.get_lines():
        anchors |= {(line.start.x, line.start.y), (line.end.x, line.end.y)}
    for item in [*sch.get_labels(), *sch.get_no_connects()]:
        anchors.add((item.position.x, item.position.y))
    pins: list[tuple[float, float]] = []
    for s in sch.get_symbols():
        pins += [(p["position_mm"]["x"], p["position_mm"]["y"]) for p in access.symbol_pins(s)]
    anchors_mm = {(round(x / 1e6, 4), round(y / 1e6, 4)) for x, y in anchors}
    hits = sum(1 for p in pins if p in anchors_mm or pins.count(p) > 1)
    return hits / len(pins)


async def test_pin_positions_land_on_connections(
    ecc83_session: KicadSession, hier_session: KicadSession
) -> None:
    # Pin geometry is derived from the symbol transform; a wrong rotation or
    # mirror would put most pins off the wires.
    assert await ecc83_session.call(_pin_hit_ratio) > 0.85
    assert await hier_session.call(_pin_hit_ratio) > 0.95
