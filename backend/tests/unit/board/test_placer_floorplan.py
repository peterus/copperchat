"""R2 placer: global block floorplan, `place_blocks`, pair alignment (B-C11 round 2)."""

from __future__ import annotations

import itertools
import random
from collections.abc import Mapping, Sequence
from dataclasses import replace

import pytest

from copperchat.board.boardmd import BlockRow, BoardDoc, EdgeRow
from copperchat.board.classify import BlockProposal, classify
from copperchat.board.geom import rect_poly, straight_corridor
from copperchat.board.metrics import alignment_need
from copperchat.board.model import BoardModel, Part, Rect, Side
from copperchat.board.placer import DoesNotFit, Move, PlaceResult, apply_moves
from copperchat.board.placer.floorplan import (
    CHANNEL_MAX_MM,
    CHANNEL_MIN_MM,
    SPREAD_SPAN_MIN,
    Floorplan,
    align_pairs,
    floorplan,
    parse_region,
    place_blocks,
)
from copperchat.board.placer.layout import box_of
from copperchat.board.placer.legalize import edge_margin

from .builders import outline, part
from .builders_circuit import cap, circuit, conn, ic, pad, res

TOP: frozenset[Side] = frozenset({"top"})


def blob(ref: str, w: float, h: float, nets: Sequence[str], at: tuple[float, float]) -> Part:
    """A part with a w x h courtyard and one pad per net (a row inside the courtyard)."""
    x, y = at
    pads = tuple(
        pad(ref, str(i + 1), n, (x - w / 2 + 0.5 + i * min(0.5, (w - 1) / max(len(nets), 1)), y))
        for i, n in enumerate(nets)
    )
    return part(ref, x, y, pads=pads, courtyard=tuple(rect_poly(x - w / 2, y - h / 2, w, h)))


def _blocks(groups: Mapping[str, Sequence[str]]) -> list[BlockProposal]:
    return [BlockProposal(n, refs[0], tuple(refs), "/") for n, refs in groups.items()]


def _plan(
    m: BoardModel,
    groups: Mapping[str, Sequence[str]],
    doc: BoardDoc | None = None,
    keep: float = 0.5,
) -> Floorplan | DoesNotFit:
    d = doc or BoardDoc()
    return floorplan(m, classify(m, d), d, _blocks(groups), keep_channels=keep, allowed_sides=TOP)


def _ok(fp: Floorplan | DoesNotFit) -> Floorplan:
    assert isinstance(fp, Floorplan), fp
    return fp


def _usable(m: BoardModel) -> tuple[float, float, float, float]:
    o, e = m.outline, edge_margin(m)
    return (o.x + e, o.y + e, o.x + o.w - e, o.y + o.h - e)


def _disjoint(regions: Sequence[Rect]) -> bool:
    """Pairwise disjoint (regions are rounded to 1e-6 mm)."""
    for a, b in itertools.combinations(regions, 2):
        x_hit = a.x < b.x + b.w - 1e-5 and b.x < a.x + a.w - 1e-5
        if x_hit and a.y < b.y + b.h - 1e-5 and b.y < a.y + a.h - 1e-5:
            return False
    return True


def _inside(r: Rect, u: tuple[float, float, float, float]) -> bool:
    return (
        r.x >= u[0] - 1e-6
        and r.y >= u[1] - 1e-6
        and r.x + r.w <= u[2] + 1e-6
        and r.y + r.h <= u[3] + 1e-6
    )


def _six(w: float = 80.0, h: float = 60.0) -> tuple[BoardModel, dict[str, list[str]]]:
    parts = [blob(f"U{i}", 10, 8, [f"N{i}a", f"N{i}b"], (150 + 15 * i, 20)) for i in range(1, 7)]
    return circuit(*parts, outline=outline(0, 0, w, h)), {f"b{i}": [f"U{i}"] for i in range(1, 7)}


# ---- Task 1: floorplan ------------------------------------------------------------------------


