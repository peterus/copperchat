"""`pcb_fanout` (SP4 spec §7.2 "Copper", §11, P7 Task 3): every SMD pad of a plane net gets
a short exactly octilinear stub + through via (both **locked**, no via-in-pad), legalised
by KiCad: candidates (`planes.fanout_candidates`, once, in the CPU worker) are tried in
DRC rounds (spike 4A Q5).

Per round i = 1 ... MAX_ROUNDS three stages (a round without open pads is a no-op):
`round_i_add` (one commit: every open pad's next candidate), `round_i_refill` (the zones,
so the vias land in the planes; `sctx.refill` - KiCad applies a refill at once, no commit),
`round_i_check` (the DRC task on that pushed state; the stub + via of every pad named by a
marker other than unconnected-items - a dangling via included - removed in one commit, the
pad moves on to its next candidate). The DRC only ever sees pushed state.

Deadline `FANOUT_DEADLINE_S` from the start of the call: checked before every round;
inside, a refill gets min(REFILL_TIMEOUT_S, remaining) (none below REFILL_MIN_S) and a DRC
min(60, remaining). A skipped / timed-out refill or DRC removes that round's unchecked
items (their pads: `timeout`), then one cleanup refill with the full REFILL_TIMEOUT_S runs;
also when the last check removed items and no refill followed (final refill) - at most
one of the two. Pads that never got a via: `via_pour` when a pour of the net on the
pad's layer connects them (no unconnected-items marker names the pad), else
`unconnected` (`no_candidate`, `all_rejected` or `timeout`)."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from kipy.board_types import Track as KipyTrack
from kipy.board_types import Via as KipyVia
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common.types import KIID
from kipy.util.board_layer import layer_from_canonical_name

from copperchat.board.geom import _bbox_of, _pad_rect
from copperchat.board.jobs import fanout_candidates_job, strip_fills
from copperchat.board.model import BoardModel, Pad, Point
from copperchat.board.planes import (
    ViaCandidate,
    fanout_pads,
    hole_to_hole,
    pad_key,
    pad_layer,
    vias_spaced,
)
from copperchat.board.worker import run_cpu
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board.api import (
    REFILL_MIN_S,
    REFILL_TIMEOUT_S,
    DrcResult,
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
from copperchat.kicad_tools.board.read import BOARD_READ_TIMEOUT_S, board_model, require_board
from copperchat.kicad_tools.board.route import class_params
from copperchat.kicad_tools.board.setup import require_board_tools
from copperchat.kicad_tools.board.zones import check_net, invalid, keys, m11_report, number, text
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical
from copperchat.kicad_tools.units import mm_to_nm
from copperchat.tools.spec import ToolContext, ToolResult

__all__ = [
    "FANOUT_DEADLINE_S",
    "MAX_ROUNDS",
    "MAX_STUB_MM",
    "apply_fanout",
    "create_copper",
    "parse_via",
    "prepare_fanout",
    "remove_copper",
]

FANOUT_DEADLINE_S = 300.0
MAX_ROUNDS = 16
DRC_TIMEOUT_S = 60.0
CANDIDATES_TIMEOUT_S = 30.0
MIN_STUB_MM, MAX_STUB_MM, DEFAULT_STUB_MM = 0.1, 3.0, 1.0
MAX_VIA_DIAMETER_MM, MAX_VIA_DRILL_MM = 2.0, 1.0
MAX_NET_CHARS = 256
IGNORED_DRC = frozenset({"unconnected_items"})
MAX_LISTED = 50


# -- shared with pcb_stitch ----------------------------------------------------------------


def parse_via(value: Any) -> tuple[float, float] | None:
    """`via: {diameter, drill}` (drill < diameter) or None."""
    if value is None:
        return None
    v = keys(value, {"diameter", "drill"}, "via")
    if set(v) != {"diameter", "drill"}:
        raise invalid("via needs diameter and drill")
    d = number(v["diameter"], "via.diameter", 0.0, MAX_VIA_DIAMETER_MM)
    drill = number(v["drill"], "via.drill", 0.0, MAX_VIA_DRILL_MM)
    if not drill < d:
        raise invalid("via.drill must be smaller than via.diameter")
    return d, drill


def _vec(p: Point) -> Vector2:
    return Vector2.from_xy(mm_to_nm(p[0]), mm_to_nm(p[1]))


def _kiids(ids: Sequence[str]) -> list[KIID]:
    out = []
    for value in sorted(ids):
        k = KIID()
        k.value = value
        out.append(k)
    return out


@dataclass(frozen=True)
class NewCopper:
    """One locked via, with the stub that leads to it (fanout) or none (stitching)."""

    net: str
    at: Point
    via: tuple[float, float] | None  # None: a tie stub only (R7, `ViaCandidate.tie`)
    stub: tuple[str, Point, Point, float] | None = None  # (layer, start, end, width)


def create_copper(h: KicadHandle, items: Sequence[NewCopper]) -> list[list[str]]:
    """Locked vias (+ stubs) in one batch, inside the caller's commit; returns per item its
    created KIIDs (stub first)."""
    board = h.board()
    nets = {n.name: n for n in board.get_nets()}
    made: list[Any] = []
    sizes: list[int] = []
    for it in items:
        net = nets.get(it.net)
        if net is None:
            raise StructuredToolError(
                "Net not on the board",
                f"KiCad has no net {it.net!r} (the board changed since it was read)",
                "Re-read the board (pcb_status) and retry.",
                code="not_found",
            )
        n = 0
        if it.stub is not None:
            layer, a, b, width = it.stub
            t = KipyTrack()
            t.layer = layer_from_canonical_name(layer)
            t.net = net
            t.start, t.end = _vec(a), _vec(b)
            t.width = mm_to_nm(width)
            t.locked = True
            made.append(t)
            n += 1
        if it.via is None:
            sizes.append(n)
            continue
        v = KipyVia()
        v.position = _vec(it.at)
        v.net = net
        if not len(v.padstack.proto.copper_layers):  # PST_NORMAL: one entry, all layers
            v.padstack.proto.copper_layers.add().layer = BoardLayer.BL_F_Cu
        v.diameter = mm_to_nm(it.via[0])
        v.drill_diameter = mm_to_nm(it.via[1])
        v.locked = True
        made.append(v)
        sizes.append(n + 1)
    created = [str(c.id.value) for c in board.create_items(made)] if made else []
    if len(created) != len(made):
        raise RuntimeError(f"created {len(created)} of {len(made)} items")
    out: list[list[str]] = []
    pos = 0
    for n in sizes:
        out.append(created[pos : pos + n])
        pos += n
    return out


async def push_copper(
    sctx: BoardStageContext, tool: str, items: Sequence[NewCopper]
) -> list[list[str]]:
    async with sctx.ctx.session.commit(f"copperchat: {tool}", doc="pcb"):
        made: list[list[str]] = await sctx.call(
            lambda h: create_copper(h, items), timeout=BOARD_READ_TIMEOUT_S
        )
    for ids in made:
        sctx.created.update(ids)
    return made


async def remove_copper(sctx: BoardStageContext, tool: str, ids: Sequence[str]) -> None:
    """Delete this call's items `ids` in one commit."""
    if not ids:
        return
    batch = sorted(set(ids))
    async with sctx.ctx.session.commit(f"copperchat: {tool}: reject", doc="pcb"):
        await sctx.call(
            lambda h: h.board().remove_items_by_id(_kiids(batch)), timeout=BOARD_READ_TIMEOUT_S
        )
    sctx.created.difference_update(batch)


