"""THROWAWAY prototypes of the SP2 editing tools (spec §6) for the 2D agent eval.

Not the implementation: no two-phase adapter, no plan/budget enforcement, no snapshots.
What they do keep from the spec, because the eval is about the tool *shape*:
- API-only writes (kipy), coordinates in mm snapped to the 1.27 mm grid;
- computed geometry: pin ends from API data, orthogonal router, collision checks;
- read-back after the push: connectivity from the live GetSchematicNetlist compared with the
  intended merge of pin groups; a mismatch removes the created items (compensation) and
  returns a structured error;
- structured JSON results stating exactly what changed + an item-change count (§7.1).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from harness import by_id, cli_env, kicad_cli, new_kiid, sid, update_symbols, v2

import geo
from geo import Pin, Scene, Sym, groups, net_key, read_scene, snap, snap_pt

PASSIVE_SYMBOLS = re.compile(r"^Device:(R|C|L|C_Polarized|R_Small|C_Small|L_Small|FerriteBead\w*)$")
POWER_LIB = "power"


class ToolError(Exception):
    def __init__(self, what: str, why: str, suggestion: str = "", **extra) -> None:
        super().__init__(what)
        self.payload = {"error": {"what": what, "why": why, "suggestion": suggestion, **extra}}


@dataclass
class Ctx:
    k: object
    sch: object
    lib: object  # kit.Library
    project_dir: Path
    out_dir: Path
    changes: list[dict] = field(default_factory=list)  # change log (§8.1)
    calls: list[dict] = field(default_factory=list)
    renders: int = 0
    version: int = 1  # tool set: 1 = first prototype, 2 = + find_space/delete/move + misuse guards

    @property
    def item_changes(self) -> int:
        return len(self.changes)


# ------------------------------------------------------------------ helpers


def pt(v) -> list[float]:
    return [round(v[0], 3), round(v[1], 3)]


def norm_pin_name(n: str) -> str:
    return re.sub(r"[~{}]", "", n).upper()


def resolve_pin(scene: Scene, spec: str) -> Pin:
    if "." not in spec:
        raise ToolError(f"'{spec}' is not REF.PIN", "pins are addressed as REF.PIN (number or unique name)", "e.g. U1.3 or U1.VIN")
    ref, pin = spec.split(".", 1)
    syms = scene.by_ref(ref)
    if not syms:
        known = sorted({s.ref for s in scene.syms.values() if not s.power})
        raise ToolError(f"no symbol {ref}", "reference not on the sheet", f"known references: {known}")
    pins = [p for s in syms for p in s.pins.values()]
    hit = [p for p in pins if p.number == pin]
    if not hit:
        want = norm_pin_name(pin)
        hit = [p for p in pins if norm_pin_name(p.name) == want]
        if not hit:
            hit = [p for p in pins if want in norm_pin_name(p.name).split("/")]
    if len(hit) != 1:
        listing = [f"{p.number}:{p.name}" for p in pins]
        why = "no such pin" if not hit else "pin name is ambiguous"
        raise ToolError(f"{spec}: {why}", why, f"pins of {ref}: {listing}")
    return hit[0]


def pin_json(p: Pin, nets: dict | None = None, scene: Scene | None = None) -> dict:
    d = {"pin": p.number, "name": p.name, "type": p.etype, "at": pt(p.pos), "points": geo.DIR_NAME.get(p.out, "?")}
    if nets is not None:
        d["net"] = nets.get(p.key)
    if scene is not None:
        d["attached"] = [w for w in scene.items_at(p.pos) if not w.startswith(f"pin {p.label}")]
    return d


def net_members(nets: dict, scene: Scene, name: str) -> list[str]:
    out = []
    for (u, num), n in nets.items():
        if n == name and u in scene.syms:
            out.append(f"{scene.syms[u].ref}.{num}")
    return sorted(out)


def snapshot_state(ctx: Ctx) -> tuple[Scene, dict]:
    scene = read_scene(ctx.sch)
    return scene, geo.partition(ctx.sch, scene)


def expected_partition(before: dict, merges: list[set], new_keys: list) -> set[frozenset]:
    """Groups after merging; new_keys start as singletons (or are listed in a merge)."""
    gs = [set(g) for g in groups(before).values()]
    gs += [{k} for k in new_keys if not any(k in g for g in gs)]
    for m in merges:
        hit = [g for g in gs if g & m]
        union = set(m).union(*hit) if hit else set(m)
        gs = [g for g in gs if not (g & m)] + [union]
    return {frozenset(g) for g in gs}


def verify(
    ctx: Ctx, before: dict, merges: list[set], new_keys: list, created: list[str], label: str, only_old: bool = False
) -> tuple[Scene, dict]:
    """Read back after the push; compensate (remove created items) on a connectivity mismatch."""
    scene, after = snapshot_state(ctx)
    want = expected_partition(before, merges, new_keys)
    got = {frozenset(g) for g in groups(after).values()}
    if only_old:
        old = set(before)
        want = {g & old for g in want if g & old}
        got = {g & old for g in got if g & old}
    if want != got:
        extra = [sorted(f"{scene.syms[u].ref}.{n}" if u in scene.syms else f"?{n}" for u, n in g) for g in got - want]
        missing = [sorted(f"{scene.syms[u].ref}.{n}" if u in scene.syms else f"?{n}" for u, n in g) for g in want - got]
        _remove(ctx, created)
        raise ToolError(
            f"{label}: connectivity mismatch after write — undone",
            "the net set read back from KiCad differs from the intended one",
            "check positions with sch_get_pin / render_view; try via='label' or move a part",
            expected_groups=missing,
            found_groups=extra,
        )
    return scene, after


def _remove(ctx: Ctx, ids: list[str]) -> None:
    if not ids:
        return
    from kipy.proto.common.types import KIID

    kiids = []
    for i in ids:
        k = KIID()
        k.value = i
        kiids.append(k)
    ctx.sch.remove_items_by_id(kiids)
    ctx.changes = [c for c in ctx.changes if c.get("uuid") not in set(ids)]


def log_change(ctx: Ctx, op: str, kind: str, uuid: str, **kw) -> None:
    ctx.changes.append({"op": op, "kind": kind, "uuid": uuid, **kw})


def create(ctx: Ctx, items: list, label: str) -> list:
    c = ctx.sch.begin_commit()
    try:
        made = ctx.sch.create_items(items)
    except BaseException:
        ctx.sch.drop_commit(c)
        raise
    ctx.sch.push_commit(c, label)
    return list(made)


def make_wire(a, b):
    from harness import wire

    w = wire(a, b)
    w.proto.id.CopyFrom(new_kiid())
    return w


def make_label(text: str, at, out: tuple[int, int], kind: str):
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLabelSpinStyle as S
    from kipy.schematic_types import GlobalLabel, LocalLabel

    lb = GlobalLabel() if kind == "global" else LocalLabel()
    lb.proto.id.CopyFrom(new_kiid())
    lb.text.value = text
    lb.position = v2(*at)
    lb.text.position = v2(*at)
    # text extends away from the pin: spin style by the outward direction
    lb.spin_style = {(1, 0): S.SLSS_RIGHT, (-1, 0): S.SLSS_LEFT, (0, -1): S.SLSS_UP, (0, 1): S.SLSS_BOTTOM}[out]
    return lb


def make_junction(at):
    from harness import junction

    j = junction(at)
    j.proto.id.CopyFrom(new_kiid())
    return j


def wire_items(points) -> list:
    return [make_wire(a, b) for a, b in zip(points, points[1:], strict=False)]


def needs_junction(scene: Scene, p: Pin) -> bool:
    """A pin end that already has a wire gets a second one -> KiCad would draw a junction."""
    return sum(1 for _u, a, b in scene.wires if geo.same(a, p.pos) or geo.same(b, p.pos)) >= 1


# ------------------------------------------------------------------ read-only tools


def lib_search_symbols(ctx: Ctx, args: dict) -> dict:
    q = str(args.get("query", "")).strip()
    if not q:
        raise ToolError("empty query", "query is required", "e.g. 'LDO 3.3V SOT-223' or 'AMS1117'")
    limit = max(1, min(int(args.get("limit", 10)), 25))
    rows = ctx.lib.search(q, args.get("library"), limit)
    return {"query": q, "results": rows, "note": "pin_count null = derived symbol; lib_get_symbol shows its pins"}


def lib_get_symbol(ctx: Ctx, args: dict) -> dict:
    lib_id = str(args.get("lib_id", ""))
    rec = ctx.lib.symbols.get(lib_id)
    if rec is None:
        close = [r["lib_id"] for r in ctx.lib.search(lib_id.split(":")[-1], None, 5)]
        raise ToolError(f"symbol {lib_id} not in the libraries", "unknown lib_id", f"closest: {close}")
    c = ctx.sch.begin_commit()  # dropped-commit probe placement (2B finding 3): flattened pins
    try:
        s = ctx.sch.place_symbol_from_library(lib_id, v2(0, 0))
        y = geo.sym_of(s)
        units = sorted({ch.unit for ch in s.definition.items if ch.unit})
    finally:
        ctx.sch.drop_commit(c)
    extra = {}
    if ctx.version >= 2 and len(y.pins) <= 4:
        from tools_v2 import pin_directions

        extra["pin_directions_by_rotation"] = pin_directions(ctx, lib_id)
    return extra | {
        "lib_id": lib_id,
        "description": rec.description,
        "reference_prefix": y.ref.rstrip("?"),
        "default_footprint": rec.footprint or None,
        "footprint_filters": rec.footprint_filters[:6],
        "unit_count": rec.unit_count,
        "units": units or [1],
        "size_mm": [round(y.bbox[2] - y.bbox[0], 2), round(y.bbox[3] - y.bbox[1], 2)] if y.bbox else None,
        "pins_at_rotation_0": [
            {"pin": p.number, "name": p.name, "type": p.etype, "offset": pt(p.pos), "points": geo.DIR_NAME.get(p.out)}
            for p in y.pins.values()
        ],
        "note": "offset = pin end relative to the symbol origin (mm, y down) for unit 1 at rotation 0",
    }


def lib_search_footprints(ctx: Ctx, args: dict) -> dict:
    q = str(args.get("query", "")).strip()
    return {"query": q, "results": ctx.lib.search_footprints(q, max(1, min(int(args.get("limit", 10)), 25)))}


def sch_list_symbols(ctx: Ctx, args: dict) -> dict:
    scene, nets = snapshot_state(ctx)
    out = []
    for s in sorted(scene.syms.values(), key=lambda s: s.ref):
        if s.power and not args.get("include_power"):
            continue
        unconnected = [p.number for p in s.pins.values() if str(nets.get(p.key, "")).startswith("unconnected-") or p.key not in nets]
        out.append(
            {
                "ref": s.ref,
                "uuid": s.uuid,
                "lib_id": s.lib_id,
                "value": s.value,
                "footprint": s.footprint,
                "at": pt(s.pos),
                "rotation": s.rotation,
                "mirror": s.mirror,
                "bbox": list(s.bbox) if s.bbox else None,
                "unconnected_pins": unconnected,
            }
        )
    return {"symbols": out, "power_symbols": sum(1 for s in scene.syms.values() if s.power)}


def sch_get_pin(ctx: Ctx, args: dict) -> dict:
    scene, nets = snapshot_state(ctx)
    pins = args.get("pins") or [args.get("pin")]
    out = []
    for spec in pins:
        p = resolve_pin(scene, str(spec))
        d = pin_json(p, nets, scene)
        d["ref"] = p.ref
        name = nets.get(p.key)
        d["net_members"] = [m for m in net_members(nets, scene, name) if m != p.label] if name else []
        d["free"] = not d["attached"]
        out.append(d)
    return {"pins": out}


def sch_get_net(ctx: Ctx, args: dict) -> dict:
    scene, nets = snapshot_state(ctx)
    want = str(args.get("net", ""))
    names = sorted({n for n in nets.values() if not n.startswith("unconnected-")})
    hit = [n for n in names if n == want or net_key(n) == net_key(want)]
    if not hit:
        raise ToolError(f"no net {want}", "unknown net name", f"named nets: {names[:40]}")
    return {"nets": [{"name": n, "members": net_members(nets, scene, n)} for n in hit]}


# ------------------------------------------------------------------ place


def _next_ref(scene: Scene, prefix: str) -> str:
    used = {s.ref for s in scene.syms.values()}
    n = 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


ORIENT = {0: "SSO_0", 90: "SSO_90", 180: "SSO_180", 270: "SSO_270"}


def _probe(ctx: Ctx, lib_id: str, at, rotation: int, unit: int | None, with_fields: bool = False) -> Sym:
    """Dropped-commit placement: what the symbol would occupy (fields placed like the real one)."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O

    c = ctx.sch.begin_commit()
    try:
        s = ctx.sch.place_symbol_from_library(lib_id, v2(*at), orientation=getattr(O, ORIENT[rotation]), unit=unit)
        if with_fields:
            s.reference_field.text.value = s.reference_field.text.value.replace("?", "99")
            _place_fields(s, geo.sym_of(s))
        return geo.sym_of(s)
    finally:
        ctx.sch.drop_commit(c)


