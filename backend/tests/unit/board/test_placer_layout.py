"""P4 placer: cluster layout around the anchor (B-C11 `layout_cluster`)."""

from __future__ import annotations

import itertools
import math

import pytest

from copperchat.board.classify import classify
from copperchat.board.geom import rect_poly
from copperchat.board.model import BoardModel, Pad, Part
from copperchat.board.placer import Cluster, Move, build_clusters, layout_cluster, moved_part
from copperchat.board.placer.layout import box_of

from .builders import part
from .builders_circuit import cap, circuit, ind, pad, res, xtal


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


# ---- P9a: a satellite's pads face the host pins of THEIR nets (no crossed crystal) --------


def test_crystal_pads_face_their_own_pins() -> None:
    """A crystal's two signal pads each go next to the IC pin of the same net: the pad of
    OSC_IN at the OSC_IN pin, never crossed over to the OSC_OUT pin (found on sp4_synth,
    where the crossed crystal made the routed OSC_IN path 8.6 mm long)."""
    for a, b in (("OSC_IN", "OSC_OUT"), ("OSC_OUT", "OSC_IN")):
        pins = {"1": a, "2": b, "3": "GND", "4": "S4", "5": "S5", "6": "+3V3", "7": "S7", "8": "S8"}
        m = circuit(qfn("U1", pins), xtal("Y1", "OSC_IN", "OSC_OUT", (70.0, 60.0)))
        placed = _placed(m, _layout(m, ["U1", "Y1"]))
        u, y = placed["U1"], placed["Y1"]
        for q in y.pads:
            own = next(p for p in u.pads if p.net == q.net)
            other = next(p for p in u.pads if p.net != q.net and p.net.startswith("OSC"))
            assert math.dist((q.x, q.y), (own.x, own.y)) < math.dist(
                (q.x, q.y), (other.x, other.y)
            ), (a, q, own, other)


def test_crystal_keeps_an_escape_channel() -> None:
    """A crystal stays CRYSTAL_STANDOFF_MM further from its IC than the minimum gap: the
    IC pins next to the oscillator pins need a track out between them (sp4_synth: a
    flush crystal boxed in the reset pin)."""
    from copperchat.board.placer.layout import CRYSTAL_STANDOFF_MM

    pins = {"1": "S1", "2": "OSC_IN", "3": "OSC_OUT", "4": "S4", "5": "GND", "6": "S6"}
    pins |= {"7": "S7", "8": "S8"}
    m = circuit(qfn("U1", pins), xtal("Y1", "OSC_IN", "OSC_OUT", (70.0, 60.0)))
    gap = 0.05
    placed = _placed(m, _layout(m, ["U1", "Y1"], gap=gap))
    assert CRYSTAL_STANDOFF_MM > 0
    got = _gap(box_of(placed["Y1"]), box_of(placed["U1"]))
    assert got >= gap + CRYSTAL_STANDOFF_MM - 1e-6, got
    assert got <= gap + CRYSTAL_STANDOFF_MM + 0.1, got


def _sot(ref: str, nets: tuple[str, str, str], at: tuple[float, float]) -> Part:
    """A 3-pad part (SOT-23-like, 2 x 3 mm courtyard)."""
    pads = tuple(
        pad(ref, str(i + 1), n, (at[0] - 0.95 + 0.95 * i, at[1])) for i, n in enumerate(nets)
    )
    return part(
        ref, at[0], at[1], pads=pads, lib_id="Package_TO_SOT_SMD:SOT-23",
        courtyard=tuple(rect_poly(at[0] - 1.5, at[1] - 1.0, 3.0, 2.0)),
    )  # fmt: skip


