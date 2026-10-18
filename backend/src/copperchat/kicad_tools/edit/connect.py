"""Connections between pins and nets (E5 Tasks 2 and 4): `sch_connect` by a routed wire
(incl. T-joins onto the target net), a power symbol or labels; `sch_rename_net`;
`sch_reroute`. Everything here runs inside one `session.call` in `prepare`.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.access import PinKey
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
from copperchat.kicad_tools.edit.geometry import (
    EPS_MM,
    PinEnd,
    Point,
    on_grid,
    on_segment,
    same_point,
)
from copperchat.kicad_tools.edit.labels import (
    NET_NAME_RE,
    RAIL_RE,
    World,
    bad,
    base_name,
    fmt,
    is_named,
    labels_change_set,
    make_junction,
    make_wire,
    no_change,
    same_name_nets,
)
from copperchat.kicad_tools.edit.router import (
    Route,
    WireTarget,
    blocked_reasons,
    needed_junctions,
    problems_summary,
    route,
    route_problems,
    simplify,
)
from copperchat.kicad_tools.edit.scene import Scene, sheet_api, short

Via = Literal["auto", "wire", "label"]
NEAR_MM = 25.4  # a wire of a named target net this close to the pin is routed to


@dataclass
class ConnectChangeSet(ChangeSet):
    """`sch_connect`'s change set: the pin whose net the result reports."""

    net_pin: PinKey | None = None


@dataclass
class PowerNeeded:
    """The connection is a rail: the caller plans a power symbol at `pin`."""

    pin: str
    net: str
    reason: str


def _distance(p: Point, a: Point, b: Point) -> float:
    x = min(max(p[0], min(a[0], b[0])), max(a[0], b[0]))
    y = min(max(p[1], min(a[1], b[1])), max(a[1], b[1]))
    return abs(p[0] - x) + abs(p[1] - y)


def _wire_targets(
    world: World,
    sheet: SheetRef,
    net: str | None,
    near: Point | None = None,
    limit: float | None = None,
) -> list[WireTarget]:
    if net is None:
        return []
    scene = world.scene(sheet)
    nets = world.nets(sheet)
    out = [
        WireTarget(w.uuid, w.start, w.end, net)
        for w in scene.wires.values()
        if nets.get(w.uuid) == net and not w.uuid.startswith("planned-")
    ]
    if near is not None and limit is not None:
        out = [t for t in out if _distance(near, t.a, t.b) <= limit]
    if near is not None:
        out.sort(key=lambda t: _distance(near, t.a, t.b))
    return out[:12]


def _best(routes: Sequence[tuple[Route, PinEnd] | None]) -> tuple[Route, PinEnd] | None:
    found = [r for r in routes if r is not None]
    return min(found, key=lambda r: r[0].cost) if found else None


def _route_from(
    world: World, start: PinEnd, target: PinEnd | WireTarget, allowed: set[str]
) -> tuple[Route, PinEnd] | None:
    sheet = world.sheet_of(start)
    r = route(
        start,
        target,
        world.scene(sheet),
        world.netlist,
        allowed_nets=allowed,
        item_nets=world.nets(sheet),
        deadline=world.deadline,
    )
    return None if r is None else (r, start)


def _wire_change_set(
    world: World,
    tool: str,
    found: tuple[Route, PinEnd],
    group: set[PinKey],
    summary: dict[str, Any],
    net_pin: PinKey,
) -> ConnectChangeSet:
    r, start = found
    sheet = world.sheet_of(start)
    scene = world.scene(sheet)
    junctions = needed_junctions(r.points, scene)
    items = [make_wire(a, b) for a, b in itertools.pairwise(r.points)]
    items += [make_junction(j) for j in junctions]
    summary = {
        **summary,
        "via_used": "wire",
        "route": {
            "points": [list(p) for p in r.points],
            "junction": list(r.junction_at) if r.junction_at else None,
        },
    }
    return ConnectChangeSet(
        tool=tool,
        sheet=sheet,
        stages=[Stage("connect", lambda _inp: [Create(items, sheet=sheet)])],
        pre_images={},
        intent=NetIntent(merge=[g for g in world.replicate(group, sheet) if len(g) > 1]),
        logical=Logical(1),
        summary=summary,
        net_pin=net_pin,
    )


