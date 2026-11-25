"""P4 placer: cluster layout around the anchor (B-C11 `layout_cluster`)."""

from __future__ import annotations

import itertools
import math

from copperchat.board.classify import classify
from copperchat.board.geom import rect_poly
from copperchat.board.model import BoardModel, Pad, Part
from copperchat.board.placer import Cluster, Move, build_clusters, layout_cluster, moved_part
from copperchat.board.placer.layout import box_of

from .builders import part
from .builders_circuit import cap, circuit, pad, res


def qfn(ref: str, pins: dict[str, str], at: tuple[float, float] = (50.0, 40.0)) -> Part:
    """A 2-row IC: pins 1..n/2 down the left column (x - 2), the rest up the right column
    (x + 2), 1 mm pitch; courtyard = pad bbox grown 1 mm (x - 3 .. x + 3)."""
    numbers = sorted(pins, key=int)
    half = len(numbers) // 2
    rows = half - 1
    pads: list[Pad] = []
    for k, n in enumerate(numbers):
        if k < half:
            xy = (at[0] - 2.0, at[1] - rows / 2 + k)
        else:
            xy = (at[0] + 2.0, at[1] + rows / 2 - (k - half))
        pads.append(pad(ref, n, pins[n], xy))
    h = rows + 2.0
    return part(
        ref,
        at[0],
        at[1],
        lib_id="Package_DFN_QFN:QFN",
        pads=tuple(pads),
        courtyard=tuple(rect_poly(at[0] - 3.0, at[1] - h / 2, 6.0, h)),
    )


_PINS = {
    "1": "A",
    "2": "B",
    "3": "GND",
    "4": "C",
    "5": "D",
    "6": "+3V3",  # right column, y - 0.5
    "7": "E",
    "8": "F",
}


def _placed(m: BoardModel, moves: tuple[Move, ...]) -> dict[str, Part]:
    return {mv.ref: moved_part(m.part(mv.ref), mv) for mv in moves}


def _layout(m: BoardModel, refs: list[str], gap: float = 0.05) -> tuple[Move, ...]:
    cls = classify(m)
    (c,) = build_clusters(m, cls, refs)
    return layout_cluster(m, cls, c, gap=gap)


