"""Sheet pins as sch_move / sch_delete targets (v0.5.0 #1, spec §5): by UUID or
'<sheet>.<pin>', snapped onto an edge of their sheet symbol, side changes derive the
orientation, deleting takes the child's unconnected hierarchical label along. Real KiCad;
hierarchies made through the tools / kipy API."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.scene import resolve_sheet, sheet_api
from tests.kicad.edit.connect_support import place, state
from tests.kicad.edit.helpers import local_label, wire
from tests.kicad.edit.test_sheets import REFERENCE, _sheet_pin_lines
from tests.support.edit import call_tool, edit_ctx, place_raw

pytestmark = pytest.mark.kicad

SHEET_AT = (50.8, 50.8)  # 'Power' sheet symbol, 50.8 x 38.1 mm: right edge 101.6, bottom 88.9
FIRST_Y = 53.34  # the first free pin row on an edge (one pitch below the top)


async def _power(s: KicadSession, ctx: Any, *pins: tuple[str, str]) -> list[dict[str, Any]]:
    """Sheet 'Power' on the blank root with the given (name, side) pins."""
    out, err = await call_tool(
        ctx, "sch_add_sheet", name="Power", file="power.kicad_sch", at=list(SHEET_AT)
    )
    assert not err, out
    added = []
    for name, side in pins:
        res, err = await call_tool(
            ctx, "sch_add_sheet_pin", sheet="/Power/", name=name, direction="input", side=side
        )
        assert not err, res
        added.append(res)
    return added


async def _commit(s: KicadSession, sheet: str, *items: Any) -> None:
    def run(h: KicadHandle) -> None:
        sheet_api(h, resolve_sheet(h, sheet)).create_items(list(items))

    async with s.commit("fixture"):
        await s.call(run)


async def _pins(s: KicadSession, sheet: str = "/") -> dict[str, dict[str, Any]]:
    """Sheet pins on `sheet` by name: position, side, spin style, uuid."""

    def run(h: KicadHandle) -> dict[str, dict[str, Any]]:
        out = {}
        for sym in sheet_api(h, resolve_sheet(h, sheet)).get_sheet_symbols():
            for p in sym.pins:
                out[f"{sym.name_field.text.value}.{p.text.value}"] = {
                    "uuid": access.kiid(p),
                    "at": (p.position.x / 1e6, p.position.y / 1e6),
                    "side": int(p.side),
                    "spin": int(p.spin_style),
                }
        return out

    return await s.call(run)


async def _hier_labels(s: KicadSession, sheet: str) -> dict[str, tuple[float, float]]:
    def run(h: KicadHandle) -> dict[str, tuple[float, float]]:
        return {
            access.kiid(lb): (lb.position.x / 1e6, lb.position.y / 1e6)
            for lb in sheet_api(h, resolve_sheet(h, sheet)).get_labels()
            if type(lb).__name__ == "HierarchicalLabel"
        }

    return await s.call(run)


async def _items(s: KicadSession, sheet: str = "/") -> dict[str, Any]:
    def run(h: KicadHandle) -> dict[str, Any]:
        api = sheet_api(h, resolve_sheet(h, sheet))
        lines = {access.kiid(w): (w.start.x / 1e6, w.start.y / 1e6, w.end.x / 1e6, w.end.y / 1e6)
                 for w in api.get_lines()}  # fmt: skip
        labels = {access.kiid(lb): (lb.position.x / 1e6, lb.position.y / 1e6)
                  for lb in api.get_labels()}  # fmt: skip
        return {"wires": lines, "labels": labels}

    return await s.call(run)


# -- addressing and sch_add_sheet_pin ------------------------------------------------------


async def test_add_sheet_pin_returns_pin_and_label_uuids(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    (out,) = await _power(s, edit_ctx(s, edit_db), ("VIN", "left"))
    pins = await _pins(s)
    assert out["pin"]["uuid"] == pins["Power.VIN"]["uuid"]
    labels = await _hier_labels(s, "/Power/")
    assert out["label"]["uuid"] in labels


async def test_duplicate_pin_name_still_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power(s, ctx, ("VIN", "left"))
    out, err = await call_tool(ctx, "sch_add_sheet_pin", sheet="/Power/", name="VIN",
                               direction="input", side="right")  # fmt: skip
    assert err and out["error"]["code"] == "invalid_argument", out
    assert "already" in out["error"]["why"]


# -- sch_move ------------------------------------------------------------------------------


async def test_move_sheet_pin_by_uuid_along_edge(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Along the same edge the pin's own stub (a wire with a label at its free end) travels
    with it, shape kept."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    (added,) = await _power(s, ctx, ("VIN", "left"))
    x, y = added["pin"]["at_mm"]
    assert (x, y) == (SHEET_AT[0], FIRST_Y)
    stub, label = wire((x, y), (x - 7.62, y)), local_label("VIN_EXT", (x - 7.62, y))
    await _commit(s, "/", stub, label)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=[added["pin"]["uuid"]], by=[0, 5.08])
    assert not err, out
    assert out["connectivity_verified"] is True and out["logical_changes"] == 1
    assert out["moved"][0]["kind"] == "sheet_pin" and out["moved"][0]["side"] == "left"
    assert out.get("carried_wires") == 1, out
    pins = await _pins(s)
    assert pins["Power.VIN"]["at"] == (x, y + 5.08)
    items = await _items(s)
    assert items["wires"][access.kiid(stub)] == (x, y + 5.08, x - 7.62, y + 5.08)
    assert items["labels"][access.kiid(label)] == (x - 7.62, y + 5.08)
    assert (await state(s))[1] == before[1]  # partition unchanged


async def test_move_sheet_pin_drags_a_wire_to_another_part(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    (added,) = await _power(s, ctx, ("VIN", "left"))
    x, y = added["pin"]["at_mm"]
    await place(s, ("Device:R", "R1", (x - 15.24, y), 90))
    facing = []
    for p in ("R1.1", "R1.2"):
        data, err = await call_tool(ctx, "sch_get_pin", pin=p)
        assert not err, data
        if data["direction"] == "right":
            facing.append(p)
    (r1,) = facing
    out, err = await call_tool(ctx, "sch_connect", **{"from": r1, "to": "Power.VIN"})
    assert not err, out
    before = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=["Power.VIN"], by=[0, 7.62])
    assert not err, out
    assert out["dragged_wires"] >= 1 and out["connectivity_verified"] is True
    assert (await state(s))[1] == before[1]


@pytest.mark.parametrize("pin_end", ["from", "to"])
async def test_connect_sheet_pin_by_uuid(
    blank_session: KicadSession, edit_db: Database, pin_end: str
) -> None:
    """Spec 5.1: a sheet pin's UUID works as a sch_connect endpoint, on either end."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    (added,) = await _power(s, ctx, ("VIN", "left"))
    x, y = added["pin"]["at_mm"]
    await place(s, ("Device:R", "R1", (x - 15.24, y), 90))
    facing = []
    for p in ("R1.1", "R1.2"):
        data, err = await call_tool(ctx, "sch_get_pin", pin=p)
        assert not err, data
        if data["direction"] == "right":
            facing.append(p)
    (r1,) = facing
    uid = added["pin"]["uuid"]
    ends = {"from": uid, "to": r1} if pin_end == "from" else {"from": r1, "to": uid}
    out, err = await call_tool(ctx, "sch_connect", **ends)
    assert not err, out
    assert out["connectivity_verified"] is True
    data, err = await call_tool(ctx, "sch_get_pin", pin=r1)
    assert not err, data
    assert data["net"] is not None and not data["free"]


