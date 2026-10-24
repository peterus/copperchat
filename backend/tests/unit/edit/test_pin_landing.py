"""sch_move: a moved sheet pin must not land on an item of another net (v0.5.0 #1); what
leaves that spot in the same call does not count. Synthetic scenes (no KiCad)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.geometry import Box, PinEnd, Point
from copperchat.kicad_tools.edit.scene import (
    SHEET_PIN,
    LabelInfo,
    Scene,
    SheetPinInfo,
    SymbolInfo,
    WireInfo,
)
from copperchat.kicad_tools.edit.transform import _Moving, _refuse_pin_landing

SHEET = SheetRef("/r", "1", "top.kicad_sch", "", "/")
OLD, NEW = (50.0, 50.0), (50.0, 52.54)


def _spin(uid: str, name: str, at: Point) -> SheetPinInfo:
    pin = PinEnd((SHEET.path, "s", name), "Power", name, name, SHEET_PIN, at, "left")
    return SheetPinInfo(uid, "s", "/r/s", pin)


def _sym(uid: str, at: Point) -> SymbolInfo:
    end = PinEnd((SHEET.path, uid, "1"), "R1", "1", "", "passive", at, "up")
    return SymbolInfo(uid, "R1", "Device:R", "R", 1, SHEET, at, 0, "", [end],
                      Box(0, 0, 0, 0), [], False)  # fmt: skip


def _world(pin_nets: dict[Any, str], sheet_pin_nets: dict[str, str]) -> Any:
    return SimpleNamespace(
        netlist=SimpleNamespace(net_of=lambda key: pin_nets.get(key)),
        sheet_pin_net=lambda info: sheet_pin_nets.get(info.uuid),
    )


def _scene(*items: object) -> Scene:
    sc = Scene(sheet=SHEET)
    for it in items:
        if isinstance(it, SheetPinInfo):
            sc.sheet_pins[it.uuid] = it
        elif isinstance(it, WireInfo):
            sc.wires[it.uuid] = it
        elif isinstance(it, LabelInfo):
            sc.labels[it.uuid] = it
        elif isinstance(it, SymbolInfo):
            sc.symbols[it.uuid] = it
    return sc


def _move(net: str | None = "/VIN") -> _Moving:
    return _Moving(OLD, NEW, net, "s", "left", sheet_pin="vin")


def _run(sc: Scene, nets: dict[str, str], world: Any = None, **kw: Any) -> None:
    _refuse_pin_landing(world or _world({}, {}), "sch_move", sc, nets, [_move()],
                        kw.get("moved", set()), kw.get("dragged", set()),
                        kw.get("vacated", [OLD]))  # fmt: skip


VIN = _spin("vin", "VIN", OLD)


@pytest.mark.parametrize(
    ("item", "nets", "world"),
    [
        (WireInfo("w", NEW, (40.0, 52.54)), {"w": "/GND"}, None),
        (LabelInfo("lb", "GND", NEW, "label", 0), {"lb": "/GND"}, None),
        (WireInfo("w", NEW, (40.0, 52.54)), {}, None),  # an unconnected wire joins too
        (_sym("r", NEW), {}, _world({(SHEET.path, "r", "1"): "/GND"}, {})),
        (_spin("gnd", "GND", NEW), {}, _world({}, {"gnd": "/GND"})),
    ],
)
def test_landing_on_another_net_refused(item: Any, nets: dict[str, str], world: Any) -> None:
    with pytest.raises(StructuredToolError) as exc:
        _run(_scene(VIN, item), nets, world)
    assert exc.value.code == "overlap"


def test_landing_on_own_net_allowed() -> None:
    _run(_scene(VIN, WireInfo("w", NEW, (40.0, 52.54))), {"w": "/VIN"})


def test_items_moving_away_do_not_count() -> None:
    gnd = _spin("gnd", "GND", NEW)
    lb = LabelInfo("lb", "GND", NEW, "label", 0)
    _run(_scene(VIN, gnd, lb), {"lb": "/GND"}, _world({}, {"gnd": "/GND"}),
         moved={"gnd", "lb"})  # fmt: skip


def test_wire_end_at_a_vacated_point_is_dragged_away() -> None:
    """GND (moving too) sat at NEW with a wire: the wire's end leaves with it."""
    w = WireInfo("w", NEW, (40.0, 52.54))
    _run(_scene(VIN, w), {"w": "/GND"}, dragged={"w"}, vacated=[OLD, NEW])


def test_wire_that_stays_put_at_a_vacated_point_refused() -> None:
    """Not dragged (something joins its interior): its end stays at NEW."""
    w = WireInfo("w", NEW, (40.0, 52.54))
    with pytest.raises(StructuredToolError):
        _run(_scene(VIN, w), {"w": "/GND"}, vacated=[OLD, NEW])


def test_dragged_wire_end_elsewhere_still_refused() -> None:
    """The wire is dragged by its other end; the end at NEW stays."""
    w = WireInfo("w", NEW, (40.0, 52.54))
    with pytest.raises(StructuredToolError):
        _run(_scene(VIN, w), {"w": "/GND"}, dragged={"w"}, vacated=[OLD, (40.0, 52.54)])


def test_wire_interior_at_a_vacated_point_still_refused() -> None:
    w = WireInfo("w", (50.0, 45.0), (50.0, 60.0))  # runs through NEW
    with pytest.raises(StructuredToolError):
        _run(_scene(VIN, w), {"w": "/GND"}, dragged={"w"}, vacated=[OLD, NEW])
