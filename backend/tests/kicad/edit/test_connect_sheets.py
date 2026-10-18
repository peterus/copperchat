"""Connections across sheets and in a shared sheet file (complex_hierarchy: ampli_ht.kicad_sch
has two instances). E5 review round 1."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.scene import resolve_sheet, sheet_api
from tests.kicad.edit.connect_support import place, scene, sheet_pin_nets, state
from tests.kicad.edit.helpers import v2
from tests.support.edit import call_tool, edit_ctx, place_raw

pytestmark = pytest.mark.kicad

R = "Device:R"
VERTICAL = "/ampli_ht_vertical/"
HORIZONTAL = "/ampli_ht_horizontal/"
VERTICAL_SYMBOL = "00000000-0000-0000-0000-00004b3a1333"  # at (71.12, 111.76), 50.8 x 36.83


async def _parts(s: KicadSession) -> None:
    """R950 on the root sheet (left of the vertical sheet symbol), R951 in ampli_ht (both
    instances show it)."""
    await place(s, (R, "R950", (55.88, 130.81), 90))
    await place_raw(s, R, (15.24, 17.78), "R951", sheet=VERTICAL)


async def _add_sheet_pin(s: KicadSession, name: str, at: tuple[float, float]) -> None:
    """A sheet pin on the vertical sheet symbol, through the API (as E7's tool does)."""
    from kipy.proto.schematic.schematic_types_pb2 import SheetSide
    from kipy.schematic_types import SheetPin

    def run(h: KicadHandle) -> None:
        sch = sheet_api(h, resolve_sheet(h, None))
        sym = next(x for x in sch.get_sheet_symbols() if access.kiid(x) == VERTICAL_SYMBOL)
        pin = SheetPin()
        pin.text.value = name
        pin.position = v2(*at)
        pin.text.position = v2(*at)
        pin.side = SheetSide.SHS_LEFT
        updated = type(sym)(proto=sym.proto)
        updated.pins = [*sym.pins, pin]
        sch.update_items([updated])

    async with s.commit("sheet pin"):
        await s.call(run)


async def _right_pin(ctx: Any, ref: str) -> tuple[str, list[float]]:
    pins = []
    for n in ("1", "2"):
        data, err = await call_tool(ctx, "sch_get_pin", pin=f"{ref}.{n}")
        assert not err, data
        pins.append((f"{ref}.{n}", data["at_mm"]))
    return max(pins, key=lambda p: p[1][0])


async def test_cross_sheet_connect_uses_global_labels(hier_session: KicadSession) -> None:
    s = hier_session
    await _parts(s)
    ctx = edit_ctx(s)
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R950.1", "to": "R951.1"})
    assert not err, out
    assert out["via_used"] == "label" and out["kind"] == "global"
    nets = await sheet_pin_nets(s)
    assert nets[("/", "R950.1")] == nets[(VERTICAL, "R951.1")] == "N_R950_1"


async def test_cross_sheet_fallback_refuses_a_sheet_local_name(hier_session: KicadSession) -> None:
    s = hier_session
    await _parts(s)
    ctx = edit_ctx(s)
    _, err = await call_tool(ctx, "sch_add_label", net="SDA", at=["R951.1"])
    assert not err
    before = await state(s)
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R950.1", "to": "R951.1"})
    assert err and out["error"]["code"] == "invalid_argument"
    assert "local" in out["error"]["why"]
    assert await state(s) == before


async def test_to_a_local_net_of_another_sheet_is_refused(hier_session: KicadSession) -> None:
    s = hier_session
    await _parts(s)
    ctx = edit_ctx(s)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R951.1", "to": "12Vext"})
    assert err and "another sheet" in out["error"]["what"]
    assert await state(s) == before


async def test_hierarchical_label_joins_the_wired_sheet_pin(hier_session: KicadSession) -> None:
    s = hier_session
    await _parts(s)
    await _add_sheet_pin(s, "HIN", (71.12, 130.81))
    ctx = edit_ctx(s)
    pin, at = await _right_pin(ctx, "R950")
    _, err = await call_tool(ctx, "sch_add_wire", points=[at, [71.12, 130.81]])
    assert not err
    out, err = await call_tool(ctx, "sch_add_label", net="HIN", at=["R951.1"], kind="hierarchical")
    assert not err, out
    nets = await sheet_pin_nets(s)
    assert nets[(VERTICAL, "R951.1")] == nets[("/", pin)]


async def test_label_in_a_shared_sheet_file_names_both_instances(
    hier_session: KicadSession,
) -> None:
    s = hier_session
    await _parts(s)
    ctx = edit_ctx(s)
    out, err = await call_tool(ctx, "sch_add_label", net="LOCAL1", at=["R951.1"])
    assert not err, out
    nets = await sheet_pin_nets(s)
    assert nets[(VERTICAL, "R951.1")] == f"{VERTICAL}LOCAL1"
    # the label lives in the shared file: the other instance shows it too (KiCad gives
    # an API-placed symbol no pins in the other instance at the pinned commit)
    other = await scene(s, HORIZONTAL)
    assert any(lb.text == "LOCAL1" for lb in other.labels.values())


async def test_rename_in_a_shared_sheet_and_no_false_clash(hier_session: KicadSession) -> None:
    s = hier_session
    ctx = edit_ctx(s)
    out, err = await call_tool(ctx, "sch_rename_net", old=f"{VERTICAL}PIEZO_IN", new="PIEZO_X")
    assert not err, out
    assert any(HORIZONTAL in n for n in out["notes"])
    nets = set((await sheet_pin_nets(s)).values())
    assert {f"{VERTICAL}PIEZO_X", f"{HORIZONTAL}PIEZO_X"} <= nets
    # a local name used on another sheet does not block a rename on the root sheet
    out, err = await call_tool(ctx, "sch_rename_net", old="/12Vext", new="PIEZO_OUT")
    assert not err, out
    assert out["new"] == "/PIEZO_OUT"
    sc = await scene(s)
    assert any(lb.text == "PIEZO_OUT" for lb in sc.labels.values())


async def test_to_a_net_named_on_the_parent_through_a_sheet_pin(
    hier_session: KicadSession,
) -> None:
    s = hier_session
    await _parts(s)
    await _add_sheet_pin(s, "HIN", (71.12, 130.81))
    ctx = edit_ctx(s)
    pin, at = await _right_pin(ctx, "R950")
    _, err = await call_tool(ctx, "sch_add_wire", points=[at, [71.12, 130.81]])
    assert not err
    _, err = await call_tool(ctx, "sch_add_label", net="HIN", at=[66.04, 130.81])
    assert not err  # the parent net is now '/HIN'
    _, err = await call_tool(ctx, "sch_add_label", net="HIN", at=["R951.1"], kind="hierarchical")
    assert not err
    out, err = await call_tool(ctx, "sch_connect", **{"from": "R951.2", "to": "HIN"})
    assert not err, out
    nets = await sheet_pin_nets(s)
    assert nets[(VERTICAL, "R951.2")] == nets[("/", pin)] == "/HIN"


async def test_same_pin_in_two_instances_is_one_pin(hier_session: KicadSession) -> None:
    s = hier_session
    ctx = edit_ctx(s)

    def pair(h: KicadHandle) -> tuple[str, str]:
        from copperchat.kicad_tools.edit.scene import all_symbols

        by_uuid: dict[str, dict[str, str]] = {}
        for x in all_symbols(h):
            if not x.power and x.pins:
                by_uuid.setdefault(x.uuid, {})[x.sheet.human_path] = x.ref
        refs = next(r for r in by_uuid.values() if VERTICAL in r and HORIZONTAL in r)
        return refs[VERTICAL], refs[HORIZONTAL]

    a, b = await s.call(pair)
    assert a != b  # one symbol of the shared file, two references
    before = await state(s)
    out, err = await call_tool(ctx, "sch_no_connect", pins=[f"{a}.1", f"{b}.1"])
    assert err and "listed twice" in out["error"]["what"]
    assert await state(s) == before
