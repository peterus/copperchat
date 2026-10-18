"""Sheet pins as wire ends and sch_connect endpoints (gap from E7): a sheet pin joins the
sub-sheet's net through the hierarchical label of its name, so a wire onto it merges the
parent net with that net. Real KiCad; hierarchies made through the tools / kipy API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadHandle, KicadSession
from tests.kicad.edit.connect_support import place, sheet_pin_nets, state
from tests.kicad.edit.test_connect_sheets import VERTICAL, _add_sheet_pin, _parts, _right_pin
from tests.support.edit import call_tool, edit_ctx, place_raw
from tests.support.kicad_cli import cli_netlist, cli_pin_nets

pytestmark = pytest.mark.kicad

SHEET_AT = (50.8, 50.8)  # top-left of the 'Power' sheet symbol on the blank root sheet


async def _power_sheet(s: KicadSession, ctx: Any) -> dict[str, Any]:
    """Sheet 'Power' with sheet pin VIN (left edge) whose hierarchical label inside is
    wired to R201.1; R1 on the root sheet, horizontal, its pins level with VIN. Returns
    the sheet pin and R1's pin facing the sheet."""
    await s.ensure_symbol_libraries()
    out, err = await call_tool(
        ctx, "sch_add_sheet", name="Power", file="power.kicad_sch", at=list(SHEET_AT)
    )
    assert not err, out
    pin, err = await call_tool(
        ctx, "sch_add_sheet_pin", sheet="/Power/", name="VIN", direction="input"
    )
    assert not err, pin
    await place_raw(s, "Device:R", (101.6, 76.2), "R201", sheet="/Power/")
    r201, err = await call_tool(ctx, "sch_get_pin", pin="R201.1")
    assert not err, r201
    (lx, ly), (ix, iy) = pin["label"]["at_mm"], r201["at_mm"]
    inner = [[lx, ly], [lx, iy], [ix, iy]] if lx != ix else [[lx, ly], [ix, iy]]
    res, err = await call_tool(ctx, "sch_add_wire", sheet="/Power/", points=inner)
    assert not err, res
    px, py = pin["pin"]["at_mm"]
    assert px == SHEET_AT[0] and pin["pin"]["side"] == "left"
    await place(s, ("Device:R", "R1", (px - 15.24, py), 90))
    facing, at = await _right_pin(ctx, "R1")
    return {"pin_at": [px, py], "r1": facing, "r1_at": at}


async def _net(s: KicadSession, sheet: str, pin: str) -> str:
    return (await sheet_pin_nets(s))[(sheet, pin)]