def rejected_kiids(drc: DrcResult, mine: set[str]) -> set[str]:
    """This call's KIIDs named by a DRC marker other than unconnected-items (exclusions
    ignored) - a dangling via included."""
    out: set[str] = set()
    for m in drc.markers:
        if m.type in IGNORED_DRC or m.severity == "exclusion":
            continue
        out |= mine.intersection(m.kiids)
    return out


def remaining(deadline: float) -> float:
    return deadline - monotonic()


# -- arguments -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FanoutArgs:
    net: str
    via: tuple[float, float] | None
    max_stub: float


def parse_fanout_args(args: Any) -> FanoutArgs:
    a = keys(args, {"net", "via", "max_stub"})
    if "net" not in a:
        raise invalid("net is required")
    net = text(a["net"], "net", MAX_NET_CHARS)
    stub = DEFAULT_STUB_MM
    if "max_stub" in a:
        stub = number(a["max_stub"], "max_stub", MIN_STUB_MM, MAX_STUB_MM, lo_open=False)
    return FanoutArgs(net, parse_via(a.get("via")), stub)


# -- the rounds ----------------------------------------------------------------------------


@dataclass
class _PadState:
    pad: Pad
    key: tuple[str, str]
    layer: str
    cands: tuple[ViaCandidate, ...]
    next: int = 0  # index of the next candidate to try
    tried: int = 0  # candidates DRC-rejected so far
    status: str = "open"  # open | pending | fanned | exhausted | timeout
    kiids: list[str] = field(default_factory=list)  # pending or kept stub + via
    at: Point | None = None


