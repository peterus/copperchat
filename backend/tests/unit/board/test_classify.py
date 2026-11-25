from __future__ import annotations

import random
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from copperchat.board.boardmd import BoardDoc, ClassOverride
from copperchat.board.classify import (
    BlockProposal,
    Crystal,
    Decoupling,
    DiffPair,
    Switcher,
    classify,
    primary_ground,
)
from copperchat.board.geom import rect_poly
from copperchat.board.model import BoardModel, Part, Point

from .builders import zone
from .builders_circuit import cap, circuit, conn, ic, ind, res, xtal


def test_no_kipy_import() -> None:
    code = "import sys, copperchat.board.classify;assert 'kipy' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


# ---- nets ----------------------------------------------------------------------------------


def test_ground_patterns() -> None:
    m = circuit(
        res("R1", "GND", "SIGNAL"),
        res("R2", "/AGND", "PGND_1"),
        res("R3", "VSS", "GNDA"),
    )
    kinds = classify(m).net_kind
    for net in ("GND", "/AGND", "PGND_1", "VSS", "GNDA"):
        assert kinds[net] == "ground", net
    assert kinds["SIGNAL"] == "signal"


def test_primary_ground_most_pads() -> None:
    m = circuit(
        res("R1", "GND", "A"),
        res("R2", "GND", "B"),
        res("R3", "GND", "AGND"),
        res("R4", "AGND", "C"),
        res("R5", "AGND", "DGND"),
    )
    # GND 3 pads, AGND 3 pads -> tie broken by name; add one more GND pad
    m2 = circuit(*m.parts, res("R6", "GND", "D"))
    assert primary_ground(classify(m2), m2) == "GND"
    m3 = circuit(*m.parts, res("R6", "AGND", "D"))
    assert primary_ground(classify(m3), m3) == "AGND"
    m4 = circuit(res("R1", "A", "B"))
    assert primary_ground(classify(m4), m4) is None


def test_supply_patterns() -> None:
    m = circuit(
        res("R1", "+3V3", "3V3"),
        res("R2", "VDD_IO", "+12V"),
        res("R3", "VBUS", "/sub/VCC_A"),
        res("R4", "DATA", "V3X"),
    )
    kinds = classify(m).net_kind
    for net in ("+3V3", "3V3", "VDD_IO", "+12V", "VBUS", "/sub/VCC_A"):
        assert kinds[net] == "supply", net
    assert kinds["DATA"] == "signal"
    assert kinds["V3X"] == "signal"


def test_supply_structural() -> None:
    m = circuit(
        ic("U1", {"1": "RAIL", "2": "RAIL", "3": "GND", "4": "A", "5": "B"}),
        res("R1", "RAIL", "N"),
        cap("C1", "RAIL", "GND"),
        # RAIL2: 4 pads on 3 parts but no cap to ground
        ic("U2", {"1": "RAIL2", "2": "RAIL2", "3": "GND", "4": "C", "5": "D"}, (10.0, 10.0)),
        res("R2", "RAIL2", "M"),
        res("R3", "RAIL2", "GND"),
    )
    kinds = classify(m).net_kind
    assert kinds["RAIL"] == "supply"
    assert kinds["RAIL2"] == "signal"


# ---- decoupling, switchers, crystals ------------------------------------------------------


def _mcu(ref: str = "U1", at: Point = (50.0, 40.0)) -> Part:
    pins = {"1": "VDD", "2": "GND", "3": "A", "4": "B", "5": "C", "6": "D", "7": "VDD", "8": "E"}
    return ic(ref, pins, at)


def test_decoupling_and_served_pin() -> None:
    # pin 1 at x=50, pin 7 at x=56; the cap's VDD pad at x=56.5
    m = circuit(_mcu(), cap("C1", "VDD", "GND", (57.0, 42.0)))
    cls = classify(m)
    assert cls.decoupling == (Decoupling("C1", "U1", "7", "VDD", "GND"),)
    assert cls.part_class["C1"] == "decoupling_cap"
    assert cls.part_class["U1"] == "anchor"
    m2 = circuit(_mcu(), cap("C1", "GND", "VDD", (49.0, 42.0)))
    assert classify(m2).decoupling == (Decoupling("C1", "U1", "1", "VDD", "GND"),)


