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
    escape_zone,
    neck_width,
    octi_dist,
    point_in_polygon,
    straight_corridor,
    straight_link_corridor,
)
from copperchat.board.metrics import connections, routed_length
from copperchat.board.model import BoardModel, NetClassInfo, Pad, Point, Rect, Track, Via, Zone
from copperchat.board.presets import PRESETS
from copperchat.board.router import (
    RoutePath,
    RouteRequest,
    RouteTooLarge,
    prune_model,
    solve,
    solve_pair,
)
from copperchat.board.router.astar import _entry_bad, tree_end
from copperchat.board.router.grid import _pad_shape, seg_seg, seg_shape
from copperchat.board.router.pairs import pair_coupling
from copperchat.board.router.simplify import layer_runs, merge_collinear, neck_pieces
from copperchat.board.staging import natural_key
from copperchat.board.worker import run_cpu
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board.analysis import preset_for
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
    early: bool = False  # R3: an M8-measured link (decap / crystal), routed before pass 0


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
    pad_entry: bool = False  # R3: a pad-entry loop / jog (M16 b / c) the router had to accept


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
    within PAIR_MATCH_MM), except two same-part connections that cross; (i, j, swapped) —
    swapped: N's b end is next to P's a end."""

    def cost(pc: tuple[Pad, Pad], nc: tuple[Pad, Pad]) -> tuple[float, bool]:
        (pa, pb), (na, nb) = pc, nc
        d = (math.dist((pa.x, pa.y), (na.x, na.y)), math.dist((pb.x, pb.y), (nb.x, nb.y)))
        s = (math.dist((pa.x, pa.y), (nb.x, nb.y)), math.dist((pb.x, pb.y), (na.x, na.y)))
        if max(s) < max(d):
            return max(s), True
        return max(d), False

    def crossing(pc: tuple[Pad, Pad], nc: tuple[Pad, Pad]) -> bool:
        # R3: two connections inside one part each whose pad-to-pad lines cross or overlap
        # cannot run coupled (USB-C A6 - B6 / A7 - B7 interleave)
        (pa, pb), (na, nb) = pc, nc
        if pa.ref != pb.ref or na.ref != nb.ref:
            return False
        return seg_seg((pa.x, pa.y), (pb.x, pb.y), (na.x, na.y), (nb.x, nb.y)) <= 1e-9

    cand = sorted(
        (c[0], i, j, c[1])
        for i, pc in enumerate(p_conns)
        for j, nc in enumerate(n_conns)
        if (c := cost(pc, nc))[0] <= PAIR_MATCH_MM and not crossing(pc, nc)
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


NECK_ZONE_INSET_MM = 0.002  # the router's neck zone lies this far inside the escape zone


def default_min_track(model: BoardModel) -> float:
    """The preset minimum track a neck-down may not go below when the caller names none:
    the JLCPCB preset of the copper layer count, else `jlcpcb-4l` (as M12 / M16 do)."""
    preset = PRESETS.get(f"jlcpcb-{len(model.copper_layers)}l") or PRESETS["jlcpcb-4l"]
    return preset.min_track


def neck_zones(
    model: BoardModel, pads: Sequence[Pad], width: float, min_track: float
) -> tuple[tuple[tuple[Point, ...], float, tuple[str, ...]], ...]:
    """`RouteRequest.neck` for a connection of `width` (R3, spec §22.4): per end pad whose
    `geom.neck_width` is below `width`, its part's escape zone (inset by
    NECK_ZONE_INSET_MM, so a split point rounded to 1 nm stays inside the zone M12 checks),
    that neck width and the pad's copper layers (the only layers the neck applies on).
    Two pads of one part with the same neck share one entry (their layers merged)."""
    parts = {p.ref: p for p in model.parts}
    out: list[tuple[tuple[Point, ...], float, tuple[str, ...]]] = []
    for pad in pads:
        nw = neck_width(pad, width, min_track)
        part = parts.get(pad.ref)
        if nw >= width - 1e-9 or part is None:
            continue
        zone = escape_zone(part)
        if len(zone) < 3:
            continue
        xs, ys = [p[0] for p in zone], [p[1] for p in zone]
        g = NECK_ZONE_INSET_MM
        x0, y0, x1, y1 = min(xs) + g, min(ys) + g, max(xs) - g, max(ys) - g
        if not (x1 > x0 and y1 > y0):
            continue
        poly = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        names = model.copper_layers or tuple(n for n in pad.layers if n.endswith(".Cu"))
        on = tuple(n for n in names if _pad_on_layer(pad, n))
        same = next((k for k, (z, w, _l) in enumerate(out) if z == poly and w == nw), None)
        if same is None:
            out.append((poly, nw, on))
        else:
            z, w, old = out[same]
            out[same] = (z, w, tuple(n for n in model.copper_layers if n in old or n in on))
    return tuple(out)


def plan_connections(
    model: BoardModel,
    cls: Classification,
    nets: Sequence[str],
    *,
    layers: tuple[str, ...] | None,
    allow_vias: bool,
    tree: bool = False,
    min_track: float | None = None,
) -> list[ConnPlan]:
    """Per net (request order) its unrouted connections (`metrics.connections` without a
    copper path), shortest first; widths, clearance and via from the net class, layers in
    `layer_order` restricted to `layers` (default: `default_layers`). A net of a
    `cls.pairs` pair whose partner is requested too: its connections that match one of
    the partner's (ends close together) are planned once, as two adjacent plans with
    `pair` set (P first, then N), at the partner's first position in the request.
    Round 2 (R3): a connection ending at a pad narrower than its width gets `req.neck`
    (`neck_zones`, `min_track` default `default_min_track`); pairs keep their widths. With
    `tree`, every non-pair connection gets `req.tree`: it may end as a T-junction on the
    copper connected to pad b at the time it is solved (`build_grid` finds that copper).
    Unmatched connections between two pads of one part come last in their net
    (`_late_split`); crossing connections are never pair-matched (`_match`)."""
    allowed = layers if layers is not None else default_layers(model)
    floor = default_min_track(model) if min_track is None else min_track
    wanted = set(nets)
    out: list[ConnPlan] = []
    done: set[str] = set()

    def req(net: str, a: Pad, b: Pad, par: _Params, pair: bool) -> RouteRequest:
        kind = cls.net_kind.get(net, "signal")
        width = par.pair_width if pair and par.pair_width else par.width
        return RouteRequest(
            net,
            a,
            b,
            width,
            par.clearance,
            _order(model, kind, allowed),
            allow_vias,
            par.via,
            neck=() if pair else neck_zones(model, (a, b), width, floor),
            tree=tree and not pair,
        )

    for net in nets:
        if net in done:
            continue
        done.add(net)
        partner = _pair_partner(cls, net)
        own, own_late = _late_split(_unrouted(model, net))
        par = class_params(model, net)
        if partner is None or partner not in wanted or partner in done:
            out += [
                ConnPlan(net, a, b, req(net, a, b, par, False), None) for a, b in own + own_late
            ]
            continue
        done.add(partner)
        pair = next(p for p in cls.pairs if net in (p.p, p.n))
        p_net, n_net = pair.p, pair.n
        mine, theirs = own + own_late, _unrouted(model, partner)
        p_conns = mine if net == p_net else theirs
        n_conns = mine if net == n_net else theirs
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
        p_rest = _late_split([c for k, c in enumerate(p_conns) if k not in mp])
        n_rest = _late_split([c for k, c in enumerate(n_conns) if k not in mn])
        for k in (0, 1):  # every early one first, then the same-part ones
            out += [
                ConnPlan(p_net, a, b, req(p_net, a, b, p_par, False), None) for a, b in p_rest[k]
            ]
            out += [
                ConnPlan(n_net, a, b, req(n_net, a, b, n_par, False), None) for a, b in n_rest[k]
            ]
    return _m8_first(out, cls)


def _m8_first(plans: list[ConnPlan], cls: Classification) -> list[ConnPlan]:
    """`plans` with the connections M8 measures - a decoupling capacitor's supply pad to its
    IC pin, a crystal to its IC - first (crystals, then decaps, each in plan order; `early`
    set: `route_connections` routes them even before pass 0), the rest in order. R3: on
    the sp4_synth golden the crystal nets, routed earlier, walled C1.1 - U1.5 off to
    9.4 mm (an M8 hard failure); on R2b's placement other nets' straight segments and the
    shorter /OSC_OUT link pushed Y1.1 - U1.2 past 5 mm. Pair groups stay adjacent (an M8
    plan is never part of one)."""
    decap = {(d.cap, d.ic, d.pin) for d in cls.decoupling}
    xtal = {(x.ref, x.ic) for x in cls.crystals}

    def m8(p: ConnPlan) -> bool:
        return p.pair is None and (
            is_xtal(p)
            or any(
                (small.ref, big.ref, big.number) in decap for small, big in ((p.a, p.b), (p.b, p.a))
            )
        )

    def is_xtal(p: ConnPlan) -> bool:
        return (p.a.ref, p.b.ref) in xtal or (p.b.ref, p.a.ref) in xtal

    # crystals before decaps (as routing_order: crystal before supply), otherwise in the
    # plans' order (routing_order's net order: on sp4_synth the shorter /OSC_OUT link,
    # routed first, walled /OSC_IN Y1.1 - U1.2 off past 5 mm); pad to pin directly - a T
    # onto other copper would lengthen the path M8 measures
    first = sorted(
        (
            dataclasses.replace(
                p, early=True, req=dataclasses.replace(p.req, tree=False, escape=False)
            )
            for p in plans
            if m8(p)
        ),
        key=lambda p: not is_xtal(p),
    )
    return first + [p for p in plans if not m8(p)]


def _late_split(
    conns: list[tuple[Pad, Pad]],
) -> tuple[list[tuple[Pad, Pad]], list[tuple[Pad, Pad]]]:
    """(other connections, connections between two pads of ONE part). The latter route
    after the rest of their net (R3): e.g. USB-C A6 - B6 straddle A7 and can only be joined
    on one layer by a U-turn at the pads (an M16 pad loop) - once the net's other copper
    exists, a tree request lands on it as a T instead."""
    early = [(a, b) for a, b in conns if a.ref != b.ref]
    late = [(a, b) for a, b in conns if a.ref == b.ref]
    return early, late


def pair_gap(model: BoardModel, p_net: str, n_net: str) -> float:
    """The pair's gap (edge to edge): the class's diff-pair gap, else the larger clearance."""
    pp, pn = class_params(model, p_net), class_params(model, n_net)
    gaps = [g for g in (pp.pair_gap, pn.pair_gap) if g]
    return max(gaps) if gaps else max(pp.clearance, pn.clearance)


def with_footprint_keepouts(model: BoardModel, *reqs: RouteRequest) -> BoardModel:
    """`model` plus every footprint-embedded rule area as a track + via keepout zone on all
    copper layers (module docstring), except areas holding a pad of one of the requests."""
    pads = tuple((q.x, q.y) for r in reqs for q in (r.a, r.b))
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
    """`model` plus a via keepout around every existing hole — vias and drilled pads of any
    net — so a new via keeps the board's hole-to-hole rule from it (P5a residual: the
    router checks copper clearance only; own-net vias and pads are no via obstacle at all,
    and KiCad's DRC rejected new vias placed on top of own-net vias). For other-net
    plated holes the copper clearance is already larger; the octagon is harmless there."""
    if not req.allow_vias:
        return model
    holes = [(f"v:{v.kiid}", v.at, v.drill) for v in model.vias]
    holes += [
        (f"p:{q.ref}.{q.number}", (q.x, q.y), q.drill)
        for p in model.parts
        for q in p.pads
        if q.drill
    ]
    h2h = dict(model.rules).get("hole_to_hole_min", HOLE_TO_HOLE_DEFAULT_MM) or 0.0
    extra = []
    for name, at, drill in holes:
        # the new via's centre keeps via_r from the octagon (build_grid's via keepout)
        apothem = drill / 2 + req.via[1] / 2 + h2h - req.via[0] / 2
        if apothem <= 0:
            continue  # copper clearance already keeps the holes apart
        extra.append(
            Zone(
                kiid=f"hole-spacing:{name}",
                name="hole spacing",
                net="",
                layers=("*.Cu",),
                outline=_octagon(at, apothem),
                priority=0,
                rule_area=True,
                keepout=frozenset({"vias"}),
                settings=(),
                filled_area_mm2=0.0,
            )
        )
    return dataclasses.replace(model, zones=model.zones + tuple(extra)) if extra else model


def router_model(model: BoardModel, *reqs: RouteRequest) -> BoardModel:
    """What the router sees for `reqs` (one request or a pair): footprint keepouts and
    hole spacing added (one pass each)."""
    return with_hole_spacing(with_footprint_keepouts(model, *reqs), reqs[0])


# -- straight first (R3, owner rule b) ------------------------------------------------------


def straight_path(model: BoardModel, plan: ConnPlan) -> RoutePath | None:
    """The one straight segment of an aligned adjacent pad pair (spec §22.4, M16 a), or
    None: `geom.straight_corridor` for the narrower neck width of the two pads (as M16
    measures it) on the first request layer both pads share, run along the corridor's centre
    line from pad a's centre coordinate to pad b's (so both ends lie inside the pads),
    split at the neck zones (`req.neck`) like a routed path."""
    req = plan.req
    w = min([req.width, *(nw for _z, nw, _zl in req.neck)])
    # R4: narrower than the class only inside a necked end pad's escape zone (the request's
    # neck zones; M12, as M16's `neck_link_zones`)
    zones = tuple(z for z, _nw, _zl in req.neck)
    link = straight_link_corridor(
        plan.a, plan.b, req.width, w, zones, copper_layers=model.copper_layers
    )
    if link is None:
        return None
    layers, s, e, _outside = link
    layer = next((la for la in req.layers if la in layers), None)
    if layer is None:
        return None
    a, b = plan.a, plan.b
    if abs(s[1] - e[1]) <= 1e-9:  # along x
        start, end = (a.x, s[1]), (b.x, e[1])
    else:
        start, end = (s[0], a.y), (e[0], b.y)
    start = (round(start[0], 6), round(start[1], 6))
    end = (round(end[0], 6), round(end[1], 6))
    if not (_in_pad(start, a) and _in_pad(end, b)) or math.dist(start, end) < 1e-6:
        return None
    segs = tuple((layer, p, q, pw) for p, q, pw in neck_pieces(start, end, req.neck, req.width))
    return RoutePath(segments=segs, vias=(), length=math.dist(start, end))


@dataclass(frozen=True)
class Reserved:
    """A still-free straight corridor of an unrouted connection (R3): other nets' searches
    treat it as copper of `net`, so routing in chunks cannot block a later chunk's corridor."""

    key: str  # conn_key of the connection
    net: str
    layer: str
    start: Point
    end: Point
    width: float


