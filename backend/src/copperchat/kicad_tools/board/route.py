"""Own-router tools (SP4 spec §7.2 "Routing", §10.1, §10.2, P5b): `pcb_route` (the critical
set routed connection by connection with the octilinear router, every result checked by
KiCad's DRC task before it is kept, locked) and `pcb_unroute` (delete or `unlock_only`).

`route_connections` is the reusable per-connection loop (B-C12; P6b's rip-and-reroute and
ladder rung (c) call it too): solve in the CPU worker on the pruned model (margins
`MARGINS`), push the copper in one commit, run the DRC task, keep it when no marker of the
new items appears, else remove it in one commit and retry with the next margin. A total
deadline bounds the whole loop (checked before every margin attempt; each step gets at
most the remaining time).

Footprint-embedded keepouts (`Part.rule_areas`, e.g. an antenna keepout) are router
obstacles: the model carries no layer or keepout type for them, so each counts as a track
and via keepout on every copper layer (conservative), except for a request whose own pad
lies inside it."""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import functools
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kipy.board_types import Track as KipyTrack
from kipy.board_types import Via as KipyVia
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common.types import KIID
from kipy.util.board_layer import layer_from_canonical_name

from copperchat.board.boardmd import read_board_doc
from copperchat.board.classify import Classification, classify
from copperchat.board.geom import TrackIndex, analyse_tracks, octi_dist, point_in_polygon
from copperchat.board.metrics import connections, routed_length
from copperchat.board.model import BoardModel, NetClassInfo, Pad, Point, Rect, Track, Via, Zone
from copperchat.board.router import (
    RoutePath,
    RouteRequest,
    RouteTooLarge,
    prune_model,
    solve,
    solve_pair,
)
from copperchat.board.router.pairs import pair_coupling
from copperchat.board.staging import natural_key
from copperchat.board.worker import run_cpu
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board.api import DrcResult, DrcTimeout
from copperchat.kicad_tools.board.checkpoints import require_placement_approved
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.board.read import BOARD_READ_TIMEOUT_S, board_model, require_board
from copperchat.kicad_tools.board.setup import require_board_tools
from copperchat.kicad_tools.board.unroute_ops import CopperSelection, select_copper, unroute_stage
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical
from copperchat.kicad_tools.units import mm_to_nm
from copperchat.tools.spec import ToolContext, ToolResult

__all__ = [
    "MARGINS",
    "ROUTE_DEADLINE_S",
    "ConnPlan",
    "RouteOutcome",
    "conn_key",
    "default_layers",
    "layer_order",
    "plan_connections",
    "prepare_route",
    "prepare_unroute",
    "route_connections",
    "unlock_stage",
]

MARGINS = (0.0, 0.05, 0.10)  # extra clearance (mm) of the three attempts per connection
ROUTE_DEADLINE_S = 300.0  # pcb_route total deadline (global constraints)
SOLVE_TIMEOUT_S = 30.0  # run_cpu budget of one solve (the router stops itself after 25 s)
DRC_TIMEOUT_S = 60.0
MAX_NETS = 50
MAX_LAYERS = 32
MAX_NET_CHARS = 256
MAX_REGION_MM = 1000.0
MAX_COORD_MM = 1000.0
NETS_PER_COST = 5  # pcb_route costs ceil(nets / 5)
POWER_KINDS = frozenset({"supply", "switch_node", "high_current"})
PAIR_MATCH_MM = 3.0  # pads of two pair connections this close (each end) route as a pair
IGNORED_DRC = frozenset({"unconnected_items"})  # the connection's other airwires
MAX_LISTED = 50
NEXT_ROUTE = (
    "pcb_render view top (and bottom) with highlight_nets = the routed nets; show the user, "
    "then pcb_route_fill for the rest"
)


# -- planning (pure) -----------------------------------------------------------------------


@dataclass(frozen=True)
class ConnPlan:
    net: str
    a: Pad
    b: Pad
    req: RouteRequest
    pair: str | None  # the partner net: this plan and the partner's next plan route together


@dataclass(frozen=True)
class ConnResult:
    key: str
    net: str
    a: str  # "U1.3"
    b: str
    status: str  # routed | failed
    reason: str | None
    length_mm: float
    vias: int
    kiids: tuple[str, ...]
    coupling: dict[str, float] | None = None


@dataclass(frozen=True)
class RouteOutcome:
    routed: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]
    results: tuple[ConnResult, ...] = ()
    drc: DrcResult | None = None  # the last DRC of the loop


def _pad_name(p: Pad) -> str:
    return f"{p.ref}.{p.number}"


def conn_key(net: str, a: Pad, b: Pad) -> str:
    """`"{net}:{refA}.{padA}-{refB}.{padB}"`, pads in natural order (as the post-processor's
    keys, B-C12)."""
    a, b = sorted((a, b), key=lambda p: (natural_key(p.ref), natural_key(p.number)))
    return f"{net}:{_pad_name(a)}-{_pad_name(b)}"