def _free_spot(scene: Scene, rel_boxes: list, at, margin: float) -> tuple[float, float] | None:
    boxes = scene.text_boxes()
    for r in range(0, 40):
        ring = [(dx, dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1) if max(abs(dx), abs(dy)) == r]
        ring.sort(key=lambda d: abs(d[0]) + abs(d[1]))
        for dx, dy in ring:
            p = (snap(at[0] + dx * 2.54), snap(at[1] + dy * 2.54))
            cand = [geo.shift(b, *p) for b in rel_boxes]
            if min(b[0] for b in cand) < 10 or min(b[1] for b in cand) < 10:
                continue
            if not geo.any_overlap(cand, boxes, margin):
                return p
    return None


def _place_fields(s, y: Sym) -> None:
    """Headless placement does no field auto-placement (2A Q2): stack Reference and Value
    on a side of the body that has no pins, text horizontal."""
    from kipy.proto.common.types import HorizontalAlignment as H

    x0, y0, x1, y1 = y.bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    used = {geo.DIR_NAME.get(p.out) for p in y.pins.values()}
    wide = (x1 - x0) >= (y1 - y0)
    order = ["up", "down", "right", "left"] if wide else ["right", "left", "up", "down"]
    side = next((d for d in order if d not in used), "corner")
    spots = {
        "up": ((cx, y0 - 3.81), (cx, y0 - 1.27), H.HA_CENTER),
        "down": ((cx, y1 + 1.905), (cx, y1 + 4.445), H.HA_CENTER),
        "right": ((x1 + 0.635, cy - 1.27), (x1 + 0.635, cy + 1.27), H.HA_LEFT),
        "left": ((x0 - 0.635, cy - 1.27), (x0 - 0.635, cy + 1.27), H.HA_RIGHT),
        "corner": ((x1 + 0.635, y0 - 3.81), (x1 + 0.635, y0 - 1.27), H.HA_LEFT),
    }
    ref_at, val_at, align = spots[side]
    rotated = y.rotation in (90, 270)
    angle = 90.0 if rotated else 0.0  # field angles are relative to the symbol
    for f, at in ((s.reference_field, ref_at), (s.value_field, val_at)):
        f.text.attributes.angle = angle
        x = at[0]
        if rotated and align != H.HA_CENTER:  # justification is transformed with the symbol: centre it
            w = 1.1 * len(f.text.value)
            x = at[0] + (w / 2 if align == H.HA_LEFT else -w / 2)
        f.text.attributes.horizontal_alignment = H.HA_CENTER if rotated else align
        f.text.position = v2(round(x, 3), round(at[1], 3))