def test_floorplan_uses_whole_board() -> None:
    m, groups = _six()
    fp = _ok(_plan(m, groups))
    regs = list(fp.regions.values())
    assert sorted(fp.regions) == sorted(groups)
    assert _disjoint(regs)
    u = _usable(m)
    assert all(_inside(r, u) for r in regs)
    x0 = min(r.x for r in regs)
    x1 = max(r.x + r.w for r in regs)
    y0 = min(r.y for r in regs)
    y1 = max(r.y + r.h for r in regs)
    assert (x1 - x0) / (u[2] - u[0]) >= 0.9
    assert (y1 - y0) / (u[3] - u[1]) >= 0.9
    assert 0 < fp.courtyard_fill < 1
    assert fp.courtyard_mm2 == pytest.approx(6 * 80.0)


def test_floorplan_edge_block_strip() -> None:
    j = conn("J1", {str(i): f"S{i}" for i in range(1, 6)}, (150.0, 50.0))
    u = blob("U1", 12, 10, ["S1", "S2"], (170, 50))
    m = circuit(j, u, outline=outline(0, 0, 80, 60))
    doc = BoardDoc(edges=(EdgeRow("J1", "left", "left", "USB"),))
    fp = _ok(_plan(m, {"conn": ["J1"], "mcu": ["U1"]}, doc))
    r = fp.regions["conn"]
    usable = _usable(m)
    assert r.x == pytest.approx(usable[0])
    assert r.w < 15.0  # a strip, not half the board
    assert r.y + r.h / 2 == pytest.approx((usable[1] + usable[3]) / 2, abs=0.5)  # edge middle
    assert fp.fixed == ("conn",)
    # the free block takes the rest, right of the strip + channel
    assert fp.regions["mcu"].x >= r.x + r.w + CHANNEL_MIN_MM - 1e-6
    # a position in the row: mm along the edge from its top end
    doc2 = BoardDoc(edges=(EdgeRow("J1", "left", "left, 15 mm", "USB"),))
    r2 = _ok(_plan(m, {"conn": ["J1"], "mcu": ["U1"]}, doc2)).regions["conn"]
    assert r2.y + r2.h / 2 == pytest.approx(usable[1] + 15.0, abs=0.5)


def test_floorplan_region_row_wins() -> None:
    m, groups = _six()
    doc = BoardDoc(
        blocks=(BlockRow("b3", ("U3",), "0, 0, 30, 20", ""),),
        edges=(EdgeRow("U3", "right", "right", ""),),
    )
    fp = _ok(_plan(m, groups, doc))
    u = _usable(m)
    assert fp.regions["b3"] == Rect(u[0], u[1], 30.0 - u[0], 20.0 - u[1])
    assert "b3" in fp.fixed
    others = [r for n, r in fp.regions.items() if n != "b3"]
    assert _disjoint([*others, fp.regions["b3"]])


def test_parse_region() -> None:
    u = (0.0, 0.0, 80.0, 60.0)
    assert parse_region("10, 5, 20, 30", u) == Rect(10, 5, 20, 30)
    assert parse_region("left half", u) == Rect(0, 0, 40, 60)
    assert parse_region("top-right", u) == Rect(40, 0, 40, 30)
    assert parse_region("bottom", u) == Rect(0, 30, 80, 30)
    assert parse_region("near J1", u) is None
    assert parse_region("", u) is None
    assert parse_region("1, 2, 0, 4", u) is None
    assert parse_region("left right", u) is None


def test_floorplan_connected_blocks_adjacent() -> None:
    shared = [f"S{i}" for i in range(10)]
    parts = [
        blob("U1", 10, 8, shared, (150, 20)),
        blob("U2", 10, 8, shared, (170, 20)),
        blob("U3", 10, 8, ["X1", "X2"], (190, 20)),
        blob("U4", 10, 8, ["Y1", "Y2"], (210, 20)),
    ]
    m = circuit(*parts, outline=outline(0, 0, 80, 60))
    fp = _ok(_plan(m, {"a": ["U1"], "b": ["U2"], "c": ["U3"], "d": ["U4"]}))
    a, b = fp.regions["a"], fp.regions["b"]
    # siblings: they share one side (apart only by the channel between them)
    gap_x = max(b.x - (a.x + a.w), a.x - (b.x + b.w))
    gap_y = max(b.y - (a.y + a.h), a.y - (b.y + b.h))
    assert max(gap_x, gap_y) <= CHANNEL_MAX_MM + 1e-6
    # the first cut separates {a, b} from {c, d}: no shared net crosses it
    widths = sorted(w for _, _, w in fp.channels)
    assert widths[-1] == pytest.approx(min(0.3 * 10, CHANNEL_MAX_MM))