def _classes_of(model: BoardModel, net: str) -> list[NetClassInfo]:
    """The net's class(es): KiCad names a pattern-assigned net's effective class by its
    constituents ("Default,Power", P2 residual); unknown names are ignored, none known →
    Default."""
    by_name = {c.name: c for c in model.net_classes}
    name = dict(model.net_class_of).get(net, "Default")
    found = [by_name[n.strip()] for n in name.split(",") if n.strip() in by_name]
    if not found and "Default" in by_name:
        found = [by_name["Default"]]
    return found


@dataclass(frozen=True)
class _Params:
    width: float
    clearance: float
    via: tuple[float, float]
    pair_width: float | None
    pair_gap: float | None


def class_params(model: BoardModel, net: str) -> _Params:
    """Width, clearance and via of `net` from its net class (B-C12 `width_from` =
    "netclass"); a composite class takes the largest value of its constituents (the
    conservative choice, like M12). Never below the board's minimum rules."""
    rules = dict(model.rules)
    classes = _classes_of(model, net)
    width = max([c.track_width for c in classes] + [rules.get("min_track_width", 0.0)])
    clearance = max([c.clearance for c in classes] + [rules.get("min_clearance", 0.0)])
    width = width if width > 0 else 0.2
    via_d = max([c.via_diameter for c in classes] + [0.0])
    via_drill = max([c.via_drill for c in classes] + [0.0])
    if not via_d > via_drill > 0:
        via_d, via_drill = 0.6, 0.3
    pw = [c.diff_pair_width for c in classes if c.diff_pair_width]
    pg = [c.diff_pair_gap for c in classes if c.diff_pair_gap]
    return _Params(
        width, clearance, (via_d, via_drill), max(pw) if pw else None, max(pg) if pg else None
    )


def layer_order(model: BoardModel, kind: str) -> tuple[str, ...]:
    """The copper layers in routing preference (the router prefers the first, B-C12):
    power nets (supply / switch node / high current) outer layers first, then the inner
    ones in board order; every other net in board order."""
    copper = tuple(model.copper_layers)
    if kind not in POWER_KINDS:
        return copper
    outer = tuple(n for n in copper if n in ("F.Cu", "B.Cu"))
    return outer + tuple(n for n in copper if n not in outer)


def default_layers(model: BoardModel) -> tuple[str, ...]:
    """The layers `pcb_route` may use when the call names none: every copper layer except
    the default plane layers of the stack (owner D1: In1.Cu on 4 layers, In1.Cu + In4.Cu
    on 6; plan call 16), which stay solid GND."""
    copper = tuple(model.copper_layers)
    planes = {4: {"In1.Cu"}, 6: {"In1.Cu", "In4.Cu"}}.get(len(copper), set())
    return tuple(n for n in copper if n not in planes)


def _order(model: BoardModel, kind: str, allowed: Sequence[str]) -> tuple[str, ...]:
    keep = set(allowed)
    return tuple(n for n in layer_order(model, kind) if n in keep)


def _pair_partner(cls: Classification, net: str) -> str | None:
    for p in cls.pairs:
        if p.p == net:
            return p.n
        if p.n == net:
            return p.p
    return None


def _unrouted(model: BoardModel, net: str) -> list[tuple[Pad, Pad]]:
    conns = [(a, b) for a, b in connections(model, net) if routed_length(model, a, b) is None]
    return sorted(conns, key=lambda ab: octi_dist((ab[0].x, ab[0].y), (ab[1].x, ab[1].y)))


def _match(
    p_conns: list[tuple[Pad, Pad]], n_conns: list[tuple[Pad, Pad]]
) -> list[tuple[int, int, bool]]:
    """Greedy pairing of the P and N connections whose ends lie close together (each end
    within PAIR_MATCH_MM); (i, j, swapped) — swapped: N's b end is next to P's a end."""

    def cost(pc: tuple[Pad, Pad], nc: tuple[Pad, Pad]) -> tuple[float, bool]:
        (pa, pb), (na, nb) = pc, nc
        d = (math.dist((pa.x, pa.y), (na.x, na.y)), math.dist((pb.x, pb.y), (nb.x, nb.y)))
        s = (math.dist((pa.x, pa.y), (nb.x, nb.y)), math.dist((pb.x, pb.y), (na.x, na.y)))
        if max(s) < max(d):
            return max(s), True
        return max(d), False

    cand = sorted(
        (c[0], i, j, c[1])
        for i, pc in enumerate(p_conns)
        for j, nc in enumerate(n_conns)
        if (c := cost(pc, nc))[0] <= PAIR_MATCH_MM
    )
    used_p: set[int] = set()
    used_n: set[int] = set()
    out: list[tuple[int, int, bool]] = []
    for _, i, j, swapped in cand:
        if i in used_p or j in used_n:
            continue
        used_p.add(i)
        used_n.add(j)
        out.append((i, j, swapped))
    return sorted(out)


