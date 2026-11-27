"""R2b: routing room of a placement - pitch, channel, RUDY congestion, largest hole, block
gaps (`cc/board/placer/routability.py`)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from copperchat.board.boardmd import BoardDoc
from copperchat.board.classify import BlockProposal, classify
from copperchat.board.placer.routability import (
    CHANNEL_BASE_MM,
    CHANNEL_MAX_MM,
    DEFAULT_PITCH_MM,
    SUPPLY_WEIGHT,
    Congestion,
    block_gaps,
    channel_mm,
    congestion,
    gap_summary,
    largest_hole,
    net_pitch,
    net_weight,
    signal_pitch,
)

from .builders import net_class, outline
from .builders_circuit import circuit, ic, res


def test_net_pitch_from_class() -> None:
    m = circuit(
        res("R1", "A", "VBUS", (10, 10)),
        net_classes=(net_class(), net_class("Power", 0.5, 0.25)),
        net_class_of=(("VBUS", "Power"),),
    )
    assert net_pitch(m, "VBUS") == pytest.approx(0.75)
    assert net_pitch(m, "A") == pytest.approx(0.4)  # Default
    assert signal_pitch(m) == pytest.approx(0.4)
    bare = circuit(res("R1", "A", "B", (10, 10)), net_classes=(), net_class_of=())
    assert net_pitch(bare, "A") == DEFAULT_PITCH_MM
    assert signal_pitch(bare) == DEFAULT_PITCH_MM


def test_net_weight_by_kind() -> None:
    m = circuit(
        ic("U1", {"1": "+3V3", "2": "GND", "3": "SIG"}, (20, 20)),
        res("R1", "SIG", "+3V3", (30, 20)),
        outline=outline(0, 0, 50, 40),
    )
    cls = classify(m, BoardDoc())
    assert net_weight(cls, "SIG") == 1.0
    assert net_weight(cls, "GND") == 0.0
    assert net_weight(cls, "+3V3") == SUPPLY_WEIGHT


@pytest.mark.parametrize(
    ("shared", "keep", "want"),
    [
        (0, 0.5, CHANNEL_BASE_MM),
        (1, 0.5, CHANNEL_BASE_MM + 0.4),
        (2, 0.5, CHANNEL_BASE_MM + 0.4),
        (3, 0.5, CHANNEL_BASE_MM + 0.8),
        (0, 1.5, 1.5),  # never below keep_channels
        (40, 0.5, CHANNEL_MAX_MM),
    ],
)
def test_channel_clamped(shared: float, keep: float, want: float) -> None:
    assert channel_mm(shared, 0.4, keep) == pytest.approx(want)


def _rows(crossed: bool) -> tuple[object, ...]:
    """Two 8-pin ICs 6 mm apart, 8 nets - straight across, or the second row reversed (every
    connection's box spans the whole row: all of them pile up in the same cells)."""
    a = ic("U1", {str(i + 1): f"N{i}" for i in range(8)}, (10, 20), pitch=0.5)
    order = range(7, -1, -1) if crossed else range(8)
    b = ic("U2", {str(k + 1): f"N{i}" for k, i in enumerate(order)}, (10, 26), pitch=0.5)
    return a, b


def test_congestion_crossed_beats_straight() -> None:
    crossed = circuit(*_rows(True), outline=outline(0, 0, 40, 40))
    straight = circuit(*_rows(False), outline=outline(0, 0, 40, 40))
    cc = congestion(crossed, classify(crossed, BoardDoc()))
    cs = congestion(straight, classify(straight, BoardDoc()))
    assert cc.worst > cs.worst > 0.0
    assert cs.cells >= 1 and 0.0 < cs.p90 <= cs.worst


def test_congestion_empty_and_full_cells() -> None:
    empty = circuit(outline=outline(0, 0, 20, 20))
    assert congestion(empty, classify(empty, BoardDoc())) == Congestion(0.0, 0.0, 0, 0)
    # a cell entirely under pads: capacity floor (no division by zero), finite ratio
    big = ic("U1", {"1": "A", "2": "B"}, (5, 5), pitch=2.0)
    big = replace(big, pads=tuple(replace(q, w=3.0, h=3.0) for q in big.pads))
    m = circuit(big, res("R1", "A", "B", (5, 9)), outline=outline(0, 0, 20, 20))
    c = congestion(m, classify(m, BoardDoc()))
    assert c.worst < float("inf") and c.cells >= 1


def test_largest_hole_finds_gap() -> None:
    m = circuit(
        res("R1", "A", "B", (5, 10)),
        res("R2", "A", "B", (30, 10)),
        outline=outline(0, 0, 40, 20),
    )
    hole = largest_hole(m, classify(m, BoardDoc()))
    # the courtyards span x 4..31 (2 x 1 mm each); between them ≈ 23 mm minus the 0.5 mm
    # growth on both sides, 1 mm tall (the union bbox is one courtyard tall)
    assert hole == pytest.approx(22.0, abs=1.0)


def test_largest_hole_empty_board() -> None:
    m = circuit(outline=outline(0, 0, 20, 20))
    assert largest_hole(m, classify(m, BoardDoc())) == 0.0
    no_outline = circuit(res("R1", "A", "B", (5, 5)))
    assert largest_hole(no_outline, classify(no_outline, BoardDoc())) == 0.0


def test_block_gaps_rows() -> None:
    m = circuit(
        res("R1", "S1", "S2", (5, 10)),
        res("R2", "S1", "S2", (15, 10)),
        res("R3", "X", "Y", (30, 10)),
        outline=outline(0, 0, 40, 20),
    )
    cls = classify(m, BoardDoc())
    blocks = [
        BlockProposal("a", "R1", ("R1",), "/"),
        BlockProposal("b", "R2", ("R2",), "/"),
        BlockProposal("c", "R3", ("R3",), "/"),
    ]
    rows = block_gaps(m, cls, blocks, keep=0.5)
    assert [(r.a, r.b) for r in rows] == [("a", "b")]  # c shares nothing
    r = rows[0]
    assert r.shared == 2.0
    assert r.gap == pytest.approx(8.0)  # courtyards 4..6 and 14..16
    assert r.required == pytest.approx(channel_mm(2.0, 0.4, 0.5))
    s = gap_summary(rows)
    assert s["worst"] == pytest.approx(r.excess) and s["mean"] == pytest.approx(r.excess)
    assert gap_summary(()) == {"worst": 0.0, "mean": 0.0}


def test_gap_summary_worst_only_signal_pairs() -> None:
    from copperchat.board.placer.routability import GapRow

    rows = (GapRow("a", "b", 2.0, 3.0, 1.5), GapRow("a", "c", 0.25, 40.0, 1.0))
    s = gap_summary(rows)
    assert s["worst"] == pytest.approx(1.5)  # a-c share only a supply net
    assert s["mean"] == pytest.approx((1.5 * 2.0 + 39.0 * 0.25) / 2.25, abs=1e-3)
