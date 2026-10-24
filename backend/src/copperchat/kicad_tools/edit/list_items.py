"""`sch_list_items` (#3, spec §10.1): the items of one sheet with full UUIDs, filtered by
type, box, net and text, for sch_move / sch_delete. Read-only."""

from __future__ import annotations

import fnmatch
import math
from collections import Counter
from collections.abc import Callable
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.access import Netlist
from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.geometry import Box, Point, same_point
from copperchat.kicad_tools.edit.scene import (
    Scene,
    netlists,
    read_scene,
    resolve_sheet,
    sheet_api,
    sheet_refs,
)
from copperchat.tools.spec import ToolContext, ToolResult, ToolSpec

TYPES = (
    "label",
    "global_label",
    "hier_label",
    "directive_label",
    "wire",
    "junction",
    "no_connect",
    "sheet_pin",
    "power",
    "symbol",
    "sheet",
)
LABEL_TYPE = {"hierarchical_label": "hier_label"}  # Scene's label kind -> item type
SIDES = {"left": "left", "right": "right", "up": "top", "down": "bottom"}
DEFAULT_LIMIT, MAX_LIMIT = 100, 500
MAX_OFFSET = 1_000_000


def _bad(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _segment_touches(box: Box, a: Point, b: Point) -> bool:
    """The segment a-b meets the box (edges included): Liang-Barsky clipping."""
    eps = 1e-6
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-dx, a[0] - box.x0 + eps),
        (dx, box.x1 - a[0] + eps),
        (-dy, a[1] - box.y0 + eps),
        (dy, box.y1 - a[1] + eps),
    ):
        if abs(p) < 1e-12:
            if q < 0:
                return False
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return False
    return True


def _text_matcher(text: str | None) -> Callable[[list[str]], bool] | None:
    if not text:
        return None
    pat = text.lower()
    if any(c in pat for c in "*?["):
        return lambda vals: any(fnmatch.fnmatchcase(v.lower(), pat) for v in vals)
    return lambda vals: any(pat in v.lower() for v in vals)


def _sheet_pin_nets(
    h: KicadHandle, scene: Scene, nets: dict[tuple[str, str], str], refs: dict[str, SheetRef]
) -> dict[str, str | None]:
    """`scene.sheet_pin_net` for every sheet pin, each sub-sheet read once."""
    by_child: dict[str, dict[str, str]] = {}
    out: dict[str, str | None] = {}
    for uid, info in scene.sheet_pins.items():
        net = nets.get((scene.sheet.path, uid))
        if net is None and info.child in refs:
            if info.child not in by_child:
                found: dict[str, str] = {}
                for lb in sheet_api(h, refs[info.child]).get_labels():
                    hit = nets.get((info.child, access.kiid(lb)))
                    if type(lb).__name__ == "HierarchicalLabel" and hit is not None:
                        found.setdefault(str(lb.text.value), hit)
                by_child[info.child] = found
            net = by_child[info.child].get(info.pin.name)
        out[uid] = net
    return out