def plan_connections(
    model: BoardModel,
    cls: Classification,
    nets: Sequence[str],
    *,
    layers: tuple[str, ...] | None,
    allow_vias: bool,
) -> list[ConnPlan]:
    """Per net (request order) its unrouted connections (`metrics.connections` without a
    copper path), shortest first; widths, clearance and via from the net class, layers in
    `layer_order` restricted to `layers` (default: `default_layers`). A net of a
    `cls.pairs` pair whose partner is requested too: its connections that match one of
    the partner's (ends close together) are planned once, as two adjacent plans with
    `pair` set (P first, then N), at the partner's first position in the request."""
    allowed = layers if layers is not None else default_layers(model)
    wanted = set(nets)
    out: list[ConnPlan] = []
    done: set[str] = set()

    def req(net: str, a: Pad, b: Pad, par: _Params, pair: bool) -> RouteRequest:
        kind = cls.net_kind.get(net, "signal")
        width = par.pair_width if pair and par.pair_width else par.width
        return RouteRequest(
            net, a, b, width, par.clearance, _order(model, kind, allowed), allow_vias, par.via
        )

    for net in nets:
        if net in done:
            continue
        done.add(net)
        partner = _pair_partner(cls, net)
        own = _unrouted(model, net)
        par = class_params(model, net)
        if partner is None or partner not in wanted or partner in done:
            out += [ConnPlan(net, a, b, req(net, a, b, par, False), None) for a, b in own]
            continue
        done.add(partner)
        pair = next(p for p in cls.pairs if net in (p.p, p.n))
        p_net, n_net = pair.p, pair.n
        p_conns = own if net == p_net else _unrouted(model, p_net)
        n_conns = own if net == n_net else _unrouted(model, n_net)
        p_par, n_par = class_params(model, p_net), class_params(model, n_net)
        matched = _match(p_conns, n_conns)
        mp = {i for i, _, _ in matched}
        mn = {j for _, j, _ in matched}
        for i, j, swapped in matched:
            pa, pb = p_conns[i]
            na, nb = n_conns[j]
            if swapped:
                na, nb = nb, na
            out.append(ConnPlan(p_net, pa, pb, req(p_net, pa, pb, p_par, True), n_net))
            out.append(ConnPlan(n_net, na, nb, req(n_net, na, nb, n_par, True), p_net))
        out += [
            ConnPlan(p_net, a, b, req(p_net, a, b, p_par, False), None)
            for k, (a, b) in enumerate(p_conns)
            if k not in mp
        ]
        out += [
            ConnPlan(n_net, a, b, req(n_net, a, b, n_par, False), None)
            for k, (a, b) in enumerate(n_conns)
            if k not in mn
        ]
    return out


def pair_gap(model: BoardModel, p_net: str, n_net: str) -> float:
    """The pair's gap (edge to edge): the class's diff-pair gap, else the larger clearance."""
    pp, pn = class_params(model, p_net), class_params(model, n_net)
    gaps = [g for g in (pp.pair_gap, pn.pair_gap) if g]
    return max(gaps) if gaps else max(pp.clearance, pn.clearance)


def with_footprint_keepouts(model: BoardModel, req: RouteRequest) -> BoardModel:
    """`model` plus every footprint-embedded rule area as a track + via keepout zone on all
    copper layers (module docstring), except areas holding one of the request's pads."""
    pads = ((req.a.x, req.a.y), (req.b.x, req.b.y))
    extra = tuple(
        Zone(
            kiid=f"fp-keepout:{p.ref}:{k}",
            name=f"fp:{p.ref}",
            net="",
            layers=("*.Cu",),
            outline=tuple(ra),
            priority=0,
            rule_area=True,
            keepout=frozenset({"tracks", "vias"}),
            settings=(),
            filled_area_mm2=0.0,
        )
        for p in model.parts
        for k, ra in enumerate(p.rule_areas)
        if len(ra) >= 3 and not any(point_in_polygon(c, ra) for c in pads)
    )
    return dataclasses.replace(model, zones=model.zones + extra) if extra else model


# -- KiCad writes ----------------------------------------------------------------------------


def _vec(p: Point) -> Vector2:
    return Vector2.from_xy(mm_to_nm(p[0]), mm_to_nm(p[1]))


def _kiids(ids: Sequence[str]) -> list[KIID]:
    out = []
    for value in sorted(ids):
        k = KIID()
        k.value = value
        out.append(k)
    return out


def _create(
    h: KicadHandle, items: Sequence[tuple[RouteRequest, RoutePath]], lock: bool
) -> list[str]:
    """Tracks and through vias of the paths, one batch (inside the caller's commit);
    returns the created KIIDs in input order."""
    board = h.board()
    nets = {n.name: n for n in board.get_nets()}
    made: list[Any] = []
    for req, path in items:
        net = nets.get(req.net)
        if net is None:
            raise StructuredToolError(
                "Net not on the board",
                f"KiCad has no net {req.net!r} (the board changed since it was read)",
                "Re-read the board (pcb_status) and retry.",
                code="not_found",
            )
        for layer, a, b in path.segments:
            t = KipyTrack()
            t.layer = layer_from_canonical_name(layer)
            t.net = net
            t.start, t.end = _vec(a), _vec(b)
            t.width = mm_to_nm(req.width)
            t.locked = lock
            made.append(t)
        for at in path.vias:
            v = KipyVia()
            v.position = _vec(at)
            v.net = net
            if not len(v.padstack.proto.copper_layers):  # PST_NORMAL: one entry, all layers
                v.padstack.proto.copper_layers.add().layer = BoardLayer.BL_F_Cu
            v.diameter = mm_to_nm(req.via[0])
            v.drill_diameter = mm_to_nm(req.via[1])
            v.locked = lock
            made.append(v)
    created = board.create_items(made) if made else []
    return [str(c.id.value) for c in created]


