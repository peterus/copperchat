"""Connection tools (E5): `sch_connect`, `sch_add_label`, `sch_add_power`,
`sch_no_connect`, `sch_rename_net`, `sch_reroute` — all two-phase (E-C4)."""

from __future__ import annotations

import contextlib
import re
import time
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical
from copperchat.kicad_tools.edit.connect import (
    ConnectChangeSet,
    PowerNeeded,
    looks_like_rail,
    plan_connect_net,
    plan_connect_pins,
    plan_rename,
    plan_reroute,
)
from copperchat.kicad_tools.edit.engine import apply_change_set, edit_result
from copperchat.kicad_tools.edit.geometry import Point
from copperchat.kicad_tools.edit.labels import (
    PLAN_BUDGET_S,
    UUID_RE,
    LabelKind,
    PowerSymbol,
    World,
    bad,
    check_net_name,
    is_rail,
    label_at_point_change_set,
    labels_change_set,
    no_connect_change_set,
    pins_arg,
    power_change_set,
    power_symbol,
    refuse_rail_label,
    sheet_pin_uuid_not_found,
)
from copperchat.kicad_tools.edit.scene import resolve_sheet, short
from copperchat.kicad_tools.edit.specs_core import MAX_POINTS, _coord
from copperchat.tools.spec import ToolContext, ToolResult, ToolSpec, edit_spec

_KINDS = ("local", "global", "hierarchical")
# 'U1.3', 'R9.VIN', '#PWR01.1': a pin, never a net name (net names like '+3.3V' start
# with a sign or digit).
PIN_RE = re.compile(r"^[A-Za-z_#][A-Za-z0-9_#]*\.[A-Za-z0-9_+\-~]+$")
_VIA = ("auto", "wire", "label")


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


def _kind(args: dict[str, Any], tool: str) -> LabelKind:
    kind = args.get("kind", "local")
    if kind not in _KINDS:
        raise bad(
            f"{tool}: invalid kind {short(kind)}",
            "kind is local, global or hierarchical",
            "Use kind='local' for a net on one sheet, 'global' across sheets.",
        )
    return kind  # type: ignore[no-any-return]


def _is_point(raw: Any) -> bool:
    return (
        isinstance(raw, list)
        and len(raw) == 2
        and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in raw)
    )


async def _apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    if not cs.stages:  # nothing to write: every target already is as asked
        data: dict[str, Any] = {
            "created": [],
            "modified": [],
            "deleted": [],
            "side_effects": [],
            "nets_touched": [],
            "connectivity_verified": True,
            "logical_changes": 0,
            "budget": None,
            "notes": list(cs.notes),
        }
        data.update(cs.summary)
        return ToolResult(data=data)
    applied = await apply_change_set(ctx, cs)
    extra: dict[str, Any] = {}
    if isinstance(cs, ConnectChangeSet) and cs.net_pin is not None:
        extra["net"] = applied.netlist.net_of(cs.net_pin)
    return edit_result(cs, applied, **extra)


# -- library -----------------------------------------------------------------------------


async def _power_symbol(ctx: ToolContext, net: str, symbol: Any, tool: str) -> PowerSymbol:
    from copperchat.library.service import library_for
    from copperchat.library.specs import READY_WAIT_S

    flag = net.upper() == "PWR_FLAG"
    if symbol is not None and (not isinstance(symbol, str) or ":" not in symbol):
        raise bad(
            f"{tool}: invalid symbol {short(symbol)}",
            "symbol is a library id like 'power:VCC'",
            "Omit it to use power:<net>.",
        )
    lib_id = (symbol or ("power:PWR_FLAG" if flag else f"power:{net}"))[:128]
    svc = library_for(ctx.session)
    await svc.wait_ready(READY_WAIT_S)
    try:
        detail = await svc.get_symbol(lib_id)
    except StructuredToolError as exc:
        if exc.code != "not_found":
            raise
        close: list[str] = []
        with contextlib.suppress(StructuredToolError):
            close = [r["lib_id"] for r in await svc.search_symbols(net, "power", 8)]
        raise StructuredToolError(
            f"{tool}: no power symbol {lib_id}",
            "power symbols come from the 'power' library"
            + (f"; close matches: {', '.join(close)}" if close else ""),
            f"Pass symbol='power:VCC' (any power symbol) to carry the net name {net!r}, "
            "or pick one of the matches.",
            code="not_found",
            details={"candidates": close},
        ) from exc
    if not detail.is_power:
        raise bad(
            f"{tool}: {lib_id} is not a power symbol",
            "only power symbols define a rail by their name",
            "Pick a symbol from the 'power' library.",
        )
    return power_symbol(detail)


# -- sch_add_label ---------------------------------------------------------------------


