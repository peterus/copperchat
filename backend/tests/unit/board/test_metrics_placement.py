"""B-C7 metrics, placement side: M1, M8, M9, M10 and the stage."""

from __future__ import annotations

from dataclasses import replace

import pytest

from copperchat.board import metrics
from copperchat.board.boardmd import BoardDoc, EdgeRow
from copperchat.board.classify import Classification, Crystal, Decoupling, Switcher, classify
from copperchat.board.geom import rect_poly
from copperchat.board.metrics import Metric, MetricsReport, compute_metrics
from copperchat.board.model import BoardModel, Outline, Part

from .builders import board, part, track
from .builders_circuit import cap, circuit, ic, ind, xtal


def run(
    m: BoardModel,
    cls: Classification | None = None,
    doc: BoardDoc | None = None,
    only: frozenset[str] | None = None,
) -> MetricsReport:
    return compute_metrics(
        m,
        cls if cls is not None else classify(m),
        doc if doc is not None else BoardDoc(),
        drc=None,
        parity=None,
        preset=None,
        only=only,
    )


def get(rep: MetricsReport, mid: str) -> Metric:
    return next(x for x in rep.metrics if x.id == mid)


# ---- M1 ------------------------------------------------------------------------------------


def test_m1_outside_by_001() -> None:
    m = board(parts=(part("R1", 50, 40), part("R2", 1.99, 40)))
    m1 = get(run(m), "M1")
    assert m1.value == 1
    assert m1.verdict == "fail"
    assert m1.offenders[0].refs == ("R2",)


def test_m1_inside_passes() -> None:
    m = board(parts=(part("R1", 50, 40), part("R2", 2.0, 40)))
    assert get(run(m), "M1").verdict == "pass"


def test_m1_overlap_same_side_only() -> None:
    m = board(parts=(part("R1", 50, 40), part("R2", 51, 40, side="bottom")))
    m1 = get(run(m), "M1")
    assert m1.value == 0
    assert m1.verdict == "pass"
    same = board(parts=(part("R1", 50, 40), part("R2", 51, 40)))
    m1 = get(run(same), "M1")
    assert m1.value == 1
    assert m1.verdict == "fail"
    assert set(m1.offenders[0].refs) == {"R1", "R2"}


def test_m1_bottom_smd_not_allowed() -> None:
    m = circuit(cap("C1", "A", "B", (50, 40), side="bottom"))
    m1 = get(run(m), "M1")
    assert m1.value == 1
    assert m1.verdict == "fail"


def test_m1_bottom_smd_allowed() -> None:
    m = circuit(cap("C1", "A", "B", (50, 40), side="bottom"))
    doc = BoardDoc(allowed_sides=frozenset({"top", "bottom"}))
    assert get(run(m, doc=doc), "M1").verdict == "pass"


def test_m1_no_courtyard_counts() -> None:
    m = board(parts=(part("R1", 50, 40, courtyard=()),))
    assert get(run(m), "M1").value == 1


def test_m1_na_without_outline() -> None:
    m = board(outline=Outline("none", 0, 0, 0, 0, 0, ()))
    m1 = get(run(m), "M1")
    assert m1.verdict == "n/a"
    assert m1.value is None


def _edge_board(x: float, y: float = 40.0) -> tuple[BoardModel, BoardDoc]:
    m = board(parts=(part("J1", x, y),))
    return m, BoardDoc(edges=(EdgeRow("J1", "left", "left edge", "cable"),))


def test_m1_edge_overhang_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    m, doc = _edge_board(1.0)  # courtyard x -1..3: overhangs the left edge by 1 mm
    monkeypatch.setattr(metrics, "M1_EDGE_OVERHANG", False)
    assert get(run(m, doc=doc), "M1").verdict == "fail"
    monkeypatch.setattr(metrics, "M1_EDGE_OVERHANG", True)
    assert get(run(m, doc=doc), "M1").verdict == "pass"
    # not listed in board.md: still a violation
    assert get(run(m), "M1").verdict == "fail"


def test_m1_edge_overhang_other_edge_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "M1_EDGE_OVERHANG", True)
    m, doc = _edge_board(10.0, 1.0)  # overhangs the top edge, assigned edge is left
    assert get(run(m, doc=doc), "M1").verdict == "fail"
    m, doc = _edge_board(1.0, 1.0)  # overhangs left (allowed) and top (not allowed)
    assert get(run(m, doc=doc), "M1").verdict == "fail"


def test_placed_with_overhang_rounded_outline() -> None:
    o = Outline("rect", 0, 0, 100, 80, 3.0, tuple(rect_poly(0, 0, 100, 80, 3.0)))
    m = board(outline=o, parts=(part("J1", 1.0, 40), part("J2", 1.0, 3.0)))
    assert metrics.placed_with_overhang(m, m.part("J1"), "left")
    # near the rounded corner the clipped courtyard leaves the outline
    assert not metrics.placed_with_overhang(m, m.part("J2"), "left")


