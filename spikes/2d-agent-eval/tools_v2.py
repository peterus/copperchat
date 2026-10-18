"""Tool-set v2 additions (after the v1 runs): sch_find_space, sch_delete, sch_move.

v1 runs showed the agent cannot correct a placement (no move/rotate/delete) and picks
coordinates blind (no free-space query). These are the smallest prototypes that close
those gaps; sch_move only moves symbols whose pins are still unconnected (the spec's
wire-dragging move is 2A-verified but not prototyped here).
"""

from __future__ import annotations

from harness import by_id, update_symbols, v2

import geo
from geo import snap_pt
from tools import (
    ORIENT,
    Ctx,
    ToolError,
    _place_fields,
    _probe,
    _remove,
    log_change,
    pt,
    read_scene,
    resolve_pin,
    snapshot_state,
)


def sch_find_space(ctx: Ctx, args: dict) -> dict:
    """Free position for lib_id (at rotation) as close as possible to `near` (REF.PIN, ref or [x, y]);
    for a pin, the part goes in the direction the pin points, leaving room for the connection."""
    lib_id = str(args.get("lib_id", ""))
    if lib_id not in ctx.lib.symbols:
        raise ToolError(f"symbol {lib_id} not in the libraries", "unknown lib_id", "use lib_search_symbols")
    rotation = int(args.get("rotation", 0)) % 360
    scene = read_scene(ctx.sch)
    near = args.get("near")
    direction = None
    if isinstance(near, str) and "." in near:
        p = resolve_pin(scene, near)
        origin, direction = p.pos, p.out
    elif isinstance(near, str):
        syms = scene.by_ref(near)
        if not syms:
            raise ToolError(f"no symbol {near}", "unknown reference", "")
        b = syms[0].occupied
        origin = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    elif isinstance(near, list) and len(near) == 2:
        origin = tuple(near)
    else:
        raise ToolError("near must be REF.PIN, REF or [x, y]", "missing anchor", "e.g. near='U1.VCC'")
    corridor = None
    if direction:
        tip = (origin[0] + direction[0] * 7.62, origin[1] + direction[1] * 7.62)
        corridor = (
            min(origin[0], tip[0]) - 2.54,
            min(origin[1], tip[1]) - 2.54,
            max(origin[0], tip[0]) + 2.54,
            max(origin[1], tip[1]) + 2.54,
        )
    probe = _probe(ctx, lib_id, (100, 100), rotation, None, with_fields=True)
    # the part's boxes plus the room in front of its own pins (v2.1: else its pins get boxed in)
    rel = [geo.shift(b, -100, -100) for b in probe.boxes + geo.pin_corridors(probe)]
    boxes = scene.text_boxes()
    margin = float(args.get("clearance", 2.54))
    best = None
    for r in range(1, 60):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                at = snap_pt((origin[0] + dx * 2.54, origin[1] + dy * 2.54))
                if direction and (dx * direction[0] + dy * direction[1]) <= 0:
                    continue  # stay on the side the pin points to
                cand = [geo.shift(b, *at) for b in rel]
                if min(b[0] for b in cand) < 10 or min(b[1] for b in cand) < 10:
                    continue
                if geo.any_overlap(cand, boxes, margin):
                    continue
                if corridor and geo.any_overlap(cand, [corridor], 0):
                    continue  # keep the anchor pin's own stub / power symbol / label room free
                box = (min(b[0] for b in cand), min(b[1] for b in cand), max(b[2] for b in cand), max(b[3] for b in cand))
                d = abs(at[0] - origin[0]) + abs(at[1] - origin[1])
                if best is None or d < best[0]:
                    best = (d, at, box)
        if best and r > 3:
            break
    if best is None:
        raise ToolError("no free space found", "the area is full", "pick another anchor")
    return {"lib_id": lib_id, "rotation": rotation, "at": pt(best[1]), "occupies": [round(v, 2) for v in best[2]]}


def _attached(scene, sym_uuids: set[str]) -> tuple[list[str], list[str]]:
    """Wires, labels, junctions, no-connects and power symbols that only serve the given
    symbols' pins (wire chains from their pins up to another symbol's pin)."""
    starts = [p.pos for s in sym_uuids for p in scene.syms[s].pins.values()]
    keep_pts = [p.pos for s in scene.syms.values() if s.uuid not in sym_uuids and not s.power for p in s.pins.values()]
    wires, seen, frontier = set(), {tuple(x) for x in starts}, list(starts)
    while frontier:
        q = frontier.pop()
        if any(geo.same(q, k) for k in keep_pts):
            continue
        for u, a, b in scene.wires:
            if u in wires:
                continue
            for x, y in ((a, b), (b, a)):
                if geo.same(x, q):
                    wires.add(u)
                    if tuple(y) not in seen:
                        seen.add(tuple(y))
                        frontier.append(y)
    pts = [tuple(x) for x in seen]
    other = [u for u, _t, p, _k in scene.labels if any(geo.same(p, z) for z in pts)]
    other += [u for u, p in scene.junctions + scene.no_connects if any(geo.same(p, z) for z in pts)]
    power = [s.uuid for s in scene.syms.values() if s.power and any(geo.same(p.pos, z) for p in s.pins.values() for z in pts)]
    return sorted(wires), sorted(set(other)) + power