def _remove(h: KicadHandle, ids: Sequence[str]) -> None:
    if ids:
        h.board().remove_items_by_id(_kiids(ids))


def _model_items(
    items: Sequence[tuple[RouteRequest, RoutePath]], kiids: Sequence[str], lock: bool
) -> tuple[list[Track], list[Via]]:
    """The created copper as model items (KIIDs in `_create`'s order)."""
    tracks: list[Track] = []
    vias: list[Via] = []
    it = iter(kiids)
    for req, path in items:
        for layer, a, b in path.segments:
            tracks.append(Track(next(it), req.net, layer, a, b, req.width, lock))
        for at in path.vias:
            vias.append(Via(next(it), req.net, at, req.via[0], req.via[1], lock))
    return tracks, vias


# -- the per-connection loop (B-C12) -----------------------------------------------------


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


async def _solve(
    model: BoardModel, group: Sequence[ConnPlan], margin: float, remaining: float | None
) -> list[RoutePath] | None:
    timeout = SOLVE_TIMEOUT_S if remaining is None else min(SOLVE_TIMEOUT_S, remaining)
    if len(group) == 2:
        p, n = group[0].req, group[1].req
        m = with_footprint_keepouts(with_footprint_keepouts(model, p), n)
        gap = pair_gap(model, p.net, n.net)
        got = await run_cpu(
            solve_pair,
            prune_model(m, (p, n)),
            p,
            n,
            gap,
            margin=margin,
            timeout_s=timeout,
            wait_s=remaining,
        )
        return None if got is None else [got[0], got[1]]
    req = group[0].req
    m = with_footprint_keepouts(model, req)
    path = await run_cpu(
        solve, prune_model(m, req), req, margin=margin, timeout_s=timeout, wait_s=remaining
    )
    return None if path is None else [path]


async def _push(
    sctx: BoardStageContext, items: Sequence[tuple[RouteRequest, RoutePath]], lock: bool
) -> list[str]:
    async with sctx.ctx.session.commit("copperchat: pcb_route", doc="pcb"):
        made: list[str] = await sctx.call(
            lambda h: _create(h, items, lock), timeout=BOARD_READ_TIMEOUT_S
        )
    sctx.created.update(made)
    return made


async def _drop(sctx: BoardStageContext, made: Sequence[str]) -> None:
    async with sctx.ctx.session.commit("copperchat: pcb_route: reject", doc="pcb"):
        await sctx.call(lambda h: _remove(h, made), timeout=BOARD_READ_TIMEOUT_S)
    sctx.created.difference_update(made)


def _rejecting(drc: DrcResult, made: Sequence[str]) -> list[str]:
    mine = set(made)
    return [
        m.text or m.type
        for m in drc.markers
        if m.type not in IGNORED_DRC and m.severity != "exclusion" and mine.intersection(m.kiids)
    ]


@dataclass
class _Loop:
    model: BoardModel
    lock: bool
    deadline: float | None
    results: list[ConnResult] = field(default_factory=list)
    drc: DrcResult | None = None


def _failed(group: Sequence[ConnPlan], reason: str) -> list[ConnResult]:
    return [
        ConnResult(
            conn_key(p.net, p.a, p.b), p.net, _pad_name(p.a), _pad_name(p.b), "failed", reason,
            0.0, 0, (),
        )
        for p in group
    ]  # fmt: skip


