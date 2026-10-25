"""`orient` (v0.5.0 #2 / #20): frames, label spin, sheet rotation as KiCad's
`SCH_SHEET::Rotate` / `Mirror*` / `AutoplaceFields` do it."""

from __future__ import annotations

import pytest

from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.geometry import Box, on_grid
from copperchat.kicad_tools.edit.labels import _v2, make_label
from copperchat.kicad_tools.edit.orient import (
    Frame,
    decompose,
    label_matrix,
    matrix_of,
    sheet_center,
    turn_label,
    turn_sheet,
)
from copperchat.kicad_tools.edit.sheets import build_sheet_pin

CCW = matrix_of(90, "")


def test_frame_rotates_about_old_and_lands_on_new() -> None:
    f = Frame((10.0, 10.0), (20.0, 10.0), CCW)
    assert f((12.54, 10.0)) == (20.0, 7.46)  # right of the anchor -> above it (y down)
    assert f.dir((1, 0)) == (0, -1)
    assert Frame((0.0, 0.0), (1.27, 0.0)).rigid


@pytest.mark.parametrize(
    ("rotation", "mirror"), [(0, ""), (90, ""), (180, ""), (270, ""), (0, "x"), (90, "x"), (0, "y")]
)
def test_decompose_round_trips(rotation: int, mirror: str) -> None:
    from copperchat.kicad_tools.edit.geometry import canonical_transform, transform_matrix

    m = matrix_of(rotation, mirror)  # type: ignore[arg-type]
    assert transform_matrix(*canonical_transform(*decompose(m))) == m


@pytest.mark.parametrize(
    ("spin", "rotation", "mirror", "new_dir"),
    [
        (3, 90, None, (0, -1)),  # RIGHT, absolute 90 -> UP
        (3, 180, None, (-1, 0)),
        (2, 0, None, (1, 0)),  # UP -> 0 = RIGHT
        (3, None, "y", (-1, 0)),  # left-right flip
        (3, None, "x", (1, 0)),  # up-down flip of a horizontal label: unchanged
        (2, None, "x", (0, 1)),
        (3, 90, "x", (0, 1)),  # 90 then upside down
    ],
)
def test_label_matrix(spin: int, rotation: int | None, mirror: str | None, new_dir: tuple) -> None:
    from copperchat.kicad_tools.edit.orient import SPIN_DIR, turn

    m = label_matrix(spin, rotation, mirror)  # type: ignore[arg-type]
    assert turn(m, SPIN_DIR[spin]) == new_dir


def test_turn_label_about_anchor_and_spin() -> None:
    lb = make_label("SDA", (50.8, 50.8), (1, 0), "local")
    new_dir = turn_label(lb, Frame((50.8, 50.8), (50.8, 50.8), label_matrix(3, 270, None)))
    assert new_dir == (0, 1)
    assert int(lb.spin_style) == 4  # BOTTOM
    assert (lb.position.x, lb.position.y) == (50_800_000, 50_800_000)


def test_turn_global_label_intersheet_refs_follow_spin() -> None:
    lb = make_label("CLK", (50.8, 50.8), (1, 0), "global")
    lb.fields_autoplaced = True
    lb.intersheet_refs_field.text.position = _v2((58.0, 50.8))  # 7.2 mm along the text
    turn_label(lb, Frame((50.8, 50.8), (50.8, 50.8), label_matrix(3, 180, None)))
    f = lb.intersheet_refs_field
    assert (f.text.position.x, f.text.position.y) == (43_600_000, 50_800_000)
    from kipy.proto.common.types.enums_pb2 import HA_RIGHT

    assert f.text.attributes.horizontal_alignment == HA_RIGHT


@pytest.mark.parametrize(
    ("box", "m", "c"),
    [
        (Box(0, 0, 13.97, 13.97), CCW, (6.985, 6.985)),  # both half: fine
        (Box(0, 0, 12.7, 10.16), CCW, (6.35, 5.08)),  # both whole
        (Box(0, 0, 13.97, 10.16), CCW, (6.985, 5.715)),  # mixed: moved by half a step
        (Box(0, 0, 13.97, 10.16), matrix_of(0, "y"), (6.985, 5.08)),  # a mirror is fine
    ],
)
def test_sheet_center_keeps_the_grid(box: Box, m: tuple, c: tuple) -> None:
    got = sheet_center(box, m)
    assert got == c
    f = Frame(got, got, m)
    assert on_grid(f((box.x0, box.y0))) and on_grid(f((box.x1, box.y1)))