def _check_footprint(ctx: Ctx, lib_id: str, footprint: str | None) -> list[str]:
    notes = []
    if footprint and footprint not in ctx.lib.footprints:
        close = ctx.lib.search_footprints(footprint.split(":")[-1].split("_")[0] + " " + " ".join(re.findall(r"\d{4}", footprint)), 5)
        raise ToolError(f"footprint {footprint} not in the libraries", "unknown footprint", f"closest: {close}; use lib_search_footprints")
    if PASSIVE_SYMBOLS.match(lib_id):
        from libdata import package_of

        if not footprint:
            notes.append("no footprint set (part preference: passives 0805)")
        elif package_of(footprint.split(":")[-1]) != "0805":
            notes.append(f"preference_deviation: {footprint} is not 0805 (part preference: passives 0805) — justify to the user")
    return notes


def is_power_net(ctx: Ctx, net: str) -> bool:
    return f"{POWER_LIB}:{net}" in ctx.lib.symbols


def sch_place_symbol(ctx: Ctx, args: dict) -> dict:
    lib_id = str(args.get("lib_id", ""))
    if ctx.version >= 2 and lib_id.startswith(f"{POWER_LIB}:"):
        raise ToolError(
            f"{lib_id} is a power symbol",
            "power symbols are attached to pins, not placed loose",
            f"sch_add_power(net='{lib_id.split(':')[1]}', at=[REF.PIN, ...])",
        )
    if lib_id not in ctx.lib.symbols:
        close = [r["lib_id"] for r in ctx.lib.search(lib_id.split(":")[-1], None, 5)]
        raise ToolError(f"symbol {lib_id} not in the libraries", "unknown lib_id", f"closest: {close}; never invent a lib_id")
    at = args.get("at")
    if not (isinstance(at, list | tuple) and len(at) == 2):
        raise ToolError("at must be [x_mm, y_mm]", "missing position", "e.g. at=[101.6, 76.2]")
    at = snap_pt(at)
    rotation = int(args.get("rotation", 0)) % 360
    if rotation not in ORIENT:
        raise ToolError("rotation must be 0/90/180/270", "bad rotation", "")
    mirror = args.get("mirror") or "none"
    unit = args.get("unit")
    footprint = args.get("footprint")
    notes = _check_footprint(ctx, lib_id, footprint or ctx.lib.symbols[lib_id].footprint)
    scene, before = snapshot_state(ctx)
    probe = _probe(ctx, lib_id, at, rotation, unit, with_fields=True)
    margin = 1.27
    clash = [s.ref for s in scene.syms.values() if geo.any_overlap(probe.boxes, s.boxes, margin)]
    clash += [f"label/text at {pt(b[:2])}" for b in scene.label_boxes() if geo.any_overlap(probe.boxes, [b], 0.5)]
    pin_clash = [f"{p.number}@{pt(p.pos)}: {scene.items_at(p.pos)}" for p in probe.pins.values() if scene.items_at(p.pos)]
    if clash or pin_clash:
        spot = _free_spot(scene, [geo.shift(b, -at[0], -at[1]) for b in probe.boxes], at, margin)
        raise ToolError(
            f"{lib_id} at {pt(at)} would overlap {clash or pin_clash}",
            "placement must not overlap other symbols (body + pins + 1.27 mm) or land a pin on a connection",
            f"nearest free position: {pt(spot) if spot else 'none found'}",
            size_mm=[round(probe.bbox[2] - probe.bbox[0], 2), round(probe.bbox[3] - probe.bbox[1], 2)],
        )
    prefix = re.sub(r"[0-9?]+$", "", probe.ref) or "U"
    ref = args.get("reference") or _next_ref(scene, prefix)
    if any(s.ref == ref for s in scene.syms.values()):
        raise ToolError(
            f"reference {ref} already used", "references must be unique", f"omit reference to auto-assign ({_next_ref(scene, prefix)})"
        )
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O

    s = ctx.sch.place_symbol_from_library(lib_id, v2(*at), orientation=getattr(O, ORIENT[rotation]), unit=unit, reference=ref)
    uuid = sid(s)
    s = by_id(ctx.sch, [uuid])[uuid]
    if mirror in ("x", "y"):
        if rotation == 180 and mirror == "x":  # 2A Q1: 180+mirror_x collapses -> send 0+mirror_y
            s.transform.orientation = O.SSO_0
            s.transform.mirror_y = True
        else:
            s.transform.mirror_x = mirror == "x"
            s.transform.mirror_y = mirror == "y"
    if args.get("value"):
        s.value_field.text.value = str(args["value"])
    if footprint:
        s.footprint_field.text.value = footprint
    _set_user_fields(s, args.get("fields") or {})
    y = geo.sym_of(s)
    _place_fields(s, y)
    update_symbols(ctx.sch, [s])
    s = by_id(ctx.sch, [uuid])[uuid]
    y = geo.sym_of(s)
    # new pins may join global power nets through hidden power pins; old nets must not change
    verify(ctx, before, [], [p.key for p in y.pins.values()], [uuid], "sch_place_symbol", only_old=True)
    log_change(ctx, "add", "symbol", uuid, ref=ref, lib_id=lib_id)
    return {
        "placed": {
            "ref": ref,
            "uuid": uuid,
            "lib_id": lib_id,
            "value": y.value,
            "footprint": y.footprint,
            "at": pt(y.pos),
            "rotation": y.rotation,
            "mirror": y.mirror,
            "bbox": list(y.bbox),
            "pins": [pin_json(p) for p in y.pins.values()],
        },
        "notes": notes,
        "item_changes": 1,
    }