def _no_route(tool: str, a: str, b: str, why: str) -> StructuredToolError:
    return StructuredToolError(
        f"{tool}: no clean wire route {a} -> {b}",
        why,
        "Move a part so the pins face each other with free room between them (sch_move), "
        "or connect with labels: via='label' (or 'auto').",
        code="no_route",
    )


def _as_labels(
    world: World,
    tool: str,
    pins: list[PinEnd],
    name: str,
    kind: Literal["local", "global"],
    summary: dict[str, Any],
) -> ConnectChangeSet:
    cs = labels_change_set(world, tool, pins, name, kind)
    keys = frozenset(p.key for p in pins)
    if not cs.stages and len({world.net(p) for p in pins}) > 1:
        raise bad(
            f"{tool}: labels cannot connect {', '.join(p.label for p in pins)}",
            f"every pin already carries a label {name}, yet they are on different nets",
            "Check the sheets: local labels connect only within one sheet.",
        )
    if len(keys) > 1:  # verification proves the labels really join the pins
        sheets = {world.sheet_of(p).path: world.sheet_of(p) for p in pins}
        if len(sheets) == 1:
            groups = [g for g in world.replicate(keys, cs.sheet) if len(g) > 1]
            cs.intent.merge = [*cs.intent.merge, *groups]
        else:  # global labels: every instance of both sheets on one net
            union: set[PinKey] = set()
            for p in pins:
                for group in world.replicate([p.key], world.sheet_of(p)):
                    union |= group
            cs.intent.merge = [*cs.intent.merge, frozenset(union)]
    return ConnectChangeSet(
        tool=cs.tool,
        sheet=cs.sheet,
        stages=cs.stages,
        pre_images=cs.pre_images,
        intent=cs.intent,
        logical=Logical(min(cs.logical.changes, 1) if cs.stages else 0),
        summary={**summary, **cs.summary, "via_used": "label"},
        notes=cs.notes,
        net_pin=pins[0].key,
    )


def plan_connect_pins(
    world: World, tool: str, pa: PinEnd, pb: PinEnd, via: Via, net_name: str | None
) -> ConnectChangeSet | PowerNeeded:
    if pa.key == pb.key:
        raise bad(
            f"{tool}: from and to are the same pin",
            f"both are {pa.label}",
            "Pass two different pins.",
        )
    na, nb = world.net(pa), world.net(pb)
    base = {"from": pa.label, "to": pb.label}
    if na is not None and na == nb:
        cs = no_change(
            tool, world.sheet_of(pa), {**base, "already_connected": True, "via_used": None}, []
        )
        return ConnectChangeSet(**{f: getattr(cs, f) for f in _CS_FIELDS}, net_pin=pa.key)
    named = [n for n in (na, nb) if is_named(n)]
    if len({base_name(n) for n in named if n}) > 1:
        raise bad(
            f"{tool}: {pa.label} is on {na}, {pb.label} on {nb}",
            "connecting them would merge two named nets",
            "Check the intent: connect to the right pin, or rename one net first (sch_rename_net).",
        )
    sa, sb = world.sheet_of(pa), world.sheet_of(pb)
    allowed = {n for n in (na, nb) if n is not None}
    found = None
    reasons: list[str] = []
    if via in ("auto", "wire"):
        if sa.path == sb.path:
            cands: list[tuple[Route, PinEnd] | None] = [_route_from(world, pa, pb, allowed)]
            cands += [
                _route_from(world, pa, t, allowed) for t in _wire_targets(world, sa, nb, pa.at)
            ]
            cands += [
                _route_from(world, pb, t, allowed) for t in _wire_targets(world, sa, na, pb.at)
            ]
            found = _best(cands)
            if found is None:
                scene = world.scene(sa)
                reasons = blocked_reasons(pa, pb, scene, world.netlist, allowed, world.nets(sa))
                if not on_grid(pa.at) or not on_grid(pb.at):
                    reasons.insert(0, "a pin end is off the 1.27 mm grid")
        else:
            reasons = [f"{pa.label} and {pb.label} are on different sheets"]
    if found is not None:
        return _wire_change_set(world, tool, found, {pa.key, pb.key}, base, pa.key)
    if via == "wire":
        raise _no_route(
            tool,
            pa.label,
            pb.label,
            "every route of at most 3 segments is blocked: " + problems_summary(reasons),
        )
    rail = next((n for n in named if n and base_name(n) in world.power_values()), None)
    if rail is not None and not net_name:
        other = pb if world.net(pa) == rail else pa
        return PowerNeeded(other.label, base_name(rail), "the other pin is on a power net")
    kind: Literal["local", "global"] = "local" if sa.path == sb.path else "global"
    local_named = [n for n in named if n and n.startswith("/")]
    if kind == "global" and local_named and not net_name:
        raise bad(
            f"{tool}: {pa.label} and {pb.label} are on different sheets",
            f"{local_named[0]} is a net local to its sheet; a global label of the same name "
            "would be a second, different net",
            "Connect the sheets through a sheet pin (hierarchical label), or pass net_name "
            "for a new global net.",
        )
    if net_name:
        name = net_name
    elif named:
        name = base_name(named[0])  # type: ignore[arg-type]
    else:
        name = safe_net_name(f"N_{pa.ref}_{pa.number}")  # only the generated name
    summary = {**base, "route_blocked_by": reasons[:6]}
    return _as_labels(world, tool, [pa, pb], name, kind, summary)