def _buck(sheet: str = "/", at: Point = (10.0, 10.0)) -> list[Part]:
    x, y = at
    pins = {"1": "VIN", "2": "GND", "3": "SW", "4": "BST", "5": "FB", "6": "VIN"}
    return [
        ic("U1", pins, at, sheet=sheet),
        conn("J1", {"1": "VIN", "2": "GND"}, (x - 8, y), sheet=sheet),
        cap("C1", "VIN", "GND", (x, y + 2), sheet=sheet),
        cap("C2", "BST", "SW", (x + 3, y - 2), sheet=sheet),
        ind("L1", "SW", "+3V3", (x + 3, y + 3), sheet=sheet),
        cap("C3", "+3V3", "GND", (x + 5, y + 3), sheet=sheet),
        res("R1", "+3V3", "FB", (x + 4, y + 5), sheet=sheet),
        res("R2", "FB", "GND", (x + 4, y + 6), sheet=sheet),
    ]


def test_switcher_switch_node() -> None:
    cls = classify(circuit(*_buck()))
    assert cls.switchers == (
        Switcher(
            ic="U1",
            inductor="L1",
            sw_net="SW",
            vin_net="VIN",
            in_caps=("C1",),
            out_caps=("C3",),
            fb_parts=("R1", "R2"),
        ),
    )
    assert cls.part_class["U1"] == "switching_regulator"
    assert cls.part_class["L1"] == "inductor"
    assert cls.net_kind["SW"] == "switch_node"
    assert cls.net_kind["VIN"] == "supply"
    assert {d.cap for d in cls.decoupling}.isdisjoint({"C1", "C3"})


def test_not_switcher_with_third_part() -> None:
    cls = classify(circuit(*_buck(), res("R9", "SW", "GND", (20.0, 20.0))))
    assert cls.switchers == ()
    assert cls.part_class["U1"] == "anchor"
    assert cls.net_kind["SW"] == "signal"


def test_out_caps_by_sheet() -> None:
    mcu_pins = {"1": "+3V3", "2": "GND", "3": "A", "4": "B", "5": "+3V3", "6": "C"}
    m = circuit(
        *_buck(sheet="/power/"),
        ic("U2", mcu_pins, (80.0, 40.0), sheet="/"),
        cap("C4", "+3V3", "GND", (80.0, 42.0), sheet="/"),
    )
    cls = classify(m)
    (sw,) = cls.switchers
    assert sw.out_caps == ("C3",)
    assert cls.decoupling == (Decoupling("C4", "U2", "1", "+3V3", "GND"),)


def test_out_caps_nearer_anchor_on_same_sheet() -> None:
    mcu_pins = {"1": "+3V3", "2": "GND", "3": "A", "4": "B", "5": "+3V3", "6": "C"}
    m = circuit(
        *_buck(sheet="/"),
        ic("U2", mcu_pins, (80.0, 40.0), sheet="/"),
        cap("C4", "+3V3", "GND", (80.0, 42.0), sheet="/"),
    )
    cls = classify(m)
    (sw,) = cls.switchers
    assert sw.out_caps == ("C3",)
    assert [d.cap for d in cls.decoupling] == ["C4"]


def test_crystal_load_caps() -> None:
    pins = {"1": "XIN", "2": "XOUT", "3": "GND", "4": "+3V3", "5": "IO"}
    m = circuit(
        ic("U1", pins),
        xtal("Y1", "XIN", "XOUT", (50.0, 45.0)),
        cap("C1", "XIN", "GND", (48.0, 46.0)),
        cap("C2", "XOUT", "GND", (52.0, 46.0)),
        cap("C5", "+3V3", "GND", (53.0, 38.0)),
    )
    cls = classify(m)
    assert cls.crystals == (Crystal("Y1", "U1", ("C1", "C2"), ("XIN", "XOUT")),)
    assert cls.part_class["Y1"] == "crystal"
    assert cls.net_kind["XIN"] == "crystal"
    assert [d.cap for d in cls.decoupling] == ["C5"]


