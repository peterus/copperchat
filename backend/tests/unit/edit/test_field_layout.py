"""Field layout, free-space search and annotation planning (E4), pure data - no KiCad."""

from __future__ import annotations

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.annotate_tool import components, plan_renames
from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.fields import FieldSpot, field_layout, normalize_fields, spot_box
from copperchat.kicad_tools.edit.geometry import Box, PinEnd, on_grid
from copperchat.kicad_tools.edit.scene import LabelInfo, Scene, SymbolInfo, WireInfo
from copperchat.kicad_tools.edit.space import (
    find_space,
    pin_corridor,
    search_obstacles,
    unconnected_pins,
)

ROOT = SheetRef("/r", "1", "root.kicad_sch", "", "/")
SUB2 = SheetRef("/r/a", "2", "sub.kicad_sch", "sub", "/sub/")
SUB3 = SheetRef("/r/b", "3", "sub.kicad_sch", "sub2", "/sub2/")


def _pin(at: tuple[float, float], direction: str, number: str = "1", uid: str = "u") -> PinEnd:
    return PinEnd(("/r", uid, number), "R1", number, "", "passive", at, direction)  # type: ignore[arg-type]


def _sym(
    uid: str,
    ref: str,
    body: Box,
    pins: list[PinEnd],
    *,
    sheet: SheetRef = ROOT,
    unit: int = 1,
    lib_id: str = "Device:R",
    at: tuple[float, float] | None = None,
) -> SymbolInfo:
    centre = at or ((body.x0 + body.x1) / 2, (body.y0 + body.y1) / 2)
    return SymbolInfo(uid, ref, lib_id, "", unit, sheet, centre, 0, "", pins, body, [], False)


# -- field layout -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "dirs", "side"),
    [
        (Box(0, 0, 10, 4), {"up", "down"}, "right"),  # wide, pins top/bottom -> right
        (Box(0, 0, 10, 4), {"left", "right"}, "up"),  # wide resistor (horizontal)
        (Box(0, 0, 2, 8), {"up", "down"}, "right"),  # tall resistor (vertical)
        (Box(0, 0, 2, 8), {"up", "down", "right"}, "left"),
        (Box(0, 0, 8, 8), {"up", "down", "left", "right"}, "corner"),
    ],
)
def test_fields_readable(body: Box, dirs: set[str], side: str) -> None:
    ref, val = field_layout(body, dirs, 0, ("R12", "10k"))
    boxes = [spot_box(ref, "R12"), spot_box(val, "10k")]
    assert ref.angle == val.angle == 0.0
    for b in boxes:
        assert not b.overlaps(body), (side, b, body)
    assert not boxes[0].overlaps(boxes[1])
    if side == "up":
        assert all(b.y1 <= body.y0 for b in boxes)
    elif side == "right":
        assert all(b.x0 >= body.x1 for b in boxes)
    elif side == "left":
        assert all(b.x1 <= body.x0 for b in boxes)
    elif side == "corner":
        assert all(b.x0 >= body.x1 and b.y1 <= body.y0 for b in boxes)
    assert boxes[0].y0 < boxes[1].y0  # Reference above Value


def test_rotated_fields_are_compensated() -> None:
    ref, val = field_layout(Box(0, 0, 10, 2), {"left", "right"}, 90, ("R1", "1k"))
    assert ref.angle == val.angle == 90.0  # relative to the symbol: horizontal as displayed


def test_normalize_fields_maps_aliases() -> None:
    out = normalize_fields({"value": "10k", "mpn": "X1", "Manufacturer": "Y", "Tol": "1%"})
    assert out == {"Value": "10k", "MPN": "X1", "Manufacturer": "Y", "Tol": "1%"}


@pytest.mark.parametrize(
    "raw",
    [{"reference": "R1"}, {"": "x"}, {"a": "x\ny"}, {"value": "1", "Value": "2"}, ["x"]],
)
def test_normalize_fields_refuses(raw: object) -> None:
    with pytest.raises(StructuredToolError) as exc:
        normalize_fields(raw)
    assert exc.value.code == "invalid_argument"


# -- free space ---------------------------------------------------------------------------

SHAPE = [Box(-2, -4, 2, 4)]  # a 4 x 8 mm part centred on its origin


def test_find_space_free_sheet_starts_at_anchor() -> None:
    scene = Scene(ROOT)
    found = find_space(scene, SHAPE, (50.8, 50.8))
    assert found[0].at == (50.8, 50.8)
    assert len(found) == 3
    assert all(on_grid(c.at) for c in found)
    assert [c.distance for c in found] == sorted(c.distance for c in found)