async def test_add_wire_onto_a_sheet_pin_joins_the_sub_sheet_net(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _power_sheet(s, ctx)
    out, err = await call_tool(ctx, "sch_add_wire", points=[g["r1_at"], g["pin_at"]])
    assert not err, out
    assert out["connectivity_verified"] is True
    assert await _net(s, "/", g["r1"]) == await _net(s, "/Power/", "R201.1") == "/Power/VIN"
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    cli = cli_pin_nets(cli_netlist(s.settings.kicad_cli, dest, tmp_path))
    r1 = ("R1", g["r1"].split(".")[1])
    assert cli[r1] == cli[("R201", "1")]
    assert cli[("R201", "2")] != cli[("R201", "1")]


async def test_add_wire_onto_a_sheet_pin_in_a_shared_hierarchy(
    hier_session: KicadSession,
) -> None:
    """complex_hierarchy: the hierarchical label HIN on R951.1 (shared ampli_ht file) first,
    then the wire onto the sheet pin HIN of the vertical instance."""
    s = hier_session
    await _parts(s)
    await _add_sheet_pin(s, "HIN", (71.12, 130.81))
    ctx = edit_ctx(s)
    out, err = await call_tool(ctx, "sch_add_label", net="HIN", at=["R951.1"], kind="hierarchical")
    assert not err, out
    pin, at = await _right_pin(ctx, "R950")
    out, err = await call_tool(ctx, "sch_add_wire", points=[at, [71.12, 130.81]])
    assert not err, out
    nets = await sheet_pin_nets(s)
    assert nets[("/", pin)] == nets[(VERTICAL, "R951.1")]


async def test_connect_a_pin_to_a_sheet_pin(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _power_sheet(s, ctx)
    out, err = await call_tool(ctx, "sch_connect", **{"from": g["r1"], "to": "Power.VIN"})
    assert not err, out
    assert out["via_used"] == "wire" and out["connectivity_verified"] is True
    assert out["to"] == "Power.VIN"
    assert out["route"]["points"][-1] == g["pin_at"]
    assert out["net"] == "/Power/VIN"
    assert await _net(s, "/", g["r1"]) == await _net(s, "/Power/", "R201.1")


async def test_connect_to_a_sheet_pin_in_the_shared_hierarchy(hier_session: KicadSession) -> None:
    s = hier_session
    await _parts(s)
    await _add_sheet_pin(s, "HIN", (71.12, 130.81))
    ctx = edit_ctx(s)
    _, err = await call_tool(ctx, "sch_add_label", net="HIN", at=["R951.1"], kind="hierarchical")
    assert not err
    pin, _at = await _right_pin(ctx, "R950")
    out, err = await call_tool(ctx, "sch_connect", **{"from": pin, "to": "ampli_ht_vertical.HIN"})
    assert not err, out
    assert out["via_used"] == "wire"
    nets = await sheet_pin_nets(s)
    assert nets[("/", pin)] == nets[(VERTICAL, "R951.1")] == out["net"]


async def test_connect_from_a_sheet_pin_to_a_net(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _power_sheet(s, ctx)
    out, err = await call_tool(ctx, "sch_add_label", net="VIN", at=[g["r1"]])
    assert not err, out
    assert await _net(s, "/", g["r1"]) == "/VIN"
    out, err = await call_tool(ctx, "sch_connect", **{"from": "Power.VIN", "to": "VIN"})
    assert not err, out
    assert out["from"] == "Power.VIN" and out["connectivity_verified"] is True
    assert await _net(s, "/", g["r1"]) == await _net(s, "/Power/", "R201.1") == out["net"]


async def test_connect_from_a_sheet_pin_to_a_pin(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _power_sheet(s, ctx)
    out, err = await call_tool(ctx, "sch_connect", **{"from": "Power.VIN", "to": g["r1"]})
    assert not err, out
    assert out["via_used"] == "wire"
    assert out["route"]["points"][0] == g["pin_at"]
    assert await _net(s, "/", g["r1"]) == await _net(s, "/Power/", "R201.1") == out["net"]


async def test_wire_over_a_foreign_sheet_pin_is_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """A wire interior over a sheet pin connects it in KiCad (probed: a wire along the
    sheet edge joins the pin's net): refused before anything is written."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _power_sheet(s, ctx)
    px, py = g["pin_at"]
    before = await state(s)
    for points in (
        [[px, py - 2.54], [px, py + 2.54]],  # along the sheet edge
        [g["r1_at"], [px + 5.08, py]],  # through the pin into the sheet box
    ):
        out, err = await call_tool(ctx, "sch_add_wire", points=points)
        assert err and out["error"]["code"] == "overlap", out
        assert "sheet pin Power.VIN" in out["error"]["why"]
    assert await state(s) == before


async def test_unknown_sheet_pin_is_not_found(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _power_sheet(s, ctx)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_connect", **{"from": g["r1"], "to": "Power.NOPE"})
    assert err and out["error"]["code"] == "not_found", out
    assert out["error"]["details"]["candidates"] == ["VIN"]
    out, err = await call_tool(ctx, "sch_connect", **{"from": "Power.VOUT", "to": g["r1"]})
    assert err and out["error"]["code"] == "not_found", out
    assert await state(s) == before


async def test_symbol_reference_wins_over_a_sheet_name(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """A sheet named like a part ('R1' with a pin '2'): 'R1.2' is the resistor's pin."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power_sheet(s, ctx)
    out, err = await call_tool(ctx, "sch_add_sheet", name="R1", file="r1.kicad_sch", at=[127, 50.8])
    assert not err, out
    out, err = await call_tool(ctx, "sch_add_sheet_pin", sheet="/R1/", name="2", direction="input")
    assert not err, out

    def kinds(h: KicadHandle) -> list[str]:
        from copperchat.kicad_tools.edit.labels import World

        world = World(h)
        return [world.endpoint("R1.2").type, world.endpoint("Power.VIN").type]

    assert await s.call(kinds) == ["passive", "sheet_pin"]


async def _empty_sheet(s: KicadSession, ctx: Any) -> dict[str, Any]:
    """Sheet 'Power' with sheet pin VIN whose sub-sheet net holds no symbol pin (only the
    hierarchical label); R1 on the root sheet, level with VIN."""
    await s.ensure_symbol_libraries()
    out, err = await call_tool(
        ctx, "sch_add_sheet", name="Power", file="power.kicad_sch", at=list(SHEET_AT)
    )
    assert not err, out
    pin, err = await call_tool(
        ctx, "sch_add_sheet_pin", sheet="/Power/", name="VIN", direction="input"
    )
    assert not err, pin
    px, py = pin["pin"]["at_mm"]
    await place(s, ("Device:R", "R1", (px - 15.24, py), 90))
    facing, at = await _right_pin(ctx, "R1")
    return {"pin_at": [px, py], "pin_uuid": pin["pin"]["uuid"], "r1": facing, "r1_at": at}


async def _item_net(s: KicadSession, uid: str) -> str | None:
    from copperchat.kicad_tools.edit.scene import item_nets

    nets = await s.call(item_nets)
    return next((n for (_p, u), n in nets.items() if u == uid), None)


async def test_connect_to_a_sheet_pin_whose_net_has_no_symbol_pin(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Nothing in the pin partition stands in for the sheet pin: the result is verified
    by the sheet pin's own netlist item (intent.joined) and reports a net."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _empty_sheet(s, ctx)
    out, err = await call_tool(ctx, "sch_connect", **{"from": "Power.VIN", "to": g["r1"]})
    assert not err, out
    assert out["connectivity_verified"] is True and out["net"] is not None
    assert await _item_net(s, g["pin_uuid"]) == await _net(s, "/", g["r1"])


async def test_add_wire_onto_a_sheet_pin_whose_net_has_no_symbol_pin(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _empty_sheet(s, ctx)
    out, err = await call_tool(ctx, "sch_add_wire", points=[g["r1_at"], g["pin_at"]])
    assert not err, out
    assert await _item_net(s, g["pin_uuid"]) == await _net(s, "/", g["r1"])


async def test_wire_extending_a_wire_onto_a_sheet_pin(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """The new segment is collinear with an existing wire and KiCad merges it at push:
    the joined check leaves the vanished segment out instead of failing."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _empty_sheet(s, ctx)
    mid = [g["r1_at"][0] + 5.08, g["r1_at"][1]]
    out, err = await call_tool(ctx, "sch_add_wire", points=[g["r1_at"], mid])
    assert not err, out
    out, err = await call_tool(ctx, "sch_add_wire", points=[mid, g["pin_at"]])
    assert not err, out
    assert await _item_net(s, g["pin_uuid"]) == await _net(s, "/", g["r1"])


async def test_label_connection_to_a_sheet_pin_whose_net_has_no_symbol_pin(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _empty_sheet(s, ctx)
    out, err = await call_tool(
        ctx, "sch_connect", **{"from": "Power.VIN", "to": g["r1"], "via": "label"}
    )
    assert not err, out
    assert out["via_used"] == "label" and out["net"] is not None
    assert await _item_net(s, g["pin_uuid"]) == await _net(s, "/", g["r1"])


async def test_connect_wires_a_pin_to_the_hierarchical_label_of_a_sheet_pin(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """E10 (golden scenario s3): inside the sub-sheet, sch_connect(pin, '<pin name>') draws
    a wire onto the still unwired hierarchical label sch_add_sheet_pin made, instead of a
    second label that leaves the hierarchical label dangling (ERC error)."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    out, err = await call_tool(
        ctx, "sch_add_sheet", name="Sensor", file="sensor.kicad_sch", at=[177.8, 88.9]
    )
    assert not err, out
    pin, err = await call_tool(
        ctx, "sch_add_sheet_pin", sheet="Sensor", name="SDA", direction="bidirectional"
    )
    assert not err, pin
    lx, ly = pin["label"]["at_mm"]
    r, err = await call_tool(
        ctx, "sch_place_symbol", lib_id="Device:R", at=[lx + 15.24, ly], rotation=90,
        sheet="/Sensor/",
    )  # fmt: skip
    assert not err, r
    ref = r["placed"]["ref"]
    near = min(r["placed"]["pins"], key=lambda p: abs(p["at_mm"][0] - lx))["pin"]
    out, err = await call_tool(ctx, "sch_connect", **{"from": f"{ref}.{near}", "to": "SDA"})
    assert not err, out
    assert out["via_used"] == "wire", out
    assert [c["kind"] for c in out["created"]] == ["wire"]
    erc, err = await call_tool(ctx, "run_erc")
    assert not err, erc
    assert not [v for v in erc["violations"] if v["type"] == "label_dangling"], erc