# ---- M8 ------------------------------------------------------------------------------------

_U1_PINS = {"1": "+3V3", "2": "GND", "3": "XI", "4": "XO", "5": "SIG"}


def _dec_board(cap_at: tuple[float, float], **cap_kw: object) -> tuple[BoardModel, Classification]:
    u1 = ic("U1", _U1_PINS, (50, 40))
    c1 = cap("C1", "+3V3", "GND", cap_at, **cap_kw)  # pad 1 (+3V3) at cap_at.x - 0.5
    m = circuit(u1, c1)
    cls = replace(classify(m), decoupling=(Decoupling("C1", "U1", "1", "+3V3", "GND"),))
    return m, cls


def test_m8_estimate_review_at_3_1() -> None:
    m, cls = _dec_board((50.5, 43.1))
    rep = run(m, cls)
    assert rep.stage == "placement"
    m8 = get(rep, "M8")
    assert m8.verdict == "review"
    assert m8.value == pytest.approx(3.1)
    assert m8.offenders[0].refs == ("C1", "U1")


def test_m8_estimate_over_5_is_review_at_placement() -> None:
    m, cls = _dec_board((50.5, 46.0))
    assert get(run(m, cls), "M8").verdict == "review"


def test_m8_close_passes() -> None:
    m, cls = _dec_board((50.5, 42.0))
    assert get(run(m, cls), "M8").verdict == "pass"


def test_m8_other_side_fails() -> None:
    m, cls = _dec_board((50.5, 42.0), side="bottom")
    m8 = get(run(m, cls), "M8")
    assert m8.verdict == "fail"
    assert "side" in m8.offenders[0].what


def test_m8_duplicate_pad_numbers_use_nearest_supply_pad() -> None:
    """Two U1 pads numbered "1" (both +3V3) plus one "1" on GND nearer still: M8 measures
    from the cap to the same-number pad on the supply net nearest the cap (as classify)."""
    from .builders_circuit import pad

    pads = (
        pad("U1", "1", "+3V3", (40.0, 40.0)),
        pad("U1", "1", "GND", (60.0, 41.0)),
        pad("U1", "1", "+3V3", (60.0, 40.0)),
        pad("U1", "2", "GND", (61.0, 40.0)),
    )
    u1 = part("U1", 50, 40, pads=pads, courtyard=tuple(rect_poly(39, 39, 23, 3)))
    c1 = cap("C1", "+3V3", "GND", (60.5, 41.5))  # +3V3 pad at (60, 41.5)
    m = circuit(u1, c1)
    cls = replace(classify(m), decoupling=(Decoupling("C1", "U1", "1", "+3V3", "GND"),))
    m8 = get(run(m, cls), "M8")
    assert m8.value == pytest.approx(1.5)
    assert m8.verdict == "pass"


def test_m8_crystal() -> None:
    u1 = ic("U1", _U1_PINS, (50, 40))  # XI pin 3 at (52, 40), XO pin 4 at (53, 40)
    y1 = xtal("Y1", "XI", "XO", (52.5, 44.0))  # XI pad (52, 44): 4 mm; XO pad (53, 44): 4 mm
    m = circuit(u1, y1)
    cls = replace(classify(m), decoupling=(), crystals=(Crystal("Y1", "U1", (), ("XI", "XO")),))
    m8 = get(run(m, cls), "M8")
    assert m8.verdict == "review"
    assert m8.value == pytest.approx(4.0)
    assert len(m8.offenders) == 2
    assert {o.nets for o in m8.offenders} == {("XI",), ("XO",)}
    near = circuit(u1, xtal("Y1", "XI", "XO", (52.5, 42.0)))
    assert get(run(near, replace(cls)), "M8").verdict == "pass"


# ---- M9 ------------------------------------------------------------------------------------


def test_m9_hot_loop_review() -> None:
    u1 = ic("U1", {"1": "VIN", "2": "SW", "3": "GND", "4": "FB", "5": "OUT"}, (50, 40))
    c1 = cap("C1", "VIN", "GND", (50.5, 44.0))  # VIN pad (50, 44) -> pin 1 (50, 40): 4 mm
    l1 = ind("L1", "SW", "OUT", (51.5, 41.5))  # SW pad (51, 41.5) -> pin 2 (51, 40): 1.5 mm
    m = circuit(u1, c1, l1)
    sw = Switcher("U1", "L1", "SW", "VIN", ("C1",), (), ())
    cls = replace(classify(m), switchers=(sw,), decoupling=())
    m9 = get(run(m, cls), "M9")
    assert m9.verdict == "review"
    assert m9.value == 1
    assert m9.offenders[0].refs == ("C1", "U1")
    assert m9.hard is None
    close = circuit(u1, cap("C1", "VIN", "GND", (50.5, 42.0)), l1)
    assert get(run(close, cls), "M9").verdict == "pass"


