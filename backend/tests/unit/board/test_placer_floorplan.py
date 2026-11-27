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
    Floorplan,
    align_pairs,
    floorplan,
    parse_region,
    place_blocks,
)
from copperchat.board.placer.layout import box_of
from copperchat.board.placer.legalize import edge_margin
from copperchat.board.placer.pack import PLACE_GRID_MM
from copperchat.board.placer.routability import channel_mm

from .builders import outline, part
from .builders_circuit import cap, circuit, conn, ic, pad, res, xtal

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
    # measured from the outline's top end, like pcb_place_block's offset
    assert r2.y + r2.h / 2 == pytest.approx(m.outline.y + 15.0, abs=0.5)


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
    # available = what the blocks may use (PACK_MAX of the usable area): an area refusal
    assert fp.needed_mm2 > fp.available_mm2
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


def test_place_blocks_legal_and_compact() -> None:
    """R2b: legal, every block placed, the chain's neighbours next to each other (their
    gap at most the channel + slack + the grid / alignment shift); `spread` is reported, no
    longer a target (owner 2026-11-27: blocks too far apart)."""
    m, groups = _eight()
    res_, fp = _place(m, groups)
    assert res_.score.m1_violations == 0
    assert 0.0 < res_.score.spread <= 1.0
    assert fp.relaxed and fp.order[0] == "blk1"
    chain = {(f"blk{i}", f"blk{i + 1}") for i in range(1, 8)}
    rows = {(r.a, r.b): r for r in fp.gaps}
    assert chain <= set(rows)
    for k in chain:
        assert rows[k].gap <= rows[k].required + fp.slack_mm + 1.0, rows[k]
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


def test_edge_offset_parse() -> None:
    from copperchat.board.placer.floorplan import _edge_offset

    assert _edge_offset("left, 15 mm") == 15.0
    assert _edge_offset("right: 7.5mm") == 7.5
    assert _edge_offset("left") is None
    assert _edge_offset("bottom, 10 mm from right") is None  # not from the top/left end
    assert _edge_offset("USB 2.0 connector") is None
    # review round 3: the supported reference spelled out, comments, bad numbers
    assert _edge_offset("left, 15 mm from top") == 15.0
    assert _edge_offset("bottom, 10 mm from left") == 10.0
    assert _edge_offset("top, 12 mm from the left end") == 12.0
    assert _edge_offset("left, 15 mm (USB)") == 15.0
    assert _edge_offset("left 15 from left") is None  # wrong end for a left edge
    assert _edge_offset("left, -5 mm") is None
    assert _edge_offset("top, 2 x USB") is None


def test_floorplan_area_refusal_in_pack_band() -> None:
    """Σ boxes between PACK_MAX and 100 % of the usable area is an area refusal: needed >
    available (= PACK_MAX x usable), so the tool says "area", not "do not tile"."""
    m, groups = _six(30.0, 23.0)
    fp = _plan(m, groups)
    assert isinstance(fp, DoesNotFit)
    u = _usable(m)
    usable = (u[2] - u[0]) * (u[3] - u[1])
    assert fp.needed_mm2 <= usable  # inside the 85-100 % band
    assert fp.needed_mm2 > fp.available_mm2 == pytest.approx(0.85 * usable, abs=1e-2)


def test_region_row_too_small_or_overlapping() -> None:
    m, groups = _six()
    small = BoardDoc(blocks=(BlockRow("b1", ("U1",), "10, 10, 5, 5", ""),))
    bad = _plan(m, groups, small)
    assert isinstance(bad, DoesNotFit) and bad.refs == ("U1",)
    both = BoardDoc(
        blocks=(
            BlockRow("b1", ("U1",), "10, 10, 20, 20", ""),
            BlockRow("b2", ("U2",), "20, 20, 20, 20", ""),
        )
    )
    bad = _plan(m, groups, both)
    assert isinstance(bad, DoesNotFit) and bad.refs == ("U2",)


