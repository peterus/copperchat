"""Part transforms and the cluster layout (B-C11, spec §9.2). Pure, deterministic.

`layout_cluster` lays one cluster out around its anchor at `(0, 0)`, rotation 0, top side:

* every satellite goes to a **host** — the part it belongs next to: a decoupling cap to its
  IC pin, a load cap to its crystal, a divider resistor to the pin or its partner resistor,
  otherwise the placed part sharing the most signal nets (then supply, then ground nets;
  ties: fewer pads, placed earlier);
* it sits on the side of the host where the connected host pad is, **flush** outside the
  host's courtyard (ring 1), its connected pad aligned with that pad, so the airwire is a
  short straight line; a 2-pad part keeps its pad axis radial with the connected pad facing
  the host (decoupling caps: supply pad towards the served pin, ground pad outwards);
* when that spot is taken it slides along the row first and steps outwards (ring 2, …)
  only when that is cheaper (`RADIAL_WEIGHT`), so rows stay aligned;
* decoupling caps and crystals keep the minimum courtyard gap `gap`; every other satellite keeps
  `gap + SATELLITE_EXTRA_MM` (routing room between neighbours);
* decoupling caps of one IC and supply net are spread over the IC's pins of that net, the
  smallest value at each pin first (spec §9.3 "the smallest value closest").

Every position is snapped to the 0.05 mm grid (outwards, so gaps never shrink).
"""

from __future__ import annotations

import dataclasses
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from copperchat.board.classify import Classification
from copperchat.board.geom import octi_dist
from copperchat.board.model import BoardModel, Pad, Part, Point, TextItem
from copperchat.board.placer.cluster import Cluster, NetSets
from copperchat.board.staging import Move, natural_key, part_box

__all__ = [
    "GRID_MM",
    "Box",
    "Move",
    "apply_moves",
    "box_of",
    "layout_cluster",
    "moved_part",
    "snap",
]

Box = tuple[float, float, float, float]  # x0, y0, x1, y1

GRID_MM = 0.05
SATELLITE_EXTRA_MM = 0.3  # extra courtyard gap for satellites that are not decoupling caps
RADIAL_WEIGHT = 1.5  # unconnected satellites: one mm outwards costs 1.5 mm along a row
ALIGN_WEIGHT = 0.5  # connected satellites: cost per mm slid away from the aligned spot
SIDE_PENALTY_MM = 1.5  # a host side next to the one the connected pin points to
NEAR_TARGET_MM = 3.0  # host pads of the satellite's nets this close to the first form a target
_EPS = 1e-9


# ---- grid ----------------------------------------------------------------------------------


def snap(v: float, grid: float = GRID_MM) -> float:
    return round(round(v / grid) * grid, 6)


def snap_up(v: float, grid: float = GRID_MM) -> float:
    return round(math.ceil(v / grid - 1e-6) * grid, 6)


def snap_down(v: float, grid: float = GRID_MM) -> float:
    return round(math.floor(v / grid + 1e-6) * grid, 6)


# ---- transforms ----------------------------------------------------------------------------


def _cs(deg: float) -> tuple[float, float]:
    d = deg % 360
    exact = {0: (1.0, 0.0), 90: (0.0, 1.0), 180: (-1.0, 0.0), 270: (0.0, -1.0)}
    if round(d, 9) in exact:
        return exact[int(round(d, 9))]
    a = math.radians(d)
    return math.cos(a), math.sin(a)


def rotate(pt: Point, deg: float) -> Point:
    """`pt` (relative to an origin) rotated by `deg` (KiCad: counter-clockwise on screen,
    y down — the convention of `read._to_board`)."""
    c, s = _cs(deg)
    x, y = pt
    return (x * c + y * s, -x * s + y * c)


_FLIP_LAYER = re.compile(r"^([FB])\.")


def _flip_layer(layer: str) -> str:
    return _FLIP_LAYER.sub(lambda m: "B." if m.group(1) == "F" else "F.", layer)


