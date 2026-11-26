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
from fnmatch import fnmatchcase
from typing import Any

from kipy.board_types import Track as KipyTrack
from kipy.board_types import Via as KipyVia
from kipy.geometry import Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common.types import KIID
from kipy.util.board_layer import layer_from_canonical_name

from copperchat.board.boardmd import read_board_doc
from copperchat.board.classify import Classification, classify
from copperchat.board.geom import (
    TrackIndex,
    _in_pad,
    _pad_on_layer,
    analyse_tracks,
    octi_dist,
    point_in_polygon,
)
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
RIP_MAX = 6  # nearby connections a blocked one may take out, one at a time
RIP_HALO_MM = 2.0  # how far around a blocked connection's pads a blocker may lie
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
    seconds: float = 0.0  # solve time spent on it


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
    """The net's class(es). KiCad names a pattern-assigned net's effective class by its
    constituents ("Default,Power") or as "Effective for net: <net>" (P2 residual): known
    constituent names are used; else the classes whose patterns match the net (KiCad
    wildcards, case-sensitive) plus Default; none → Default."""
    by_name = {c.name: c for c in model.net_classes}
    name = dict(model.net_class_of).get(net, "Default")
    found = [by_name[n.strip()] for n in name.split(",") if n.strip() in by_name]
    if found:
        return found
    found = [c for c in model.net_classes if any(fnmatchcase(net, p) for p in c.patterns)]
    if "Default" in by_name:
        found.append(by_name["Default"])
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


def _joined(a: Pad, b: Pad) -> bool:
    """Pads of one net that overlap on a shared copper layer (e.g. USB-C's stacked VBUS
    pads A9/B4) are connected without copper: one centre lies inside the other pad."""
    names = {n for n in a.layers | b.layers if n.endswith(".Cu")} | {"F.Cu", "B.Cu"}
    shared = any(_pad_on_layer(a, n) and _pad_on_layer(b, n) for n in names)
    return shared and (_in_pad((a.x, a.y), b) or _in_pad((b.x, b.y), a))


def _unrouted(model: BoardModel, net: str) -> list[tuple[Pad, Pad]]:
    conns = [
        (a, b)
        for a, b in connections(model, net)
        if not _joined(a, b) and routed_length(model, a, b) is None
    ]
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


KIND_PRIORITY = {"diff_pair": 0, "crystal": 1, "switch_node": 2, "supply": 3, "high_current": 3}
OTHER_PRIORITY = 4  # signal nets


def routing_order(model: BoardModel, cls: Classification, nets: Sequence[str]) -> list[str]:
    """The order `pcb_route` routes the requested nets in (the report keeps the request
    order): the most constrained first — differential pairs, crystal, switch node, then
    supply (decoupling paths short, M8), then signals; within a kind the net with the
    shortest unrouted connection first (P5a: short connections before long ones)."""

    def key(item: tuple[int, str]) -> tuple[int, float, int]:
        i, net = item
        conns = _unrouted(model, net)
        shortest = min((octi_dist((a.x, a.y), (b.x, b.y)) for a, b in conns), default=math.inf)
        return (KIND_PRIORITY.get(cls.net_kind.get(net, "signal"), OTHER_PRIORITY), shortest, i)

    return [net for _, net in sorted(enumerate(dict.fromkeys(nets)), key=key)]


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


HOLE_TO_HOLE_DEFAULT_MM = 0.25  # KiCad's default when the board has no rule


def _octagon(c: Point, apothem: float) -> tuple[Point, ...]:
    """A regular octagon around `c` that contains the circle of radius `apothem`."""
    r = apothem / math.cos(math.pi / 8)
    return tuple(
        (c[0] + r * math.cos(math.pi / 8 + k * math.pi / 4),
         c[1] + r * math.sin(math.pi / 8 + k * math.pi / 4))
        for k in range(8)
    )  # fmt: skip


def with_hole_spacing(model: BoardModel, req: RouteRequest) -> BoardModel:
    """`model` plus a via keepout around every existing via, so a new via keeps the
    board's hole-to-hole rule from it (P5a residual: the router checks copper clearance
    only; an own-net via is no obstacle at all, and KiCad's DRC rejected new vias placed
    on top of one). Pads with holes need no extra keepout: their copper keeps a via
    farther away than the hole rule."""
    if not req.allow_vias or not model.vias:
        return model
    h2h = dict(model.rules).get("hole_to_hole_min", HOLE_TO_HOLE_DEFAULT_MM) or 0.0
    extra = []
    for v in model.vias:
        # the new via's centre keeps via_r from the octagon (build_grid's via keepout)
        apothem = v.drill / 2 + req.via[1] / 2 + h2h - req.via[0] / 2
        if apothem <= 0:
            continue  # copper clearance already keeps the holes apart
        extra.append(
            Zone(
                kiid=f"hole-spacing:{v.kiid}",
                name="hole spacing",
                net="",
                layers=("*.Cu",),
                outline=_octagon(v.at, apothem),
                priority=0,
                rule_area=True,
                keepout=frozenset({"vias"}),
                settings=(),
                filled_area_mm2=0.0,
            )
        )
    return dataclasses.replace(model, zones=model.zones + tuple(extra)) if extra else model


