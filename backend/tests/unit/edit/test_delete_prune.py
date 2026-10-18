"""sch_delete's attached-item pruning on synthetic scenes (no KiCad)."""

from __future__ import annotations

from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.delete import prune
from copperchat.kicad_tools.edit.geometry import Box, PinEnd, Point
from copperchat.kicad_tools.edit.scene import LabelInfo, Scene, SymbolInfo, WireInfo

SHEET = SheetRef("/r", "1", "top.kicad_sch", "", "/")


def _sym(uid: str, ref: str, *pins: Point, power: bool = False) -> SymbolInfo:
    ends = [
        PinEnd((SHEET.path, uid, str(i + 1)), ref, str(i + 1), "", "passive", p, "left")
        for i, p in enumerate(pins)
    ]
    return SymbolInfo(uid, ref, "Device:R", "R", 1, SHEET, pins[0], 0, "", ends,
                      Box(0, 0, 0, 0), [], power)  # fmt: skip


def _scene(*items: object) -> Scene:
    sc = Scene(sheet=SHEET)
    for it in items:
        if isinstance(it, SymbolInfo):
            sc.symbols[it.uuid] = it
        elif isinstance(it, WireInfo):
            sc.wires[it.uuid] = it
        elif isinstance(it, LabelInfo):
            sc.labels[it.uuid] = it
    return sc


def test_stub_with_power_symbol_and_corner_dies() -> None:
    r1 = _sym("r1", "R1", (10, 10), (10, 20))
    pwr = _sym("p", "#PWR01", (0, 5), power=True)
    sc = _scene(r1, pwr, WireInfo("w1", (10, 10), (0, 10)), WireInfo("w2", (0, 10), (0, 5)))
    out = prune(sc, [p.at for p in r1.pins], {"r1"})
    assert sorted(out.wires) == ["w1", "w2"] and out.power == ["p"]


def test_wire_to_another_pin_dies_but_the_pin_stays_anchor() -> None:
    r1 = _sym("r1", "R1", (10, 10), (10, 20))
    r2 = _sym("r2", "R2", (30, 10), (30, 20))
    sc = _scene(r1, r2, WireInfo("w", (10, 10), (30, 10)))
    out = prune(sc, [p.at for p in r1.pins], {"r1"})
    assert out.wires == ["w"]  # served only R1 -> R2 (R2 alone is no connection)


def test_branch_point_keeps_the_shared_wires() -> None:
    r1 = _sym("r1", "R1", (10, 10), (10, 20))
    r2 = _sym("r2", "R2", (40, 0), (50, 0))
    r3 = _sym("r3", "R3", (40, 20), (50, 20))
    sc = _scene(
        r1, r2, r3,
        WireInfo("a", (10, 10), (30, 10)),
        WireInfo("b", (30, 10), (30, 0)), WireInfo("b2", (30, 0), (40, 0)),
        WireInfo("c", (30, 10), (30, 20)), WireInfo("c2", (30, 20), (40, 20)),
    )  # fmt: skip
    out = prune(sc, [p.at for p in r1.pins], {"r1"})
    assert out.wires == ["a"] and not out.shortened


def test_t_join_on_a_wire_interior_shortens_it() -> None:
    """R1.1 -- (20,10) T -- R2.1, R3 T-joins at (20, 10): deleting R1 cuts the wire
    back to the T."""
    r1 = _sym("r1", "R1", (10, 10), (0, 0))
    r2 = _sym("r2", "R2", (30, 10), (40, 40))
    r3 = _sym("r3", "R3", (20, 30), (40, 50))
    sc = _scene(r1, r2, r3, WireInfo("main", (10, 10), (30, 10)),
                WireInfo("t", (20, 30), (20, 10)))  # fmt: skip
    out = prune(sc, [p.at for p in r1.pins], {"r1"})
    assert not out.wires and out.shortened == {"main": ((20, 10), (30, 10))}


def test_a_label_still_reached_by_a_live_wire_stays() -> None:
    """R1.2 has a label stub; R2 is wired to the label's end. Deleting R2 keeps R1's
    label and stub."""
    r1 = _sym("r1", "R1", (10, 10), (10, 20))
    r2 = _sym("r2", "R2", (30, 20), (40, 20))
    sc = _scene(
        r1, r2, WireInfo("stub", (10, 20), (10, 25)), LabelInfo("lb", "MID", (10, 25), "label"),
        WireInfo("link", (10, 25), (30, 25)), WireInfo("link2", (30, 25), (30, 20)),
    )  # fmt: skip
    out = prune(sc, [p.at for p in r2.pins], {"r2"})
    assert sorted(out.wires) == ["link", "link2"] and not out.labels


def test_label_only_on_the_deleted_part_dies_hierarchical_one_stays() -> None:
    r1 = _sym("r1", "R1", (10, 10), (10, 20))
    sc = _scene(
        r1, WireInfo("s1", (10, 10), (10, 5)), LabelInfo("lb", "SIG", (10, 5), "label"),
        WireInfo("s2", (10, 20), (10, 25)), LabelInfo("h", "IN", (10, 25), "hierarchical_label"),
    )  # fmt: skip
    out = prune(sc, [p.at for p in r1.pins], {"r1"})
    # the sheet interface (hierarchical label) stays; the stubs only served R1
    assert out.labels == ["lb"] and sorted(out.wires) == ["s1", "s2"]


def test_kept_sheet_pin_is_an_anchor() -> None:
    r1 = _sym("r1", "R1", (10, 10), (10, 20))
    sc = _scene(r1, WireInfo("w", (10, 10), (30, 10)), WireInfo("w2", (30, 10), (30, 15)))
    out = prune(sc, [(10, 10)], {"r1"}, cuts=[(30, 10)], keep=[(30, 10)])
    assert out.wires == ["w"]  # w2 hangs on the sheet pin and stays
