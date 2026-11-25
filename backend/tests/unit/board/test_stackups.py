"""Stackup presets and the `pcb_set_rules.stackup` branch without KiCad (SP4 P2b Task 3)."""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kipy.errors import ApiError
from kipy.proto.board import board_commands_pb2, board_pb2
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common import ApiStatusCode
from kipy.proto.common.types.base_types_pb2 import DocumentSpecifier, DocumentType

from copperchat.board.presets import STACKUPS, StackupLayer
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board import engine as board_engine
from copperchat.kicad_tools.board import setup
from copperchat.kicad_tools.board.engine import apply_board_change_set
from copperchat.kicad_tools.board.setup import (
    board_thickness_mm,
    parse_rules_args,
    plan_stackup,
    stackup_request,
    stackup_thicknesses,
)
from copperchat.kicad_tools.board.specs_setup import PCB_SET_RULES_SCHEMA
from tests.unit.board.test_engine_unit import FakeSession, Spy, _ctx

NM = 1_000_000
NO_HANDLER = (
    "KiCad returned error: no handler available for request of type "
    "kiapi.board.commands.UpdateBoardStackup"
)


def _cu(t: float) -> StackupLayer:
    return StackupLayer("copper", t, "copper")


def test_jlc_4l_values() -> None:
    p = STACKUPS["jlc-4l-1.6-7628"]
    assert (p.copper_layers, p.jlc_name) == (4, "JLC04161H-7628")
    assert p.layers == (
        _cu(0.035),
        StackupLayer("prepreg", 0.2104, "7628", 4.4),
        _cu(0.0152),
        StackupLayer("core", 1.065, "FR4", 4.6),
        _cu(0.0152),
        StackupLayer("prepreg", 0.2104, "7628", 4.4),
        _cu(0.035),
    )


def test_jlc_6l_values() -> None:
    p = STACKUPS["jlc-6l-1.6"]
    assert (p.copper_layers, p.jlc_name) == (6, "JLC06161H-3313")
    assert p.layers == (
        _cu(0.035),
        StackupLayer("prepreg", 0.0994, "3313", 4.1),
        _cu(0.0152),
        StackupLayer("core", 0.55, "FR4", 4.6),
        _cu(0.0152),
        StackupLayer("prepreg", 0.1088, "2116", 4.16),
        _cu(0.0152),
        StackupLayer("core", 0.55, "FR4", 4.6),
        _cu(0.0152),
        StackupLayer("prepreg", 0.0994, "3313", 4.1),
        _cu(0.035),
    )


def test_jlc_2l_values() -> None:
    p = STACKUPS["jlc-2l-1.6"]
    assert (p.copper_layers, p.jlc_name) == (2, None)
    assert p.layers == (_cu(0.035), StackupLayer("core", 1.53, "FR4", 4.5), _cu(0.035))


def test_copper_counts() -> None:
    assert {k: p.copper_layers for k, p in STACKUPS.items()} == {
        "jlc-2l-1.6": 2,
        "jlc-4l-1.6-7628": 4,
        "jlc-6l-1.6": 6,
    }
    for p in STACKUPS.values():
        kinds = [layer.kind for layer in p.layers]
        assert kinds[::2] == ["copper"] * p.copper_layers  # copper and dielectric alternate
        assert all(k != "copper" for k in kinds[1::2])
        assert all((layer.epsilon_r is None) == (layer.kind == "copper") for layer in p.layers)


def test_total_thickness() -> None:
    # Mask excluded. Sums: 2l 1.6, 4l 1.5862, 6l 1.5384 (mm).
    totals = {k: round(p.total_mm, 4) for k, p in STACKUPS.items()}
    assert totals == {"jlc-2l-1.6": 1.6, "jlc-4l-1.6-7628": 1.5862, "jlc-6l-1.6": 1.5384}
    assert all(abs(t - 1.6) <= 0.1 for t in totals.values())


def test_stackup_enum_in_schema() -> None:
    prop = PCB_SET_RULES_SCHEMA["properties"]["stackup"]
    assert prop["enum"] == sorted(STACKUPS)