def router_model(model: BoardModel, req: RouteRequest) -> BoardModel:
    """What the router sees for `req`: footprint keepouts and hole spacing added."""
    return with_hole_spacing(with_footprint_keepouts(model, req), req)


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
        m = with_footprint_keepouts(router_model(model, p), n)
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
    m = router_model(model, req)
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
class _Kept:
    """Copper this loop accepted: one connection or one coupled pair."""

    group: list[ConnPlan]
    paths: list[RoutePath]
    kiids: list[str]  # every item of the group, in creation order


@dataclass
class _Loop:
    model: BoardModel
    lock: bool
    deadline: float | None
    results: dict[str, ConnResult] = field(default_factory=dict)  # key -> result
    kept: list[_Kept] = field(default_factory=list)
    drc: DrcResult | None = None
    started: dict[str, float] = field(default_factory=dict)  # key -> seconds spent


def _key(p: ConnPlan) -> str:
    return conn_key(p.net, p.a, p.b)


def _fail(st: _Loop, group: Sequence[ConnPlan], reason: str) -> None:
    for p in group:
        st.results[_key(p)] = ConnResult(
            _key(p), p.net, _pad_name(p.a), _pad_name(p.b), "failed", reason, 0.0, 0, (),
            seconds=round(st.started.get(_key(p), 0.0), 2),
        )  # fmt: skip


def _record(st: _Loop, kept: _Kept) -> None:
    coupling = pair_coupling(kept.paths[0], kept.paths[1]) if len(kept.paths) == 2 else None
    it = iter(kept.kiids)
    for p, path in zip(kept.group, kept.paths, strict=True):
        own = tuple(next(it) for _ in range(len(path.segments) + len(path.vias)))
        st.results[_key(p)] = ConnResult(
            _key(p), p.net, _pad_name(p.a), _pad_name(p.b), "routed", None, path.length,
            len(path.vias), own, coupling, round(st.started.get(_key(p), 0.0), 2),
        )  # fmt: skip


def _timed_out(st: _Loop) -> bool:
    remaining = _remaining(st.deadline)
    return remaining is not None and remaining <= 0


async def _place(
    sctx: BoardStageContext, st: _Loop, group: Sequence[ConnPlan], paths: list[RoutePath]
) -> str | None:
    """Push `paths`, run the DRC task, keep them when no marker names their items.
    None = kept (model, `kept` and results updated); else the failure reason
    ("timeout" for a DRC timeout, else "drc_rejected: …"); rejected copper is removed."""
    items = [(p.req, path) for p, path in zip(group, paths, strict=True)]
    made = await _push(sctx, items, st.lock)
    remaining = _remaining(st.deadline)
    drc_s = DRC_TIMEOUT_S if remaining is None else max(0.0, min(DRC_TIMEOUT_S, remaining))
    try:
        drc = await sctx.drc(timeout_s=drc_s)
    except DrcTimeout:
        await _drop(sctx, made)  # unverified: never kept
        return "timeout"
    st.drc = drc
    bad = _rejecting(drc, made)
    if bad:
        await _drop(sctx, made)
        return f"drc_rejected: {bad[0]}"
    tracks, vias = _model_items(items, made, st.lock)
    st.model = dataclasses.replace(
        st.model, tracks=st.model.tracks + tuple(tracks), vias=st.model.vias + tuple(vias)
    )
    kept = _Kept(list(group), list(paths), list(made))
    st.kept.append(kept)
    _record(st, kept)
    return None


async def _attempt(sctx: BoardStageContext, st: _Loop, group: Sequence[ConnPlan]) -> str | None:
    """Margins in order, each solve → push → DRC (the deadline checked before every
    margin). None = routed; else the failure reason (not recorded)."""
    reason = "no_path"
    for margin in MARGINS:
        remaining = _remaining(st.deadline)
        if remaining is not None and remaining <= 0:
            return "timeout"
        t0 = time.monotonic()
        try:
            paths = await _solve(st.model, group, margin, remaining)
        except RouteTooLarge:
            return "too_large"
        except StructuredToolError as exc:
            if exc.code != "board_compute_timeout":
                raise
            return "timeout"
        finally:
            for p in group:
                st.started[_key(p)] = st.started.get(_key(p), 0.0) + time.monotonic() - t0
        if paths is None:
            continue  # a DRC rejection of an earlier margin stays the reason
        got = await _place(sctx, st, group, paths)
        if got is None:
            return None
        if got == "timeout":
            return got
        reason = got
    return reason