@dataclass
class _Fanout:
    net: str
    via: tuple[float, float]
    width: float
    clearance: float
    h2h: float
    pads: list[_PadState]
    already: int
    deadline: float = 0.0
    rounds: int = 0  # rounds that added copper
    drc_seconds: float = 0.0
    need_refill: bool = False  # removals since the last refill
    last_drc: DrcResult | None = None
    stopped: bool = False  # the deadline stopped the rounds
    refills: int = 0
    drc_fresh: bool = False  # last_drc saw the current copper (no removal / refill since)

    def pending(self) -> list[_PadState]:
        return [p for p in self.pads if p.status == "pending"]


def _pick(st: _Fanout) -> list[_PadState]:
    """Each open pad's next candidate that keeps clear of the vias kept or picked this
    round (same net: spacing and the hole rule); a pad with none left is exhausted."""
    reach = max(st.via[0] + st.clearance, st.via[1] + st.h2h)
    taken: list[Point] = []
    grid: dict[tuple[int, int], list[Point]] = {}  # taken vias bucketed by `reach`

    def cell(p: Point) -> tuple[int, int]:
        return math.floor(p[0] / reach), math.floor(p[1] / reach)

    def take(p: Point) -> None:
        taken.append(p)
        grid.setdefault(cell(p), []).append(p)

    def clear(p: Point) -> bool:
        cx, cy = cell(p)
        return all(
            vias_spaced(p, t, via=st.via, clearance=st.clearance, h2h=st.h2h)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for t in grid.get((cx + dx, cy + dy), ())
        )

    for p in st.pads:
        if p.status == "fanned" and p.at is not None and not p.cands[p.next].tie:
            take(p.at)
    picked: list[_PadState] = []
    for ps in st.pads:
        if ps.status != "open":
            continue
        while (
            ps.next < len(ps.cands)
            and not ps.cands[ps.next].tie
            and not clear(ps.cands[ps.next].at)
        ):
            ps.next += 1
        if ps.next >= len(ps.cands):
            ps.status = "exhausted"
            continue
        c = ps.cands[ps.next]
        ps.at = c.at
        if not c.tie:
            take(c.at)
        picked.append(ps)
    return picked


async def _time_out_pending(sctx: BoardStageContext, st: _Fanout) -> None:
    """A skipped or timed-out refill / DRC: this round's unchecked items go, their pads
    are reported `timeout`; the cleanup refill follows at the end."""
    pend = st.pending()
    st.drc_fresh = False
    await remove_copper(sctx, "pcb_fanout", [k for p in pend for k in p.kiids])
    for p in pend:
        p.status, p.kiids, p.at = "timeout", [], None
    st.need_refill = True
    st.stopped = True