async def test_move_two_sheet_pins_one_pitch_together(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """A pin may land where another pin moving away in the same call sat."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power(s, ctx, ("VIN", "left"), ("GND", "left"))
    pins = await _pins(s)
    (vx, vy), (gx, gy) = pins["Power.VIN"]["at"], pins["Power.GND"]["at"]
    assert vx == gx and gy > vy
    d = round(gy - vy, 4)
    out, err = await call_tool(ctx, "sch_move", items=["Power.VIN", "Power.GND"], by=[0, d])
    assert not err, out
    pins = await _pins(s)
    assert pins["Power.VIN"]["at"] == pytest.approx((vx, gy))
    assert pins["Power.GND"]["at"] == pytest.approx((gx, gy + d))


async def test_move_top_pin_and_part_on_its_stub_by_a_diagonal(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """The pin snaps back onto the top edge (offset (2.54, 0)) while R1 at its stub's end
    moves by (2.54, 2.54): the stub is dragged, not carried, and R1 stays connected."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    await _power(s, ctx, ("VIN", "left"))
    out, err = await call_tool(ctx, "sch_move", items=["Power.VIN"], to=[76.2, 49.53])
    assert not err, out
    x, y = (await _pins(s))["Power.VIN"]["at"]
    assert (x, y) == (76.2, SHEET_AT[1])
    await place(s, ("Device:R", "R1", (x, y - 7.62 - 3.81), 0))
    bottom = None
    for p in ("R1.1", "R1.2"):
        data, err = await call_tool(ctx, "sch_get_pin", pin=p)
        assert not err, data
        if data["direction"] == "down":
            bottom = data["at_mm"]
    assert bottom == [x, y - 7.62], bottom
    await _commit(s, "/", wire((x, y), (x, y - 7.62)))
    before = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=["R1", "Power.VIN"], by=[2.54, 2.54])
    assert not err, out
    assert out["connectivity_verified"] is True, out
    assert (await _pins(s))["Power.VIN"]["at"] == pytest.approx((x + 2.54, y))
    assert (await state(s))[1] == before[1]  # R1 still on the sheet pin's net


