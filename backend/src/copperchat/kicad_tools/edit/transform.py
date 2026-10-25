"""`sch_move` planning (E6 Task 1): move / rotate / mirror symbols, move labels, sheet
symbols and sheet pins, keeping every connection.

KiCad's API moves only the item: fields stay where they were and attached wires are not
dragged (spike 2A Q4). So the move is planned here and written in one commit:

- the symbol with its canonical transform (definition cleared by the engine), every
  field shifted with it (rotated about the origin on a transform change, Reference /
  Value laid out again as on placement);
- power symbols, labels and no-connects sitting directly on a moved pin travel with it;
- a stub chain hanging only on moved pins (no junction, branch, other pin or sheet pin;
  any length) travels whole with the power symbols, labels and no-connects at its end:
  by the same offset (#7/#8), or on a rotation / mirror turned with the part about its
  anchor (`orient.Frame`, v0.5.0 #20; a power symbol turns along, a label's text follows
  its stub). A carried item that would hit something refuses the whole call;
- labels turn about their anchor and sheet symbols about their centre, as KiCad's own
  rotate / mirror do it (`orient`, v0.5.0 #2);
- a label or power symbol moved without its part gets its stub chain laid anew from the
  pin that stays (its own stub is no obstacle);
- a wire ending on a moved point is dragged: its end follows the point; where that
  makes the segment diagonal it is replaced by an L (Delete + Create: KiCad may merge
  a leg with a collinear wire at push), the corner chosen by the router's checks;
- where a moved point has connections that cannot be dragged (a wire passing through
  it, a wire with something attached to its interior, another part's pin, a sheet pin),
  a bridge wire joins the new point to the old one;
- a sheet pin (v0.5.0 #1) is snapped onto the nearest edge of its sheet symbol
  (`sheets.snap_to_sheet_edge`); its side, and so its orientation, follows the edge. Along
  the same edge its own stub chains travel with it like a part's; on a side change they
  are dragged. The child sheet's hierarchical label is not touched.

Every new or changed segment is checked like a router route (no foreign pin, wire end,
label or body touched; corners not on wires) and the moved bodies like a placement
(overlap -> the nearest free position is suggested) — all before anything is written.
Verification: the netlist partition must stay identical (`keep_partition`) — or, with
`expect_net` (v0.5.0 #4), exactly one item-level group of the moved items joins that
net and nothing else changes (`verify.check_expect`).
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Create,
    Delete,
    ExpectNet,
    Logical,
    NetIntent,
    SheetRef,
    Stage,
    Update,
)
from copperchat.kicad_tools.edit.engine import copy_item
from copperchat.kicad_tools.edit.fields import FieldSpot
from copperchat.kicad_tools.edit.geometry import (
    Box,
    Point,
    apply,
    body_box,
    field_boxes,
    in_segment_interior,
    mm_point,
    orthogonal,
    pin_ends,
    same_point,
    symbol_fields,
    transform_matrix,
)
from copperchat.kicad_tools.edit.labels import (
    DIRS,
    Clearance,
    World,
    _v2,
    fmt,
    label_box,
    make_junction,
    make_wire,
)
from copperchat.kicad_tools.edit.orient import (
    DIR_ANGLE,
    IDENT,
    SPIN_DIR,
    Dir,
    Frame,
    TurnedSheet,
    compose,
    decompose,
    label_matrix,
    matrix_of,
    side_direction,
    translation,
    turn_label,
    turn_sheet,
)
from copperchat.kicad_tools.edit.place import Layout, clashes, lay_out_fields, overlap_error
from copperchat.kicad_tools.edit.router import problems_summary, route_problems
from copperchat.kicad_tools.edit.scene import (
    LabelInfo,
    Scene,
    SymbolInfo,
    WireInfo,
    sheet_api,
    short,
    symbol_info,
)
from copperchat.kicad_tools.edit.sheet_edges import (
    path_edge_problems,
    sheet_boxes,
    sheet_edge_problems,
)
from copperchat.kicad_tools.edit.sheets import side_attrs, snap_to_sheet_edge
from copperchat.kicad_tools.edit.targets import LABEL_KINDS, Target, bad, sheet_symbols

Mirror = Literal["", "x", "y"]
MOVE_KINDS = ("symbol", *LABEL_KINDS, "sheet", "sheet_pin")
ORIENT_KINDS = ("symbol", *LABEL_KINDS, "sheet")  # what rotates / mirrors (v0.5.0 #2)


side_of = {"left": "left", "right": "right", "up": "top", "down": "bottom"}  # pin dir -> side


def _key(p: Point) -> tuple[int, int]:
    return (round(p[0] * 100), round(p[1] * 100))


def _add(p: Point, d: Point) -> Point:
    return (round(p[0] + d[0], 4), round(p[1] + d[1], 4))


def _sub(a: Point, b: Point) -> Point:
    return (round(a[0] - b[0], 4), round(a[1] - b[1], 4))


@dataclass
class _Moving:
    """A connection point that moves: old -> new, with what it belongs to."""

    old: Point
    new: Point
    net: str | None
    own: str | None  # symbol uuid whose pin this is (route checks may touch its body)
    direction: str | None  # outward direction at the new point (pins)
    label: bool = False  # a moved label (its pin stays: connections there are fixed)
    sheet_pin: str | None = None  # a moved sheet pin's uuid (`own`: its sheet symbol)
    frame: Frame | None = None  # how its owner moves (carries stub chains along, #20)
    pin_uuid: str | None = None  # the sheet pin's uuid when its whole sheet symbol moves


@dataclass
class MovePlan:
    moved: list[dict[str, Any]] = field(default_factory=list)
    along: list[str] = field(default_factory=list)  # what travelled with the parts
    dragged: int = 0
    bridges: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class StubChain:
    """Wires that hang only on moved pins (#7/#8): from a pin through plain corners to a
    free end (nothing there, or only power symbols, labels, no-connects: `end_items`) or
    to another pin moving with it. `points`: the pin, the corners, the far end."""

    wires: list[str]
    end_items: list[str]
    points: list[Point] = field(default_factory=list)


def owned_stub_chains(
    scene: Scene,
    pin_points: Sequence[Point],
    moving: Collection[str] = (),
    *,
    sheet_pins: Collection[str] = (),
    tick: Callable[[], None] | None = None,
) -> list[StubChain]:
    """The stub chains of the pins at `pin_points`. `moving`: the symbols travelling by
    the same offset (a chain may run to one of their pins). `sheet_pins`: uuids of sheet
    pins whose point may start a chain like a symbol pin (a moved sheet pin, v0.5.0 #1).
    A chain is not owned when it meets a junction, a point joining three or more
    connections, a wire interior, anything attached to its own interior, a sheet pin or a
    pin of any other symbol."""
    moving = set(moving)
    starts = {_key(i.pin.at) for u, i in scene.sheet_pins.items() if u in sheet_pins}
    ends: dict[tuple[int, int], list[WireInfo]] = {}
    for w in scene.wires.values():
        for p in (w.start, w.end):
            ends.setdefault(_key(p), []).append(w)
    pins: dict[tuple[int, int], list[SymbolInfo]] = {}
    for s in scene.symbols.values():
        for pe in s.pins:
            pins.setdefault(_key(pe.at), []).append(s)
    blocked = {_key(j) for j in scene.junctions.values()}
    blocked |= {_key(i.pin.at) for i in scene.sheet_pins.values()}
    labels: dict[tuple[int, int], list[str]] = {}
    for lb in scene.labels.values():
        labels.setdefault(_key(lb.at), []).append(lb.uuid)
    for uid, nc_at in scene.no_connects.items():
        labels.setdefault(_key(nc_at), []).append(uid)
    # every connection point once: what may sit inside a chain wire (its own ends are no
    # interior points, so the wire itself needs no exclusion)
    points = [p.at for p in scene.all_pins()] + [i.pin.at for i in scene.sheet_pins.values()]
    points += [q for w in scene.wires.values() for q in (w.start, w.end)]
    points += [lb.at for lb in scene.labels.values()] + list(scene.junctions.values())
    points += list(scene.no_connects.values())

    def attached_inside(w: WireInfo) -> bool:
        return any(in_segment_interior(q, w.start, w.end) for q in points)

    def passes_through(q: Point) -> bool:
        return any(in_segment_interior(q, w.start, w.end) for w in scene.wires.values())

    def walk(p: Point) -> StubChain | None:
        k = _key(p)
        here = pins.get(k, [])
        if k in starts:  # a sheet pin: nothing else may sit on it
            sheet_pin_count = sum(1 for i in scene.sheet_pins.values() if _key(i.pin.at) == k)
            if len(ends.get(k, [])) != 1 or passes_through(p) or sheet_pin_count != 1:
                return None
            if any(not s.power for s in here):
                return None
        elif len(ends.get(k, [])) != 1 or k in blocked or passes_through(p):
            return None
        elif sum(1 for s in here if not s.power) != 1:
            return None  # another part's pin sits on it too (bridged as before)
        w, q, chain = ends[k][0], p, StubChain([], [], [p])
        while True:
            if tick is not None:
                tick()
            if w.uuid in chain.wires or attached_inside(w):
                return None
            chain.wires.append(w.uuid)
            q = w.end if same_point(q, w.start) else w.start
            k = _key(q)
            chain.points.append(q)
            if k in blocked or passes_through(q):
                return None
            here = pins.get(k, [])
            if any(not s.power and s.uuid not in moving for s in here):
                return None
            further = [x for x in ends.get(k, []) if x.uuid != w.uuid]
            if any(s.uuid in moving for s in here):  # another moving pin: the chain ends
                return chain if not further else None
            chain.end_items += [s.uuid for s in here]
            chain.end_items += labels.get(k, [])
            if not further:
                return chain
            if len(further) > 1:
                return None
            w = further[0]

    out: list[StubChain] = []
    seen: set[frozenset[str]] = set()
    for p in pin_points:
        if tick is not None:
            tick()  # the planning time limit (many pins on a big sheet)
        chain = walk(p)
        if chain is not None and frozenset(chain.wires) not in seen:
            seen.add(frozenset(chain.wires))
            out.append(chain)
    return out


def _matrix_inverse(m: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = m
    return (x1, x2, y1, y2)  # orthonormal: the transpose


def _compose(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """a after b."""
    a1, b1, c1, d1 = a
    a2, b2, c2, d2 = b
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2, c1 * a2 + d1 * c2, c1 * b2 + d1 * d2)


def _transform(rotation: int, mirror: Mirror) -> tuple[int, int, int, int]:
    from copperchat.kicad_tools.edit.geometry import canonical_transform

    return transform_matrix(*canonical_transform(rotation, mirror))


def moved_symbol(old: SymbolInfo, at: Point, rotation: int, mirror: Mirror) -> tuple[Any, bool]:
    """A copy of `old`'s item at `at` with the transform, fields moved along; whether
    the transform changed."""
    from copperchat.kicad_tools.edit.place import set_transform

    sym = copy_item(old.item)
    sym.position = _v2(at)
    changed = (rotation % 360, mirror) != (old.rotation, old.mirror)
    rel = (1, 0, 0, 1)
    if changed:
        set_transform(sym, rotation, mirror)
        before = _transform(old.rotation, old.mirror)  # type: ignore[arg-type]
        rel = _compose(_transform(rotation, mirror), _matrix_inverse(before))
    for f in symbol_fields(sym):
        offset = _sub(mm_point(f.text.position), old.at)
        if changed:
            offset = apply(rel, *offset)
        f.text.position = _v2(_add(at, offset))
    if changed:
        lay_out_fields(sym)  # Reference / Value as on a placement at this rotation
    return sym, changed


def _layout(sym: Any, rotation: int, mirror: Mirror) -> Layout:
    at = mm_point(sym.position)
    spots = tuple(
        FieldSpot(mm_point(f.text.position), float(f.text.attributes.angle))
        for f in (sym.reference_field, sym.value_field)
    )
    return Layout(
        item=sym,
        at=at,
        rotation=rotation,
        mirror=mirror,
        pins=pin_ends(sym),
        body=body_box(sym, margin=0.0),
        spots=spots,  # type: ignore[arg-type]
        fields=field_boxes(sym),
    )


def _interior_attached(scene: Scene, w: WireInfo, sheet_pin_points: list[Point]) -> bool:
    """Something connects to the wire's interior (a T-join, a pin, a label ...)."""
    pts = [p.at for p in scene.all_pins()] + sheet_pin_points
    pts += [q for o in scene.wires.values() if o.uuid != w.uuid for q in (o.start, o.end)]
    pts += [lb.at for lb in scene.labels.values()] + list(scene.junctions.values())
    pts += list(scene.no_connects.values())
    return any(in_segment_interior(p, w.start, w.end) for p in pts)


def _corner_paths(a: Point, b: Point, a_dir: str | None) -> list[list[Point]]:
    """Orthogonal paths a -> b: straight, else the two L shapes (the one leaving `a` in
    its outward direction first)."""
    if same_point(a, b):
        return []
    if orthogonal(a, b):
        return [[a, b]]
    paths = [[a, (b[0], a[1]), b], [a, (a[0], b[1]), b]]
    if a_dir is not None:
        d = DIRS[a_dir]
        paths.sort(key=lambda p: (p[1][0] - a[0]) * d[0] + (p[1][1] - a[1]) * d[1] <= 0)
    return paths


class _Check:
    """The scene after the move (moved parts at their new place, dragged wires gone)
    for route checks of every new segment."""

    def __init__(self, world: World, sheet: SheetRef, scene: Scene) -> None:
        self.world = world
        self.scene = Scene(
            sheet=scene.sheet,
            symbols=dict(scene.symbols),
            wires=dict(scene.wires),
            labels=dict(scene.labels),
            junctions=dict(scene.junctions),
            no_connects=dict(scene.no_connects),
            sheets=dict(scene.sheets),
            sheet_pins=dict(scene.sheet_pins),
        )
        self.nets = dict(world.nets(sheet))
        self.fake = itertools.count()
        self.also: set[str] = set()  # further nets a route may touch (expect_net)
        self._pin_nets: dict[str, str | None] = {}  # sheet pin uuid -> net (read once)

    def add_wire(self, a: Point, b: Point, net: str | None) -> None:
        uid = f"planned-move-{next(self.fake)}"
        self.scene.wires[uid] = WireInfo(uid, a, b)
        if net is not None:
            self.nets[uid] = net

    def problems(
        self,
        pts: list[Point],
        net: str | None,
        own: tuple[str | None, str | None],
        legacy_line: tuple[Point, Point] | None = None,
    ) -> list[str]:
        allowed = ({net} if net is not None else set()) | self.also
        # sheet pins of other nets (not at the path's ends) are foreign connection points:
        # one-pin stand-ins with a point body, as sch_connect's router sees them
        symbols = dict(self.scene.symbols)
        for info in self.scene.sheet_pins.values():
            if any(same_point(info.pin.at, e) for e in (pts[0], pts[-1])):
                continue
            if info.uuid not in self._pin_nets:
                self._pin_nets[info.uuid] = self.world.sheet_pin_net(info)
            if self._pin_nets[info.uuid] in allowed:
                continue
            uid = f"sheet-pin-{info.uuid}"
            pin = replace(info.pin, key=(self.scene.sheet.path, uid, info.pin.number))
            symbols[uid] = SymbolInfo(uid, info.pin.ref, "", "", 1, self.scene.sheet, pin.at,
                                      0, "", [pin], Box(*pin.at, *pin.at), [], False)  # fmt: skip
        scene = replace(self.scene, symbols=symbols)
        return route_problems(
            pts,
            scene,
            self.world.netlist,
            allowed,
            item_nets=self.nets,
            own_symbols=own,
            legacy_line=legacy_line,
        )


def plan_move(
    world: World,
    tool: str,
    targets: list[Target],
    to: Point | None,
    by: Point | None,
    rotation: int | None,
    mirror: Mirror | None,
    expect_net: str | None = None,
) -> tuple[ChangeSet, MovePlan]:
    """`expect_net` (v0.5.0 #4): the move is to join exactly one group of the moved items
    to that net (checked on the netlist, item-level); without it nothing may change."""
    sheets = {t.sheet.path for t in targets}
    if len(sheets) > 1:
        raise bad(
            f"{tool}: the items are on different sheets",
            ", ".join(sorted({t.sheet.human_path for t in targets})),
            "Move the items of one sheet per call.",
        )
    if to is not None and len(targets) > 1:
        raise bad(
            f"{tool}: 'to' needs exactly one item",
            f"{len(targets)} items were given ("
            + ", ".join(t.ref or t.uuid[:8] for t in targets[:6])
            + "; a reference names every unit of a part)",
            "Use 'by' to move several items, or pass one unit by its UUID.",
        )
    if (rotation is not None or mirror is not None) and any(t.kind == "sheet_pin" for t in targets):
        raise bad(
            f"{tool}: a sheet pin does not rotate or mirror",
            "the side defines a sheet pin's orientation",
            "Move it to another edge of its sheet symbol instead (to=[x, y] on that edge).",
        )
    moved_sheets = {t.uuid for t in targets if t.kind == "sheet"}
    if any(t.pin is not None and t.pin.sheet_uuid in moved_sheets for t in targets):
        raise bad(
            f"{tool}: a sheet and one of its own pins were both given",
            "moving the sheet symbol carries all its pins",
            "Move the sheet, or its pins, per call.",
        )
    turning = rotation is not None or mirror is not None
    if turning and (odd := [t for t in targets if t.kind not in ORIENT_KINDS]):
        raise bad(
            f"{tool}: rotate / mirror applies to symbols, labels and sheet symbols",
            f"{odd[0].kind.replace('_', ' ')} {odd[0].spec} has no orientation",
            "Drop rotation/mirror for it, or move it with to/by only.",
        )
    sheet = targets[0].sheet
    if expect_net is not None:
        known = set(world.netlist.nets) | {n for d in world.item_nets.values() for n in d.values()}
        if expect_net not in known:
            raise bad(
                f"{tool}: unknown expect_net {short(expect_net, 70)}",
                "no net of that name exists before the move",
                "Pass a net name as sch_get_net / sch_list_items report it (e.g. "
                "'/SDA'), or omit expect_net.",
            )
    scene = world.scene(sheet)
    nets = world.nets(sheet)
    plan = MovePlan()
    moving: list[_Moving] = []
    updates: list[Any] = []
    moved_ids: set[str] = set()
    new_infos: list[tuple[SymbolInfo, Any, int, Mirror, str]] = []
    turned = False  # some target's orientation changes
    turned_labels: dict[str, tuple[Point | None, Dir]] = {}  # label -> (its pin point, text dir)
    turned_sheets: dict[str, TurnedSheet] = {}  # moved sheet symbols: new box and pins
    sheet_pin_points = [i.pin.at for i in scene.sheet_pins.values()]
    pin_moves: dict[str, tuple[str, Point]] = {}  # sheet pin uuid -> (new side, new point)
    rigid_pins: set[str] = set()  # sheet pins moved along their edge
    api = sheet_api(world.h, sheet)
    cache: dict[str, dict[str, Any]] = {}
    notes_snap: list[str] = []

    def item_of(kind: str, uid: str) -> Any:
        """One read per item kind and call (`labels`, `no_connects`, `junctions` ...)."""
        if kind not in cache:
            cache[kind] = {access.kiid(i): i for i in getattr(api, f"get_{kind}")()}
        return cache[kind][uid]

    def delta_for(at: Point) -> Point:
        if to is not None:
            return _sub(to, at)
        return by or (0.0, 0.0)

    # -- the targets ---------------------------------------------------------------------
    for t in targets:
        world.check_time(tool)
        if t.kind == "symbol":
            assert t.symbol is not None
            old = t.symbol
            at = _add(old.at, delta_for(old.at))
            rot = old.rotation if rotation is None else rotation % 360
            mir: Mirror = old.mirror if mirror is None else mirror  # type: ignore[assignment]
            sym, changed = moved_symbol(old, at, rot, mir)
            rel = IDENT
            if changed:
                turned = True
                before = _transform(old.rotation, old.mirror)  # type: ignore[arg-type]
                rel = compose(_transform(rot, mir), _matrix_inverse(before))
            frame = Frame(old.at, at, rel)
            new_pins = {p.number: p for p in pin_ends(sym)}
            for pe in old.pins:
                q = new_pins.get(pe.number)
                if q is None:
                    continue
                moving.append(
                    _Moving(pe.at, q.at, world.netlist.net_of(pe.key), old.uuid, q.direction,
                            frame=frame)
                )  # fmt: skip
            updates.append(sym)
            moved_ids.add(old.uuid)
            new_infos.append((old, sym, rot, mir, t.spec))
            plan.moved.append(
                {
                    "kind": "symbol",
                    "ref": old.ref,
                    "uuid": old.uuid,
                    "from_mm": list(old.at),
                    "to_mm": list(at),
                    "rotation": rot,
                    "mirror": mir,
                }
            )
        elif t.kind in LABEL_KINDS:
            lb = scene.labels[t.uuid]
            at = _add(lb.at, delta_for(lb.at))
            item = item_of("labels", t.uuid)
            lm = label_matrix(lb.spin, rotation, mirror) if turning else IDENT
            before, in_place = copy_item(item), copy_item(item)
            turn_label(in_place, Frame(lb.at, lb.at, lm))  # the turn alone changes it?
            new_dir = turn_label(item, Frame(lb.at, at, lm))  # fields follow (#2)
            if lm != IDENT and in_place.proto != before.proto:  # text / fields turn: re-checked
                turned = True
                turned_labels[t.uuid] = (None, new_dir)
            updates.append(item)
            moved_ids.add(t.uuid)
            moving.append(_Moving(lb.at, at, nets.get(t.uuid), None, None, label=True))
            plan.moved.append(
                {
                    "kind": lb.kind,
                    "text": lb.text,
                    "uuid": t.uuid,
                    "from_mm": list(lb.at),
                    "to_mm": list(at),
                    **({"rotation": DIR_ANGLE[new_dir]} if turning else {}),
                }
            )
        elif t.kind == "sheet":
            sym = copy_item(sheet_symbols(world, sheet)[t.uuid])
            old_at = mm_point(sym.position)
            d = delta_for(old_at)
            sm = matrix_of(rotation, mirror) if turning else IDENT
            old_pins = {access.kiid(p): mm_point(p.position) for p in sym.pins}
            if sm != IDENT:  # about its centre, as KiCad's SCH_SHEET::Rotate / Mirror (#2)
                turned = True
                ts = turn_sheet(sym, sm, by or (0.0, 0.0), to=to)
            else:
                sym.position = _v2(_add(old_at, d))
                pins = []
                for pin in sym.pins:
                    pin.position = _v2(_add(mm_point(pin.position), d))
                    pin.text.position = _v2(_add(mm_point(pin.text.position), d))
                    pins.append(pin)
                sym.pins = pins
                for f in (sym.name_field, sym.filename_field):
                    f.text.position = _v2(_add(mm_point(f.text.position), d))
                old_box = scene.sheets[t.uuid]
                ts = TurnedSheet(
                    sym,
                    Box(old_box.x0 + d[0], old_box.y0 + d[1], old_box.x1 + d[0], old_box.y1 + d[1]),
                    Frame(old_at, _add(old_at, d)),
                    {
                        u: (_add(p, d), side_of.get(i.pin.direction, "left"))
                        for u, p in old_pins.items()
                        if (i := scene.sheet_pins.get(u)) is not None
                    },
                )
            turned_sheets[t.uuid] = ts
            for uid, p_old in old_pins.items():
                at_side = ts.pins.get(uid)
                if at_side is None:
                    continue
                moving.append(
                    _Moving(p_old, at_side[0], nets.get(uid), t.uuid, side_direction(at_side[1]),
                            frame=ts.frame, pin_uuid=uid)
                )  # fmt: skip
            updates.append(sym)
            moved_ids.add(t.uuid)
            new_at = mm_point(sym.position)
            plan.moved.append(
                {
                    "kind": "sheet",
                    "uuid": t.uuid,
                    "from_mm": list(old_at),
                    "to_mm": [round(new_at[0], 4), round(new_at[1], 4)],
                    **({"rotation": rotation or 0, "mirror": mirror or ""} if sm != IDENT else {}),
                }
            )
        elif t.kind == "sheet_pin":
            assert t.pin is not None
            info = t.pin
            box = scene.sheets[info.sheet_uuid]
            old_side = side_of[info.pin.direction]
            others_on_sheet = [
                (o.pin.name, *pin_moves.get(o.uuid, (side_of[o.pin.direction], o.pin.at)))
                for o in scene.sheet_pins.values()
                if o.sheet_uuid == info.sheet_uuid
                and o.uuid != info.uuid
                and (o.uuid in pin_moves or not any(x.uuid == o.uuid for x in targets))
            ]
            wanted = _add(info.pin.at, delta_for(info.pin.at))
            side, at = snap_to_sheet_edge(
                info.pin.ref, box, wanted, others_on_sheet, current=old_side
            )
            pin_moves[info.uuid] = (side, at)
            if side == old_side:
                rigid_pins.add(info.uuid)
            if not same_point(at, wanted):
                notes_snap.append(
                    f"sheet pin {info.pin.ref}.{info.pin.name}: {fmt(wanted)} snapped onto "
                    f"the {side} edge at {fmt(at)}"
                )
            direction = {"left": "left", "right": "right", "top": "up", "bottom": "down"}[side]
            moving.append(
                _Moving(
                    info.pin.at, at, nets.get(info.uuid), info.sheet_uuid, direction,
                    sheet_pin=info.uuid,
                    frame=translation(_sub(at, info.pin.at)) if side == old_side else None,
                )
            )  # fmt: skip
            plan.moved.append(
                {
                    "kind": "sheet_pin",
                    "sheet": info.pin.ref,
                    "pin": info.pin.name,
                    "uuid": info.uuid,
                    "from_mm": list(info.pin.at),
                    "to_mm": list(at),
                    "side": side,
                    **({"from_side": old_side} if side != old_side else {}),
                }
            )
        else:
            raise bad(
                f"{tool} cannot move {t.kind.replace('_', ' ')} {t.spec}",
                "sch_move moves symbols, labels, sheet symbols and sheet pins; wires follow them",
                "Use sch_reroute to change a wire's course.",
            )
    if not turned and all(x["from_mm"] == x["to_mm"] for x in plan.moved):
        raise bad(
            f"{tool}: nothing moves",
            "the items already are at that position and orientation",
            "Pass another 'to' / 'by', or a rotation / mirror.",
        )
    if pin_moves:  # one updated copy per sheet symbol whose pins move
        updates += _moved_sheet_pins(world, sheet, scene, pin_moves)

    # -- items that travel with moved pins -----------------------------------------------
    along_ids: set[str] = set()
    carried_by: dict[str, Point] = {}  # carried item uuid -> its pin's point (named in errors)
    # symbol and sheet pins
    pin_points = [m for m in moving if not m.label and (m.own in moved_ids or m.sheet_pin)]

    def frame_of(m: _Moving) -> Frame:
        return m.frame if m.frame is not None else translation(_sub(m.new, m.old))

    def carry_power(s: SymbolInfo, f: Frame, pin: Point) -> None:
        """A power symbol travels with frame `f`: it turns with it, so it keeps pointing
        away from its pin (#20)."""
        rot, mir = decompose(compose(f.m, _transform(s.rotation, s.mirror)))  # type: ignore[arg-type]
        psym, _ = moved_symbol(s, f(s.at), rot, mir)
        updates.append(psym)
        along_ids.add(s.uuid)
        carried_by[s.uuid] = pin
        new_infos.append((s, psym, rot, mir, s.ref))
        plan.along.append(f"power symbol {s.ref}")

    def carry_label(uid: str, f: Frame, pin: Point, direction: str | None = None) -> None:
        item = item_of("labels", uid)
        old_dir = SPIN_DIR.get(scene.labels[uid].spin, (1, 0))
        # a label on the pin reads away from it; fields laid out for that direction
        new_dir = turn_label(item, f, DIRS[direction] if direction is not None else None)
        if new_dir != old_dir or not f.rigid:  # its text is checked at the new place
            turned_labels[uid] = (pin, new_dir)
        updates.append(item)
        along_ids.add(uid)
        carried_by[uid] = pin
        plan.along.append(f"label {scene.labels[uid].text}")

    for m in pin_points:
        f = frame_of(m)
        world.check_time(tool)
        pin = m.old
        for lb in scene.labels.values():
            if lb.uuid in moved_ids or lb.uuid in along_ids or not same_point(lb.at, m.old):
                continue
            carry_label(lb.uuid, f, pin, m.direction)
        for s in scene.symbols.values():
            if not s.power or s.uuid in moved_ids or s.uuid in along_ids:
                continue
            if any(same_point(p.at, m.old) for p in s.pins):
                carry_power(s, f, pin)
        for uid, nc_at in scene.no_connects.items():
            if uid not in along_ids and same_point(nc_at, m.old):
                nc = item_of("no_connects", uid)
                nc.position = _v2(m.new)
                updates.append(nc)
                along_ids.add(uid)
                carried_by[uid] = pin
                plan.along.append("no-connect marker")
    # stub chains hanging only on the moved pins travel whole, shape kept (#7/#8), turned
    # with the part on a rotation / mirror (#20); not a sheet pin changing sides (dragged)
    carried: dict[str, Frame] = {}  # wire uuid -> how it moves
    starts = [m for m in pin_points if m.frame is not None]

    def moving_at(p: Point, among: list[_Moving]) -> _Moving | None:
        return next((m for m in among if same_point(m.old, p)), None)

    chain_starts: dict[int, _Moving] = {}
    chains = []
    for c in owned_stub_chains(
        scene,
        [m.old for m in starts],
        moved_ids,
        sheet_pins={u for m in starts if (u := m.sheet_pin or m.pin_uuid) is not None},
        tick=lambda: world.check_time(tool),
    ):
        start = moving_at(c.points[0], starts)
        assert start is not None
        if any(u in moved_ids and u in scene.symbols for u in c.end_items):
            continue  # a moved part sits at its end
        far = moving_at(c.points[-1], moving)  # its far end moves another way: dragged
        if far is not None and not same_point(far.new, frame_of(start)(c.points[-1])):
            continue
        chain_starts[id(c)] = start
        chains.append(c)
    for chain in chains:
        start = chain_starts[id(chain)]
        f = frame_of(start)
        pin = start.old
        for uid in chain.wires:
            carried[uid] = f
            carried_by[uid] = pin
        for uid in chain.end_items:
            if uid in moved_ids or uid in along_ids:
                continue
            if uid in scene.symbols:
                carry_power(scene.symbols[uid], f, pin)
            elif uid in scene.labels:
                carry_label(uid, f, pin)
            else:
                along_ids.add(uid)
                carried_by[uid] = pin
                nc = item_of("no_connects", uid)
                nc.position = _v2(f(scene.no_connects[uid]))
                updates.append(nc)
                plan.along.append("no-connect marker")
    if carried:
        plan.along.append("stub wires")
    # a label / power symbol moved without its part: its stub is laid anew from the
    # part's pin to the new place (no own-stub clash, no L left on the old course)
    restubs: list[tuple[StubChain, _Moving]] = []
    for m in moving:
        lone = m.label or (m.own in scene.symbols and scene.symbols[m.own].power)
        if not lone or same_point(m.old, m.new):
            continue
        world.check_time(tool)
        stub = _own_stub(scene, m.old, moved_ids)
        if stub is None or any(u in carried for u in stub.wires):
            continue
        if any(stub.wires == other.wires for other, _m in restubs):
            continue  # a label and a power symbol on one stub end: laid once
        restubs.append((stub, m))
    restubbed = {u for stub, _m in restubs for u in stub.wires}
    landing = [m for m in moving if m.sheet_pin or m.pin_uuid]
    if landing:  # what leaves in this call: it moves (pins of moved sheets too),
        # travels along, is carried, or is a wire dragged off a moving pin's or label's old point
        vacated = [m.old for m in moving if not same_point(m.old, m.new)]
        gone_from = {_key(p) for p in vacated}  # keyed like the drag pass's `mapping`
        dragged = set()
        for w in scene.wires.values():
            if _key(w.start) in gone_from or _key(w.end) in gone_from:
                world.check_time(tool)
                if not _interior_attached(scene, w, sheet_pin_points):  # those stay put
                    dragged.add(w.uuid)
        leaving = {x["uuid"] for x in plan.moved} | along_ids | set(carried) | restubbed
        leaving |= {u for u, i in scene.sheet_pins.items() if i.sheet_uuid in moved_ids}
        _refuse_pin_landing(
            world, tool, scene, nets, landing, leaving, dragged, vacated, expect_net,
        )  # fmt: skip

    # -- the scene after the move --------------------------------------------------------
    check = _Check(world, sheet, scene)
    if expect_net is not None:
        check.also = {expect_net}  # dragged wires may reach the declared net (#4)
    others = Scene(
        sheet=scene.sheet,
        symbols={},
        wires=dict(scene.wires),
        labels={},
        junctions=dict(scene.junctions),
        no_connects={},
        sheets={},
        sheet_pins={},
    )
    gone = moved_ids | along_ids
    others.symbols = {u: s for u, s in scene.symbols.items() if u not in gone}
    others.labels = {u: lb for u, lb in scene.labels.items() if u not in gone}
    others.no_connects = {u: p for u, p in scene.no_connects.items() if u not in gone}
    others.sheets = {u: b for u, b in scene.sheets.items() if u not in gone}
    for old, sym, *_rest in new_infos:
        check.scene.symbols[old.uuid] = symbol_info(sym, sheet)
    for item in updates:
        uid = access.kiid(item)
        if uid in check.scene.labels:
            lb = check.scene.labels[uid]
            check.scene.labels[uid] = LabelInfo(
                uid, lb.text, mm_point(item.position), lb.kind, int(item.spin_style)
            )
        if uid in check.scene.no_connects:
            check.scene.no_connects[uid] = mm_point(item.position)
    for uid, ts in turned_sheets.items():
        check.scene.sheets[uid] = ts.box
        for pin_uid, (at, side) in ts.pins.items():
            pinfo = scene.sheet_pins.get(pin_uid)
            if pinfo is not None:
                spin = replace(pinfo.pin, at=at, direction=side_direction(side))  # type: ignore[arg-type]
                check.scene.sheet_pins[pin_uid] = replace(pinfo, pin=spin)
    for m in moving:
        if m.sheet_pin is not None:
            info = scene.sheet_pins[m.sheet_pin]
            pin = replace(info.pin, at=m.new, direction=m.direction)  # type: ignore[arg-type]
            check.scene.sheet_pins[m.sheet_pin] = replace(info, pin=pin)
    new_pin_points = [i.pin.at for i in check.scene.sheet_pins.values()]

    moved_points = [m.old for m in moving if not same_point(m.old, m.new)]
    for w in scene.wires.values():  # attached wires follow (checked as routes below)
        if (
            w.uuid in carried
            or w.uuid in restubbed
            or any(same_point(w.start, q) or same_point(w.end, q) for q in moved_points)
        ):
            others.wires.pop(w.uuid, None)

    # -- bodies --------------------------------------------------------------------------
    for old, sym, rot, mir, _spec in new_infos:
        lay = _layout(sym, rot, mir)
        problems = [
            p
            for p in clashes(others, lay)
            if not _stays(p, lay, old) and not _lands_on(p, lay, others, world, nets, expect_net)
        ]
        if problems and old.uuid in carried_by:  # carried with a part: nothing written (O8)
            raise _carried_error(
                tool, _pin_name(scene, carried_by[old.uuid]), f"power symbol {old.ref}",
                old.uuid, lay.at, problems, _obstacles_in(others, [lay.body]),
            )  # fmt: skip
        if problems:
            raise overlap_error(others, lay, problems, f"{tool} {old.ref}")
    room_wires = dict(others.wires)  # staying wires and carried ones at their new place
    room_wires.update({u: WireInfo(u, f(scene.wires[u].start), f(scene.wires[u].end))
                       for u, f in carried.items()})  # fmt: skip
    for uid, (pin_name, d) in turned_labels.items():  # a turned label's text (#2, #20)
        world.check_time(tool)
        lb = check.scene.labels[uid]
        box = label_box(lb.at, lb.text, d, lb.kind)
        room = replace(others, symbols={  # a power symbol on the same stub end is its own
            u: x for u, x in check.scene.symbols.items()
            if not (x.power and any(same_point(p.at, lb.at) for p in x.pins))
        }, sheets=check.scene.sheets,
                       wires=room_wires, labels={
            u: x for u, x in check.scene.labels.items() if u != uid
        })  # fmt: skip
        if not Clearance(room).clear(box, ignore=[lb.at]):
            blockers = _obstacles_in(room, [box], ignore=[lb.at])
            if pin_name is not None:
                raise _carried_error(
                    tool, _pin_name(scene, pin_name), f"label {lb.text}", uid, lb.at,
                    ["have its text cover something"], blockers,
                )  # fmt: skip
            raise StructuredToolError(
                f"{tool} refused: label {lb.text} turned at {fmt(lb.at)} would cover something",
                "its text would overlap " + (", ".join(blockers[:4]) or "a wire or another item"),
                "Nothing was written. Pick another rotation, or move the label to free room.",
                code="overlap",
                details={"obstacles": blockers[:12]},
            )
    for t in targets:
        if t.kind == "sheet":
            box = check.scene.sheets[t.uuid]
            hit = [
                n for u, b in others.sheets.items() if u != t.uuid and b.overlaps(box) for n in [u]
            ]
            hit += [s.ref for s in others.symbols.values() if s.body.grow(2.0).overlaps(box)]
            hit += [  # what moves in the same call, at its new place
                f"sheet {u}" for u in turned_sheets
                if u != t.uuid and check.scene.sheets[u].overlaps(box)
            ]  # fmt: skip
            hit += [
                check.scene.symbols[u].ref for u in moved_ids | along_ids
                if u in check.scene.symbols and check.scene.symbols[u].body.overlaps(box)
            ]  # fmt: skip
            if hit:
                raise StructuredToolError(
                    f"{tool}: the sheet at its new position would overlap",
                    "it overlaps " + ", ".join(hit[:6]),
                    "Pick another position with free room for the whole sheet box.",
                    code="overlap",
                )
            if t.uuid in turned_sheets:
                _refuse_sheet_on_wires(tool, scene, others, t.uuid, box, new_pin_points)

    # -- wires ---------------------------------------------------------------------------
    mapping = {_key(m.old): m for m in moving if not same_point(m.old, m.new)}
    creates: list[Any] = []
    deletes: list[str] = []
    wire_updates: dict[str, tuple[Point, Point]] = {}
    fixed_points: set[tuple[int, int]] = set()
    edge_sheets = sheet_boxes(scene)
    for uid in [*carried, *restubbed]:  # laid at their new place after the drags
        del check.scene.wires[uid]
    for w in list(scene.wires.values()):
        world.check_time(tool)
        if w.uuid in carried or w.uuid in restubbed:
            continue
        ms, me = mapping.get(_key(w.start)), mapping.get(_key(w.end))
        if ms is None and me is None:
            continue
        if _interior_attached(scene, w, sheet_pin_points):
            for end in (ms, me):
                if end is not None:
                    fixed_points.add(_key(end.old))
            continue
        s_new = ms.new if ms else w.start
        e_new = me.new if me else w.end
        del check.scene.wires[w.uuid]
        others.wires.pop(w.uuid, None)
        net = nets.get(w.uuid)
        plan.dragged += 1
        if same_point(s_new, e_new):
            deletes.append(w.uuid)
            continue
        if orthogonal(s_new, e_new):
            paths = [[s_new, e_new]]
        else:
            moved_end, fixed_end, end = (s_new, e_new, ms) if ms else (e_new, s_new, me)
            assert end is not None
            paths = _corner_paths(moved_end, fixed_end, end.direction)
        # a wire that already broke the sheet-edge rule may keep doing so along its own
        # line (new legs are checked strictly): warn (#9)
        legacy = sheet_edge_problems((w.start, w.end), edge_sheets, sheet_pins=sheet_pin_points)
        chosen = None
        found: list[str] = []
        for pts in paths:
            found = check.problems(
                pts,
                net,
                (_owner(check.scene, pts[0]), _owner(check.scene, pts[-1])),
                legacy_line=(w.start, w.end) if legacy else None,
            )
            if not found:
                chosen = pts
                break
        if chosen is None:
            raise _short(tool, w, found)
        still = path_edge_problems(chosen, edge_sheets, sheet_pins=new_pin_points)
        if legacy and still:
            plan.warnings.append(
                f"dragged wire {w.uuid} still breaks the sheet-edge rule: {still[0]}; "
                "consider sch_reroute"
            )
        if len(chosen) == 2:
            wire_updates[w.uuid] = (chosen[0], chosen[1])
            check.add_wire(chosen[0], chosen[1], net)
        else:
            # Replaced by a new L (Delete + Create): KiCad merges a leg collinear with a
            # wire at the fixed end at push, which an updated wire would not survive
            # (its UUID vanishes); a created one merging is a recorded side effect.
            deletes.append(w.uuid)
            for a, b in itertools.pairwise(chosen):
                creates.append(make_wire(a, b))
                check.add_wire(a, b, net)
    # points with connections that cannot be dragged: bridge new -> old
    for m in moving:
        world.check_time(tool)
        if same_point(m.old, m.new):
            continue
        fixed = _key(m.old) in fixed_points
        fixed = fixed or any(
            in_segment_interior(m.old, w.start, w.end) for w in scene.wires.values()
        )
        for s in scene.symbols.values():
            if s.uuid in moved_ids or s.uuid in along_ids:
                continue
            if any(same_point(p.at, m.old) for p in s.pins):
                fixed = True
        if any(  # a sheet pin that stays where it is
            same_point(i.pin.at, m.old)
            and i.sheet_uuid not in moved_ids
            and i.uuid not in pin_moves
            for i in scene.sheet_pins.values()
        ):
            fixed = True
        if m.label:  # a moved label leaves the wire end / pin it sat on: bridged unless dragged
            fixed = fixed or any(
                same_point(p.at, m.old) for s in scene.symbols.values() for p in s.pins
            )
        if not fixed:
            continue
        paths = _corner_paths(m.new, m.old, m.direction)
        chosen = None
        found = []
        for pts in paths:
            found = check.problems(pts, m.net, (m.own, _owner(check.scene, pts[-1])))
            if not found:
                chosen = pts
                break
        if chosen is None:
            raise StructuredToolError(
                f"{tool} refused: the connection at {fmt(m.old)} cannot follow",
                "a wire from the new position back to it would " + problems_summary(found),
                "Move the part elsewhere (sch_find_space) or reconnect it afterwards.",
                code="overlap",
                details={"problems": found[:12]},
            )
        for a, b in itertools.pairwise(chosen):
            creates.append(make_wire(a, b))
            check.add_wire(a, b, m.net)
        plan.bridges += 1
    # carried chains and laid-anew stubs: checked like routes at their new place, after
    # every dragged wire and bridge is in the check scene
    for chain in chains:
        world.check_time(tool)
        f = carried[chain.wires[0]]
        net = nets.get(chain.wires[0])
        own_ncs = {
            u: check.scene.no_connects.pop(u)
            for u in chain.end_items
            if u in check.scene.no_connects
        }
        old_segs = list(itertools.pairwise(chain.points))
        segs = [(f(a), f(b)) for a, b in old_segs]
        # a wire that already broke the sheet-edge rule may keep doing so on its own line
        # (as a dragged wire does): such a chain is checked segment by segment, warned (#9);
        # a turned one is on a new line, checked strictly
        broke = [
            f.rigid and bool(sheet_edge_problems(seg, edge_sheets, sheet_pins=sheet_pin_points))
            for seg in old_segs
        ]
        pts = [f(p) for p in chain.points]
        checks: list[tuple[list[Point], tuple[Point, Point] | None, str | None]] = [
            (pts, None, None)
        ]
        if any(broke):
            checks = [
                ([a, b], (a, b) if was_bad else None, uid)
                for uid, (a, b), was_bad in zip(chain.wires, segs, broke, strict=True)
            ]
        for path, line, seg_uid in checks:
            trouble = check.problems(
                path,
                net,
                (_owner(check.scene, path[0]), _owner(check.scene, path[-1])),
                legacy_line=line,
            )
            if trouble:
                wire = seg_uid or chain.wires[0]
                raise _carried_error(
                    tool, _pin_name(scene, carried_by[wire]), "stub wire", wire, path[0], trouble,
                    _obstacles_on(check.scene, path, set(chain.wires) | set(chain.end_items)),
                )  # fmt: skip
            if seg_uid is not None:  # the next segment joins this one
                check.scene.wires[seg_uid] = WireInfo(seg_uid, path[0], path[1])
        for uid, was_bad in zip(chain.wires, broke, strict=True):
            w = scene.wires[uid]  # its own direction kept
            a, b = f(w.start), f(w.end)
            check.scene.wires[uid] = WireInfo(uid, a, b)
            wire_updates[uid] = (a, b)
            if was_bad and (still := sheet_edge_problems((a, b), edge_sheets,
                                                     sheet_pins=new_pin_points)):  # fmt: skip
                plan.warnings.append(
                    f"carried wire {uid} still breaks the sheet-edge rule: {still[0]}; "
                    "consider sch_reroute"
                )
        check.scene.no_connects.update(own_ncs)  # its own markers travel with it
    for stub, m in restubs:
        world.check_time(tool)
        pin = stub.points[0]
        net = nets.get(stub.wires[0])
        plan.dragged += len(stub.wires)
        first = scene.wires[stub.wires[0]]  # the wire at the pin: its line may stay (#9)
        legacy = sheet_edge_problems(
            (first.start, first.end), edge_sheets, sheet_pins=sheet_pin_points
        )
        paths = _corner_paths(pin, m.new, _pin_direction(scene, pin))
        chosen = None
        trouble = []
        for pts in paths:
            trouble = check.problems(
                pts,
                net,
                (_owner(check.scene, pts[0]), _owner(check.scene, pts[-1])),
                legacy_line=(first.start, first.end) if legacy else None,
            )
            if not trouble:
                chosen = pts
                break
        if paths and chosen is None:
            raise StructuredToolError(
                f"{tool} refused: the stub from {fmt(pin)} cannot follow to {fmt(m.new)}",
                "a wire there would " + problems_summary(trouble),
                "Pick another position, or reconnect it afterwards (sch_connect).",
                code="overlap",
                details={"problems": trouble[:12]},
            )
        if (
            chosen is not None
            and legacy
            and (still := path_edge_problems(chosen, edge_sheets, sheet_pins=new_pin_points))
        ):
            plan.warnings.append(
                f"stub wire {first.uuid} still breaks the sheet-edge rule: {still[0]}; "
                "consider sch_reroute"
            )
        if chosen is not None and len(chosen) == 2 and len(stub.wires) == 1:
            wire_updates[stub.wires[0]] = (chosen[0], chosen[1])
            check.scene.wires[stub.wires[0]] = WireInfo(stub.wires[0], chosen[0], chosen[1])
            continue
        deletes += stub.wires
        for a, b in itertools.pairwise(chosen or []):
            creates.append(make_wire(a, b))
            check.add_wire(a, b, net)
    # junctions: one where 3+ connections meet at a new point (KiCad drops the others)
    junction_points: list[Point] = []
    for item in creates:
        for end_at in (mm_point(item.start), mm_point(item.end)):
            if any(same_point(end_at, jp) for jp in junction_points):
                continue
            if _connections(check.scene, end_at) >= 3 and not any(
                same_point(end_at, j) for j in check.scene.junctions.values()
            ):
                junction_points.append(end_at)
    for uid, j_at in scene.junctions.items():
        jm = mapping.get(_key(j_at))
        if jm is None or _key(j_at) in fixed_points or _connections(check.scene, jm.new) < 3:
            continue
        j = item_of("junctions", uid)
        j.position = _v2(jm.new)
        updates.append(j)
        check.scene.junctions[uid] = jm.new
    creates += [make_junction(jp) for jp in junction_points]

    # -- the change set ------------------------------------------------------------------
    lines = {access.kiid(i): i for i in api.get_lines()} if wire_updates else {}
    for uid, (a, b) in wire_updates.items():
        line = lines[uid]
        line.start, line.end = _v2(a), _v2(b)
        updates.append(line)
    ops: list[Any] = [Update(updates, sheet=sheet)]
    if creates:
        ops.append(Create(creates, sheet=sheet))
    if deletes:
        ops.append(Delete(deletes, sheet=sheet))
    # one per part: (sheet, reference) for annotated parts (all units), the UUID for 'R?'
    parts = {
        t.uuid if not t.ref or t.ref.endswith("?") else (t.sheet.path, t.ref)
        for t in targets
        if t.kind == "symbol"
    }
    logical = len(parts) + sum(1 for t in targets if t.kind != "symbol")
    notes: list[str] = list(notes_snap)
    if any("from_side" in x for x in plan.moved):
        notes.append(
            "a sheet pin changed sides: its orientation follows the new edge; its wires were "
            "dragged (the child sheet's hierarchical label is unchanged)"
        )
    if plan.along:
        notes.append("moved along with the parts: " + ", ".join(sorted(set(plan.along))))
    if plan.bridges:
        notes.append(
            f"{plan.bridges} connection(s) could not be dragged (a wire passing through, a "
            "T-joined wire or another pin): a wire now joins the new position to the old one"
        )
    others_inst = [r.human_path for r in world.instances(sheet) if r.path != sheet.path]
    if others_inst:
        notes.append(
            f"{sheet.file} is used by several sheet instances: the move shows in "
            + ", ".join([sheet.human_path, *others_inst])
        )
    cs = ChangeSet(
        tool=tool,
        sheet=sheet,
        stages=[Stage("move", lambda _inp: list(ops))],
        pre_images={},
        intent=NetIntent(
            keep_partition=expect_net is None,
            expect=None
            if expect_net is None
            else ExpectNet(expect_net, _moved_keys(scene, targets)),
        ),
        logical=Logical(logical),
        summary={
            "moved": plan.moved,
            "dragged_wires": plan.dragged,
            **({"carried_wires": len(carried)} if carried else {}),
            "bridges": plan.bridges,
            **({"warnings": plan.warnings} if plan.warnings else {}),
        },
        notes=notes,
    )
    return cs, plan


def _moved_sheet_pins(
    world: World, sheet: SheetRef, scene: Scene, moves: dict[str, tuple[str, Point]]
) -> list[Any]:
    """Copies of the sheet symbols whose pins move (`moves`: pin uuid -> side, point). A
    pin keeps its UUID; on a new side it gets that side's spin style (`side_attrs`)."""
    out = []
    for sheet_uuid in dict.fromkeys(scene.sheet_pins[u].sheet_uuid for u in moves):
        sym = copy_item(sheet_symbols(world, sheet)[sheet_uuid])
        pins = []
        for pin in sym.pins:
            uid = access.kiid(pin)
            if uid in moves:
                side, at = moves[uid]
                old = mm_point(pin.position)
                pin.position = _v2(at)
                if side == _SIDE_NAMES.get(int(pin.side)):
                    pin.text.position = _v2(_add(mm_point(pin.text.position), _sub(at, old)))
                else:
                    pin.text.position = _v2(at)
                    pin.side, pin.spin_style = side_attrs(side)
            pins.append(pin)
        sym.pins = pins
        out.append(sym)
    return out


_SIDE_NAMES = {1: "left", 2: "right", 3: "top", 4: "bottom"}  # SheetSide


def _refuse_pin_landing(
    world: World,
    tool: str,
    scene: Scene,
    nets: dict[str, str],
    moving: list[_Moving],
    moved: set[str],
    dragged: set[str],
    vacated: list[Point],
    expect_net: str | None = None,
) -> None:
    """A moved sheet pin must not land on an item of another net (it would join it).
    What leaves that spot in the same call does not count: items in `moved` (by uuid)
    and the ends of `dragged` wires at `vacated`, the old points of moving pins (their
    other ends and interiors stay)."""
    for m in moving:
        info = scene.sheet_pins[m.sheet_pin or m.pin_uuid or ""]
        leaves = any(same_point(m.new, q) for q in vacated)
        for i in scene.items_at(m.new):
            drags = leaves and i["uuid"] in dragged and not i.get("where")
            if i["uuid"] in moved or drags:
                continue
            if i["kind"] == "pin":
                net = world.netlist.net_of((scene.sheet.path, i["uuid"], i["pin"]))
                what = f"pin {i.get('ref')}.{i['pin']}"
            elif i["kind"] == "sheet_pin":
                other = scene.sheet_pins[i["uuid"]]
                net = world.sheet_pin_net(other)
                what = f"sheet pin {other.pin.label}"
            else:
                net = nets.get(i["uuid"])
                what = i["kind"].replace("_", " ")
            if net is None or net not in {m.net, expect_net}:
                raise StructuredToolError(
                    f"{tool} refused: sheet pin {info.pin.label} would land on {what}",
                    f"at {fmt(m.new)} sits {what} ({net or 'unconnected'}); the pin would join it",
                    "Pick a free spot on the edge.",
                    code="overlap",
                )


def _own_stub(scene: Scene, p: Point, moved: set[str]) -> StubChain | None:
    """The stub chain a moved label / power symbol at `p` hangs on, seen from the pin of
    the part that stays; None unless everything at the chain's end moves."""

    def wires_at(q: Point) -> list[WireInfo]:
        return [w for w in scene.wires.values() if same_point(w.start, q) or same_point(w.end, q)]

    first = wires_at(p)
    if len(first) != 1 or any(  # something that stays sits there too
        i.get("where") or (i["kind"] != "wire" and i["uuid"] not in moved)
        for i in scene.items_at(p)
    ):
        return None
    w, q, seen = first[0], p, set()
    while w.uuid not in seen:  # along plain corners to the chain's other end
        seen.add(w.uuid)
        q = w.end if same_point(q, w.start) else w.start
        further = [x for x in wires_at(q) if x.uuid != w.uuid]
        if len(further) != 1 or any(i["kind"] != "wire" for i in scene.items_at(q)):
            break
        w = further[0]
    if any(
        s.uuid in moved for s in scene.symbols.values() for pe in s.pins if same_point(pe.at, q)
    ):
        return None  # the part moves too
    for chain in owned_stub_chains(scene, [q], moved):
        if same_point(chain.points[-1], p) and set(chain.end_items) <= moved:
            return chain
    return None


def _pin_direction(scene: Scene, p: Point) -> str | None:
    for pe in scene.all_pins():
        if same_point(pe.at, p) and pe.direction is not None:
            return str(pe.direction)
    return None


def _owner(scene: Scene, p: Point) -> str | None:
    """The symbol (or sheet symbol) whose pin is at `p`: a route may touch its body."""
    for s in scene.symbols.values():
        if any(same_point(pin.at, p) for pin in s.pins):
            return s.uuid
    for info in scene.sheet_pins.values():
        if same_point(info.pin.at, p):
            return info.sheet_uuid
    return None


def _stays(problem: str, lay: Layout, old: SymbolInfo) -> bool:
    """A pin that did not move lands on its own connections: not a clash."""
    for pin in lay.pins:
        if problem.startswith(f"pin {pin.number} at ") and any(
            same_point(pin.at, p.at) for p in old.pins
        ):
            return True
    return False


def _connections(scene: Scene, p: Point) -> int:
    n = 0
    for w in scene.wires.values():
        if same_point(w.start, p) or same_point(w.end, p):
            n += 1
        elif in_segment_interior(p, w.start, w.end):
            n += 2
    n += sum(1 for pin in scene.all_pins() if same_point(pin.at, p))
    n += sum(1 for i in scene.sheet_pins.values() if same_point(i.pin.at, p))
    return n


def _short(tool: str, w: WireInfo, problems: list[str]) -> StructuredToolError:
    return StructuredToolError(
        f"{tool} refused: dragging the wire {fmt(w.start)}-{fmt(w.end)} would change connections",
        problems_summary(problems),
        "Move the part to a position where its wires can follow cleanly (e.g. keep it on "
        "the wire's line), or reconnect it afterwards (sch_delete the wire, sch_connect).",
        code="overlap",
        details={"problems": problems[:12]},
    )


def _pin_name(scene: Scene, p: Point) -> str:
    """The pin (of a part or a sheet symbol) at `p`, for error messages."""
    for s in scene.symbols.values():
        if not s.power and any(same_point(pe.at, p) for pe in s.pins):
            pe = next(pe for pe in s.pins if same_point(pe.at, p))
            return f"{s.ref}.{pe.number}"
    for i in scene.sheet_pins.values():
        if same_point(i.pin.at, p):
            return f"sheet pin {i.pin.label}"
    return f"the pin at {fmt(p)}"


def _obstacles_in(scene: Scene, boxes: list[Box], ignore: Sequence[Point] = ()) -> list[str]:
    """What of `scene` overlaps one of `boxes`: kind, UUID and position (errors)."""

    def hit(b: Box) -> bool:
        return any(x.overlaps(b) for x in boxes)

    def inside(p: Point) -> bool:
        return any(x.contains(p) for x in boxes) and not any(same_point(p, q) for q in ignore)

    syms = list(scene.symbols.values())
    out = [f"symbol {short(s.ref, 40)} ({s.uuid}) at {fmt(s.at)}" for s in syms if hit(s.body)]
    out += [f"a field of {short(s.ref, 40)} ({s.uuid})" for s in syms if any(map(hit, s.fields))]
    out += [
        f"pin {s.ref}.{pe.number} at {fmt(pe.at)}"
        for s in scene.symbols.values() for pe in s.pins if inside(pe.at)
    ]  # fmt: skip
    out += [
        f"label {short(lb.text, 40)} ({u}) at {fmt(lb.at)}"
        for u, lb in scene.labels.items()
        if hit(label_box(lb.at, lb.text, SPIN_DIR.get(lb.spin, (1, 0)), lb.kind))
    ]
    out += [f"sheet symbol ({u}) at {fmt((b.x0, b.y0))}" for u, b in scene.sheets.items() if hit(b)]
    out += [
        f"wire {w.uuid} {fmt(w.start)}-{fmt(w.end)}"
        for w in scene.wires.values()
        if any(x.hit_by_segment(w.start, w.end) for x in boxes)
    ]
    out += [f"junction ({u}) at {fmt(p)}" for u, p in scene.junctions.items() if inside(p)]
    out += [f"no-connect ({u}) at {fmt(p)}" for u, p in scene.no_connects.items() if inside(p)]
    return out


def _obstacles_on(scene: Scene, path: list[Point], own: set[str]) -> list[str]:
    """Foreign items a carried wire along `path` meets (its start is the moving pin):
    kind, UUID and position (errors)."""
    from copperchat.kicad_tools.edit.geometry import on_segment

    segs = list(itertools.pairwise(path))

    def on(p: Point) -> bool:
        return not same_point(p, path[0]) and any(on_segment(p, a, b) for a, b in segs)

    out = [
        f"wire {w.uuid} {fmt(w.start)}-{fmt(w.end)}"
        for w in scene.wires.values()
        if w.uuid not in own
        and (on(w.start) or on(w.end) or any(on_segment(p, w.start, w.end) for p in path[1:]))
    ]
    start_owner = _owner(scene, path[0])
    for s in scene.symbols.values():
        if s.uuid in own:
            continue
        out += [f"pin {s.ref}.{pe.number} at {fmt(pe.at)}" for pe in s.pins if on(pe.at)]
        if s.uuid != start_owner and any(s.body.hit_by_segment(a, b) for a, b in segs):
            out.append(f"symbol {s.ref} ({s.uuid}) at {fmt(s.at)}")
    out += [f"label {short(lb.text, 40)} ({u}) at {fmt(lb.at)}" for u, lb in scene.labels.items()
            if u not in own and on(lb.at)]  # fmt: skip
    out += [f"sheet pin {short(i.pin.label, 40)} ({u}) at {fmt(i.pin.at)}"
            for u, i in scene.sheet_pins.items() if on(i.pin.at)]  # fmt: skip
    out += [f"junction ({u}) at {fmt(p)}" for u, p in scene.junctions.items() if on(p)]
    out += [f"no-connect ({u}) at {fmt(p)}" for u, p in scene.no_connects.items()
            if u not in own and on(p)]  # fmt: skip
    return out


def _carried_error(
    tool: str,
    pin: str,
    what: str,
    uid: str,
    at: Point,
    problems: list[str],
    obstacles: list[str],
) -> StructuredToolError:
    """A stub wire or end item carried with a turned / moved part would collide (O8)."""
    hit = f"; in the way: {'; '.join(obstacles[:3])}" if obstacles else ""
    return StructuredToolError(
        f"{tool} refused: {what} ({uid}) on pin {pin} cannot travel with the part to {fmt(at)}",
        "at its new place it would " + problems_summary(problems) + hit,
        "Nothing was written. Make room there, pick another rotation / position "
        "(sch_find_space), or delete the stub and reconnect afterwards.",
        code="overlap",
        details={
            "pin": pin,
            "carried": {"item": what, "uuid": uid, "at_mm": list(at)},
            "problems": problems[:12],
            "obstacles": obstacles[:12],
        },
    )


def _refuse_sheet_on_wires(
    tool: str, scene: Scene, others: Scene, uid: str, box: Box, pin_points: list[Point]
) -> None:
    """A moved / turned sheet box must not newly break the sheet-edge rule with a wire
    that stays (crossing, on or along an edge) nor cover a label, junction or no-connect
    (Review Focus 5)."""
    from copperchat.kicad_tools.edit.sheet_edges import SheetBox

    name = next((i.pin.ref for i in scene.sheet_pins.values() if i.sheet_uuid == uid), "")
    new, old = SheetBox(uid, name, box), SheetBox(uid, name, scene.sheets[uid])
    old_pins = [i.pin.at for i in scene.sheet_pins.values()]
    for w in others.wires.values():
        found = sheet_edge_problems((w.start, w.end), [new], sheet_pins=pin_points)
        if found and not sheet_edge_problems((w.start, w.end), [old], sheet_pins=old_pins):
            raise StructuredToolError(
                f"{tool} refused: {new.label()} at its new place would meet wire {w.uuid}",
                f"wire {fmt(w.start)}-{fmt(w.end)}: {found[0]}",
                "Nothing was written. Pick another position / rotation with free room around "
                "the sheet box (2.54 mm from its edges), or move the wire first.",
                code="overlap",
                details={"obstacle": {"uuid": w.uuid, "start_mm": list(w.start),
                                      "end_mm": list(w.end)}, "problems": found[:12]},
            )  # fmt: skip
    inner = box.grow(-0.01)
    points = [("label", u, lb.at) for u, lb in others.labels.items()]
    points += [("junction", u, p) for u, p in others.junctions.items()]
    points += [("no-connect", u, p) for u, p in others.no_connects.items()]
    for kind, u, p in points:
        if inner.contains(p, eps=0.0) and not scene.sheets[uid].contains(p):
            raise StructuredToolError(
                f"{tool} refused: {new.label()} at its new place would cover a {kind}",
                f"{kind} {u} at {fmt(p)} lies inside the new sheet box",
                "Nothing was written. Pick another position / rotation with free room.",
                code="overlap",
                details={"obstacle": {"uuid": u, "kind": kind, "at_mm": list(p)}},
            )


def _moved_keys(scene: Scene, targets: list[Target]) -> frozenset[tuple[str, ...]]:
    """The netlist members the targets bring along (`ExpectNet.moved`): pin keys of moved
    symbols, (sheet_path, uuid) of moved labels and sheet pins (all pins of a sheet)."""
    path = scene.sheet.path
    out: set[tuple[str, ...]] = set()
    for t in targets:
        if t.kind == "symbol" and t.symbol is not None:
            out |= {pe.key for pe in t.symbol.pins}
        elif t.kind == "sheet":
            out |= {(path, u) for u, i in scene.sheet_pins.items() if i.sheet_uuid == t.uuid}
        else:
            out.add((path, t.uuid))
    return frozenset(out)


def _lands_on(
    problem: str,
    lay: Layout,
    scene: Scene,
    world: World,
    nets: dict[str, str],
    expect_net: str | None,
) -> bool:
    """A moved pin landing on items of the declared `expect_net` only: the intended
    connection (#4), not a clash (the verification checks it is the only one)."""
    if expect_net is None:
        return False
    for pin in lay.pins:
        if not problem.startswith(f"pin {pin.number} at "):
            continue
        found = []
        for i in scene.items_at(pin.at):
            if i["kind"] == "pin":
                found.append(world.netlist.net_of((scene.sheet.path, i["uuid"], i["pin"])))
            elif i["kind"] == "sheet_pin":
                found.append(world.sheet_pin_net(scene.sheet_pins[i["uuid"]]))
            else:
                found.append(nets.get(i["uuid"]))
        return bool(found) and all(n == expect_net for n in found)
    return False