def _corridor_free(
    model: BoardModel, net: str, layer: str, s: Point, e: Point, width: float, clear: float
) -> bool:
    """No other-net pad on `layer`, via or track on `layer` within `width / 2 + clear`
    of segment se, no track keepout on `layer` and no footprint rule area touching it."""
    reach = width / 2 + clear
    x0, x1 = min(s[0], e[0]) - reach - 1.0, max(s[0], e[0]) + reach + 1.0
    y0, y1 = min(s[1], e[1]) - reach - 1.0, max(s[1], e[1]) + reach + 1.0

    def far(pts: Sequence[Point], grow: float) -> bool:
        xs, ys = [q[0] for q in pts], [q[1] for q in pts]
        return (
            min(xs) - grow > x1 or max(xs) + grow < x0 or min(ys) - grow > y1 or max(ys) + grow < y0
        )

    for part in model.parts:
        if part.courtyard and far(part.courtyard, 5.0) and not part.rule_areas:
            continue
        for q in part.pads:
            near = q.net != net and _pad_on_layer(q, layer)
            if near and seg_shape(s, e, _pad_shape(q, 0.0)) < reach - 1e-9:
                return False
        for ra in part.rule_areas:
            if len(ra) >= 3 and seg_shape(s, e, ("p", tuple(ra), 0.0)) < width / 2:
                return False
    for v in model.vias:
        if v.net != net and seg_shape(s, e, ("d", *v.at, v.diameter / 2, 0.0)) < reach - 1e-9:
            return False
    for t in model.tracks:
        if far((t.start, t.end), t.width):
            continue
        near = t.net != net and t.layer == layer
        if near and seg_seg(s, e, t.start, t.end) < reach + t.width / 2 - 1e-9:
            return False
    for z in model.zones:
        if z.rule_area and z.keepout & {"tracks", "copper"} and len(z.outline) >= 3:
            on = not z.layers or "*.Cu" in z.layers or layer in z.layers
            if on and seg_shape(s, e, ("p", tuple(z.outline), 0.0)) < width / 2:
                return False
    return True