# ---- pairs ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("p", "n"),
    [("USB_DP", "USB_DM"), ("USB_D+", "USB_D-"), ("SIG+", "SIG-"), ("USB_DP", "USB_DN")],
)
def test_diff_pair_suffixes(p: str, n: str) -> None:
    m = circuit(
        ic("U1", {"1": p, "2": n, "3": "GND", "4": "+3V3", "5": "X"}),
        ic("U2", {"1": p, "2": n, "3": "GND", "4": "+3V3", "5": "Y", "6": "Z"}, (10.0, 10.0)),
        conn("J1", {"1": p, "2": n}),
    )
    cls = classify(m)
    assert cls.pairs == (DiffPair(p, n, "U2"),)
    assert cls.net_kind[p] == cls.net_kind[n] == "diff_pair"


def test_no_pair_without_common_anchor() -> None:
    m = circuit(
        ic("U1", {"1": "USB_DP", "2": "GND", "3": "A", "4": "B", "5": "C"}),
        ic("U2", {"1": "USB_DM", "2": "GND", "3": "D", "4": "E", "5": "F"}, (10.0, 10.0)),
        conn("J1", {"1": "USB_DP", "2": "USB_DM"}),
    )
    cls = classify(m)
    assert cls.pairs == ()
    assert cls.net_kind["USB_DP"] == "signal"


# ---- part classes --------------------------------------------------------------------------


def test_connector_by_prefix_and_lib() -> None:
    m = circuit(
        conn("J1", {"1": "A"}, lib_id="Lib:Thing"),
        conn("P2", {"1": "A"}, lib_id="Lib:Thing"),
        conn("CN3", {"1": "A"}, lib_id="Lib:Thing"),
        conn("USB1", {"1": "A"}, lib_id="Lib:Thing"),
        conn("K4", {"1": "A"}, lib_id="Connector_USB:USB_C"),
        res("R1", "A", "B", lib_id="Resistor_SMD:R_0603"),
        res("PS1", "A", "B", lib_id="Lib:Thing"),
    )
    pc = classify(m).part_class
    for ref in ("J1", "P2", "CN3", "USB1", "K4"):
        assert pc[ref] == "connector", ref
    assert pc["R1"] == "other"
    assert pc["PS1"] == "other"


_ANT_PINS = {str(i): f"N{i}" for i in range(1, 9)}


def test_module_with_antenna_rule_area() -> None:
    antenna_poly = tuple(rect_poly(50.0, 38.0, 3.0, 1.0))
    m = circuit(ic("U3", _ANT_PINS, rule_areas=(antenna_poly,)))
    assert classify(m).part_class["U3"] == "module_with_antenna"


def test_module_board_keepout_in_courtyard() -> None:
    # courtyard of U3: x 49..58, y 39..41
    keepout = zone(
        "ant",
        net="",
        poly=tuple(rect_poly(50.0, 39.5, 2.0, 1.0)),
        rule_area=True,
        keepout=frozenset({"tracks", "copper"}),
    )
    m = circuit(ic("U3", _ANT_PINS), zones=(keepout,))
    assert classify(m).part_class["U3"] == "module_with_antenna"


def test_no_rule_area_not_module() -> None:
    elsewhere = zone(
        "ant",
        net="",
        poly=tuple(rect_poly(5.0, 5.0, 2.0, 1.0)),
        rule_area=True,
        keepout=frozenset({"tracks"}),
    )
    assert classify(circuit(ic("U3", _ANT_PINS))).part_class["U3"] == "anchor"
    m = circuit(ic("U3", _ANT_PINS), zones=(elsewhere,))
    assert classify(m).part_class["U3"] == "anchor"


