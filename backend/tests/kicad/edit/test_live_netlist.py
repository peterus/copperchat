"""live_netlist (E-C2) against kicad-cli's netlist and unsaved edits (spike 2A, Q8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.access import Netlist, live_netlist
from tests.kicad.edit.helpers import wire
from tests.support.kicad_cli import cli_netlist, cli_pin_nets

pytestmark = pytest.mark.kicad


def _refs(h: KicadHandle) -> dict[tuple[str, str], str]:
    """(sheet path, symbol uuid) -> reference of that instance."""
    return {
        (access.sheet_path_key(s.path), access.kiid(s)): str(s.reference)
        for s in h.schematic().get_symbols()
    }


async def test_live_netlist_matches_cli_on_hierarchy(
    hier_session: KicadSession, tmp_path: Path
) -> None:
    s = hier_session
    netlist: Netlist = await s.call(live_netlist)
    refs = await s.call(_refs)
    root_sch = s.project.sch_file
    assert root_sch is not None
    # The project's own files: the session has not changed them.
    cli = cli_pin_nets(cli_netlist(s.settings.kicad_cli, root_sch, tmp_path))
    live: dict[tuple[str, str], str] = {}
    for name, pins in netlist.nets.items():
        for sheet_path, uuid, number in pins:
            ref = refs[(sheet_path, uuid)]
            if not ref.startswith("#"):  # kicad-cli's netlist leaves power symbols out
                live[(ref, number)] = name
    assert len(cli) >= 160
    assert {k: v for k, v in live.items() if live[k] != cli.get(k)} == {}
    assert set(live) == set(cli)
    # Sheet paths are `/`-joined KIID paths starting at the root sheet (2C).
    paths = {key[0] for pins in netlist.nets.values() for key in pins}
    root_uuid = await s.call(
        lambda h: str(next(iter(h.schematic().get_hierarchy())).path.proto.path[0].value)
    )
    assert f"/{root_uuid}" in paths
    assert all(p.startswith(f"/{root_uuid}") for p in paths)
    assert any(p.count("/") == 2 for p in paths)  # a sub-sheet instance


async def test_live_netlist_sees_unsaved_wire(ecc83_session: KicadSession) -> None:
    s = ecc83_session

    def pins(
        h: KicadHandle,
    ) -> dict[tuple[str, str], tuple[tuple[str, str, str], dict[str, float]]]:
        out = {}
        for sym in h.schematic().get_symbols():
            key = access.sheet_path_key(sym.path), access.kiid(sym)
            for p in access.symbol_pins(sym):
                out[(str(sym.reference), p["pin"])] = ((*key, p["pin"]), p["position_mm"])
        return out

    by_pin = await s.call(pins)
    before: Netlist = await s.call(live_netlist)
    (a, a_at), (b, b_at) = by_pin[("R1", "1")], by_pin[("R2", "1")]
    assert before.net_of(a) != before.net_of(b)
    async with s.commit("wire"):
        w = wire((a_at["x"], a_at["y"]), (b_at["x"], b_at["y"]))
        await s.call(lambda h: h.schematic().create_items([w]))
    after: Netlist = await s.call(live_netlist)
    assert after.net_of(a) is not None and after.net_of(a) == after.net_of(b)
    assert any(a in group and b in group for group in after.partition())
    assert await s.call(lambda h: h.schematic().is_document_modified()) is True  # unsaved


async def test_sch_get_net_matches_cli_nodes(hier_session: KicadSession, tmp_path: Path) -> None:
    """sch_get_net on the live netlist keeps the shape of kicad-cli's netlist nodes."""
    from copperchat.kicad_tools import schematic
    from copperchat.tools.spec import ToolContext

    s = hier_session
    ctx = ToolContext(project=s.project, session=s, settings=s.settings)
    assert s.project.sch_file is not None
    root = cli_netlist(s.settings.kicad_cli, s.project.sch_file, tmp_path)
    checked = 0
    for net in root.iterfind("./nets/net"):
        expected = sorted(
            (n.get("ref"), n.get("pin"), n.get("pinfunction", ""),
             n.get("pintype", "").removesuffix("+no_connect"))
            for n in net.iterfind("node")
        )  # fmt: skip
        got = await schematic.sch_get_net(ctx, {"name": net.get("name", "")})
        assert got.data is not None
        pins = got.data["pins"]
        assert sorted((p["ref"], p["pin"], p["function"], p["type"]) for p in pins) == expected
        checked += len(pins)
    assert checked >= 160
