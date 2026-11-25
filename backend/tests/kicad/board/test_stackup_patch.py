"""KiCad patch 0005 (`UpdateBoardStackup`, gap 4A-G1) and `pcb_set_rules.stackup` on the real
api-server (SP4 P2b Tasks 2 and 3). The stackup is only written through the API; the saved
`.kicad_pcb` is only read."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from kipy.errors import ApiError
from kipy.proto.board import board_pb2
from kipy.proto.common import ApiStatusCode
from kipy.util.board_layer import canonical_name

from copperchat.board.model import BoardModel, board_hash
from copperchat.board.presets import STACKUPS
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools.board.engine import BoardChangeSet
from copperchat.kicad_tools.board.read import load_board_model
from copperchat.kicad_tools.board.setup import (
    get_stackup,
    has_stackup_handler,
    stackup_request,
    stackup_thicknesses,
    update_stackup,
)
from copperchat.tools import registry
from copperchat.tools.spec import ToolContext
from tests.kicad.board.conftest import SynthRig
from tests.support.edit import call_tool

pytestmark = pytest.mark.kicad

JLC_4L = STACKUPS["jlc-4l-1.6-7628"]
JLC_4L_MM = [0.035, 0.2104, 0.0152, 1.065, 0.0152, 0.2104, 0.035]


def _set_four_layers(h: KicadHandle) -> None:
    board = h.board()
    non_copper = [
        la for la in board.get_enabled_layers() if not str(canonical_name(la)).endswith(".Cu")
    ]
    board.set_enabled_layers(4, non_copper)


def _close(a: list[float], b: list[float], tol: float = 1e-6) -> bool:
    return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


async def _four_layer_ecc83(s: KicadSession) -> board_pb2.BoardStackup:
    await s.call(_set_four_layers)
    pre: board_pb2.BoardStackup = await s.call(get_stackup)
    assert len([la for la in pre.layers if la.type == board_pb2.BSLT_COPPER]) == 4
    return pre


# -- patch 0005 through the raw API (Task 2) ----------------------------------------------


async def test_set_get_roundtrip(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    pre = await _four_layer_ecc83(s)
    response = await s.call(lambda h: update_stackup(h, stackup_request(pre, JLC_4L)))
    assert _close(stackup_thicknesses(response), JLC_4L_MM)
    got: board_pb2.BoardStackup = await s.call(get_stackup)
    assert _close(stackup_thicknesses(got), JLC_4L_MM)  # within 1 nm
    diel = [la for la in got.layers if la.type == board_pb2.BSLT_DIELECTRIC]
    assert [d.dielectric.layer[0].material_name for d in diel] == ["7628", "FR4", "7628"]
    assert [d.dielectric.type for d in diel] == [
        board_pb2.BSDT_PREPREG,
        board_pb2.BSDT_CORE,
        board_pb2.BSDT_PREPREG,
    ]
    assert diel[1].dielectric.layer[0].epsilon_r == pytest.approx(4.6)


async def test_saved_board_has_stackup(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    pre = await _four_layer_ecc83(s)
    await s.call(lambda h: update_stackup(h, stackup_request(pre, JLC_4L)))
    await s.save("pcb")
    pcb = s.project.pcb_file
    assert pcb is not None
    text = pcb.read_text(encoding="utf-8")
    assert "(stackup" in text and "0.2104" in text and "1.065" in text


async def test_copper_count_mismatch_rejected(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    pre = await _four_layer_ecc83(s)
    # F.Cu, one dielectric, B.Cu: a 2-copper message on the 4-layer board
    copper = [la for la in pre.layers if la.type == board_pb2.BSLT_COPPER]
    diel = [la for la in pre.layers if la.type == board_pb2.BSLT_DIELECTRIC]
    two = board_pb2.BoardStackup()
    two.CopyFrom(pre)
    del two.layers[:]
    two.layers.extend([copper[0], diel[0], copper[-1]])
    with pytest.raises(ApiError) as info:
        await s.call(lambda h: update_stackup(h, two))
    assert info.value.code == ApiStatusCode.AS_BAD_REQUEST
    assert await s.call(get_stackup) == pre  # unchanged


async def test_zero_dielectric_rejected(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    pre = await _four_layer_ecc83(s)
    bad = stackup_request(pre, JLC_4L)
    diel = next(la for la in bad.layers if la.type == board_pb2.BSLT_DIELECTRIC)
    diel.thickness.value_nm = 0
    diel.dielectric.layer[0].thickness.value_nm = 0
    with pytest.raises(ApiError) as info:
        await s.call(lambda h: update_stackup(h, bad))
    assert info.value.code == ApiStatusCode.AS_BAD_REQUEST
    assert await s.call(get_stackup) == pre


async def test_probe_finds_patch_without_writing(ecc83_session: KicadSession) -> None:
    """`has_stackup_handler`'s empty-stackup probe: refused by patch 0005, nothing stored."""
    s = ecc83_session
    pre: board_pb2.BoardStackup = await s.call(get_stackup)
    assert await s.call(has_stackup_handler) is True
    assert await s.call(get_stackup) == pre