def _sheet(pins: list[tuple[str, tuple[float, float], str]]) -> object:
    from kipy.schematic_types import SheetSymbol

    s = SheetSymbol()
    s.proto.id.value = "11111111-1111-1111-1111-111111111111"
    s.position = _v2((157.48, 95.25))
    s.size = _v2((13.97, 13.97))
    s.fields_autoplaced = True
    s.name_field.text.value = "sub"
    s.name_field.text.position = _v2((157.48, 94.5384))
    s.name_field.text.attributes.size = _v2((1.27, 1.27))
    s.filename_field.text.value = "sub.kicad_sch"
    s.filename_field.text.position = _v2((157.48, 109.8046))
    s.pins = [build_sheet_pin(n, at, side, "input") for n, at, side in pins]
    return s


def _xy(v: object) -> tuple[float, float]:
    return (v.x / 1e6, v.y / 1e6)  # type: ignore[attr-defined]


def test_turn_sheet_ccw_like_kicad() -> None:
    """The eeschema reference sheet (tests/fixtures/reference/sheet_pins.kicad_sch,
    'in' left at y 101.6, 'out' right) turned 90 deg CCW as SCH_SHEET::Rotate does:
    about the half-grid centre (164.465, 102.235), the left pin goes to the bottom, the
    right one to the top; fields laid out vertically (pins only top / bottom)."""
    s = _sheet([("in", (157.48, 101.6), "left"), ("out", (171.45, 101.6), "right")])
    t = turn_sheet(s, CCW, (0.0, 0.0))
    assert (t.box.x0, t.box.y0, t.box.x1, t.box.y1) == (157.48, 95.25, 171.45, 109.22)
    sides = {p.text.value: (_xy(p.position), int(p.side), int(p.spin_style)) for p in s.pins}  # type: ignore[attr-defined]
    # bottom: SetSide(BOTTOM) -> spin UP (2); top: spin BOTTOM (4)
    assert sides["in"] == ((163.83, 109.22), 4, 2)
    assert sides["out"] == ((163.83, 95.25), 3, 4)
    name = s.name_field.text  # type: ignore[attr-defined]
    assert _xy(name.position) == (pytest.approx(156.7684), 109.22)
    assert name.attributes.angle == 90


def test_mirror_sheet_swaps_pin_sides_and_shifts_fields() -> None:
    s = _sheet([("in", (157.48, 101.6), "left")])
    t = turn_sheet(s, matrix_of(0, "y"), (0.0, 0.0))
    assert (t.box.x0, t.box.x1) == (157.48, 171.45)
    pin = s.pins[0]  # type: ignore[attr-defined]
    assert _xy(pin.position) == (171.45, 101.6) and int(pin.side) == 2
    assert t.pins[access.kiid(pin)] == ((171.45, 101.6), "right")
    assert _xy(s.name_field.text.position) == (157.48, 94.5384)  # type: ignore[attr-defined]


def test_turn_label_direction_override_lays_fields_out_for_it() -> None:
    """A label on a turned pin reads away from the pin: the override is the direction
    the autoplaced fields are laid out for (not a spin set afterwards)."""
    from kipy.proto.common.types.enums_pb2 import HA_RIGHT

    lb = make_label("CLK", (50.8, 50.8), (1, 0), "global")
    lb.fields_autoplaced = True
    lb.intersheet_refs_field.text.position = _v2((58.0, 50.8))
    frame = Frame((50.8, 50.8), (50.8, 50.8), matrix_of(90, ""))  # would turn it UP
    assert turn_label(lb, frame, (-1, 0)) == (-1, 0)
    assert int(lb.spin_style) == 1  # LEFT
    f = lb.intersheet_refs_field
    assert (f.text.position.x, f.text.position.y) == (43_600_000, 50_800_000)
    assert f.text.attributes.horizontal_alignment == HA_RIGHT
    assert f.text.attributes.angle == 0
