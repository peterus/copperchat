"""`pcb_fanout`'s round / final stages without KiCad (SP4 P7, review round 2): the
`drc_fresh` state machine decides whether `_report` may trust the last DRC for
`via_pour`; KiCad writes, refills and DRCs are fakes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from copperchat.board.model import Pad
from copperchat.board.planes import ViaCandidate
from copperchat.kicad_tools.board import fanout
from copperchat.kicad_tools.board.api import DrcMarker, DrcResult, DrcTimeout
from copperchat.kicad_tools.board.fanout import (
    _Fanout,
    _final_stage,
    _PadState,
    _report,
    _round_stages,
)

from .builders import board, part, zone


def _pad(n: str, x: float) -> Pad:
    return Pad("U1", n, "GND", x, 30, 0.4, 0.4, 0.0, "rect", frozenset({"F.Cu"}), True, None)


A, B = _pad("1", 30), _pad("2", 40)  # A has a candidate, B none (exhausted)
MODEL = board(
    parts=(part("U1", 35, 30, half=6, pads=(A, B), item_kiids=(("ka", "pad:1"), ("kb", "pad:2"))),),
    zones=(zone("GND_F", "GND", ("F.Cu",)),),
)


class FakeSctx:
    def __init__(self, drcs: list[Any]) -> None:
        self.drcs = drcs  # DrcResult or an exception per DRC call
        self.log: list[str] = []

    async def refill(self, *, timeout_s: float) -> None:
        self.log.append("refill")

    async def drc(self, *, timeout_s: float = 60.0) -> DrcResult:
        self.log.append("drc")
        got = self.drcs.pop(0)
        if isinstance(got, Exception):
            raise got
        return got


def _drc(*markers: DrcMarker) -> DrcResult:
    return DrcResult(markers=markers, unconnected=0, seconds=0.0)


@pytest.fixture(autouse=True)
def fake_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    n = [0]

    async def push(sctx: Any, tool: str, items: Any) -> list[list[str]]:
        out = []
        for _ in items:
            n[0] += 1
            out.append([f"t{n[0]}", f"v{n[0]}"])
        return out

    async def remove(sctx: Any, tool: str, ids: Any) -> None:
        if ids:  # the real one returns at once for none
            sctx.log.append(f"remove {len(ids)}")

    monkeypatch.setattr(fanout, "push_copper", push)
    monkeypatch.setattr(fanout, "remove_copper", remove)


def _state(rounds: int = 2) -> tuple[_Fanout, list[Any]]:
    cand = ViaCandidate((30, 31.5), ((30, 30), (30, 31.5)), ("U1", "1"), 0)
    st = _Fanout(
        "GND",
        (0.6, 0.3),
        0.2,
        0.2,
        0.25,
        [_PadState(A, ("U1", "1"), "F.Cu", (cand,)), _PadState(B, ("U1", "2"), "F.Cu", ())],
        0,
    )
    st.deadline = fanout.monotonic() + 300
    stages = [s for i in range(1, rounds + 1) for s in _round_stages(st, i)] + [_final_stage(st)]
    return st, stages


def _run(st: _Fanout, stages: list[Any], sctx: FakeSctx) -> dict[str, Any]:
    async def go() -> None:
        for s in stages:
            await s.run(sctx)

    asyncio.run(go())
    return _report(st, MODEL)


def test_clean_round_trusts_its_drc() -> None:
    st, stages = _state()
    sctx = FakeSctx([_drc()])
    rep = _run(st, stages, sctx)
    assert sctx.log == ["refill", "drc"]  # no final DRC: round 1's saw the final copper
    assert rep["fanned_out"] == 1 and rep["via_pour"] == 1


def test_rejection_refills_and_checks_again() -> None:
    st, stages = _state(rounds=1)
    bad = DrcMarker("clearance", "error", "x", ("v1",), None)
    sctx = FakeSctx([_drc(bad), _drc()])
    rep = _run(st, stages, sctx)
    assert sctx.log == ["refill", "drc", "remove 2", "refill", "drc"]
    assert rep["fanned_out"] == 0 and rep["via_pour"] == 2  # final DRC fresh, no markers


def test_final_drc_marker_keeps_pad_unconnected() -> None:
    st, stages = _state(rounds=1)
    bad = DrcMarker("clearance", "error", "x", ("v1",), None)
    unc = DrcMarker("unconnected_items", "error", "x", ("kb",), None)
    rep = _run(st, stages, FakeSctx([_drc(bad), _drc(unc)]))
    assert {u["pad"] for u in rep["unconnected"]} == {"2"}


def test_final_drc_timeout_leaves_pads_unconnected() -> None:
    st, stages = _state(rounds=1)
    bad = DrcMarker("clearance", "error", "x", ("v1",), None)
    rep = _run(st, stages, FakeSctx([_drc(bad), DrcTimeout(60, "slow")]))
    assert rep["via_pour"] == 0 and len(rep["unconnected"]) == 2


def test_stopped_call_runs_no_final_drc(monkeypatch: pytest.MonkeyPatch) -> None:
    st, stages = _state()
    st.deadline = fanout.monotonic() - 1  # passed before round 1
    sctx = FakeSctx([])
    rep = _run(st, stages, sctx)
    assert sctx.log == []
    assert rep["via_pour"] == 0
    assert {u["reason"] for u in rep["unconnected"]} == {"timeout"}  # never tried
