"""`shift` (sch_shift_sheet): the generic proto translation, grid snapping, the page
frame and the centring math, without KiCad."""

from __future__ import annotations

import io
from typing import Any

import pytest
from kipy.proto.common.types import base_types_pb2 as bt
from kipy.proto.schematic import schematic_types_pb2 as st
from kipy.schematic_types import (
    BusEntry,
    GlobalLabel,
    Group,
    Junction,
    SchematicGraphicShape,
    SchematicImage,
    SchematicLine,
    SchematicText,
    SchematicTextBox,
    SheetSymbol,
)

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.geometry import NM, Box, on_grid
from copperchat.kicad_tools.edit.sheets import build_sheet_pin, build_sheet_symbol
from copperchat.kicad_tools.edit.shift import (
    Page,
    item_boxes,
    item_points,
    page_of,
    plan_centring,
    plan_shift,
    read_page,
    shifted,
    snap_offset,
    translate_proto,
    union,
)

D = (2_540_000, -1_270_000)  # 2.54, -1.27 mm


def _set(v: Any, x: float, y: float) -> None:
    v.x_nm, v.y_nm = round(x * NM), round(y * NM)


def _symbol_proto() -> st.SchematicSymbolInstance:
    p = st.SchematicSymbolInstance()
    p.id.value = "sym"
    _set(p.position, 100, 50)
    _set(p.reference_field.text.position, 102, 48)
    _set(p.reference_field.text.attributes.size, 1.27, 1.27)
    _set(p.value_field.text.position, 102, 52)
    uf = p.user_fields.add()
    _set(uf.text.position, 90, 40)
    pin = st.SchematicPin()
    _set(pin.position, -3.81, 0)  # relative to the symbol origin
    p.definition.items.add().item.Pack(pin)
    return p


def test_symbol_moves_position_and_fields_not_definition_or_text_size() -> None:
    p = _symbol_proto()
    assert translate_proto(p, *D) == 4
    assert (p.position.x_nm, p.position.y_nm) == (102_540_000, 48_730_000)
    assert p.reference_field.text.position.x_nm == 104_540_000
    assert p.value_field.text.position.y_nm == 50_730_000
    assert p.user_fields[0].text.position.x_nm == 92_540_000
    assert p.reference_field.text.attributes.size.x_nm == 1_270_000  # an extent
    pin = st.SchematicPin()
    assert p.definition.items[0].item.Unpack(pin)
    assert pin.position.x_nm == -3_810_000  # relative frame


def test_sheet_moves_box_pins_and_fields_not_size() -> None:
    sym = build_sheet_symbol("amp", "amp.kicad_sch", (50.8, 50.8), (25.4, 20.32))
    pin = build_sheet_pin("IN", (50.8, 55.88), "left", "input")
    sym.pins = [pin]
    before = item_points(sym.proto)
    n = translate_proto(sym.proto, *D)
    assert n == len(before) >= 4  # position, pin + its text, name / file fields
    after = item_points(sym.proto)
    assert after == [pytest.approx((x + 2.54, y - 1.27)) for x, y in before]
    assert (sym.size.x, sym.size.y) == (25_400_000, 20_320_000)
    assert (sym.pins[0].position.x, sym.pins[0].position.y) == (53_340_000, 54_610_000)


def test_bus_entry_and_image_keep_their_extents() -> None:
    be = BusEntry()
    _set(be.proto.position, 10, 10)
    _set(be.proto.size, 2.54, 2.54)
    assert translate_proto(be.proto, *D) == 1
    assert be.proto.size.x_nm == 2_540_000
    img = SchematicImage()
    _set(img.proto.position, 20, 20)
    _set(img.proto.transform_origin_offset, 1, 1)
    assert translate_proto(img.proto, *D) == 1
    assert img.proto.transform_origin_offset.x_nm == 1_000_000


def test_shapes_text_box_and_table_cells_move_every_point() -> None:
    sh = st.SchematicGraphicShape()
    node = sh.shape.polygon.polygons.add().outline.nodes.add()
    _set(node.point, 1, 2)
    arc = sh.shape.polygon.polygons[0].outline.nodes.add().arc
    _set(arc.start, 1, 1)
    _set(arc.mid, 2, 2)
    _set(arc.end, 3, 1)
    assert translate_proto(sh, *D) == 4
    circle = st.SchematicGraphicShape()
    _set(circle.shape.circle.center, 10, 10)
    _set(circle.shape.circle.radius_point, 12, 10)
    assert translate_proto(circle, *D) == 2
    assert circle.shape.circle.radius_point.x_nm == 14_540_000
    tb = st.SchematicTextBox()
    _set(tb.textbox.top_left, 0, 0)
    _set(tb.textbox.bottom_right, 10, 5)
    _set(tb.textbox.attributes.size, 1.27, 1.27)
    assert translate_proto(tb, *D) == 2
    table = st.SchematicTable()
    for x in (0, 10):
        cell = table.cells.add()
        _set(cell.text_box.textbox.top_left, x, 0)
        _set(cell.text_box.textbox.bottom_right, x + 10, 5)
    assert translate_proto(table, *D) == 4