@pytest.mark.parametrize(("n", "want"), [(1, CHANNEL_MIN_MM), (8, 2.4), (20, CHANNEL_MAX_MM)])
def test_floorplan_channel_width_follows_crossings(n: int, want: float) -> None:
    shared = [f"S{i}" for i in range(n)]
    m = circuit(
        blob("U1", 12, 8, shared, (150, 20)),
        blob("U2", 12, 8, shared, (180, 20)),
        outline=outline(0, 0, 80, 60),
    )
    fp = _ok(_plan(m, {"a": ["U1"], "b": ["U2"]}))
    assert [w for _, _, w in fp.channels] == [pytest.approx(want)]
    a, b = fp.regions["a"], fp.regions["b"]
    assert b.x - (a.x + a.w) == pytest.approx(want)  # the cut runs across the longer side


def test_floorplan_channel_never_below_keep_channels() -> None:
    m = circuit(
        blob("U1", 12, 8, ["S1"], (150, 20)),
        blob("U2", 12, 8, ["S1"], (180, 20)),
        outline=outline(0, 0, 80, 60),
    )
    fp = _ok(_plan(m, {"a": ["U1"], "b": ["U2"]}, keep=2.0))
    assert [w for _, _, w in fp.channels] == [pytest.approx(2.0)]


def test_floorplan_does_not_fit_reports_fill() -> None:
    # Σ block boxes > PACK_MAX x usable: refused before any cut, every member named
    m, groups = _six(28.0, 22.0)
    fp = _plan(m, groups)
    assert isinstance(fp, DoesNotFit)
    assert fp.courtyard_fill == pytest.approx(480.0 / ((28 - 0.6) * (22 - 0.6)), abs=1e-3)
    assert set(fp.refs) == {f"U{i}" for i in range(1, 7)}
    assert fp.needed_mm2 > 0.85 * fp.available_mm2
    # enough area, but the boxes do not tile the shape: the blocks of the failing cut
    m, groups = _six(30.0, 25.0)
    fp = _plan(m, groups)
    assert isinstance(fp, DoesNotFit)
    assert fp.courtyard_fill is not None and fp.courtyard_fill > 0.5
    assert fp.refs and set(fp.refs) <= {f"U{i}" for i in range(1, 7)}


def test_floorplan_edge_conflict() -> None:
    a = blob("J1", 8, 5, ["S1"], (150, 20))
    b = blob("J2", 8, 5, ["S2"], (170, 20))
    m = circuit(a, b, outline=outline(0, 0, 80, 60))
    # two blocks on one edge at the same explicit position
    doc = BoardDoc(edges=(EdgeRow("J1", "top", "top 20", ""), EdgeRow("J2", "top", "top 22", "")))
    fp = _plan(m, {"a": ["J1"], "b": ["J2"]}, doc)
    assert isinstance(fp, DoesNotFit)
    assert set(fp.refs) == {"J1", "J2"}
    assert fp.courtyard_fill is not None
    # without positions the second one slides along the edge instead
    doc = BoardDoc(edges=(EdgeRow("J1", "top", "top", ""), EdgeRow("J2", "top", "top", "")))
    ok = _ok(_plan(m, {"a": ["J1"], "b": ["J2"]}, doc))
    assert _disjoint(list(ok.regions.values()))
    assert ok.regions["a"].y == ok.regions["b"].y
    # an edge block longer than its edge (and than the board is deep)
    long_ = blob("J3", 70, 4, ["S3"], (150, 40))
    m2 = circuit(long_, outline=outline(0, 0, 60, 60))
    doc = BoardDoc(edges=(EdgeRow("J3", "top", "top", ""),))
    fp = _plan(m2, {"c": ["J3"]}, doc)
    assert isinstance(fp, DoesNotFit)
    assert fp.refs == ("J3",)


