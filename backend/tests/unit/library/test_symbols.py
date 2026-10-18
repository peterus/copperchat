"""Symbol protos built in code (the real-library cases run in tests/kicad/edit)."""

from __future__ import annotations

from kipy.proto.schematic import schematic_types_pb2 as st

from copperchat.library.symbols import (
    body_size_mm,
    detail_from_proto,
    has_drawing,
    pin_directions_by_rotation,
    pins_from_proto,
    row_from_proto,
)

MM = 1_000_000
PO = st.SchematicPinOrientation


def _pin(
    sy: st.SchematicSymbol,
    number: str,
    x: float,
    y: float,
    orient: int,
    unit: int = 1,
    style: int = 1,
) -> None:
    pin = st.SchematicPin(number=number, name=f"P{number}", orientation=orient)  # type: ignore[arg-type]
    pin.position.x_nm, pin.position.y_nm = int(x * MM), int(y * MM)
    child = sy.items.add()
    child.item.Pack(pin)
    if unit:
        child.unit.unit = unit
    if style:
        child.body_style.style = style


def _rect(
    sy: st.SchematicSymbol, x0: float, y0: float, x1: float, y1: float, unit: int = 1
) -> None:
    shape = st.SchematicGraphicShape()
    shape.shape.rectangle.top_left.x_nm, shape.shape.rectangle.top_left.y_nm = (
        int(x0 * MM),
        int(y0 * MM),
    )
    r = shape.shape.rectangle.bottom_right
    r.x_nm, r.y_nm = int(x1 * MM), int(y1 * MM)
    child = sy.items.add()
    child.item.Pack(shape)
    child.unit.unit = unit


def _led() -> st.SchematicSymbol:
    sy = st.SchematicSymbol()
    sy.id.library_nickname, sy.id.entry_name = "Device", "LED"
    sy.description_field.text.text = "Light emitting diode"
    sy.reference_field.text.text = "D"
    sy.unit_count = 1
    sy.body_style.add(name="?")
    _pin(sy, "1", -3.81, 0, PO.SPO_RIGHT)  # shape extends right: pin on the left side
    _pin(sy, "2", 3.81, 0, PO.SPO_LEFT)
    _rect(sy, -1.27, -1.27, 1.27, 1.27)
    return sy


def test_led_pins_and_directions() -> None:
    sy = _led()
    pins = pins_from_proto(sy)
    assert [(p.number, p.direction, p.at_mm) for p in pins] == [
        ("1", "left", (-3.81, 0.0)),
        ("2", "right", (3.81, 0.0)),
    ]
    dirs = pin_directions_by_rotation(pins)
    assert dirs == {
        "0": {"1": "left", "2": "right"},
        "90": {"1": "down", "2": "up"},
        "180": {"1": "right", "2": "left"},
        "270": {"1": "up", "2": "down"},
    }
    assert body_size_mm(sy) == (7.62, 2.54)
    d = detail_from_proto(sy)
    assert d.reference_prefix == "D" and d.body_styles == 1 and not d.is_power
    assert d.to_dict()["pin_count"] == 2


def test_dedup_per_unit_and_number_and_body_style() -> None:
    sy = st.SchematicSymbol()
    sy.id.library_nickname, sy.id.entry_name = "74xx", "X"
    sy.unit_count = 2
    for style in (1, 2):
        _pin(sy, "1", -5, 0, PO.SPO_RIGHT, unit=1, style=style)
        _pin(sy, "2", -5, 0, PO.SPO_RIGHT, unit=2, style=style)
    _pin(sy, "14", 0, -5, PO.SPO_DOWN, unit=0, style=0)
    pins = pins_from_proto(sy)
    assert [(p.unit, p.number, p.body_style) for p in pins] == [
        (0, "14", 0),
        (1, "1", 1),
        (2, "2", 1),
    ]
    assert pin_directions_by_rotation(pins) is not None  # 3 pins
    assert row_from_proto(sy, "global").pin_count == 3


def test_more_than_four_pins_no_directions() -> None:
    sy = st.SchematicSymbol()
    for i in range(5):
        _pin(sy, str(i + 1), -5, i * 2.54, PO.SPO_RIGHT)
    assert pin_directions_by_rotation(pins_from_proto(sy)) is None


def test_derived_symbol_has_no_drawing() -> None:
    sy = st.SchematicSymbol()
    sy.id.library_nickname, sy.id.entry_name = "MCU", "Derived"
    assert not has_drawing(sy)
    assert row_from_proto(sy, "project").pin_count is None
    assert body_size_mm(sy) == (0.0, 0.0)
    d = detail_from_proto(sy, pins_known=False)
    assert d.to_dict()["pin_count"] is None and d.pins == []