def _path_free(model: BoardModel, plan: ConnPlan, path: RoutePath) -> bool:
    """`straight_path`'s segment has no other-net copper or keepout in the way - each piece
    at its own width (R4: a neck piece next to fine-pitch pins is checked at the neck
    width, not at the class width of the piece in the other pad's zone)."""
    return all(
        _corridor_free(model, plan.net, la, s, e, w, plan.req.clearance)
        for la, s, e, w in path.segments
    )


def corridor_reservations(
    model: BoardModel, cls: Classification, *, min_track: float | None = None
) -> tuple[Reserved, ...]:
    """Every still-free straight corridor (`straight_path`) of an unrouted connection of a
    non-ground net on the board (R3, `route_connections(reserve=...)`). Cheap first: only
    MST connections with a corridor between their two pads are checked for copper
    (`routed_length`) and a free path; reserved per piece at its own width (R4)."""
    floor = default_min_track(model) if min_track is None else min_track
    nets = sorted({q.net for p in model.parts for q in p.pads if q.net})
    out: list[Reserved] = []
    for net in nets:
        if cls.net_kind.get(net, "signal") == "ground":
            continue
        par = class_params(model, net)
        # (width, neck) as plan_connections places it: a matched pair half at the pair
        # width without neck-down, any other connection at the class width with necks; a
        # pair net's connection may be either, so the pair width is tried first
        options = [(par.width, True)]
        if _pair_partner(cls, net) is not None:
            options.insert(0, (par.pair_width or par.width, False))  # as plan_connections
        for a, b in connections(model, net):
            if _joined(a, b):
                continue
            for width, necked in options:
                w = min(width, neck_width(a, width, floor), neck_width(b, width, floor))
                w = w if necked else width
                if straight_corridor(a, b, w, copper_layers=model.copper_layers) is None:
                    continue
                if routed_length(model, a, b) is not None:
                    break
                req = RouteRequest(
                    net, a, b, width, par.clearance, default_layers(model), False, par.via,
                    neck=neck_zones(model, (a, b), width, floor) if necked else (),
                )  # fmt: skip
                plan = ConnPlan(net, a, b, req, None)
                path = straight_path(model, plan)
                if path is None:
                    continue
                if _path_free(model, plan, path):
                    # one reservation per piece at its own width (R4, as `_path_free`)
                    out += [Reserved(_key(plan), net, la, s, e, w) for la, s, e, w in path.segments]
                    break  # else the next option (a narrower width may still be free)
    return tuple(out)