def test_floorplan_single_block() -> None:
    # one free block: one region = the whole usable area minus the edge channel
    m = circuit(blob("U1", 10, 8, ["S1"], (150, 20)), outline=outline(0, 0, 80, 60))
    fp = _ok(_plan(m, {"a": ["U1"]}))
    u = _usable(m)
    e = CHANNEL_MIN_MM
    r = fp.regions["a"]
    assert list(fp.regions) == ["a"]
    assert (r.x, r.y, r.w, r.h) == pytest.approx(
        (u[0] + e, u[1] + e, u[2] - u[0] - 2 * e, u[3] - u[1] - 2 * e)
    )
    assert fp.channels == ()


def test_floorplan_rebalance_then_fail() -> None:
    # a long thin block (45 x 5) and a square one: the area ratio gives the thin one too
    # little width; the cut moves once toward its need
    o = outline(0, 0, 80, 30)
    m = circuit(
        blob("U1", 44.5, 4.5, ["S1"], (150, 20)),
        blob("U2", 19.5, 19.5, ["S2"], (200, 20)),
        outline=o,
    )
    fp = _ok(_plan(m, {"a": ["U1"], "b": ["U2"]}, keep=0.0))
    assert fp.regions["a"].w >= 45.0 - 1e-6
    assert _disjoint(list(fp.regions.values()))
    # 60 + 25 + channel along x and 5 + 25 + channel along y both exceed the board
    m2 = circuit(
        blob("U1", 59.5, 4.5, ["S1"], (150, 20)),
        blob("U2", 24.5, 24.5, ["S2"], (220, 20)),
        outline=o,
    )
    bad = _plan(m2, {"a": ["U1"], "b": ["U2"]}, keep=0.0)
    assert isinstance(bad, DoesNotFit)
    assert bad.courtyard_fill is not None


def test_floorplan_deterministic() -> None:
    m, groups = _six()
    assert _plan(m, groups) == _plan(m, groups)
    assert _plan(m, dict(reversed(list(groups.items())))) == _plan(m, groups)


def test_floorplan_many_blocks_greedy() -> None:
    parts = [blob(f"U{i}", 6, 5, [f"N{i}", f"N{i + 1}"], (150 + 8 * i, 20)) for i in range(1, 17)]
    m = circuit(*parts, outline=outline(0, 0, 80, 60))
    fp = _ok(_plan(m, {f"b{i}": [f"U{i}"] for i in range(1, 17)}))
    assert len(fp.regions) == 16
    assert _disjoint(list(fp.regions.values()))


def test_floorplan_property_legal() -> None:
    rng = random.Random(20261127)
    for case in range(200):
        w, h = rng.uniform(30, 120), rng.uniform(25, 100)
        n = rng.randint(1, 9)
        nets = [f"N{k}" for k in range(12)]
        parts = []
        for i in range(n):
            bw, bh = rng.uniform(2, 25), rng.uniform(2, 25)
            parts.append(blob(f"U{i}", bw, bh, rng.sample(nets, 3), (300 + 30 * i, 50)))
        edges = []
        if n > 1 and rng.random() < 0.4:
            edges.append(EdgeRow("U0", rng.choice(("top", "bottom", "left", "right")), "", ""))
        m = circuit(*parts, outline=outline(0, 0, w, h))
        groups = {f"b{i}": [f"U{i}"] for i in range(n)}
        fp = _plan(m, groups, BoardDoc(edges=tuple(edges)))
        if isinstance(fp, DoesNotFit):
            assert fp.courtyard_fill is not None, case
            continue
        regs = list(fp.regions.values())
        assert sorted(fp.regions) == sorted(groups), case
        assert _disjoint(regs), case
        for a, b in itertools.combinations(regs, 2):  # a routing channel between any two
            gap = max(b.x - (a.x + a.w), a.x - (b.x + b.w), b.y - (a.y + a.h), a.y - (b.y + b.h))
            assert gap >= CHANNEL_MIN_MM - 1e-5, (case, a, b)
        u = _usable(m)
        assert all(_inside(r, u) for r in regs), case
        for name, r in fp.regions.items():
            bb = box_of(m.part(groups[name][0]))
            bw, bh = bb[2] - bb[0], bb[3] - bb[1]
            assert (bw <= r.w + 1e-6 and bh <= r.h + 1e-6) or (
                bh <= r.w + 1e-6 and bw <= r.h + 1e-6
            ), case


