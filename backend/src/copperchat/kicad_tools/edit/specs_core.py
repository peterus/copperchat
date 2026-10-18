"""Core edit tools (E2): `sch_get_pin` (read-only) and `sch_add_wire` (two-phase)."""

from __future__ import annotations

import itertools
import math
import uuid as uuidlib
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.access import Netlist, PinKey
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Create,
    Logical,
    NetIntent,
    SheetRef,
    static_stage,
)
from copperchat.kicad_tools.edit.engine import apply_change_set, edit_result
from copperchat.kicad_tools.edit.geometry import (
    NM,
    Point,
    in_segment_interior,
    on_segment,
    orthogonal,
    same_point,
    snap_point,
)
from copperchat.kicad_tools.edit.scene import (
    Scene,
    all_symbols,
    netlists,
    read_scene,
    resolve_pin,
    resolve_sheet,
)
from copperchat.tools.spec import ToolContext, ToolResult, ToolSpec, edit_spec

EXACT_MM = 1e-4  # a point given "on" a pin must be on it up to float noise


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


def _bad(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


def _fmt(p: Point) -> str:
    return f"({p[0]:g}, {p[1]:g})"


# -- sch_get_pin ------------------------------------------------------------------------


def _get_pin(spec: str) -> Any:
    def run(h: KicadHandle) -> dict[str, Any]:
        raw = h.schematic().get_symbols()
        units = all_symbols(h, raw, ref=spec.strip().partition(".")[0])
        pin = resolve_pin(h, spec, symbols=units) if units else resolve_pin(h, spec, raw=raw)
        owner = next(s for s in units if s.uuid == pin.key[1] and s.sheet.path == pin.key[0])
        scene = read_scene(h, owner.sheet, boxes=False)
        net = netlists(h, raw)[0].net_of(pin.key)
        connected = [
            i
            for i in scene.items_at(pin.at)
            if not (i["kind"] == "pin" and i["uuid"] == owner.uuid and i["pin"] == pin.number)
        ]
        return {
            "ref": pin.ref,
            "pin": pin.number,
            "name": pin.name,
            "type": pin.type,
            "unit": owner.unit,
            "symbol_uuid": owner.uuid,
            "at_mm": list(pin.at),
            "direction": pin.direction,
            "sheet": owner.sheet.human_path,
            "page": owner.sheet.page,
            "net": None if net is None or net.startswith("unconnected-") else net,
            "free": not connected,
            "connected": connected,
        }

    return run


async def sch_get_pin(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return ToolResult(data=await ctx.session.call(_get_pin(str(args["pin"])[:64])))


# -- sch_add_wire -----------------------------------------------------------------------


MAX_POINTS = 64


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _coord(c: Any) -> bool:
    if not isinstance(c, (int, float)) or isinstance(c, bool):
        return False
    try:
        return abs(c) < 10_000 and math.isfinite(c)
    except OverflowError:  # an int too large for a float
        return False


def _parse_points(raw: Any) -> list[Point]:
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_POINTS:
        raise _bad(
            f"sch_add_wire needs 2 to {MAX_POINTS} points",
            f"points was {_short(raw)}",
            "Pass points as [[x, y], [x, y], ...] in mm; split long runs into several wires.",
        )
    out: list[Point] = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) != 2 or not all(_coord(c) for c in p):
            raise _bad(
                "Invalid point in sch_add_wire",
                f"{_short(p)} is not an [x, y] pair of mm numbers",
                "Pass points as [[x, y], [x, y], ...] in mm.",
            )
        out.append((float(p[0]), float(p[1])))
    return out


def _wire(a: Point, b: Point) -> Any:
    from kipy.geometry import Vector2
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLineType
    from kipy.schematic_types import SchematicLine

    w = SchematicLine()
    w.start = Vector2.from_xy(round(a[0] * NM), round(a[1] * NM))
    w.end = Vector2.from_xy(round(b[0] * NM), round(b[1] * NM))
    w.type = SchematicLineType.SLT_WIRE
    w.proto.id.value = str(uuidlib.uuid4())
    return w


def _junction(at: Point) -> Any:
    from kipy.geometry import Vector2
    from kipy.schematic_types import Junction

    j = Junction()
    j.position = Vector2.from_xy(round(at[0] * NM), round(at[1] * NM))
    j.proto.id.value = str(uuidlib.uuid4())
    return j


def _end_nets(
    scene: Scene,
    sheet: SheetRef,
    end: Point,
    netlist: Netlist,
    nets: dict[tuple[str, str], str],
) -> tuple[list[PinKey], set[str], bool]:
    """Terminal pins at an end, nets of the other items there, and whether the end lands
    on a wire interior (a T-joint that gets a junction)."""
    pins = [p.key for p in scene.all_pins() if same_point(p.at, end)]
    end_nets: set[str] = set()
    interiors = 0
    for w in scene.wires.values():
        if on_segment(end, w.start, w.end):
            interiors += in_segment_interior(end, w.start, w.end)
            if (net := nets.get((sheet.path, w.uuid))) is not None:
                end_nets.add(net)
    has_junction = any(same_point(j, end) for j in scene.junctions.values())
    if interiors > 1 and not has_junction:
        # A crossing without a junction joins nothing yet; our junction would short it.
        raise StructuredToolError(
            "sch_add_wire refused: the end is on a wire crossing",
            f"the end {_fmt(end)} lies where two wires cross without a junction; "
            "connecting there would join both wires",
            "End the wire on one of the wires away from the crossing.",
            code="overlap",
        )
    t_joint = interiors > 0 and not has_junction
    for uid, item_at in [
        *((lb.uuid, lb.at) for lb in scene.labels.values()),
        *scene.junctions.items(),
    ]:
        if same_point(item_at, end) and (net := nets.get((sheet.path, uid))) is not None:
            end_nets.add(net)
    pin_nets = {n for k in pins if (n := netlist.net_of(k)) is not None}
    if len(end_nets | pin_nets) > 1:
        raise StructuredToolError(
            "sch_add_wire refused: one end touches several nets",
            f"the end {_fmt(end)} touches the nets {', '.join(sorted(end_nets | pin_nets))}; "
            "the wire would short them",
            "End the wire on exactly one net.",
            code="overlap",
        )
    return pins, end_nets, t_joint


def _check_path(
    points: list[Point],
    scene: Scene,
    sheet: SheetRef,
    netlist: Netlist,
    nets: dict[tuple[str, str], str],
    allowed: set[str],
    terminal_syms: tuple[set[str], set[str]],
) -> None:
    """Refuse before writing what would connect a foreign net or cross a symbol body."""
    segments = list(itertools.pairwise(points))
    ends = (points[0], points[-1])

    def overlap(why: str, suggestion: str) -> StructuredToolError:
        return StructuredToolError("sch_add_wire refused: the path is unsafe", why, suggestion,
                                   code="overlap")  # fmt: skip

    for pin in scene.foreign_pin_ends(allowed, netlist):
        if any(same_point(pin.at, e) for e in ends):
            continue
        if any(on_segment(pin.at, a, b) for a, b in segments):
            raise overlap(
                f"the wire would run over pin {pin.label} at {_fmt(pin.at)} and connect it "
                "(a wire interior over a pin end makes a connection)",
                "Route around the pin (another corner position) or connect with labels.",
            )
    foreign_points: list[tuple[Point, str]] = []
    for w in scene.wires.values():
        if nets.get((sheet.path, w.uuid)) in allowed:
            continue
        for a, b in segments:
            if _collinear_overlap(a, b, w.start, w.end):
                raise overlap(
                    f"the wire would run along the existing wire {_fmt(w.start)}-{_fmt(w.end)} "
                    f"(net {nets.get((sheet.path, w.uuid))}) and join it",
                    "Choose a parallel track at least 1.27 mm away.",
                )
        foreign_points += [(w.start, "a wire end"), (w.end, "a wire end")]
    for lb in scene.labels.values():
        if nets.get((sheet.path, lb.uuid)) not in allowed:
            foreign_points.append((lb.at, f"label {lb.text}"))
    for uid, j in scene.junctions.items():
        if nets.get((sheet.path, uid)) not in allowed:
            foreign_points.append((j, "a junction"))
    for pt, what in foreign_points:
        if any(same_point(pt, e) for e in ends):
            continue
        if any(on_segment(pt, a, b) for a, b in segments):
            raise overlap(
                f"the wire would touch {what} at {_fmt(pt)} and connect another net",
                "Route around it.",
            )
    last = len(segments) - 1
    for i, (a, b) in enumerate(segments):
        for sym in scene.symbols.values():
            own = (i == 0 and sym.uuid in terminal_syms[0]) or (
                i == last and sym.uuid in terminal_syms[1]
            )
            if not own and sym.body.hit_by_segment(a, b):
                raise overlap(
                    f"segment {_fmt(a)}-{_fmt(b)} crosses the body of {sym.ref}",
                    f"Route around the symbol (its body box is {sym.body.as_dict()} mm).",
                )


def _collinear_overlap(a: Point, b: Point, c: Point, d: Point) -> bool:
    eps = 0.01
    if abs(a[0] - b[0]) < eps and abs(c[0] - d[0]) < eps and abs(a[0] - c[0]) < eps:
        lo = max(min(a[1], b[1]), min(c[1], d[1]))
        hi = min(max(a[1], b[1]), max(c[1], d[1]))
        return hi - lo > eps
    if abs(a[1] - b[1]) < eps and abs(c[1] - d[1]) < eps and abs(a[1] - c[1]) < eps:
        lo = max(min(a[0], b[0]), min(c[0], d[0]))
        hi = min(max(a[0], b[0]), max(c[0], d[0]))
        return hi - lo > eps
    return False


def _plan_wire(raw_points: list[Point], sheet_arg: str | None) -> Any:
    def run(h: KicadHandle) -> ChangeSet:
        sheet = resolve_sheet(h, sheet_arg)
        scene = read_scene(h, sheet)
        netlist, nets = netlists(h)
        notes: list[str] = []
        snapped = [snap_point(p) for p in raw_points]
        moved = [(r, s) for r, s in zip(raw_points, snapped, strict=True)
                 if not same_point(r, s, EXACT_MM)]  # fmt: skip
        pin_ends = scene.all_pins()
        for idx in (0, len(raw_points) - 1):
            raw, end = raw_points[idx], snapped[idx]
            for pin in pin_ends:
                if same_point(pin.at, raw, EXACT_MM) and not same_point(pin.at, end, EXACT_MM):
                    raise _bad(
                        "sch_add_wire cannot connect an off-grid pin",
                        f"pin {pin.label} at {_fmt(pin.at)} is off the 1.27 mm grid; the "
                        f"snapped end {_fmt(end)} would miss it",
                        "Connect this pin with a label placed on the pin end instead.",
                    )
                if same_point(pin.at, end, EXACT_MM) and not same_point(pin.at, raw, EXACT_MM):
                    off = math.dist(pin.at, raw)
                    raise _bad(
                        "sch_add_wire end is not on the pin",
                        f"the end {_fmt(raw)} is {off:.3g} mm off pin {pin.label} at "
                        f"{_fmt(pin.at)}; a wire end must be exactly on a pin end to connect",
                        f"Use the exact pin position {_fmt(pin.at)} from sch_get_pin.",
                    )
        if moved:
            notes.append(
                "points snapped to the 1.27 mm grid: "
                + ", ".join(f"{_fmt(r)} -> {_fmt(s)}" for r, s in moved)
            )
        points = [snapped[0]]
        for p in snapped[1:]:
            if not same_point(p, points[-1]):
                points.append(p)
        if len(points) < 2:
            raise _bad(
                "sch_add_wire has no length",
                "all points are the same after snapping to the 1.27 mm grid",
                "Pass two different points.",
            )
        for a, b in itertools.pairwise(points):
            if not orthogonal(a, b):
                raise _bad(
                    "sch_add_wire refused a diagonal segment",
                    f"segment {_fmt(a)}-{_fmt(b)} is neither horizontal nor vertical",
                    "Add a corner point so every segment is horizontal or vertical.",
                )
        start_pins, start_nets, start_t = _end_nets(scene, sheet, points[0], netlist, nets)
        end_pins, end_nets, end_t = _end_nets(scene, sheet, points[-1], netlist, nets)
        terminals = [*start_pins, *end_pins]
        allowed = {n for k in terminals if (n := netlist.net_of(k)) is not None}
        allowed |= start_nets | end_nets
        _check_path(
            points,
            scene,
            sheet,
            netlist,
            nets,
            allowed,
            ({k[1] for k in start_pins}, {k[1] for k in end_pins}),
        )
        group: set[PinKey] = set(terminals)
        for net in start_nets | end_nets:  # one pin represents an existing net, if any
            members = sorted(netlist.nets.get(net, ()))
            if members:
                group.add(members[0])
        items: list[Any] = [_wire(a, b) for a, b in itertools.pairwise(points)]
        for at, t_joint in ((points[0], start_t), (points[-1], end_t)):
            if t_joint:
                items.append(_junction(at))
        for at, pins_here, nets_here in (
            (points[0], start_pins, start_nets),
            (points[-1], end_pins, end_nets),
        ):
            if not pins_here and not nets_here:
                notes.append(f"the end {_fmt(at)} is not connected to anything")
        return ChangeSet(
            tool="sch_add_wire",
            sheet=sheet,
            stages=[static_stage("add wire", Create(items))],
            pre_images={},
            intent=NetIntent(merge=[frozenset(group)] if len(group) > 1 else []),
            logical=Logical(1),
            summary={"points_mm": [list(p) for p in points], "sheet": sheet.human_path},
            notes=notes,
        )

    return run


async def prepare_add_wire(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    points = _parse_points(args.get("points"))
    sheet = args.get("sheet")
    return await ctx.session.call(_plan_wire(points, str(sheet)[:256] if sheet else None))


async def apply_change(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    """The generic apply of E2-style tools: write, verify, report."""
    applied = await apply_change_set(ctx, cs)
    return edit_result(cs, applied)


_SHEET = {
    "sheet": {
        "type": "string",
        "maxLength": 256,
        "description": "Sheet instance path like '/amp/' or page number; default root.",
    }
}

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "sch_get_pin",
        "One schematic pin by 'REF.PIN' (number, or a unique pin name like 'U1.VIN'): "
        "name, electrical type, exact connection point in mm, outward direction, sheet, "
        "net, whether it is free, and the items connected at its end (wires, labels, "
        "pins, no-connects with full UUIDs). Use its at_mm for wire end points.",
        _obj(
            {"pin": {"type": "string", "maxLength": 64, "description": "e.g. 'R5.2' or 'U1.VIN'."}},
            ["pin"],
        ),
        sch_get_pin,
    ),
    edit_spec(
        "sch_add_wire",
        "Draw one wire as an orthogonal polyline (mm, snapped to the 1.27 mm grid). Ends "
        "must be exactly on pin ends (take at_mm from sch_get_pin), on existing wires/labels, "
        "or free. Refused before writing when a segment would run over a foreign pin, wire "
        "end, label or symbol body. Verified against KiCad's netlist after writing: the "
        "pins at both ends end up on one net, nothing else changes. One logical change.",
        _obj(
            {
                "points": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": MAX_POINTS,
                    "items": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "description": "[[x, y], ...] corner points in mm, first and last are ends.",
                },
                **_SHEET,
            },
            ["points"],
        ),
        prepare_add_wire,
        apply_change,
    ),
]