def moved_part(p: Part, m: Move) -> Part:
    """`p` at `m` (position, rotation, side): courtyard, pads, texts and rule areas moved
    with it. A side change is KiCad's left/right flip about the footprint position
    (x mirrored, orientation 180° - rot), then the rotation to `m.rot`."""
    ox, oy = p.x, p.y
    rot0 = p.rot
    flip = m.side != p.side

    def local(pt: Point) -> Point:
        x, y = pt[0] - ox, pt[1] - oy
        return (-x, y) if flip else (x, y)

    if flip:
        rot0 = (180.0 - p.rot) % 360
    delta = (m.rot - rot0) % 360

    def place(pt: Point) -> Point:
        dx, dy = rotate(local(pt), delta)
        return (round(m.x + dx, 6), round(m.y + dy, 6))

    def pad(q: Pad) -> Pad:
        x, y = place((q.x, q.y))
        qrot = (180.0 - q.rot) % 360 if flip else q.rot
        layers = frozenset(_flip_layer(layer) for layer in q.layers) if flip else q.layers
        return dataclasses.replace(q, x=x, y=y, rot=(qrot + delta) % 360, layers=layers)

    def text(t: TextItem) -> TextItem:
        layer = _flip_layer(t.layer) if flip else t.layer
        return dataclasses.replace(t, at=place(t.at), layer=layer)

    return dataclasses.replace(
        p,
        x=m.x,
        y=m.y,
        rot=m.rot % 360,
        side=m.side,
        courtyard=tuple(place(pt) for pt in p.courtyard),
        pads=tuple(pad(q) for q in p.pads),
        texts=tuple(text(t) for t in p.texts),
        rule_areas=tuple(tuple(place(pt) for pt in area) for area in p.rule_areas),
    )


def apply_moves(model: BoardModel, moves: Sequence[Move]) -> BoardModel:
    """`model` with every move applied (unknown refs ignored; the last move of a ref wins)."""
    by_ref = {m.ref: m for m in moves}
    if not by_ref:
        return model
    parts = tuple(moved_part(p, by_ref[p.ref]) if p.ref in by_ref else p for p in model.parts)
    return dataclasses.replace(model, parts=parts)


def box_of(p: Part) -> Box:
    return part_box(p)


def grow(b: Box, d: float) -> Box:
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def boxes_hit(a: Box, b: Box, gap: float = 0.0) -> bool:
    """True when `a` and `b` are closer than `gap` (strictly; touching at `gap` is fine)."""
    return (
        a[0] < b[2] + gap - _EPS
        and b[0] < a[2] + gap - _EPS
        and a[1] < b[3] + gap - _EPS
        and b[1] < a[3] + gap - _EPS
    )


# ---- cluster layout ------------------------------------------------------------------------

_CAP_VALUE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([pnuµm]?)(\d*)", re.IGNORECASE)
_SCALE = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0}


def cap_value(value: str) -> float:
    """Capacitance in F of a value text (`100nF`, `4.7uF`, `4u7`); unparsable → inf."""
    m = _CAP_VALUE.match(value)
    if not m:
        return math.inf
    num = float(m.group(1) + ("." + m.group(3) if m.group(3) else ""))
    return num * _SCALE[m.group(2).lower() if m.group(2) != "µ" else "µ"]


@dataclass(frozen=True)
class _Want:
    """Where a satellite wants to be: next to `target` pads of `host`, its own `pads` on
    those nets facing them."""

    host: str
    target: tuple[Pad, ...]
    own_nets: frozenset[str]


# side → (radial axis, sign): the outward normal of the host's side
_SIDES: Mapping[str, tuple[int, int]] = {
    "left": (0, -1),
    "right": (0, 1),
    "top": (1, -1),
    "bottom": (1, 1),
}


PAD_AXIS_RATIO = 1.3  # a pad this much longer than wide leaves its part along its long axis


def _pad_axis(q: Pad) -> int | None:
    """0 when the (rotated) pad is long in x, 1 when long in y, None when about square."""
    c, s = _cs(q.rot)
    w = abs(q.w * c) + abs(q.h * s)
    h = abs(q.w * s) + abs(q.h * c)
    if w >= PAD_AXIS_RATIO * h:
        return 0
    if h >= PAD_AXIS_RATIO * w:
        return 1
    return None


def _side_of(b: Box, pads: Sequence[Pad]) -> str:
    """The host side a satellite of `pads` goes to: the nearest box edge to their centre;
    an elongated pin pad (SOIC, connector row) only allows the two edges along its long
    axis — a pin leaves the part where the pad points."""
    t = _mean([(q.x, q.y) for q in pads])
    d = {
        "left": t[0] - b[0],
        "right": b[2] - t[0],
        "top": t[1] - b[1],
        "bottom": b[3] - t[1],
    }
    axis = _pad_axis(pads[0])
    sides = [s for s in _SIDES if axis is None or _SIDES[s][0] == axis]
    return min(sides, key=lambda s: (d[s], list(_SIDES).index(s)))