def safe_net_name(name: str) -> str:
    """A generated label name restricted to plain net-name characters (no bus or
    hierarchy syntax from refs, pin numbers or existing names)."""
    clean = re.sub(r"[^A-Za-z0-9_+\-.~]", "_", name)[:64]
    return clean if NET_NAME_RE.fullmatch(clean) else "N_NET"


_CS_FIELDS = ("tool", "sheet", "stages", "pre_images", "intent", "logical", "summary", "notes")


def _net_candidates(world: World, sheet: SheetRef, name: str) -> list[str]:
    """Existing nets `name` refers to from `sheet`: its local net there, else the global
    net of that name (or a full net name as given)."""
    out = []
    local = f"{sheet.human_path}{name}"
    for n in (local, name):
        if n in world.netlist.nets and n not in out:
            out.append(n)
    return out


def plan_connect_net(world: World, tool: str, pa: PinEnd, name: str, via: Via) -> ConnectChangeSet:
    sheet = world.sheet_of(pa)
    na = world.net(pa)
    targets = _net_candidates(world, sheet, name)
    # a label or hierarchical label of that name on the pin's sheet already joins a net
    # (e.g. '/IN' named on the parent, reaching this sheet through a sheet pin)
    targets += sorted(same_name_nets(world, name, "local", sheet) - set(targets))
    target = targets[0] if targets else None
    base = {"from": pa.label, "to": name}
    elsewhere = sorted(
        n for n in world.netlist.nets if n.startswith("/") and base_name(n) == name and is_named(n)
    )
    if target is None and elsewhere:
        raise bad(
            f"{tool}: {name} is a local net of another sheet",
            f"{', '.join(elsewhere[:4])} exists, but local nets connect only within their "
            f"sheet; a label {name} on {sheet.human_path} would be a different net",
            "Connect the sheets through a sheet pin (hierarchical label), or use a global "
            "label on both sides.",
        )
    if target is not None and na == target:
        cs = no_change(tool, sheet, {**base, "already_connected": True, "via_used": None}, [])
        return ConnectChangeSet(**{f: getattr(cs, f) for f in _CS_FIELDS}, net_pin=pa.key)
    if is_named(na) and base_name(na) != base_name(name):  # type: ignore[arg-type]
        raise bad(
            f"{tool}: {pa.label} is already on net {na}",
            f"connecting it to {name} would merge two named nets",
            f"Rename the net (sch_rename_net(old='{base_name(na)}', new='{name}')) or pick "  # type: ignore[arg-type]
            "the right pin.",
        )
    reasons: list[str] = []
    if via in ("auto", "wire") and target is not None:
        allowed = {n for n in (na, target) if n is not None}
        cands: list[tuple[Route, PinEnd] | None] = [
            _route_from(world, pa, t, allowed)
            for t in _wire_targets(world, sheet, target, pa.at, NEAR_MM)
        ]
        scene = world.scene(sheet)
        near = sorted(
            (
                (abs(p.at[0] - pa.at[0]) + abs(p.at[1] - pa.at[1]), p)
                for p in scene.all_pins()
                if world.netlist.net_of(p.key) == target
            ),
            key=lambda dp: dp[0],
        )
        cands += [_route_from(world, pa, p, allowed) for d, p in near[:12] if d <= NEAR_MM]
        found = _best(cands)
        if found is not None:
            members = sorted(world.members(target))
            here = [k for k in members if k[0] == sheet.path]  # a rep on this sheet
            group = {pa.key} | set((here or members)[:1])
            return _wire_change_set(world, tool, found, group, base, pa.key)
        reasons.append(f"no clean route onto net {target} within {NEAR_MM:g} mm")
    elif target is None:
        reasons.append(f"no net {name} exists yet")
    if via == "wire":
        raise _no_route(tool, pa.label, name, "; ".join(reasons) or "no route")
    kind: Literal["local", "global"] = (
        "global" if target == name and _is_global(world, name) else "local"
    )
    return _as_labels(world, tool, [pa], name, kind, {**base, "route_blocked_by": reasons})