def test_holes_fiducials_testpoints() -> None:
    m = circuit(
        conn("H1", {}, lib_id="Lib:Hole"),
        conn("M2", {}, lib_id="MountingHole:MountingHole_3.2mm_M3"),
        conn("FID1", {}, lib_id="Lib:Mark"),
        conn("F2", {}, lib_id="Fiducial:Fiducial_1mm"),
        conn("TP1", {"1": "A"}, lib_id="Lib:Pad"),
        conn("T2", {"1": "A"}, lib_id="TestPoint:TestPoint_Pad_1.0x1.0mm"),
    )
    pc = classify(m).part_class
    assert pc["H1"] == pc["M2"] == "mounting_hole"
    assert pc["FID1"] == pc["F2"] == "fiducial"
    assert pc["TP1"] == pc["T2"] == "test_point"


# ---- blocks, critical nets -----------------------------------------------------------------


def test_blocks_per_anchor_by_sheet() -> None:
    m = circuit(
        ic(
            "U7",
            {"1": "+5V", "2": "GND", "3": "A", "4": "B", "5": "C"},
            (10.0, 10.0),
            sheet="/power/",
        ),
        cap("C1", "+5V", "GND", (12.0, 12.0), sheet="/power/"),
        res("R1", "A", "GND", (12.0, 14.0), sheet="/power/"),
        ic("U1", {"1": "+3V3", "2": "GND", "3": "X", "4": "Y", "5": "Z"}, (60.0, 40.0)),
        cap("C2", "+3V3", "GND", (61.0, 42.0)),
        res("R2", "X", "Y", (61.0, 44.0)),
    )
    blocks = classify(m).blocks
    assert blocks == (
        BlockProposal("power-U7", "U7", ("C1", "R1", "U7"), "power"),
        BlockProposal("root-U1", "U1", ("C2", "R2", "U1"), "root"),
    )


def test_leftover_joins_most_shared() -> None:
    m = circuit(
        ic("U1", {"1": "N1", "2": "N2", "3": "GND", "4": "+3V3", "5": "N3"}, (10.0, 10.0)),
        ic(
            "U2",
            {"1": "N4", "2": "+5V", "3": "GND", "4": "+3V3", "5": "N5"},
            (60.0, 10.0),
            sheet="/b/",
        ),
        res("R3", "N1", "N4", (30.0, 30.0), sheet="/b/"),  # one signal net each: sheet wins
        res("R5", "+5V", "GND", (30.0, 40.0)),  # no signal nets: U2 shares 2 nets
        res("R9", "Q1", "Q2", (30.0, 50.0)),  # nothing shared: misc
    )
    blocks = {b.name: b for b in classify(m).blocks}
    assert blocks["b-U2"].refs == ("R3", "R5", "U2")
    assert blocks["root-U1"].refs == ("U1",)
    assert blocks["misc"] == BlockProposal("misc", None, ("R9",), "")


def test_critical_default() -> None:
    # R1 pad 2 at x=10.5, R2 pad 1 at x=20.4 -> 9.9 mm; R3/R4: 10.1 mm
    m = circuit(
        res("R1", "A", "X1", (10.0, 10.0)),
        res("R2", "X1", "B", (20.9, 10.0)),
        res("R3", "C", "X2", (10.0, 30.0)),
        res("R4", "X2", "D", (21.1, 30.0)),
        res("R5", "GND", "E", (10.0, 50.0)),
        res("R6", "GND", "F", (12.0, 50.0)),
    )
    crit = classify(m).critical_nets
    assert "X1" in crit
    assert "X2" not in crit
    assert "GND" not in crit
    assert list(crit) == sorted(crit)


def test_critical_includes_special_nets() -> None:
    cls = classify(circuit(*_buck(), _mcu("U5", (80.0, 40.0)), cap("C9", "VDD", "GND")))
    for net in ("SW", "VIN", "VDD"):
        assert net in cls.critical_nets, net