def _set_user_fields(s, fields: dict) -> None:
    from kipy.schematic_types import SchematicField

    std = {"value": s.value_field, "footprint": s.footprint_field, "datasheet": s.datasheet_field, "description": s.description_field}
    user = list(s.user_fields)
    for name, val in fields.items():
        if name.lower() in std:
            std[name.lower()].text.value = str(val)
            continue
        existing = next((f for f in user if f.name == name), None)
        if existing is not None:
            existing.text.value = str(val)
            continue
        f = SchematicField()
        f.name = name
        f.text.value = str(val)
        f.text.position = s.position
        f.visible = False
        user.append(f)
    s.user_fields = user


def sch_set_fields(ctx: Ctx, args: dict) -> dict:
    scene = read_scene(ctx.sch)
    ref = str(args.get("ref", ""))
    syms = [s for s in scene.syms.values() if s.ref == ref]
    if not syms:
        raise ToolError(f"no symbol {ref}", "unknown reference", "use sch_list_symbols")
    fields = dict(args.get("fields") or {})
    notes = []
    if "footprint" in {k.lower() for k in fields}:
        fp = next(v for k, v in fields.items() if k.lower() == "footprint")
        notes = _check_footprint(ctx, syms[0].lib_id, fp)
    protos = []
    before = {}
    for y in syms:  # all units of a multi-unit reference (2A Q2: the API does not sync them)
        s = by_id(ctx.sch, [y.uuid])[y.uuid]
        before = {"value": s.value_field.text.value, "footprint": s.footprint_field.text.value}
        _set_user_fields(s, fields)
        if "dnp" in args:
            s.attributes.do_not_populate = bool(args["dnp"])
        protos.append(s)
    update_symbols(ctx.sch, protos)
    after = by_id(ctx.sch, [syms[0].uuid])[syms[0].uuid]
    for y in syms:
        log_change(ctx, "modify", "symbol", y.uuid, ref=ref, fields=list(fields))
    return {
        "ref": ref,
        "before": before,
        "after": {
            "value": after.value_field.text.value,
            "footprint": after.footprint_field.text.value,
            "fields": {f.name: f.text.value for f in after.user_fields},
        },
        "notes": notes,
        "item_changes": len(syms),
    }


