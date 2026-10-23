"""Spots for the hierarchical labels `sch_add_sheet_pin` puts into a sub-sheet (#5)."""

from __future__ import annotations

import itertools

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.labels import label_extent
from copperchat.kicad_tools.edit.scene import LabelInfo, Scene, WireInfo
from copperchat.kicad_tools.edit.sheets import (
    LABEL_BOTTOM,
    LABEL_COLUMN_STEP,
    LABEL_TOP,
    PIN_PITCH,
    _label_spot,
)

SUB = SheetRef("/r/s", "2", "power.kicad_sch", "Power", "/Power/")
SPIN_LEFT = 1  # SchematicLabelSpinStyle.SLSS_LEFT, as build_hier_label sets it


def _fill(scene: Scene, count: int, prefix: str = "SIG_") -> list[tuple[float, float]]:
    """Place `count` labels the way sch_add_sheet_pin does, one after the other."""
    spots = []
    for i in range(count):
        name = f"{prefix}{i:02d}"
        at = _label_spot(scene, name)
        scene.labels[name] = LabelInfo(name, name, at, "hierarchical_label", SPIN_LEFT)
        spots.append(at)
    return spots


def test_labels_stack_on_the_grid_pitch() -> None:
    spots = _fill(Scene(sheet=SUB), 20)
    assert len({x for x, _y in spots}) == 1
    ys = [y for _x, y in spots]
    assert ys == [pytest.approx(LABEL_TOP + i * PIN_PITCH) for i in range(20)]


def test_label_boxes_never_overlap() -> None:
    scene = Scene(sheet=SUB)
    _fill(scene, 30)
    boxes = [label_extent(lb) for lb in scene.labels.values()]
    assert not any(a.overlaps(b) for a, b in itertools.combinations(boxes, 2))


def test_full_column_opens_the_next_one() -> None:
    scene = Scene(sheet=SUB)
    rows = int((LABEL_BOTTOM - LABEL_TOP) / PIN_PITCH + 1e-6) + 1
    spots = _fill(scene, rows + 3)
    first_x = spots[0][0]
    assert {x for x, _y in spots[:rows]} == {first_x}
    assert [x for x, _y in spots[rows:]] == [pytest.approx(first_x + LABEL_COLUMN_STEP)] * 3
    assert spots[rows][1] == pytest.approx(LABEL_TOP)


def test_long_name_moves_its_column_right() -> None:
    at = _label_spot(Scene(sheet=SUB), "A_VERY_LONG_HIERARCHICAL_SIGNAL_NAME")
    assert at[0] > LABEL_TOP


def test_wire_through_a_spot_is_avoided() -> None:
    scene = Scene(sheet=SUB)
    x, y = _label_spot(scene, "A")
    scene.wires["w"] = WireInfo("w", (x - 10, y), (x + 10, y))
    assert _label_spot(scene, "A")[1] > y


def test_no_room_left_fails_clearly() -> None:
    scene = Scene(sheet=SUB)
    for x in range(200):  # a vertical wire every mm across the page
        scene.wires[f"v{x}"] = WireInfo(f"v{x}", (float(x), 0.0), (float(x), 200.0))
    with pytest.raises(StructuredToolError) as err:
        _label_spot(scene, "A")
    assert err.value.code == "overlap"