async def test_move_two_wired_sheet_pins_one_pitch_together(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """VIN lands on the end of GND's stub, which GND carries away in the same call."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power(s, ctx, ("VIN", "left"), ("GND", "left"))
    pins = await _pins(s)
    (x, vy), (_, gy) = pins["Power.VIN"]["at"], pins["Power.GND"]["at"]
    items = []
    for name, y in (("VIN", vy), ("GND", gy)):
        items += [wire((x, y), (x - 7.62, y)), local_label(f"{name}_EXT", (x - 7.62, y))]
    await _commit(s, "/", *items)
    before = await state(s)
    d = round(gy - vy, 4)
    out, err = await call_tool(ctx, "sch_move", items=["Power.VIN", "Power.GND"], by=[0, d])
    assert not err, out
    assert out["connectivity_verified"] is True and out.get("carried_wires") == 2, out
    pins = await _pins(s)
    assert pins["Power.VIN"]["at"] == pytest.approx((x, gy))
    assert (await state(s))[1] == before[1]  # partition unchanged


async def test_move_wired_sheet_pin_to_other_edge_refused_when_wire_cannot_follow(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """A side change drags the pin's wire (no rigid carry). Dragged onto the top edge, the
    wire would cross the sheet box and run along its edge: refused, nothing written."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    (added,) = await _power(s, ctx, ("VIN", "left"))
    x, y = added["pin"]["at_mm"]
    await _commit(s, "/", wire((x, y), (x - 7.62, y)), local_label("VIN_EXT", (x - 7.62, y)))
    before = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=["Power.VIN"], to=[x + 7.62, SHEET_AT[1]])
    assert err and out["error"]["code"] == "overlap", out
    assert "dragging the wire" in out["error"]["what"]
    assert any("sheet Power" in p for p in out["error"]["details"]["problems"]), out
    assert await state(s) == before
    assert (await _pins(s))["Power.VIN"]["at"] == (x, y)