# ------------------------------------------------------------------ connect / label / power


DIRS = [(1, 0), (-1, 0), (0, -1), (0, 1)]


def anchors(scene: Scene, p: Pin) -> list[tuple[tuple, list, int]]:
    """Where a stub for pin p can start: the pin end (outward only) if nothing leaves it that
    way, else the free ends/corners of the wires already attached to it.
    Returns [(point, free directions, items already there)]."""
    pts, frontier = {p.pos}, [p.pos]
    while frontier:  # wire chain reachable from the pin through shared end points
        q = frontier.pop()
        for _u, a, b in scene.wires:
            for x, y in ((a, b), (b, a)):
                if geo.same(x, q) and not any(geo.same(y, z) for z in pts):
                    pts.add(y)
                    frontier.append(y)
    out = []
    for q in sorted(pts, key=lambda z: (not geo.same(z, p.pos), abs(z[0] - p.pos[0]) + abs(z[1] - p.pos[1]))):
        used = set()
        for _u, a, b in scene.wires:
            for x, y in ((a, b), (b, a)):
                if geo.same(x, q):
                    used.add((int((y[0] > x[0]) - (y[0] < x[0])), int((y[1] > x[1]) - (y[1] < x[1]))))
        n_items = len(scene.items_at(q))
        dirs = [p.out] if geo.same(q, p.pos) else DIRS
        dirs = [d for d in dirs if d not in used]
        if dirs:
            out.append((q, dirs, n_items))
    return out


def _stub_and_label(ctx: Ctx, scene: Scene, p: Pin, net: str, kind: str) -> tuple[list, list[str]]:
    """Items for a stub + label at pin p (or at the free end of a wire already on it)."""
    for q, dirs, n_items in anchors(scene, p):
        for d in dirs:
            for length in (2.54, 5.08):
                end = snap_pt((q[0] + d[0] * length, q[1] + d[1] * length))
                at_pin = geo.same(q, p.pos)
                if geo.path_problems([q, end], scene, [p], {p.sym_uuid}) if at_pin else _blocked(scene, [q, end], q):
                    continue
                if scene.items_at(end):
                    continue
                tlen = 1.0 * len(net) + 2.0  # room for the label text along d
                tip = (end[0] + d[0] * tlen, end[1] + d[1] * tlen)
                if any(geo.seg_hits_box(end, tip, b, 0.0) for b in scene.text_boxes()):
                    continue
                items = [make_wire(q, end), make_label(net, end, d, kind)]
                if n_items >= 2:
                    items.append(make_junction(q))
                return items, [] if geo.same(q, p.pos) else [f"label attached at the wire end {pt(q)} (pin already wired)"]
    return [], ["no room"]


def _blocked(scene: Scene, pts, start) -> list[str]:
    """path_problems for a path that starts on an existing wire end (not a pin)."""
    fake = Pin("-", "-", "-", "", "", start, (0, 0))
    return geo.path_problems(pts, scene, [fake], set())


def _label_merges(before: dict, scene: Scene, net: str, keys: list, kind: str = "local") -> list[set]:
    """A label joins every group that already carries the same name (root sheet: local
    label nets are '/NAME', global label and power nets 'NAME')."""
    want = net if kind == "global" else f"/{net}"
    named = [g for name, g in groups(before).items() if name == want]
    m = set(keys)
    for g in named:
        m |= set(g)
    return [m]


def sch_add_label(ctx: Ctx, args: dict) -> dict:
    net = str(args.get("net", "")).strip()
    if ctx.version >= 2 and is_power_net(ctx, net):
        raise ToolError(
            f"{net} is a power net",
            "a label with a power-net name does not join the rail (local vs global)",
            f"sch_add_power(net='{net}', at=[...])",
        )
    if not re.fullmatch(r"[A-Za-z0-9_+\-./]+", net or "-"):
        raise ToolError(f"bad net name {net!r}", "net names: letters, digits, _ + - . /", "e.g. I2C1_SDA")
    kind = args.get("kind", "local")
    scene, before = snapshot_state(ctx)
    at = args.get("at")
    targets = at if isinstance(at, list) and at and isinstance(at[0], str) else [at]
    items, notes, keys = [], [], []
    for t in targets:
        if not isinstance(t, str):
            raise ToolError("at must be REF.PIN (or a list of them)", "the prototype labels pins only", "e.g. at='U1.5'")
        p = resolve_pin(scene, t)
        its, n = _stub_and_label(ctx, scene, p, net, kind)
        if not its:
            raise ToolError(f"no room for a label at {t}", "stub/label would hit other items", "move the part or use another pin side")
        items += its
        notes += n
        keys.append(p.key)
    made = create(ctx, items, f"label {net}")
    ids = [sid(m) for m in made]
    verify(ctx, before, _label_merges(before, scene, net, keys, kind), [], ids, "sch_add_label")
    for m in made:
        log_change(ctx, "add", type(m).__name__, sid(m), net=net)
    _, nets = snapshot_state(ctx)
    return {
        "net": net,
        "created": [{"kind": type(m).__name__, "uuid": sid(m)} for m in made],
        "members": net_members(nets, read_scene(ctx.sch), nets.get(keys[0])),
        "connectivity_verified": True,
        "notes": notes,
        "item_changes": len(made),
    }


