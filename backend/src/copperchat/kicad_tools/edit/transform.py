"""`sch_move` planning (E6 Task 1): move / rotate / mirror symbols, move labels and sheet
symbols, keeping every connection.

KiCad's API moves only the item: fields stay where they were and attached wires are not
dragged (spike 2A Q4). So the move is planned here and written in one commit:

- the symbol with its canonical transform (definition cleared by the engine), every
  field shifted with it (rotated about the origin on a transform change, Reference /
  Value laid out again as on placement);
- power symbols, labels and no-connects sitting directly on a moved pin travel with it;
- a wire ending on a moved point is dragged: its end follows the point; where that
  makes the segment diagonal it is replaced by an L (Delete + Create: KiCad may merge
  a leg with a collinear wire at push), the corner chosen by the router's checks;
- where a moved point has connections that cannot be dragged (a wire passing through
  it, a wire with something attached to its interior, another part's pin, a sheet pin),
  a bridge wire joins the new point to the old one.

Every new or changed segment is checked like a router route (no foreign pin, wire end,
label or body touched; corners not on wires) and the moved bodies like a placement
(overlap -> the nearest free position is suggested) — all before anything is written.
Verification: the netlist partition must stay identical (`keep_partition`).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Create,
    Delete,
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
    World,
    _v2,
    fmt,
    make_junction,
    make_wire,
    spin_of,
)
from copperchat.kicad_tools.edit.place import Layout, clashes, lay_out_fields, overlap_error
from copperchat.kicad_tools.edit.router import problems_summary, route_problems
from copperchat.kicad_tools.edit.scene import (
    LabelInfo,
    Scene,
    SymbolInfo,
    WireInfo,
    sheet_api,
    symbol_info,
)
from copperchat.kicad_tools.edit.targets import LABEL_KINDS, Target, bad, sheet_symbols

Mirror = Literal["", "x", "y"]
MOVE_KINDS = ("symbol", *LABEL_KINDS, "sheet")


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


@dataclass
class MovePlan:
    moved: list[dict[str, Any]] = field(default_factory=list)
    along: list[str] = field(default_factory=list)  # what travelled with the parts
    dragged: int = 0
    bridges: int = 0


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
        self._pin_nets: dict[str, str | None] = {}  # sheet pin uuid -> net (read once)

    def add_wire(self, a: Point, b: Point, net: str | None) -> None:
        uid = f"planned-move-{next(self.fake)}"
        self.scene.wires[uid] = WireInfo(uid, a, b)
        if net is not None:
            self.nets[uid] = net

    def problems(
        self, pts: list[Point], net: str | None, own: tuple[str | None, str | None]
    ) -> list[str]:
        allowed = {net} if net is not None else set()
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
            pts, scene, self.world.netlist, allowed, item_nets=self.nets, own_symbols=own
        )


def plan_move(
    world: World,
    tool: str,
    targets: list[Target],
    to: Point | None,
    by: Point | None,
    rotation: int | None,
    mirror: Mirror | None,
) -> tuple[ChangeSet, MovePlan]:
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
    if (rotation is not None or mirror is not None) and any(t.kind != "symbol" for t in targets):
        raise bad(
            f"{tool}: only symbols rotate or mirror",
            "labels and sheet symbols are moved with to/by only",
            "Drop rotation/mirror for labels and sheets.",
        )
    sheet = targets[0].sheet
    scene = world.scene(sheet)
    nets = world.nets(sheet)
    plan = MovePlan()
    moving: list[_Moving] = []
    updates: list[Any] = []
    moved_ids: set[str] = set()
    new_infos: list[tuple[SymbolInfo, Any, int, Mirror, str]] = []
    sheet_pin_points = [i.pin.at for i in scene.sheet_pins.values()]
    api = sheet_api(world.h, sheet)
    cache: dict[str, dict[str, Any]] = {}

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
            sym, _changed = moved_symbol(old, at, rot, mir)
            new_pins = {p.number: p for p in pin_ends(sym)}
            for pe in old.pins:
                q = new_pins.get(pe.number)
                if q is None:
                    continue
                moving.append(
                    _Moving(pe.at, q.at, world.netlist.net_of(pe.key), old.uuid, q.direction)
                )
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
            d = _sub(at, lb.at)
            item.position = _v2(at)
            item.text.position = _v2(_add(mm_point(item.text.position), d))
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
                }
            )
        elif t.kind == "sheet":
            sym = copy_item(sheet_symbols(world, sheet)[t.uuid])
            old_at = mm_point(sym.position)
            d = delta_for(old_at)
            sym.position = _v2(_add(old_at, d))
            pins = []
            for pin in sym.pins:
                p_old = mm_point(pin.position)
                pin.position = _v2(_add(p_old, d))
                pin.text.position = _v2(_add(mm_point(pin.text.position), d))
                pins.append(pin)
                info = scene.sheet_pins.get(access.kiid(pin))
                moving.append(
                    _Moving(
                        p_old,
                        _add(p_old, d),
                        nets.get(access.kiid(pin)),
                        t.uuid,
                        info.pin.direction if info else None,
                    )
                )
            sym.pins = pins
            for f in (sym.name_field, sym.filename_field):
                f.text.position = _v2(_add(mm_point(f.text.position), d))
            updates.append(sym)
            moved_ids.add(t.uuid)
            plan.moved.append(
                {
                    "kind": "sheet",
                    "uuid": t.uuid,
                    "from_mm": list(old_at),
                    "to_mm": list(_add(old_at, d)),
                }
            )
        else:
            raise bad(
                f"{tool} cannot move {t.kind.replace('_', ' ')} {t.spec}",
                "sch_move moves symbols, labels and sheet symbols; wires follow them",
                "Use sch_reroute to change a wire's course.",
            )
    if rotation is None and mirror is None and all(x["from_mm"] == x["to_mm"] for x in plan.moved):
        raise bad(
            f"{tool}: nothing moves",
            "the items already are at that position",
            "Pass another 'to' / 'by', or a rotation / mirror.",
        )

    # -- items that travel with moved pins -----------------------------------------------
    along_ids: set[str] = set()
    pin_points = [m for m in moving if not m.label and m.own in moved_ids]  # symbol/sheet pins
    for m in pin_points:
        d = _sub(m.new, m.old)
        for lb in scene.labels.values():
            if lb.uuid in moved_ids or lb.uuid in along_ids or not same_point(lb.at, m.old):
                continue
            item = item_of("labels", lb.uuid)
            item.position = _v2(m.new)
            item.text.position = _v2(_add(mm_point(item.text.position), d))
            if m.direction is not None:
                item.spin_style = spin_of(DIRS[m.direction])
            updates.append(item)
            along_ids.add(lb.uuid)
            plan.along.append(f"label {lb.text}")
        for s in scene.symbols.values():
            if not s.power or s.uuid in moved_ids or s.uuid in along_ids:
                continue
            if any(same_point(p.at, m.old) for p in s.pins):
                psym, _ = moved_symbol(s, _add(s.at, d), s.rotation, s.mirror)  # type: ignore[arg-type]
                updates.append(psym)
                along_ids.add(s.uuid)
                new_infos.append((s, psym, s.rotation, s.mirror, s.ref))  # type: ignore[arg-type]
                plan.along.append(f"power symbol {s.ref}")
        for uid, nc_at in scene.no_connects.items():
            if uid not in along_ids and same_point(nc_at, m.old):
                nc = item_of("no_connects", uid)
                nc.position = _v2(m.new)
                updates.append(nc)
                along_ids.add(uid)
                plan.along.append("no-connect marker")

    # -- the scene after the move --------------------------------------------------------
    check = _Check(world, sheet, scene)
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
                uid, lb.text, mm_point(item.position), lb.kind, lb.spin
            )
        if uid in check.scene.no_connects:
            check.scene.no_connects[uid] = mm_point(item.position)
    for t in targets:
        if t.kind == "sheet":
            box = scene.sheets[t.uuid]
            d = delta_for((box.x0, box.y0))
            check.scene.sheets[t.uuid] = Box(
                box.x0 + d[0], box.y0 + d[1], box.x1 + d[0], box.y1 + d[1]
            )
            for uid, info in scene.sheet_pins.items():
                if info.sheet_uuid == t.uuid:
                    pin = replace(info.pin, at=_add(info.pin.at, d))
                    check.scene.sheet_pins[uid] = replace(info, pin=pin)

    moved_points = [m.old for m in moving if not same_point(m.old, m.new)]
    for w in scene.wires.values():  # attached wires follow (checked as routes below)
        if any(same_point(w.start, q) or same_point(w.end, q) for q in moved_points):
            others.wires.pop(w.uuid, None)

    # -- bodies --------------------------------------------------------------------------
    for old, sym, rot, mir, _spec in new_infos:
        lay = _layout(sym, rot, mir)
        problems = [p for p in clashes(others, lay) if not _stays(p, lay, old)]
        if problems:
            raise overlap_error(others, lay, problems, f"{tool} {old.ref}")
    for t in targets:
        if t.kind == "sheet":
            box = check.scene.sheets[t.uuid]
            hit = [
                n for u, b in others.sheets.items() if u != t.uuid and b.overlaps(box) for n in [u]
            ]
            hit += [s.ref for s in others.symbols.values() if s.body.grow(2.0).overlaps(box)]
            if hit:
                raise StructuredToolError(
                    f"{tool}: the sheet at its new position would overlap",
                    "it overlaps " + ", ".join(hit[:6]),
                    "Pick another position with free room for the whole sheet box.",
                    code="overlap",
                )

    # -- wires ---------------------------------------------------------------------------
    mapping = {_key(m.old): m for m in moving if not same_point(m.old, m.new)}
    creates: list[Any] = []
    deletes: list[str] = []
    wire_updates: dict[str, tuple[Point, Point]] = {}
    fixed_points: set[tuple[int, int]] = set()
    for w in list(scene.wires.values()):
        world.check_time(tool)
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
        chosen = None
        found: list[str] = []
        for pts in paths:
            found = check.problems(
                pts, net, (_owner(check.scene, pts[0]), _owner(check.scene, pts[-1]))
            )
            if not found:
                chosen = pts
                break
        if chosen is None:
            raise _short(tool, w, found)
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
            same_point(i.pin.at, m.old) and i.sheet_uuid not in moved_ids
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
    notes: list[str] = []
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
        intent=NetIntent(keep_partition=True),
        logical=Logical(logical),
        summary={"moved": plan.moved, "dragged_wires": plan.dragged, "bridges": plan.bridges},
        notes=notes,
    )
    return cs, plan


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