def test_find_space_avoids_bodies_wires_and_corridors() -> None:
    pin = _pin((60.96, 50.8), "right", uid="u1")
    scene = Scene(ROOT)
    scene.symbols["u1"] = _sym("u1", "U1", Box(40.64, 40.64, 60.96, 60.96), [pin])
    scene.wires["w"] = WireInfo("w", (40.0, 70.0), (90.0, 70.0))
    found = find_space(scene, SHAPE, (50.8, 50.8))
    obstacles = search_obstacles(scene)
    for c in found:
        boxes = [Box(b.x0 + c.at[0], b.y0 + c.at[1], b.x1 + c.at[0], b.y1 + c.at[1]) for b in SHAPE]
        assert not any(b.overlaps(o) for b in boxes for o in obstacles)
        assert not any(b.overlaps(pin_corridor(pin)) for b in boxes)


def test_find_space_direction_side_only() -> None:
    found = find_space(Scene(ROOT), SHAPE, (50.8, 50.8), direction="right")
    assert found and all(c.at[0] > 50.8 for c in found)


def test_find_space_keeps_page_margin() -> None:
    found = find_space(Scene(ROOT), SHAPE, (0.0, 0.0))
    assert found and all(c.at[0] - 2 >= 10 and c.at[1] - 4 >= 10 for c in found)


# -- annotation planning ------------------------------------------------------------------


def _unit(
    uid: str,
    ref: str,
    sheet: SheetRef,
    at: tuple[float, float],
    unit: int = 1,
    lib_id: str = "Device:R",
) -> SymbolInfo:
    return _sym(
        uid,
        ref,
        Box(at[0] - 1, at[1] - 1, at[0] + 1, at[1] + 1),
        [],
        sheet=sheet,
        unit=unit,
        lib_id=lib_id,
        at=at,
    )


def test_keep_numbers_unannotated_and_duplicates_only() -> None:
    units = [
        _unit("a", "R1", ROOT, (10, 10)),
        _unit("b", "R?", ROOT, (20, 10)),
        _unit("c", "R1", ROOT, (30, 10)),  # duplicate of a (later position)
        _unit("d", "R5", ROOT, (40, 10)),
    ]
    out = plan_renames(units, ["1"], scope="all", mode="keep", sheet=None, placed=set(), block=100)
    assert {r.component.units[0].uuid: r.new for r in out} == {"b": "R6", "c": "R7"}


def test_multi_unit_component_keeps_one_ref() -> None:
    lm = "Amplifier_Operational:LM358"
    units = [
        _unit("u1", "U299", SUB2, (10, 10), 1, lm),
        _unit("u2", "U299", SUB2, (30, 10), 2, lm),
        _unit("r", "R205", SUB2, (5, 5)),
    ]
    comps, dupes = components(units)
    assert not dupes and len(comps) == 2
    out = plan_renames(
        units, ["1", "2", "3"], scope="all", mode="renumber", sheet=None, placed=set(), block=100
    )
    news = {u.uuid: r.new for r in out for u in r.component.units}
    assert news == {"u1": "U201", "u2": "U201", "r": "R201"}


def test_renumber_per_sheet_by_position() -> None:
    units = [
        _unit("x", "R301", SUB2, (50, 10)),
        _unit("y", "R201", SUB3, (10, 10)),
        _unit("z", "R202", SUB2, (10, 10)),
        _unit("w", "R203", SUB2, (90, 10)),
        _unit("v", "R302", SUB3, (90, 10)),
    ]
    out = plan_renames(
        units, ["1", "2", "3"], scope="all", mode="renumber", sheet=None, placed=set(), block=100
    )
    news = {r.component.units[0].uuid: r.new for r in out}
    assert news == {"z": "R201", "x": "R202", "y": "R301"}


def test_power_duplicates_get_unique_numbers() -> None:
    units = [
        _unit("p1", "#PWR01", ROOT, (10, 10), lib_id="power:GND"),
        _unit("p2", "#PWR01", ROOT, (20, 10), lib_id="power:GND"),
        _unit("p3", "#PWR02", ROOT, (30, 10), lib_id="power:GND"),
    ]
    out = plan_renames(units, ["1"], scope="all", mode="keep", sheet=None, placed=set(), block=100)
    assert [(r.component.units[0].uuid, r.new) for r in out] == [("p2", "#PWR03")]