def _power_body_dir(ctx: Ctx, lib_id: str) -> tuple[int, int]:
    y = _probe(ctx, lib_id, (0, 0), 0, None)
    cy = (y.bbox[1] + y.bbox[3]) / 2
    return (0, 1) if cy > 0 else (0, -1)


def _next_power_ref(scene: Scene, prefix: str) -> str:
    """Headless placement annotates every power symbol as #PWR01 (duplicates): number them."""
    nums = [int(m.group(1)) for s in scene.syms.values() if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", s.ref))]
    used = {s.ref for s in scene.syms.values()}
    n = max(nums, default=0) + 1
    while f"{prefix}{n:02d}" in used:
        n += 1
    return f"{prefix}{n:02d}"


def _power_plans(scene: Scene, p: Pin, body):
    """Candidate (wire points, symbol position, rotation) for a power symbol on pin p:
    upright symbols first (supplies up, GND down), then shorter wires, flipped ones last."""
    cands = []
    for ai, (q, dirs, n_items) in enumerate(anchors(scene, p)):
        is_pin = geo.same(q, p.pos)
        for d in dirs:
            for s_len in (2.54, 5.08, 7.62):
                end = snap_pt((q[0] + d[0] * s_len, q[1] + d[1] * s_len))
                if d == body:  # straight stub, symbol upright
                    cands.append(([q, end], end, 0, n_items, ai))
                elif d == (-body[0], -body[1]):
                    if is_pin:  # pin points against the body: flip the symbol
                        cands.append(([q, end], end, 180, n_items, ai))
                else:  # sideways: stub out, then turn towards the body direction (or flipped, away)
                    at = snap_pt((end[0], end[1] + body[1] * 2.54))
                    cands.append(([q, end, at], at, 0, n_items, ai))
                    at = snap_pt((end[0], end[1] - body[1] * 2.54))
                    cands.append(([q, end, at], at, 180, n_items, ai))
    cands.sort(key=lambda c: (c[2] != 0, c[4], sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(c[0], c[0][1:], strict=False))))
    for pts, at, rot, n_items, _ai in cands:
        yield pts, at, rot, n_items


def sch_add_power(ctx: Ctx, args: dict) -> dict:
    net = str(args.get("net", "")).strip()
    lib_id = args.get("symbol") or f"{POWER_LIB}:{net}"
    if lib_id not in ctx.lib.symbols:
        close = [r["lib_id"] for r in ctx.lib.search(net, POWER_LIB, 8)]
        raise ToolError(f"no power symbol {lib_id}", "power symbols come from the power: library", f"closest: {close}")
    scene, before = snapshot_state(ctx)
    targets = args.get("at")
    targets = targets if isinstance(targets, list) else [targets]
    body = _power_body_dir(ctx, lib_id)
    prefix = "#FLG" if "FLAG" in lib_id else "#PWR"
    results, created_all, merges, new_keys = [], [], [], []
    from kipy.proto.schematic.schematic_types_pb2 import SchematicSymbolOrientation as O

    for t in targets:
        p = resolve_pin(scene, str(t))
        plan = None
        for pts, at, rot, n_items in _power_plans(scene, p, body):
            probs = geo.path_problems(pts, scene, [p], {p.sym_uuid}) if geo.same(pts[0], p.pos) else _blocked(scene, pts, pts[0])
            if probs or scene.items_at(at):
                continue
            probe = _probe(ctx, lib_id, at, rot, None)
            if geo.any_overlap(probe.boxes, scene.text_boxes(), 0.2):
                continue
            if any(geo.seg_hits_box(a, b, probe.bbox, 0.3) for a, b in zip(pts, pts[1:-1], strict=False)):
                continue
            plan = (pts, at, rot, n_items)
            break
        if plan is None:  # all-or-nothing: undo the pins already done in this call (v2.1)
            _remove(ctx, created_all)
            raise ToolError(
                f"no room for {lib_id} at {t}" + (" — nothing was changed by this call" if created_all else ""),
                "stub/symbol would hit other items",
                "move or re-place that part (sch_find_space), then call again",
            )
        pts, at, rot, n_items = plan
        items = wire_items(pts) + ([make_junction(pts[0])] if n_items >= 2 else [])
        made = create(ctx, items, f"power stub {net}")
        sym = ctx.sch.place_symbol_from_library(
            lib_id, v2(*at), orientation=getattr(O, ORIENT[rot]), reference=_next_power_ref(scene, prefix)
        )
        ids = [sid(m) for m in made] + [sid(sym)]
        created_all += ids
        y = geo.sym_of(by_id(ctx.sch, [sid(sym)])[sid(sym)])
        results.append(
            {"pin": p.label, "power_symbol": y.ref, "uuid": y.uuid, "at": pt(at), "rotation": rot, "wire_segments": len(pts) - 1}
        )
        merges.append({p.key, *[q.key for q in y.pins.values()]})
        new_keys += [q.key for q in y.pins.values()]
        scene = read_scene(ctx.sch)
    if "FLAG" not in lib_id:  # all pins join one global net with every existing group of that name
        merges = [set().union(*merges, *[g for name, g in groups(before).items() if name == net])]
    verify(ctx, before, merges, new_keys, created_all, "sch_add_power")
    for i in created_all:
        log_change(ctx, "add", "power", i, net=net)
    return {"net": net, "symbol": lib_id, "placed": results, "connectivity_verified": True, "item_changes": len(created_all)}