async def test_move_sheet_pin_by_name_to_other_edge_changes_side(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Landing near the right edge moves the pin onto it; its text runs into the box like
    a pin KiCad itself placed on that side (reference file written by eeschema). The
    child's hierarchical label stays where it is."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    (added,) = await _power(s, ctx, ("VIN", "left"))
    labels = await _hier_labels(s, "/Power/")
    out, err = await call_tool(ctx, "sch_move", items=["Power.VIN"], to=[102.87, 63.5])
    assert not err, out
    moved = out["moved"][0]
    assert moved["side"] == "right" and moved["to_mm"] == [101.6, 63.5], moved
    pins = await _pins(s)
    assert pins["Power.VIN"]["at"] == (101.6, 63.5)
    assert pins["Power.VIN"]["uuid"] == added["pin"]["uuid"]  # the same pin, moved
    assert await _hier_labels(s, "/Power/") == labels
    await s.save("sch")
    root = s.project.sch_file
    assert root is not None
    ours = _sheet_pin_lines(root.read_text())
    assert ours == {"right": _sheet_pin_lines(REFERENCE.read_text())["right"]}


async def test_move_sheet_pin_to_top_edge(blank_session: KicadSession, edit_db: Database) -> None:
    """Top edge: side TOP, spin style BOTTOM (what KiCad's SCH_SHEET_PIN::SetSide gives),
    written with angle 90."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLabelSpinStyle as Spin
    from kipy.proto.schematic.schematic_types_pb2 import SheetSide

    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power(s, ctx, ("VIN", "left"))
    out, err = await call_tool(ctx, "sch_move", items=["Power.VIN"], to=[76.2, 49.53])
    assert not err, out
    assert out["moved"][0]["side"] == "top"
    pin = (await _pins(s))["Power.VIN"]
    assert pin["at"] == (76.2, 50.8)
    assert (pin["side"], pin["spin"]) == (SheetSide.SHS_TOP, Spin.SLSS_BOTTOM)
    await s.save("sch")
    root = s.project.sch_file
    assert root is not None
    assert '(pin "VIN" input\n\t\t\t(at 76.2 50.8 90)' in root.read_text()


async def test_move_sheet_pin_keeps_clear_of_corners(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power(s, ctx, ("VIN", "left"))
    out, err = await call_tool(ctx, "sch_move", items=["Power.VIN"], to=[50.8, 88.9])
    assert not err, out
    assert out["moved"][0]["to_mm"] == [50.8, 86.36], out  # one pitch above the corner


async def test_move_sheet_pin_far_from_edge_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power(s, ctx, ("VIN", "left"))
    before = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=["Power.VIN"], to=[76.2, 69.85])
    assert err and out["error"]["code"] == "invalid_argument", out
    edges = out["error"]["details"]["edges"]
    assert edges["left"] == {"x": 50.8, "y": [53.34, 86.36]}, edges
    assert set(edges) == {"left", "right", "top", "bottom"}
    assert await state(s) == before


async def test_move_sheet_pin_onto_other_pin_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power(s, ctx, ("VIN", "left"), ("GND", "left"))
    pins = await _pins(s)
    gx, gy = pins["Power.GND"]["at"]
    before = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=["Power.VIN"], to=[gx - 1.27, gy + 1.27])
    assert err and out["error"]["code"] == "overlap", out
    assert "GND" in out["error"]["why"]
    assert await state(s) == before


async def test_rotate_sheet_pin_refused(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power(s, ctx, ("VIN", "left"))
    out, err = await call_tool(ctx, "sch_move", items=["Power.VIN"], rotation=90)
    assert err and out["error"]["code"] == "invalid_argument", out
    assert "side defines" in out["error"]["why"], out


async def test_move_sheet_and_its_pin_together_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power(s, ctx, ("VIN", "left"))
    out, err = await call_tool(ctx, "sch_move", items=["sheet:Power", "Power.VIN"], by=[0, 2.54])
    assert err and out["error"]["code"] == "invalid_argument", out


async def test_unknown_sheet_pin_not_found(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power(s, ctx, ("VIN", "left"))
    out, err = await call_tool(ctx, "sch_move", items=["Power.NOPE"], by=[0, 2.54])
    assert err and out["error"]["code"] == "not_found", out
    assert "VIN" in out["error"]["why"]


# -- sch_delete ----------------------------------------------------------------------------


async def test_delete_sheet_pin_removes_unconnected_child_label(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    (added,) = await _power(s, ctx, ("VIN", "left"))
    out, err = await call_tool(ctx, "sch_delete", items=["Power.VIN"])
    assert not err, out
    assert out["connectivity_verified"] is True
    assert "Power.VIN" not in await _pins(s)
    assert added["label"]["uuid"] not in await _hier_labels(s, "/Power/")
    assert out["deleted_summary"]["sheet_pins"] == [{"sheet": "Power", "pin": "VIN"}]
    assert out["deleted_summary"]["child_labels"] == 1


async def test_delete_sheet_pin_costs_one(blank_session: KicadSession, edit_db: Database) -> None:
    """The pin, its owned stub with a label at the far end and the child label: 1."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    (added,) = await _power(s, ctx, ("VIN", "left"))
    x, y = added["pin"]["at_mm"]
    stub, label = wire((x, y), (x - 7.62, y)), local_label("VIN_EXT", (x - 7.62, y))
    await _commit(s, "/", stub, label)
    out, err = await call_tool(ctx, "sch_delete", items=[added["pin"]["uuid"]])
    assert not err, out
    assert out["logical_changes"] == 1
    items = await _items(s)
    assert access.kiid(stub) not in items["wires"]
    assert access.kiid(label) not in items["labels"]


async def test_delete_sheet_pin_keeps_connected_child_label_with_note(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    (added,) = await _power(s, ctx, ("VIN", "left"))
    await place_raw(s, "Device:R", (101.6, 76.2), "R201", sheet="/Power/")
    r201, err = await call_tool(ctx, "sch_get_pin", pin="R201.1")
    assert not err, r201
    (lx, ly), (ix, iy) = added["label"]["at_mm"], r201["at_mm"]
    inner = [[lx, ly], [lx, iy], [ix, iy]] if lx != ix else [[lx, ly], [ix, iy]]
    res, err = await call_tool(ctx, "sch_add_wire", sheet="/Power/", points=inner)
    assert not err, res
    out, err = await call_tool(ctx, "sch_delete", items=["Power.VIN"])
    assert not err, out
    assert added["label"]["uuid"] in await _hier_labels(s, "/Power/")
    assert any("VIN" in n and "connected" in n for n in out["notes"]), out["notes"]
    assert out["deleted_summary"]["child_labels"] == 0


async def test_delete_sheet_pin_shared_child_keeps_label(hier_session: KicadSession) -> None:
    """Review Focus 3: complex_hierarchy uses ampli_ht.kicad_sch for two sheet symbols.
    Deleting HIN of one keeps the shared file's label (the other still has HIN, a note
    says so); deleting the other's HIN then removes it."""
    s = hier_session
    ctx = edit_ctx(s)
    first, err = await call_tool(ctx, "sch_add_sheet_pin", sheet="/ampli_ht_vertical/",
                                 name="HIN", direction="input")  # fmt: skip
    assert not err, first
    second, err = await call_tool(ctx, "sch_add_sheet_pin", sheet="/ampli_ht_horizontal/",
                                  name="HIN", direction="input")  # fmt: skip
    assert not err, second
    label = first["label"]["uuid"]
    assert second["label"]["uuid"] == label  # reused
    out, err = await call_tool(edit_ctx(s), "sch_delete", items=["ampli_ht_vertical.HIN"])
    assert not err, out
    assert label in await _hier_labels(s, "/ampli_ht_vertical/")
    assert any("ampli_ht_horizontal" in n and "HIN" in n for n in out["notes"]), out["notes"]
    out, err = await call_tool(edit_ctx(s), "sch_delete", items=["ampli_ht_horizontal.HIN"])
    assert not err, out
    assert label not in await _hier_labels(s, "/ampli_ht_vertical/")
    assert out["deleted_summary"]["child_labels"] == 1


async def test_delete_both_shared_pins_in_one_call_removes_label(
    hier_session: KicadSession,
) -> None:
    s = hier_session
    ctx = edit_ctx(s)
    for sheet in ("/ampli_ht_vertical/", "/ampli_ht_horizontal/"):
        out, err = await call_tool(ctx, "sch_add_sheet_pin", sheet=sheet, name="HIN",
                                   direction="input")  # fmt: skip
        assert not err, out
    label = out["label"]["uuid"]
    out, err = await call_tool(
        edit_ctx(s), "sch_delete", items=["ampli_ht_vertical.HIN", "ampli_ht_horizontal.HIN"]
    )
    assert not err, out
    assert out["logical_changes"] == 2
    assert out["deleted_summary"]["child_labels"] == 1  # one label of the shared file
    assert label not in await _hier_labels(s, "/ampli_ht_vertical/")


async def test_delete_sheet_pin_with_attached_false_keeps_label(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    (added,) = await _power(s, ctx, ("VIN", "left"))
    out, err = await call_tool(ctx, "sch_delete", items=["Power.VIN"], with_attached=False)
    assert not err, out
    assert "Power.VIN" not in await _pins(s)
    assert added["label"]["uuid"] in await _hier_labels(s, "/Power/")


async def test_delete_sheet_with_its_own_pin_costs_one(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """The pin goes with its sheet symbol: no extra logical change."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power(s, ctx, ("VIN", "left"))
    out, err = await call_tool(ctx, "sch_delete", items=["sheet:Power", "Power.VIN"])
    assert not err, out
    assert out["logical_changes"] == 1
    assert "Power.VIN" not in await _pins(s)


async def test_delete_bus_sheet_pin_keeps_the_child_bus_label(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Buses are not followed: a bus label in the sub-sheet stays, with a note."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    (added,) = await _power(s, ctx, ("D[0..7]", "left"))
    out, err = await call_tool(ctx, "sch_delete", items=["Power.D[0..7]"])
    assert not err, out
    assert added["label"]["uuid"] in await _hier_labels(s, "/Power/")
    assert any("bus" in n for n in out["notes"]), out["notes"]


@pytest.mark.parametrize("pin_end", ["from", "to"])
async def test_connect_unknown_sheet_pin_uuid_not_found(
    blank_session: KicadSession, edit_db: Database, pin_end: str
) -> None:
    """A UUID that is no sheet pin is refused, never placed as a net label."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    await place(s, ("Device:R", "R1", (101.6, 101.6), 0))
    before = await state(s)
    uid = "0f0f0f0f-0000-4000-8000-000000000000"
    ends = {"from": uid, "to": "R1.1"} if pin_end == "from" else {"from": "R1.1", "to": uid}
    out, err = await call_tool(ctx, "sch_connect", **ends)
    assert err and out["error"]["code"] == "not_found", out
    assert "sch_list_items" in out["error"]["suggestion"]
    assert await state(s) == before
