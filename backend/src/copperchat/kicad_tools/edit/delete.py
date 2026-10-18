"""`sch_delete` planning (E6 Task 2; port of spike 2D `_attached` / `sch_delete`).

Deleting a symbol leaves its wires and no-connects behind (spike 2A Q5), so the tool
also removes what only served it: starting at the deleted pins, the wire network of
the sheet is pruned leaf by leaf — a connection point dies when at most one live wire
piece reaches it and nothing that stays (another part's pin, a sheet pin, a
hierarchical label) sits there. Wires are split at every point something attaches to
their interior, so a stub ending in a T is cut back to the T (the wire is shortened,
not deleted), and a wire shared with other pins stays. Labels, junctions, no-connects
and power symbols at dead points go with it (spec §6.2). One `Delete` stage (plus
`Update`s of shortened wires); KiCad's push cleanup (dropped junctions, merged
segments) is read back by the engine as side effects.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from copperchat.kicad_tools.access import Netlist, PinKey
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Delete,
    Logical,
    NetIntent,
    SheetRef,
    Stage,
    Update,
)
from copperchat.kicad_tools.edit.geometry import (
    EPS_MM,
    Point,
    in_segment_interior,
    on_segment,
    orthogonal,
)
from copperchat.kicad_tools.edit.labels import World, _v2
from copperchat.kicad_tools.edit.scene import Scene, sheet_api
from copperchat.kicad_tools.edit.targets import LABEL_KINDS, Target, sheet_pins

NodeKey = tuple[int, int]


def node_key(p: Point) -> NodeKey:
    return (round(p[0] * 100), round(p[1] * 100))


@dataclass
class Pruned:
    """What dies with the deleted items on one sheet."""

    wires: list[str] = field(default_factory=list)  # deleted entirely
    shortened: dict[str, tuple[Point, Point]] = field(default_factory=dict)  # uuid -> new ends
    labels: list[str] = field(default_factory=list)
    junctions: list[str] = field(default_factory=list)
    no_connects: list[str] = field(default_factory=list)
    power: list[str] = field(default_factory=list)  # power symbol uuids

    def all_ids(self) -> list[str]:
        return [*self.wires, *self.labels, *self.junctions, *self.no_connects, *self.power]


@dataclass
class _Edge:
    wire: str
    a: NodeKey
    b: NodeKey
    pa: Point
    pb: Point
    idx: int  # order along the wire (from its start)


class _LineIndex:
    """Wires by the line they lie on and points by row/column (0.01 mm keys): a point or
    segment query looks only at items on its line, not the whole sheet."""

    def __init__(self, wires: Iterable[Any], points: Iterable[Point] = ()) -> None:
        self.h: dict[int, list[Any]] = {}  # y key -> horizontal wires
        self.v: dict[int, list[Any]] = {}  # x key -> vertical wires
        self.rows: dict[int, list[Point]] = {}
        self.cols: dict[int, list[Point]] = {}
        for w in wires:
            if abs(w.start[1] - w.end[1]) < EPS_MM:
                self.h.setdefault(round(w.start[1] * 100), []).append(w)
            elif abs(w.start[0] - w.end[0]) < EPS_MM:
                self.v.setdefault(round(w.start[0] * 100), []).append(w)
        for p in points:
            self.rows.setdefault(round(p[1] * 100), []).append(p)
            self.cols.setdefault(round(p[0] * 100), []).append(p)

    @staticmethod
    def _near(index: dict[int, list[Any]], v: float) -> list[Any]:
        k = round(v * 100)
        return [*index.get(k - 1, ()), *index.get(k, ()), *index.get(k + 1, ())]

    def wires_at(self, q: Point) -> list[Any]:
        cands = self._near(self.h, q[1]) + self._near(self.v, q[0])
        return [w for w in cands if on_segment(q, w.start, w.end)]

    def points_inside(self, w: Any) -> list[Point]:
        if abs(w.start[1] - w.end[1]) < EPS_MM:
            cands = self._near(self.rows, w.start[1])
        else:
            cands = self._near(self.cols, w.start[0])
        return [p for p in cands if in_segment_interior(p, w.start, w.end)]


class WireGraph:
    """The wire network of one sheet around some start points: wires split at every
    attachment point of their interior, items per connection point."""

    def __init__(
        self,
        scene: Scene,
        extra_points: Iterable[Point],
        starts: Iterable[Point],
        check: Callable[[], None] | None = None,
    ):
        self.scene = scene
        self.edges: list[_Edge] = []
        self.at: dict[NodeKey, list[int]] = {}
        self.pieces: dict[str, list[int]] = {}  # wire uuid -> its edges, in order
        self._check = check or (lambda: None)
        index = _LineIndex(scene.wires.values(), self._points(extra_points))
        for w in self._component(list(starts), index):
            cuts = [w.start, w.end, *index.points_inside(w)]
            horizontal = abs(w.start[1] - w.end[1]) < EPS_MM
            axis = 0 if horizontal else 1
            sign = 1 if w.end[axis] >= w.start[axis] else -1
            cuts.sort(key=lambda p: sign * p[axis])
            uniq: list[Point] = []
            for p in cuts:
                if not uniq or node_key(p) != node_key(uniq[-1]):
                    uniq.append(p)
            for i in range(len(uniq) - 1):
                e = _Edge(w.uuid, node_key(uniq[i]), node_key(uniq[i + 1]), uniq[i], uniq[i + 1], i)
                n = len(self.edges)
                self.edges.append(e)
                self.at.setdefault(e.a, []).append(n)
                self.at.setdefault(e.b, []).append(n)
                self.pieces.setdefault(w.uuid, []).append(n)

    def _component(self, starts: list[Point], index: _LineIndex) -> list[Any]:
        """Wires connected (by ends or T-joins) to the start points."""
        ends = _LineIndex((), [q for w in self.scene.wires.values() for q in (w.start, w.end)])
        found: dict[str, Any] = {}
        frontier = list(starts)
        seen: set[NodeKey] = {node_key(p) for p in frontier}
        steps = 0
        while frontier:
            q = frontier.pop()
            steps += 1
            if steps % 256 == 0:
                self._check()
            for w in index.wires_at(q):
                if w.uuid in found:
                    continue
                found[w.uuid] = w
                # its ends, and the ends of other wires lying on it (T-joins)
                for p in [w.start, w.end, *ends.points_inside(w)]:
                    if node_key(p) not in seen:
                        seen.add(node_key(p))
                        frontier.append(p)
        return list(found.values())

    def _points(self, extra: Iterable[Point]) -> list[Point]:
        s = self.scene
        pts = [p.at for p in s.all_pins()]
        pts += [w.start for w in s.wires.values()] + [w.end for w in s.wires.values()]
        pts += [lb.at for lb in s.labels.values()]
        pts += list(s.junctions.values()) + list(s.no_connects.values()) + list(extra)
        return pts


def prune(
    scene: Scene,
    starts: list[Point],
    dead_symbols: set[str],
    *,
    cuts: Iterable[Point] = (),
    keep: Iterable[Point] = (),
    dead_items: Iterable[str] = (),
    dead_wires: Iterable[str] = (),
    check: Callable[[], None] | None = None,
) -> Pruned:
    """Items that only served `starts` (the deleted symbols' pins, a deleted label ...).
    `cuts`: extra connection points (sheet pins); `keep`: points that stay connected
    whatever happens (pins of sheet symbols that stay)."""
    graph = WireGraph(scene, cuts, starts, check)
    anchors: set[NodeKey] = {node_key(p) for p in keep}
    for w in scene.wires.values():  # the graph sees orthogonal wires only: a diagonal
        if not orthogonal(w.start, w.end):  # wire's ends keep whatever they touch
            anchors |= {node_key(w.start), node_key(w.end)}
    power_at: dict[NodeKey, list[str]] = {}
    for sym in scene.symbols.values():
        if sym.uuid in dead_symbols:
            continue
        for p in sym.pins:
            if sym.power:
                power_at.setdefault(node_key(p.at), []).append(sym.uuid)
            else:
                anchors.add(node_key(p.at))
    gone = set(dead_items)
    # A label or power symbol still reached by a live wire keeps serving that wire (a
    # label stub of another part): pruning stops there.
    holders: set[NodeKey] = set(power_at)
    for lb in scene.labels.values():
        if lb.uuid in gone:
            continue
        if lb.kind == "hierarchical_label":  # the sheet's interface stays
            anchors.add(node_key(lb.at))
        holders.add(node_key(lb.at))
    dead_edges: set[int] = set()
    dead_nodes: set[NodeKey] = set()
    queue = [node_key(p) for p in starts]
    for uid in dead_wires:  # listed wires: gone, their ends may leave dangling items
        for e in graph.pieces.get(uid, []):
            dead_edges.add(e)
            queue += [graph.edges[e].a, graph.edges[e].b]
    while queue:
        n = queue.pop()
        if n in dead_nodes or n in anchors:
            continue
        live = [e for e in graph.at.get(n, []) if e not in dead_edges]
        if len(live) > 1 or (live and n in holders):
            continue
        dead_nodes.add(n)
        for e in live:
            dead_edges.add(e)
            edge = graph.edges[e]
            queue.append(edge.b if edge.a == n else edge.a)
    out = Pruned()
    for uid, pieces in graph.pieces.items():
        alive = [e for e in pieces if e not in dead_edges]
        if not alive:
            out.wires.append(uid)
        elif len(alive) < len(pieces):
            first, last = graph.edges[alive[0]], graph.edges[alive[-1]]
            out.shortened[uid] = (first.pa, last.pb)
    for lb in scene.labels.values():
        if node_key(lb.at) in dead_nodes and lb.uuid not in gone:
            out.labels.append(lb.uuid)
    out.junctions = [u for u, p in scene.junctions.items() if node_key(p) in dead_nodes]
    out.no_connects = [u for u, p in scene.no_connects.items() if node_key(p) in dead_nodes]
    for n in dead_nodes:
        out.power += power_at.get(n, [])
    out.power = sorted(set(out.power))
    return out


# -- the change set -----------------------------------------------------------------------


def _net_keys(world: World, sheet: SheetRef, uid: str) -> set[PinKey]:
    """Members of the net an item (wire, label ...) is on, in every instance."""
    out: set[PinKey] = set()
    for inst in world.instances(sheet):
        net = world.nets(inst).get(uid)
        out |= set(world.members(net))
    return out


def _symbol_keys(world: World, uid: str, sheet: SheetRef) -> set[PinKey]:
    """Pin keys of a symbol in every instance of its sheet file."""
    paths = {r.path for r in world.instances(sheet)}
    return {k for k in world.known if k[1] == uid and k[0] in paths}


def nets_changed(
    before: Netlist, after: Netlist, nets: Iterable[str], label: Any
) -> list[dict[str, Any]]:
    """Per former net: its pins before, and the nets its remaining pins are on now."""
    out: list[dict[str, Any]] = []
    a_keys = {k for keys in after.nets.values() for k in keys}
    for net in sorted(set(nets)):
        members = before.nets.get(net, frozenset())
        remaining = [k for k in members if k in a_keys]
        now: dict[str, list[str]] = {}
        for k in remaining:
            now.setdefault(after.net_of(k) or "?", []).append(label(k))
        if len(now) == 1 and next(iter(now)) == net and len(remaining) == len(members):
            continue
        out.append(
            {
                "net": net,
                "before": sorted(label(k) for k in members)[:40],
                "after": [{"net": n, "pins": sorted(p)[:40]} for n, p in sorted(now.items())],
            }
        )
    return out[:50]


@dataclass
class DeletePlan:
    """What the tool deletes, for the result and `nets_changed`."""

    targets: list[Target]
    attached: dict[str, Pruned]  # sheet path -> pruned items
    nets: set[str]  # nets of the deleted items before the call
    shared: list[str]  # notes about shared sheet files
    sheet_users: dict[str, list[str]] = field(default_factory=dict)  # deleted sheet -> users


def plan_delete(
    world: World,
    tool: str,
    targets: list[Target],
    with_attached: bool,
    placed_this_turn: set[str],
) -> tuple[ChangeSet, DeletePlan]:
    by_sheet: dict[str, tuple[SheetRef, list[Target]]] = {}
    for t in targets:
        by_sheet.setdefault(t.sheet.path, (t.sheet, []))[1].append(t)
    may_change: set[PinKey] = set()
    nets: set[str] = set()
    deleted_ids: dict[str, tuple[SheetRef, list[str]]] = {}
    updates: dict[str, tuple[SheetRef, list[Any]]] = {}
    attached: dict[str, Pruned] = {}
    for sheet, ts in by_sheet.values():
        world.check_time(tool)
        scene = world.scene(sheet)
        pins = sheet_pins(world, sheet)
        dead_symbols = {t.uuid for t in ts if t.kind == "symbol"}
        dead_sheets = {t.uuid for t in ts if t.kind == "sheet"}
        ids = deleted_ids.setdefault(sheet.path, (sheet, []))[1]
        starts: list[Point] = []
        for t in ts:
            ids.append(t.uuid)
            if t.kind == "symbol":
                assert t.symbol is not None
                starts += [p.at for p in t.symbol.pins]
                keys = _symbol_keys(world, t.uuid, sheet)
                for k in keys:
                    if (n := world.netlist.net_of(k)) is not None:
                        nets.add(n)
                        may_change |= set(world.members(n))
            elif t.kind == "sheet":
                starts += [p.pin.at for p in pins if p.sheet_uuid == t.uuid]
                for p in pins:
                    if p.sheet_uuid != t.uuid:
                        continue
                    here: set[str | None] = set()
                    for inst in world.instances(sheet):  # a shared parent file: each copy
                        here.add(world.nets(inst).get(p.uuid))
                        here |= {
                            world.nets(inst).get(w.uuid) for w in scene.wires_through(p.pin.at)
                        }
                    for n in here - {None}:
                        nets.add(n)  # type: ignore[arg-type]
                        may_change |= set(world.members(n))
                inside = removed_paths(world, t)  # every pin inside the sheet goes with it
                may_change |= {k for k in world.known if k[0] in inside}
            else:
                if t.kind in LABEL_KINDS and t.at is not None:
                    starts.append(t.at)
                if t.kind == "wire" and t.uuid in scene.wires:
                    starts += [scene.wires[t.uuid].start, scene.wires[t.uuid].end]
                for inst in world.instances(sheet):
                    if (n := world.nets(inst).get(t.uuid)) is not None:
                        nets.add(n)
                may_change |= _net_keys(world, sheet, t.uuid)
        if not with_attached:
            continue
        pruned = prune(
            scene,
            starts,
            dead_symbols,
            cuts=[p.pin.at for p in pins],
            keep=[p.pin.at for p in pins if p.sheet_uuid not in dead_sheets],
            dead_items=[t.uuid for t in ts],
            dead_wires=[t.uuid for t in ts if t.kind == "wire"],
            check=lambda: world.check_time(tool),
        )
        listed = set(ids)
        pruned.wires = [u for u in pruned.wires if u not in listed]
        pruned.labels = [u for u in pruned.labels if u not in listed]
        pruned.junctions = [u for u in pruned.junctions if u not in listed]
        pruned.no_connects = [u for u in pruned.no_connects if u not in listed]
        pruned.power = [u for u in pruned.power if u not in listed]
        pruned.shortened = {u: e for u, e in pruned.shortened.items() if u not in listed}
        attached[sheet.path] = pruned
        ids += pruned.all_ids()
        for u in pruned.power:
            may_change |= _symbol_keys(world, u, sheet)
        if pruned.shortened:
            lines = {
                str(i.id.value): i
                for i in sheet_api(world.h, sheet).get_lines()
                if str(i.id.value) in pruned.shortened
            }
            items = []
            for uid, (a, b) in pruned.shortened.items():
                line = lines[uid]
                line.start, line.end = _v2(a), _v2(b)
                items.append(line)
            updates.setdefault(sheet.path, (sheet, []))[1].extend(items)
    ops: list[Any] = [Delete(list(dict.fromkeys(u)), sheet=s) for s, u in deleted_ids.values() if u]
    ops += [Update(items, sheet=s) for s, items in updates.values() if items]
    logical, deleted_symbols = _logical(world, targets, placed_this_turn)
    shared = sorted(
        {
            f"{t.sheet.file} is used by {len(t.also_in) + 1} sheet instances: the deletion "
            f"shows in {', '.join(r.human_path for r in [t.sheet, *t.also_in])}"
            for t in targets
            if t.also_in
        }
    )
    cs = ChangeSet(
        tool=tool,
        sheet=targets[0].sheet,
        stages=[Stage("delete", lambda _inp: list(ops))],
        pre_images={},
        intent=NetIntent(may_change=frozenset(may_change)),
        logical=Logical(logical, deleted_symbols=deleted_symbols),
        notes=list(shared),
    )
    users: dict[str, list[str]] = {}
    for t in targets:
        if t.kind == "sheet" and t.child is not None:
            gone = t.child.path
            users[t.uuid] = sorted(
                r.human_path
                for r in world.sheets.values()
                if r.file == t.child.file and r.path != gone and not r.path.startswith(gone + "/")
            )
    return cs, DeletePlan(targets, attached, nets, shared, users)


def removed_paths(world: World, t: Target) -> set[str]:
    """Sheet instance paths a deleted sheet symbol removes: its sub-sheet (and below) in
    every instance of the file that holds the sheet symbol."""
    roots = {f"{inst.path}/{t.uuid}" for inst in world.instances(t.sheet)}
    return {
        r.path
        for r in world.sheets.values()
        if any(r.path == g or r.path.startswith(g + "/") for g in roots)
    }


def _logical(
    world: World, targets: list[Target], placed_this_turn: set[str]
) -> tuple[int, list[str]]:
    """E-C6: 1 per deleted symbol (all units of a reference), label, sheet, junction,
    no-connect; directly deleted wires 1 per net they were on. `deleted_symbols`: every
    own placement (refunded by the gate) plus one entry per pre-existing non-power
    reference and sheet instance (the <= 3 rule counts parts, not units): a symbol of a
    sheet file used N times leaves N parts (`uuid@path` for the other instances), and
    the parts inside a deleted sheet instance count too."""
    changes = 0
    deleted: list[str] = []
    units: dict[str, set[int]] = {}  # part key -> units counted
    wire_nets: set[str] = set()

    def part_key(path: str, ref: str, uid: str) -> str:
        # unannotated parts ('R?') are separate parts even with equal references
        return f"{path}:{uid}" if ref.endswith("?") or not ref else f"{path}:{ref}"

    def count(uid: str, path: str, ref: str, unit: int) -> bool:
        """Book one part (all its units count once); False when already booked. A unit
        number seen again under the same reference is a second part (duplicate ref)."""
        key = part_key(path, ref, uid)
        seen = units.get(key)
        if seen is not None and unit not in seen:
            seen.add(unit)
            return False
        if seen is not None:  # a duplicate reference: its own part
            key = f"{path}:{uid}"
            if key in units:
                return False
        units[key] = {unit}
        deleted.append(uid if uid not in deleted else f"{uid}@{path}")
        return True

    for t in targets:
        if t.kind == "symbol":
            assert t.symbol is not None
            if t.uuid in placed_this_turn:
                deleted.append(t.uuid)
                changes += 1
                continue
            if t.power:
                key = part_key(t.sheet.path, t.ref or "", t.uuid)
                if key not in units:
                    units[key] = {t.symbol.unit}
                    changes += 1
                continue
            first = True
            for inst in world.instances(t.sheet):
                sym = next(
                    (s for s in world.symbols if s.uuid == t.uuid and s.sheet.path == inst.path),
                    None,
                )
                if sym is not None and count(t.uuid, inst.path, sym.ref, sym.unit) and first:
                    changes += 1  # one logical change per part (its first instance)
                first = False
        elif t.kind == "wire":
            net = world.nets(t.sheet).get(t.uuid) or t.uuid
            if net not in wire_nets:
                wire_nets.add(net)
                changes += 1
        elif t.kind == "sheet":
            changes += 1
            # The parts of the removed sheet instance(s) leave the design: they count
            # toward the <= 3 rule like deleted symbols (one per part).
            gone = removed_paths(world, t)
            for sym in world.symbols:
                if sym.power or sym.sheet.path not in gone:
                    continue
                if sym.uuid in placed_this_turn:
                    if sym.uuid not in deleted:
                        deleted.append(sym.uuid)
                        changes += 1  # refunded by the gate with the placement
                else:
                    count(sym.uuid, sym.sheet.path, sym.ref, sym.unit)
        else:
            changes += 1
    return changes, deleted