def _with_reserved(model: BoardModel, reserved: Sequence[Reserved], nets: set[str]) -> BoardModel:
    """`model` plus the reserved corridors of other nets as (unreal) tracks."""
    extra = tuple(
        Track(f"reserved:{r.key}", r.net, r.layer, r.start, r.end, r.width, True)
        for r in reserved
        if r.net not in nets
    )
    return dataclasses.replace(model, tracks=model.tracks + extra) if extra else model


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
        for layer, a, b, width in path.segments:
            t = KipyTrack()
            t.layer = layer_from_canonical_name(layer)
            t.net = net
            t.start, t.end = _vec(a), _vec(b)
            t.width = mm_to_nm(width)
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
        for layer, a, b, width in path.segments:
            tracks.append(Track(next(it), req.net, layer, a, b, width, lock))
        for at in path.vias:
            vias.append(Via(next(it), req.net, at, req.via[0], req.via[1], lock))
    return tracks, vias


# -- the per-connection loop (B-C12) -----------------------------------------------------


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


async def _solve(
    model: BoardModel,
    group: Sequence[ConnPlan],
    margin: float,
    remaining: float | None,
    reserved: Sequence[Reserved] = (),
    deadline: float | None = None,
) -> list[RoutePath] | None:
    timeout = SOLVE_TIMEOUT_S if remaining is None else min(SOLVE_TIMEOUT_S, remaining)
    model = _with_reserved(model, reserved, {p.net for p in group})
    if len(group) == 2:
        p, n = group[0].req, group[1].req
        m = router_model(model, p, n)
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
    # R4 (owner 2026-11-27): a pad-entry loop or jog is never accepted - also not for a
    # link between two pads of one part (USB-C A6 - B6) or an M8-measured link; those go
    # through a via, enter from the other side or land on the net's copper as a T instead
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
    straight: bool = False  # a pass-0 straight segment (owner rule b): never ripped


