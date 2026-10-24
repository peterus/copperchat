"""sch_move through the real tool adapter (E6 Task 1): dragged wires, fields, items on
pins, overlaps and shorts refused before writing, sub-sheets."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.geometry import mm_point, orthogonal, same_point
from copperchat.kicad_tools.edit.labels import _v2
from copperchat.kicad_tools.edit.scene import read_scene, resolve_sheet
from tests.kicad.edit.connect_support import pin_nets, state
from tests.support.edit import call_tool, edit_ctx, free_area, place_direct, place_raw

pytestmark = pytest.mark.kicad

VERTICAL = "/ampli_ht_vertical/"


async def _ok(ctx: Any, tool: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, tool, **args)
    assert not err, __import__("json").dumps(out, indent=1)
    return out


async def _place(ctx: Any, lib_id: str, at: list[float], rotation: int = 0) -> str:
    out = await _ok(ctx, "sch_place_symbol", lib_id=lib_id, at=at, rotation=rotation)
    return str(out["placed"]["ref"])


async def _pin(ctx: Any, pin: str) -> tuple[float, float]:
    at = (await _ok(ctx, "sch_get_pin", pin=pin))["at_mm"]
    return (at[0], at[1])


async def _scene(s: KicadSession, sheet: str | None = None) -> Any:
    return await s.call(lambda h: read_scene(h, resolve_sheet(h, sheet), boxes=False))


async def _symbol(s: KicadSession, ref: str) -> Any:
    def run(h: KicadHandle) -> Any:
        return next(x for x in h.schematic().get_symbols() if str(x.reference) == ref)

    return await s.call(run)


def _wired(sc: Any, at: tuple[float, float]) -> bool:
    return any(same_point(at, w.start) or same_point(at, w.end) for w in sc.wires.values())


def _all_orthogonal(sc: Any) -> bool:
    return all(orthogonal(w.start, w.end) for w in sc.wires.values())


async def _pair(ctx: Any, power: bool = True) -> tuple[str, str]:
    """R1 and R2 horizontal, R1's right pin wired to R2's left pin; GND on R1's left pin."""
    r1 = await _place(ctx, "Device:R", [101.6, 76.2], 90)
    r2 = await _place(ctx, "Device:R", [127.0, 76.2], 90)
    right, left = await _pin(ctx, f"{r1}.1"), await _pin(ctx, f"{r1}.2")
    out_pin, in_pin = (f"{r1}.1", f"{r1}.2") if right[0] > left[0] else (f"{r1}.2", f"{r1}.1")
    r2a, r2b = await _pin(ctx, f"{r2}.1"), await _pin(ctx, f"{r2}.2")
    r2_in = f"{r2}.1" if r2a[0] < r2b[0] else f"{r2}.2"
    await _ok(ctx, "sch_connect", **{"from": out_pin, "to": r2_in})
    if power:
        await _ok(ctx, "sch_add_power", net="GND", at=[in_pin])
    return r1, r2


