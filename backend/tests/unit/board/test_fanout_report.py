"""`pcb_fanout`'s pad classification (SP4 P7): a pad without a via counts as `via_pour`
only on a DRC of the final copper (no removal or refill since)."""

from __future__ import annotations

from copperchat.board.model import Pad
from copperchat.kicad_tools.board.api import DrcMarker, DrcResult
from copperchat.kicad_tools.board.fanout import _Fanout, _PadState, _report

from .builders import board, part, zone

PAD = Pad("U1", "1", "GND", 30, 30, 0.4, 0.4, 0.0, "rect", frozenset({"F.Cu"}), True, None)


def _model() -> object:
    u1 = part("U1", 30, 30, pads=(PAD,), item_kiids=(("pad-k", "pad:1"),))
    return board(parts=(u1,), zones=(zone("GND_F", "GND", ("F.Cu",)),))


def _state(fresh: bool, markers: tuple[DrcMarker, ...] = ()) -> _Fanout:
    st = _Fanout("GND", (0.6, 0.3), 0.2, 0.2, 0.25, [_PadState(PAD, ("U1", "1"), "F.Cu", ())], 0)
    st.pads[0].status = "exhausted"
    st.last_drc = DrcResult(markers=markers, unconnected=len(markers), seconds=0.0)
    st.drc_fresh = fresh
    return st


def test_fresh_drc_without_marker_is_via_pour() -> None:
    rep = _report(_state(True), _model())  # type: ignore[arg-type]
    assert rep["via_pour"] == 1 and rep["unconnected"] == []


def test_stale_drc_is_unconnected() -> None:
    rep = _report(_state(False), _model())  # type: ignore[arg-type]
    assert rep["via_pour"] == 0
    assert rep["unconnected"] == [{"ref": "U1", "pad": "1", "reason": "no_candidate"}]


def test_unconnected_marker_on_the_pad() -> None:
    m = DrcMarker("unconnected_items", "error", "x", ("pad-k", "other"), None)
    rep = _report(_state(True, (m,)), _model())  # type: ignore[arg-type]
    assert rep["via_pour"] == 0 and rep["unconnected"][0]["reason"] == "no_candidate"