async def prepare_add_label(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    tool = "sch_add_label"
    net = check_net_name(args.get("net"), tool)
    kind = _kind(args, tool)
    at = args.get("at")
    sheet_arg = args.get("sheet")
    if await is_rail(ctx, set(), net):
        raise refuse_rail_label(net, tool)

    def run(h: KicadHandle) -> ChangeSet:
        world = World(h)
        if looks_like_rail(world, net):
            raise refuse_rail_label(net, tool)
        if isinstance(at, list) and _is_point(at):
            sheet = resolve_sheet(h, str(sheet_arg)[:256] if sheet_arg else None)
            if not all(_coord(c) for c in at):
                raise bad(
                    f"{tool}: position out of range",
                    f"at was {short(at)}",
                    "Pass a point on the sheet in mm.",
                )
            point = (float(at[0]), float(at[1]))
            return label_at_point_change_set(world, tool, sheet, point, net, kind)
        return labels_change_set(world, tool, pins_arg(world, at, tool), net, kind)

    return await ctx.session.call(run)


# -- sch_add_power ---------------------------------------------------------------------


async def prepare_add_power(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    tool = "sch_add_power"
    net = check_net_name(args.get("net"), tool)
    sym = await _power_symbol(ctx, net, args.get("symbol"), tool)
    target = "PWR_FLAG" if sym.flag else net

    def run(h: KicadHandle) -> ChangeSet:
        world = World(h)
        return power_change_set(world, tool, pins_arg(world, args.get("at"), tool), sym, target)

    return await ctx.session.call(run)


# -- sch_no_connect --------------------------------------------------------------------


async def prepare_no_connect(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    tool = "sch_no_connect"

    def run(h: KicadHandle) -> ChangeSet:
        world = World(h)
        return no_connect_change_set(world, tool, pins_arg(world, args.get("pins"), tool, "pins"))

    return await ctx.session.call(run)


# -- sch_connect -----------------------------------------------------------------------


async def prepare_connect(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    tool = "sch_connect"
    frm, to = args.get("from"), args.get("to")
    via = args.get("via", "auto")
    if via not in _VIA:
        raise bad(
            f"{tool}: invalid via {short(via)}",
            "via is auto, wire or label",
            "Use via='auto' unless you need a wire or labels.",
        )
    if not isinstance(to, str) or not to.strip():
        raise bad(
            f"{tool}: to is missing", "to is 'REF.PIN' or a net name", "e.g. to='U1.3' or to='VIN'."
        )
    to = to.strip()
    if len(to) > 64:
        raise bad(
            f"{tool}: to is too long",
            "pins and net names have at most 64 characters",
            "Pass 'REF.PIN' or a net name.",
        )
    pin_shaped = bool(PIN_RE.fullmatch(to))
    net_name = args.get("net_name")
    if net_name is not None:
        net_name = check_net_name(net_name, tool)
        if await is_rail(ctx, set(), net_name):
            raise refuse_rail_label(net_name, tool)
    to_rail = not pin_shaped and await is_rail(ctx, set(), to)
    worlds: list[World] = []

    def plan(world: World) -> ConnectChangeSet | PowerNeeded:
        pa = world.endpoint(frm)
        ref = to.partition(".")[0]
        known_ref = any(x.ref == ref for x in world.symbols)
        # '<sheet name>.<pin name>': a sheet pin (sheet names may hold any character);
        # a dotted net name that is no pin of that sheet stays a net ('I2C.SDA')
        sheet_pin = world.sheet_pin_by_uuid(to)
        if sheet_pin is None and UUID_RE.fullmatch(to.strip()):
            raise sheet_pin_uuid_not_found(to)  # no net label named after a UUID
        if sheet_pin is None and not known_ref:
            try:
                sheet_pin = world.sheet_pin(to)
            except StructuredToolError as exc:
                if exc.code != "not_found" or not _net_exists(world, world.sheet_of(pa), to):
                    raise
        if sheet_pin is not None:
            return plan_connect_pins(world, tool, pa, sheet_pin, via, net_name)
        if pin_shaped and (known_ref or not _net_exists(world, world.sheet_of(pa), to)):
            # 'R9.1' is a pin, also when R9 does not exist (not_found); a dotted name is
            # a net only when no part has that ref and the net exists ('I2C.SDA')
            return plan_connect_pins(world, tool, pa, world.pin(to), via, net_name)
        name = check_net_name(to, tool)
        if to_rail or looks_like_rail(world, name):
            return PowerNeeded(pa.label, name, "to is a power net", end=pa)
        return plan_connect_net(world, tool, pa, name, via)

    def run(h: KicadHandle) -> ConnectChangeSet | PowerNeeded:
        world = World(h)
        worlds.append(world)
        out = plan(world)
        if isinstance(out, ConnectChangeSet):
            _report_real_pin(world, out)
        return out

    planned = await ctx.session.call(run)
    if isinstance(planned, ConnectChangeSet):
        return planned
    if via == "wire":
        raise StructuredToolError(
            f"{tool}: {planned.net} is a power net",
            "rails are connected with power symbols, not with long supply wires",
            f"Use via='auto' (places a {planned.net} power symbol at {planned.pin}).",
            code="no_route",
        )
    sym = await _power_symbol(ctx, planned.net, None, tool)

    def run_power(h: KicadHandle) -> ConnectChangeSet:
        world = worlds[0]  # nothing was written since (the adapter holds the edit lock)
        world.h = h
        world.deadline = time.monotonic() + PLAN_BUDGET_S
        pin = planned.end if planned.end is not None else world.endpoint(planned.pin)
        cs = power_change_set(world, tool, [pin], sym, "PWR_FLAG" if sym.flag else planned.net)
        out = ConnectChangeSet(
            tool=cs.tool,
            sheet=cs.sheet,
            stages=cs.stages,
            pre_images=cs.pre_images,
            intent=cs.intent,
            logical=Logical(cs.logical.changes),
            summary={
                "from": str(frm),
                "to": to,
                **cs.summary,
                "via_used": "power",
                "reason": planned.reason,
            },
            notes=cs.notes,
            net_pin=pin.key,
        )
        _report_real_pin(world, out)
        return out

    return await ctx.session.call(run_power)


def _report_real_pin(world: World, cs: ConnectChangeSet) -> None:
    """The result's net is read at `net_pin`: a sheet pin is no netlist key, so a symbol
    pin of the net it joins stands in (else the other end of the connection)."""
    if cs.net_pin is None:
        return
    real = world.real_key(cs.net_pin)
    if real is None:
        merged = [k for g in cs.intent.merge for k in sorted(g)]
        merged += [k for g in cs.intent.joined for k in sorted(g) if len(k) == 3]
        real = merged[0] if merged else None
    cs.net_pin = real


def _net_exists(world: World, sheet: Any, name: str) -> bool:
    from copperchat.kicad_tools.edit.labels import same_name_nets

    nets = world.netlist.nets
    return (
        name in nets
        or f"{sheet.human_path}{name}" in nets
        or bool(same_name_nets(world, name, "local", sheet))
        or any(n.startswith("/") and n.rsplit("/", 1)[-1] == name for n in nets)
    )


# -- sch_rename_net / sch_reroute ------------------------------------------------------


async def prepare_rename(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    tool = "sch_rename_net"
    old = args.get("old")
    if not isinstance(old, str) or not old.strip() or len(old) > 128:
        raise bad(
            f"{tool}: invalid old name {short(old)}",
            "old is the net's name",
            "e.g. old='SDA' or old='/amp/IN'.",
        )
    new = check_net_name(args.get("new"), tool)
    if await is_rail(ctx, set(), new):
        raise refuse_rail_label(new, tool)
    return await ctx.session.call(lambda h: plan_rename(World(h), tool, old.strip(), new))


def _points(raw: Any, tool: str) -> list[Point]:
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_POINTS:
        raise bad(
            f"{tool} needs 2 to {MAX_POINTS} points",
            f"points was {short(raw)}",
            "Pass points as [[x, y], [x, y], ...] in mm.",
        )
    out: list[Point] = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) != 2 or not all(_coord(c) for c in p):
            raise bad(
                f"Invalid point in {tool}",
                f"{short(p)} is not an [x, y] pair of mm numbers",
                "Pass points as [[x, y], [x, y], ...] in mm.",
            )
        out.append((float(p[0]), float(p[1])))
    return out


async def prepare_reroute(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    tool = "sch_reroute"
    points = _points(args.get("points"), tool)
    wires = args.get("wires")
    return await ctx.session.call(lambda h: plan_reroute(World(h), tool, wires, points))


# -- specs -----------------------------------------------------------------------------

_PIN = {"type": "string", "maxLength": 64}
_PINS = {"type": "array", "items": _PIN, "minItems": 1, "maxItems": 64}

TOOL_SPECS: list[ToolSpec] = [
    edit_spec(
        "sch_connect",
        "Connect a pin to another pin or to a net. A pin is 'REF.PIN' or a sheet pin "
        "'<sheet name>.<pin name>' or its UUID on the parent sheet (joins the sub-sheet's net; a "
        "symbol reference wins over a sheet name). to=pin: a computed orthogonal wire "
        "(<= 3 segments, may T-join onto a wire of the target net with a junction); if no "
        "clean route exists, via='auto' falls back to labels (net_name, an existing net "
        "name, or N_<ref>_<pin>). to=<net name>: power nets get a power symbol, other nets a "
        "wire when one of their wires/pins is within 25.4 mm, else a label. Refuses to merge "
        "two named nets. "
        "Sheet-edge rule (same for sch_connect, sch_add_wire, sch_reroute): no segment on a "
        "sheet symbol's edge or within 2.54 mm parallel to it; a wire touches a sheet edge "
        "only perpendicular, ending on a sheet pin. Existing wires that break it are "
        "named under warnings, not refused. "
        "Verified against KiCad's netlist. Budget: 1 logical change.",
        _obj(
            {
                "from": {
                    **_PIN,
                    "description": "Pin 'REF.PIN', e.g. 'C1.1', or sheet pin 'SHEET.PIN' / UUID.",
                },
                "to": {
                    **_PIN,
                    "description": "Pin 'REF.PIN', sheet pin 'SHEET.PIN' (sheet name and "
                    "pin name) or UUID, or a net name like 'VIN'/'GND'.",
                },
                "via": {"type": "string", "enum": list(_VIA), "default": "auto"},
                "net_name": {
                    "type": "string",
                    "maxLength": 64,
                    "description": "Net name for a label fallback.",
                },
            },
            ["from", "to"],
        ),
        prepare_connect,
        _apply,
    ),
    edit_spec(
        "sch_add_label",
        "Label pins with a net name: a short stub + label per pin (text pointing away from "
        "the part; at the free end of an existing wire when the pin is wired), or one label "
        "exactly at a point [x, y] that touches a wire or pin end. All pins or none: a pin "
        "without room fails the call before anything is written. Rail names (GND, +3V3 ...) "
        "are refused: use sch_add_power. Budget: 1 logical change per pin.",
        _obj(
            {
                "net": {"type": "string", "maxLength": 64},
                "at": {
                    "description": "['REF.PIN', ...] or one point [x, y] in mm.",
                    "anyOf": [
                        _PINS,
                        _PIN,
                        {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                    ],
                },
                "kind": {"type": "string", "enum": list(_KINDS), "default": "local"},
                "sheet": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "Sheet of a point label; default root.",
                },
            },
            ["net", "at"],
        ),
        prepare_add_label,
        _apply,
    ),
    edit_spec(
        "sch_add_power",
        "Attach a power symbol from the 'power' library to each pin (stub + symbol, supplies "
        "pointing up, grounds down; from the free stub end when the pin is wired). "
        "net='PWR_FLAG' places power flags. symbol overrides the library symbol (the net "
        "keeps the given name). All pins or none. Budget: 1 logical change per pin.",
        _obj(
            {
                "net": {"type": "string", "maxLength": 64, "description": "e.g. 'GND', '+3V3'."},
                "at": {**_PINS, "description": "['REF.PIN', ...]"},
                "symbol": {
                    "type": "string",
                    "maxLength": 128,
                    "description": "Library id, default 'power:<net>'.",
                },
            },
            ["net", "at"],
        ),
        prepare_add_power,
        _apply,
    ),
    edit_spec(
        "sch_no_connect",
        "Mark free pins as intentionally unconnected (no-connect marker on the pin end). "
        "Refused for a connected pin. All pins or none. Budget: 1 logical change per pin.",
        _obj({"pins": {**_PINS, "description": "['REF.PIN', ...]"}}, ["pins"]),
        prepare_no_connect,
        _apply,
    ),
    edit_spec(
        "sch_rename_net",
        "Rename a label-named net: every label of the net (on every sheet) gets the new "
        "text; members stay identical. Refuses power nets and names already in use. One "
        "logical change.",
        _obj(
            {
                "old": {"type": "string", "maxLength": 128},
                "new": {"type": "string", "maxLength": 64},
            },
            ["old", "new"],
        ),
        prepare_rename,
        _apply,
    ),
    edit_spec(
        "sch_reroute",
        "Replace the course of an existing wire route (wire uuids, or the chain between two "
        "pins) with new orthogonal points (mm, snapped); the new route must start and end on "
        "the old route's ends. Refused before writing when it would touch another net, and "
        "undone unless the netlist stays identical. "
        "Sheet-edge rule (same for sch_connect, sch_add_wire, sch_reroute): no segment on a "
        "sheet symbol's edge or within 2.54 mm parallel to it; a wire touches a sheet edge "
        "only perpendicular, ending on a sheet pin. Existing wires that break it are "
        "named under warnings, not refused. "
        "Budget: 1 logical change.",
        _obj(
            {
                "wires": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 64},
                            "minItems": 1,
                            "maxItems": 64,
                        },
                        _obj({"from": _PIN, "to": _PIN}, ["from", "to"]),
                    ]
                },
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
                },
            },
            ["wires", "points"],
        ),
        prepare_reroute,
        _apply,
    ),
]