# ---- overrides -----------------------------------------------------------------------------


def test_override_cap_to_other() -> None:
    m = circuit(_mcu(), cap("C1", "VDD", "GND", (57.0, 42.0)))
    doc = BoardDoc(class_overrides=(ClassOverride("C1", "other", "bulk cap"),))
    cls = classify(m, doc)
    assert cls.part_class["C1"] == "other"
    assert cls.decoupling == ()
    assert len(cls.overrides_applied) == 1
    assert cls.overrides_applied[0].startswith("C1:")


def test_override_unknown_ref() -> None:
    m = circuit(_mcu())
    doc = BoardDoc(class_overrides=(ClassOverride("C99", "other", "x"),))
    cls = classify(m, doc)
    assert cls.overrides_applied == ("C99: unknown ref",)
    assert "C99" not in cls.part_class


def test_override_module() -> None:
    m = circuit(ic("U3", _ANT_PINS))
    doc = BoardDoc(class_overrides=(ClassOverride("U3", "module_with_antenna", "wifi"),))
    assert classify(m, doc).part_class["U3"] == "module_with_antenna"


# ---- determinism, performance --------------------------------------------------------------


def _mixed() -> BoardModel:
    pins = {
        "1": "XIN",
        "2": "XOUT",
        "3": "GND",
        "4": "+3V3",
        "5": "USB_DP",
        "6": "USB_DM",
        "7": "+3V3",
        "8": "IO1",
    }
    return circuit(
        *_buck(sheet="/power/"),
        ic("U2", pins, (60.0, 40.0)),
        xtal("Y1", "XIN", "XOUT", (60.0, 45.0)),
        cap("C10", "XIN", "GND", (58.0, 46.0)),
        cap("C11", "XOUT", "GND", (62.0, 46.0)),
        cap("C12", "+3V3", "GND", (63.0, 38.0)),
        cap("C13", "+3V3", "GND", (67.0, 38.0)),
        conn("J2", {"1": "USB_DP", "2": "USB_DM", "3": "GND", "4": "VBUS"}, (90.0, 40.0)),
        res("R10", "IO1", "LED", (70.0, 50.0)),
        res("R11", "Q1", "Q2", (70.0, 60.0)),
    )


def test_deterministic() -> None:
    m = _mixed()
    base = classify(m)
    assert base.switchers and base.crystals and base.pairs and base.decoupling
    rnd = random.Random(3)
    for _ in range(10):
        parts = list(m.parts)
        rnd.shuffle(parts)
        shuffled = replace(
            m, parts=tuple(replace(p, pads=tuple(rnd.sample(p.pads, len(p.pads)))) for p in parts)
        )
        assert classify(shuffled) == base


def _random_circuit(seed: int, n_ics: int = 20, n_small: int = 180) -> BoardModel:
    rnd = random.Random(seed)
    nets = [f"N{i}" for i in range(280)] + ["GND", "+3V3", "+5V", "VIN"]
    parts: list[Part] = []
    for i in range(n_ics):
        pins = {str(k): rnd.choice(nets) for k in range(1, 17)}
        parts.append(
            ic(
                f"U{i + 1}",
                pins,
                (rnd.uniform(0, 100), rnd.uniform(0, 80)),
                sheet=rnd.choice(["/", "/a/", "/b/"]),
            )
        )
    makers = [cap, res, ind]
    for i in range(n_small):
        mk = rnd.choice(makers)
        prefix = {cap: "C", res: "R", ind: "L"}[mk]
        parts.append(
            mk(
                f"{prefix}{i + 1}",
                rnd.choice(nets),
                rnd.choice(nets),
                (rnd.uniform(0, 100), rnd.uniform(0, 80)),
            )
        )
    return circuit(*parts)


def test_timing_200_parts() -> None:
    m = _random_circuit(7)
    t0 = time.perf_counter()
    cls = classify(m)
    elapsed = time.perf_counter() - t0
    assert len(cls.part_class) == 200
    assert elapsed < 0.5, elapsed