# -- pcb_set_rules.stackup (Task 3) --------------------------------------------------------


async def _synced(rig: SynthRig) -> ToolContext:
    ctx = rig.ctx()
    data, err = await call_tool(ctx, "pcb_sync_from_schematic")
    assert not err, data
    return rig.ctx()  # the board creation reopened the session


async def _model(ctx: ToolContext) -> BoardModel:
    project = ctx.project
    return await ctx.session.call(lambda h: load_board_model(h, project), timeout=120)


async def test_set_rules_stackup_4l(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    before = board_hash(await _model(ctx))
    data, err = await call_tool(
        ctx, "pcb_set_rules", layers=4, preset="jlcpcb-4l", stackup="jlc-4l-1.6-7628"
    )
    assert not err, data
    stackup = data["stackup"]
    assert {k: stackup[k] for k in ("id", "jlc_name", "total_mm")} == {
        "id": "jlc-4l-1.6-7628",
        "jlc_name": "JLC04161H-7628",
        "total_mm": 1.5862,
    }
    # KiCad's board thickness adds the solder mask layers (0.01 mm each by default)
    assert 1.5862 < stackup["board_mm"] <= 1.5862 + 0.1
    got: board_pb2.BoardStackup = await ctx.session.call(get_stackup)
    assert _close(stackup_thicknesses(got), JLC_4L_MM)
    assert board_hash(await _model(ctx)) != before
    assert data["board_hash_changed"] is True


async def test_stackup_layer_mismatch(synth_rig: SynthRig) -> None:
    ctx = await _synced(synth_rig)
    data, err = await call_tool(ctx, "pcb_set_rules", layers=4)
    assert not err, data
    before = board_hash(await _model(ctx))
    data, err = await call_tool(ctx, "pcb_set_rules", stackup="jlc-6l-1.6")
    assert err and data["error"]["code"] == "invalid_argument", data
    assert board_hash(await _model(ctx)) == before


async def test_fail_after_stackup_restores(
    synth_rig: SynthRig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 -> 4 layers + stackup + design rules, failing after the stackup stage: the
    stackup, the layer count and the board hash are the pre-call ones."""
    ctx = await _synced(synth_rig)
    pre_model = await _model(ctx)
    pre_stackup: board_pb2.BoardStackup = await ctx.session.call(get_stackup)
    specs = registry.all_specs()
    spec = next(s for s in specs if s.name == "pcb_set_rules")
    assert spec.prepare is not None
    prepare = spec.prepare

    async def failing(c: ToolContext, args: dict[str, Any]) -> Any:
        cs = await prepare(c, args)
        assert isinstance(cs, BoardChangeSet)
        assert [st.label for st in cs.board_stages][:2] == ["layer count", "stackup"]
        cs.fail_after = 2
        return cs

    patched = [dataclasses.replace(s, prepare=failing) if s is spec else s for s in specs]
    monkeypatch.setattr(registry, "all_specs", lambda: patched)
    data, err = await call_tool(
        ctx, "pcb_set_rules", layers=4, preset="jlcpcb-4l", stackup="jlc-4l-1.6-7628"
    )
    assert err, data
    after = await _model(ctx)
    assert after.copper_layers == pre_model.copper_layers
    assert after.stackup == pre_model.stackup
    assert board_hash(after) == board_hash(pre_model)
    got: board_pb2.BoardStackup = await ctx.session.call(get_stackup)
    assert stackup_thicknesses(got) == stackup_thicknesses(pre_stackup)
