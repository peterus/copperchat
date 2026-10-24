"""sch_list_items and sch_get_pin.stub_end / sheet pins in connected (#3, spec §10).
Real KiCad; projects built through the tools / kipy API."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadSession
from tests.kicad.edit.connect_support import place
from tests.kicad.edit.helpers import local_label
from tests.support.edit import call_tool, edit_ctx

pytestmark = pytest.mark.kicad

R = "Device:R"
SHEET_AT = (50.8, 50.8)


async def _board(s: KicadSession, ctx: Any) -> None:
    """R1 (vertical) with label DATA on pin 1 and GND on pin 2; R2 with a no-connect on
    pin 1."""
    await place(s, (R, "R1", (101.6, 101.6), 0), (R, "R2", (127.0, 101.6), 0))
    for tool, args in (
        ("sch_add_label", {"net": "DATA", "at": ["R1.1"]}),
        ("sch_add_power", {"net": "GND", "at": ["R1.2"]}),
        ("sch_no_connect", {"pins": ["R2.1"]}),
    ):
        out, err = await call_tool(ctx, tool, **args)
        assert not err, out


async def _items(ctx: Any, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, "sch_list_items", **args)
    assert not err, out
    return out


async def _power_sheet(s: KicadSession, ctx: Any) -> dict[str, Any]:
    """Sheet 'Power' with sheet pin VIN on its left edge; R1 horizontal with its right pin
    exactly on the sheet pin."""
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
    await place(s, (R, "R1", (px - 3.81, py), 90))
    for n in ("1", "2"):
        data, err = await call_tool(ctx, "sch_get_pin", pin=f"R1.{n}")
        assert not err, data
        if data["at_mm"] == [px, py]:
            return {"pin": data, "at": [px, py]}
    raise AssertionError("no R1 pin on the sheet pin")


async def test_list_items_types_filter(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _board(s, ctx)
    labels = (await _items(ctx, types=["label"]))["items"]
    assert [(i["type"], i["text"], i["net"], i["sheet"]) for i in labels] == [
        ("label", "DATA", "/DATA", "/")
    ]
    assert len(labels[0]["uuid"]) == 36 and len(labels[0]["position_mm"]) == 2
    power = (await _items(ctx, types=["power"]))["items"]
    assert [(i["value"], i["text"], i["net"]) for i in power] == [("GND", "GND", "GND")]
    assert power[0]["ref"].startswith("#PWR")
    syms = (await _items(ctx, types=["symbol"]))["items"]
    assert [(i["ref"], i["value"], i["unit"]) for i in syms] == [("R1", "R", 1), ("R2", "R", 1)]
    assert all("net" not in i for i in syms)
    wires = (await _items(ctx, types=["wire"]))["items"]
    assert {i["net"] for i in wires} == {"/DATA", "GND"}
    assert all("start_mm" in i and "end_mm" in i and "position_mm" not in i for i in wires)
    ncs = (await _items(ctx, types=["no_connect"]))["items"]
    assert len(ncs) == 1 and ncs[0]["net"] is None
    both = (await _items(ctx, types=["label", "no_connect"]))["items"]
    assert [i["type"] for i in both] == ["label", "no_connect"]
    out, err = await call_tool(ctx, "sch_list_items", types=["label", "bogus"])
    assert err and out["error"]["code"] == "invalid_argument"
    assert "hier_label" in out["error"]["why"] + out["error"]["suggestion"]


async def test_list_items_bbox_and_net(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _board(s, ctx)
    data = await _items(ctx, net="/DATA")
    assert {i["type"] for i in data["items"]} == {"label", "wire", "symbol"}
    assert [i["ref"] for i in data["items"] if i["type"] == "symbol"] == ["R1"]
    # the box around R2 only: R2 and its no-connect, nothing of R1
    box = await _items(ctx, bbox=[132.0, 90.0, 122.0, 110.0])
    assert sorted(i["type"] for i in box["items"]) == ["no_connect", "symbol"]
    # a box touching only the DATA stub wire's middle: the wire, not its label
    pin, err = await call_tool(ctx, "sch_get_pin", pin="R1.1")
    assert not err, pin
    (wire,) = pin["stub_wires"]
    x, _y = pin["at_mm"]
    lb = next(i for i in data["items"] if i["type"] == "label")
    mid = (pin["at_mm"][1] + lb["position_mm"][1]) / 2
    hit = await _items(ctx, bbox=[x - 0.5, mid - 0.1, x + 0.5, mid + 0.1])
    assert [i["uuid"] for i in hit["items"]] == [wire]
    both = await _items(ctx, net="GND", bbox=[90.0, 90.0, 140.0, 140.0], types=["wire", "power"])
    assert {i["type"] for i in both["items"]} == {"wire", "power"}
    assert await _items(ctx, net="NOPE") == {**data, "items": [], "total": 0}


async def test_list_items_text_glob(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _board(s, ctx)
    assert [i["text"] for i in (await _items(ctx, text="dat"))["items"]] == ["DATA"]
    refs = [i["ref"] for i in (await _items(ctx, text="r?"))["items"]]
    assert refs == ["R1", "R2"]
    assert [i["type"] for i in (await _items(ctx, text="#pwr*"))["items"]] == ["power"]
    assert [i["type"] for i in (await _items(ctx, text="gnd"))["items"]] == ["power"]
    assert (await _items(ctx, text="zz*"))["total"] == 0


async def test_list_items_unconnected_label_net_null(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    async with s.commit("setup"):
        await s.call(lambda h: h.schematic().create_items([local_label("FLOAT", (152.4, 152.4))]))
    items = (await _items(ctx, types=["label"]))["items"]
    assert [(i["text"], i["net"]) for i in items] == [("FLOAT", None)]


async def test_list_items_pagination_limit_500(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _board(s, ctx)
    full = await _items(ctx)
    assert full["limit"] == 100 and full["offset"] == 0 and full["sheet"] == "/"
    assert full["total"] == len(full["items"]) > 4
    order = ["label", "wire", "no_connect", "power", "symbol"]
    assert [i["type"] for i in full["items"]] == sorted(
        (i["type"] for i in full["items"]), key=order.index
    )
    pages: list[str] = []
    for offset in range(0, full["total"], 2):
        page = await _items(ctx, limit=2, offset=offset)
        assert page["total"] == full["total"] and page["offset"] == offset
        pages += [i["uuid"] for i in page["items"]]
    assert pages == [i["uuid"] for i in full["items"]]
    assert (await _items(ctx, offset=full["total"]))["items"] == []
    assert len((await _items(ctx, limit=500))["items"]) == full["total"]
    for bad in ({"limit": 501}, {"limit": 0}, {"offset": -1}, {"limit": 2.5}):
        out, err = await call_tool(ctx, "sch_list_items", **bad)
        assert err and out["error"]["code"] == "invalid_argument", (bad, out)


async def test_get_pin_stub_end_shows_label_at_far_end(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _board(s, ctx)
    pin, err = await call_tool(ctx, "sch_get_pin", pin="R1.1")
    assert not err, pin
    label = (await _items(ctx, types=["label"]))["items"][0]
    wires = (await _items(ctx, types=["wire"], net="/DATA"))["items"]
    assert pin["stub_wires"] == [w["uuid"] for w in wires]
    end = {"uuid": label["uuid"], "type": "label", "text": "DATA"}
    assert pin["stub_end"] == [{**end, "position_mm": label["position_mm"]}]
    gnd, err = await call_tool(ctx, "sch_get_pin", pin="R1.2")
    assert not err, gnd
    power = (await _items(ctx, types=["power"]))["items"][0]
    assert [(e["uuid"], e["type"], e["text"]) for e in gnd["stub_end"]] == [
        (power["uuid"], "power", "GND")
    ]
    free, err = await call_tool(ctx, "sch_get_pin", pin="R2.2")
    assert not err, free
    assert free["stub_end"] == [] and free["stub_wires"] == []


async def test_get_pin_connected_includes_sheet_pin(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _power_sheet(s, ctx)
    sheet_pins = [i for i in g["pin"]["connected"] if i["kind"] == "sheet_pin"]
    assert [(i["sheet"], i["pin"]) for i in sheet_pins] == [("Power", "VIN")]
    assert not g["pin"]["free"]
    listed = (await _items(ctx, types=["sheet_pin"]))["items"]
    assert [i["uuid"] for i in listed] == [sheet_pins[0]["uuid"]]


async def test_list_items_sheet_pins_and_sheets(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    g = await _power_sheet(s, ctx)
    (sp,) = (await _items(ctx, types=["sheet_pin"]))["items"]
    assert sp["address"] == "Power.VIN" and sp["side"] == "left"
    assert sp["sheet_name"] == "Power" and sp["text"] == "VIN"
    assert sp["position_mm"] == g["at"] and sp["net"] is not None
    assert sp["net"] == g["pin"]["net"]
    (sheet,) = (await _items(ctx, types=["sheet"]))["items"]
    assert sp["sheet_uuid"] == sheet["uuid"]
    assert (sheet["name"], sheet["file"], sheet["path"]) == ("Power", "power.kicad_sch", "/Power/")
    assert sheet["position_mm"] == list(SHEET_AT) and len(sheet["size_mm"]) == 2
    assert "net" not in sheet
    assert [i["type"] for i in (await _items(ctx, text="power.v*"))["items"]] == ["sheet_pin"]
    assert {i["type"] for i in (await _items(ctx, net=sp["net"]))["items"]} == {
        "sheet_pin",
        "symbol",
        "sheet",
    }
    inner = await _items(ctx, sheet="/Power/")
    assert inner["sheet"] == "/Power/"
    assert [(i["type"], i["text"], i["sheet"]) for i in inner["items"]] == [
        ("hier_label", "VIN", "/Power/")
    ]
