"""sch_move's owned stub chains (#7/#8) on synthetic scenes (no KiCad)."""

from __future__ import annotations

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
from copperchat.kicad_tools.edit.transform import owned_stub_chains

SHEET = SheetRef("/r", "1", "top.kicad_sch", "", "/")


def _sym(uid: str, *pins: Point, power: bool = False) -> SymbolInfo:
    ends = [
        PinEnd((SHEET.path, uid, str(i + 1)), uid, str(i + 1), "", "passive", p, "up")
        for i, p in enumerate(pins)
    ]
    return SymbolInfo(uid, uid, "Device:R", "R", 1, SHEET, pins[0], 0, "", ends,
                      Box(0, 0, 0, 0), [], power)  # fmt: skip


def _scene(*items: object, junctions: tuple[Point, ...] = ()) -> Scene:
    sc = Scene(sheet=SHEET)
    for it in items:
        if isinstance(it, SymbolInfo):
            sc.symbols[it.uuid] = it
        elif isinstance(it, WireInfo):
            sc.wires[it.uuid] = it
        elif isinstance(it, LabelInfo):
            sc.labels[it.uuid] = it
    sc.junctions = {f"j{i}": j for i, j in enumerate(junctions)}
    return sc


C = _sym("c", (10, 10), (10, 20))
PWR = _sym("p", (0, 5), power=True)
L_STUB = (WireInfo("w1", (10, 10), (10, 5)), WireInfo("w2", (10, 5), (0, 5)))


def test_l_stub_to_power_symbol_is_owned() -> None:
    (chain,) = owned_stub_chains(_scene(C, PWR, *L_STUB), [(10, 10)], {"c"})
    assert chain.wires == ["w1", "w2"] and chain.end_items == ["p"]
    assert chain.points == [(10, 10), (10, 5), (0, 5)]


def test_free_end_and_label_end_are_owned() -> None:
    lb = LabelInfo("lb", "SDA", (0, 5), "label", 0)
    sc = _scene(C, lb, *L_STUB, WireInfo("w3", (10, 20), (10, 25)))
    chains = owned_stub_chains(sc, [(10, 10), (10, 20)], {"c"})
    assert [(c.wires, c.end_items) for c in chains] == [(["w1", "w2"], ["lb"]), (["w3"], [])]


def test_junction_branch_and_other_pin_are_not_owned() -> None:
    assert owned_stub_chains(_scene(C, PWR, *L_STUB, junctions=((10, 5),)), [(10, 10)], {"c"}) == []
    branch = WireInfo("w9", (10, 5), (20, 5))
    assert owned_stub_chains(_scene(C, PWR, *L_STUB, branch), [(10, 10)], {"c"}) == []
    r = _sym("r", (0, 5), (0, 0))
    assert owned_stub_chains(_scene(C, r, *L_STUB), [(10, 10)], {"c"}) == []


def test_t_join_or_label_inside_a_chain_wire_is_not_owned() -> None:
    tee = WireInfo("w9", (5, 5), (5, 0))  # ends on w2's interior
    assert owned_stub_chains(_scene(C, PWR, *L_STUB, tee), [(10, 10)], {"c"}) == []
    inside = LabelInfo("lb", "X", (5, 5), "label", 0)
    assert owned_stub_chains(_scene(C, PWR, *L_STUB, inside), [(10, 10)], {"c"}) == []


def test_a_wire_passing_through_the_pin_is_not_owned() -> None:
    through = WireInfo("w9", (5, 10), (15, 10))
    assert owned_stub_chains(_scene(C, PWR, *L_STUB, through), [(10, 10)], {"c"}) == []


def test_chain_between_two_moving_pins_is_found_once() -> None:
    """Both pins of R on one GND sitting on the shared stub's corner: one chain."""
    r = _sym("r", (0, 0), (10, 0))
    gnd = _sym("g", (0, 5), power=True)
    wires = (
        WireInfo("a", (0, 0), (0, 5)),
        WireInfo("b", (0, 5), (10, 5)),
        WireInfo("c", (10, 5), (10, 0)),
    )
    (chain,) = owned_stub_chains(_scene(r, gnd, *wires), [(0, 0), (10, 0)], {"r"})
    assert chain.wires == ["a", "b", "c"] and chain.end_items == ["g"]
    # the second part does not move: its pin ends the chain foreign
    assert owned_stub_chains(_scene(r, gnd, *wires), [(0, 0)], set()) == []


def _sheet_pin(uid: str, at: Point) -> SheetPinInfo:
    pin = PinEnd((SHEET.path, "sh", uid), "Power", uid, uid, SHEET_PIN, at, "left")
    return SheetPinInfo(uid, "sh", f"{SHEET.path}/sh", pin)


def test_a_listed_sheet_pin_starts_a_chain() -> None:
    """v0.5.0 #1: a moved sheet pin's own stub (here to a label) is owned; an unlisted
    sheet pin stays a blocker, as before."""
    lb = LabelInfo("lb", "VIN", (0, 5), "label", 0)
    sc = _scene(lb, WireInfo("w1", (10, 5), (0, 5)))
    sc.sheet_pins["sp"] = _sheet_pin("sp", (10, 5))
    (chain,) = owned_stub_chains(sc, [(10, 5)], sheet_pins={"sp"})
    assert chain.wires == ["w1"] and chain.end_items == ["lb"]
    assert owned_stub_chains(sc, [(10, 5)]) == []


def test_sheet_pin_chain_to_another_pin_is_not_owned() -> None:
    sc = _scene(_sym("r", (0, 5), (0, 0)), WireInfo("w1", (10, 5), (0, 5)))
    sc.sheet_pins["sp"] = _sheet_pin("sp", (10, 5))
    assert owned_stub_chains(sc, [(10, 5)], sheet_pins={"sp"}) == []