def sch_connect(ctx: Ctx, args: dict) -> dict:
    via = args.get("via", "auto")
    scene, before = snapshot_state(ctx)
    pa = resolve_pin(scene, str(args.get("from", "")))
    to = str(args.get("to", ""))
    if "." not in to or not scene.by_ref(to.split(".", 1)[0]):
        # 'to' is a net name -> label at the pin (v2: power symbol for a power net)
        if ctx.version >= 2 and is_power_net(ctx, to):
            return sch_add_power(ctx, {"net": to, "at": [pa.label]}) | {"mode": "power symbol (to = power net)"}
        return sch_add_label(ctx, {"net": to, "at": pa.label, "kind": args.get("kind", "local")}) | {"mode": "label (to = net name)"}
    pb = resolve_pin(scene, to)
    if pa.key == pb.key:
        raise ToolError("from and to are the same pin", "nothing to connect", "")
    if before.get(pa.key) and before.get(pa.key) == before.get(pb.key) and not before[pa.key].startswith("unconnected-"):
        return {"already_connected": True, "net": before[pa.key], "item_changes": 0}
    na, nb = before.get(pa.key, ""), before.get(pb.key, "")
    named = [n for n in (na, nb) if n and not n.startswith("unconnected-") and not n.startswith("Net-(")]
    if len({net_key(n) for n in named}) > 1:
        raise ToolError(
            f"{pa.label} is on {na}, {pb.label} on {nb}",
            "connecting would merge two named nets",
            "check the intent; rename one net or connect to the right pin",
        )
    path, reasons = (None, [])
    if via in ("auto", "wire"):
        path, reasons = geo.route(pa, pb, scene)
    if path:
        items = wire_items(path)
        junc = [make_junction(p.pos) for p in (pa, pb) if needs_junction(scene, p)]
        made = create(ctx, items + junc, f"connect {pa.label}-{pb.label}")
        ids = [sid(m) for m in made]
        verify(ctx, before, [{pa.key, pb.key}], [], ids, "sch_connect")
        for m in made:
            log_change(ctx, "add", type(m).__name__, sid(m))
        _, nets = snapshot_state(ctx)
        return {
            "mode": "wire",
            "points": [pt(p) for p in path],
            "created": [{"kind": type(m).__name__, "uuid": sid(m)} for m in made],
            "net": nets.get(pa.key),
            "connectivity_verified": True,
            "item_changes": len(made),
        }
    if via == "wire":
        raise ToolError(
            f"no clean wire route {pa.label} -> {pb.label}",
            f"every candidate route (<= 4 segments) hits something: {reasons}",
            "use via='label' (or 'auto'), or move a part so the pins face each other",
        )
    net = args.get("net_name") or (named[0].rsplit("/", 1)[-1] if named else None)
    if not net:
        raise ToolError(
            f"no clean wire route {pa.label} -> {pb.label}; a label fallback needs a net name",
            f"route blocked: {reasons}",
            "call again with net_name='SOME_NET' (or via='wire' after moving a part)",
        )
    res = sch_add_label(ctx, {"net": net, "at": [pa.label, pb.label], "kind": args.get("kind", "local")})
    return res | {"mode": "label (no clean wire route)", "route_blocked_by": reasons}


def sch_no_connect(ctx: Ctx, args: dict) -> dict:
    from harness import no_connect

    scene, before = snapshot_state(ctx)
    items = []
    for spec in args.get("pins") or []:
        p = resolve_pin(scene, str(spec))
        if scene.items_at(p.pos) != [f"pin {p.label}"]:
            raise ToolError(f"{p.label} is connected", "no-connect only on a free pin", f"attached: {scene.items_at(p.pos)}")
        n = no_connect(p.pos)
        n.proto.id.CopyFrom(new_kiid())
        items.append(n)
    made = create(ctx, items, "no-connect")
    for m in made:
        log_change(ctx, "add", "no_connect", sid(m))
    verify(ctx, before, [], [], [sid(m) for m in made], "sch_no_connect")
    return {"created": len(made), "item_changes": len(made)}


# ------------------------------------------------------------------ checks: render + ERC


def _saved_copy(ctx: Ctx) -> Path:
    ctx.sch.save()
    dst = Path(tempfile.mkdtemp(prefix="s2d-copy-")) / ctx.project_dir.name
    shutil.copytree(ctx.project_dir, dst, ignore=shutil.ignore_patterns("*.lck"))
    return dst


