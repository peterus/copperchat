"""sch_add_label, sch_add_power, sch_no_connect (E5 Task 3, Review Focus 1 and 4)."""

from __future__ import annotations

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.edit.geometry import on_grid, same_point
from copperchat.library.service import library_for
from tests.kicad.edit.connect_support import Counter, pin_nets, place, scene, state
from tests.support.edit import call_tool, edit_ctx

pytestmark = pytest.mark.kicad

R = "Device:R"
LDO = "Regulator_Linear:AMS1117-3.3"
SLSS_UP = 2


async def test_label_stub_per_pin_all_on_net(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await place(
        s,
        (R, "R1", (101.6, 101.6), 0),
        (R, "R2", (116.84, 101.6), 0),
        (R, "R3", (132.08, 101.6), 0),
    )
    ctx = edit_ctx(s, edit_db)
    out, err = await call_tool(ctx, "sch_add_label", net="DATA", at=["R1.1", "R2.1", "R3.1"])
    assert not err, out
    assert out["logical_changes"] == 3
    kinds = sorted(c["kind"] for c in out["created"])
    assert kinds == ["label"] * 3 + ["wire"] * 3
    sc = await scene(s)
    labels = [lb for lb in sc.labels.values() if lb.text == "DATA"]
    assert len(labels) == 3
    assert all(lb.spin == SLSS_UP for lb in labels)  # pins point up: text runs upward
    assert all(on_grid(lb.at) for lb in labels)
    nets = await pin_nets(s)
    assert nets["R1.1"] == nets["R2.1"] == nets["R3.1"] == "/DATA"


async def test_multi_pin_is_all_or_nothing(blank_session: KicadSession) -> None:
    s = blank_session
    await place(
        s,
        (R, "R1", (101.6, 101.6), 0),
        (R, "R2", (116.84, 101.6), 0),
        (R, "R3", (132.08, 101.6), 0),
        (R, "R9", (132.08, 95.25), 90),  # lies right on top of R3.1: no room for a stub
    )
    snapshots = Counter()
    ctx = edit_ctx(s, on_mutation=snapshots)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_add_label", net="DATA", at=["R1.1", "R2.1", "R3.1"])
    assert err
    assert out["error"]["code"] == "overlap"
    assert "R3.1" in out["error"]["what"]
    assert await state(s) == before
    assert snapshots.calls == 0
    out, err = await call_tool(ctx, "sch_add_power", net="GND", at=["R1.1", "R3.1"])
    assert err and "R3.1" in out["error"]["what"]
    assert await state(s) == before


async def test_rail_name_label_refused(blank_session: KicadSession) -> None:
    s = blank_session
    await place(s, (R, "R1", (101.6, 101.6), 0))
    assert await library_for(s).wait_ready(120)
    ctx = edit_ctx(s)
    before = await state(s)
    for net in ("GND", "+5V", "VBUS"):
        out, err = await call_tool(ctx, "sch_add_label", net=net, at=["R1.1"])
        assert err, net
        assert out["error"]["code"] == "invalid_argument"
        assert "sch_add_power" in out["error"]["suggestion"]
    assert await state(s) == before


async def test_power_symbol_upright_and_numbered(blank_session: KicadSession) -> None:
    s = blank_session
    await place(s, (R, "R1", (101.6, 101.6), 0), (R, "R2", (116.84, 101.6), 0))
    ctx = edit_ctx(s)
    out, err = await call_tool(ctx, "sch_add_power", net="+3V3", at=["R1.1", "R2.1"])
    assert not err, out
    assert out["logical_changes"] == 2
    assert [p["reference"] for p in out["placed"]] == ["#PWR01", "#PWR02"]
    assert [p["rotation"] for p in out["placed"]] == [0, 0]
    out, err = await call_tool(ctx, "sch_add_power", net="GND", at=["R1.2"])
    assert not err, out
    assert out["placed"][0]["reference"] == "#PWR03"
    sc = await scene(s)
    syms = {x.ref: x for x in sc.symbols.values()}
    for ref, lib in (("#PWR01", "power:+3V3"), ("#PWR02", "power:+3V3"), ("#PWR03", "power:GND")):
        assert syms[ref].lib_id == lib and syms[ref].rotation == 0
        (pin,) = syms[ref].pins
        # supplies point up (pin below the body), ground down (pin above the body)
        assert pin.direction == ("down" if "3V3" in lib else "up")
    nets = await pin_nets(s)
    assert nets["R1.1"] == nets["R2.1"] == "+3V3"
    assert nets["R1.2"] == "GND"


async def test_power_from_free_stub_end_when_pin_wired(blank_session: KicadSession) -> None:
    s = blank_session
    await place(s, (R, "R1", (101.6, 101.6), 0))
    ctx = edit_ctx(s)
    # R1.1 (101.6, 97.79) gets a wire up and to the right: the pin's outward way is used.
    _, err = await call_tool(
        ctx, "sch_add_wire", points=[[101.6, 97.79], [101.6, 92.71], [106.68, 92.71]]
    )
    assert not err
    out, err = await call_tool(ctx, "sch_add_power", net="+5V", at=["R1.1"])
    assert not err, out
    (placed,) = out["placed"]
    stub = placed["stub_mm"]
    assert not same_point(stub[0], (101.6, 97.79))
    assert any("wire end" in n for n in out["notes"])
    nets = await pin_nets(s)
    assert nets["R1.1"] == "+5V"


async def test_pwr_flag(blank_session: KicadSession) -> None:
    s = blank_session
    await place(s, (LDO, "U1", (106.68, 101.6), 0))
    ctx = edit_ctx(s)
    _, err = await call_tool(ctx, "sch_add_power", net="+5V", at=["U1.VI"])
    assert not err

    async def not_driven() -> int:
        erc, e = await call_tool(ctx, "run_erc")
        assert not e, erc
        return sum(  # the +5V net (U1.GND is left unconnected on purpose)
            1
            for v in erc["violations"]
            if v["type"] == "power_pin_not_driven"
            and any("#PWR" in i["description"] or "VI" in i["description"] for i in v["items"])
        )

    assert await not_driven() > 0
    out, err = await call_tool(ctx, "sch_add_power", net="PWR_FLAG", at=["U1.VI"])
    assert not err, out
    assert out["placed"][0]["reference"].startswith("#FLG")
    assert await not_driven() == 0


async def test_no_connect_on_pin_end(blank_session: KicadSession) -> None:
    s = blank_session
    await place(s, (R, "R1", (101.6, 101.6), 0))
    ctx = edit_ctx(s)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_no_connect", pins=["R1.1", "R1.2"])
    assert not err, out
    assert out["logical_changes"] == 2
    assert [c["kind"] for c in out["created"]] == ["no_connect"] * 2
    sc = await scene(s)
    assert sorted(sc.no_connects.values()) == [(101.6, 97.79), (101.6, 105.41)]
    assert (await state(s))[1] == before[1]


async def test_no_connect_on_connected_pin_refused(blank_session: KicadSession) -> None:
    s = blank_session
    await place(s, (R, "R1", (101.6, 101.6), 0), (R, "R2", (116.84, 101.6), 0))
    ctx = edit_ctx(s)
    _, err = await call_tool(ctx, "sch_connect", **{"from": "R1.1", "to": "R2.1"})
    assert not err
    before = await state(s)
    out, err = await call_tool(ctx, "sch_no_connect", pins=["R2.2", "R1.1"])
    assert err and out["error"]["code"] == "invalid_argument"
    assert "R1.1" in out["error"]["what"]
    assert await state(s) == before


async def test_label_at_position_must_touch_wire(blank_session: KicadSession) -> None:
    s = blank_session
    await place(s, (R, "R1", (101.6, 101.6), 0))
    snapshots = Counter()
    ctx = edit_ctx(s, on_mutation=snapshots)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_add_label", net="SIG", at=[120.65, 120.65])
    assert err and out["error"]["code"] == "invalid_argument"
    assert await state(s) == before and snapshots.calls == 0
    _, err = await call_tool(ctx, "sch_add_wire", points=[[101.6, 105.41], [101.6, 113.03]])
    assert not err
    out, err = await call_tool(ctx, "sch_add_label", net="SIG", at=[101.6, 110.49])
    assert not err, out
    nets = await pin_nets(s)
    assert nets["R1.2"] == "/SIG"