@dataclass
class _Loop:
    model: BoardModel
    lock: bool
    deadline: float | None
    # by plan, not by key: duplicate pad numbers (a SOT-223 tab, shield pads) give two
    # connections of one net the same key
    results: dict[ConnPlan, ConnResult] = field(default_factory=dict)
    kept: list[_Kept] = field(default_factory=list)
    drc: DrcResult | None = None
    started: dict[ConnPlan, float] = field(default_factory=dict)  # seconds spent solving
    reserved: list[Reserved] = field(default_factory=list)  # other connections' corridors
    # R7 `_alternate_ends`: an alternative plan records its result under the original one
    alias: dict[ConnPlan, ConnPlan] = field(default_factory=dict)


def _key(p: ConnPlan) -> str:
    return conn_key(p.net, p.a, p.b)


def _fail(st: _Loop, group: Sequence[ConnPlan], reason: str) -> None:
    for p in group:
        st.results[st.alias.get(p, p)] = ConnResult(
            _key(p), p.net, _pad_name(p.a), _pad_name(p.b), "failed", reason, 0.0, 0, (),
            seconds=round(st.started.get(p, 0.0), 2),
        )  # fmt: skip


def entry_defect(path: RoutePath, req: RouteRequest) -> bool:
    """`path` loops or jogs in an end pad's entry zone (M16 b / c): only `solve`'s last
    resort or a same-part link produces one - reported, never silent."""
    runs = layer_runs(path)
    if not runs:
        return False
    first = merge_collinear(runs[0][1])
    last = merge_collinear(runs[-1][1])[::-1]
    return _entry_bad(first, req.a, req.width) or (
        not tree_end(path, req) and _entry_bad(last, req.b, req.width)
    )