def test_escape_room_per_shared_signal_net() -> None:
    """R2: a > 2-pad satellite keeps ESCAPE_PER_NET_MM more gap per signal net it shares
    with the anchor (capped at ESCAPE_MAX_MM); supply / ground nets and 2-pad parts add
    nothing."""
    from copperchat.board.placer.layout import (
        ESCAPE_MAX_MM,
        ESCAPE_PER_NET_MM,
        SATELLITE_EXTRA_MM,
    )

    gap = 0.05
    m = circuit(qfn("U1", _PINS), _sot("Q1", ("A", "B", "GND"), (70.0, 60.0)))
    cls = classify(m)
    (c,) = build_clusters(m, cls, ["U1", "Q1"])
    placed = _placed(m, layout_cluster(m, cls, c, gap=gap, escape_room=True))
    want = gap + SATELLITE_EXTRA_MM + 2 * ESCAPE_PER_NET_MM
    assert _gap(box_of(placed["Q1"]), box_of(placed["U1"])) >= want - 1e-6
    # off by default (pcb_place_block keeps P4's layout): the plain satellite gap
    plain = _placed(m, _layout(m, ["U1", "Q1"], gap=gap))
    assert _gap(box_of(plain["Q1"]), box_of(plain["U1"])) < want - 1e-6
    # only supply / ground shared: the plain satellite gap
    m = circuit(qfn("U1", _PINS), _sot("Q2", ("+3V3", "GND", "X9"), (70.0, 60.0)))
    cls = classify(m)
    (c,) = build_clusters(m, cls, ["U1", "Q2"])
    placed = _placed(m, layout_cluster(m, cls, c, gap=gap, escape_room=True))
    got = _gap(box_of(placed["Q2"]), box_of(placed["U1"]))
    assert gap + SATELLITE_EXTRA_MM - 1e-6 <= got < gap + SATELLITE_EXTRA_MM + ESCAPE_PER_NET_MM
    # capped: many shared signal nets never add more than ESCAPE_MAX_MM
    pins = {str(i): f"S{i}" for i in range(1, 17)}
    big = part(
        "J9", 80.0, 60.0, lib_id="Lib:Header",
        pads=tuple(pad("J9", str(i), f"S{i}", (76.0 + 0.5 * i, 60.0)) for i in range(1, 9)),
        courtyard=tuple(rect_poly(76.0, 59.0, 5.0, 2.0)),
    )  # fmt: skip
    m = circuit(qfn("U1", pins), big)
    from copperchat.board.placer.layout import _Layout

    # an 8-pad header anchors its own cluster in build_clusters; as a satellite (e.g. a
    # header absorbed by its MCU) its escape room is capped
    lay = _Layout(m, classify(m), Cluster("U1", ("U1", "J9")), gap)
    assert lay._escape("J9") == ESCAPE_MAX_MM
    assert lay._escape("U1") == 0.0


# ---- R2b: relaxed layout ----------------------------------------------------------------------


def _column_ic(ref: str, nets: list[str], pitch: float) -> Part:
    """An IC whose pins form one column on the right edge of its courtyard (x = 52.5, from
    y = 40 down in `pitch` steps); courtyard x 47..53."""
    pads = tuple(pad(ref, str(i + 1), n, (52.5, 40.0 + i * pitch)) for i, n in enumerate(nets))
    h = (len(nets) - 1) * pitch
    return part(
        ref, 50.0, 40.0 + h / 2, lib_id="Package_SO:SOIC", pads=pads,
        courtyard=tuple(rect_poly(47.0, 39.0, 6.0, h + 2.0)),
    )  # fmt: skip


def _relaxed_gap(nets: list[str], pitch: float, sat_net: str) -> tuple[float, float]:
    """(plain, relaxed) courtyard gap of a resistor on `sat_net` next to `_column_ic`."""
    m = circuit(_column_ic("U1", nets, pitch), res("R1", sat_net, "X1", (70.0, 60.0)))
    cls = classify(m)
    (c,) = build_clusters(m, cls, ["U1", "R1"])
    out = []
    for relax in (False, True):
        placed = _placed(m, layout_cluster(m, cls, c, gap=0.05, relax=relax))
        out.append(_gap(box_of(placed["R1"]), box_of(placed["U1"])))
    return out[0], out[1]