def test_snap_offset_rounds_to_the_grid() -> None:
    assert snap_offset((1.0, -0.7)) == (1.27, -1.27)
    assert snap_offset((0.5, 0.6)) == (0.0, 0.0)
    assert snap_offset((-0.3, 0.0)) == (0.0, 0.0)
    d = snap_offset((123.4, -56.7))
    assert on_grid(d) and abs(d[0] - 123.4) <= 0.635 and abs(d[1] + 56.7) <= 0.635
    assert str(snap_offset((-0.1, 0.1))[0]) == "0.0"  # no -0.0 in a result


def _settings(size: str, orientation: str = "PO_LANDSCAPE", user: tuple[float, float] = (0, 0),
              sheet: str = "") -> Any:  # fmt: skip
    from kipy.common_types import PageSettings

    p = bt.PageSettings()
    p.page_size = bt.PageSize.Value(size)
    p.orientation = bt.PageOrientation.Value(orientation)
    _set(p.user_page_size, *user)
    p.drawing_sheet = sheet
    return PageSettings(p)


def test_read_page_sizes_orientation_and_frame() -> None:
    a4 = read_page(_settings("PS_A4"))
    assert (a4.name, a4.paper) == ("A4", Box(0, 0, 297, 210))
    assert a4.frame == Box(10, 10, 287, 200)
    assert a4.title_block == Box(177, 166, 287, 200)
    assert not a4.custom_sheet
    portrait = read_page(_settings("PS_A3", "PO_PORTRAIT"))
    assert portrait.paper == Box(0, 0, 297, 420)
    user = read_page(_settings("PS_USER", user=(500, 300), sheet="my.kicad_wks"))
    assert (user.name, user.paper, user.custom_sheet) == ("User", Box(0, 0, 500, 300), True)


A4: Page = page_of("A4", 297, 210)