def test_floorplan_needs_outline() -> None:
    m, groups = _six()
    none = replace(m, outline=replace(m.outline, kind="none"))
    with pytest.raises(ValueError, match="outline"):
        _plan(none, groups)


# ---- Task 2: place_blocks, spread, pair alignment --------------------------------------------


def _eight() -> tuple[BoardModel, dict[str, list[str]]]:
    parts: list[Part] = []
    groups: dict[str, list[str]] = {}
    for i in range(1, 9):
        x = 150.0 + 30 * i
        pins = {str(k): f"B{i}S{k}" for k in range(1, 9)}
        pins.update({"1": "+3V3", "8": "GND"})
        if i > 1:
            pins["2"] = f"L{i - 1}"  # a chain: block i talks to block i - 1
        if i < 8:
            pins["3"] = f"L{i}"
        parts += [
            ic(f"U{i}", pins, (x, 10.0)),
            cap(f"C{i}", "+3V3", "GND", (x, 20.0), value="100nF"),
            res(f"R{i}", f"B{i}S4", f"B{i}S5", (x + 4, 20.0)),
        ]
        groups[f"blk{i}"] = [f"U{i}", f"C{i}", f"R{i}"]
    return circuit(*parts, outline=outline(0, 0, 80, 60)), groups


def _place(
    m: BoardModel, groups: Mapping[str, Sequence[str]], doc: BoardDoc | None = None
) -> tuple[PlaceResult, Floorplan]:
    d = doc or BoardDoc()
    r = place_blocks(m, classify(m, d), d, _blocks(groups), keep_channels=0.5, allowed_sides=TOP)
    assert not isinstance(r, DoesNotFit), r
    return r


def test_place_blocks_legal_and_spread() -> None:
    m, groups = _eight()
    res_, fp = _place(m, groups)
    assert res_.score.m1_violations == 0
    assert res_.score.spread >= SPREAD_SPAN_MIN
    assert sorted(mv.ref for mv in res_.moves) == sorted(r for g in groups.values() for r in g)
    after = apply_moves(m, res_.moves)
    o = after.outline
    for p in after.parts:
        b = box_of(p)
        assert o.x <= b[0] and b[2] <= o.x + o.w and o.y <= b[1] and b[3] <= o.y + o.h, p.ref
    assert len(fp.regions) == 8


def test_place_blocks_deterministic() -> None:
    m, groups = _eight()
    assert _place(m, groups) == _place(m, groups)


def test_place_blocks_keeps_other_parts() -> None:
    m, groups = _eight()
    # blk8 already placed and locked on the board: it stays, the others avoid it
    m = circuit(
        *(
            p
            if not p.ref.endswith("8")
            else part(p.ref, 40 + (p.x - 390), 30 + (p.y - 10), locked=True, pads=p.pads, half=1.5)
            for p in m.parts
        ),
        outline=m.outline,
    )
    del groups["blk8"]
    res_, fp = _place(m, groups)
    assert not {mv.ref for mv in res_.moves} & {"U8", "C8", "R8"}
    assert res_.score.m1_violations == 0
    # the free regions leave the locked parts out (review round 1)
    for ref in ("U8", "C8", "R8"):
        b = box_of(m.part(ref))
        for name, r in fp.regions.items():
            hit = b[0] < r.x + r.w and r.x < b[2] and b[1] < r.y + r.h and r.y < b[3]
            assert not hit, (ref, name, r)


def test_place_blocks_rerun_from_placed_state() -> None:
    """Review round 1: a block is packed around the blocks placed before it, never around
    the old positions of members still to come - placing again from the placed state
    gives the same placement."""
    m, groups = _eight()
    first, _ = _place(m, groups)
    again, _ = _place(apply_moves(m, first.moves), groups)
    assert again.moves == first.moves


