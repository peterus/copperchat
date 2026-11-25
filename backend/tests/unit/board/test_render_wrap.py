"""The pure board render wrapper (SP4 P1c Task 5, B-C9): escaping, scale bar, block
hulls, path simplification and the region viewBox."""

from __future__ import annotations

import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import replace

import pytest

from copperchat.board.model import Rect
from copperchat.board.render import (
    Hull,
    Marker,
    block_hulls,
    highlight_shapes,
    scale_bar,
    simplify_paths,
    wrap_svg,
)
from tests.unit.board.builders import board as model
from tests.unit.board.builders import part, track

NS = "{http://www.w3.org/2000/svg}"
KICAD = (
    '<svg viewBox="10 20 50 40" width="50mm" height="40mm">'
    '<path d="M 12,22 L 30,22 L 30,30 Z" style="fill:#C83434"/></svg>'
)


def _view_box(svg: str) -> tuple[float, ...]:
    root = ET.fromstring(svg)
    return tuple(float(v) for v in root.get("viewBox", "").split())


def test_escapes_injected_text() -> None:
    evil = '<script>&"'
    m = model(parts=(part("R1", 20, 30),), tracks=(track(net=evil, start=(12, 25), end=(40, 25)),))
    out = wrap_svg(
        KICAD,
        hulls=[Hull(name=evil, points=((12, 22), (40, 22), (40, 50)))],
        markers=[Marker(id=1, x=20, y=30)],
        highlight=highlight_shapes(m, [evil]),
        highlight_nets=[evil],
        title=evil,
        board_size=(50.0, 40.0),
    )
    assert "<script" not in out
    assert "&lt;script&gt;&amp;" in out
    root = ET.fromstring(out)  # still well-formed XML
    texts = "".join(t.text or "" for t in root.iter(f"{NS}text"))
    assert evil in texts
    assert all(el.tag != f"{NS}script" for el in root.iter())


@pytest.mark.parametrize("width", [3.0, 8.0, 20.0, 55.8, 100.0, 160.0, 400.0, 1000.0])
def test_scale_bar_step(width: float) -> None:
    step, count = scale_bar(width)
    assert step in (1.0, 2.0, 5.0, 10.0)
    assert count >= 1
    if width >= 4.0:  # below 4 mm even one 1 mm step is more than 25 %
        assert 0.10 * width - 1e-9 <= step * count <= 0.25 * width + 1e-9
    assert scale_bar(55.8) == (10.0, 1)
    assert scale_bar(20.0) == (5.0, 1)


def test_hull_per_group() -> None:
    parts = (
        part("U1", 20, 20, group="blk:power"),
        part("C1", 30, 20, group="blk:power"),
        part("C2", 25, 30, group="blk:power"),
        part("R9", 70, 70),
    )
    m = model(parts=parts, groups=(("blk:power", ("U1", "C1", "C2")), ("other", ("R9",))))
    hulls = block_hulls(m)
    assert [h.name for h in hulls] == ["power"]
    corners = {pt for p in parts[:3] for pt in p.courtyard}
    xs = [x for x, _ in hulls[0].points]
    ys = [y for _, y in hulls[0].points]
    assert min(xs) == min(x for x, _ in corners) and max(xs) == max(x for x, _ in corners)
    assert min(ys) == min(y for _, y in corners) and max(ys) == max(y for _, y in corners)
    out = wrap_svg(KICAD, hulls=hulls)
    root = ET.fromstring(out)
    group = root.find(f".//{NS}g[@id='hulls']")
    assert group is not None
    assert len(group.findall(f"{NS}polygon")) == 1
    assert [t.text for t in group.iter(f"{NS}text")] == ["power"]
    assert "stroke-dasharray" in ET.tostring(group, encoding="unicode")


def _seg_dist(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    t = (
        0.0
        if dx == dy == 0
        else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    )
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def test_simplify_keeps_shape() -> None:
    r, n = 10.0, 1000
    circle = [
        (50 + r * math.cos(2 * math.pi * i / n), 50 + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]
    d = "M " + "\n".join(f"{x:.4f},{y:.4f}" for x, y in circle) + "\nZ"
    root = ET.fromstring(
        f'<svg viewBox="0 0 100 100"><path d="{d}"/><path d="M 1,1 A 2,2 0 0 1 5,5"/>'
        '<path d="M 1,1 L 2,2 L 3,1"/></svg>'
    )
    arc_before, short_before = root[1].get("d"), root[2].get("d")
    simplify_paths(root, tol_mm=0.02)
    nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", root[0].get("d", ""))]
    pts = list(zip(nums[0::2], nums[1::2], strict=True))
    assert len(pts) < 200
    ring = [*pts, pts[0]]
    worst = max(
        min(_seg_dist(p, ring[i], ring[i + 1]) for i in range(len(ring) - 1)) for p in circle
    )
    assert worst <= 0.02 + 1e-4
    assert root[0].get("d", "").rstrip().endswith("Z")
    assert root[1].get("d") == arc_before  # curves untouched
    assert root[2].get("d") == short_before  # 200 points or fewer: untouched


def test_region_viewbox() -> None:
    full = _view_box(wrap_svg(KICAD))
    assert full[0] < 10 and full[1] < 20  # the drawing plus margins
    assert full[0] + full[2] > 60 and full[1] + full[3] > 60
    region = Rect(x=30.0, y=25.0, w=10.0, h=5.0)
    x, y, w, h = _view_box(wrap_svg(KICAD, extent=region))
    assert x <= 30 and y <= 25 and x + w >= 40 and y + h >= 30
    assert w < 15 and h < 15  # zoomed: not the whole drawing
    # bottom (mirrored) view: board x maps to -x in KiCad's drawing
    mirrored = KICAD.replace('viewBox="10 20 50 40"', 'viewBox="-60 20 50 40"')
    x, y, w, h = _view_box(wrap_svg(mirrored, extent=region, mirror=True))
    assert x <= -40 and x + w >= -30


def test_markers_and_highlight_drawn() -> None:
    m = model(
        tracks=(
            track(net="GND", start=(12, 25), end=(40, 25)),
            replace(track(net="VCC"), kiid="t2"),
        )
    )
    shapes = highlight_shapes(m, ["GND"])
    assert len(shapes) == 1 and shapes[0].net == "GND"
    out = wrap_svg(
        KICAD, markers=[Marker(id=3, x=20, y=30)], highlight=shapes, highlight_nets=["GND"]
    )
    root = ET.fromstring(out)
    marks = root.find(f".//{NS}g[@id='markers']")
    assert marks is not None and [t.text for t in marks.iter(f"{NS}text")] == ["3"]
    hl = root.find(f".//{NS}g[@id='highlight']")
    assert hl is not None and len(list(hl)) == 1


def test_simplify_work_is_bounded() -> None:
    # A zig-zag where every point is kept costs RDP O(n^2); the budget stops it and the
    # rest of the path stays exactly as it was.
    n = 4000
    zigzag = [(i * 0.01, 1.0 if i % 2 else 0.0) for i in range(n)]
    d = "M " + "\n".join(f"{x:.4f},{y:.4f}" for x, y in zigzag)
    root = ET.fromstring(f'<svg viewBox="0 0 100 100"><path d="{d}"/></svg>')
    start = time.monotonic()
    removed = simplify_paths(root, tol_mm=0.02, budget=10_000)
    assert time.monotonic() - start < 1.0
    nums = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", root[0].get("d", ""))]
    pts = list(zip(nums[0::2], nums[1::2], strict=True))
    assert removed == 0
    assert pts == [(round(x, 4), y) for x, y in zigzag]
