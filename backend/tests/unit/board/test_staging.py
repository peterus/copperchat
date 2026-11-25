"""The staging area of a synced board (B-C11 `staging_positions`, spec §7.2)."""

from __future__ import annotations

import random
from dataclasses import replace
from typing import Any

from copperchat.board.geom import polygons_overlap, rect_poly
from copperchat.board.model import BoardModel, Outline, Part
from copperchat.board.staging import Move, staging_positions
from tests.unit.board.builders import board, outline, part

NO_OUTLINE = Outline("none", 0.0, 0.0, 0.0, 0.0, 0.0, ())


def _moved(m: BoardModel, moves: tuple[Move, ...]) -> list[tuple[float, ...]]:
    """The courtyards of the moved parts as (x0, y0, x1, y1) boxes."""
    by_ref = {p.ref: p for p in m.parts}
    boxes = []
    for mv in moves:
        p = by_ref[mv.ref]
        xs = [q[0] - p.x + mv.x for q in p.courtyard]
        ys = [q[1] - p.y + mv.y for q in p.courtyard]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


def _box_poly(b: tuple[float, ...]) -> list[tuple[float, float]]:
    return rect_poly(b[0], b[1], b[2] - b[0], b[3] - b[1])


def test_right_of_outline() -> None:
    m = board(outline=outline(10, 20, 50, 40))
    moves = staging_positions(m, ["R1", "R2"])
    assert [mv.ref for mv in moves] == ["R1", "R2"]
    for box in _moved(m, moves):
        assert box[0] >= 10 + 50 + 5 - 1e-9
        assert box[1] >= 20 - 1e-9


def test_no_outline_right_of_parts() -> None:
    m = board(
        outline=NO_OUTLINE,
        parts=(part("U1", 30, 30, half=5), part("R1", 0, 0), part("R2", 0, 0)),
    )
    moves = staging_positions(m, ["R1", "R2"])
    for box in _moved(m, moves):
        assert box[0] >= 35 + 5 - 1e-9  # U1's courtyard ends at x = 35


def test_empty_board_origin() -> None:
    m = board(outline=NO_OUTLINE, parts=(part("R1", 7, 9), part("R2", -3, 4)))
    moves = staging_positions(m, ["R2", "R1"])
    boxes = _moved(m, moves)
    assert min(b[0] for b in boxes) == 0.0
    assert min(b[1] for b in boxes) == 0.0


def test_natural_ref_order_and_rotation_kept() -> None:
    parts = (part("R10", rot=90), part("R2", side="bottom"), part("C1"), part("R1"))
    m = board(parts=parts)
    moves = staging_positions(m, ["R10", "R2", "C1", "R1"])
    assert [mv.ref for mv in moves] == ["C1", "R1", "R2", "R10"]
    by_ref = {mv.ref: mv for mv in moves}
    assert by_ref["R10"].rot == 90 and by_ref["R2"].side == "bottom"


def _random_parts(rng: random.Random, n: int) -> tuple[Part, ...]:
    out = []
    for i in range(n):
        w, h = rng.uniform(0.5, 12), rng.uniform(0.5, 12)
        x, y = rng.uniform(-50, 50), rng.uniform(-50, 50)
        dx, dy = rng.uniform(-w, 0), rng.uniform(-h, 0)  # origin anywhere in the courtyard
        out.append(
            replace(part(f"P{i + 1}", x, y), courtyard=tuple(rect_poly(x + dx, y + dy, w, h)))
        )
    return tuple(out)


def test_no_overlaps() -> None:
    rng = random.Random(7)
    for case in range(200):
        parts = _random_parts(rng, rng.randint(1, 40))
        o = outline(0, 0, rng.uniform(5, 80), rng.uniform(5, 80))
        m = board(parts=parts, outline=o)
        moves = staging_positions(m, [p.ref for p in parts])
        boxes = _moved(m, moves)
        for i, a in enumerate(boxes):
            assert a[0] >= o.x + o.w + 5 - 1e-9, f"seed 7 case {case}"
            for b in boxes[i + 1 :]:
                assert not polygons_overlap(_box_poly(a), _box_poly(b)), f"seed 7 case {case}"


def test_deterministic() -> None:
    m = board(parts=_random_parts(random.Random(3), 25))
    refs = [p.ref for p in m.parts]
    assert staging_positions(m, refs) == staging_positions(m, list(reversed(refs)))


def test_wraps_columns() -> None:
    parts = tuple(part(f"R{i}", half=2) for i in range(1, 31))
    m = board(parts=parts, outline=outline(0, 0, 30, 20))
    moves = staging_positions(m, [p.ref for p in parts])
    assert len({mv.x for mv in moves}) >= 2
    for box in _moved(m, moves):
        assert box[3] <= 20 + 1e-9