async def test_move_connected_resistor_keeps_nets(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r1, _r2 = await _pair(ctx)
    _ids, partition = await state(s)
    before = await _symbol(s, r1)
    ref_before = mm_point(before.reference_field.text.position)
    out = await _ok(ctx, "sch_move", items=[r1], by=[0, -10.16])
    assert out["connectivity_verified"] is True and out["logical_changes"] == 1
    assert out["dragged_wires"] >= 1
    assert (await state(s))[1] == partition
    after = await _symbol(s, r1)
    assert mm_point(after.position) == (101.6, 66.04)
    ref_after = mm_point(after.reference_field.text.position)
    assert same_point(ref_after, (ref_before[0], ref_before[1] - 10.16))
    sc = await _scene(s)
    assert _all_orthogonal(sc)
    for n in ("1", "2"):
        assert _wired(sc, await _pin(ctx, f"{r1}.{n}"))


async def test_rotate_drags_wires_orthogonally(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r1, _r2 = await _pair(ctx)
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[r1], rotation=0)
    assert out["connectivity_verified"] is True
    assert (await state(s))[1] == partition
    sc = await _scene(s)
    assert _all_orthogonal(sc)
    for n in ("1", "2"):
        assert _wired(sc, await _pin(ctx, f"{r1}.{n}"))
    sym = await _symbol(s, r1)
    assert int(sym.transform.orientation) == 1  # SSO_0


async def test_mirror_keeps_nets(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    q = await _place(ctx, "Device:Q_NPN", [101.6, 76.2])
    r = await _place(ctx, "Device:R", [76.2, 76.2], 90)
    await _ok(ctx, "sch_add_power", net="+5V", at=[f"{q}.C"])
    await _ok(ctx, "sch_add_power", net="GND", at=[f"{q}.E"])
    await _ok(ctx, "sch_connect", **{"from": f"{q}.B", "to": f"{r}.2"})
    _ids, partition = await state(s)
    nets = await pin_nets(s)
    out = await _ok(ctx, "sch_move", items=[q], mirror="y")
    assert out["connectivity_verified"] is True
    assert "carried_wires" not in out  # a mirror drags, it carries nothing (#7/#8)
    assert (await state(s))[1] == partition
    assert await pin_nets(s) == nets
    sc = await _scene(s)
    assert _all_orthogonal(sc)
    for n in ("B", "C", "E"):
        assert _wired(sc, await _pin(ctx, f"{q}.{n}"))


async def test_label_on_pin_moves_with_symbol(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _place(ctx, "Device:R", [101.6, 76.2])
    pin = await _pin(ctx, f"{r}.1")
    await _ok(ctx, "sch_add_label", net="SDA", at=list(pin))  # exactly on the pin end
    out = await _ok(ctx, "sch_move", items=[r], by=[12.7, 0])
    assert any("label SDA" in n for n in out["notes"] if isinstance(n, str))
    new_pin = await _pin(ctx, f"{r}.1")
    sc = await _scene(s)
    label = next(lb for lb in sc.labels.values() if lb.text == "SDA")
    assert same_point(label.at, new_pin)
    assert (await pin_nets(s))[f"{r}.1"] == "/SDA"


async def test_move_into_overlap_refused(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r1, _r2 = await _pair(ctx)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=[r1], to=[127.0, 76.2])
    assert err and out["error"]["code"] == "overlap", out
    assert out["error"]["details"].get("suggested_at_mm") is not None, out
    assert await state(s) == before


async def test_drag_that_would_short_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r1, _r2 = await _pair(ctx, power=False)
    r3 = await _place(ctx, "Device:R", [88.9, 80.01])  # pin 1 at (88.9, 76.2)
    assert same_point(await _pin(ctx, f"{r3}.1"), (88.9, 76.2))
    before = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=[r1], by=[-25.4, 0])
    assert err, out
    assert out["error"]["code"] == "overlap"
    assert await state(s) == before


async def test_move_on_subsheet(hier_session: KicadSession, edit_db: Database) -> None:
    s = hier_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    x, y = await s.call(lambda h: free_area(h, 30.48, 20.32, VERTICAL))
    await place_raw(s, "Device:R", (x + 5.08, y + 10.16), "R960", sheet=VERTICAL)
    await _ok(ctx, "sch_add_label", net="MOVED", at=["R960.1"])
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=["R960"], by=[10.16, 0])
    assert out["connectivity_verified"] is True
    assert (await state(s))[1] == partition

    def where(h: KicadHandle) -> tuple[float, float]:
        sch = h.schematic().for_sheet(
            next(i for i in h.schematic().get_hierarchy() if str(i.name) == "ampli_ht_vertical")
        )
        sym = next(x for x in sch.get_symbols() if str(x.reference) == "R960")
        return mm_point(sym.position)

    assert same_point(await s.call(where), (x + 15.24, y + 10.16))
    sc = await _scene(s, VERTICAL)
    sym = next(v for v in sc.symbols.values() if v.ref == "R960")
    p1 = next(p for p in sym.pins if p.number == "1")
    assert _wired(sc, p1.at)
    assert _all_orthogonal(sc)
    assert access.kiid(await _symbol(s, "R960")) == sym.uuid


async def test_move_label_drags_its_stub(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _place(ctx, "Device:R", [101.6, 76.2])
    out = await _ok(ctx, "sch_add_label", net="CLK", at=[f"{r}.1"])
    label = next(c for c in out["created"] if c["kind"] == "label")
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[label["uuid"]], by=[5.08, -2.54])
    assert out["logical_changes"] == 1
    assert (await state(s))[1] == partition
    sc = await _scene(s)
    moved = sc.labels[label["uuid"]]
    assert same_point(moved.at, (label["at_mm"][0] + 5.08, label["at_mm"][1] - 2.54))
    assert _wired(sc, moved.at) and _all_orthogonal(sc)
    assert (await pin_nets(s))[f"{r}.1"] == "/CLK"


async def test_move_sheet_drags_sheet_pin_wires(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    await _ok(ctx, "sch_add_sheet", name="Power", file="power.kicad_sch", at=[50.8, 50.8])
    pin = await _ok(ctx, "sch_add_sheet_pin", sheet="/Power/", name="VIN", direction="input")
    await place_raw(s, "Device:R", (101.6, 76.2), "R201", sheet="/Power/")
    await _ok(ctx, "sch_add_label", net="VIN", kind="hierarchical", at=["R201.1"])
    px, py = pin["pin"]["at_mm"]
    r1 = await _place(ctx, "Device:R", [px - 15.24, py], 90)
    a, b = await _pin(ctx, f"{r1}.1"), await _pin(ctx, f"{r1}.2")
    facing = f"{r1}.1" if a[0] > b[0] else f"{r1}.2"
    await _ok(ctx, "sch_connect", **{"from": facing, "to": "Power.VIN"})
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=["sheet:Power"], by=[0, 5.08])
    assert out["connectivity_verified"] is True
    assert (await state(s))[1] == partition
    sc = await _scene(s)
    info = next(i for i in sc.sheet_pins.values() if i.pin.name == "VIN")
    assert same_point(info.pin.at, (px, py + 5.08))
    assert _wired(sc, info.pin.at) and _all_orthogonal(sc)


async def test_pin_to_pin_connection_is_bridged(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """R2's pin sits directly on R1's pin (no wire): moving R1 adds a bridge wire."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    await place_direct(s, "Device:R", (101.6, 76.2), "R1", transform=(90, False, False))
    await place_direct(s, "Device:R", (109.22, 76.2), "R2", transform=(90, False, False))
    nets = await pin_nets(s)
    assert nets["R1.2"] == nets["R2.1"]
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=["R1"], by=[0, -7.62])
    assert out["bridges"] == 1
    assert (await state(s))[1] == partition
    assert _all_orthogonal(await _scene(s))


async def _sheet_with_pin(ctx: Any) -> tuple[float, float]:
    await _ok(ctx, "sch_add_sheet", name="Power", file="power.kicad_sch", at=[50.8, 50.8])
    pin = await _ok(ctx, "sch_add_sheet_pin", sheet="/Power/", name="VIN", direction="input")
    return (pin["pin"]["at_mm"][0], pin["pin"]["at_mm"][1])


async def test_pin_on_a_stationary_sheet_pin_is_bridged(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    px, py = await _sheet_with_pin(ctx)
    # R1 horizontal with its right pin exactly on the sheet pin (no wire)
    await place_direct(s, "Device:R", (px - 3.81, py), "R1", transform=(90, False, False))
    assert same_point(max([await _pin(ctx, "R1.1"), await _pin(ctx, "R1.2")]), (px, py))
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=["R1"], by=[-10.16, 0])
    assert out["bridges"] == 1
    assert (await state(s))[1] == partition


async def test_label_on_a_moved_sheet_pin_travels_with_it(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    px, py = await _sheet_with_pin(ctx)
    await _ok(ctx, "sch_add_label", net="VIN", at=[px, py])
    out = await _ok(ctx, "sch_move", items=["sheet:Power"], by=[0, 5.08])
    assert any("label VIN" in n for n in out["notes"] if isinstance(n, str))
    sc = await _scene(s)
    label = next(lb for lb in sc.labels.values() if lb.text == "VIN" and lb.kind == "label")
    assert same_point(label.at, (px, py + 5.08))


# -- owned stub chains (#7/#8) -----------------------------------------------------------


def _shape(sc: Any) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    return sorted((w.start, w.end) for w in sc.wires.values())


def _shifted(
    segs: list[tuple[tuple[float, float], tuple[float, float]]], d: tuple[float, float]
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    def mv(p: tuple[float, float]) -> tuple[float, float]:
        return (round(p[0] + d[0], 4), round(p[1] + d[1], 4))

    return sorted((mv(a), mv(b)) for a, b in segs)


def _power_at(sc: Any) -> dict[str, tuple[float, float]]:
    return {v.ref: v.at for v in sc.symbols.values() if v.power}


async def _cap_with_rails(ctx: Any) -> str:
    """C401's shape: a vertical Device:C, +3V3 on a stub above, GND on a stub below."""
    c = await _place(ctx, "Device:C", [101.6, 76.2])
    a, b = await _pin(ctx, f"{c}.1"), await _pin(ctx, f"{c}.2")
    top, bottom = (f"{c}.1", f"{c}.2") if a[1] < b[1] else (f"{c}.2", f"{c}.1")
    await _ok(ctx, "sch_add_power", net="+3V3", at=[top])
    await _ok(ctx, "sch_add_power", net="GND", at=[bottom])
    return c


async def test_move_cap_carries_power_stubs(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    c = await _cap_with_rails(ctx)
    sc0 = await _scene(s)
    assert len(sc0.wires) == 2, _shape(sc0)
    _ids, partition = await state(s)
    nets = await pin_nets(s)
    out = await _ok(ctx, "sch_move", items=[c], by=[7.62, 0])
    assert out["connectivity_verified"] is True
    assert (await state(s))[1] == partition
    assert await pin_nets(s) == nets
    sc = await _scene(s)
    d = (7.62, 0.0)
    assert _shape(sc) == _shifted(_shape(sc0), d)  # same wires, same shape, offset
    p0, p1 = _power_at(sc0), _power_at(sc)
    assert set(p0) == set(p1)
    for ref, at in p0.items():
        assert same_point(p1[ref], (at[0] + d[0], at[1] + d[1])), (ref, at, p1[ref])


async def test_move_symbol_stub_to_label_and_noconnect_carried(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """A label at the end of a stub and a no-connect at the end of a two-segment stub."""
    from kipy.schematic_types import NoConnectMarker

    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _place(ctx, "Device:R", [101.6, 76.2])
    a, b = await _pin(ctx, f"{r}.1"), await _pin(ctx, f"{r}.2")
    top, bottom = (f"{r}.1", b) if a[1] < b[1] else (f"{r}.2", a)
    await _ok(ctx, "sch_add_label", net="SDA", at=[top])
    corner, free = (bottom[0], bottom[1] + 5.08), (bottom[0] + 5.08, bottom[1] + 5.08)
    await _ok(ctx, "sch_add_wire", points=[list(bottom), list(corner), list(free)])
    nc = NoConnectMarker()
    nc.position = _v2(free)
    async with s.commit("setup"):
        await s.call(lambda h: h.schematic().create_items([nc]))
    sc0 = await _scene(s)
    assert len(sc0.wires) == 3 and len(sc0.no_connects) == 1, _shape(sc0)
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[r], by=[7.62, -5.08])
    assert out["connectivity_verified"] is True
    assert out["carried_wires"] == 3
    assert (await state(s))[1] == partition
    d = (7.62, -5.08)
    sc = await _scene(s)
    assert _shape(sc) == _shifted(_shape(sc0), d)
    label0 = next(lb for lb in sc0.labels.values() if lb.text == "SDA")
    assert same_point(sc.labels[label0.uuid].at, (label0.at[0] + d[0], label0.at[1] + d[1]))
    (nc_at,) = sc.no_connects.values()
    assert same_point(nc_at, (free[0] + d[0], free[1] + d[1]))


async def test_move_stub_to_other_pin_rubber_bands(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """A wire ending at another part's pin is no stub: dragged as before, R2 untouched."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r1, r2 = await _pair(ctx, power=False)
    r2_pins = [await _pin(ctx, f"{r2}.{n}") for n in ("1", "2")]
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[r1], by=[0, -10.16])
    assert "carried_wires" not in out and out["dragged_wires"] >= 1
    assert (await state(s))[1] == partition
    assert [await _pin(ctx, f"{r2}.{n}") for n in ("1", "2")] == r2_pins
    sc = await _scene(s)
    assert any(_wired(sc, p) for p in r2_pins) and _all_orthogonal(sc)


async def test_move_stub_ending_at_sheet_pin_not_carried(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    px, py = await _sheet_with_pin(ctx)
    r1 = await _place(ctx, "Device:R", [px - 15.24, py], 90)
    a, b = await _pin(ctx, f"{r1}.1"), await _pin(ctx, f"{r1}.2")
    facing = f"{r1}.1" if a[0] > b[0] else f"{r1}.2"
    await _ok(ctx, "sch_connect", **{"from": facing, "to": "Power.VIN"})
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[r1], by=[0, 5.08])
    assert "carried_wires" not in out
    assert (await state(s))[1] == partition
    sc = await _scene(s)
    assert _wired(sc, (px, py)) and _all_orthogonal(sc)


async def test_move_power_symbol_alone_rubber_bands(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    c = await _cap_with_rails(ctx)
    cap_at = mm_point((await _symbol(s, c)).position)
    sc0 = await _scene(s)
    vcc = next(v for v in sc0.symbols.values() if v.power and v.value == "+3V3")
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[vcc.uuid], by=[7.62, 0])
    assert out["connectivity_verified"] is True
    assert (await state(s))[1] == partition
    assert mm_point((await _symbol(s, c)).position) == cap_at
    sc = await _scene(s)
    assert same_point(sc.symbols[vcc.uuid].at, (vcc.at[0] + 7.62, vcc.at[1]))
    assert _wired(sc, sc.symbols[vcc.uuid].pins[0].at) and _all_orthogonal(sc)


async def test_move_power_symbol_onto_own_stub_corner(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """+3V3 at the end of an L stub moved onto the L's corner: no 'pin would land on a
    wire' refusal for its own stub; the stub is laid anew as one straight wire."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    c = await _place(ctx, "Device:C", [101.6, 76.2])
    a, b = await _pin(ctx, f"{c}.1"), await _pin(ctx, f"{c}.2")
    top = a if a[1] < b[1] else b
    corner, end = (top[0], top[1] - 5.08), (top[0] + 7.62, top[1] - 5.08)
    await place_raw(s, "power:+3V3", end, "#PWR01")
    await _ok(ctx, "sch_add_wire", points=[list(top), list(corner), list(end)])
    sc0 = await _scene(s)
    vcc = next(v for v in sc0.symbols.values() if v.power)
    assert same_point(vcc.pins[0].at, end)
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[vcc.uuid], by=[-7.62, 0])
    assert (await state(s))[1] == partition
    sc = await _scene(s)
    assert _shape(sc) == [(top, corner)] or _shape(sc) == [(corner, top)], _shape(sc)
    assert same_point(sc.symbols[vcc.uuid].pins[0].at, corner)
    assert out["connectivity_verified"] is True


async def test_move_two_pins_same_power_symbol_carried_once(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Both pins of R on one GND via a shared stub (GND on its corner): carried once."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _place(ctx, "Device:R", [101.6, 76.2], 90)
    a, b = await _pin(ctx, f"{r}.1"), await _pin(ctx, f"{r}.2")
    left, right = sorted([a, b])
    c1, c2 = (left[0], left[1] + 5.08), (right[0], right[1] + 5.08)
    await place_raw(s, "power:GND", c1, "#PWR01")
    await _ok(ctx, "sch_add_wire", points=[list(left), list(c1)])
    await _ok(ctx, "sch_add_wire", points=[list(c1), list(c2), list(right)])
    sc0 = await _scene(s)
    assert len(sc0.wires) == 3 and not sc0.junctions, (_shape(sc0), sc0.junctions)
    gnd = next(v for v in sc0.symbols.values() if v.power)
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[r], by=[5.08, 2.54])
    assert out["carried_wires"] == 3
    assert (await state(s))[1] == partition
    sc = await _scene(s)
    assert _shape(sc) == _shifted(_shape(sc0), (5.08, 2.54))
    assert same_point(sc.symbols[gnd.uuid].at, (gnd.at[0] + 5.08, gnd.at[1] + 2.54))


async def test_move_switch_carries_label_and_power(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """SW301's shape: a push button with a label stub on one pin and GND on the other."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    sw = await _place(ctx, "Switch:SW_Push", [101.6, 76.2])
    await _ok(ctx, "sch_add_label", net="BTN", at=[f"{sw}.1"])
    await _ok(ctx, "sch_add_power", net="GND", at=[f"{sw}.2"])
    sc0 = await _scene(s)
    _ids, partition = await state(s)
    nets = await pin_nets(s)
    d = (5.08, 7.62)
    out = await _ok(ctx, "sch_move", items=[sw], by=list(d))
    assert out["carried_wires"] == len(sc0.wires)
    assert (await state(s))[1] == partition
    assert await pin_nets(s) == nets
    sc = await _scene(s)
    assert _shape(sc) == _shifted(_shape(sc0), d)
    p0, p1 = _power_at(sc0), _power_at(sc)
    assert all(same_point(p1[r], (a[0] + d[0], a[1] + d[1])) for r, a in p0.items())
    (lb0,) = sc0.labels.values()
    assert same_point(sc.labels[lb0.uuid].at, (lb0.at[0] + d[0], lb0.at[1] + d[1]))


async def test_move_two_labels_on_one_stub_laid_once(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Two labels on the same stub end, both moved: the stub is laid once."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _place(ctx, "Device:R", [101.6, 76.2])
    out = await _ok(ctx, "sch_add_label", net="SDA", at=[f"{r}.1"])
    first = next(x for x in out["created"] if x["kind"] == "label")
    out = await _ok(ctx, "sch_add_label", net="SDA", at=first["at_mm"])
    second = next(x for x in out["created"] if x["kind"] == "label")
    _ids, partition = await state(s)
    n_wires = len((await _scene(s)).wires)
    out = await _ok(ctx, "sch_move", items=[first["uuid"], second["uuid"]], by=[7.62, -5.08])
    assert out["connectivity_verified"] is True
    assert (await state(s))[1] == partition
    sc = await _scene(s)
    new_at = (first["at_mm"][0] + 7.62, first["at_mm"][1] - 5.08)
    assert all(same_point(sc.labels[x["uuid"]].at, new_at) for x in (first, second))
    assert _wired(sc, new_at) and _all_orthogonal(sc)
    assert len(sc.wires) <= n_wires + 1  # one L at most, not two
    assert mm_point((await _symbol(s, r)).position) == (101.6, 76.2)


async def test_move_label_alone_lays_multi_wire_stub_anew(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _place(ctx, "Device:R", [101.6, 76.2])
    a, b = await _pin(ctx, f"{r}.1"), await _pin(ctx, f"{r}.2")
    top = a if a[1] < b[1] else b
    corner, end = (top[0], top[1] - 5.08), (top[0] + 7.62, top[1] - 5.08)
    await _ok(ctx, "sch_add_wire", points=[list(top), list(corner), list(end)])
    out = await _ok(ctx, "sch_add_label", net="CLK", at=list(end))
    label = next(x for x in out["created"] if x["kind"] == "label")
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[label["uuid"]], by=[-7.62, -2.54])
    assert (await state(s))[1] == partition
    sc = await _scene(s)
    new_at = (corner[0], corner[1] - 2.54)
    assert same_point(sc.labels[label["uuid"]].at, new_at)
    assert _shape(sc) == sorted([(top, new_at)]) or _shape(sc) == sorted([(new_at, top)])


async def test_move_refused_when_carried_stub_would_touch_another_net(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """The carried +3V3 stub would touch the end of a FOO wire at its new place: refused,
    nothing written."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    c = await _cap_with_rails(ctx)
    await _ok(ctx, "sch_add_wire", points=[[109.22, 71.12], [124.46, 71.12]])
    await _ok(ctx, "sch_add_label", net="FOO", at=[124.46, 71.12])
    before = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=[c], by=[7.62, 0])
    assert err and out["error"]["code"] == "overlap", out
    assert "stub wire" in out["error"]["what"], out
    assert await state(s) == before


async def test_move_carried_stubs_are_free(blank_session: KicadSession, edit_db: Database) -> None:
    """#13 (spec §9.1): a moved symbol costs 1; the stub chains it carries are free."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _place(ctx, "Device:R", [101.6, 76.2])
    await _ok(ctx, "sch_add_label", net="SDA", at=[f"{r}.1"])
    await _ok(ctx, "sch_add_power", net="GND", at=[f"{r}.2"])
    out = await _ok(ctx, "sch_move", items=[r], by=[7.62, 0])
    assert out["carried_wires"] >= 2
    assert out["logical_changes"] == 1