def _is_global(world: World, name: str) -> bool:
    """A net of that exact name carried by a global label somewhere."""
    for sheet in world.sheets.values():
        scene = world.scene(sheet)
        if any(lb.kind == "global_label" and lb.text == name for lb in scene.labels.values()):
            return True
    return False


def looks_like_rail(world: World, name: str) -> bool:
    return name in world.power_values() or bool(RAIL_RE.fullmatch(name))


# -- sch_rename_net ---------------------------------------------------------------------


def _scenes(world: World) -> list[tuple[SheetRef, Scene]]:
    return [(s, world.scene(s)) for s in world.sheets.values()]


def plan_rename(world: World, tool: str, old: str, new: str) -> ChangeSet:
    nets = [n for n in world.netlist.nets if n == old] or [
        n for n in world.netlist.nets if base_name(n) == old and is_named(n)
    ]
    labelled: dict[str, list[tuple[SheetRef, str]]] = {}
    for sheet, scene in _scenes(world):
        item_nets = world.nets(sheet)
        for lb in scene.labels.values():
            net = item_nets.get(lb.uuid)
            if net is not None and lb.text == base_name(old) and (net in nets or not nets):
                labelled.setdefault(net, []).append((sheet, lb.uuid))
    candidates = sorted(set(nets) | set(labelled))
    if not candidates:
        raise StructuredToolError(
            f"{tool}: net {short(old, 70)} not found",
            "no net and no label has that name",
            "Look the name up with sch_get_net or sch_get_pin and retry.",
            code="not_found",
        )
    if len(candidates) > 1:
        raise bad(
            f"{tool}: {old} names several nets",
            f"it matches {', '.join(candidates[:12])}",
            "Pass the full net name (e.g. '/amp/IN') instead.",
            candidates=candidates[:20],
        )
    net = candidates[0]
    members = world.members(net)
    power = [k for k in members if any(s.uuid == k[1] and s.power for s in world.symbols)]
    if power or base_name(net) in world.power_values():
        raise bad(
            f"{tool}: {net} is a power net",
            "its name comes from power symbols, not labels",
            "Replace the power symbols (sch_delete + sch_add_power) to change a rail.",
        )
    if base_name(net) == new:
        return no_change(tool, next(iter(world.sheets.values())), {"old": net, "new": new}, [])
    if looks_like_rail(world, new):
        raise bad(
            f"{tool}: {new} is a power net name",
            "a label named like a rail does not join the rail",
            "Pick a signal name; rails use sch_add_power.",
        )
    found = labelled.get(net, [])
    if not found:
        raise bad(
            f"{tool}: net {net} has no label",
            "only label-named nets can be renamed (automatic names follow a pin)",
            f"Name the net with sch_add_label(net='{new}', at=[...]) instead.",
        )
    kinds = {world.scene(sheet).labels[uid].kind for sheet, uid in found}
    if "hierarchical_label" in kinds:
        raise bad(
            f"{tool}: net {net} is named by a hierarchical label",
            "a hierarchical label must keep the name of its sheet pin; renaming only the "
            "label would disconnect the sheet",
            "Rename the sheet pin and its hierarchical label together, or rename the net "
            "on the parent sheet.",
        )
    expected = net[: len(net) - len(base_name(net))] + new
    # The renamed labels would join every label of the new name in their scope: on
    # their own sheets, and anywhere for a global label.
    label_sheets = {sheet.path for sheet, _uid in found}
    clash = {n for n in world.netlist.nets if n == expected and n != net}
    for sheet, scene in _scenes(world):
        for lb in scene.labels.values():
            if lb.text != new or world.nets(sheet).get(lb.uuid) == net:
                continue
            if sheet.path in label_sheets or (
                "global_label" in kinds and lb.kind == "global_label"
            ):
                clash.add(world.nets(sheet).get(lb.uuid) or f"label {new} on {sheet.human_path}")
    if "global_label" in kinds and new in world.netlist.nets:
        clash.add(new)
    if clash:
        raise bad(
            f"{tool}: the name {new} is already used",
            f"net(s) {', '.join(sorted(clash)[:6])} carry it; renaming would merge them",
            "Pick an unused name, or connect the nets deliberately with sch_connect.",
        )
    by_file: dict[str, tuple[SheetRef, set[str]]] = {}
    for sheet, uid in found:  # a shared sheet file holds one label for all its instances
        by_file.setdefault(sheet.file, (sheet, set()))[1].add(uid)
    updates: list[Update] = []
    for sheet, uids in by_file.values():
        items = [lb for lb in sheet_api(world.h, sheet).get_labels() if str(lb.id.value) in uids]
        for lb in items:
            lb.text.value = new
        if items:
            updates.append(Update(items, sheet=sheet))
    notes = []
    shared = sorted(
        r.human_path
        for r in world.sheets.values()
        if r.file in by_file and r.path not in {s.path for s, _ in by_file.values()}
    )
    if shared:
        notes.append(
            "the label lives in a sheet file used by several instances; the nets of "
            f"{', '.join(shared[:6])} are renamed too"
        )
    rep = sorted(members)[:1]
    labels = sum(len(u) for _s, u in by_file.values())
    return ChangeSet(
        tool=tool,
        sheet=updates[0].sheet or next(iter(world.sheets.values())),
        stages=[Stage(f"rename {net}", lambda _inp: list(updates))],
        pre_images={},
        intent=NetIntent(keep_partition=True, names={k: expected for k in rep}),
        logical=Logical(1),
        summary={"old": net, "new": expected, "labels": labels},
        notes=notes,
    )