def test_place_blocks_falls_back_to_whole_board() -> None:
    """A Region row whose rectangle holds the block's box but whose middle is taken by a
    mounting hole (fixtures stay in the regions): the block is packed on the whole board
    instead, recorded in `fallback`, and the result is legal."""
    m, groups = _six()
    hole = part(
        "H1", 15.0, 15.0, lib_id="MountingHole:MountingHole_2.2mm_M2", half=2.5,
        pads=(pad("H1", "1", "", (15.0, 15.0)),),
    )  # fmt: skip
    m = circuit(*m.parts, hole, outline=m.outline)
    doc = BoardDoc(blocks=(BlockRow("b1", ("U1",), "5, 5, 20, 20", ""),))
    res_, fp = _place(m, groups, doc)
    assert fp.fallback == ("b1",)
    assert res_.score.m1_violations == 0


def test_align_pairs_never_tightens_a_crystal() -> None:
    from copperchat.board.placer.floorplan import align_pairs as ap

    y1 = xtal("Y1", "XI", "XO", (20.0, 20.0))
    r1 = res("R1", "XO", "SIG", (24.0, 20.0))
    m = circuit(y1, r1, outline=outline(0, 0, 60, 40))
    cls = classify(m)
    assert [x.ref for x in cls.crystals] == ["Y1"]
    out = ap(m, cls, _moves(m), movable=frozenset({"Y1", "R1"}))
    after = apply_moves(m, out)
    assert after.part("R1").x == pytest.approx(24.0)
    assert after.part("Y1").x == pytest.approx(20.0)