async def _route_group(sctx: BoardStageContext, st: _Loop, group: Sequence[ConnPlan]) -> None:
    """One connection (or one coupled pair): margins in order, each solve → push → DRC."""
    reason = "no_path"
    for margin in MARGINS:
        remaining = _remaining(st.deadline)
        if remaining is not None and remaining <= 0:
            st.results += _failed(group, "timeout")
            return
        try:
            paths = await _solve(st.model, group, margin, remaining)
        except RouteTooLarge:
            st.results += _failed(group, "too_large")
            return
        except StructuredToolError as exc:
            if exc.code != "board_compute_timeout":
                raise
            st.results += _failed(group, "timeout")
            return
        if paths is None:
            reason = "no_path"
            continue
        items = [(p.req, path) for p, path in zip(group, paths, strict=True)]
        made = await _push(sctx, items, st.lock)
        remaining = _remaining(st.deadline)
        drc_s = DRC_TIMEOUT_S if remaining is None else max(0.0, min(DRC_TIMEOUT_S, remaining))
        try:
            drc = await sctx.drc(timeout_s=drc_s)
        except DrcTimeout:
            await _drop(sctx, made)  # unverified: never kept
            st.results += _failed(group, "timeout")
            return
        st.drc = drc
        bad = _rejecting(drc, made)
        if bad:
            await _drop(sctx, made)
            reason = f"drc_rejected: {bad[0]}"
            continue
        tracks, vias = _model_items(items, made, st.lock)
        st.model = dataclasses.replace(
            st.model, tracks=st.model.tracks + tuple(tracks), vias=st.model.vias + tuple(vias)
        )
        coupling = pair_coupling(paths[0], paths[1]) if len(paths) == 2 else None
        it = iter(made)
        for p, path in zip(group, paths, strict=True):
            n = len(path.segments) + len(path.vias)
            own = tuple(next(it) for _ in range(n))
            st.results.append(
                ConnResult(
                    conn_key(p.net, p.a, p.b), p.net, _pad_name(p.a), _pad_name(p.b), "routed",
                    None, path.length, len(path.vias), own, coupling,
                )
            )  # fmt: skip
        return
    st.results += _failed(group, reason)


def _groups(plans: Sequence[ConnPlan]) -> list[list[ConnPlan]]:
    out: list[list[ConnPlan]] = []
    i = 0
    while i < len(plans):
        p = plans[i]
        nxt = plans[i + 1] if i + 1 < len(plans) else None
        if p.pair is not None and nxt is not None and nxt.net == p.pair and nxt.pair == p.net:
            out.append([p, nxt])
            i += 2
        else:
            out.append([p])
            i += 1
    return out


async def route_connections(
    sctx: BoardStageContext,
    plans: Sequence[ConnPlan],
    *,
    lock: bool,
    deadline: float | None = None,
) -> RouteOutcome:
    """Route `plans` in order (B-C12): the model is read once, accepted copper is added to
    it in memory, so later connections see it. `deadline` (a `time.monotonic()` value):
    a connection not started before it is failed `timeout` and never solved; each
    `run_cpu` gets `timeout_s=min(30, remaining)` and `wait_s=remaining`, each DRC
    `min(60, remaining)`. A `DrcTimeout` fails only its connection (its items removed).
    Never relies on DRC seeing an open commit (P1a probe): the DRC runs after the push."""
    st = _Loop(await sctx.model(), lock, deadline)
    for group in _groups(plans):
        await _route_group(sctx, st, group)
    return RouteOutcome(
        routed=tuple(r.key for r in st.results if r.status == "routed"),
        failed=tuple((r.key, r.reason or "") for r in st.results if r.status == "failed"),
        results=tuple(st.results),
        drc=st.drc,
    )


# -- argument parsing ------------------------------------------------------------------------


def _invalid(why: str, suggestion: str = "See the tool schema.") -> StructuredToolError:
    return StructuredToolError("Invalid argument", why, suggestion, code="invalid_argument")


def _bool(args: Mapping[str, Any], key: str, default: bool) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise _invalid(f"{key} must be true or false")
    return value


def _names(value: Any, key: str, lo: int, hi: int, chars: int) -> list[str]:
    if not isinstance(value, list) or not lo <= len(value) <= hi:
        raise _invalid(f"{key} must be a list of {lo}-{hi} names")
    out: list[str] = []
    for v in value:
        if not isinstance(v, str) or not 1 <= len(v) <= chars:
            raise _invalid(f"every {key} entry must be a string of 1-{chars} characters")
        if v not in out:
            out.append(v)
    return out


