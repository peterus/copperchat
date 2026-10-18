"""sch_connect through the real tool adapter (E5 Task 2, Review Focus 1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.edit.geometry import on_grid, same_point
from copperchat.library.service import library_for
from tests.kicad.edit.connect_support import Counter, pin_nets, place, scene, state
from tests.support.edit import call_tool, edit_ctx
from tests.support.kicad_cli import cli_netlist, cli_pin_nets

pytestmark = pytest.mark.kicad

R = "Device:R"
LDO = "Regulator_Linear:AMS1117-3.3"
CONN = "Connector:Conn_01x02_Pin"


def _points(out: dict[str, Any]) -> list[list[float]]:
    return [p for c in out["created"] for p in c.get("points_mm", [c.get("at_mm")]) if p]


async def _pin(ctx: Any, spec: str) -> dict[str, Any]:
    data, err = await call_tool(ctx, "sch_get_pin", pin=spec)
    assert not err, data
    return data


async def test_wire_between_two_pins(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    await place(s, (R, "R1", (101.6, 101.6), 0), (R, "R2", (111.76, 101.6), 0))
    ctx = edit_ctx(s, edit_db)
    a, b = await _pin(ctx, "R1.2"), await _pin(ctx, "R2.2")
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R1.2", "to": "R2.2"})
    assert not err, out
    assert out["via_used"] == "wire"
    assert out["connectivity_verified"] is True
    assert out["logical_changes"] == 1 and out["budget"] == {"used": 1, "limit": 20}
    pts = out["route"]["points"]
    assert pts[0] == a["at_mm"] and pts[-1] == b["at_mm"]
    assert len(pts) - 1 <= 3 and all(on_grid(p) for p in pts)
    nets = await pin_nets(s)
    assert nets["R1.2"] == nets["R2.2"] == out["net"]
    assert nets["R1.1"] != nets["R1.2"]
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    cli = cli_pin_nets(cli_netlist(s.settings.kicad_cli, dest, tmp_path))
    assert cli[("R1", "2")] == cli[("R2", "2")]
    assert cli[("R1", "1")] != cli[("R2", "1")]


async def test_t_join_to_existing_net(blank_session: KicadSession) -> None:
    s = blank_session
    await place(
        s,
        (CONN, "J1", (76.2, 101.6), 0),
        (LDO, "U1", (106.68, 101.6), 0),
        (R, "R1", (91.44, 114.3), 0),
    )
    ctx = edit_ctx(s)
    first, err = await call_tool(ctx, "sch_connect", **{"from": "J1.1", "to": "U1.VI"})
    assert not err, first
    assert first["route"]["points"] == [[81.28, 101.6], [99.06, 101.6]]
    top = "R1.1"  # pin 1 of Device:R is the upper one
    pin = await _pin(ctx, top)
    assert pin["direction"] == "up"
    out, err = await call_tool(ctx, "sch_connect", **{"from": top, "to": "U1.VI"})
    assert not err, out
    assert out["via_used"] == "wire"
    assert out["route"]["junction"] == [91.44, 101.6]
    assert out["route"]["points"][-1] == [91.44, 101.6]
    sc = await scene(s)
    assert any(same_point(j, (91.44, 101.6)) for j in sc.junctions.values())
    nets = await pin_nets(s)
    assert nets["J1.1"] == nets["U1.3"] == nets[top]


async def test_to_net_name_routes_when_close(blank_session: KicadSession) -> None:
    s = blank_session
    await place(
        s,
        (CONN, "J1", (76.2, 101.6), 0),
        (LDO, "U1", (106.68, 101.6), 0),
        (R, "R1", (91.44, 114.3), 0),
    )
    ctx = edit_ctx(s)
    _, err = await call_tool(ctx, "sch_connect", **{"from": "J1.1", "to": "U1.VI"})
    assert not err
    lab, err = await call_tool(ctx, "sch_add_label", net="VIN", at=[86.36, 101.6])
    assert not err, lab
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R1.1", "to": "VIN"})
    assert not err, out
    assert out["via_used"] == "wire"
    assert out["net"] == "/VIN"
    nets = await pin_nets(s)
    assert nets["R1.1"] == nets["J1.1"] == "/VIN"


async def test_to_power_net_places_power_symbol(blank_session: KicadSession) -> None:
    s = blank_session
    await place(s, (R, "R1", (101.6, 101.6), 0))
    ctx = edit_ctx(s)
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R1.2", "to": "GND"})
    assert not err, out
    assert out["via_used"] == "power"
    assert [p["rotation"] for p in out["placed"]] == [0]
    sc = await scene(s)
    assert not any(lb.text == "GND" for lb in sc.labels.values())
    gnd = [x for x in sc.symbols.values() if x.lib_id == "power:GND"]
    assert len(gnd) == 1 and gnd[0].ref.startswith("#PWR")
    nets = await pin_nets(s)
    assert nets["R1.2"] == "GND"


async def _caged(s: KicadSession) -> None:
    """R1.1 points up, R2.2 points down below it; foreign pin ends right beside both pin
    ends block every sideways exit, so no route of <= 3 segments exists."""
    await place(
        s,
        (R, "R1", (101.6, 101.6), 0),
        (R, "R2", (101.6, 121.92), 0),
        (R, "R3", (96.52, 97.79), 90),
        (R, "R4", (106.68, 97.79), 90),
        (R, "R5", (96.52, 125.73), 90),
        (R, "R6", (106.68, 125.73), 90),
    )


async def test_auto_falls_back_to_labels(blank_session: KicadSession) -> None:
    s = blank_session
    await _caged(s)
    ctx = edit_ctx(s)
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R1.1", "to": "R2.2"})
    assert not err, out
    assert out["via_used"] == "label"
    assert out["route_blocked_by"]
    nets = await pin_nets(s)
    assert nets["R1.1"] == nets["R2.2"] == "/N_R1_1"
    args = {"from": "R3.1", "to": "R5.1", "via": "label", "net_name": "SIG"}
    out, err = await call_tool(ctx, "sch_connect", **args)
    assert not err, out
    nets = await pin_nets(s)
    assert nets["R3.1"] == nets["R5.1"] == "/SIG"


async def test_wire_only_fails_with_suggestion(blank_session: KicadSession) -> None:
    s = blank_session
    await _caged(s)
    snapshots = Counter()
    ctx = edit_ctx(s, on_mutation=snapshots)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R1.1", "to": "R2.2", "via": "wire"})
    assert err
    assert out["error"]["code"] == "no_route"
    assert "via='label'" in out["error"]["suggestion"] and "sch_move" in out["error"]["suggestion"]
    assert await state(s) == before
    assert snapshots.calls == 0


async def test_merging_two_named_nets_refused(blank_session: KicadSession) -> None:
    s = blank_session
    await place(s, (R, "R1", (101.6, 101.6), 0), (R, "R2", (116.84, 101.6), 0))
    ctx = edit_ctx(s)
    for net, pin in (("SDA", "R1.1"), ("SCL", "R2.1")):
        _, err = await call_tool(ctx, "sch_add_label", net=net, at=[pin])
        assert not err
    before = await state(s)
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R1.1", "to": "R2.1"})
    assert err and out["error"]["code"] == "invalid_argument"
    assert "two named nets" in out["error"]["why"]
    assert await state(s) == before


async def test_off_grid_or_near_miss_impossible(blank_session: KicadSession) -> None:
    s = blank_session
    await place(
        s, (R, "R1", (101.6, 101.6), 90), (R, "R2", (121.92, 109.22), 0), (R, "R3", (88.9, 88.9), 0)
    )
    ctx = edit_ctx(s)
    for frm, to in (("R1.2", "R2.1"), ("R3.2", "R1.1")):
        a, b = await _pin(ctx, frm), await _pin(ctx, to)
        out, err = await call_tool(ctx, "sch_connect", **{"from": frm, "to": to})
        assert not err, out
        assert out["via_used"] == "wire"
        assert all(on_grid(p) for p in _points(out))
        route = out["route"]["points"]
        assert route[0] == a["at_mm"] and route[-1] == b["at_mm"]  # exactly on both pins
        after_a, after_b = await _pin(ctx, frm), await _pin(ctx, to)
        assert after_a["net"] == after_b["net"] is not None


async def test_t_join_from_the_target_pin_onto_the_from_net(blank_session: KicadSession) -> None:
    s = blank_session
    await place(
        s,
        (CONN, "J1", (76.2, 101.6), 0),
        (LDO, "U1", (106.68, 101.6), 0),
        (R, "R1", (91.44, 114.3), 0),
    )
    ctx = edit_ctx(s)
    _, err = await call_tool(ctx, "sch_connect", **{"from": "J1.1", "to": "U1.VI"})
    assert not err
    out, err = await call_tool(ctx, "sch_connect", **{"from": "U1.VI", "to": "R1.1"})
    assert not err, out
    assert out["route"]["junction"] == [91.44, 101.6]
    nets = await pin_nets(s)
    assert nets["J1.1"] == nets["U1.3"] == nets["R1.1"]


async def test_label_fallback_joins_an_already_named_net(blank_session: KicadSession) -> None:
    s = blank_session
    await _caged(s)
    ctx = edit_ctx(s)
    _, err = await call_tool(ctx, "sch_add_label", net="SDA", at=["R1.1"])
    assert not err
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R1.1", "to": "R2.2"})
    assert not err, out
    assert out["via_used"] == "label" and out["net"] == "/SDA"
    assert out["logical_changes"] == 1
    nets = await pin_nets(s)
    assert nets["R1.1"] == nets["R2.2"] == "/SDA"


async def test_unknown_pin_in_to_is_not_a_net_name(blank_session: KicadSession) -> None:
    s = blank_session
    await place(s, (R, "R1", (101.6, 101.6), 0))
    ctx = edit_ctx(s)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R1.1", "to": "R9.1"})
    assert err and out["error"]["code"] == "not_found"
    assert await state(s) == before


async def test_dotted_library_rail_gets_a_power_symbol(blank_session: KicadSession) -> None:
    s = blank_session
    await place(s, (R, "R1", (101.6, 101.6), 0))
    assert await library_for(s).wait_ready(120)
    assert library_for(s).has_symbol("power:+3.3VA")
    ctx = edit_ctx(s)
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R1.1", "to": "+3.3VA"})
    assert not err, out
    assert out["via_used"] == "power"
    nets = await pin_nets(s)
    assert nets["R1.1"] == "+3.3VA"