def sch_delete(ctx: Ctx, args: dict) -> dict:
    refs = args.get("refs") or []
    scene, before = snapshot_state(ctx)
    syms = {s.uuid for r in refs for s in scene.by_ref(str(r))}
    missing = [r for r in refs if not scene.by_ref(str(r))]
    if missing:
        raise ToolError(f"unknown references {missing}", "not on the sheet", "use sch_list_symbols")
    wires, other = _attached(scene, syms) if args.get("with_attached", True) else ([], [])
    ids = sorted(syms) + wires + other
    _remove(ctx, ids)
    scene2, after = snapshot_state(ctx)
    gone = {k for k in before if k[0] in syms or k[0] in other}
    changed = sorted({after.get(k, "?") for k in before if k not in gone and before[k] != after.get(k)})
    for i in ids:
        log_change(ctx, "delete", "item", i)
    return {
        "deleted": {"symbols": [scene.syms[u].ref for u in syms], "wires": len(wires), "labels_power_junctions_nc": len(other)},
        "nets_renamed_or_split": changed,
        "item_changes": len(ids),
    }


def sch_move(ctx: Ctx, args: dict) -> dict:
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O

    ref = str(args.get("ref", ""))
    scene, before = snapshot_state(ctx)
    syms = scene.by_ref(ref)
    if not syms:
        raise ToolError(f"no symbol {ref}", "unknown reference", "")
    y = syms[0]
    wired = [p.label for p in y.pins.values() if scene.items_at(p.pos) != [f"pin {p.label}"]]
    if wired:
        raise ToolError(
            f"{ref} has connected pins {wired}",
            "the prototype moves only unconnected symbols (no wire dragging)",
            f"sch_delete(refs=['{ref}']) and place it again, then reconnect",
        )
    to = snap_pt(args.get("to") or y.pos)
    rotation = int(args.get("rotation", y.rotation)) % 360
    mirror = args.get("mirror", y.mirror)
    probe = _probe(ctx, y.lib_id, to, rotation, None, with_fields=True)
    others = [s for s in scene.syms.values() if s.uuid != y.uuid]
    clash = [s.ref for s in others if geo.any_overlap(probe.boxes, s.boxes, 1.27)]
    if clash:
        raise ToolError(f"{ref} at {pt(to)} would overlap {clash}", "placement must not overlap", "use sch_find_space")
    s = by_id(ctx.sch, [y.uuid])[y.uuid]
    s.position = v2(*to)
    s.transform.orientation = getattr(O, ORIENT[rotation])
    s.transform.mirror_x = mirror == "x"
    s.transform.mirror_y = mirror == "y"
    _place_fields(s, geo.sym_of(s))
    update_symbols(ctx.sch, [s])
    s = by_id(ctx.sch, [y.uuid])[y.uuid]
    ny = geo.sym_of(s)
    _, after = snapshot_state(ctx)
    log_change(ctx, "modify", "symbol", y.uuid, ref=ref, moved=True)
    return {
        "ref": ref,
        "at": pt(ny.pos),
        "rotation": ny.rotation,
        "mirror": ny.mirror,
        "pins": [{"pin": p.number, "name": p.name, "at": pt(p.pos), "points": geo.DIR_NAME.get(p.out)} for p in ny.pins.values()],
        "item_changes": 1,
    }


def pin_directions(ctx: Ctx, lib_id: str) -> dict:
    """For small symbols: where each pin points at each rotation (v1 runs: LED placed upside down)."""
    out = {}
    for rot in (0, 90, 180, 270):
        y = _probe(ctx, lib_id, (100, 100), rot, None)
        out[str(rot)] = {f"{p.number}{'/' + p.name if p.name else ''}": geo.DIR_NAME.get(p.out) for p in y.pins.values()}
    return out


V2_HANDLERS = {"sch_find_space": sch_find_space, "sch_delete": sch_delete, "sch_move": sch_move}
V2_MUTATING = {"sch_delete", "sch_move"}