def _record(st: _Loop, kept: _Kept) -> None:
    coupling = pair_coupling(kept.paths[0], kept.paths[1]) if len(kept.paths) == 2 else None
    it = iter(kept.kiids)
    for p, path in zip(kept.group, kept.paths, strict=True):
        own = tuple(next(it) for _ in range(len(path.segments) + len(path.vias)))
        st.results[st.alias.get(p, p)] = ConnResult(
            _key(p), p.net, _pad_name(p.a), _pad_name(p.b), "routed", None, path.length,
            len(path.vias), own, coupling, round(st.started.get(p, 0.0), 2),
            pad_entry=entry_defect(path, p.req),
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
    if _timed_out(st):
        return "timeout"  # the solve used up the time: no push, no DRC that cannot finish
    items = [(p.req, path) for p, path in zip(group, paths, strict=True)]
    made = await _push(sctx, items, st.lock)
    remaining = _remaining(st.deadline)
    drc_s = DRC_TIMEOUT_S if remaining is None else min(DRC_TIMEOUT_S, remaining)
    if drc_s <= 0:  # the push used up the time: unverified copper never stays
        await _drop(sctx, made)
        return "timeout"
    try:
        drc = await sctx.drc(timeout_s=drc_s)
    except DrcTimeout:
        await _drop(sctx, made)  # unverified: never kept
        return "timeout"
    bad = _rejecting(drc, made)
    if bad:
        await _drop(sctx, made)
        return f"drc_rejected: {bad[0]}"
    st.drc = drc  # the last DRC whose copper stays (the report's drc_errors)
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
            paths = await _solve(st.model, group, margin, remaining, st.reserved, st.deadline)
        except RouteTooLarge:
            return "too_large"
        except StructuredToolError as exc:
            if exc.code != "board_compute_timeout":
                raise
            return "timeout"
        finally:
            for p in group:
                st.started[p] = st.started.get(p, 0.0) + time.monotonic() - t0
        if paths is None:
            continue  # a DRC rejection of an earlier margin stays the reason
        got = await _place(sctx, st, group, paths)
        if got is None:
            return None
        if got == "timeout":
            return got
        reason = got
    return reason


async def _restore(
    sctx: BoardStageContext, st: _Loop, group: list[ConnPlan], paths: list[RoutePath]
) -> None:
    """Put a taken-out group back exactly: the board around it is again what its DRC
    verified (everything added since was removed), so no DRC gate — which also keeps a
    restore from failing on a deadline that ran out during the retry."""
    items = [(p.req, path) for p, path in zip(group, paths, strict=True)]
    made = await _push(sctx, items, st.lock)
    tracks, vias = _model_items(items, made, st.lock)
    st.model = dataclasses.replace(
        st.model, tracks=st.model.tracks + tuple(tracks), vias=st.model.vias + tuple(vias)
    )
    kept = _Kept(list(group), list(paths), list(made))
    st.kept.append(kept)
    _record(st, kept)


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
        st.results.pop(st.alias.get(p, p), None)


def _blockers(st: _Loop, group: Sequence[ConnPlan]) -> list[_Kept]:
    """Kept groups of other nets whose copper passes within RIP_HALO_MM of the box around
    the group's pads, nearest first (at most RIP_MAX). Never a pass-0 straight segment
    (owner rule b) nor an M8-measured link (`early`: a re-route could push it past the M8
    limit). `_rip_and_retry` takes the T paths that end on a ripped group out with it."""
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
            for _, a, b, _w in path.segments:
                for q in (a, b, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)):
                    best = min(best, min(math.dist(q, c) for c in pts))
        return best

    near = [
        k
        for k in st.kept
        if not k.straight
        and not any(p.early for p in k.group)
        and not nets & {p.net for p in k.group}
        and any(
            inside(q)
            for path in k.paths
            for _, a, b, _w in path.segments
            for q in (a, b, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        )
    ]
    return sorted(near, key=dist)[:RIP_MAX]


def _pt_seg_dist(p: Point, a: Point, b: Point) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    ll = vx * vx + vy * vy
    t = 0.0 if ll <= 0 else max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / ll))
    return math.hypot(p[0] - a[0] - t * vx, p[1] - a[1] - t * vy)


async def _rip_and_retry(sctx: BoardStageContext, st: _Loop, group: Sequence[ConnPlan]) -> bool:
    """A blocked connection: take one nearby connection of this loop out - with the kept
    paths of its net that end on it as a T (R3, `_dependants`: they would dangle) - route
    the blocked one, then the removed ones again; when any of that fails, everything routed
    in the try goes and the removed ones are put back exactly (same paths, `_restore`).
    True = all routed."""
    for cand in _blockers(st, group):
        if _timed_out(st):
            return False
        if not any(k is cand for k in st.kept):
            continue  # taken out and restored (as new copper) with an earlier try's anchor
        removed = [cand, *_dependants(st, cand)]
        saved = [(k.group, k.paths) for k in removed]
        for k in removed:
            await _unkeep(sctx, st, k)
        done: list[list[ConnPlan]] = []
        ok = await _attempt(sctx, st, group) is None
        if ok:
            done.append(list(group))
            for old_group, _paths in saved:
                if await _attempt(sctx, st, old_group) is not None:
                    ok = False
                    break
                done.append(list(old_group))
        if ok:
            return True
        for g in reversed(done):  # undo the try, then put the removed copper back
            await _unkeep(sctx, st, next(k for k in st.kept if k.group == g))
        for old_group, old_paths in saved:
            await _restore(sctx, st, old_group, old_paths)
    return False


def _dependants(st: _Loop, anchor: _Kept) -> list[_Kept]:
    """Kept groups with a tree path (same net) ending as a T on `anchor`'s copper, and
    transitively on theirs."""
    out: list[_Kept] = []
    todo = [anchor]
    while todo:
        cur = todo.pop()
        nets = {q.net for q in cur.group}
        for j in st.kept:
            if j is anchor or j in out:
                continue
            for p, path in zip(j.group, j.paths, strict=True):
                if p.net not in nets or not path.segments or not tree_end(path, p.req):
                    continue
                layer, end = path.segments[-1][0], path.segments[-1][2]
                if any(
                    la == layer and _pt_seg_dist(end, a, b) <= 0.002
                    for ap in cur.paths
                    for la, a, b, _w in ap.segments
                ):
                    out.append(j)
                    todo.append(j)
                    break
    return out


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
    if reason == "no_path" and await _alternate_ends(sctx, st, group[0]):
        return
    if await _rip_and_retry(sctx, st, group):
        return
    _fail(st, group, reason if not _timed_out(st) else "timeout")


ALT_ENDS_MAX = 3  # R7: other end pairs tried after a no_path