def test_place_blocks_does_not_fit() -> None:
    m, groups = _eight()
    small = circuit(*m.parts, outline=outline(0, 0, 25, 20))
    d = BoardDoc()
    r = place_blocks(
        small, classify(small, d), d, _blocks(groups), keep_channels=0.5, allowed_sides=TOP
    )
    assert isinstance(r, DoesNotFit)
    assert r.courtyard_fill is not None and r.courtyard_fill > 0


def _led_pair(dy: float, *extra: Part) -> BoardModel:
    d1 = part(
        "D1",
        20.0,
        20.0,
        lib_id="LED_SMD:LED_0603",
        pads=(pad("D1", "1", "VLED", (19.5, 20.0)), pad("D1", "2", "LA", (20.5, 20.0))),
        courtyard=tuple(rect_poly(19.0, 19.5, 2.0, 1.0)),
    )
    r1 = res("R1", "LA", "SIG", (23.0, 20.0 + dy))
    return circuit(d1, r1, *extra, outline=outline(0, 0, 60, 40))


def _moves(m: BoardModel) -> list[Move]:
    return [Move(p.ref, p.x, p.y, p.rot, p.side) for p in m.parts]


def test_align_pairs_led_resistor() -> None:
    m = _led_pair(0.5)
    a, b = m.part("D1").pads[1], m.part("R1").pads[0]
    assert straight_corridor(a, b, 0.2, copper_layers=m.copper_layers) is None
    assert alignment_need(a, b, 0.2, m.copper_layers)
    out = align_pairs(m, classify(m), _moves(m), movable=frozenset({"D1", "R1"}))
    after = apply_moves(m, out)
    a, b = after.part("D1").pads[1], after.part("R1").pads[0]
    assert straight_corridor(a, b, 0.2, copper_layers=after.copper_layers) is not None
    # centre-aligned: the later ref (R1) moved by the full offset, D1 stayed
    assert after.part("R1").y == pytest.approx(20.0)
    assert after.part("D1").y == pytest.approx(20.0)


def test_align_pairs_never_overlaps() -> None:
    # a part right below R1: neither the centre (+0.5) nor the least shift (+0.1) is free
    # for R1; D1 moves up instead
    blocker = part("X1", 23.0, 20.65, half=0.6)
    m = _led_pair(-0.5, blocker)
    out = align_pairs(m, classify(m), _moves(m), movable=frozenset({"D1", "R1"}))
    after = apply_moves(m, out)
    assert after.part("R1").y == pytest.approx(19.5)
    assert after.part("D1").y == pytest.approx(19.5)
    for x, y in itertools.combinations(after.parts, 2):
        bx, by = box_of(x), box_of(y)
        assert not (bx[0] < by[2] and by[0] < bx[2] and bx[1] < by[3] and by[1] < bx[3]), (x, y)
    # both blocked (D1 locked) → nothing moves
    locked = circuit(*(p if p.ref != "D1" else part_locked(p) for p in m.parts), outline=m.outline)
    out = align_pairs(locked, classify(locked), _moves(locked), movable=frozenset({"D1", "R1"}))
    assert out == tuple(sorted(_moves(locked), key=lambda mv: mv.ref))
    # off the board: R1 at the bottom edge cannot move down
    edge = circuit(
        part(
            "D1",
            20.0,
            39.0,
            pads=(pad("D1", "1", "VLED", (19.5, 39.0)), pad("D1", "2", "LA", (20.5, 39.0))),
            courtyard=tuple(rect_poly(19.0, 38.5, 2.0, 1.0)),
        ),
        res("R1", "LA", "SIG", (23.0, 39.0 - 0.5)),
        outline=outline(0, 0, 60, 39.3),  # inner bottom 39.0 = R1's courtyard bottom
    )
    out = align_pairs(edge, classify(edge), _moves(edge), movable=frozenset({"R1"}))
    assert apply_moves(edge, out).part("R1").y == pytest.approx(38.5)


def part_locked(p: Part) -> Part:
    return replace(p, locked=True)