def _mean(pts: Sequence[Point]) -> Point:
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


class _Layout:
    def __init__(self, model: BoardModel, cls: Classification, c: Cluster, gap: float) -> None:
        self.model = model
        self.cls = cls
        self.gap = gap
        self.anchor = c.anchor if c.anchor in c.members else c.members[0]
        self.members = c.members
        self.ns = NetSets(model, cls, c.members)
        self.placed: dict[str, Part] = {}
        self.order: list[str] = []
        self.decap_round: dict[str, int] = {}
        self.shared_sig: dict[str, int] = defaultdict(int)
        self.shared_all: dict[str, int] = defaultdict(int)
        self.decap_pin = self._assign_decaps()
        self.priority = self._priorities()

    # -- decoupling assignment --

    def _assign_decaps(self) -> dict[str, tuple[str, str]]:
        """cap → (ic, pin number): per IC and supply net the caps (smallest value, then
        smallest courtyard, then ref) go round-robin over the IC's pins of that net."""
        inside = set(self.members)
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for d in self.cls.decoupling:
            if d.cap in inside and d.ic in inside:
                groups[(d.ic, d.supply_net)].append(d.cap)
        out: dict[str, tuple[str, str]] = {}
        for (ic, net), caps in sorted(groups.items(), key=lambda kv: natural_key(kv[0][0])):
            pins = sorted(
                {q.number for q in self.model.part(ic).pads if q.net == net}, key=natural_key
            )
            if not pins:
                continue
            caps.sort(
                key=lambda r: (
                    cap_value(self.model.part(r).value),
                    _area(self.model.part(r)),
                    natural_key(r),
                )
            )
            for i, cap in enumerate(caps):
                out[cap] = (ic, pins[i % len(pins)])
                self.decap_round[cap] = i // len(pins)
        return out

    def _priorities(self) -> dict[str, tuple[object, ...]]:
        """Placement order: crystals (big, pinned to two IC pins), the first decoupling cap
        of every supply pin, load caps, switcher passives, then everything else by
        connectivity (see `_next`), bulk decoupling caps last (they take the supply pin
        with the most room). Small parts slide around the big ones placed before them."""
        pr: dict[str, tuple[object, ...]] = {}
        for x in self.cls.crystals:
            if x.ref in self.members:
                pr.setdefault(x.ref, (0,))
                for r in x.load_caps:
                    pr.setdefault(r, (2,))
        for cap, (ic, pin) in self.decap_pin.items():
            rnd = self.decap_round[cap]
            pr[cap] = (
                (1, natural_key(ic), natural_key(pin)) if rnd == 0 else (5, rnd, natural_key(ic))
            )
        for s in self.cls.switchers:
            if s.ic not in self.members:
                continue
            for i, refs in enumerate(((s.inductor,), s.in_caps, s.out_caps, s.fb_parts)):
                for r in refs:
                    pr.setdefault(r, (3, i))
        return pr

    def _host_needed(self, ref: str) -> str | None:
        """The part `ref` must wait for (its IC, crystal or inductor), if any."""
        if ref in self.decap_pin:
            return self.decap_pin[ref][0]
        for x in self.cls.crystals:
            if ref in x.load_caps and x.ref in self.members:
                return x.ref
            if ref == x.ref and x.ic in self.members:
                return x.ic
        for s in self.cls.switchers:
            if ref == s.inductor or ref in s.in_caps or ref in s.fb_parts:
                return s.ic if s.ic in self.members else None
            if ref in s.out_caps:
                return s.inductor if s.inductor in self.members else None
        return None

    def _next(self, todo: list[str]) -> str:
        def key(ref: str) -> tuple[object, ...]:
            if ref in self.priority:
                return (*self.priority[ref], natural_key(ref))
            return (4, -self.shared_sig[ref], -self.shared_all[ref], natural_key(ref))

        ready = [r for r in todo if (h := self._host_needed(r)) is None or h in self.placed]
        return min(ready or todo, key=key)

    # -- geometry of one satellite --

    def _want(self, ref: str, pin_override: str | None = None) -> _Want | None:
        part = self.model.part(ref)
        if ref in self.decap_pin:
            ic, pin = self.decap_pin[ref]
            pin = pin_override or pin
            host = self.placed[ic]
            d = next(d for d in self.cls.decoupling if d.cap == ref)
            pads = tuple(q for q in host.pads if q.number == pin and q.net == d.supply_net)
            if pads:
                return _Want(ic, pads[:1], frozenset({d.supply_net}))
        kinds = self.cls.net_kind
        mine = {q.net for q in part.pads if q.net}
        for tier in ("signal", "supply", "ground"):
            nets = {
                n
                for n in mine
                if (kinds.get(n, "signal") in ("ground", "supply")) == (tier != "signal")
                and (tier == "signal" or kinds.get(n) == tier)
            }
            if not nets:
                continue
            best: tuple[tuple[object, ...], str] | None = None
            for i, (r, p) in enumerate(self.placed.items()):
                n = len(nets & {q.net for q in p.pads})
                if n:
                    k = (-n, len(p.pads), i)
                    if best is None or k < best[0]:
                        best = (k, r)
            if best is None:
                continue
            host = self.placed[best[1]]
            pads = tuple(
                sorted((q for q in host.pads if q.net in nets), key=lambda q: natural_key(q.number))
            )
            first = pads[0]
            near = tuple(
                q for q in pads if math.dist((q.x, q.y), (first.x, first.y)) <= NEAR_TARGET_MM
            )
            return _Want(best[1], near, frozenset(q.net for q in near))
        return None

    def _rotations(self, part: Part, want: _Want | None, side: str) -> list[float]:
        """Rotations to try: a 2-pad part with one connected pad only radial, connected
        pad facing the host; everything else all four."""
        rots = [0.0, 90.0, 180.0, 270.0]
        if want is None or len(part.pads) != 2:
            return rots
        own = [q for q in part.pads if q.net in want.own_nets]
        if len(own) != 1:
            return rots
        axis, sign = _SIDES[side]
        out = []
        for r in rots:
            q = moved_part(part, Move(part.ref, 0.0, 0.0, r, "top"))
            a, b = q.pads
            near, far = (a, b) if a.net in want.own_nets else (b, a)
            radial = abs((near.x, near.y)[axis] - (far.x, far.y)[axis]) > abs(
                (near.x, near.y)[1 - axis] - (far.x, far.y)[1 - axis]
            )
            facing = sign * ((near.x, near.y)[axis] - (far.x, far.y)[axis]) < 0
            if radial and facing:
                out.append(r)
        return out or rots

    def _gap_for(self, ref: str) -> float:
        """Decoupling caps and crystals (spec §9.3 distance rules) keep the minimum gap."""
        critical = ref in self.decap_pin or any(x.ref == ref for x in self.cls.crystals)
        return self.gap if critical else self.gap + SATELLITE_EXTRA_MM

    def _fit(
        self, part: Part, rot: float, host: Part, side: str, want: _Want | None
    ) -> tuple[float, Move]:
        """(cost, move) of the best free spot for `part` at `rot` on `side` of `host`.

        Connected satellites cost the octilinear distance of their connected pads to the
        target pads plus `ALIGN_WEIGHT` x the slide along the row away from the aligned
        spot; unconnected ones the slide plus `RADIAL_WEIGHT` x the step outwards."""
        g = self._gap_for(part.ref)
        axis, sign = _SIDES[side]
        local = moved_part(part, Move(part.ref, 0.0, 0.0, rot, "top"))
        sb = box_of(local)

        def frame(b: Box) -> tuple[float, float, float, float]:
            """(r0, r1, t0, t1) of box `b` in the side's frame (r outwards)."""
            lo, hi = (b[axis], b[axis + 2])
            r0, r1 = (lo, hi) if sign > 0 else (-hi, -lo)
            return (r0, r1, b[1 - axis], b[3 - axis])

        s_r0, s_r1, s_t0, s_t1 = frame(sb)
        _, h_r1, h_t0, h_t1 = frame(box_of(host))
        u0 = snap_up(h_r1 + g - s_r0)
        own: list[Pad] = []
        if want is not None:
            own = [q for q in local.pads if q.net in want.own_nets] or list(local.pads)
            tgt = _mean([(q.x, q.y) for q in want.target])
            mine = _mean([(q.x, q.y) for q in own])
            t_star = snap(tgt[1 - axis] - mine[1 - axis])
        else:
            t_star = snap((h_t0 + h_t1) / 2 - (s_t0 + s_t1) / 2)

        def at(u: float, t: float) -> tuple[float, float]:
            pos = [0.0, 0.0]
            pos[axis] = u * sign
            pos[1 - axis] = t
            return snap(pos[0]), snap(pos[1])

        def cost_of(u: float, t: float) -> float:
            if want is None:
                return abs(t - t_star) + RADIAL_WEIGHT * (u - u0)
            x, y = at(u, t)
            dist = sum(
                min(octi_dist((q.x + x, q.y + y), (p.x, p.y)) for p in want.target) for q in own
            ) / len(own)
            return dist + ALIGN_WEIGHT * abs(t - t_star)

        obstacles = [frame(box_of(p)) for p in self.placed.values()]
        us = sorted({u0, *(snap_up(o[1] + g - s_r0) for o in obstacles if o[1] + g - s_r0 > u0)})
        best: tuple[float, float, float] | None = None  # cost, u, t
        floor = cost_of(u0, t_star)
        for u in us:
            # every cost at this depth is at least the step outwards over the ideal spot
            if best is not None and floor + (u - u0) >= best[0]:
                break
            blocked = [
                (o[2] - g - s_t1, o[3] + g - s_t0)
                for o in obstacles
                if o[0] - g < u + s_r1 - _EPS and u + s_r0 < o[1] + g - _EPS
            ]
            cands = {t_star}
            for lo, hi in blocked:
                cands.add(snap_down(lo))
                cands.add(snap_up(hi))
            for t in sorted(cands):
                if any(lo + _EPS < t < hi - _EPS for lo, hi in blocked):
                    continue
                cost = cost_of(u, t)
                if best is None or cost < best[0] - _EPS:
                    best = (cost, u, t)
        assert best is not None  # the last u clears every obstacle radially
        cost, u, t = best
        x, y = at(u, t)
        return cost, Move(part.ref, x, y, rot, "top")

    def place(self, ref: str) -> None:
        if self.decap_round.get(ref, 0) > 0:
            # a bulk cap: the supply pin of its net with the most room
            ic, _ = self.decap_pin[ref]
            net = next(d.supply_net for d in self.cls.decoupling if d.cap == ref)
            pins = sorted({q.number for q in self.placed[ic].pads if q.net == net}, key=natural_key)
            options = [self._best(ref, self._want(ref, pin)) for pin in pins]
            self._commit(min(options, key=lambda o: o[0])[1])
            return
        self._commit(self._best(ref, self._want(ref))[1])

    def _best(self, ref: str, want: _Want | None) -> tuple[float, Move]:
        part = self.model.part(ref)
        sides: list[tuple[str, float]]
        if want is None:
            host = self.placed[self.anchor]
            sides = [(s, 0.0) for s in _SIDES]
        else:
            host = self.placed[want.host]
            first = _side_of(box_of(host), want.target)
            # the two neighbouring sides too (a pin at a corner), never the opposite one
            sides = [(first, 0.0)] + [
                (s, SIDE_PENALTY_MM) for s in _SIDES if _SIDES[s][0] != _SIDES[first][0]
            ]
        best: tuple[float, Move] | None = None
        for side, penalty in sides:
            for rot in self._rotations(part, want, side):
                cost, mv = self._fit(part, rot, host, side, want)
                cost += penalty
                if best is None or cost < best[0] - _EPS:
                    best = (cost, mv)
        assert best is not None
        return best

    def _commit(self, mv: Move) -> None:
        self.placed[mv.ref] = moved_part(self.model.part(mv.ref), mv)
        self.order.append(mv.ref)
        # nets shared with what is placed (the order of the satellites without a rule)
        for r in self.members:
            if r not in self.placed:
                self.shared_sig[r] += len(self.ns.signal[r] & self.ns.signal[mv.ref])
                self.shared_all[r] += len(self.ns.all[r] & self.ns.all[mv.ref])

    def run(self) -> tuple[Move, ...]:
        self._commit(Move(self.anchor, 0.0, 0.0, 0.0, "top"))
        todo = [r for r in self.members if r != self.anchor]
        while todo:
            ref = self._next(todo)
            todo.remove(ref)
            self.place(ref)
        return tuple(
            Move(r, self.placed[r].x, self.placed[r].y, self.placed[r].rot, "top")
            for r in self.order
        )


def _area(p: Part) -> float:
    b = box_of(p)
    return (b[2] - b[0]) * (b[3] - b[1])


def layout_cluster(
    model: BoardModel, cls: Classification, c: Cluster, *, gap: float
) -> tuple[Move, ...]:
    """Moves of `c`'s members relative to its anchor at `(0, 0)`, rotation 0, top side (the
    anchor's move first, then the satellites in placement order). No two courtyards are
    closer than `gap`."""
    return _Layout(model, cls, c, gap).run()