async def _alternate_ends(sctx: BoardStageContext, st: _Loop, p: ConnPlan) -> bool:
    """R7 (ODH: the charger's CHG_N pin was walled in by its own short link to a pull-up,
    the far MCU pin unreachable from it - from the pull-up's other side it was free): the
    connection joins two copper islands of its net, so any pad of a's island may link to
    any pad of b's. After a `no_path`, the `ALT_ENDS_MAX` nearest other end pairs are
    tried (same width / layers; neck zones for the new ends); the first routed one stands
    for the connection (its result names its own pads)."""
    pads = [q for part in st.model.parts for q in part.pads if q.net == p.net]

    def island(x: Pad) -> list[Pad]:
        return [
            q for q in pads if q is x or _joined(x, q) or routed_length(st.model, x, q) is not None
        ]

    ia, ib = island(p.a), island(p.b)
    if any(q in ib for q in ia):
        return False  # joined meanwhile (another connection did it)
    alts = sorted(
        ((x, y) for x in ia for y in ib if (x, y) != (p.a, p.b)),
        key=lambda xy: octi_dist((xy[0].x, xy[0].y), (xy[1].x, xy[1].y)),
    )[:ALT_ENDS_MAX]
    floor = default_min_track(st.model)
    for x, y in alts:
        neck = neck_zones(st.model, (x, y), p.req.width, floor) if p.pair is None else ()
        alt = dataclasses.replace(p, a=x, b=y, req=dataclasses.replace(p.req, a=x, b=y, neck=neck))
        st.alias[alt] = p
        if await _attempt(sctx, st, [alt]) is None:
            return True
        del st.alias[alt]
        if _timed_out(st):
            return False
    return False


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


def _couple_straight(st: _Loop, plans: Sequence[ConnPlan], done: set[ConnPlan]) -> None:
    """A matched pair (two adjacent plans) whose two connections both got their straight
    segment is coupled: its results carry `pair_coupling` like a coupled route."""
    by_plan = {k.group[0]: k for k in st.kept if k.straight and len(k.group) == 1}
    for group in _groups(plans):
        if len(group) != 2 or not all(q in done and q in by_plan for q in group):
            continue
        c = pair_coupling(by_plan[group[0]].paths[0], by_plan[group[1]].paths[0])
        for q in group:
            st.results[q] = dataclasses.replace(st.results[q], coupling=c)


STRAIGHT_RETRY_MAX = 4  # pass-0 segments retried alone (each its own push + DRC)


async def _straight_pass(
    sctx: BoardStageContext, st: _Loop, plans: Sequence[ConnPlan]
) -> set[ConnPlan]:
    """Pass 0 of `route_connections`: the plans routed by their straight segment. A half
    of a matched pair goes straight even when its partner cannot (owner rule b: a dogleg
    is an error, pair coupling only a review - on sp4_synth J1.B7-U2.3 otherwise doglegged
    and pushed USB-C A7-B7 into an M5 detour failure); its partner then routes alone."""
    model = st.model

    def scan() -> list[tuple[ConnPlan, RoutePath]]:
        return [
            (p, path)
            for p in plans
            if (path := straight_path(model, p)) is not None and _path_free(model, p, path)
        ]

    cands = await asyncio.to_thread(scan)  # off the event loop (whole-board scans)
    if not cands or _timed_out(st):
        return set()
    items = [(p.req, path) for p, path in cands]
    made = await _push(sctx, items, st.lock)
    remaining = _remaining(st.deadline)
    drc_s = DRC_TIMEOUT_S if remaining is None else min(DRC_TIMEOUT_S, remaining)
    try:
        if drc_s <= 0:
            raise DrcTimeout(0.0, "no time left")
        drc = await sctx.drc(timeout_s=drc_s)
    except DrcTimeout:
        await _drop(sctx, made)  # unverified: never kept; the search tries them again
        return set()
    it = iter(made)
    own = [[next(it) for _ in path.segments] for _p, path in cands]
    mine = set(made)
    rejected: set[int] = set()
    retry: list[int] = []
    for k, ids in enumerate(own):
        hits = [
            m
            for m in drc.markers
            if m.type not in IGNORED_DRC and m.severity != "exclusion" and set(ids) & set(m.kiids)
        ]
        if not hits:
            continue
        rejected.add(k)
        others = [set(m.kiids) - set(ids) for m in hits]
        if all(o and o <= mine for o in others):
            retry.append(k)  # only another straight segment of this pass is in the way
    gone = [i for k in sorted(rejected) for i in own[k]]
    if gone:
        await _drop(sctx, gone)
    done: set[ConnPlan] = set()
    for k, (p, path) in enumerate(cands):
        if k in rejected:
            continue
        tracks, vias = _model_items([(p.req, path)], own[k], st.lock)
        st.model = dataclasses.replace(
            st.model, tracks=st.model.tracks + tuple(tracks), vias=st.model.vias + tuple(vias)
        )
        kept = _Kept([p], [path], list(own[k]), straight=True)
        st.kept.append(kept)
        _record(st, kept)
        done.add(p)
    if done:  # the last verified DRC, without the markers of copper dropped again
        dropped = set(gone)
        st.drc = dataclasses.replace(
            drc, markers=tuple(m for m in drc.markers if not dropped & set(m.kiids))
        )
    for k in retry[:STRAIGHT_RETRY_MAX]:  # alone, after the others: each with its own DRC
        p, path = cands[k]
        if _timed_out(st):
            break
        if await _place(sctx, st, [p], [path]) is None:
            st.kept[-1].straight = True
            done.add(p)
    _couple_straight(st, plans, done)
    return done