def test_part_without_courtyard_gets_a_cell() -> None:
    m = board(parts=(part("R1", courtyard=()), part("R2")))
    moves = staging_positions(m, ["R1", "R2"])
    assert len(moves) == 2 and moves[0].y != moves[1].y


def test_unknown_refs_ignored() -> None:
    assert staging_positions(board(), ["R9"]) == ()


def test_warning_rows_pad_lines() -> None:
    from copperchat.kicad_tools.board.sync import warning_rows

    report = (
        "Processing symbol 'U3:Package_TO_SOT_SMD:SOT-23'.\n"
        "Disconnect U3 pin 5.\n"
        "Warning: No pad found for pin 7 in footprint R10.\n"
        "Warning: something else entirely.\n"
        "Add U3 'Do not place' fabrication attribute.\n"
    )
    rows = warning_rows(report, {"U3", "R10"})
    assert [r["text"] for r in rows] == [
        "Disconnect U3 pin 5.",
        "Warning: No pad found for pin 7 in footprint R10.",
        "Warning: something else entirely.",
    ]
    assert rows[0]["suggestion"] == "check the footprint of U3 with fp_check"
    assert rows[1]["ref"] == "R10" and "suggestion" not in rows[2]


def test_second_sync_stages_right_of_earlier_staged_parts() -> None:
    m = board(outline=outline(0, 0, 30, 20), parts=tuple(part(f"R{i}") for i in range(1, 6)))
    first = staging_positions(m, ["R1", "R2"])
    by = {mv.ref: mv for mv in first}
    m2 = board(
        outline=outline(0, 0, 30, 20),
        parts=tuple(
            part(r, by[r].x, by[r].y) if r in by else part(r) for r in ("R1", "R2", "R3", "R4")
        ),
    )
    second = staging_positions(m2, ["R3", "R4"])
    old = _moved(m2, tuple(Move(r, by[r].x, by[r].y, 0.0, "top") for r in by))
    for a in _moved(m2, second):
        for b in old:
            assert not polygons_overlap(_box_poly(a), _box_poly(b))


def test_pad_box_respects_rotation() -> None:
    from copperchat.board.model import Pad
    from copperchat.board.staging import part_box

    pad = Pad("R1", "1", "N", 10.0, 10.0, 4.0, 1.0, 90.0, "rect", frozenset({"F.Cu"}), True, None)
    x0, y0, x1, y1 = part_box(part("R1", 10, 10, courtyard=(), pads=(pad,)))
    assert abs((x1 - x0) - 1.0) < 1e-9 and abs((y1 - y0) - 4.0) < 1e-9


def _ctx_for(tmp: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(project=SimpleNamespace(dir=tmp, name="p"))


def test_netlist_dir_refuses_symlinked_agent_dir(tmp_path: Any, monkeypatch: Any) -> None:
    import pytest

    from copperchat.errors import StructuredToolError
    from copperchat.kicad_tools.board import sync

    outside = tmp_path / "outside"
    outside.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".kicad-agent").symlink_to(outside)
    monkeypatch.setattr(sync, "agent_dir", lambda p: p.dir / ".kicad-agent")
    with pytest.raises(StructuredToolError) as err:
        sync._netlist_dir(_ctx_for(proj))
    assert err.value.code == "output_path_unsafe"
    assert list(outside.iterdir()) == []


def test_netlist_dir_sweeps_stale_scratch(tmp_path: Any, monkeypatch: Any) -> None:
    import os
    import time

    from copperchat.kicad_tools.board import sync

    monkeypatch.setattr(sync, "agent_dir", lambda p: p.dir / ".kicad-agent")
    base = tmp_path / ".kicad-agent" / "board"
    stale, fresh = base / "sync-old", base / "sync-new"
    stale.mkdir(parents=True)
    fresh.mkdir()
    old = time.time() - 2 * sync.STALE_SCRATCH_S
    os.utime(stale, (old, old))
    d = sync._netlist_dir(_ctx_for(tmp_path))
    assert not stale.exists() and fresh.exists() and d.is_dir()


def test_staging_stays_in_kicad_range() -> None:
    from copperchat.board.staging import MAX_STAGING_X_MM

    parts = tuple(part(f"R{i}", half=2) for i in range(1, 401))
    m = board(parts=parts, outline=outline(1000, 0, 500, 5))
    moves = staging_positions(m, [p.ref for p in parts])
    boxes = _moved(m, moves)
    assert max(b[2] for b in boxes) <= MAX_STAGING_X_MM
    assert max(b[3] for b in boxes) < 2147
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert not polygons_overlap(_box_poly(a), _box_poly(b))