def _round_stages(st: _Fanout, i: int) -> list[BoardStage]:
    async def add(sctx: BoardStageContext) -> None:
        if st.stopped or not any(p.status == "open" for p in st.pads):
            return
        if remaining(st.deadline) <= 0:
            st.stopped = True
            return
        picked = _pick(st)
        if not picked:
            return
        items = [
            NewCopper(
                st.net,
                p.cands[p.next].at,
                None if p.cands[p.next].tie else st.via,
                (p.layer, (p.pad.x, p.pad.y), p.cands[p.next].at, st.width),
            )
            for p in picked
        ]
        made = await push_copper(sctx, "pcb_fanout", items)
        for p, ids in zip(picked, made, strict=True):
            p.status, p.kiids = "pending", ids
        st.rounds += 1
        st.drc_fresh = False

    async def refill(sctx: BoardStageContext) -> None:
        if not st.pending():
            return
        left = remaining(st.deadline)
        if left < REFILL_MIN_S:
            await _time_out_pending(sctx, st)
            return
        try:
            await sctx.refill(timeout_s=min(REFILL_TIMEOUT_S, left))
        except RefillTimeout:
            await _time_out_pending(sctx, st)
            return
        st.refills += 1
        st.need_refill = False

    async def check(sctx: BoardStageContext) -> None:
        pend = st.pending()
        if not pend:
            return
        left = remaining(st.deadline)
        if left <= 0:
            await _time_out_pending(sctx, st)
            return
        try:
            drc = await sctx.drc(timeout_s=min(DRC_TIMEOUT_S, left))
        except DrcTimeout:
            await _time_out_pending(sctx, st)
            return
        st.drc_seconds += drc.seconds
        st.last_drc = drc
        bad = rejected_kiids(drc, {k for p in pend for k in p.kiids})
        rejected = [p for p in pend if bad.intersection(p.kiids)]
        await remove_copper(sctx, "pcb_fanout", [k for p in rejected for k in p.kiids])
        for p in pend:
            if p in rejected:
                p.status, p.kiids, p.at = "open", [], None
                p.next += 1
                p.tried += 1
            else:
                p.status = "fanned"
        if rejected:
            st.need_refill = True
        st.drc_fresh = not rejected

    return [
        BoardStage(f"round_{i}_add", add, commit=False),
        BoardStage(f"round_{i}_refill", refill, commit=False),
        BoardStage(f"round_{i}_check", check, commit=False),
    ]


def _final_stage(st: _Fanout) -> BoardStage:
    async def final(sctx: BoardStageContext) -> None:
        for p in st.pads:
            if p.status == "open":  # the deadline (or the round limit) stopped it
                p.status = "timeout" if st.stopped else "exhausted"
        if st.need_refill:  # the cleanup or the final refill: at most one
            await sctx.refill(timeout_s=REFILL_TIMEOUT_S)
            st.refills += 1
            st.need_refill = False
            st.drc_fresh = False
        # `via_pour` needs a DRC of the final copper: the last check's DRC may predate a
        # removal (a rejected stub/via) or the cleanup refill. Without time left the
        # pads stay `unconnected` (`_report` trusts only a fresh DRC).
        # A deadline-stopped call spends no more time: its pads stay `unconnected`.
        if not st.stopped and not st.drc_fresh and any(p.status == "exhausted" for p in st.pads):
            left = remaining(st.deadline)
            if left > 0:
                try:
                    st.last_drc = await sctx.drc(timeout_s=min(DRC_TIMEOUT_S, left))
                    st.drc_seconds += st.last_drc.seconds
                    st.drc_fresh = True
                except DrcTimeout:
                    pass

    return BoardStage("final_refill", final, commit=False)


# -- report ----------------------------------------------------------------------------------