async def route_connections(
    sctx: BoardStageContext,
    plans: Sequence[ConnPlan],
    *,
    lock: bool,
    deadline: float | None = None,
    reserve: Sequence[Reserved] = (),
) -> RouteOutcome:
    """Route `plans` in order (B-C12): the model is read once, accepted copper is added to
    it in memory, so later connections see it. `deadline` (a `time.monotonic()` value):
    a connection not started before it is failed `timeout` and never solved; each
    `run_cpu` gets `timeout_s=min(30, remaining)` and `wait_s=remaining`, each DRC
    `min(60, remaining)`. A `DrcTimeout` fails only its connection (its items removed).
    A coupled pair that cannot be routed is routed net by net; a connection that stays
    blocked may take out one nearby connection of this call at a time (at most RIP_MAX
    tries), route first and re-route the other — never copper the call did not create.
    Never relies on DRC seeing an open commit (P1a probe): the DRC runs after the push.
    Round 2 (R3, owner rule b): pass 0 first gives every plan with a straight corridor
    (`straight_path`) its one segment (one push, one DRC; a segment rejected only because of
    another pass-0 segment is retried once, alone, at the end of the pass); the search
    routes the rest. `reserve` (`corridor_reservations`): the free
    corridors of unrouted connections not planned here, obstacles for every other net's
    search."""
    st = _Loop(await sctx.model(), lock, deadline)
    keys = {_key(p) for p in plans}
    st.reserved = [r for r in reserve if r.key not in keys]
    # the M8-measured links first (straight when they can be, else searched), so other
    # nets' pass-0 segments cannot wall them off; then pass 0 and the search for the rest
    early = [p for p in plans if p.early]
    straight_e = await _straight_pass(sctx, st, early) if early else set()
    groups = _groups([p for p in early if p not in straight_e])
    for group in groups:
        await _route_group(sctx, st, group)
    rest = [p for p in plans if not p.early]
    straight = await _straight_pass(sctx, st, rest)
    late = _groups([p for p in rest if p not in straight])
    for group in late:
        await _route_group(sctx, st, group)
    groups += late
    # one more round for what stayed blocked: the board around it changed since
    for group in groups:
        if _timed_out(st):
            break
        failed = [
            p
            for p in group
            if st.results[p].status == "failed"
            and st.results[p].reason not in ("timeout", "too_large")
        ]
        for p in failed:  # one by one: a failed pair was already routed net by net
            if _timed_out(st):
                _fail(st, [p], "timeout")
                continue
            reason = st.results[p].reason or "no_path"
            if not await _rip_and_retry(sctx, st, [p]):
                _fail(st, [p], reason if not _timed_out(st) else "timeout")
    order = {p: i for i, p in enumerate(plans)}
    results: list[ConnResult] = []
    seen: dict[str, int] = {}
    for _, r in sorted(st.results.items(), key=lambda pr: order.get(pr[0], len(order))):
        seen[r.key] = seen.get(r.key, 0) + 1
        if seen[r.key] > 1:  # the same pad names twice: "<key>#2", "<key>#3", ...
            r = dataclasses.replace(r, key=f"{r.key}#{seen[r.key]}")
        results.append(r)
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
    defects = [f"{r.a}-{r.b}" for r in routed if r.pad_entry]
    if defects:  # forced (no clean path) or a same-part link: M16 reports them
        entry["pad_entry_defects"] = defects
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
    """M4 / M7 / M15 counts over the call's new copper (B-C5 `analyse_tracks`, pads and
    vias of the whole board). Round 2 (R3): a T-junction is no M4 failure (owner rule a,
    the router's tree branches); a Y-junction counts as M15."""
    sub = dataclasses.replace(
        model, tracks=tuple(t for t in model.tracks if t.kiid in kiids), arcs=()
    )
    rep = analyse_tracks(sub, index=TrackIndex(model))
    m4 = len(rep.non_octilinear) + len(rep.ninety) + len(rep.acute) + len(rep.off_angle)
    return {
        "M4": m4,
        "M7": len(rep.micro_jogs) + len(rep.short_chamfers),
        "M15": len(rep.y_junctions),
        "bends": rep.bends,
    }


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

    min_track = (preset_for(model, doc) or PRESETS["jlcpcb-4l"]).min_track

    def plan() -> tuple[list[ConnPlan], tuple[Reserved, ...]]:
        order = routing_order(model, cls, nets)
        planned = plan_connections(
            model,
            cls,
            order,
            layers=route_layers,
            allow_vias=allow_vias,
            tree=True,
            min_track=min_track,
        )
        return planned, corridor_reservations(model, cls, min_track=min_track)

    plans, reserve = await asyncio.to_thread(plan)
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
        out = await route_connections(
            sctx, plans, lock=lock, deadline=state.deadline, reserve=reserve
        )
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