def _keys(args: Any, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(args, dict):
        raise _invalid("the arguments must be an object")
    extra = sorted(set(args) - allowed)
    if extra:
        raise _invalid(f"unknown arguments: {', '.join(map(str, extra[:5]))}")
    return args


def _closest(name: str, pool: Sequence[str]) -> list[str]:
    return difflib.get_close_matches(name, sorted(set(pool)), n=5, cutoff=0.4)


def _board_nets(model: BoardModel) -> set[str]:
    return {q.net for p in model.parts for q in p.pads if q.net}


def _check_nets(model: BoardModel, nets: Sequence[str]) -> None:
    known = _board_nets(model) | {t.net for t in model.tracks} | {v.net for v in model.vias}
    unknown = [n for n in nets if n not in known]
    if unknown:
        closest = {n: _closest(n, list(known)) for n in unknown[:20]}
        first = closest[unknown[0]]
        raise StructuredToolError(
            f"Net {unknown[0]!r} not found"
            if len(unknown) == 1
            else f"{len(unknown)} nets not found",
            f"the board has no net {', '.join(unknown[:20])}",
            f"Closest: {', '.join(first)}" if first else "Check the net names with pcb_analyze.",
            code="not_found",
            details={"nets": unknown[:20], "closest": closest},
        )


def _check_layers(model: BoardModel, layers: Sequence[str]) -> None:
    copper = list(model.copper_layers)
    bad = [n for n in layers if n not in copper]
    if bad:
        raise StructuredToolError(
            f"Layer {bad[0]!r} not found",
            f"{', '.join(bad[:10])} is not an enabled copper layer (enabled: {', '.join(copper)})",
            "Pass enabled copper layers, or change the layer count with pcb_set_rules.",
            code="not_found",
            details={"layers": bad[:10], "closest": {n: _closest(n, copper) for n in bad[:10]}},
        )


def _parse_region(value: Any) -> Rect:
    r = _keys(value, {"x", "y", "w", "h"})
    vals: dict[str, float] = {}
    for k in ("x", "y", "w", "h"):
        v = r.get(k)
        ok = isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(float(v))
        if not ok:
            raise _invalid("region needs finite numbers x, y, w, h")
        vals[k] = float(v)  # type: ignore[arg-type]
    if not (abs(vals["x"]) <= MAX_COORD_MM and abs(vals["y"]) <= MAX_COORD_MM):
        raise _invalid(f"region x/y must be within ±{MAX_COORD_MM:g} mm")
    if not (0 < vals["w"] <= MAX_REGION_MM and 0 < vals["h"] <= MAX_REGION_MM):
        raise _invalid(f"region w/h must be > 0 and <= {MAX_REGION_MM:g} mm")
    return Rect(vals["x"], vals["y"], vals["w"], vals["h"])


# -- pcb_route -------------------------------------------------------------------------------


@dataclass
class _RouteState:
    deadline: float | None = None
    accepted: dict[str, bool] = field(default_factory=dict)  # kiid -> locked
    drc: DrcResult | None = None


@dataclass(kw_only=True)
class RouteChangeSet(BoardChangeSet):
    """A `pcb_route` write: the loop's state (accepted items, last DRC) and the read-back
    model for the after-report."""

    state: _RouteState = field(default_factory=_RouteState)
    read_back: list[BoardModel] = field(default_factory=list)


def _net_entry(net: str, results: Sequence[ConnResult], lock: bool) -> dict[str, Any]:
    routed = [r for r in results if r.status == "routed"]
    failed = [r for r in results if r.status == "failed"]
    status = "routed" if not failed else ("partial" if routed else "failed")
    entry: dict[str, Any] = {
        "net": net,
        "status": status,
        "connections": len(results),
        "routed": len(routed),
        "failed": [{"a": r.a, "b": r.b, "reason": r.reason} for r in failed],
        "length_mm": round(sum(r.length_mm for r in routed), 3),
        "vias": sum(r.vias for r in routed),
        "locked": lock,
    }
    coupling = [r.coupling for r in routed if r.coupling is not None]
    if coupling:
        entry["coupling"] = {
            "length_mismatch": round(max(c["length_mismatch"] for c in coupling), 4),
            "gap_min": round(min(c["gap_min"] for c in coupling), 4),
            "gap_max": round(max(c["gap_max"] for c in coupling), 4),
        }
    return entry


def _bends(model: BoardModel, kiids: frozenset[str]) -> dict[str, int]:
    """M4 / M7 counts over the call's new copper (B-C5 `analyse_tracks`, pads and vias
    of the whole board)."""
    sub = dataclasses.replace(
        model, tracks=tuple(t for t in model.tracks if t.kiid in kiids), arcs=()
    )
    rep = analyse_tracks(sub, index=TrackIndex(model))
    m4 = (
        len(rep.non_octilinear)
        + len(rep.ninety)
        + len(rep.acute)
        + len(rep.off_angle)
        + len(rep.t_junctions)
    )
    return {"M4": m4, "M7": len(rep.micro_jogs) + len(rep.short_chamfers), "bends": rep.bends}


async def prepare_route(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_route` (module docstring): placement checkpoint first (spec §8.3), then
    nets (unknown → `not_found` + closest, ground → `invalid_argument`) and layers
    (unknown / disabled → `not_found`); one stage per net with connections to route."""
    require_board_tools(ctx)
    a = _keys(args, {"nets", "layers", "width_from", "lock", "allow_vias"})
    nets = _names(a.get("nets"), "nets", 1, MAX_NETS, MAX_NET_CHARS)
    layers = (
        _names(a["layers"], "layers", 1, MAX_LAYERS, 64) if a.get("layers") is not None else None
    )
    if a.get("width_from", "netclass") != "netclass":
        raise _invalid("width_from must be 'netclass'")
    lock = _bool(a, "lock", True)
    allow_vias = _bool(a, "allow_vias", True)
    require_board(ctx)
    model = await board_model(ctx)
    await require_placement_approved(ctx, model)
    _check_nets(model, nets)
    if layers is not None:
        _check_layers(model, layers)
    doc = await read_board_doc(ctx)
    cls = await asyncio.to_thread(classify, model, doc)
    ground = [n for n in nets if cls.net_kind.get(n) == "ground"]
    if ground:
        raise _invalid(
            f"{', '.join(ground[:10])} {'is a ground net' if len(ground) == 1 else 'are ground nets'}",  # noqa: E501
            "Ground connects through planes and pcb_fanout (spec §10.1); route the other nets.",
        )
    plans = await asyncio.to_thread(
        functools.partial(
            plan_connections,
            model,
            cls,
            nets,
            layers=tuple(layers) if layers is not None else None,
            allow_vias=allow_vias,
        )
    )
    cost = math.ceil(len(nets) / NETS_PER_COST)
    state = _RouteState()
    by_net: dict[str, list[ConnPlan]] = {n: [] for n in nets}
    owner: dict[str, str] = {}  # pair partner -> the net whose stage routes the pair
    for p in plans:
        stage_net = owner.get(p.net, p.net) if p.pair is not None else p.net
        if p.pair is not None and p.pair in by_net and p.net not in owner:
            owner[p.pair] = p.net
        by_net[stage_net].append(p)
    results: dict[str, list[ConnResult]] = {n: [] for n in nets}
    report: dict[str, Any] = {
        "nets": [],
        "logical_changes": cost if plans else 0,
        "budget": None,  # filled by the adapter
    }
    already = [n for n in nets if not any(p.net == n for p in plans)]

    def stage_for(net: str, mine: list[ConnPlan]) -> BoardStage:
        async def run(sctx: BoardStageContext) -> None:
            if state.deadline is None:
                state.deadline = time.monotonic() + ROUTE_DEADLINE_S
            out = await route_connections(sctx, mine, lock=lock, deadline=state.deadline)
            for r in out.results:
                results[r.net].append(r)
                for k in r.kiids:
                    state.accepted[k] = lock
            if out.drc is not None:
                state.drc = out.drc

        return BoardStage(f"route {net}", run, commit=False)

    stages = [stage_for(n, by_net[n]) for n in nets if by_net[n]]

    def finish(m: BoardModel, rep: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        items = {t.kiid: t.locked for t in m.tracks} | {v.kiid: v.locked for v in m.vias}
        for k, locked in state.accepted.items():
            if k not in items:
                problems.append(f"routed item {k} is missing")
            elif items[k] != locked:
                problems.append(f"routed item {k} has locked={items[k]}")
        cs.read_back[:] = [m]
        entries = []
        for n in nets:
            if n in already:
                entries.append({"net": n, "status": "already_routed", "connections": 0})
            else:
                entries.append(_net_entry(n, results[n], lock))
        rep["nets"] = entries
        return problems

    cs = RouteChangeSet(
        tool="pcb_route",
        logical=Logical(cost if plans else 0),
        kind="board_route",
        board_stages=stages,
        verify=finish,
        report=report,
        state=state,
    )
    if not plans:
        report["nets"] = [{"net": n, "status": "already_routed", "connections": 0} for n in nets]
        report.update(_totals(report["nets"]))
        report["next"] = "pcb_route_fill for the remaining connections"
    return cs


def _totals(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "totals": {
            "nets": len(entries),
            "connections": sum(e.get("connections", 0) for e in entries),
            "routed": sum(e.get("routed", 0) for e in entries),
            "failed": sum(len(e.get("failed", ())) for e in entries),
            "length_mm": round(sum(e.get("length_mm", 0.0) for e in entries), 3),
            "vias": sum(e.get("vias", 0) for e in entries),
        }
    }


async def apply_route(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    """Apply a `pcb_route` change set; the report gets totals, M4/M7 over the new copper
    and the DRC errors of the last check (other than unconnected items)."""
    assert isinstance(cs, RouteChangeSet)
    if not cs.board_stages:
        return ToolResult(data=dict(cs.report))  # nothing to route: nothing written, cost 0
    result = await apply_board_change_set(ctx, cs)
    state = cs.state
    data = result.data if isinstance(result.data, dict) else {}
    data.update(_totals(data.get("nets", ())))
    if cs.read_back:
        data["bends"] = await asyncio.to_thread(_bends, cs.read_back[0], frozenset(state.accepted))
    drc = state.drc
    data["drc_errors"] = (
        None
        if drc is None
        else sum(1 for m in drc.markers if m.severity == "error" and m.type not in IGNORED_DRC)
    )
    data["next"] = NEXT_ROUTE
    return result


# -- pcb_unroute -----------------------------------------------------------------------------


def _set_unlocked(h: KicadHandle, ids: Sequence[str]) -> None:
    board = h.board()
    items = list(board.get_items_by_id(_kiids(ids)))
    if len(items) != len(ids):
        raise StructuredToolError(
            "Copper changed",
            f"{len(ids) - len(items)} of the selected items are no longer on the board",
            "Re-read the board (pcb_status) and retry.",
            code="not_found",
        )
    for it in items:
        it.locked = False  # type: ignore[attr-defined]
    board.update_items(items)


def unlock_stage(kiids: Sequence[str]) -> BoardStage:
    """One board commit clearing `locked` on the given tracks / vias (spec §10.3 ladder
    step (b))."""
    ids = sorted(kiids)

    async def run(sctx: BoardStageContext) -> None:
        await sctx.call(lambda h: _set_unlocked(h, ids), timeout=BOARD_READ_TIMEOUT_S)

    return BoardStage("unlock", run)


def plane_nets(model: BoardModel, cls: Classification) -> set[str]:
    """Ground nets (P3a) plus every net carrying a copper zone."""
    out = {n for n, k in cls.net_kind.items() if k == "ground"}
    out |= {z.net for z in model.zones if not z.rule_area and z.net}
    return out


def _parse_unroute(args: Any) -> tuple[list[str], Rect | None, bool, bool]:
    a = _keys(args, {"nets", "region", "include_locked", "unlock_only"})
    nets = (
        _names(a["nets"], "nets", 1, MAX_NETS, MAX_NET_CHARS) if a.get("nets") is not None else []
    )
    region = _parse_region(a["region"]) if a.get("region") is not None else None
    if not nets and region is None:
        raise _invalid(
            "pcb_unroute needs nets or a region", "Pass nets: [...] or region: {x, y, w, h}."
        )
    return nets, region, _bool(a, "include_locked", False), _bool(a, "unlock_only", False)


async def prepare_unroute(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_unroute`: delete the selected connections' copper (default; locked
    components skipped unless `include_locked`), or with `unlock_only` clear the `locked`
    flag of the selected locked items, plane-net items only when their net is named."""
    require_board_tools(ctx)
    nets, region, include_locked, unlock_only = _parse_unroute(args)
    require_board(ctx)
    model = await board_model(ctx)
    if nets:
        _check_nets(model, nets)
    select = functools.partial(select_copper, model, nets=nets, region=region)
    if unlock_only:
        doc = await read_board_doc(ctx)
        cls = await asyncio.to_thread(classify, model, doc)
        planes = plane_nets(model, cls)
        sel: CopperSelection = await asyncio.to_thread(select, include_locked=True)
        named = set(nets)
        locked = [t for t in model.tracks + model.arcs if t.kiid in sel.track_kiids and t.locked]
        locked_v = [v for v in model.vias if v.kiid in sel.via_kiids and v.locked]
        chosen = [it for it in (*locked, *locked_v) if it.net not in planes or it.net in named]
        skipped = len(locked) + len(locked_v) - len(chosen)
        ids = [it.kiid for it in chosen]
        unlocked_nets = sorted({it.net for it in chosen}, key=natural_key)
        report: dict[str, Any] = {
            "unlocked": len(ids),
            "unlocked_nets": unlocked_nets,
            "skipped_plane_items": skipped,
            "logical_changes": 1 if ids else 0,
            "budget": None,
            "next": "pcb_route_fill with relock_nets = unlocked_nets",
        }
        before = {t.kiid for t in model.tracks + model.arcs} | {v.kiid for v in model.vias}
        want = set(ids)

        def verify_unlock(m: BoardModel, rep: dict[str, Any]) -> list[str]:
            now = {t.kiid: t.locked for t in m.tracks + m.arcs} | {v.kiid: v.locked for v in m.vias}
            problems = [f"item {k} still locked" for k in sorted(want) if now.get(k) is not False]
            if set(now) != before:
                problems.append("the set of tracks/vias changed")
            return problems

        return BoardChangeSet(
            tool="pcb_unroute",
            logical=Logical(1 if ids else 0),
            kind="board_unroute",
            board_stages=[unlock_stage(ids)] if ids else [],
            verify=verify_unlock if ids else None,
            report=report,
        )
    sel = await asyncio.to_thread(select, include_locked=include_locked)
    skipped_nets: list[str] = []
    if sel.locked:
        every = await asyncio.to_thread(select, include_locked=True)
        skipped_nets = sorted(set(every.nets) - set(sel.nets), key=natural_key)
    gone = sel.track_kiids | sel.via_kiids
    report = {
        "removed_tracks": len(sel.track_kiids),
        "removed_vias": len(sel.via_kiids),
        "nets": list(sel.nets),
        "skipped_locked": sel.locked,
        "skipped_locked_nets": skipped_nets,
        "logical_changes": 0 if sel.empty else 1,
        "budget": None,
        "next": "pcb_route for the nets to route again",
    }

    def verify_delete(m: BoardModel, rep: dict[str, Any]) -> list[str]:
        now = {t.kiid for t in m.tracks + m.arcs} | {v.kiid for v in m.vias}
        left = sorted(gone & now)
        return [f"{len(left)} selected items are still on the board"] if left else []

    return BoardChangeSet(
        tool="pcb_unroute",
        logical=Logical(0 if sel.empty else 1),
        kind="board_unroute",
        board_stages=[] if sel.empty else [unroute_stage(sel)],
        verify=None if sel.empty else verify_delete,
        snapshot="full" if include_locked else "ids",
        unrouted_by_call=list(sel.nets),
        report=report,
    )


async def apply_unroute(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    assert isinstance(cs, BoardChangeSet)
    if not cs.board_stages:
        return ToolResult(data=dict(cs.report))  # nothing selected: nothing written, cost 0
    return await apply_board_change_set(ctx, cs)
