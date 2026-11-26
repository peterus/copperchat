"""Pure reference placement for `pcb_tidy_silk` (SP4 P7 Task 5, `cc/board/silk.py`)."""

from __future__ import annotations

import random
from dataclasses import replace

from copperchat.board.metrics import _pad_poly
from copperchat.board.model import BoardModel, GraphicItem, Pad, Part, Point, TextItem
from copperchat.board.silk import GAP_MM, candidate_spots, plan_refs, text_size

from .builders import board, part

H, LINE = 1.0, 0.15


def _pad(ref: str, n: str, at: Point, w: float = 0.6, h: float = 0.6) -> Pad:
    return Pad(ref, n, "N", at[0], at[1], w, h, 0.0, "rect", frozenset({"F.Cu"}), True, None)


def _two_pad(ref: str, x: float, y: float, **kw: object) -> Part:
    pads = (_pad(ref, "1", (x - 0.8, y)), _pad(ref, "2", (x + 0.8, y)))
    return part(ref, x, y, half=1.2, pads=pads, **kw)  # type: ignore[arg-type]


def _boxes(m: BoardModel, placements: tuple[object, ...]) -> list[tuple[str, tuple[float, ...]]]:
    out = []
    for pl in placements:
        p = m.part(pl.ref)  # type: ignore[attr-defined]
        w, h = text_size(p.ref, H, LINE, pl.rot)  # type: ignore[attr-defined]
        x, y = pl.at  # type: ignore[attr-defined]
        out.append((pl.layer, (x - w / 2, y - h / 2, x + w / 2, y + h / 2)))  # type: ignore[attr-defined]
    return out


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _hits_pad(box: tuple[float, ...], m: BoardModel) -> bool:
    for p in m.parts:
        for q in p.pads:
            xs = [c[0] for c in _pad_poly(q)]
            ys = [c[1] for c in _pad_poly(q)]
            if _overlap(box, (min(xs) - 0.1, min(ys) - 0.1, max(xs) + 0.1, max(ys) + 0.1)):
                return True
    return False


def test_ref_outside_pads() -> None:
    m = board(parts=(_two_pad("R1", 30, 30), _two_pad("R2", 33, 30)))
    placements, unresolved = plan_refs(m, height=H, line=LINE)
    assert unresolved == () and {p.ref for p in placements} == {"R1", "R2"}
    for _, box in _boxes(m, placements):
        assert not _hits_pad(box, m)


def test_prefers_above_then_next_free() -> None:
    m = board(parts=(_two_pad("R1", 30, 30),))
    (pl,), _ = plan_refs(m, height=H, line=LINE)
    assert pl.candidate == 0
    assert abs(pl.at[1] - (30 - 1.2 - GAP_MM - (H + LINE) / 2)) < 1e-9
    (pl2,), _ = plan_refs(m, height=H, line=LINE, exclude={"R1": frozenset({0})})
    assert pl2.candidate == 1 and pl2.at[1] > 30


def test_refs_do_not_overlap() -> None:
    """200 random parts (seeded; the seed is printed on failure): no two placed
    references overlap, none overlaps a pad."""
    seed = 11
    rnd = random.Random(seed)
    placed = 0
    for _ in range(20):
        parts = []
        for i in range(10):
            x, y = rnd.uniform(8, 92), rnd.uniform(8, 72)
            parts.append(_two_pad(f"R{i + 1}", x, y, rot=rnd.choice([0.0, 90.0])))
        m = board(parts=tuple(parts))
        placements, _ = plan_refs(m, height=H, line=LINE)
        placed += len(placements)
        boxes = _boxes(m, placements)
        for i, (la, a) in enumerate(boxes):
            assert not _hits_pad(a, m), f"seed {seed}"
            for lb, b in boxes[i + 1 :]:
                assert la != lb or not _overlap(a, b), f"seed {seed}"
    assert placed >= 150


def test_rotated_part_gets_vertical_ref() -> None:
    m = board(parts=(_two_pad("R1", 30, 30, rot=90.0),))
    (pl,), _ = plan_refs(m, height=H, line=LINE)
    assert pl.rot == 90.0
    assert text_size("R1", H, LINE, 90.0) == (H + LINE, 2 * H + LINE)


def test_bottom_part_on_b_silk() -> None:
    m = board(parts=(_two_pad("R1", 30, 30, side="bottom"),))
    (pl,), _ = plan_refs(m, height=H, line=LINE)
    assert pl.layer == "B.SilkS"


def test_avoids_silk_graphics_and_texts() -> None:
    p = _two_pad("R1", 30, 30)
    above = candidate_spots(p, "R1", height=H, line=LINE)[0]
    line = GraphicItem("F.SilkS", "segment", ((25.0, above[1]), (35.0, above[1])), 0.12)
    m = board(parts=(replace(p, silk=(line,)),))
    (pl,), _ = plan_refs(m, height=H, line=LINE)
    assert pl.candidate != 0
    logo = TextItem("text", "LOGO", "F.SilkS", (30.0, 31.8), (1.0, 1.0), 0.15, True)
    m2 = board(parts=(replace(p, silk=(line,)),), texts=(logo,))
    (pl2,), _ = plan_refs(m2, height=H, line=LINE)
    assert pl2.candidate not in (0, 1)


def test_prefers_spot_off_other_courtyards() -> None:
    a = _two_pad("R1", 30, 30)
    b = part("U1", 30, 27.0, half=1.0, pads=())  # its courtyard covers R1's "above" spot
    m = board(parts=(a, b))
    pl = next(p for p in plan_refs(m, height=H, line=LINE)[0] if p.ref == "R1")
    assert pl.candidate == 1


def test_crowded_part_unresolved() -> None:
    p = _two_pad("R1", 30, 30)
    ring = []
    for dx in range(-6, 7):
        for dy in range(-4, 5):
            if abs(dx) <= 1 and abs(dy) <= 1:
                continue
            ring.append(_pad("X1", str(len(ring)), (30 + dx * 0.9, 30 + dy * 0.9), 0.7, 0.7))
    crowd = part("X1", 30, 30, half=0.1, pads=tuple(ring), texts=())
    m = board(parts=(p, crowd))
    placements, unresolved = plan_refs(m, height=H, line=LINE)
    assert unresolved == ("R1",) and placements == ()


def test_skip_keeps_ref() -> None:
    m = board(parts=(_two_pad("R1", 30, 30), _two_pad("R2", 50, 30)))
    placements, _ = plan_refs(m, height=H, line=LINE, skip=frozenset({"R2"}))
    assert [p.ref for p in placements] == ["R1"]


def test_uniform_size() -> None:
    """Every candidate is computed from the one height/line: the above-spot sits exactly
    GAP + (height + line) / 2 off the courtyard for every part."""
    parts = tuple(_two_pad(f"R{i}", 20 + 6 * i, 30) for i in range(1, 6))
    m = board(parts=parts)
    placements, _ = plan_refs(m, height=H, line=LINE)
    for pl in placements:
        assert pl.candidate == 0
        assert abs(pl.at[1] - (30 - 1.2 - GAP_MM - (H + LINE) / 2)) < 1e-9


def test_deterministic() -> None:
    parts = tuple(_two_pad(f"R{i}", 20 + 2.5 * i, 30 + (i % 3)) for i in range(1, 12))
    m = board(parts=parts)
    assert plan_refs(m, height=H, line=LINE) == plan_refs(m, height=H, line=LINE)
    order = [p.ref for p in plan_refs(m, height=H, line=LINE)[0]]
    assert order == sorted(order, key=lambda r: int(r[1:]))