@pytest.mark.parametrize(
    ("args", "msg"),
    [
        ({"stackup": "jlc-6l-1.6"}, "6 copper layers"),  # the board has 4
        ({"layers": 2, "stackup": "jlc-4l-1.6-7628"}, "4 copper layers"),
        ({"stackup": "jlc-8l"}, "unknown stackup"),
    ],
)
def test_stackup_refused(args: dict[str, Any], msg: str) -> None:
    with pytest.raises(StructuredToolError) as info:
        plan_stackup(parse_rules_args(args), 4)
    assert info.value.code == "invalid_argument"
    assert msg in str(info.value.why)


def test_stackup_plan() -> None:
    assert plan_stackup(parse_rules_args({"layers": 2}), 4) is None
    assert (
        plan_stackup(parse_rules_args({"layers": 4, "stackup": "jlc-4l-1.6-7628"}), 2)
        == (STACKUPS["jlc-4l-1.6-7628"])
    )


def _dielectric(t_nm: int, *, sub: int = 1, lt: float = 0.02) -> board_pb2.BoardStackupLayer:
    layer = board_pb2.BoardStackupLayer(
        layer=BoardLayer.BL_UNDEFINED, enabled=True, type=board_pb2.BSLT_DIELECTRIC
    )
    layer.thickness.value_nm = t_nm
    layer.dielectric.type = board_pb2.BSDT_CORE
    for _ in range(sub):
        props = layer.dielectric.layer.add(epsilon_r=4.5, loss_tangent=lt, material_name="FR4")
        props.thickness.value_nm = t_nm // sub
    return layer


def _copper(layer: BoardLayer.ValueType, t_nm: int = 35_000) -> board_pb2.BoardStackupLayer:
    out = board_pb2.BoardStackupLayer(layer=layer, enabled=True, type=board_pb2.BSLT_COPPER)
    out.thickness.value_nm = t_nm
    return out


def _default_4l() -> board_pb2.BoardStackup:
    """KiCad's default-like 4-layer stackup (mask and silk around the copper)."""
    s = board_pb2.BoardStackup()
    mask = s.layers.add(layer=BoardLayer.BL_F_Mask, enabled=True, type=board_pb2.BSLT_SOLDERMASK)
    mask.thickness.value_nm = 10_000
    s.layers.append(_copper(BoardLayer.BL_F_Cu))
    s.layers.append(_dielectric(480_000, sub=2, lt=0.03))
    s.layers.append(_copper(BoardLayer.BL_In1_Cu))
    s.layers.append(_dielectric(480_000))
    s.layers.append(_copper(BoardLayer.BL_In2_Cu))
    s.layers.append(_dielectric(480_000))
    s.layers.append(_copper(BoardLayer.BL_B_Cu))
    s.layers.add(layer=BoardLayer.BL_B_Mask, enabled=True, type=board_pb2.BSLT_SOLDERMASK)
    s.finish.type_name = "ENIG"
    return s


def test_stackup_request_sets_preset_values() -> None:
    current = _default_4l()
    req = stackup_request(current, STACKUPS["jlc-4l-1.6-7628"])
    assert current == _default_4l()  # not mutated
    assert stackup_thicknesses(req) == [0.035, 0.2104, 0.0152, 1.065, 0.0152, 0.2104, 0.035]
    diel = [la for la in req.layers if la.type == board_pb2.BSLT_DIELECTRIC]
    assert [d.dielectric.type for d in diel] == [
        board_pb2.BSDT_PREPREG,
        board_pb2.BSDT_CORE,
        board_pb2.BSDT_PREPREG,
    ]
    for d, (t, mat, er) in zip(
        diel, [(210_400, "7628", 4.4), (1_065_000, "FR4", 4.6), (210_400, "7628", 4.4)], strict=True
    ):
        assert len(d.dielectric.layer) == 1  # sub-layers merged into one
        props = d.dielectric.layer[0]
        assert (props.thickness.value_nm, props.material_name, props.epsilon_r) == (t, mat, er)
        assert d.thickness.value_nm == t
    assert diel[0].dielectric.layer[0].loss_tangent == pytest.approx(0.03)  # kept from KiCad
    # Mask, finish and layer ids are kept
    assert req.layers[0] == current.layers[0] and req.finish.type_name == "ENIG"
    assert [la.layer for la in req.layers] == [la.layer for la in current.layers]