def test_relax_gap_counts_escaping_pins() -> None:
    """R2b: a satellite in front of host pins it does not connect to keeps one track pitch
    per two of them (+ one via pitch on a fine-pitch side) more than the plain gap, plus at
    least one pitch to its neighbours (Default class: 0.2 + 0.2 mm)."""
    from copperchat.board.placer.layout import SATELLITE_EXTRA_MM

    nets = [f"S{i}" for i in range(1, 12)]  # pin 6 (y = 42.5 at 0.5 mm) is the satellite's
    plain, relaxed = _relaxed_gap(nets, 0.5, "S6")
    # span of the 2 x 1 mm resistor turned radial (1 mm along y) ± 0.4: y 41.6..43.4 →
    # pins 5 and 7 (y 42.0, 43.0) escape: ceil(2 / 2) = 1 pitch, + a via (0.6 + 0.2) at 0.5 mm
    want = (0.4 - SATELLITE_EXTRA_MM) + 0.4 + 0.8
    assert relaxed - plain >= want - 0.05 - 1e-6, (plain, relaxed)
    assert relaxed - plain <= want + 0.1, (plain, relaxed)
    # a coarse pitch: no via room; pins 1 mm apart: only the satellite's own pin in the span
    plain, relaxed = _relaxed_gap(nets, 1.27, "S6")
    assert relaxed - plain == pytest.approx(0.4 - SATELLITE_EXTRA_MM, abs=0.05 + 1e-6)


def test_relax_ground_pins_need_only_a_via() -> None:
    """Ground pins take no track (the plane does) but on a fine-pitch side their fanout via
    goes right at the pin: via room only; at a coarse pitch nothing."""
    from copperchat.board.placer.layout import SATELLITE_EXTRA_MM

    nets = ["GND"] * 5 + ["S6"] + ["GND"] * 5
    plain, relaxed = _relaxed_gap(nets, 0.5, "S6")
    assert relaxed - plain == pytest.approx(0.4 - SATELLITE_EXTRA_MM + 0.8, abs=0.05 + 1e-6)
    plain, relaxed = _relaxed_gap(nets, 1.27, "S6")
    assert relaxed - plain == pytest.approx(0.4 - SATELLITE_EXTRA_MM, abs=0.05 + 1e-6)


def test_relax_decap_gets_via_room_only() -> None:
    """A decap in front of a fine-pitch side: one via pitch when another pin (its GND
    neighbour) faces it, never track room (M8: close to the pin it serves)."""
    nets = ["S1", "S2", "S3", "S4", "GND", "+3V3", "S7", "S8", "S9", "S10", "S11"]
    m = circuit(_column_ic("U1", nets, 0.5), cap("C1", "+3V3", "GND", (70.0, 60.0), value="100nF"))
    cls = classify(m)
    assert [d.cap for d in cls.decoupling] == ["C1"]
    (c,) = build_clusters(m, cls, ["U1", "C1"])
    gaps = []
    for relax in (False, True):
        placed = _placed(m, layout_cluster(m, cls, c, gap=0.05, relax=relax))
        gaps.append(_gap(box_of(placed["C1"]), box_of(placed["U1"])))
    assert gaps[1] - gaps[0] == pytest.approx(0.8, abs=0.05 + 1e-6), gaps


def test_relax_keeps_decap_and_crystal_gaps() -> None:
    """Decaps (M8) and crystals (the P9a standoff window) keep their P4 gaps."""
    pins = {"1": "S1", "2": "OSC_IN", "3": "OSC_OUT", "4": "S4", "5": "GND", "6": "+3V3"}
    pins |= {"7": "S7", "8": "S8"}
    m = circuit(
        qfn("U1", pins),
        xtal("Y1", "OSC_IN", "OSC_OUT", (70.0, 60.0)),
        cap("C1", "+3V3", "GND", (80.0, 60.0), value="100nF"),
    )
    cls = classify(m)
    (c,) = build_clusters(m, cls, ["U1", "Y1", "C1"])
    plain = layout_cluster(m, cls, c, gap=0.05)
    relaxed = layout_cluster(m, cls, c, gap=0.05, relax=True)
    assert plain == relaxed


