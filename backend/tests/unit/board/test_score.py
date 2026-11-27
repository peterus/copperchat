"""P4 placer: placement score (B-C11 `score_placement`)."""

from __future__ import annotations

import math

import pytest

from copperchat.board.boardmd import BoardDoc
from copperchat.board.classify import classify
from copperchat.board.model import BoardModel
from copperchat.board.placer import MAX_CELL_PINS, PlacementScore, score_placement
from copperchat.board.placer.score import airwire_mm, max_cell_pins

from .builders import outline, part
from .builders_circuit import cap, circuit, ic, pad


def _score(m: BoardModel) -> PlacementScore:
    return score_placement(m, classify(m), BoardDoc())


def test_airwire_octilinear_mst() -> None:
    # net A: pads at (10, 10), (13, 10), (13, 14) -> MST 3 + 4 (the diagonal 4 + 3(sqrt2 - 1)
    # is longer); net B: (20, 20) - (24, 23) one octilinear leg 4 + 3(sqrt2 - 1);
    # GND is never counted
    k = math.sqrt(2) - 1
    m = circuit(
        part("P1", 10, 10, pads=(pad("P1", "1", "A", (10, 10)), pad("P1", "2", "GND", (0, 0)))),
        part("P2", 13, 10, pads=(pad("P2", "1", "A", (13, 10)), pad("P2", "2", "B", (20, 20)))),
        part("P3", 13, 14, pads=(pad("P3", "1", "A", (13, 14)), pad("P3", "2", "B", (24, 23)))),
        part("P4", 60, 60, pads=(pad("P4", "1", "GND", (60, 60)),)),
    )
    cls = classify(m)
    assert cls.net_kind["GND"] == "ground"
    want = 3 + 4 + 4 + 3 * k
    assert airwire_mm(m, cls) == pytest.approx(want, abs=1e-3)
    assert _score(m).airwire_mm == pytest.approx(want, abs=1e-3)


def test_max_cell_pins() -> None:
    # 12 pads in the cell [10, 15) x [10, 15), 7 in the cell to its right; a pad outside the
    # outline does not count
    pins_a = {str(i): f"N{i}" for i in range(1, 13)}
    pins_b = {str(i): f"M{i}" for i in range(1, 8)}
    m = circuit(
        ic("U1", pins_a, (10.0, 11.0), pitch=0.4),  # x 10 .. 14.4
        ic("U2", pins_b, (15.5, 11.0), pitch=0.5),  # x 15.5 .. 18.5
        ic("U3", {str(i): f"Q{i}" for i in range(1, 41)}, (200.0, 11.0), pitch=0.1),
        outline=outline(0, 0, 100, 80),
    )
    assert max_cell_pins(m) == 12
    s = _score(m)
    assert s.max_cell_pins == 12
    assert s.routability_ok is (MAX_CELL_PINS >= 12)
    dense = circuit(
        ic("U1", {str(i): f"N{i}" for i in range(1, MAX_CELL_PINS + 2)}, (10.0, 11.0), pitch=0.1)
    )
    d = _score(dense)
    assert d.max_cell_pins == MAX_CELL_PINS + 1
    assert d.routability_ok is False


def test_score_uses_p3a_metrics() -> None:
    pins = {"1": "+3V3", "2": "GND", "3": "A", "4": "B", "5": "C", "6": "D", "7": "+3V3"}
    good = circuit(ic("U1", pins, (50.0, 40.0)), cap("C1", "+3V3", "GND", (57.5, 42.0)))
    g = _score(good)
    assert g.m1_violations == 0
    assert g.m8_worst_mm is not None and g.m8_worst_mm <= 3.0
    # an overlapping part -> M1; the decoupling cap 10 mm away -> M8 worst > 3 mm
    bad = circuit(
        ic("U1", pins, (50.0, 40.0)),
        cap("C1", "+3V3", "GND", (67.0, 40.0)),
        cap("C2", "X", "Y", (52.0, 40.5)),
    )
    b = _score(bad)
    assert b.m1_violations >= 1
    assert b.m8_worst_mm is not None and b.m8_worst_mm > 3.0
    assert b.m10_worst_ratio is None or b.m10_worst_ratio >= 0
    assert b.m9_reviews >= 0


def test_spread_score() -> None:
    # usable = 100 x 80 outline minus the 0.3 mm edge margin; courtyards span x 9..61 and
    # y 9..31 -> min(52 / 99.4, 22 / 79.4); a mounting hole in the far corner and a part in
    # the staging area right of the outline do not count
    m = circuit(
        part("U1", 10, 10, half=1.0),
        part("U2", 60, 30, half=1.0),
        part("H1", 97, 77, half=1.0, lib_id="MountingHole:MountingHole_3.2mm_M3"),
        part("U3", 150, 70, half=1.0),
        outline=outline(0, 0, 100, 80),
    )
    s = _score(m)
    assert s.spread == pytest.approx(min(52 / 99.4, 22 / 79.4), abs=1e-4)
    assert _score(circuit(part("U1", 150, 10), outline=outline(0, 0, 100, 80))).spread == 0.0
    none = circuit(part("U1", 10, 10), outline=outline(0, 0, 100, 80))
    from dataclasses import replace

    assert _score(replace(none, outline=replace(none.outline, kind="none"))).spread == 0.0
