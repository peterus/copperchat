"""`pcb_stitch` (SP4 spec §7.2 "Copper", §11, P7 Task 4): stitching vias of a plane net on
a grid (default pitch 5 mm, spec call 31) and optionally an edge ring, all **locked**,
legalised by KiCad. Candidates (`planes.stitch_candidates`, CPU worker) lie only where
zones of the net on two layers would connect them.

Four stages: (1) every candidate via (one commit); (2) refill (`sctx.refill`); (3) the DRC
task on that pushed state - vias in any marker other than unconnected-items removed, the
ones KiCad reports dangling (no pour reached them) counted apart (one commit); (4) the
refill after the removals (the pours must not keep fills shaped by removed vias).

Deadline `STITCH_DEADLINE_S` from the start of the call, checked before every stage; the
stage-2 refill gets min(REFILL_TIMEOUT_S, remaining) (skipped below REFILL_MIN_S), the DRC
min(60, remaining). A skipped / timed-out refill or DRC removes every via the DRC has not
cleared (`rejected_timeout`); stage 4 is then the cleanup refill with the full
REFILL_TIMEOUT_S (the one permitted overrun). More than MAX_STITCH_VIAS candidates are
refused before any write."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from copperchat.board.jobs import stitch_candidates_job, strip_fills
from copperchat.board.model import BoardModel, Point
from copperchat.board.planes import ViaCandidate, edge_ring_points
from copperchat.board.worker import run_cpu
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board.api import (
    REFILL_MIN_S,
    REFILL_TIMEOUT_S,
    DrcTimeout,
    RefillTimeout,
)
from copperchat.kicad_tools.board.checkpoints import require_placement_approved
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.board.fanout import (
    IGNORED_DRC,
    NewCopper,
    parse_via,
    push_copper,
    remove_copper,
)
from copperchat.kicad_tools.board.read import board_model, require_board
from copperchat.kicad_tools.board.route import class_params
from copperchat.kicad_tools.board.setup import require_board_tools
from copperchat.kicad_tools.board.zones import check_net, invalid, keys, m11_report, number, text
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical
from copperchat.tools.spec import ToolContext, ToolResult

__all__ = [
    "MAX_PITCH_MM",
    "MAX_STITCH_VIAS",
    "MIN_PITCH_MM",
    "STITCH_DEADLINE_S",
    "apply_stitch",
    "prepare_stitch",
]

STITCH_DEADLINE_S = 180.0
MAX_STITCH_VIAS = 2000
MIN_PITCH_MM, MAX_PITCH_MM, DEFAULT_PITCH_MM = 1.0, 50.0, 5.0
DRC_TIMEOUT_S = 60.0
CANDIDATES_TIMEOUT_S = 30.0
MAX_NET_CHARS = 256
DANGLING = "dangling_via"


@dataclass(frozen=True)
class StitchArgs:
    net: str
    pitch: float
    edge_ring: bool
    via: tuple[float, float] | None


def parse_stitch_args(args: Any) -> StitchArgs:
    a = keys(args, {"net", "pitch", "edge_ring", "via"})
    if "net" not in a:
        raise invalid("net is required")
    net = text(a["net"], "net", MAX_NET_CHARS)
    pitch = DEFAULT_PITCH_MM
    if "pitch" in a:
        pitch = number(a["pitch"], "pitch", MIN_PITCH_MM, MAX_PITCH_MM, lo_open=False)
    ring = a.get("edge_ring", False)
    if not isinstance(ring, bool):
        raise invalid("edge_ring must be true or false")
    return StitchArgs(net, pitch, ring, parse_via(a.get("via")))


@dataclass
class _Stitch:
    net: str
    via: tuple[float, float]
    cands: tuple[ViaCandidate, ...]
    ring: frozenset[Point]
    deadline: float = 0.0
    kiids: dict[str, Point] = field(default_factory=dict)  # created via -> position
    kept: set[str] = field(default_factory=set)
    rejected_drc: int = 0
    removed_dangling: int = 0
    rejected_timeout: int = 0
    cleared: bool = False  # the DRC cleared the remaining vias
    timed_out: bool = False
    removed: bool = False  # vias removed after the stage-2 refill
    drc_seconds: float = 0.0


def _remaining(st: _Stitch) -> float:
    return st.deadline - monotonic()


async def _time_out(sctx: BoardStageContext, st: _Stitch) -> None:
    """Every via the DRC has not cleared goes (`rejected_timeout`)."""
    if st.cleared or st.timed_out:
        return
    gone = sorted(st.kiids)
    await remove_copper(sctx, "pcb_stitch", gone)
    st.rejected_timeout += len(gone)
    st.kiids.clear()
    st.timed_out = True
    st.removed = True


def _stages(st: _Stitch) -> list[BoardStage]:
    async def add(sctx: BoardStageContext) -> None:
        if _remaining(st) <= 0:
            st.timed_out = True
            st.rejected_timeout = len(st.cands)
            return
        items = [NewCopper(st.net, c.at, st.via) for c in st.cands]
        made = await push_copper(sctx, "pcb_stitch", items)
        for c, ids in zip(st.cands, made, strict=True):
            st.kiids[ids[-1]] = c.at

    async def refill(sctx: BoardStageContext) -> None:
        if st.timed_out or not st.kiids:
            return
        left = _remaining(st)
        if left < REFILL_MIN_S:
            await _time_out(sctx, st)
            return
        try:
            await sctx.refill(timeout_s=min(REFILL_TIMEOUT_S, left))
        except RefillTimeout:
            await _time_out(sctx, st)

    async def check(sctx: BoardStageContext) -> None:
        if st.timed_out or not st.kiids:
            return
        left = _remaining(st)
        if left <= 0:
            await _time_out(sctx, st)
            return
        try:
            drc = await sctx.drc(timeout_s=min(DRC_TIMEOUT_S, left))
        except DrcTimeout:
            await _time_out(sctx, st)
            return
        st.drc_seconds += drc.seconds
        mine = set(st.kiids)
        dangling: set[str] = set()
        other: set[str] = set()
        for m in drc.markers:
            if m.type in IGNORED_DRC or m.severity == "exclusion":
                continue
            hit = mine.intersection(m.kiids)
            (dangling if m.type == DANGLING else other).update(hit)
        dangling -= other
        gone = sorted(dangling | other)
        await remove_copper(sctx, "pcb_stitch", gone)
        st.rejected_drc += len(other)
        st.removed_dangling += len(dangling)
        for k in gone:
            st.kiids.pop(k, None)
        st.kept = set(st.kiids)
        st.cleared = True
        st.removed = bool(gone)

    async def final(sctx: BoardStageContext) -> None:
        if st.removed:  # the cleanup refill (full budget: the single permitted overrun)
            await sctx.refill(timeout_s=REFILL_TIMEOUT_S)

    return [
        BoardStage("add", add, commit=False),
        BoardStage("refill", refill, commit=False),
        BoardStage("check", check, commit=False),
        BoardStage("refill_after", final, commit=False),
    ]


@dataclass(kw_only=True)
class StitchChangeSet(BoardChangeSet):
    state: _Stitch
    read_back: list[BoardModel] = field(default_factory=list)


def _report(st: _Stitch) -> dict[str, Any]:
    ring_kept = sum(1 for k in st.kept if st.kiids.get(k) in st.ring)
    return {
        "net": st.net,
        "candidates": len(st.cands),
        "kept": len(st.kept),
        "rejected_drc": st.rejected_drc,
        "removed_dangling": st.removed_dangling,
        "rejected_timeout": st.rejected_timeout,
        "edge_ring": sum(1 for c in st.cands if c.at in st.ring),
        "edge_ring_kept": ring_kept,
        "via": {"diameter": st.via[0], "drill": st.via[1]},
        "drc_seconds": round(st.drc_seconds, 2),
    }


async def prepare_stitch(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_stitch` (module docstring)."""
    require_board_tools(ctx)
    a = parse_stitch_args(args)
    require_board(ctx)
    model = await board_model(ctx)
    await require_placement_approved(ctx, model)
    check_net(model, a.net)
    params = class_params(model, a.net)
    via = a.via if a.via is not None else params.via
    start = monotonic()
    cands: tuple[ViaCandidate, ...] = await run_cpu(
        stitch_candidates_job,
        strip_fills(model),
        a.net,
        via=via,
        clearance=params.clearance,
        pitch=a.pitch,
        edge_ring=a.edge_ring,
        timeout_s=CANDIDATES_TIMEOUT_S,
    )
    if len(cands) > MAX_STITCH_VIAS:
        raise StructuredToolError(
            "Too many stitching vias",
            f"pitch {a.pitch:g} mm{' with the edge ring' if a.edge_ring else ''} gives "
            f"{len(cands)} vias (at most {MAX_STITCH_VIAS} per call)",
            "Use a larger pitch (5 mm is the default) or no edge_ring.",
            code="invalid_argument",
            details={"candidates": len(cands), "max": MAX_STITCH_VIAS},
        )
    ring = (
        frozenset(edge_ring_points(model, via=via, pitch=a.pitch)) if a.edge_ring else frozenset()
    )
    st = _Stitch(a.net, via, cands, ring)
    st.deadline = start + STITCH_DEADLINE_S

    def verify(m: BoardModel, rep: dict[str, Any]) -> list[str]:
        live = {v.kiid: v for v in m.vias}
        problems = [f"stitching via {k} is missing" for k in sorted(st.kept) if k not in live]
        problems += [
            f"stitching via {k} is not locked"
            for k in sorted(st.kept)
            if k in live and not live[k].locked
        ]
        rep.update(_report(st))
        cs.read_back[:] = [m]
        return problems

    cost = 2 if cands else 0
    cs = StitchChangeSet(
        tool="pcb_stitch",
        logical=Logical(cost),
        kind="board_stitch",
        board_stages=_stages(st) if cands else [],
        verify=verify,
        report={"logical_changes": cost, "budget": None},
        state=st,
    )
    if a.edge_ring and model.outline.kind != "rect":
        cs.report["edge_ring_note"] = (
            "no edge ring: it needs a rectangular board outline (pcb_set_outline)"
        )
    if not cands:
        cs.report.update(_report(st))
        cs.report["note"] = (
            f"no grid point is covered by zones of {a.net} on two copper layers clear of "
            "pads, courtyards and other copper: pour the planes first (pcb_zones)"
        )
    return cs


async def apply_stitch(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    """Apply a `pcb_stitch` change set; the report gets M11 of the result."""
    assert isinstance(cs, StitchChangeSet)
    t0 = time.monotonic()
    if not cs.board_stages:  # nothing to place: nothing written, cost 0
        data = dict(cs.report) | {"seconds": 0.0, "m11": None, "next": "pcb_tidy_silk"}
        return ToolResult(data=data)
    result = await apply_board_change_set(ctx, cs)
    data = result.data if isinstance(result.data, dict) else {}
    data["seconds"] = round(time.monotonic() - t0, 2)
    data["m11"] = await m11_report(ctx, cs.read_back[0]) if cs.read_back else None
    data["next"] = "pcb_tidy_silk"
    return result