def _pad_kiids(model: BoardModel) -> dict[tuple[str, str, float, float], str]:
    """(ref, number, x, y) -> pad KIID (`item_kiids` lists the pads in model order)."""
    out: dict[tuple[str, str, float, float], str] = {}
    for part in model.parts:
        ids = [k for k, role in part.item_kiids if role.startswith("pad:")]
        for q, kiid in zip(part.pads, ids, strict=False):
            out[(q.ref, q.number, q.x, q.y)] = kiid
    return out


def _pour_on(model: BoardModel, net: str, layer: str) -> bool:
    return any(not z.rule_area and z.net == net and layer in z.layers for z in model.zones)


def _tie_without_via(
    st: _Fanout, p: _PadState, poured: Callable[[_PadState], bool] = lambda q: False
) -> bool:
    """`p` was fanned by a tie stub (R7) into a pad of this call that never got a via of
    its own and does not reach the plane through the pour either (`poured`; review round
    3); a pad not fanned here was already connected before the call."""
    if p.status != "fanned" or p.next >= len(p.cands) or not p.cands[p.next].tie:
        return False
    end = p.cands[p.next].at
    for q in st.pads:
        if q is p or q.pad.ref != p.pad.ref or not _in_pad_rect(end, q.pad):
            continue
        via = q.status == "fanned" and q.next < len(q.cands) and not q.cands[q.next].tie
        return not via and not poured(q)
    return False


def _in_pad_rect(pt: Point, q: Pad) -> bool:
    x0, y0, x1, y1 = _bbox_of(_pad_rect(q))
    return x0 - 1e-6 <= pt[0] <= x1 + 1e-6 and y0 - 1e-6 <= pt[1] <= y1 + 1e-6


def _report(st: _Fanout, model: BoardModel) -> dict[str, Any]:
    unconnected_kiids: set[str] = set()
    if st.last_drc is not None:
        for m in st.last_drc.markers:
            if m.type in IGNORED_DRC:
                unconnected_kiids.update(m.kiids)
    pad_ids = _pad_kiids(model)

    def poured(q: _PadState) -> bool:
        """`q` (no via of its own) reaches its plane through the pour."""
        return (
            q.status != "timeout"
            and st.last_drc is not None
            and st.drc_fresh
            and _pour_on(model, st.net, q.layer)
            and (kid := pad_ids.get((q.pad.ref, q.pad.number, q.pad.x, q.pad.y))) is not None
            and kid not in unconnected_kiids
        )

    dangling = {id(p) for p in st.pads if _tie_without_via(st, p, poured)}
    fanned = [p for p in st.pads if p.status == "fanned" and id(p) not in dangling]
    pour: list[dict[str, str]] = []
    unconnected: list[dict[str, str]] = []
    for p in st.pads:
        if id(p) in dangling:
            # review round 1: a tie only connects when the pad it ties to got its own via
            unconnected.append(
                {"ref": p.key[0], "pad": p.key[1], "reason": "tie_target_unconnected"}
            )
            continue
        if p.status == "fanned":
            continue
        reason = (
            "timeout"
            if p.status == "timeout"
            # a pad whose candidates all clashed with nearer vias never reached the DRC
            else ("all_rejected" if p.tried else "no_candidate")
        )
        if reason != "timeout" and poured(p):
            pour.append({"ref": p.key[0], "pad": p.key[1]})
        else:
            unconnected.append({"ref": p.key[0], "pad": p.key[1], "reason": reason})
    return {
        "net": st.net,
        "smd_pads": len(st.pads) + st.already,
        "already_connected": st.already,
        "fanned_out": len(fanned),
        "tied": sum(1 for p in fanned if p.next < len(p.cands) and p.cands[p.next].tie),
        "via_pour": len(pour),
        "via_pour_pads": pour[:MAX_LISTED],
        "unconnected": unconnected[:MAX_LISTED],
        "unconnected_omitted": max(0, len(unconnected) - MAX_LISTED),
        "rounds": st.rounds,
        "refills": st.refills,
        "drc_seconds": round(st.drc_seconds, 2),
        "stopped_by_deadline": st.stopped,
        "via": {"diameter": st.via[0], "drill": st.via[1]},
        "stub_width": st.width,
    }


