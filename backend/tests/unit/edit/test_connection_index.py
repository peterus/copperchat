"""The indexed connection-point lookup (E4 residual): identical to `Scene.items_at`."""

from __future__ import annotations

import random

from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.geometry import Box, PinEnd, Point
from copperchat.kicad_tools.edit.place import Layout, _fmt, clashes
from copperchat.kicad_tools.edit.scene import (
    ConnectionIndex,
    LabelInfo,
    Scene,
    SymbolInfo,
    WireInfo,
)

SHEET = SheetRef(path="/root", page="1", file="t.kicad_sch", name="", human_path="/")
GRID = 1.27


def _pin(uuid: str, number: str, at: Point) -> PinEnd:
    return PinEnd((SHEET.path, uuid, number), "U1", number, "", "passive", at, "left")


def _scene(rng: random.Random) -> tuple[Scene, list[Point]]:
    scene = Scene(sheet=SHEET)
    pts: list[Point] = []

    def pt() -> Point:
        p = (rng.randint(0, 120) * GRID, rng.randint(0, 120) * GRID)
        pts.append(p)
        return p

    for s in range(30):
        uid = f"sym-{s}"
        pins = [_pin(uid, str(n), pt()) for n in range(rng.randint(1, 12))]
        scene.symbols[uid] = SymbolInfo(
            uid, f"U{s}", "x:y", "", 1, SHEET, (0, 0), 0, "", pins, Box(0, 0, 1, 1), [], False
        )
    for w in range(200):
        a = pt()
        kind = rng.random()
        if kind < 0.45:
            b = (a[0] + rng.randint(-30, 30) * GRID, a[1])
        elif kind < 0.9:
            b = (a[0], a[1] + rng.randint(-30, 30) * GRID)
        else:
            b = (a[0] + rng.randint(1, 9) * GRID, a[1] + rng.randint(1, 9) * GRID)
        pts.append(b)
        scene.wires[f"w-{w}"] = WireInfo(f"w-{w}", a, b)
    for i in range(40):
        scene.labels[f"l-{i}"] = LabelInfo(f"l-{i}", f"N{i}", pt(), "label")
    for i in range(40):
        scene.junctions[f"j-{i}"] = pt()
    for i in range(20):
        scene.no_connects[f"nc-{i}"] = pt()
    return scene, pts


def _queries(rng: random.Random, pts: list[Point]) -> list[Point]:
    out: list[Point] = []
    for p in pts:
        out.append(p)
        for _ in range(2):  # inside/outside the 0.01 mm tolerance, across cell edges
            dx, dy = rng.choice([0.0, 0.004, -0.009, 0.011, -0.02]), rng.choice([0.0, 0.009])
            out.append((p[0] + dx, p[1] + dy))
    out += [(rng.uniform(-5, 160), rng.uniform(-5, 160)) for _ in range(500)]
    out += [(x * GRID, y * GRID) for x in range(0, 121, 3) for y in range(0, 121, 3)]
    return out


def test_index_matches_scene_items_at() -> None:
    for seed in range(3):
        rng = random.Random(seed)
        scene, pts = _scene(rng)
        index = ConnectionIndex(scene)
        hits = 0
        for q in _queries(rng, pts):
            want = scene.items_at(q)
            assert index.items_at(q) == want, q
            hits += bool(want)
        assert hits > 500  # the comparison is not vacuous


def test_clashes_on_a_500_pin_part_is_indexed() -> None:
    rng = random.Random(7)
    scene, _ = _scene(rng)
    spots = [(rng.randint(0, 120) * GRID, rng.randint(0, 120) * GRID) for _ in range(500)]
    pins = [_pin("new", str(i), at) for i, at in enumerate(spots)]
    lay = Layout(None, (0.0, 0.0), 0, "", pins, Box(-500, -500, -499, -499), None, [])  # type: ignore[arg-type]
    want = []
    for pin in pins:  # the unindexed reference
        items = scene.items_at(pin.at)
        if items:
            kinds = ", ".join(sorted({i["kind"] for i in items}))
            want.append(f"pin {pin.number} at {_fmt(pin.at)} would land on a {kinds}")

    def scan(_pt: object) -> list[dict[str, str]]:
        raise AssertionError("a 500-pin part must use the connection index, not a scan per pin")

    scene.items_at = scan  # type: ignore[method-assign]
    got = clashes(scene, lay)
    assert got == want and got