def _list(
    h: KicadHandle,
    sheet_arg: str | None,
    types: set[str],
    bbox: Box | None,
    net: str | None,
    text: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    sheet = resolve_sheet(h, sheet_arg)
    scene = read_scene(h, sheet, boxes=False)
    netlist, nets = netlists(h)
    members = Counter(nets.values())
    path, human = sheet.path, sheet.human_path

    def clean(name: str | None) -> str | None:
        return None if not name or name.startswith("unconnected-") else name

    def item_net(uid: str) -> str | None:
        """KiCad names a net after a lone label; a net of one item connects nothing."""
        name = clean(nets.get((path, uid)))
        return name if name is not None and members[name] > 1 else None

    by_net: dict[str, list[tuple[str, str]]] = {}
    for key, name in nets.items():
        by_net.setdefault(name, []).append(key)

    def bridge(name: str | None, me: tuple[str, str], partner: str) -> str | None:
        """A sheet pin and its child's hierarchical label alone connect nothing either:
        `name` is null when no symbol pin is on it and its only other member (if any)
        sits on the `partner` sheet path (the child for a sheet pin, the parent for a
        hierarchical label)."""
        if name is None or name in netlist.nets:
            return name
        others = [p for p, u in by_net.get(name, []) if (p, u) != me]
        return None if len(others) <= 1 and all(p == partner for p in others) else name

    def pin_net(n: Netlist, key: Any) -> str | None:
        return clean(n.net_of(key))

    match = _text_matcher(text)
    rows: list[tuple[dict[str, Any], list[str], set[str | None], list[Point], Any]] = []

    def add(
        item: dict[str, Any],
        texts: list[str],
        item_nets: set[str | None],
        points: list[Point],
        hit: Any = None,
    ) -> None:
        if item["type"] in types:
            rows.append((item, texts, item_nets, points, hit))

    for lb in scene.labels.values():
        n = item_net(lb.uuid)
        kind = LABEL_TYPE.get(lb.kind, lb.kind)
        if kind == "hier_label":
            n = bridge(n, (path, lb.uuid), path.rpartition("/")[0])
        add(_point(lb.uuid, kind, human, lb.at, text=lb.text, net=n), [lb.text], {n}, [lb.at])
    for w in scene.wires.values():
        n = item_net(w.uuid)
        item = _item(w.uuid, "wire", human, start_mm=list(w.start), end_mm=list(w.end), net=n)
        add(item, [], {n}, [w.start], ("segment", w.start, w.end))
    for uid, at in scene.junctions.items():
        n = item_net(uid)
        add(_point(uid, "junction", human, at, net=n), [], {n}, [at])
    pins_at: dict[tuple[float, float], list[Any]] = {}
    for pe in scene.all_pins() if scene.no_connects else ():
        pins_at.setdefault(_grid(pe.at), []).append(pe)
    for uid, at in scene.no_connects.items():
        on = [p.key for p in pins_at.get(_grid(at), []) if same_point(p.at, at)]
        n = next((x for k in on if (x := pin_net(netlist, k)) is not None), None)
        add(_point(uid, "no_connect", human, at, net=n), [], {n}, [at])
    refs = {r.path: r for r in sheet_refs(h)}
    sp_nets = _sheet_pin_nets(h, scene, nets, refs) if types & {"sheet_pin", "sheet"} else {}
    sp_nets = {
        u: bridge(clean(x), (path, u), scene.sheet_pins[u].child) for u, x in sp_nets.items()
    }
    nets_of_sheet: dict[str, set[str | None]] = {}
    for u, i in scene.sheet_pins.items():
        nets_of_sheet.setdefault(i.sheet_uuid, set()).add(sp_nets.get(u))
    for uid, info in scene.sheet_pins.items():
        p, n = info.pin, sp_nets.get(uid)
        address = f"{p.ref}.{p.name}"
        item = _point(uid, "sheet_pin", human, p.at, text=p.name, net=n)
        item.update(sheet_name=p.ref, side=SIDES[p.direction], address=address)
        item["sheet_uuid"] = info.sheet_uuid
        add(item, [p.name, address], {n}, [p.at])
    for s in scene.symbols.values():
        pnets = {pin_net(netlist, p.key) for p in s.pins}
        if s.power:
            n = next(iter(sorted(x for x in pnets if x)), None)
            item = _point(s.uuid, "power", human, s.at, text=s.value, net=n)
            item.update(ref=s.ref, value=s.value)
            add(item, [s.value, s.ref], {n}, [s.at])
        else:
            item = _item(s.uuid, "symbol", human, position_mm=list(s.at), ref=s.ref)
            item.update(value=s.value, unit=s.unit)
            add(item, [s.ref, s.value], pnets, [s.at])
    for uid, box in scene.sheets.items():
        child = refs.get(f"{path}/{uid}")
        name = child.name if child else ""
        item = _item(uid, "sheet", human, position_mm=[box.x0, box.y0], name=name)
        item["file"] = child.file if child else ""
        item["size_mm"] = [round(box.width, 4), round(box.height, 4)]
        item["path"] = child.human_path if child else None
        add(item, [name], nets_of_sheet.get(uid, set()), [], ("box", box))

    out: list[dict[str, Any]] = []
    for item, texts, item_nets, points, hit in rows:
        if net is not None and net not in item_nets:
            continue
        if match is not None and not (texts and match(texts)):
            continue
        if bbox is not None:
            if hit is None:
                inside = any(bbox.contains(q) for q in points)
            elif hit[0] == "segment":
                inside = _segment_touches(bbox, hit[1], hit[2])
            else:
                b = hit[1]
                inside = b.x0 <= bbox.x1 and b.x1 >= bbox.x0 and b.y0 <= bbox.y1 and b.y1 >= bbox.y0
            if not inside:
                continue
        out.append(item)
    out.sort(key=_order)
    return {
        "sheet": human,
        "items": out[offset : offset + limit],
        "total": len(out),
        "offset": offset,
        "limit": limit,
    }


def _grid(p: Point) -> tuple[float, float]:
    """A bucket key for points equal up to float noise (`same_point` still decides)."""
    return (round(p[0], 2), round(p[1], 2))


def _item(uid: str, kind: str, sheet: str, **fields: Any) -> dict[str, Any]:
    return {"uuid": uid, "type": kind, "sheet": sheet, **fields}


def _point(
    uid: str, kind: str, sheet: str, at: Point, *, text: str | None = None, net: str | None
) -> dict[str, Any]:
    item = _item(uid, kind, sheet, position_mm=list(at))
    if text is not None:
        item["text"] = text
    item["net"] = net
    return item


def _order(item: dict[str, Any]) -> tuple[int, float, float, str]:
    x, y = item.get("position_mm") or item["start_mm"]
    return (TYPES.index(item["type"]), y, x, item["uuid"])


def _parse(args: dict[str, Any]) -> dict[str, Any]:
    raw_types = args.get("types")
    if raw_types is None:
        types = set(TYPES)
    else:
        if not isinstance(raw_types, list) or not raw_types:
            raise _bad(
                "sch_list_items: types must be a non-empty list",
                f"types was {raw_types!r:.80}",
                f"Pass a subset of {', '.join(TYPES)}.",
            )
        unknown = [str(t)[:40] for t in raw_types if t not in TYPES]
        if unknown:
            raise _bad(
                "sch_list_items: unknown item type",
                f"{', '.join(unknown[:10])} is not one of {', '.join(TYPES)}",
                f"Pass a subset of {', '.join(TYPES)}.",
            )
        types = set(raw_types)
    bbox = None
    if (raw := args.get("bbox")) is not None:
        if not isinstance(raw, list) or len(raw) != 4 or not all(_num(c) for c in raw):
            raise _bad(
                "sch_list_items: invalid bbox",
                f"bbox was {raw!r:.80}",
                "Pass bbox as [x1, y1, x2, y2] in mm.",
            )
        bbox = Box.around([(raw[0], raw[1]), (raw[2], raw[3])])
    limit, offset = args.get("limit", DEFAULT_LIMIT), args.get("offset", 0)
    if not _is_int(limit) or not 1 <= limit <= MAX_LIMIT:
        raise _bad(
            "sch_list_items: invalid limit",
            f"limit was {limit!r:.40}",
            f"Pass an integer limit from 1 to {MAX_LIMIT}; page with offset.",
        )
    if not _is_int(offset) or not 0 <= offset <= MAX_OFFSET:
        raise _bad(
            "sch_list_items: invalid offset",
            f"offset was {offset!r:.40}",
            "Pass an integer offset >= 0 (the number of items to skip).",
        )
    strs = {}
    for name, cap in (("sheet", 256), ("net", 256), ("text", 128)):
        v = args.get(name)
        if v is not None and not isinstance(v, str):
            raise _bad(
                f"sch_list_items: {name} must be a string",
                f"{name} was {v!r:.40}",
                f"Pass {name} as a string.",
            )
        strs[name] = v[:cap] if v else None
    return {"types": types, "bbox": bbox, "limit": limit, "offset": offset, **strs}


async def sch_list_items(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    a = _parse(args)
    return ToolResult(
        data=await ctx.session.call(
            lambda h: _list(
                h, a["sheet"], a["types"], a["bbox"], a["net"], a["text"], a["limit"], a["offset"]
            )
        )
    )


LIST_ITEMS_SPEC = ToolSpec(
    "sch_list_items",
    "Read-only: the items of one schematic sheet with full UUIDs (usable as sch_move / "
    "sch_delete items), type, sheet, position_mm (wires: start_mm, end_mm), text and net "
    "(null when unconnected), plus sheet pin address/side, symbol ref/value. Filters, "
    "all optional and combined: sheet= (instance path like '/amp/', name or page; default "
    "root), types= (subset of " + ", ".join(TYPES) + "), bbox=[x1, y1, x2, y2] mm (wires "
    "touching it, sheets overlapping it), net= (exact net name as KiCad reports it; "
    "symbols and sheets match when any pin is on it), text= (case-insensitive substring, "
    "or a glob with * ? [ ], on label text, sheet pin name or 'Sheet.PIN', reference, "
    "value, sheet name). Sorted by type, then y, x; page with limit= (default 100, max "
    "500) and offset=, total counts all matches.",
    {
        "type": "object",
        "properties": {
            "sheet": {
                "type": "string",
                "maxLength": 256,
                "description": "Sheet instance path like '/amp/', name or page; default root.",
            },
            "types": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(TYPES),
                "items": {"type": "string", "enum": list(TYPES)},
                "description": "Item types to list; default all.",
            },
            "bbox": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
                "description": "[x1, y1, x2, y2] in mm.",
            },
            "net": {"type": "string", "maxLength": 256, "description": "e.g. '/Power/VIN'."},
            "text": {"type": "string", "maxLength": 128, "description": "Substring or glob."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 100},
            "offset": {"type": "integer", "minimum": 0, "maximum": MAX_OFFSET, "default": 0},
        },
        "additionalProperties": False,
    },
    sch_list_items,
)