def test_relax_off_is_p4_layout() -> None:
    from copperchat.board.placer.pack import block_layout

    m = circuit(qfn("U1", _PINS), _sot("Q1", ("A", "B", "GND"), (70.0, 60.0)))
    cls = classify(m)
    assert block_layout(m, cls, ["U1", "Q1"], keep_channels=0.5) == block_layout(
        m, cls, ["U1", "Q1"], keep_channels=0.5, relax=False
    )


def test_block_layout_relaxed_channels() -> None:
    """R2b: two clusters of one block (two anchors) sharing six signal nets are shelf-packed
    `channel_mm` apart (1.0 + ceil(6.25 / 2) x 0.4 mm), not `keep_channels`."""
    from copperchat.board.placer.pack import block_layout
    from copperchat.board.placer.routability import channel_mm

    shared = {str(i): f"L{i}" for i in range(1, 7)} | {"7": "GND", "8": "+3V3"}
    m = circuit(qfn("U1", shared, (50.0, 40.0)), qfn("U2", shared, (80.0, 40.0)))
    cls = classify(m)
    assert len(build_clusters(m, cls, ["U1", "U2"])) == 2
    want = channel_mm(6.25, 0.4, 0.5)
    assert want == pytest.approx(2.6)
    for relax, least in ((False, 0.5), (True, want)):
        mv = block_layout(m, cls, ["U1", "U2"], keep_channels=0.5, relax=relax)
        placed = _placed(m, mv)
        got = _gap(box_of(placed["U1"]), box_of(placed["U2"]))
        assert got >= least - 1e-6, (relax, got)
        if not relax:
            assert got < want - 1e-6


def _power_column(power: str = "VSYS") -> BoardModel:
    """`_column_ic` with pins S1, S2, VSYS (wide, Power 0.5 mm), S4 (the satellite's), S5
    at 0.5 mm pitch, plus a resistor on S4."""
    from .builders import net_class

    nets = ["S1", "S2", power, "S4", "S5"]
    return circuit(
        _column_ic("U1", nets, 0.5),
        res("R1", "S4", "X1", (70.0, 60.0)),
        net_classes=(net_class(), net_class("Power", 0.5, 0.2, patterns=("VSYS",))),
        net_class_of=((power, f"Effective for net: {power}"),),
    )


def test_escape_strip_kept_free_in_front_of_wide_pins() -> None:
    """R7 (ODH: resistors tight in front of a charger's VSYS pins, a CC resistor in front of
    a USB-C VBUS pin): with `escape_room` / `relax`, a satellite never covers the strip in
    front of a host pad of a wide net it does not connect to; P4's layout is unchanged."""
    from copperchat.board.placer.layout import escape_strips, wide_nets

    m = _power_column()
    cls = classify(m)
    assert wide_nets(m, cls, ["S1", "VSYS", "GND"]) == {"VSYS": (0.5, 0.2)}
    host = m.part("U1")
    ((net, strip),) = escape_strips(host, {"VSYS": (0.5, 0.2)})
    # pin 3 at y = 41.0 (0.6 x 0.6 pad, right edge x 52.8): across 41.0 ± (0.3 + 0.2),
    # outwards 1.5 mm (entry zone of a 0.5 mm track)
    assert net == "VSYS"
    assert strip == pytest.approx((52.8, 40.5, 54.3, 41.5))
    (c,) = build_clusters(m, cls, ["U1", "R1"])

    def covers(flag: dict[str, bool]) -> bool:
        placed = _placed(m, layout_cluster(m, cls, c, gap=0.05, **flag))  # anchor at (0, 0)
        ((_, s),) = escape_strips(placed["U1"], {"VSYS": (0.5, 0.2)})
        b = box_of(placed["R1"])
        return b[0] < s[2] and s[0] < b[2] and b[1] < s[3] and s[1] < b[3]

    assert not covers({"relax": True})
    assert not covers({"escape_room": True})
    assert covers({})  # P4's layout (pcb_place_block) unchanged: flush at the S4 pin
    # a satellite on the wide net itself may sit there (a decap / its own pin)
    own = circuit(
        _column_ic("U1", ["S1", "S2", "VSYS", "S4", "S5"], 0.5),
        res("R1", "VSYS", "X1", (70.0, 60.0)),
        net_classes=m.net_classes,
        net_class_of=m.net_class_of,
    )
    (c2,) = build_clusters(own, classify(own), ["U1", "R1"])
    placed = _placed(own, layout_cluster(own, classify(own), c2, gap=0.05, relax=True))
    ((_, s),) = escape_strips(placed["U1"], {"VSYS": (0.5, 0.2)})
    r1 = box_of(placed["R1"])
    assert r1[1] < s[3] and s[1] < r1[3]  # aligned with its own VSYS pin