async def _unkeep(sctx: BoardStageContext, st: _Loop, kept: _Kept) -> None:
    """Remove a kept group's copper again (one commit) and forget it."""
    await _drop(sctx, kept.kiids)
    gone = set(kept.kiids)
    st.model = dataclasses.replace(
        st.model,
        tracks=tuple(t for t in st.model.tracks if t.kiid not in gone),
        vias=tuple(v for v in st.model.vias if v.kiid not in gone),
    )
    st.kept.remove(kept)
    for p in kept.group:
        st.results.pop(_key(p), None)


def _blockers(st: _Loop, group: Sequence[ConnPlan]) -> list[_Kept]:
    """Kept groups of other nets whose copper passes within RIP_HALO_MM of the box around
    the group's pads, nearest first (at most RIP_MAX)."""
    nets = {p.net for p in group}
    pts = [(q.x, q.y) for p in group for q in (p.a, p.b)]
    x0 = min(x for x, _ in pts) - RIP_HALO_MM
    y0 = min(y for _, y in pts) - RIP_HALO_MM
    x1 = max(x for x, _ in pts) + RIP_HALO_MM
    y1 = max(y for _, y in pts) + RIP_HALO_MM

    def inside(q: Point) -> bool:
        return x0 <= q[0] <= x1 and y0 <= q[1] <= y1

    def dist(k: _Kept) -> float:
        best = math.inf
        for path in k.paths:
            for _, a, b in path.segments:
                for q in (a, b, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)):
                    best = min(best, min(math.dist(q, c) for c in pts))
        return best

    near = [
        k
        for k in st.kept
        if not nets & {p.net for p in k.group}
        and any(
            inside(q)
            for path in k.paths
            for _, a, b in path.segments
            for q in (a, b, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        )
    ]
    return sorted(near, key=dist)[:RIP_MAX]


async def _rip_and_retry(sctx: BoardStageContext, st: _Loop, group: Sequence[ConnPlan]) -> bool:
    """A blocked connection: take one nearby connection of this loop out, route the blocked
    one, then the removed one again; when that fails, the blocked one's copper goes and the
    removed one is put back exactly (same path, DRC-checked). True = both routed."""
    for cand in _blockers(st, group):
        if _timed_out(st):
            return False
        old_group, old_paths = cand.group, cand.paths
        await _unkeep(sctx, st, cand)
        if await _attempt(sctx, st, group) is None:
            if await _attempt(sctx, st, old_group) is None:
                return True
            mine = next(k for k in st.kept if k.group == list(group))
            await _unkeep(sctx, st, mine)
        if await _place(sctx, st, old_group, old_paths) is not None:
            # the old path is not accepted again (never seen; the board state is the same
            # as when it was accepted): route it anew, else report it failed
            reason = await _attempt(sctx, st, old_group)
            if reason is not None:
                _fail(st, old_group, reason)
    return False


async def _route_group(sctx: BoardStageContext, st: _Loop, group: Sequence[ConnPlan]) -> None:
    """One connection or one coupled pair: the margins; a pair that fails coupled is
    routed net by net; a blocked connection gets the rip-up retry."""
    reason = await _attempt(sctx, st, group)
    if reason is None:
        return
    if reason in ("timeout", "too_large"):
        _fail(st, group, reason)
        return
    if len(group) == 2:  # spec §10.2: when coupling fails, route the nets alone
        for p in group:
            await _route_group(sctx, st, [p])
        return
    if await _rip_and_retry(sctx, st, group):
        return
    _fail(st, group, reason if not _timed_out(st) else "timeout")


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
    A coupled pair that cannot be routed is routed net by net; a connection that stays
    blocked may take out one nearby connection of this call at a time (at most RIP_MAX
    tries), route first and re-route the other — never copper the call did not create.
    Never relies on DRC seeing an open commit (P1a probe): the DRC runs after the push."""
    st = _Loop(await sctx.model(), lock, deadline)
    groups = _groups(plans)
    for group in groups:
        await _route_group(sctx, st, group)
    # one more round for what stayed blocked: the board around it changed since
    for group in groups:
        if _timed_out(st):
            break
        failed = [
            p
            for p in group
            if st.results[_key(p)].status == "failed"
            and st.results[_key(p)].reason not in ("timeout", "too_large")
        ]
        if not failed:
            continue
        reason = st.results[_key(failed[0])].reason or "no_path"
        if not await _rip_and_retry(sctx, st, failed):
            _fail(st, failed, reason if not _timed_out(st) else "timeout")
    order = {_key(p): i for i, p in enumerate(plans)}
    results = sorted(st.results.values(), key=lambda r: order.get(r.key, len(order)))
    return RouteOutcome(
        routed=tuple(r.key for r in results if r.status == "routed"),
        failed=tuple((r.key, r.reason or "") for r in results if r.status == "failed"),
        results=tuple(results),
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
        "seconds": round(sum(r.seconds for r in results), 2),
    }
    coupling = [r.coupling for r in routed if r.coupling is not None]
    if coupling:
        entry["coupling"] = {
            "length_mismatch": round(max(c["length_mismatch"] for c in coupling), 4),
            "gap_min": round(min(c["gap_min"] for c in coupling), 4),
            "gap_max": round(max(c["gap_max"] for c in coupling), 4),
        }
    return entry


PAIR_MISMATCH_MM = 1.0  # M12's pair length tolerance


def _pairs_report(
    cls: Classification, nets: Sequence[str], results: Mapping[str, Sequence[ConnResult]]
) -> list[dict[str, Any]]:
    """Per differential pair with both nets requested: how many of its connections were
    routed coupled / alone, the worst length mismatch and the gap range of the coupled
    ones, and a warning for the agent when a connection is uncoupled or the mismatch
    exceeds PAIR_MISMATCH_MM."""
    out: list[dict[str, Any]] = []
    for pair in cls.pairs:
        if pair.p not in nets or pair.n not in nets:
            continue
        routed = [r for n in (pair.p, pair.n) for r in results.get(n, ()) if r.status == "routed"]
        coupled = [r.coupling for r in routed if r.coupling is not None and r.net == pair.p]
        alone = sum(1 for r in routed if r.coupling is None)
        entry: dict[str, Any] = {
            "p": pair.p,
            "n": pair.n,
            "coupled_connections": len(coupled),
            "uncoupled_connections": alone,
        }
        if coupled:
            entry["length_mismatch"] = round(max(c["length_mismatch"] for c in coupled), 4)
            entry["gap_min"] = round(min(c["gap_min"] for c in coupled), 4)
            entry["gap_max"] = round(max(c["gap_max"] for c in coupled), 4)
        notes = []
        if alone:
            notes.append(f"{alone} connection(s) routed uncoupled")
        if entry.get("length_mismatch", 0.0) > PAIR_MISMATCH_MM:
            notes.append(f"length mismatch {entry['length_mismatch']:g} mm > {PAIR_MISMATCH_MM:g}")
        if notes:
            entry["warning"] = "; ".join(notes)
        out.append(entry)
    return out


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
    route_layers = tuple(layers) if layers is not None else None

    def plan() -> list[ConnPlan]:
        order = routing_order(model, cls, nets)
        return plan_connections(model, cls, order, layers=route_layers, allow_vias=allow_vias)

    plans = await asyncio.to_thread(plan)
    cost = math.ceil(len(nets) / NETS_PER_COST)
    state = _RouteState()
    results: dict[str, list[ConnResult]] = {n: [] for n in nets}
    report: dict[str, Any] = {
        "nets": [],
        "logical_changes": cost if plans else 0,
        "budget": None,  # filled by the adapter
    }
    already = [n for n in nets if not any(p.net == n for p in plans)]

    async def run(sctx: BoardStageContext) -> None:
        state.deadline = time.monotonic() + ROUTE_DEADLINE_S
        out = await route_connections(sctx, plans, lock=lock, deadline=state.deadline)
        for r in out.results:
            results[r.net].append(r)
            for k in r.kiids:
                state.accepted[k] = lock
        state.drc = out.drc

    # One stage (it pushes its own commits): a blocked connection may take out and re-route
    # a connection of another net routed earlier in the call (`route_connections`).
    stages = [BoardStage("route", run, commit=False)] if plans else []

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
        rep["pairs"] = _pairs_report(cls, nets, results)
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
        report["pairs"] = []
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
        return ToolResult(data=dict(cs.report) | {"seconds": 0.0})  # nothing written, cost 0
    t0 = time.monotonic()
    result = await apply_board_change_set(ctx, cs)
    state = cs.state
    data = result.data if isinstance(result.data, dict) else {}
    data.update(_totals(data.get("nets", ())))
    data["seconds"] = round(time.monotonic() - t0, 2)
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