def test_place_blocks_switches_r2_packing_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review verify pass: place_blocks (and the floorplan's block boxes) use the R2
    escape room and occlusion term; pcb_place_block's defaults stay off (P4 layout)."""
    from copperchat.board.placer import floorplan as fpm
    from copperchat.board.placer.pack import OCCLUDE_WEIGHT

    packs: list[dict[str, object]] = []
    layouts: list[bool] = []
    real_pack, real_layout = fpm.pack_block, fpm.block_layout

    def pack_spy(*a: object, **kw: object) -> object:
        packs.append(kw)
        return real_pack(*a, **kw)  # type: ignore[arg-type]

    def layout_spy(*a: object, **kw: object) -> object:
        layouts.append(bool(kw.get("escape_room")))
        return real_layout(*a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(fpm, "pack_block", pack_spy)
    monkeypatch.setattr(fpm, "block_layout", layout_spy)
    m, groups = _eight()
    _place(m, groups)
    assert packs and all(
        kw["escape_room"] is True and kw["occlude_weight"] == OCCLUDE_WEIGHT for kw in packs
    )
    assert layouts and all(layouts)


# ---- R2b: compact, connectivity-ordered packing ---------------------------------------------


def _trio(w: float = 80.0, h: float = 60.0) -> tuple[BoardModel, dict[str, list[str]]]:
    """A and B share eight signal nets; C shares nothing with them."""
    a = blob("U1", 10, 8, [f"AB{i}" for i in range(8)] + ["A1"], (150, 20))
    b = blob("U2", 10, 8, [f"AB{i}" for i in range(8)] + ["B1"], (170, 20))
    c = blob("U3", 10, 8, ["C1", "C2"], (190, 20))
    return circuit(a, b, c, outline=outline(0, 0, w, h)), {"a": ["U1"], "b": ["U2"], "c": ["U3"]}


def test_place_blocks_connected_blocks_touch() -> None:
    m, groups = _trio()
    res_, fp = _place(m, groups)
    assert res_.score.m1_violations == 0
    after = apply_moves(m, res_.moves)
    boxes = {r: box_of(after.part(r)) for r in ("U1", "U2", "U3")}

    def gap(x: str, y: str) -> float:
        a, b = boxes[x], boxes[y]
        return max(b[0] - a[2], a[0] - b[2], b[1] - a[3], a[1] - b[3])

    (ab,) = [r for r in fp.gaps if (r.a, r.b) == ("a", "b")]
    assert ab.gap == pytest.approx(gap("U1", "U2"), abs=1e-3)
    assert ab.required == pytest.approx(channel_mm(8.0, 0.4, 0.5))
    assert ab.required - 1e-6 <= ab.gap <= ab.required + fp.slack_mm + PLACE_GRID_MM + 1e-6
    assert gap("U1", "U3") > ab.gap and gap("U2", "U3") > ab.gap
    assert all(r.b != "c" and r.a != "c" for r in fp.gaps)  # c shares no net: no gap row


def test_place_blocks_order_follows_connectivity() -> None:
    """Fixed blocks first, then always the block most connected to the placed ones."""
    m, groups = _eight()
    doc = BoardDoc(edges=(EdgeRow("U5", "left", "left", ""),))
    _, fp = _place(m, groups, doc)
    assert fp.fixed == ("blk5",)
    assert fp.order[0] == "blk5"
    # the chain grows from blk5: each next block shares a net with one already placed
    for i, n in enumerate(fp.order[1:], start=1):
        k = int(n[3:])
        assert {f"blk{k - 1}", f"blk{k + 1}"} & set(fp.order[:i]), fp.order


def test_place_blocks_falls_back_to_compact_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review focus 2: a board where the relaxed layout finds no spot gets P4's compact
    layout (`relaxed` False); nothing fits at all → `DoesNotFit` with `courtyard_fill`."""
    from copperchat.board.placer import floorplan as fpm

    real = fpm.pack_block

    def no_relaxed(*a: object, **kw: object) -> object:
        if kw.get("relax"):
            return DoesNotFit(1.0, 0.0, ())
        return real(*a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(fpm, "pack_block", no_relaxed)
    m, groups = _trio()
    res_, fp = _place(m, groups)
    assert not fp.relaxed and res_.score.m1_violations == 0

    def nothing(*a: object, **kw: object) -> object:
        return DoesNotFit(1.0, 0.0, ())

    monkeypatch.setattr(fpm, "pack_block", nothing)
    d = BoardDoc()
    r = place_blocks(m, classify(m, d), d, _blocks(groups), keep_channels=0.5, allowed_sides=TOP)
    assert isinstance(r, DoesNotFit) and r.courtyard_fill is not None


def test_place_blocks_retries_a_boxed_in_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """A free block that finds no spot moves to the front and the packing restarts."""
    from copperchat.board.placer import floorplan as fpm

    real = fpm.pack_block
    calls: list[tuple[str, ...]] = []

    def late_c_fails(*a: object, **kw: object) -> object:
        refs = tuple(a[2])  # type: ignore[arg-type]
        calls.append(refs)
        if refs == ("U3",) and len(calls) > 1 and kw.get("fixed"):
            return DoesNotFit(1.0, 0.0, refs)  # c cannot go after the others
        return real(*a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(fpm, "pack_block", late_c_fails)
    m, groups = _trio()
    _, fp = _place(m, groups)
    assert fp.order[0] == "c"  # moved to the front (no fixed blocks), placed first


def test_place_blocks_tries_every_pull_and_slack(monkeypatch: pytest.MonkeyPatch) -> None:
    """The greedy order is sensitive to the pull weight: every (slack, pull) pair of the
    relaxed layout is tried and the least airwire x (1 - SLACK_CREDIT x slack) wins."""
    from copperchat.board.placer import floorplan as fpm

    seen: list[tuple[float, float]] = []
    real = fpm._pack_retry

    def spy(*a: object, **kw: object) -> object:
        seen.append((float(kw["slack"]), float(kw["pull"])))  # type: ignore[arg-type]
        return real(*a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(fpm, "_pack_retry", spy)
    m, groups = _trio()
    _, fp = _place(m, groups)
    assert sorted(seen) == sorted(
        [
            *itertools.product(fpm.SLACK_STEPS_MM, fpm.TARGET_PULL_WEIGHTS),
            *itertools.product(fpm.SLACK_STEPS_MM, fpm.CENTRE_PULL_WEIGHTS),
        ]
    )  # one layout (relaxed) was enough; R7's centring pulls after the others
    assert fp.slack_mm in fpm.SLACK_STEPS_MM and fp.relaxed


def test_place_blocks_work_budget_bounds_a_board_that_cannot_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 1: the constructive packing spends at most PACK_BUDGET pack_block
    calls; a board that cannot fit fails with `does_not_fit` (courtyard_fill) instead of
    running into the job timeout."""
    from copperchat.board.placer import floorplan as fpm

    calls = [0]

    def never(*a: object, **kw: object) -> object:
        calls[0] += 1
        return DoesNotFit(1.0, 0.0, ())

    monkeypatch.setattr(fpm, "PACK_BUDGET", 7)
    monkeypatch.setattr(fpm, "pack_block", never)
    m, groups = _eight()
    d = BoardDoc()
    r = place_blocks(m, classify(m, d), d, _blocks(groups), keep_channels=0.5, allowed_sides=TOP)
    assert isinstance(r, DoesNotFit) and r.courtyard_fill is not None
    # 7 constructive calls per layout pass (relaxed, compact) + R2's region packing of the
    # first block (region, whole board)
    assert calls[0] == 7 + 7 + 2, calls


def test_place_blocks_relaxed_failures_never_starve_the_compact_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 2: each layout pass has its own PACK_BUDGET - a relaxed pass that
    spends all of it on failures still leaves P4's compact layout its full budget."""
    from copperchat.board.placer import floorplan as fpm

    real = fpm.pack_block

    def relaxed_never(*a: object, **kw: object) -> object:
        if kw.get("relax"):
            return DoesNotFit(1.0, 0.0, ())
        return real(*a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(fpm, "PACK_BUDGET", 12)
    monkeypatch.setattr(fpm, "pack_block", relaxed_never)
    m, groups = _trio()
    res_, fp = _place(m, groups)
    assert not fp.relaxed and res_.score.m1_violations == 0


def test_place_blocks_fixed_only_plan_ignores_stale_free_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 1: when no box floorplan tiles, the fixed blocks' strips are planned on
    the model with every member parked - a free block's stale position inside the strip is
    no obstacle; `usable_mm2` is the board's usable area, not 0."""
    from copperchat.board.placer import floorplan as fpm

    real = fpm.floorplan
    seen: list[tuple[list[str], set[str]]] = []

    def boxes_never_tile(model: BoardModel, *a: object, **kw: object) -> object:
        blocks = a[2]  # type: ignore[index]
        names = [b.name for b in blocks]  # type: ignore[attr-defined]
        if len(names) == 3:  # the full box floorplans: refused by shape
            return DoesNotFit(1.0, 1e9, ("U1",), courtyard_fill=0.1)
        on = {p.ref for p in model.parts if 0 <= p.x <= 80 and 0 <= p.y <= 60}
        seen.append((names, on))
        return real(model, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(fpm, "floorplan", boxes_never_tile)
    m, groups = _trio()
    # c's part starts on the left edge strip of a (U1's edge row)
    m = circuit(
        *(replace_part_at(p, 4.0, 30.0) if p.ref == "U3" else p for p in m.parts),
        outline=m.outline,
    )
    doc = BoardDoc(edges=(EdgeRow("U1", "left", "left", ""),))
    res_, fp = _place(m, groups, doc)
    assert seen and seen[0][0] == ["a"] and "U3" not in seen[0][1]
    assert fp.fixed == ("a",) and fp.usable_mm2 > 0
    assert res_.score.m1_violations == 0


def replace_part_at(p: Part, x: float, y: float) -> Part:
    return apply_moves(
        circuit(p, outline=outline(0, 0, 80, 60)), [Move(p.ref, x, y, 0.0, "top")]
    ).parts[0]


def test_place_blocks_reports_fixed_blocks_in_compact_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review round 1: a fixed block whose strip only holds P4's compact layout is listed
    in `compact_fixed` (the run is still `relaxed` for the free blocks)."""
    from copperchat.board.placer import floorplan as fpm

    real = fpm.pack_block

    def strip_too_small(*a: object, **kw: object) -> object:
        refs = tuple(a[2])  # type: ignore[arg-type]
        region = a[3]
        if refs == ("U1",) and kw.get("relax") and getattr(region, "w", 99) < 70:
            return DoesNotFit(1.0, 0.0, refs)
        return real(*a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(fpm, "pack_block", strip_too_small)
    m, groups = _trio()
    doc = BoardDoc(edges=(EdgeRow("U1", "left", "left", ""),))
    _, fp = _place(m, groups, doc)
    assert fp.relaxed and fp.compact_fixed == ("a",) and fp.fallback == ()


def test_centre_offset_weights_by_area() -> None:
    """R7: |dx| + |dy| of the moved parts' area-weighted centroid from the outline centre -
    one small part far off moves it little (a bounding box would jump)."""
    from copperchat.board.placer.floorplan import centre_offset

    big = blob("U1", 10.0, 10.0, ["A"], (40.0, 30.0))
    small = blob("R1", 1.0, 1.0, ["A"], (75.0, 55.0))
    m = circuit(big, small, outline=outline(0, 0, 80, 60))
    at = [Move("U1", 40.0, 30.0, 0.0, "top"), Move("R1", 75.0, 55.0, 0.0, "top")]
    off = centre_offset(m, at)
    # centroid x = (100 * 40 + 1 * 75) / 101, y = (100 * 30 + 55) / 101
    assert off == pytest.approx((4075 / 101 - 40) + (3055 / 101 - 30), abs=1e-6)
    assert centre_offset(m, [Move("U1", 20.0, 30.0, 0.0, "top")]) == pytest.approx(20.0)
    assert centre_offset(m, []) == 0.0


def test_place_blocks_centres_on_a_roomy_board(monkeypatch: pytest.MonkeyPatch) -> None:
    """R7 (R2b residual: ODH 80 x 100 packed at one end, next to its bottom-edge connector):
    on a board with room to spare the choice pays for the centroid's offset from the
    outline centre - the placement ends nearer the centre than the least-airwire one."""
    from copperchat.board.placer import floorplan as fpm

    parts: list[Part] = [conn("J1", {str(k): f"N{k}" for k in range(1, 7)}, (40.0, 95.0))]
    groups: dict[str, list[str]] = {"conn": ["J1"]}
    for i in range(1, 4):
        pins = {str(k): f"B{i}S{k}" for k in range(1, 9)}
        pins.update({"1": f"N{2 * i - 1}", "2": f"N{2 * i}"})
        parts.append(ic(f"U{i}", pins, (200.0 + 20 * i, 10.0)))
        parts.append(res(f"R{i}", f"B{i}S4", f"B{i}S5", (200.0 + 20 * i, 20.0)))
        groups[f"blk{i}"] = [f"U{i}", f"R{i}"]
    m = circuit(*parts, outline=outline(0, 0, 80, 100))
    doc = BoardDoc(edges=(EdgeRow("J1", "bottom", "", "user"),))
    centred, _ = _place(m, groups, doc)
    monkeypatch.setattr(fpm, "CENTRE_WEIGHT", 0.0)
    monkeypatch.setattr(fpm, "CENTRE_PULL_WEIGHTS", ())
    plain, _ = _place(m, groups, doc)
    assert fpm.centre_offset(m, centred.moves) < fpm.centre_offset(m, plain.moves) - 1.0
    assert centred.score.m1_violations == 0


def test_snap_edges_moves_the_block_to_its_edge() -> None:
    """R7 (ODH J201 0.75 mm from its edge, M13 allows 0.5): a part with a Placement edges
    row ends at the edge margin - its whole block moves - unless something is in the way."""
    from copperchat.board.placer.floorplan import snap_edges

    j = conn("J1", {"1": "A", "2": "B"}, (1.75, 30.0))  # its box 0.75 mm off the left edge
    r = res("R1", "A", "X", (5.75, 30.0))
    m = circuit(j, r, outline=outline(0, 0, 80, 60))
    doc = BoardDoc(edges=(EdgeRow("J1", "left", "", "user"),))
    blocks = _blocks({"blk": ["J1", "R1"]})
    moves = (Move("J1", 1.75, 30.0, 0.0, "top"), Move("R1", 5.75, 30.0, 0.0, "top"))
    gap0 = box_of(m.part("J1"))[0]
    assert edge_margin(m) + 0.2 < gap0 <= edge_margin(m) + PLACE_GRID_MM
    out = {mv.ref: mv for mv in snap_edges(m, doc, blocks, moves)}
    placed = apply_moves(m, tuple(out.values()))
    assert box_of(placed.part("J1"))[0] == pytest.approx(edge_margin(m), abs=0.05 + 1e-6)
    shift = out["J1"].x - 1.75
    assert shift < 0 and out["R1"].x - 5.75 == pytest.approx(shift)  # the block moved as one
    # far from its edge: at most one packing grid step (never a jump across the board)
    far = (Move("J1", 20.0, 30.0, 0.0, "top"), Move("R1", 24.0, 30.0, 0.0, "top"))
    moved = {mv.ref: mv for mv in snap_edges(m, doc, blocks, far)}
    assert 20.0 - moved["J1"].x == pytest.approx(PLACE_GRID_MM)
    # a part of another block between the connector and the edge: no move
    w = blob("W1", 0.4, 4.0, ["Z"], (edge_margin(m) + 0.2, 30.0))
    m2 = circuit(j, r, w, outline=outline(0, 0, 80, 60))
    blocked = {mv.ref: mv for mv in snap_edges(m2, doc, blocks, moves)}
    assert box_of(apply_moves(m2, tuple(blocked.values())).part("J1"))[0] >= box_of(w)[2]
    assert snap_edges(m, BoardDoc(), blocks, moves) == moves


def test_align_pairs_never_tightens_into_an_escape_strip() -> None:
    """R7 (ODH: the tighten pass pulled a CC resistor back in front of a USB-C VBUS pin):
    a shift never covers the escape strip in front of another part's wide-net pad; with
    the net at the Default width the same pair tightens up to the courtyard."""
    from .builders import net_class

    def model(wide: bool) -> BoardModel:
        u1 = part(
            "U1",
            50.0,
            42.0,
            pads=(
                pad("U1", "1", "VSYS", (52.5, 41.4), w=0.6, h=0.3),
                pad("U1", "2", "S4", (52.5, 42.0), w=0.6, h=0.3),
            ),
            courtyard=tuple(rect_poly(47.0, 40.0, 6.0, 4.0)),
        )
        classes = (net_class(), net_class("Power", 0.5, 0.2, patterns=("VSYS",)))
        return circuit(
            u1,
            res("R1", "S4", "X1", (56.0, 42.0)),
            outline=outline(0, 0, 80, 60),
            net_classes=classes if wide else (net_class(),),
            net_class_of=(("VSYS", "Effective for net: VSYS"),),
        )

    for wide, limit in ((True, 54.3), (False, 53.0)):
        m = model(wide)
        out = align_pairs(m, classify(m), _moves(m), movable=frozenset({"R1"}))
        left = box_of(apply_moves(m, out).part("R1"))[0]
        assert left < 55.0 - 1e-6  # it did tighten
        assert left >= limit - 1e-6, (wide, left)
        if not wide:
            assert left < 54.3  # without the wide class it goes past the strip


def test_snap_edges_block_mate_next_to_its_own_strip() -> None:
    """Review round 2: a block's own escape strips move with it - a resistor of the block
    right next to the connector's VBUS strip does not stop the snap (it moves exactly as
    with VBUS at the Default width); another part's strip still does."""
    from copperchat.board.placer.floorplan import snap_edges

    from .builders import net_class

    def run(wide: bool, *extra: Part) -> float:
        classes = (net_class(), net_class("Power", 0.5, 0.2, patterns=("VBUS",)))
        m = circuit(
            conn("J1", {"1": "A", "2": "VBUS"}, (1.75, 30.0)),  # VBUS strip x 3.05-4.55
            res("R1", "A", "X", (5.55, 30.0)),  # its box starts at the strip's far end
            *extra,
            outline=outline(0, 0, 80, 60),
            net_classes=classes if wide else (net_class(),),
            net_class_of=(("VBUS", "Effective for net: VBUS"),),
        )
        doc = BoardDoc(edges=(EdgeRow("J1", "left", "", "user"),))
        moves = (Move("J1", 1.75, 30.0, 0.0, "top"), Move("R1", 5.55, 30.0, 0.0, "top"))
        out = {mv.ref: mv for mv in snap_edges(m, doc, _blocks({"blk": ["J1", "R1"]}), moves)}
        assert out["R1"].x - 5.55 == pytest.approx(out["J1"].x - 1.75)
        return out["J1"].x - 1.75

    plain = run(False)
    assert plain < 0
    assert run(True) == pytest.approx(plain)
    # another part's VBUS strip where the block would move: no shift into it
    other = conn("J9", {"1": "VBUS"}, (1.0, 26.0))  # its strip reaches down past y 29
    got = run(True, other)
    assert got >= plain - 1e-9