def test_stackup_request_layer_count_mismatch() -> None:
    with pytest.raises(StructuredToolError) as info:
        stackup_request(_default_4l(), STACKUPS["jlc-6l-1.6"])
    assert info.value.code == "invalid_argument"


# -- the unpatched server, through the engine ---------------------------------------------


class FakeBoard:
    """Board stand-in: 2 copper layers, KiCad's API without an UpdateBoardStackup handler."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.copper = 2
        self.document = DocumentSpecifier(type=DocumentType.DOCTYPE_PCB)
        self.client = SimpleNamespace(send=self._send)
        self.error: ApiError | None = ApiError(
            NO_HANDLER, raw_message=NO_HANDLER[len("KiCad returned error: ") :]
        )
        self.disabled_dielectric = False
        # An explicit stackup (patched KiCad): a layer change keeps the copper but resets the
        # dielectrics to defaults (lossy, like SynchronizeWithBoard after a shrink + regrow)
        self.stored: board_pb2.BoardStackup | None = None

    def get_copper_layer_count(self) -> int:
        return self.copper

    def _none(self) -> list[Any]:  # no tracks, vias, zones, shapes or texts
        return []

    get_tracks = get_vias = get_zones = get_shapes = get_text = _none

    def get_enabled_layers(self) -> list[int]:
        return [BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu, BoardLayer.BL_Edge_Cuts]

    def set_enabled_layers(self, count: int, layers: list[int]) -> None:
        self.log.append(f"layers {count}")
        self.copper = count
        if self.stored is not None:
            self.stored = None
            self.stored = self.get_stackup().proto

    def get_stackup(self) -> Any:
        if self.stored is not None:
            out = board_pb2.BoardStackup()
            out.CopyFrom(self.stored)
            return SimpleNamespace(proto=out)
        s = _default_4l()
        if self.copper == 2:
            del s.layers[3:7]
        if self.disabled_dielectric:
            s.layers[2].enabled = False
        return SimpleNamespace(proto=s)

    def _send(self, cmd: Any, response_type: Any) -> Any:
        assert isinstance(cmd, board_commands_pb2.UpdateBoardStackup)
        self.log.append("update stackup")
        if self.error is not None:
            raise self.error
        copper = sum(1 for la in cmd.stackup.layers if la.type == board_pb2.BSLT_COPPER)
        if copper != self.copper:  # like patch 0005 (also answers the empty-stackup probe)
            raise ApiError("KiCad returned error: copper", code=ApiStatusCode.AS_BAD_REQUEST)
        self.stored = board_pb2.BoardStackup()
        self.stored.CopyFrom(cmd.stackup)
        return board_commands_pb2.BoardStackupResponse(stackup=cmd.stackup)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> Spy:
    s = Spy(log)
    monkeypatch.setattr(board_engine, "take_snapshot", s.take_snapshot)
    monkeypatch.setattr(board_engine, "restore", s.restore)
    monkeypatch.setattr(board_engine, "leftovers", s.leftovers)
    return s


@pytest.fixture
def log() -> list[str]:
    return []


async def test_unpatched_detected(tmp_path: Path, log: list[str], spy: Spy) -> None:
    session = FakeSession(log)
    board = FakeBoard(log)
    session.handle = SimpleNamespace(board=lambda: board)
    session.modified_docs = set()  # type: ignore[attr-defined]
    req = parse_rules_args({"layers": 4, "stackup": "jlc-4l-1.6-7628"})
    cs = setup._plan_rules(req, None, {}, 2, "hash", plan_stackup(req, 2))
    with pytest.raises(StructuredToolError) as info:
        await apply_board_change_set(_ctx(tmp_path, session), cs)
    assert info.value.code == "stackup_unavailable"
    assert "0005" in str(info.value.suggestion)
    # The layers stage ran, then its undo; the refused stackup is never "restored"
    assert [e for e in log if e.startswith(("layers", "update"))] == [
        "layers 4",
        "update stackup",
        "layers 2",
    ]
    assert spy.compensations == 1 and board.copper == 2


def _engine(tmp_path: Path, log: list[str], board: FakeBoard) -> Any:
    session = FakeSession(log)
    session.handle = SimpleNamespace(board=lambda: board)
    session.modified_docs = set()  # type: ignore[attr-defined]
    return _ctx(tmp_path, session)


async def test_unrestorable_board_stackup_refused(tmp_path: Path, log: list[str], spy: Spy) -> None:
    board = FakeBoard(log)
    board.copper = 4
    board.disabled_dielectric = True  # valid in a KiCad file, refused by patch 0005
    board.error = None  # patched
    req = parse_rules_args({"stackup": "jlc-4l-1.6-7628"})
    cs = setup._plan_rules(req, None, {}, 4, "hash", plan_stackup(req, 4))
    with pytest.raises(StructuredToolError) as info:
        await apply_board_change_set(_engine(tmp_path, log, board), cs)
    assert info.value.code == "invalid_argument"
    assert "cannot be restored" in str(info.value.why)
    assert log.count("update stackup") == 1 and board.stored is None  # only the probe


async def test_busy_is_not_invalid_argument(tmp_path: Path, log: list[str], spy: Spy) -> None:
    board = FakeBoard(log)
    board.copper = 4
    board.error = ApiError("KiCad returned error: busy", code=ApiStatusCode.AS_BUSY)
    req = parse_rules_args({"stackup": "jlc-4l-1.6-7628"})
    cs = setup._plan_rules(req, None, {}, 4, "hash", plan_stackup(req, 4))
    with pytest.raises(Exception) as info:
        await apply_board_change_set(_engine(tmp_path, log, board), cs)
    assert not (
        isinstance(info.value, StructuredToolError) and info.value.code == "invalid_argument"
    )
    # The write's outcome is unknown: the undo stays armed and writes the snapshot back
    assert log.count("update stackup") == 2


def test_board_thickness_includes_mask() -> None:
    req = stackup_request(_default_4l(), STACKUPS["jlc-4l-1.6-7628"])
    assert board_thickness_mm(req) == round(1.5862 + 0.01, 4)  # F.Mask 0.01, B.Mask 0


def test_layer_change_without_stackup_notes() -> None:
    cs = setup._plan_rules(parse_rules_args({"layers": 4}), None, {}, 2, "hash")
    assert any("stackup: jlc-4l-1.6-7628" in n for n in cs.report["notes"])
    cs = setup._plan_rules(
        parse_rules_args({"layers": 4, "stackup": "jlc-4l-1.6-7628"}),
        None,
        {},
        2,
        "hash",
        STACKUPS["jlc-4l-1.6-7628"],
    )
    assert not any("without stackup" in n for n in cs.report["notes"])


def _jlc_board(log: list[str]) -> FakeBoard:
    """A patched 4-layer board that already carries the JLC stackup."""
    board = FakeBoard(log)
    board.copper = 4
    board.error = None
    board.stored = stackup_request(_default_4l(), STACKUPS["jlc-4l-1.6-7628"])
    return board


async def test_layer_undo_restores_lossy_stackup(tmp_path: Path, log: list[str], spy: Spy) -> None:
    board = _jlc_board(log)
    pre = board.get_stackup().proto
    req = parse_rules_args({"layers": 2})
    cs = setup._plan_rules(req, None, {}, 4, "hash")
    cs.fail_after = 1
    with pytest.raises(RuntimeError, match="fail_after"):
        await apply_board_change_set(_engine(tmp_path, log, board), cs)
    # 4 -> 2 -> 4 lost the dielectrics; the undo wrote the pre-call stackup back
    assert [e for e in log if e.startswith(("layers", "update"))] == [
        "layers 2",
        "layers 4",
        "update stackup",
    ]
    assert board.get_stackup().proto == pre


async def test_unrestorable_stackup_refuses_layer_change(
    tmp_path: Path, log: list[str], spy: Spy
) -> None:
    board = _jlc_board(log)
    assert board.stored is not None
    board.stored.layers[2].enabled = False  # a disabled dielectric, from a KiCad file
    cs = setup._plan_rules(parse_rules_args({"layers": 2}), None, {}, 4, "hash")
    with pytest.raises(StructuredToolError) as info:
        await apply_board_change_set(_engine(tmp_path, log, board), cs)
    assert info.value.code == "invalid_argument"
    assert "cannot be restored" in str(info.value.why)
    assert not [e for e in log if e.startswith("layers")]  # nothing written
    assert log.count("update stackup") == 1  # only the probe for patch 0005


async def test_stale_stackup_refused(tmp_path: Path, log: list[str], spy: Spy) -> None:
    board = _jlc_board(log)
    board.copper = 2  # the stored stackup still describes 4 copper layers (unpatched sync)
    cs = setup._plan_rules(parse_rules_args({"layers": 4}), None, {}, 2, "hash")
    with pytest.raises(StructuredToolError) as info:
        await apply_board_change_set(_engine(tmp_path, log, board), cs)
    assert "4 copper layers, the board has 2" in str(info.value.why)


async def test_bad_request_is_invalid_argument(tmp_path: Path, log: list[str], spy: Spy) -> None:
    board = FakeBoard(log)
    board.copper = 4
    board.error = ApiError(
        "KiCad returned error: no", raw_message="no", code=ApiStatusCode.AS_BAD_REQUEST
    )
    req = parse_rules_args({"stackup": "jlc-4l-1.6-7628"})
    cs = setup._plan_rules(req, None, {}, 4, "hash", plan_stackup(req, 4))
    with pytest.raises(StructuredToolError) as info:
        await apply_board_change_set(_engine(tmp_path, log, board), cs)
    assert info.value.code == "invalid_argument"
    assert log.count("update stackup") == 1  # refused: nothing to restore


async def test_unpatched_stale_stackup_allows_layer_change(
    tmp_path: Path, log: list[str], spy: Spy
) -> None:
    """Without patch 0005 no stackup is ever written back, so a stale stackup (KiCad
    without the patch does not re-sync it) does not block a layer change."""
    board = _jlc_board(log)
    board.error = FakeBoard(log).error  # no handler
    board.copper = 2  # the stored stackup still describes 4 copper layers
    cs = setup._plan_rules(parse_rules_args({"layers": 4}), None, {}, 2, "hash")
    cs.verify = None
    await apply_board_change_set(_engine(tmp_path, log, board), cs)
    assert "layers 4" in log


async def test_unpatched_stale_stackup_is_unavailable(
    tmp_path: Path, log: list[str], spy: Spy
) -> None:
    board = _jlc_board(log)
    board.error = FakeBoard(log).error  # no handler
    board.copper = 2
    req = parse_rules_args({"stackup": "jlc-2l-1.6"})
    cs = setup._plan_rules(req, None, {}, 2, "hash", plan_stackup(req, 2))
    with pytest.raises(StructuredToolError) as info:
        await apply_board_change_set(_engine(tmp_path, log, board), cs)
    assert info.value.code == "stackup_unavailable"


@pytest.mark.parametrize(
    ("edit", "why"),
    [
        (
            lambda s: [
                s.layers[2].dielectric.layer.add(thickness={"value_nm": 1000}) for _ in range(32)
            ],
            "sub-layers",
        ),
        (lambda s: setattr(s.layers[2].dielectric.layer[0], "epsilon_r", math.nan), "material"),
        (lambda s: setattr(s.layers[2].dielectric.layer[0], "loss_tangent", -1.0), "material"),
        (lambda s: setattr(s.layers[0].soldermask, "epsilon_r", math.inf), "material"),
        (
            lambda s: s.layers[4].dielectric.layer[0].thickness.__setattr__("value_nm", 10_000_001),
            "10 mm",
        ),
        (
            lambda s: (
                [
                    s.layers[i].dielectric.layer[0].thickness.__setattr__("value_nm", 9_000_000)
                    for i in (2, 4, 6)
                ]
                and [
                    s.layers[2].dielectric.layer.add(thickness={"value_nm": 9_000_000})
                    for _ in range(10)
                ]
            ),
            "100 mm",
        ),
    ],
)
def test_unrestorable_limits(edit: Any, why: str) -> None:
    s = _default_4l()
    assert setup._unrestorable(s, 4) is None
    edit(s)
    assert why in (setup._unrestorable(s, 4) or "")