# -- sch_reroute ------------------------------------------------------------------------


def _pkey(p: Point) -> tuple[int, int]:
    return (round(p[0] * 100), round(p[1] * 100))


def _chain_between(scene: Scene, a: Point, b: Point) -> list[str]:
    """Wire uuids of the path from `a` to `b` through wire end points (BFS over an
    end-point adjacency map)."""
    adj: dict[tuple[int, int], list[tuple[Point, str]]] = {}
    for w in scene.wires.values():
        adj.setdefault(_pkey(w.start), []).append((w.end, w.uuid))
        adj.setdefault(_pkey(w.end), []).append((w.start, w.uuid))
    prev: dict[tuple[int, int], tuple[tuple[int, int], str] | None] = {_pkey(a): None}
    frontier = [a]
    goal = _pkey(b)
    while frontier and goal not in prev:
        nxt: list[Point] = []
        for q in frontier:
            for y, uid in adj.get(_pkey(q), ()):
                if _pkey(y) not in prev:
                    prev[_pkey(y)] = (_pkey(q), uid)
                    nxt.append(y)
        frontier = nxt
    if goal not in prev:
        return []
    out: list[str] = []
    node = goal
    while (step := prev[node]) is not None:
        node, uid = step
        out.append(uid)
    return out[::-1]


