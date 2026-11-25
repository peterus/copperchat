"""`load_board_model` on the ecc83 demo board (SP4 P1a, Task 4). One api-server for the
module: the tests only read, or change what the later ones tolerate (order matters:
the footprint-placing test runs last)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from kipy.board_rules import CustomRule, CustomRuleConstraint
from kipy.geometry import Angle, Vector2
from kipy.proto.board import board_rules_pb2

from copperchat.board.geom import point_in_polygon
from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools.board.api import save_document_to_string
from copperchat.kicad_tools.board.read import load_board_model

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]

# Pinned after the first green run (ecc83 demo board at the pinned KiCad).
ECC83_PARTS = 15
ECC83_TRACKS = 59
ECC83_VIAS = 0
ECC83_ZONES = 1
ECC83_COPPER = ("F.Cu", "B.Cu")
ECC83_OUTLINE = "other"
ECC83_NET_CLASSES = ("Default",)


async def _model(s: KicadSession) -> BoardModel:
    model: BoardModel = await s.call(lambda h: load_board_model(h, s.project), timeout=60)
    return model


async def test_load_ecc83(ecc83_module_session: KicadSession) -> None:
    s = ecc83_module_session
    m = await _model(s)
    n_api = await s.call(lambda h: len(h.board().get_footprints()))
    text: str = await s.call(save_document_to_string)
    n_file = len(re.findall(r"^\t\(footprint ", text, re.M))
    assert len(m.parts) == n_api == n_file
    assert m.tracks, "the demo board is hand-routed"
    assert m.outline.kind != "none"
    for p in m.parts:
        assert p.kiid and p.lib_id and p.symbol_path, p.ref
        assert p.pads and len(p.courtyard) >= 3, p.ref
    assert m.warnings == () and m.dru_marker == ""
    counts = (
        len(m.parts),
        len(m.tracks) + len(m.arcs),
        len(m.vias),
        len(m.zones),
        m.copper_layers,
        m.outline.kind,
        tuple(c.name for c in m.net_classes),
    )
    print("ecc83 counts:", counts)
    assert counts == (
        ECC83_PARTS,
        ECC83_TRACKS,
        ECC83_VIAS,
        ECC83_ZONES,
        ECC83_COPPER,
        ECC83_OUTLINE,
        ECC83_NET_CLASSES,
    )
    nets = {pad.net for p in m.parts for pad in p.pads if pad.net}
    assert nets and nets <= {n for n, _ in m.net_class_of}
    assert dict(m.layer_types)["F.Cu"] == "signal"
    assert m.rules and m.stackup


async def test_api_server_rss(ecc83_module_session: KicadSession, record_property) -> None:  # type: ignore[no-untyped-def]
    proc = ecc83_module_session.process
    assert proc is not None
    status = Path(f"/proc/{proc.pid}/status").read_text()
    rss_kb = int(re.search(r"^VmRSS:\s+(\d+) kB", status, re.M).group(1))  # type: ignore[union-attr]
    record_property("api_server_rss_kb", rss_kb)
    print(f"api-server VmRSS with the ecc83 board open: {rss_kb} kB")
    assert rss_kb > 0


async def test_hashes_stable_across_reads(ecc83_module_session: KicadSession) -> None:
    a = await _model(ecc83_module_session)
    b = await _model(ecc83_module_session)
    assert board_hash(a) == board_hash(b)
    assert placement_hash(a) == placement_hash(b)


async def test_move_changes_placement_hash(ecc83_module_session: KicadSession) -> None:
    s = ecc83_module_session
    before = await _model(s)
    ref = sorted(p.ref for p in before.parts)[0]

    def move(h: KicadHandle) -> None:
        board = h.board()
        (fp,) = [f for f in board.get_footprints() if f.reference_field.text.value == ref]
        fp.position = Vector2.from_xy(fp.position.x + 10_000, fp.position.y)  # 0.01 mm
        board.update_items([fp])

    async with s.commit("move", doc="pcb"):
        await s.call(move)
    after = await _model(s)
    assert after.part(ref).x == pytest.approx(before.part(ref).x + 0.01, abs=1e-6)
    assert placement_hash(after) != placement_hash(before)
    assert board_hash(after) != board_hash(before)


async def test_dru_text_read(ecc83_module_session: KicadSession) -> None:
    s = ecc83_module_session

    def set_rule(h: KicadHandle) -> None:
        rule = CustomRule()
        rule.name = "cc_probe_rule"
        rule.condition = "A.NetClass == 'Default'"
        con = CustomRuleConstraint.from_type(board_rules_pb2.CRCT_TRACK_WIDTH)
        con.proto.numeric.min = 300_000
        rule.constraints = [con]
        h.board().set_custom_design_rules([rule])

    await s.call(set_rule)
    await s.save("pcb")
    m = await _model(s)
    assert "cc_probe_rule" in m.dru_text
    assert m.warnings == ()


async def test_footprint_rule_area_in_board_coords(ecc83_module_session: KicadSession) -> None:
    """A footprint-embedded keepout (stock `RF_Module:DWM1000`, outline stored in the
    footprint frame by the API) comes back in board coordinates, rotated and flipped."""
    s = ecc83_module_session
    lib_id = "RF_Module:DWM1000"

    def place(h: KicadHandle) -> str:
        h.kicad.get_library_footprint(lib_id)
        fp = h.board().place_footprint_from_library(lib_id, Vector2.from_xy_mm(150, 60))
        return str(fp.id.value)

    async with s.commit("place", doc="pcb"):
        kiid = await s.call(place)

    def transform(h: KicadHandle, rot: float | None, flip: bool) -> None:
        board = h.board()
        (fp,) = [f for f in board.get_footprints() if f.id.value == kiid]
        if rot is not None:
            fp.orientation = Angle.from_degrees(rot)
            board.update_items([fp])
        if flip:
            board.flip_items([fp])

    def bbox(h: KicadHandle) -> tuple[float, float, float, float]:
        board = h.board()
        (fp,) = [f for f in board.get_footprints() if f.id.value == kiid]
        box = board.get_item_bounding_box(fp)
        assert box is not None
        x, y = box.pos.x / 1e6, box.pos.y / 1e6
        return x, y, x + box.size.x / 1e6, y + box.size.y / 1e6

    for rot, flip in ((None, False), (30.0, False), (None, True)):
        async with s.commit("turn", doc="pcb"):
            await s.call(lambda h, r=rot, f=flip: transform(h, r, f))  # type: ignore[misc]
        m = await _model(s)
        (part,) = [p for p in m.parts if p.kiid == kiid]
        x0, y0, x1, y1 = await s.call(bbox)
        assert part.rule_areas, "the DWM1000 footprint carries a keepout"
        box = (
            (x0 - 0.05, y0 - 0.05),
            (x1 + 0.05, y0 - 0.05),
            (x1 + 0.05, y1 + 0.05),
            (x0 - 0.05, y1 + 0.05),
        )
        for area in part.rule_areas:
            for pt in area:
                assert point_in_polygon(pt, box), (rot, flip, pt, (x0, y0, x1, y1))
        assert part.side == ("bottom" if flip else "top")
        # the keepout lies on the antenna side, away from the pads' centre
        cx = sum(pt[0] for pt in part.rule_areas[0]) / len(part.rule_areas[0])
        cy = sum(pt[1] for pt in part.rule_areas[0]) / len(part.rule_areas[0])
        assert (cx, cy) != pytest.approx((part.x, part.y), abs=1.0)
