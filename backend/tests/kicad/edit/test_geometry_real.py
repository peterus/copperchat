"""Geometry and scene against a real api-server (E2 Task 1, port of spike 2A Q1)."""

from __future__ import annotations

import re

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.geometry import CANONICAL, body_box, pin_ends
from copperchat.kicad_tools.edit.scene import read_scene, resolve_pin, sheet_refs
from tests.kicad.edit.helpers import local_label
from tests.support.edit import get_item, place_direct

pytestmark = pytest.mark.kicad

PARTS = [("Device:D", 1, "D"), ("Device:Q_NPN", 1, "Q"), ("Amplifier_Operational:LM358", 2, "U")]


async def test_pin_ends_match_kicad_for_all_transforms(blank_session: KicadSession) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    placed: list[str] = []
    for row, (lib_id, unit, prefix) in enumerate(PARTS):
        for col, tr in enumerate(CANONICAL):
            at = (25.4 + col * 30.48, 38.1 + row * 50.8)
            ref = f"{prefix}{row * 10 + col + 1}"
            placed.append(await place_direct(s, lib_id, at, ref, unit=unit, transform=tr))

    def ends(h: KicadHandle) -> list[tuple[access.PinKey, tuple[float, float], int, bool, bool]]:
        out = []
        for uid in placed:
            sym = get_item(h, uid)
            assert sym is not None
            tr = sym.transform
            for p in pin_ends(sym):
                out.append((p.key, p.at, int(tr.orientation), tr.mirror_x, tr.mirror_y))
        return out

    pins = await s.call(ends)
    # KiCad stored every canonical transform as requested (spike 2A Q1 workaround).
    assert len({(o, mx, my) for _k, _a, o, mx, my in pins}) == 8
    labels = {key: f"N{i}" for i, (key, _at, *_rest) in enumerate(pins)}
    items = [local_label(labels[key], at) for key, at, *_rest in pins]
    async with s.commit("labels"):
        await s.call(lambda h: h.schematic().create_items(items))
    netlist = await s.call(access.live_netlist)
    for key, at, *_ in pins:
        net = netlist.net_of(key)
        assert net is not None and net.rsplit("/", 1)[-1] == labels[key], (key, at, net)


async def test_pin_direction_outward_and_body_box(blank_session: KicadSession) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    uid = await place_direct(s, "Device:R", (50.8, 50.8), "R1")

    def read(h: KicadHandle):  # type: ignore[no-untyped-def]
        sym = get_item(h, uid)
        return {p.number: p for p in pin_ends(sym)}, body_box(sym, margin=0.0), body_box(sym)

    pins, box, padded = await s.call(read)
    assert pins["1"].direction == "up" and pins["2"].direction == "down"
    assert pins["1"].at[1] < pins["2"].at[1]
    assert box.contains(pins["1"].at) and box.contains(pins["2"].at)
    assert box.width <= 6.0
    assert padded.width == pytest.approx(box.width + 4.0)


async def test_resolve_pin(blank_session: KicadSession) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    await place_direct(s, "Amplifier_Operational:LM358", (50.8, 50.8), "U1", unit=1)
    pin = await s.call(lambda h: resolve_pin(h, "U1.3"))
    assert (pin.ref, pin.number) == ("U1", "3")
    by_name = await s.call(lambda h: resolve_pin(h, "U1.+"))
    assert by_name.number == "3"
    with pytest.raises(StructuredToolError) as err:
        await s.call(lambda h: resolve_pin(h, "U1.99"))
    assert err.value.code == "not_found"
    assert "3 (+)" in err.value.details["candidates"]
    with pytest.raises(StructuredToolError) as err:
        await s.call(lambda h: resolve_pin(h, "U7.1"))
    assert err.value.code == "not_found" and "U1" in err.value.details["candidates"]
    await place_direct(s, "Amplifier_Operational:LM358", (101.6, 50.8), "U1", unit=2)
    with pytest.raises(StructuredToolError) as err:  # "+" is pin 3 and pin 5 now
        await s.call(lambda h: resolve_pin(h, "U1.+"))
    assert err.value.code == "ambiguous_pin"


async def test_read_scene_subsheet(hier_session: KicadSession) -> None:
    s = hier_session

    def scenes(h: KicadHandle):  # type: ignore[no-untyped-def]
        refs = [r for r in sheet_refs(h) if r.file == "ampli_ht.kicad_sch"]
        return [(r, read_scene(h, r)) for r in refs]

    result = await s.call(scenes)
    assert len(result) == 2
    for sheet, scene in result:
        refs = {sym.ref for sym in scene.symbols.values() if not sym.power}
        assert refs, sheet
        page = sheet.page
        numbers = [re.sub(r"^[A-Z]+", "", r) for r in refs]
        assert all(n.startswith(page) and len(n) == 3 for n in numbers), (page, refs)
        assert all(sym.sheet == sheet for sym in scene.symbols.values())
        assert all(p.key[0] == sheet.path for sym in scene.symbols.values() for p in sym.pins)
        assert scene.wires and scene.obstacles()
