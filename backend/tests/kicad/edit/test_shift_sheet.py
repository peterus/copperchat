"""sch_shift_sheet through the real tool adapter: every item kind on the sheet moves by
the snapped offset, sub-sheets and the netlist stay as they were, centring, refusals."""

from __future__ import annotations

import io
import time
import uuid
from typing import Any

import pytest
from kipy.proto.schematic import schematic_types_pb2 as st
from kipy.schematic_types import (
    BusEntry,
    DirectiveLabel,
    GlobalLabel,
    Group,
    HierarchicalLabel,
    NoConnectMarker,
    SchematicGraphicShape,
    SchematicImage,
    SchematicLine,
    SchematicRuleArea,
    SchematicTable,
    SchematicText,
    SchematicTextBox,
)

from copperchat.db import Database
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.scene import resolve_sheet, sheet_api
from copperchat.kicad_tools.edit.shift import item_points, sheet_item_types
from tests.kicad.edit.connect_support import state
from tests.kicad.edit.helpers import local_label, v2, wire
from tests.support.edit import call_tool, edit_ctx, place_direct

pytestmark = pytest.mark.kicad

VERTICAL = "/ampli_ht_vertical/"
NM = 1_000_000


async def _ok(ctx: Any, tool: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, tool, **args)
    assert not err, __import__("json").dumps(out, indent=1)
    return out


async def _points(s: KicadSession, sheet: str | None = None) -> dict[str, list[Any]]:
    """uuid -> every absolute point of every item on the sheet (what a shift moves)."""

    def run(h: KicadHandle) -> dict[str, list[Any]]:
        sch = sheet_api(h, resolve_sheet(h, sheet))
        return {access.kiid(i): item_points(i.proto) for i in sch.get_items(sheet_item_types())}

    return await s.call(run)


def _assert_shifted(before: dict[str, list[Any]], after: dict[str, list[Any]], d: Any) -> None:
    assert set(after) == set(before)
    for uid, pts in before.items():
        want = [(round(x * NM) + round(d[0] * NM), round(y * NM) + round(d[1] * NM))
                for x, y in pts]  # fmt: skip
        got = [(round(x * NM), round(y * NM)) for x, y in after[uid]]
        assert got == want, uid


async def test_shift_by_moves_the_root_sheet_and_keeps_nets(
    hier_session: KicadSession, edit_db: Database
) -> None:
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    ids, partition = await state(s)
    root, child = await _points(s), await _points(s, VERTICAL)
    out = await _ok(ctx, "sch_shift_sheet", by=[3.0, -1.0])  # snapped to 2.54, -1.27
    assert out["offset_mm"] == [2.54, -1.27]
    assert out["connectivity_verified"] is True and out["logical_changes"] == 1
    assert any("snapped" in n for n in out["notes"])
    for kind in ("symbol", "power_symbol", "wire", "junction", "sheet", "no_connect"):
        assert out["shifted"].get(kind, 0) > 0, (kind, out["shifted"])
    assert out["modified_count"] == sum(out["shifted"].values())
    assert out["modified"] == [] and out["nets_touched"] == []  # compact on large sheets
    assert out["nets_touched_count"] > 0
    b0, b1 = out["bbox_before_mm"], out["bbox_after_mm"]
    assert b1[0] == pytest.approx(b0[0] + 2.54) and b1[3] == pytest.approx(b0[3] - 1.27)
    _assert_shifted(root, await _points(s), (2.54, -1.27))
    assert await _points(s, VERTICAL) == child  # sub-sheet contents untouched
    after_ids, after_partition = await state(s)
    assert after_partition == partition and after_ids == ids


async def test_center_a_shared_sub_sheet_then_it_is_a_no_op(
    hier_session: KicadSession, edit_db: Database
) -> None:
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    _ids, partition = await state(s)
    glued = await _ok(ctx, "sch_shift_sheet", sheet=VERTICAL, by=[-15.24, -12.7])
    assert any("2 sheet instances" in n for n in glued["notes"])
    before = await _points(s, VERTICAL)
    t0 = time.monotonic()
    out = await _ok(ctx, "sch_shift_sheet", sheet=VERTICAL, center=True)
    elapsed = time.monotonic() - t0
    assert elapsed < 60, elapsed  # one commit for every item of the sheet
    d = out["offset_mm"]
    _assert_shifted(before, await _points(s, VERTICAL), d)
    box, frame = out["bbox_after_mm"], out["page"]["frame_mm"]
    assert box[0] >= frame[0] and box[1] >= frame[1]
    assert box[2] <= frame[2] and box[3] <= frame[3]
    assert (await state(s))[1] == partition
    again = await _ok(ctx, "sch_shift_sheet", sheet=VERTICAL, center=True)
    assert again["offset_mm"] == [0.0, 0.0] and again["logical_changes"] == 0
    assert any("already centred" in n for n in again["notes"])