def test_align_pairs_skips_anchor_and_far_pairs() -> None:
    pins = {str(i): f"S{i}" for i in range(1, 9)}
    u = ic("U1", pins, (10.0, 10.0))
    # R1 serves U1 pin 1 (at (10, 10)) from 2 mm below, 0.45 mm off: R1 moves, never U1
    r1 = part(
        "R1",
        10.45,
        12.5,
        pads=(pad("R1", "1", "S1", (10.45, 12.0)), pad("R1", "2", "Z", (10.45, 13.0))),
        courtyard=tuple(rect_poly(9.95, 11.5, 1.0, 2.0)),
    )
    # R2 is 3 mm off pin 8: more than M16_align_mm, left alone
    r2 = part(
        "R2",
        20.0,
        12.5,
        pads=(pad("R2", "1", "S8", (20.0, 12.0)), pad("R2", "2", "Z2", (20.0, 13.0))),
        courtyard=tuple(rect_poly(19.5, 11.5, 1.0, 2.0)),
    )
    m = circuit(u, r1, r2, outline=outline(0, 0, 40, 30))
    out = align_pairs(m, classify(m), _moves(m), movable=frozenset({"U1", "R1", "R2"}))
    after = apply_moves(m, out)
    assert after.part("U1").x == pytest.approx(10.0)
    assert after.part("R1").x == pytest.approx(10.0)
    assert after.part("R2").x == pytest.approx(20.0)


def test_align_pairs_tightens_corridor() -> None:
    # aligned, 2.0 mm between the facing pads: R1 moves toward D1 until the courtyards
    # meet (2 x 1 mm boxes: the pads end 0.4 mm apart); a part between them stops it there
    m = _led_pair(0.0)
    m = replace(
        m,
        parts=tuple(p if p.ref != "R1" else res("R1", "LA", "SIG", (24.0, 20.0)) for p in m.parts),
    )
    out = align_pairs(m, classify(m), _moves(m), movable=frozenset({"R1"}))
    after = apply_moves(m, out)
    a, b = after.part("D1").pads[1], after.part("R1").pads[0]
    assert (b.x - b.w / 2) - (a.x + a.w / 2) == pytest.approx(0.4)  # 1.0 mm courtyards touch
    assert straight_corridor(a, b, 0.2, copper_layers=after.copper_layers) is not None
    for x, y in itertools.combinations(after.parts, 2):
        bx, by = box_of(x), box_of(y)
        assert not (
            bx[0] < by[2] - 1e-9 and by[0] < bx[2] - 1e-9 and bx[1] < by[3] and by[1] < bx[3]
        )
    blocked = replace(m, parts=(*m.parts, part("X1", 22.0, 20.0, half=0.3)))
    out = align_pairs(blocked, classify(blocked), _moves(blocked), movable=frozenset({"R1"}))
    assert apply_moves(blocked, out).part("R1").x == pytest.approx(23.3)  # touches X1 (22.3)


def test_assign_side_piece_only_what_tiles() -> None:
    """ODH 80 x 100 (R2 probe): three small blocks next to the USB strip fit the 30 x 17 mm
    side piece by area but not by shape; only what tiles it goes there, the rest stays in
    the big rectangle (else the floorplan failed with 54 % box fill)."""
    from copperchat.board.placer import floorplan as fpm

    sizes = {"a": (16.86, 11.28), "b": (9.55, 6.73), "c": (9.05, 9.61), "big": (30.0, 20.0)}
    boxes = {n: fpm.BlockBox(n, (n.upper(),), w, h, None, None) for n, (w, h) in sizes.items()}
    boxes["usb"] = fpm.BlockBox("usb", ("J1",), 12.6, 18.2, "bottom", None)
    weight = {("a", "usb"): 2, ("b", "usb"): 3, ("c", "usb"): 1}
    nets = {n: frozenset({f"N{n}"}) for n in boxes}
    ctx = fpm._Ctx(boxes=boxes, weight=weight, nets=nets, where={}, keep=1.5)
    free = [(101.8, 101.8, 178.2, 180.0), (101.8, 181.5, 132.2, 198.2)]
    fixed = {"usb": (133.7, 181.5, 146.3, 199.7)}
    groups = fpm._assign(ctx, free, ["a", "b", "big", "c"], fixed)
    assert sorted(groups[0] + groups[1]) == ["a", "b", "big", "c"]
    assert groups[1] and fpm._tiles(ctx, free[1], groups[1])
    assert "big" in groups[0]