def _gap(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Distance between two boxes along the axis that separates them (negative: overlap)."""
    return max(b[0] - a[2], a[0] - b[2], b[1] - a[3], a[1] - b[3])


def test_decap_on_served_pin_side() -> None:
    m = circuit(qfn("U1", _PINS), cap("C1", "+3V3", "GND", (60.0, 60.0), value="100nF"))
    moves = _layout(m, ["U1", "C1"])
    assert moves[0] == Move("U1", 0.0, 0.0, 0.0, "top")
    placed = _placed(m, moves)
    u, c = placed["U1"], placed["C1"]
    pin = next(q for q in u.pads if q.number == "6")
    assert pin.x > 0  # the served pin is on the anchor's right side
    assert box_of(c)[0] > box_of(u)[2]
    supply = next(q for q in c.pads if q.net == "+3V3")
    ground = next(q for q in c.pads if q.net == "GND")
    d_supply = math.dist((supply.x, supply.y), (pin.x, pin.y))
    d_ground = math.dist((ground.x, ground.y), (pin.x, pin.y))
    assert d_supply < d_ground
    # aligned with the pin (same row)
    assert abs(supply.y - pin.y) < 1e-6


def test_decaps_first_at_min_gap() -> None:
    m = circuit(
        qfn("U1", _PINS),
        cap("C1", "+3V3", "GND", (60.0, 60.0), value="100nF"),
        res("R1", "D", "X", (70.0, 60.0)),
        res("R2", "E", "Y", (75.0, 60.0)),
    )
    assert m.rules and all(k != "courtyard_clearance" for k, _ in m.rules)
    moves = _layout(m, ["U1", "C1", "R1", "R2"], gap=0.05)
    assert [mv.ref for mv in moves][:2] == ["U1", "C1"]
    placed = _placed(m, moves)
    assert abs(_gap(box_of(placed["C1"]), box_of(placed["U1"])) - 0.05) < 0.001
    # other satellites keep gap + SATELLITE_EXTRA_MM (0.2)
    for r in ("R1", "R2"):
        assert _gap(box_of(placed[r]), box_of(placed["U1"])) >= 0.25 - 1e-6


def _busy() -> BoardModel:
    pins = {str(i): f"S{i}" for i in range(1, 17)}
    pins.update({"1": "+3V3", "8": "GND", "9": "+3V3", "12": "VBAT", "16": "GND"})
    return circuit(
        qfn("U1", pins),
        cap("C1", "+3V3", "GND", (60.0, 60.0), value="100nF"),
        cap("C2", "+3V3", "GND", (62.0, 60.0), value="100nF"),
        cap("C3", "+3V3", "GND", (64.0, 60.0), value="10uF"),
        cap("C4", "VBAT", "GND", (66.0, 60.0), value="1uF"),
        res("R1", "S2", "LED1", (70.0, 60.0)),
        res("R2", "S3", "N3", (72.0, 60.0)),
        res("R3", "S3", "N4", (74.0, 60.0)),
        res("R4", "S10", "N5", (76.0, 60.0)),
        res("R5", "S11", "+3V3", (78.0, 60.0)),
        res("D1", "LED1", "GND", (80.0, 60.0), lib_id="Device:LED"),
    )


def test_layout_no_overlap() -> None:
    m = _busy()
    gap = 0.05
    moves = _layout(m, [p.ref for p in m.parts], gap=gap)
    assert sorted(mv.ref for mv in moves) == sorted(p.ref for p in m.parts)
    boxes = {r: box_of(p) for r, p in _placed(m, moves).items()}
    for (ra, a), (rb, b) in itertools.combinations(boxes.items(), 2):
        assert _gap(a, b) >= gap - 1e-6, (ra, rb, a, b)
    for mv in moves:
        assert mv.side == "top"
        for v in (mv.x, mv.y):
            assert abs(v / 0.05 - round(v / 0.05)) < 1e-6, mv


def test_layout_deterministic() -> None:
    m = _busy()
    refs = [p.ref for p in m.parts]
    a = _layout(m, refs)
    b = _layout(m, list(reversed(refs)))
    rev = circuit(*reversed(m.parts))
    c = _layout(rev, refs)
    assert a == b == c


def test_decaps_spread_over_supply_pins() -> None:
    pins = dict(_PINS)
    pins["2"] = "+3V3"  # left column, second pin; pin 6 on the right column
    m = circuit(
        qfn("U1", pins),
        cap("C1", "+3V3", "GND", (60.0, 60.0), value="1uF"),
        cap("C2", "+3V3", "GND", (62.0, 60.0), value="100nF"),
    )
    cls = classify(m)
    (c,) = build_clusters(m, cls, ["U1", "C1", "C2"])
    assert c == Cluster("U1", ("U1", "C1", "C2"))
    placed = _placed(m, layout_cluster(m, cls, c, gap=0.05))
    u = placed["U1"]
    served: dict[str, str] = {}
    for ref in ("C1", "C2"):
        sp = next(q for q in placed[ref].pads if q.net == "+3V3")
        pin = min(
            (q for q in u.pads if q.net == "+3V3"),
            key=lambda q: math.dist((q.x, q.y), (sp.x, sp.y)),
        )
        served[ref] = pin.number
    # smallest value first: 100 nF at pin 2 (first in natural order), 1 uF at pin 6
    assert served == {"C2": "2", "C1": "6"}
    assert box_of(placed["C2"])[2] < box_of(u)[0]
    assert box_of(placed["C1"])[0] > box_of(u)[2]