def test_m9_na_without_switchers() -> None:
    m = circuit(cap("C1", "A", "B"))
    assert get(run(m), "M9").verdict == "n/a"


# ---- M10 -----------------------------------------------------------------------------------


def _box(ref: str, x: float, y: float, w: float, h: float) -> Part:
    return part(ref, x + w / 2, y + h / 2, courtyard=tuple(rect_poly(x, y, w, h)))


def test_m10_ratio_and_intrusion() -> None:
    a = _box("R1", 10, 10, 3, 1.5)  # 4.5 mm2
    b = _box("R2", 10, 18.5, 3, 1.5)  # 4.5 mm2 -> hull 3 x 10 = 30 mm2
    m = board(parts=(a, b), groups=(("blk:power", ("R1", "R2")),))
    m10 = get(run(m), "M10")
    assert m10.verdict == "review"
    assert m10.value == pytest.approx(30 / 9, abs=0.01)
    intruder = _box("R3", 11, 14, 1, 1)
    m = board(parts=(a, b, intruder), groups=(("blk:power", ("R1", "R2")),))
    m10 = get(run(m), "M10")
    intrusions = [o for o in m10.offenders if "intrusion" in o.what]
    assert len(intrusions) == 1
    assert intrusions[0].refs == ("R3",)
    # the same foreign part on the other side is no intrusion
    other = replace(intruder, side="bottom")
    m = board(parts=(a, b, other), groups=(("blk:power", ("R1", "R2")),))
    assert not [o for o in get(run(m), "M10").offenders if "intrusion" in o.what]


def test_m10_compact_block_passes() -> None:
    a = _box("R1", 10, 10, 3, 3)
    b = _box("R2", 13, 10, 3, 3)
    m = board(parts=(a, b), groups=(("blk:io", ("R1", "R2")),))
    m10 = get(run(m), "M10")
    assert m10.verdict == "pass"
    assert m10.value == pytest.approx(1.0)


# ---- stage ---------------------------------------------------------------------------------


def test_stage_detection() -> None:
    m = circuit(cap("C1", "GND", "SIG"))
    assert run(m).stage == "placement"
    gnd = replace(m, tracks=(track("GND", "F.Cu", (1, 1), (5, 1)),))
    assert run(gnd).stage == "placement"
    sig = replace(m, tracks=(track("SIG", "F.Cu", (1, 1), (5, 1)),))
    assert run(sig).stage == "routed"


def test_m10_fallback_skips_misc_block() -> None:
    """Without `blk:*` groups M10 uses the classification's blocks; the `misc` block (no
    anchor, e.g. four corner mounting holes) is no functional group and is skipped."""
    holes = [
        part(f"H{k + 1}", x, y, lib_id="MountingHole:MountingHole_3.2mm")
        for k, (x, y) in enumerate(((4, 4), (96, 4), (4, 76), (96, 76)))
    ]
    u1 = ic("U1", {"1": "+3V3", "2": "GND", "3": "A"}, (50, 40))
    m = circuit(u1, cap("C1", "+3V3", "GND", (50.5, 42)), *holes)
    cls = classify(m)
    misc = [b for b in cls.blocks if b.anchor is None]
    assert misc and {"H1", "H2", "H3", "H4"} <= set(misc[0].refs)
    m10 = get(run(m, cls), "M10")
    assert m10.verdict != "review", m10.offenders


def test_m10_intrusion_timing_200_blocks_3800_parts() -> None:
    """200 anchored blocks (anchor + 1 cap each) and 3400 loose parts elsewhere on a large
    board: M10 only, budget 1 s; the one planted intruder is found."""
    import time

    from copperchat.board.classify import BlockProposal

    from .builders import outline

    parts: list[Part] = []
    blocks = []
    for k in range(200):
        x, y = 5.0 + 10.0 * (k % 20), 5.0 + 10.0 * (k // 20)
        a, b = _box(f"U{k}", x, y, 3, 3), _box(f"C{k}", x + 3, y, 1, 1)
        parts += [a, b]
        blocks.append(BlockProposal(f"blk{k}", f"U{k}", (f"U{k}", f"C{k}"), ""))
    for k in range(3400):
        parts.append(_box(f"R{k}", 5.0 + 3.0 * (k % 60), 120.0 + 3.0 * (k // 60), 1, 1))
    parts.append(_box("RX", 7.0, 5.5, 0.5, 0.5))  # inside U0 + C0's hull
    m = board(parts=tuple(parts), outline=outline(0, 0, 220, 300))
    assert len(m.parts) >= 3800
    cls = replace(classify(board(parts=())), blocks=tuple(blocks))
    t0 = time.perf_counter()
    m10 = get(run(m, cls, only=frozenset({"M10"})), "M10")
    elapsed = time.perf_counter() - t0
    print(f"M10 200 blocks / {len(m.parts)} parts: {elapsed:.3f} s")
    assert [o.refs for o in m10.offenders if "intrusion" in o.what] == [("RX",)]
    assert elapsed < 1.0, elapsed