def erc(ctx: Ctx) -> dict:
    copy = _saved_copy(ctx)
    sch_file = next(copy.glob("*.kicad_sch"))
    out = copy / "erc.json"
    subprocess.run(
        [kicad_cli(), "sch", "erc", "--format", "json", "--severity-all", "-o", str(out), str(sch_file)],
        env=cli_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    data = json.loads(out.read_text())
    vs = []
    for sheet in data.get("sheets", []):
        for v in sheet.get("violations", []):
            vs.append(
                {
                    "severity": v.get("severity"),
                    "type": v.get("type"),
                    "description": v.get("description"),
                    "items": [i.get("description") for i in v.get("items", [])],
                    "at": [round(i["pos"]["x"], 2), round(i["pos"]["y"], 2)] if (i := next(iter(v.get("items", [])), None)) else None,
                }
            )
    return {
        "errors": sum(1 for v in vs if v["severity"] == "error"),
        "warnings": sum(1 for v in vs if v["severity"] == "warning"),
        "violations": vs,
        "coordinate_units": data.get("coordinate_units"),
    }


def run_erc(ctx: Ctx, args: dict) -> dict:
    return erc(ctx)


def render_png(ctx: Ctx, region=None, max_px: int = 1400, name: str | None = None) -> tuple[bytes, dict]:
    from PIL import Image, ImageDraw

    copy = _saved_copy(ctx)
    sch_file = next(copy.glob("*.kicad_sch"))
    out_dir = copy / "png"
    dpi = 200
    subprocess.run(
        [kicad_cli(), "sch", "export", "png", "--exclude-drawing-sheet", "--dpi", str(dpi), "-o", str(out_dir), str(sch_file)],
        env=cli_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    png = next(out_dir.glob("*.png"))
    img = Image.open(png).convert("RGB")
    px_per_mm = dpi / 25.4
    scene = read_scene(ctx.sch)
    if region:
        x0, y0, x1, y1 = region
    else:
        boxes = [s.bbox for s in scene.syms.values() if s.bbox]
        pts = [p for _u, a, b in scene.wires for p in (a, b)] + [p for _u, _t, p, _k in scene.labels]
        if not boxes and not pts:
            x0, y0, x1, y1 = 0, 0, 297, 210
        else:
            xs = [b[0] for b in boxes] + [b[2] for b in boxes] + [p[0] for p in pts]
            ys = [b[1] for b in boxes] + [b[3] for b in boxes] + [p[1] for p in pts]
            x0, y0, x1, y1 = min(xs) - 15, min(ys) - 12, max(xs) + 15, max(ys) + 12
    x0, y0 = max(0, x0), max(0, y0)
    crop = img.crop((int(x0 * px_per_mm), int(y0 * px_per_mm), int(x1 * px_per_mm), int(y1 * px_per_mm)))
    # mm ruler every 10 mm along the top and left edges so positions can be read off
    d = ImageDraw.Draw(crop)
    for xm in range(int(x0 // 10 + 1) * 10, int(x1), 10):
        x = (xm - x0) * px_per_mm
        d.line([(x, 0), (x, 12)], fill=(200, 0, 0), width=2)
        d.text((x + 2, 12), str(xm), fill=(200, 0, 0))
    for ym in range(int(y0 // 10 + 1) * 10, int(y1), 10):
        y = (ym - y0) * px_per_mm
        d.line([(0, y), (12, y)], fill=(200, 0, 0), width=2)
        d.text((14, y - 5), str(ym), fill=(200, 0, 0))
    scale = min(1.0, max_px / max(crop.size))
    if scale < 1.0:
        crop = crop.resize((int(crop.size[0] * scale), int(crop.size[1] * scale)), Image.LANCZOS)
    import io

    buf = io.BytesIO()
    crop.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    if name:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        (ctx.out_dir / name).write_bytes(data)
    return data, {"region_mm": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)], "size_px": list(crop.size)}


def render_view(ctx: Ctx, args: dict) -> tuple[bytes, dict]:
    region = args.get("region")
    if isinstance(region, dict):
        region = [region.get("x0"), region.get("y0"), region.get("x1"), region.get("y1")]
    refs = args.get("around")
    if refs:
        scene = read_scene(ctx.sch)
        boxes = [s.bbox for r in refs for s in scene.by_ref(r) if s.bbox]
        if boxes:
            m = float(args.get("margin", 15))
            region = [
                min(b[0] for b in boxes) - m,
                min(b[1] for b in boxes) - m,
                max(b[2] for b in boxes) + m,
                max(b[3] for b in boxes) + m,
            ]
    ctx.renders += 1
    return render_png(ctx, region, name=f"render-{ctx.renders:02d}.png")


MUTATING = {
    "sch_place_symbol",
    "sch_set_fields",
    "sch_connect",
    "sch_add_label",
    "sch_add_power",
    "sch_no_connect",
    "sch_delete",
    "sch_move",
}
HANDLERS = {
    "lib_search_symbols": lib_search_symbols,
    "lib_get_symbol": lib_get_symbol,
    "lib_search_footprints": lib_search_footprints,
    "sch_list_symbols": sch_list_symbols,
    "sch_get_pin": sch_get_pin,
    "sch_get_net": sch_get_net,
    "sch_place_symbol": sch_place_symbol,
    "sch_set_fields": sch_set_fields,
    "sch_connect": sch_connect,
    "sch_add_label": sch_add_label,
    "sch_add_power": sch_add_power,
    "sch_no_connect": sch_no_connect,
    "run_erc": run_erc,
}


def call(ctx: Ctx, name: str, args: dict) -> dict:
    """Run one tool; record timing, item changes, errors (the eval's per-call log)."""
    from tools_v2 import V2_HANDLERS

    t0 = time.monotonic()
    n0 = ctx.item_changes
    try:
        res = (HANDLERS | V2_HANDLERS)[name](ctx, args)
        ok = True
    except ToolError as e:
        res, ok = e.payload, False
    except Exception as e:  # noqa: BLE001 - an API error is a tool error for the agent
        res, ok = {"error": {"what": f"{type(e).__name__}: {e}", "why": "KiCad API error", "suggestion": ""}}, False
    ctx.calls.append(
        {
            "tool": name,
            "args": args,
            "ok": ok,
            "ms": round((time.monotonic() - t0) * 1000),
            "item_changes": ctx.item_changes - n0,
            "error": None if ok else res["error"]["what"],
        }
    )
    return res