def test_two_pin_satellite_avoids_crossed_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """R7 (ODH L201 / TPS63000: the inductor's L1 pad sat behind its L2 pad, so the L1 track
    had to pass the L2 pad): a satellite whose pads serve two host pins pays
    `SELF_CROSS_MM` per own pad of the other net on a pad's line to its pin - an inductor
    turned radial (one pad behind the other) pays, a tangential one does not; the chosen
    layout has no crossed line."""
    from copperchat.board.placer import layout as lay

    pins = ["S1", "SW1", "GND", "SW2", "S5"]
    m = circuit(_column_ic("U1", pins, 0.5), ind("L1", "SW1", "SW2", (70.0, 60.0)))
    cls = classify(m)
    (c,) = build_clusters(m, cls, ["U1", "L1"])
    work = lay._Layout(m, cls, c, 0.05, relax=True)
    work._commit(Move("U1", 0.0, 0.0, 0.0, "top"))
    want = work._want("L1")
    assert want is not None
    host = work.placed["U1"]

    def costs() -> dict[float, float]:
        return {r: work._fit(m.part("L1"), r, host, "right", want)[0] for r in (0.0, 90.0)}

    penalty = lay.SELF_CROSS_MM
    with_penalty = costs()
    monkeypatch.setattr(lay, "SELF_CROSS_MM", 0.0)
    without = costs()
    radial, tangential = 0.0, 90.0  # the pads lie along x at rotation 0: radial on "right"
    assert with_penalty[radial] - without[radial] == pytest.approx(penalty)
    assert with_penalty[tangential] == pytest.approx(without[tangential])
    monkeypatch.undo()
    placed = _placed(m, layout_cluster(m, cls, c, gap=0.05, relax=True))
    for q in placed["L1"].pads:
        pin = next(p for p in placed["U1"].pads if p.net == q.net)
        other = next(o for o in placed["L1"].pads if o is not q)
        assert not lay._seg_hits_pad((q.x, q.y), (pin.x, pin.y), other, 0.0, 0.0), q.number
    # the helper itself
    a = pad("X", "1", "N", (0.0, 0.0), w=1.0, h=1.0)
    assert lay._seg_hits_pad((-2.0, 0.0), (2.0, 0.0), a, 0.0, 0.0)
    assert not lay._seg_hits_pad((-2.0, 2.0), (2.0, 2.0), a, 0.0, 0.0)
    assert lay._seg_hits_pad((-2.0, 2.0), (2.0, 2.0), a, 0.0, 2.0)  # shifted onto the line


def test_pad_side_follows_the_pad_axis() -> None:
    """R7 (PM #60, R2b review round 3): a SOIC row's end pin near a short edge belongs to
    its long side (the pad points out of it), an about square pad to the nearest side."""
    from copperchat.board.placer.layout import _pad_side

    box = (0.0, 0.0, 6.0, 4.0)  # a SOIC body, pins on the left / right long sides
    end_pin = pad("U1", "1", "A", (0.6, 0.4), w=1.2, h=0.4)  # long in x, 0.4 from the top
    assert _pad_side(box, end_pin) == "left"  # nearest edge would be "top"
    square = pad("U1", "2", "B", (0.6, 0.4), w=0.5, h=0.5)
    assert _pad_side(box, square) == "top"
    tall = pad("U1", "3", "C", (3.0, 3.6), w=0.4, h=1.2)
    assert _pad_side(box, tall) == "bottom"