def _png(w: int, h: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _with_id(item: Any) -> Any:
    item.proto.id.value = str(uuid.uuid4())
    return item


def _every_kind() -> list[Any]:
    """One item of every kind the API lets us create on a sheet (besides symbols and
    sheet symbols, which the test adds through tools; junctions: see the root test)."""
    bus = SchematicLine()
    bus.start, bus.end = v2(30.48, 101.6), v2(30.48, 127.0)
    bus.type = st.SchematicLineType.SLT_BUS
    entry = BusEntry()
    entry.proto.position.CopyFrom(v2(30.48, 106.68).proto)
    entry.proto.size.CopyFrom(v2(2.54, -2.54).proto)
    entry.proto.type = st.BusEntryType.BET_WIRE_TO_BUS
    nc = NoConnectMarker()
    nc.position = v2(63.5, 101.6)
    glabel = GlobalLabel()
    glabel.text.value = "SDA"
    glabel.position = v2(76.2, 101.6)
    glabel.text.position = v2(76.2, 101.6)
    hlabel = HierarchicalLabel()
    hlabel.text.value = "SCL"
    hlabel.position = v2(76.2, 109.22)
    hlabel.text.position = v2(76.2, 109.22)
    text = SchematicText()
    text.proto.text.text = "Shift me"
    text.proto.text.position.CopyFrom(v2(40.64, 132.08).proto)
    text.proto.text.attributes.size.CopyFrom(v2(1.27, 1.27).proto)
    tbox = SchematicTextBox()
    tbox.top_left, tbox.bottom_right = v2(60.96, 127.0), v2(81.28, 137.16)
    tbox.value = "A text box"
    tbox.proto.textbox.attributes.size.CopyFrom(v2(1.27, 1.27).proto)
    rect = SchematicGraphicShape()
    rect.proto.shape.rectangle.top_left.CopyFrom(v2(88.9, 101.6).proto)
    rect.proto.shape.rectangle.bottom_right.CopyFrom(v2(101.6, 111.76).proto)
    circle = SchematicGraphicShape()
    circle.proto.shape.circle.center.CopyFrom(v2(114.3, 106.68).proto)
    circle.proto.shape.circle.radius_point.CopyFrom(v2(118.11, 106.68).proto)
    arc = SchematicGraphicShape()
    arc.proto.shape.arc.start.CopyFrom(v2(127.0, 106.68).proto)
    arc.proto.shape.arc.mid.CopyFrom(v2(130.81, 102.87).proto)
    arc.proto.shape.arc.end.CopyFrom(v2(134.62, 106.68).proto)
    poly = SchematicGraphicShape()
    outline = poly.proto.shape.polygon.polygons.add().outline
    for x, y in ((88.9, 120.65), (101.6, 120.65), (95.25, 128.27)):
        outline.nodes.add().point.CopyFrom(v2(x, y).proto)
    img = SchematicImage()
    img.position = v2(127.0, 127.0)
    img.image_scale = 1.0
    img.image_data = _png(60, 30)
    directive = DirectiveLabel()
    directive.text.value = "netclass"
    directive.position = v2(25.4, 139.7)
    directive.text.position = v2(25.4, 139.7)
    table = SchematicTable()
    table.proto.column_count = 2
    for _ in range(2):
        table.proto.column_widths.add().value_nm = 10_160_000
    table.proto.row_heights.add().value_nm = 5_080_000
    for col in range(2):
        cell = table.proto.cells.add()
        x = 139.7 + col * 10.16
        cell.text_box.textbox.top_left.CopyFrom(v2(x, 139.7).proto)
        cell.text_box.textbox.bottom_right.CopyFrom(v2(x + 10.16, 144.78).proto)
        cell.text_box.textbox.text = f"c{col}"
        cell.text_box.textbox.attributes.size.CopyFrom(v2(1.27, 1.27).proto)
        cell.column_span = cell.row_span = 1
    items = [
        directive,
        table,
        bus,
        entry,
        nc,
        glabel,
        hlabel,
        text,
        tbox,
        rect,
        circle,
        arc,
        poly,
        img,
    ]
    reversed_rect = SchematicGraphicShape()  # drawn right-to-left: corners not normalised
    reversed_rect.proto.shape.rectangle.top_left.CopyFrom(v2(160.02, 50.8).proto)
    reversed_rect.proto.shape.rectangle.bottom_right.CopyFrom(v2(149.86, 40.64).proto)
    bezier = SchematicGraphicShape()
    bz = bezier.proto.shape.bezier
    for vec, (x, y) in ((bz.start, (25.4, 50.8)), (bz.control1, (30.48, 40.64)),
                        (bz.control2, (35.56, 60.96)), (bz.end, (40.64, 50.8))):  # fmt: skip
        vec.CopyFrom(v2(x, y).proto)
    area = SchematicRuleArea()
    ring = area.proto.shape.polygon.polygons.add().outline
    for x, y in ((50.8, 40.64), (71.12, 40.64), (71.12, 55.88), (50.8, 55.88)):
        ring.nodes.add().point.CopyFrom(v2(x, y).proto)
    ring.closed = True
    multi = SchematicText()
    multi.proto.text.text = "line one\nline two"
    multi.proto.text.position.CopyFrom(v2(88.9, 50.8).proto)
    multi.proto.text.attributes.size.CopyFrom(v2(1.27, 1.27).proto)
    items += [reversed_rect, bezier, area, multi]
    items += [wire((33.02, 104.14), (45.72, 104.14)), local_label("BUS_X", (40.64, 104.14))]
    return [_with_id(i) for i in items]


def _probe(h: KicadHandle) -> dict[str, tuple[int, int]]:
    """Hand-picked coordinates per kind read through kipy's own accessors (independent of
    the proto walker that does the shift)."""
    sch = sheet_api(h, resolve_sheet(h, None))
    out: dict[str, tuple[int, int]] = {}

    def put(name: str, v: Any) -> None:  # a kipy Vector2 or a raw proto one
        out[name] = (int(v.x), int(v.y)) if hasattr(v, "x") else (v.x_nm, v.y_nm)

    sym = next(x for x in sch.get_symbols() if str(x.reference) == "R1")
    put("symbol", sym.position)
    put("symbol.reference", sym.reference_field.text.position)
    put("symbol.value", sym.value_field.text.position)
    sheet = sch.get_sheet_symbols()[0]
    put("sheet", sheet.position)
    put("sheet.name", sheet.name_field.text.position)
    put("sheet.pin", sheet.pins[0].position)
    put("sheet.pin.text", sheet.pins[0].text.position)
    for lb in sch.get_labels():
        put(f"label {lb.text.value}", lb.position)
        put(f"label {lb.text.value}.text", lb.text.position)
    for t in sch.get_text():
        if isinstance(t, SchematicTextBox):
            put("text_box", t.top_left)
        else:
            put(f"text {t.value[:4]}", t.text.position)
    for sh in sch.get_shapes():
        geo = sh.proto.shape.WhichOneof("geometry")
        attr = {"rectangle": "bottom_right", "circle": "radius_point", "arc": "mid",
                "bezier": "control2"}.get(geo)  # fmt: skip
        if attr is not None:
            put(f"shape {access.kiid(sh)} {geo}", getattr(getattr(sh.proto.shape, geo), attr))
    for img in sch.get_images():
        put("image", img.position)
    for be in sch.get_bus_entries():
        put("bus_entry", be.position)
    for tb in sch.get_tables():
        put("table.cell", tb.proto.cells[1].text_box.textbox.top_left)
    for ra in sch.get_rule_areas():
        put("rule_area", ra.proto.shape.polygon.polygons[0].outline.nodes[2].point)
    return out


async def test_every_item_kind_moves(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    await place_direct(s, "Device:R", (152.4, 101.6), "R1")
    await _ok(ctx, "sch_add_sheet", name="sub", file="sub.kicad_sch", at=[165.1, 121.92])
    await _ok(ctx, "sch_add_sheet_pin", sheet="/sub/", name="IN", direction="input")

    def create(h: KicadHandle) -> None:
        sheet_api(h, resolve_sheet(h, None)).create_items(_every_kind())

    async with s.commit("every kind"):
        await s.call(create)

    def group(h: KicadHandle) -> None:
        sch = sheet_api(h, resolve_sheet(h, None))
        g = _with_id(Group())
        for t in sch.get_shapes()[:2]:
            g.proto.items.add().CopyFrom(t.proto.id)
        sch.create_items([g])

    async with s.commit("group"):
        await s.call(group)
    _ids, partition = await state(s)
    before = await _points(s)
    probe = await s.call(_probe)
    out = await _ok(ctx, "sch_shift_sheet", by=[-1.27, 2.54])
    after = await _points(s)
    _assert_shifted(before, after, (-1.27, 2.54))
    moved = await s.call(_probe)
    assert set(moved) == set(probe) and len(probe) >= 20, sorted(probe)
    for name, (x, y) in probe.items():
        assert moved[name] == (x - 1_270_000, y + 2_540_000), name
    shifted = out["shifted"]
    # (the lone junction is dropped by KiCad's cleanup at creation; junctions: root test)
    for kind in ("bus", "bus_entry", "no_connect", "global_label",
                 "hierarchical_label", "label", "text", "text_box", "shape", "image",
                 "symbol", "sheet", "wire", "directive_label", "table",
                 "rule_area"):  # fmt: skip
        assert shifted.get(kind, 0) > 0, (kind, shifted)
    assert shifted["shape"] == 6 and shifted["text"] == 2
    assert out["not_shifted"] == {"group": 1}
    assert (await state(s))[1] == partition


async def test_refused_off_the_paper_and_zero_offset_is_a_no_op(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    await place_direct(s, "Device:R", (101.6, 76.2), "R1")
    before = await _points(s)
    out, err = await call_tool(ctx, "sch_shift_sheet", by=[254.0, 0.0])
    assert err and out["error"]["code"] == "invalid_argument", out
    assert "leave the page" in out["error"]["what"]
    assert await _points(s) == before
    noop = await _ok(ctx, "sch_shift_sheet", by=[0.4, -0.3])
    assert noop["offset_mm"] == [0.0, 0.0] and noop["logical_changes"] == 0
    assert any("nothing to shift" in n for n in noop["notes"])
    assert await _points(s) == before
    both, err = await call_tool(ctx, "sch_shift_sheet", by=[1.27, 0], center=True)
    assert err and "exactly one" in both["error"]["what"]
    alone = await _ok(ctx, "sch_shift_sheet", sheet="/", center=True)
    assert alone["logical_changes"] == 1  # R1 alone is centred
    await _ok(ctx, "sch_add_sheet", name="sub", file="sub.kicad_sch", at=[25.4, 25.4])
    empty = await _ok(ctx, "sch_shift_sheet", sheet="/sub/", center=True)
    assert empty["logical_changes"] == 0 and empty["offset_mm"] == [0.0, 0.0]
    assert any("is empty" in n for n in empty["notes"])


def _forced(*_args: Any) -> list[str]:
    return ["forced mismatch"]


async def test_a_failed_verification_restores_every_item(hier_session: KicadSession) -> None:
    from copperchat.errors import StructuredToolError
    from copperchat.kicad_tools.edit.engine import apply_change_set
    from copperchat.kicad_tools.edit.specs_transform import prepare_shift_sheet

    s = hier_session
    ctx = edit_ctx(s)
    _ids, partition = await state(s)
    before = await _points(s)
    cs = await prepare_shift_sheet(ctx, {"by": [5.08, 5.08]})
    with pytest.raises(StructuredToolError) as err:
        await apply_change_set(ctx, cs, _verify=_forced)
    assert err.value.code == "verification_failed"
    assert await _points(s) == before
    assert (await state(s))[1] == partition


async def test_a_large_sheet_shifts_in_one_commit(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """600 wires and 200 labels (a dense sheet): one Update stage, well inside a turn."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)

    def create(h: KicadHandle) -> None:
        items: list[Any] = []
        for row in range(40):
            y = 25.4 + row * 3.81
            for col in range(15):
                x = 25.4 + col * 15.24
                items.append(wire((x, y), (x + 10.16, y)))
                if col % 3 == 0:
                    items.append(local_label(f"N{row}_{col}", (x, y)))
        sheet_api(h, resolve_sheet(h, None)).create_items(items)

    async with s.commit("dense"):
        await s.call(create)
    _ids, partition = await state(s)
    before = await _points(s)
    assert len(before) == 800
    t0 = time.monotonic()
    out = await _ok(ctx, "sch_shift_sheet", center=True)
    elapsed = time.monotonic() - t0
    assert elapsed < 30, elapsed
    assert out["shifted"] == {"label": 200, "wire": 600}
    assert out["offset_mm"] != [0.0, 0.0]
    _assert_shifted(before, await _points(s), out["offset_mm"])
    assert (await state(s))[1] == partition
    print(f"shift of 800 items: {elapsed:.2f} s")


async def test_an_item_kicad_did_not_move_undoes_the_shift(
    hier_session: KicadSession, edit_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check stage runs on the real read-back inside the engine: a reported item
    makes the call fail as verification_failed and every item goes back."""
    from copperchat.kicad_tools.edit import shift

    seen: list[int] = []
    real = shift.shift_problems

    def refused(sent: Any, read_back: Any, original: Any) -> list[str]:
        seen.append(len(read_back))
        assert real(sent, read_back, original) == []  # KiCad did move everything
        return ["text 00000000-0000-0000-0000-000000000000"]

    monkeypatch.setattr(shift, "shift_problems", refused)
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    _ids, partition = await state(s)
    before = await _points(s)
    out, err = await call_tool(ctx, "sch_shift_sheet", by=[2.54, 2.54])
    assert err and out["error"]["code"] == "verification_failed", out
    assert "did not move every item" in out["error"]["what"]
    assert seen == [len(before)]  # the check saw the read-back of every shifted item
    assert await _points(s) == before
    assert (await state(s))[1] == partition