# -- the tool ------------------------------------------------------------------------------


@dataclass(kw_only=True)
class FanoutChangeSet(BoardChangeSet):
    state: _Fanout
    read_back: list[BoardModel] = field(default_factory=list)


async def prepare_fanout(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_fanout` (module docstring): checks, then the candidates (CPU worker)."""
    require_board_tools(ctx)
    a = parse_fanout_args(args)
    require_board(ctx)
    model = await board_model(ctx)
    await require_placement_approved(ctx, model)
    check_net(model, a.net)
    params = class_params(model, a.net)
    via = a.via if a.via is not None else params.via
    start = monotonic()
    cands: Mapping[tuple[str, str], tuple[ViaCandidate, ...]] = await run_cpu(
        fanout_candidates_job,
        strip_fills(model),
        a.net,
        via=via,
        width=params.width,
        clearance=params.clearance,
        max_stub=a.max_stub,
        timeout_s=CANDIDATES_TIMEOUT_S,
    )
    todo, done = await asyncio.to_thread(fanout_pads, model, a.net)
    pads: list[_PadState] = []
    seen: dict[tuple[str, str], object] = {}
    for q in todo:
        key = pad_key(q, seen)
        seen[key] = True
        layer = pad_layer(q)
        assert layer is not None
        pads.append(_PadState(q, key, layer, tuple(cands.get(key, ()))))
    st = _Fanout(a.net, via, params.width, params.clearance, hole_to_hole(model), pads, len(done))
    st.deadline = start + FANOUT_DEADLINE_S
    stages = [s for i in range(1, MAX_ROUNDS + 1) for s in _round_stages(st, i)]
    stages.append(_final_stage(st))

    def verify(m: BoardModel, rep: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        items = {t.kiid: t.locked for t in m.tracks} | {v.kiid: v.locked for v in m.vias}
        for p in st.pads:
            for k in p.kiids if p.status == "fanned" else ():
                if k not in items:
                    problems.append(f"fanout item {k} of {p.key[0]}.{p.key[1]} is missing")
                elif not items[k]:
                    problems.append(f"fanout item {k} is not locked")
        rep.update(_report(st, m))
        cs.read_back[:] = [m]
        return problems

    cs = FanoutChangeSet(
        tool="pcb_fanout",
        logical=Logical(2 if pads else 0),
        kind="board_fanout",
        board_stages=stages if pads else [],
        verify=verify,
        report={"logical_changes": 2 if pads else 0, "budget": None},
        state=st,
    )
    if not pads:
        cs.report.update(_report(st, model))
    return cs


async def apply_fanout(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    """Apply a `pcb_fanout` change set; the report gets M11 of the result."""
    assert isinstance(cs, FanoutChangeSet)
    t0 = time.monotonic()
    if not cs.board_stages:  # nothing to fan out: nothing written, cost 0
        data = dict(cs.report) | {"seconds": 0.0, "m11": None, "next": "pcb_stitch"}
        return ToolResult(data=data)
    result = await apply_board_change_set(ctx, cs)
    data = result.data if isinstance(result.data, dict) else {}
    data["seconds"] = round(time.monotonic() - t0, 2)
    # the no-op rounds are not worth a line each
    data["step_report"] = [
        s
        for s in data.get("step_report", ())
        if not s["stage"].startswith("round_")
        or int(s["stage"].split("_")[1]) <= max(cs.state.rounds, 1)
    ]
    data["m11"] = await m11_report(ctx, cs.read_back[0]) if cs.read_back else None
    data["next"] = "pcb_stitch"
    return result
