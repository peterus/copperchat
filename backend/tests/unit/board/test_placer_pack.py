"""P4 placer: block packing into a region (B-C11 `place_block`)."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from typing import Any

import pytest

from copperchat.board.boardmd import BoardDoc, EdgeRow
from copperchat.board.classify import classify
from copperchat.board.model import BoardModel, Part, Rect, Side
from copperchat.board.placer import DoesNotFit, Move, PlaceResult, apply_moves, place_block
from copperchat.board.placer.layout import box_of

from .builders import part
from .builders_circuit import cap, circuit, conn, ic, res

TOP: frozenset[Side] = frozenset({"top"})
BOTH: frozenset[Side] = frozenset({"top", "bottom"})
WHOLE = Rect(0.0, 0.0, 100.0, 80.0)  # builders' default outline


def _mcu_block() -> list[Part]:
    """An MCU with decoupling and a few resistors, all waiting right of the outline."""
    pins = {str(i): f"S{i}" for i in range(1, 11)}
    pins.update({"1": "+3V3", "5": "GND", "10": "+3V3"})
    return [
        ic("U1", pins, (110.0, 10.0)),
        cap("C1", "+3V3", "GND", (110.0, 20.0), value="100nF"),
        cap("C2", "+3V3", "GND", (113.0, 20.0), value="100nF"),
        res("R1", "S2", "LED", (116.0, 20.0)),
        res("R2", "S3", "+3V3", (119.0, 20.0)),
        res("R3", "S7", "OUT", (122.0, 20.0)),
    ]


def _refs(parts: Sequence[Part]) -> list[str]:
    return [p.ref for p in parts]


def _place(m: BoardModel, refs: Sequence[str], region: Rect = WHOLE, **kw: Any) -> PlaceResult:
    args: dict[str, Any] = {"side": "top", "rotation": None, "allowed_sides": TOP}
    args.update(kw)
    r = place_block(m, classify(m), refs, region, **args)
    assert isinstance(r, PlaceResult), r
    return r


def _gap(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return max(b[0] - a[2], a[0] - b[2], b[1] - a[3], a[1] - b[3])


def _boxes(m: BoardModel, refs: Sequence[str]) -> dict[str, tuple[float, ...]]:
    return {r: box_of(m.part(r)) for r in refs}


def test_pack_into_region() -> None:
    parts = _mcu_block()
    m = circuit(*parts)
    region = Rect(10.0, 10.0, 40.0, 30.0)
    r = _place(m, _refs(parts), region)
    assert sorted(mv.ref for mv in r.moves) == sorted(_refs(parts))
    after = apply_moves(m, r.moves)
    boxes = _boxes(after, _refs(parts))
    for ref, b in boxes.items():
        assert region.x - 1e-6 <= b[0] and b[2] <= region.x + region.w + 1e-6, ref
        assert region.y - 1e-6 <= b[1] and b[3] <= region.y + region.h + 1e-6, ref
    for (ra, a), (rb, b) in itertools.combinations(boxes.items(), 2):
        assert _gap(a, b) >= 0.0 - 1e-9, (ra, rb)
    assert r.score.m1_violations == 0


def test_rotation_fixed() -> None:
    parts = _mcu_block()
    m = circuit(*parts)
    r = _place(m, _refs(parts), rotation=90)
    anchor = next(mv for mv in r.moves if mv.ref == "U1")
    assert anchor.rot == 90.0


def _with_connector() -> list[Part]:
    jpins = {"1": "GND", "2": "S2", "3": "S3", "4": "S4", "5": "+3V3", "6": "GND"}
    return [*_mcu_block(), conn("J1", jpins, (110.0, 40.0))]


def _connector_block() -> list[Part]:
    """A connector block: J1 with series resistors on two of its pins."""
    jpins = {"1": "GND", "2": "DA", "3": "DB", "4": "S4", "5": "+3V3", "6": "GND"}
    return [
        conn("J1", jpins, (110.0, 40.0)),
        res("R11", "DA", "S2", (110.0, 45.0)),
        res("R12", "DB", "S3", (113.0, 45.0)),
    ]


@pytest.mark.parametrize("how", ["board_md", "edge_kwarg"])
def test_connector_pulled_to_edge(how: str) -> None:
    jb = _connector_block()
    m = circuit(*jb)
    cls = classify(m)
    assert cls.part_class["J1"] == "connector"
    kw: dict[str, Any] = {}
    if how == "board_md":
        kw["doc"] = BoardDoc(edges=(EdgeRow("J1", "left", "", "user plugs in here"),))
    else:
        kw["edge"] = "left"
    r = _place(m, _refs(jb), **kw)
    after = apply_moves(m, r.moves)
    assert box_of(after.part("J1"))[0] - 0.0 <= 0.5


def test_connector_edge_strip_next_to_placed_block() -> None:
    # as pcb_place_block's edge form does it: a strip along the left edge as the region;
    # the MCU block already sits in the middle and pulls the connector's airwires
    mcu = _mcu_block()
    placed = _place(circuit(*mcu), _refs(mcu), Rect(40.0, 30.0, 30.0, 25.0))
    jb = _connector_block()
    m = apply_moves(circuit(*mcu, *jb), placed.moves)
    r = _place(m, _refs(jb), Rect(0.0, 0.0, 15.0, 80.0), edge="left")
    after = apply_moves(m, r.moves)
    assert box_of(after.part("J1"))[0] - 0.0 <= 0.5


def test_keep_channels_gap() -> None:
    parts = _mcu_block()
    # an already placed part on the board sharing nets with the block: the block is pulled
    # towards it but keeps the channel
    other = res("R9", "S2", "S7", (30.0, 30.0))
    m = circuit(*parts, other)
    r = _place(m, _refs(parts), Rect(20.0, 20.0, 30.0, 25.0), keep_channels=0.5)
    after = apply_moves(m, r.moves)
    b9 = box_of(after.part("R9"))
    assert box_of(after.part("R9")) == box_of(other)
    gaps = [_gap(box_of(after.part(ref)), b9) for ref in _refs(parts)]
    assert min(gaps) >= 0.5 - 1e-6
    assert min(gaps) < 3.0  # actually next to it, not trivially far away


def test_does_not_fit_reports_area() -> None:
    # ten 2 x 2 mm parts = 40 mm2 of courtyard
    parts = [part(f"R{i}", 110.0 + 3 * i, 10.0, half=1.0) for i in range(1, 11)]
    m = circuit(*parts)
    r = place_block(
        m,
        classify(m),
        _refs(parts),
        Rect(10.0, 10.0, 5.0, 5.0),
        side="top",
        rotation=None,
        allowed_sides=TOP,
    )
    assert isinstance(r, DoesNotFit)
    assert r.needed_mm2 >= 40.0
    assert r.available_mm2 <= 25.0
    assert set(r.refs) == set(_refs(parts))


def test_fixed_part_respected() -> None:
    parts = _mcu_block()
    m = circuit(*parts, res("R9", "S2", "S7", (130.0, 40.0)))
    fixed = Move("R9", 30.0, 30.0, 90.0, "top")
    r = _place(m, [*_refs(parts), "R9"], Rect(20.0, 20.0, 30.0, 25.0), fixed=[fixed])
    assert r.moves[0] == fixed
    assert [mv.ref for mv in r.moves].count("R9") == 1
    after = apply_moves(m, r.moves)
    b9 = box_of(after.part("R9"))
    assert abs((b9[0] + b9[2]) / 2 - 30.0) < 1e-6 and abs((b9[1] + b9[3]) / 2 - 30.0) < 1e-6
    for ref in _refs(parts):
        assert _gap(box_of(after.part(ref)), b9) >= 0.5 - 1e-6, ref


def test_deterministic() -> None:
    parts = _with_connector()
    m = circuit(*parts)
    a = _place(m, _refs(parts))
    b = _place(m, list(reversed(_refs(parts))))
    c = _place(circuit(*reversed(parts)), _refs(parts))
    assert a.moves == b.moves == c.moves
    assert a.score == b.score


def test_bottom_side_refused() -> None:
    parts = _mcu_block()
    m = circuit(*parts)
    with pytest.raises(ValueError):
        place_block(
            m,
            classify(m),
            _refs(parts),
            WHOLE,
            side="bottom",
            rotation=None,
            allowed_sides=TOP,
        )
    # allowed when board.md allows the bottom side
    r = place_block(
        m,
        classify(m),
        _refs(parts),
        WHOLE,
        side="bottom",
        rotation=None,
        allowed_sides=BOTH,
    )
    assert isinstance(r, PlaceResult)
    assert all(mv.side == "bottom" for mv in r.moves)