def _centre(b: Box) -> tuple[float, float]:
    return ((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2)


def test_small_content_glued_to_the_corner_is_centred_in_the_frame() -> None:
    content = Box(0.5, 1.0, 60.5, 41.0)
    c = plan_centring(content, A4)
    assert c.area == "frame" and c.clear_of_title_block and c.fits_frame
    assert on_grid(c.offset)
    cx, cy = _centre(shifted(content, c.offset))
    assert abs(cx - 148.5) <= 0.64 and abs(cy - 105) <= 0.64


def test_wide_content_goes_above_the_title_block() -> None:
    content = Box(0, 0, 250, 140)  # centred (y 35..175) it would overlap y >= 166
    c = plan_centring(content, A4)
    assert c.area == "above the title block" and c.clear_of_title_block
    moved = shifted(content, c.offset)
    assert moved.y1 <= A4.title_block.y0 + 1e-6 and moved.y0 >= A4.frame.y0 - 1e-6
    assert abs(_centre(moved)[0] - 148.5) <= 0.64  # still centred horizontally


def test_tall_content_goes_left_of_the_title_block() -> None:
    content = Box(0, 0, 150, 185)  # too tall for above (156), narrow enough for left (167)
    c = plan_centring(content, A4)
    assert c.area == "left of the title block" and c.clear_of_title_block
    moved = shifted(content, c.offset)
    assert moved.x1 <= A4.title_block.x0 + 1e-6 and moved.x0 >= A4.frame.x0 - 1e-6


def test_content_that_cannot_avoid_the_title_block_is_centred_over_it() -> None:
    content = Box(0, 0, 250, 185)
    c = plan_centring(content, A4)
    assert (c.area, c.clear_of_title_block, c.fits_frame) == ("frame", False, True)
    assert abs(_centre(shifted(content, c.offset))[0] - 148.5) <= 0.64


def test_content_larger_than_the_frame_is_flagged() -> None:
    c = plan_centring(Box(0, 0, 285, 150), A4)
    assert not c.fits_frame


def test_snapping_never_pushes_fitting_content_out_of_the_frame() -> None:
    content = Box(3.3, 0.0, 3.3 + 275.5, 50)  # 1.5 mm narrower than the frame
    c = plan_centring(content, A4)
    moved = shifted(content, c.offset)
    assert moved.x0 >= A4.frame.x0 - 1e-6 and moved.x1 <= A4.frame.x1 + 1e-6


def test_plan_shift_counts_skips_groups_and_refuses_items_without_points() -> None:
    w = SchematicLine()
    w.proto.id.value = "w1"
    w.proto.type = st.SchematicLineType.SLT_WIRE
    _set(w.proto.start, 0, 0)
    _set(w.proto.end, 10, 0)
    g = Group()
    g.proto.id.value = "g1"
    plan = plan_shift([w, g], (1.27, 0))
    assert plan.counts == {"wire": 1} and plan.skipped == {"group": 1}
    assert plan.items[0].proto.start.x_nm == 1_270_000
    assert w.proto.start.x_nm == 0  # the read item is untouched (a copy is shifted)
    empty = Junction()
    empty.proto.id.value = "j1"
    with pytest.raises(StructuredToolError) as err:
        plan_shift([w, empty], (1.27, 0))
    assert err.value.details is not None and "j1" in str(err.value.details["unmovable"])


def test_item_boxes_cover_texts_labels_and_shapes() -> None:
    t = SchematicText()
    _set(t.proto.text.position, 50, 50)
    t.proto.text.text = "NOTE: hello"
    _set(t.proto.text.attributes.size, 1.27, 1.27)
    t.proto.text.attributes.horizontal_alignment = 1  # left
    t.proto.text.attributes.vertical_alignment = 3  # bottom
    (tb,) = item_boxes(t)
    assert tb.x0 == pytest.approx(50) and tb.x1 > 60 and tb.y1 == pytest.approx(50)
    gl = GlobalLabel()
    _set(gl.proto.position, 20, 20)
    gl.proto.text.text = "SDA"
    gl.proto.spin_style = 3  # text runs right
    box = union(item_boxes(gl))
    assert box is not None and box.x0 == pytest.approx(20) and box.x1 > 22
    box2 = SchematicTextBox()
    _set(box2.proto.textbox.top_left, 1, 2)
    _set(box2.proto.textbox.bottom_right, 11, 7)
    assert item_boxes(box2) == [Box(1, 2, 11, 7)]
    c = SchematicGraphicShape()
    _set(c.proto.shape.circle.center, 10, 10)
    _set(c.proto.shape.circle.radius_point, 15, 10)
    (cb,) = item_boxes(c)
    assert (cb.x0, cb.y0, cb.x1, cb.y1) == pytest.approx((5, 5, 15, 15))
    s = SheetSymbol()
    _set(s.proto.position, 30, 30)
    _set(s.proto.size, 20, 10)
    assert union(item_boxes(s)) == Box(30, 30, 50, 40)


def test_a_reversed_rectangle_still_counts() -> None:
    r = SchematicGraphicShape()
    _set(r.proto.shape.rectangle.top_left, 100, 100)  # drawn right-to-left
    _set(r.proto.shape.rectangle.bottom_right, 50, 50)
    (box,) = item_boxes(r)
    assert (box.x0, box.y0, box.x1, box.y1) == pytest.approx((50, 50, 100, 100))
    assert union([Box(60, 60, 70, 70), Box(100, 100, 50, 50)]) == Box(50, 50, 100, 100)


def test_an_ellipse_counts_with_its_radius() -> None:
    e = SchematicGraphicShape()
    _set(e.proto.shape.ellipse.center, 40, 40)
    e.proto.shape.ellipse.major_radius.value_nm = 10 * NM
    e.proto.shape.ellipse.minor_radius.value_nm = 4 * NM
    assert item_boxes(e) == [Box(30, 30, 50, 50)]


def _image(data: bytes, scale: float = 1.0) -> SchematicImage:
    img = SchematicImage()
    _set(img.proto.position, 100, 100)
    img.image_scale = scale
    img.image_data = data
    return img


def test_image_extent_from_pixels_dpi_and_scale() -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (300, 150)).save(buf, format="PNG", dpi=(300, 300))
    (box,) = item_boxes(_image(buf.getvalue(), scale=2.0))  # 25.4 x 12.7 mm, doubled
    assert (box.x0, box.y0, box.x1, box.y1) == pytest.approx((74.6, 87.3, 125.4, 112.7), abs=1e-3)
    (neg,) = item_boxes(_image(buf.getvalue(), scale=-1.0))  # a bad scale counts as 1
    assert neg.width == pytest.approx(25.4, abs=1e-3)


def test_an_undecodable_image_is_its_anchor_and_named() -> None:
    unknown: list[str] = []
    img = _image(b"%!PS-Adobe-3.0 EPSF not a bitmap")
    img.proto.id.value = "img1"
    assert item_boxes(img, unknown) == [Box(100, 100, 100, 100)]
    assert unknown == ["image img1"]