def _chain_ends(scene: Scene, uuids: list[str]) -> list[Point]:
    """End points that appear once among the given wires (a simple chain has two)."""
    counts: list[tuple[Point, int]] = []
    for uid in uuids:
        w = scene.wires[uid]
        for p in (w.start, w.end):
            for i, (q, n) in enumerate(counts):
                if same_point(p, q):
                    counts[i] = (q, n + 1)
                    break
            else:
                counts.append((p, 1))
    return [p for p, n in counts if n == 1]


def _inner_corner_only(at: Point, chain: list[Any], scene: Scene) -> bool:
    """A junction joining only the route's own wires (it goes away with the route)."""
    chain_ids = {w.uuid for w in chain}
    return not any(
        w.uuid not in chain_ids and on_segment(at, w.start, w.end) for w in scene.wires.values()
    )


def plan_reroute(world: World, tool: str, wires: Any, points: list[Point]) -> ChangeSet:
    from copperchat.kicad_tools.edit.geometry import snap_point

    if isinstance(wires, dict):
        pa, pb = world.pin(wires.get("from")), world.pin(wires.get("to"))
        sheet = world.sheet_of(pa)
        if world.sheet_of(pb).path != sheet.path:
            raise bad(
                f"{tool}: the pins are on different sheets",
                "a wire route stays on one sheet",
                "Reroute the wires on each sheet by uuid.",
            )
        scene = world.scene(sheet)
        uuids = _chain_between(scene, pa.at, pb.at)
        if not uuids:
            raise StructuredToolError(
                f"{tool}: no wire route {pa.label} -> {pb.label}",
                "the pins are not joined by a chain of wires",
                "Pass the wire uuids (sch_get_pin lists the wires at a pin).",
                code="not_found",
            )
    else:
        if not isinstance(wires, list) or not wires or len(wires) > 64:
            raise bad(
                f"{tool}: wires needs 1 to 64 uuids",
                f"wires was {short(wires)}",
                "Pass wires=[uuid, ...] or wires={'from': 'R1.1', 'to': 'R2.2'}.",
            )
        uuids = [str(u)[:64] for u in wires]
        holder = [s for s, sc in _scenes(world) if uuids[0] in sc.wires]
        if not holder:
            raise StructuredToolError(
                f"{tool}: wire {uuids[0]} not found",
                "no wire has that uuid",
                "Re-read the wires and retry.",
                code="not_found",
            )
        sheet = holder[0]
        scene = world.scene(sheet)
        missing = [u for u in uuids if u not in scene.wires]
        if missing:
            raise StructuredToolError(
                f"{tool}: wire {missing[0]} not found on {sheet.human_path}",
                "all wires of one reroute must be on one sheet",
                "Re-read the wires and retry.",
                code="not_found",
            )
        uuids = list(dict.fromkeys(uuids))
    ends = _chain_ends(scene, uuids)
    if len(ends) != 2:
        raise bad(
            f"{tool}: the wires are not one simple route",
            f"they have {len(ends)} open ends (a route has two)",
            "Pass the wires of one connection, from end to end.",
        )
    snapped = [snap_point(p) for p in points]
    pts = simplify(snapped)
    if not pts:
        raise bad(
            f"{tool}: the new route reverses on itself",
            "a segment runs back over the previous one",
            "Pass the corner points in order from one end to the other.",
        )
    if len(pts) < 2:
        raise bad(
            f"{tool}: the new route has no length",
            "all points coincide",
            "Pass the route as [[x, y], ...] from one end to the other.",
        )
    for a, b in itertools.pairwise(pts):
        if abs(a[0] - b[0]) > EPS_MM and abs(a[1] - b[1]) > EPS_MM:
            raise bad(
                f"{tool}: diagonal segment {fmt(a)}-{fmt(b)}",
                "every segment must be horizontal or vertical",
                "Add a corner point.",
            )
    if not (
        (same_point(pts[0], ends[0]) and same_point(pts[-1], ends[1]))
        or (same_point(pts[0], ends[1]) and same_point(pts[-1], ends[0]))
    ):
        raise bad(
            f"{tool}: the new route must keep both ends",
            f"the old route runs {fmt(ends[0])} - {fmt(ends[1])}; the new one "
            f"{fmt(pts[0])} - {fmt(pts[-1])}",
            "Start and end the points exactly on the old route's ends.",
        )
    nets = world.nets(sheet)
    chain_nets = {nets[u] for u in uuids if u in nets}
    if len(chain_nets) > 1:
        raise bad(
            f"{tool}: the wires belong to several nets",
            ", ".join(sorted(chain_nets)),
            "Reroute one connection at a time.",
        )
    chain = [scene.wires[u] for u in uuids]
    joins = [
        p
        for w in scene.wires.values()
        if w.uuid not in uuids
        for p in (w.start, w.end)
        if not any(same_point(p, e) for e in ends)
        and any(on_segment(p, c.start, c.end) for c in chain)
    ]
    joins += [
        p.at
        for p in scene.all_pins()
        if not any(same_point(p.at, e) for e in ends)
        and any(on_segment(p.at, c.start, c.end) for c in chain)
    ]
    on_chain = [
        (at, is_junction)
        for at, is_junction in [(lb.at, False) for lb in scene.labels.values()]
        + [(j, True) for j in scene.junctions.values()]
        if not any(same_point(at, e) for e in ends)
        and any(on_segment(at, c.start, c.end) for c in chain)
    ]
    # labels always join; a junction only when another wire meets it
    joins += [at for at, j in on_chain if not j or not _inner_corner_only(at, chain, scene)]
    if joins:
        raise bad(
            f"{tool}: other items join this route",
            f"something else connects to the route at {fmt(joins[0])} (a wire, pin or "
            "label); moving the route would disconnect it",
            "Reroute only a simple route between two ends (no T-joins or labels on it).",
        )
    trimmed = Scene(
        sheet=scene.sheet,
        symbols=scene.symbols,
        wires={u: w for u, w in scene.wires.items() if u not in uuids},
        labels=scene.labels,
        junctions=scene.junctions,
        no_connects=scene.no_connects,
        sheets=scene.sheets,
    )
    pin_owner = {p.at: s.uuid for s in scene.symbols.values() for p in s.pins}
    own = (
        next((u for at, u in pin_owner.items() if same_point(at, pts[0])), None),
        next((u for at, u in pin_owner.items() if same_point(at, pts[-1])), None),
    )
    problems = route_problems(
        pts, trimmed, world.netlist, chain_nets, item_nets=nets, own_symbols=own
    )
    if problems:
        raise StructuredToolError(
            f"{tool} refused: the new route would change connections",
            problems_summary(problems),
            "Choose corner points that keep clear of other pins, wires, labels and bodies.",
            code="overlap",
            details={"problems": problems[:12]},
        )
    junctions = needed_junctions(pts, trimmed)
    items = [make_wire(a, b) for a, b in itertools.pairwise(pts)]
    items += [make_junction(j) for j in junctions]
    notes = []
    moved = [(r, s) for r, s in zip(points, snapped, strict=True) if not same_point(r, s, 1e-4)]
    if moved:
        notes.append(
            "points snapped to the 1.27 mm grid: "
            + ", ".join(f"{fmt(r)} -> {fmt(s)}" for r, s in moved)
        )
    return ChangeSet(
        tool=tool,
        sheet=sheet,
        stages=[
            Stage(
                "reroute",
                lambda _inp: [Delete(list(uuids), sheet=sheet), Create(items, sheet=sheet)],
            )
        ],
        pre_images={},
        intent=NetIntent(keep_partition=True),
        logical=Logical(1),
        summary={
            "points_mm": [list(p) for p in pts],
            "replaced_wires": list(uuids),
            "sheet": sheet.human_path,
        },
        notes=notes,
    )