def test_renumber_keeps_a_flat_scheme() -> None:
    units = [
        _unit("a", "R2", SUB2, (10, 10)),
        _unit("b", "R1", SUB3, (10, 10)),
        _unit("c", "R3", ROOT, (10, 10)),
    ]
    out = plan_renames(
        units, ["1", "2", "3"], scope="all", mode="renumber", sheet=None, placed=set(), block=100
    )
    news = {r.component.units[0].uuid: r.new for r in out}
    assert news == {"c": "R1", "b": "R3"}  # flat stays flat (R2 already in place)


def test_text_validation_rejects_control_characters() -> None:
    for bad in ("10k\r", "a\tb", "x\x00", "\ud800"):
        with pytest.raises(StructuredToolError):
            normalize_fields({"value": bad})


def _scene_with_pin(at: tuple[float, float] = (50.8, 50.8)) -> tuple[Scene, PinEnd]:
    pin = _pin(at, "right", uid="u1")
    scene = Scene(ROOT)
    scene.symbols["u1"] = _sym("u1", "R1", Box(40, 45, 50.8, 55), [pin])
    return scene, pin


def test_unconnected_pin_lone() -> None:
    scene, pin = _scene_with_pin()
    assert unconnected_pins(scene) == [pin]


@pytest.mark.parametrize(
    "kind", ["wire_end", "wire_interior", "label", "junction", "no_connect", "other_pin"]
)
def test_connected_pin_kinds(kind: str) -> None:
    scene, pin = _scene_with_pin()
    x, y = pin.at
    if kind == "wire_end":
        scene.wires["w"] = WireInfo("w", (x, y), (x + 10, y))
    elif kind == "wire_interior":
        scene.wires["w"] = WireInfo("w", (x, y - 5), (x, y + 5))
    elif kind == "label":
        scene.labels["l"] = LabelInfo("l", "NET", (x, y), "label")
    elif kind == "junction":
        scene.junctions["j"] = (x, y)
    elif kind == "no_connect":
        scene.no_connects["n"] = (x, y)
    else:
        other = _pin((x, y), "left", uid="u2")
        scene.symbols["u2"] = _sym("u2", "R2", Box(50.8, 45, 60, 55), [other])
    assert pin not in unconnected_pins(scene)


def test_wire_passing_beside_pin_leaves_it_free() -> None:
    scene, pin = _scene_with_pin()
    x, y = pin.at
    scene.wires["w"] = WireInfo("w", (x + 1.27, y - 5), (x + 1.27, y + 5))
    scene.wires["h"] = WireInfo("h", (x + 2.54, y), (x + 10, y))  # same line, not touching
    assert unconnected_pins(scene) == [pin]


def test_find_space_empty_scene_is_fast_and_nearest_first() -> None:
    import time

    t0 = time.perf_counter()
    found = find_space(Scene(ROOT), SHAPE, (101.6, 101.6), limit=3)
    assert time.perf_counter() - t0 < 0.2
    assert len(found) == 3 and found[0].distance == 0.0
    assert all(c.distance <= 2 * 2 * 2.54 + 1e-6 for c in found)


def test_field_name_must_be_utf8_and_limit_zero_is_empty() -> None:
    with pytest.raises(StructuredToolError):
        normalize_fields({"\ud800x": "a"})
    assert find_space(Scene(ROOT), SHAPE, (50.8, 50.8), limit=0) == []


def test_overlap_check_reports_field_over_own_pins() -> None:
    from copperchat.kicad_tools.edit.place import Layout, clashes

    body = Box(0, 0, 6.35, 35.56)  # body + pins of a connector
    spots = (FieldSpot((8, 1), 0.0), FieldSpot((3, 10), 0.0))
    fine = Layout(
        None,
        (0, 0),
        0,
        "",
        [],
        body,
        spots,
        [Box(7, 0, 9, 2), Box(7, 3, 18, 5)],  # type: ignore[arg-type]
    )
    bad = Layout(None, (0, 0), 0, "", [], body, spots, [Box(7, 0, 9, 2), Box(0, 8, 11, 10)])  # type: ignore[arg-type]
    assert clashes(Scene(ROOT), fine, own_fields=True) == []
    assert any("own body or pins" in p for p in clashes(Scene(ROOT), bad, own_fields=True))
    assert clashes(Scene(ROOT), bad) == []  # a moved part keeps its author's layout
