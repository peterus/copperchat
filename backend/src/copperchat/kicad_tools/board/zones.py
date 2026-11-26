"""`pcb_zones` (SP4 spec §7.2 "Copper", §11, P7 Task 2): add, update or delete copper zones,
then refill. `action: "add"` without `zones` pours the owner's default stack (D1,
`planes.default_planes`): a solid GND plane on In1.Cu and GND pours on the outer layers.

Two stages: (1) every create / update / delete in one commit - its `undo` writes the
updated and deleted zones back from their pre-call protobufs (KIIDs kept) and refills;
(2) the refill (`sctx.refill`, never a bare `refill_zones`). Compensation removes the
zones the call created (KIID diff). Needs the approved placement (spec §8.3).

Also the helpers the other copper tools share (`pcb_fanout`, `pcb_stitch`,
`pcb_tidy_silk`): argument checks, net lookup and the M11 report."""

from __future__ import annotations

import asyncio
import difflib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from kipy.board_types import Zone as KipyZone
from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode
from kipy.proto.board.board_types_pb2 import ZoneConnectionStyle, ZoneType
from kipy.util.board_layer import layer_from_canonical_name

from copperchat.board.boardmd import BoardDoc, read_board_doc
from copperchat.board.classify import classify
from copperchat.board.geom import rect_poly
from copperchat.board.jobs import metrics_job
from copperchat.board.model import BoardModel, Point, Zone
from copperchat.board.planes import default_planes
from copperchat.board.worker import run_cpu
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board.analysis import metrics_data
from copperchat.kicad_tools.board.api import REFILL_TIMEOUT_S
from copperchat.kicad_tools.board.checkpoints import require_placement_approved
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.board.read import BOARD_READ_TIMEOUT_S, board_model, require_board
from copperchat.kicad_tools.board.route import class_params
from copperchat.kicad_tools.board.setup import require_board_tools
from copperchat.kicad_tools.board.specs_setup import NAME_PATTERN
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical
from copperchat.kicad_tools.units import mm_to_nm
from copperchat.tools.spec import ToolContext, ToolResult

__all__ = [
    "ACTIONS",
    "MAX_CLEARANCE_MM",
    "MAX_POLY_POINTS",
    "MAX_PRIORITY",
    "MAX_THERMAL_MM",
    "MAX_ZONES",
    "ZoneArgs",
    "apply_zones",
    "parse_zones_args",
    "prepare_zones",
]

ACTIONS = ("add", "update", "delete")
MAX_ZONES = 16
MAX_POLY_POINTS = 256
MAX_LAYERS = 32
MAX_NET_CHARS = 256
MAX_LAYER_CHARS = 64
MAX_COORD_MM = 1000.0
MAX_PRIORITY = 100
MAX_CLEARANCE_MM = 10.0
MAX_THERMAL_MM = 5.0
M11_TIMEOUT_S = 30.0
_NAME = re.compile(NAME_PATTERN)
_ZONE_KEYS = {"name", "net", "layers", "outline", "priority", "clearance", "thermal"}

Outline = Literal["board"] | tuple[Point, ...]


# -- shared helpers (pcb_fanout, pcb_stitch, pcb_tidy_silk) ------------------------------------


def invalid(why: str, suggestion: str = "See the tool schema.") -> StructuredToolError:
    return StructuredToolError("Invalid argument", why, suggestion, code="invalid_argument")


def keys(args: Any, allowed: set[str], what: str = "the arguments") -> Mapping[str, Any]:
    if not isinstance(args, dict):
        raise invalid(f"{what} must be an object")
    extra = sorted(set(args) - allowed)
    if extra:
        raise invalid(f"unknown keys in {what}: {', '.join(map(str, extra[:5]))}")
    return args


def number(value: Any, what: str, lo: float, hi: float, *, lo_open: bool = True) -> float:
    """A finite number in (lo, hi] (or [lo, hi] with `lo_open=False`)."""
    ok = isinstance(value, int | float) and not isinstance(value, bool)
    v = float(value) if ok else math.nan
    if not math.isfinite(v) or v > hi or (v <= lo if lo_open else v < lo):
        bounds = f"{'>' if lo_open else '>='} {lo:g} and <= {hi:g}"
        raise invalid(f"{what} must be a number {bounds}")
    return v


def text(value: Any, what: str, chars: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= chars:
        raise invalid(f"{what} must be a string of 1-{chars} characters")
    return value


def closest(name: str, pool: Sequence[str]) -> list[str]:
    return difflib.get_close_matches(name, sorted(set(pool)), n=5, cutoff=0.4)


def board_nets(model: BoardModel) -> set[str]:
    """Every net with a pad, track, via or zone on the board."""
    out = {q.net for p in model.parts for q in p.pads if q.net}
    out |= {t.net for t in model.tracks if t.net} | {v.net for v in model.vias if v.net}
    out |= {z.net for z in model.zones if z.net and not z.rule_area}
    return out


def check_net(model: BoardModel, net: str) -> None:
    """`not_found` with the closest names when the board has no net `net`."""
    known = board_nets(model)
    if net not in known:
        near = closest(net, list(known))
        raise StructuredToolError(
            f"Net {net!r} not found",
            f"the board has no net {net!r}",
            f"Closest: {', '.join(near)}" if near else "Check the net names with pcb_analyze.",
            code="not_found",
            details={"nets": [net], "closest": {net: near}},
        )


def check_layers(model: BoardModel, layers: Sequence[str]) -> None:
    copper = list(model.copper_layers)
    bad = [n for n in layers if n not in copper]
    if bad:
        raise StructuredToolError(
            f"Layer {bad[0]!r} not found",
            f"{', '.join(bad[:10])} is not an enabled copper layer (enabled: {', '.join(copper)})",
            "Pass enabled copper layers, or change the layer count with pcb_set_rules.",
            code="not_found",
            details={"layers": bad[:10], "closest": {n: closest(n, copper) for n in bad[:10]}},
        )


async def m11_report(ctx: ToolContext, model: BoardModel) -> dict[str, Any] | None:
    """M11 (plane fill per layer, void crossings) of `model` - fills kept, M11 needs them -
    through `run_cpu(metrics_job, ...)` (B-C7: never `compute_metrics` directly). A
    worker timeout gives None: the write itself succeeded."""
    doc: BoardDoc = await read_board_doc(ctx)
    try:
        rep = await run_cpu(
            metrics_job, model, doc, None, None, only=frozenset({"M11"}), timeout_s=M11_TIMEOUT_S
        )
    except StructuredToolError as exc:
        if exc.code == "board_compute_timeout":
            return None
        raise
    metrics = metrics_data(rep, model)["metrics"]
    return metrics[0] if metrics else None


# -- pcb_zones arguments -----------------------------------------------------------------


@dataclass(frozen=True)
class ZoneArgs:
    """One `zones` entry; None = not given (update keeps the current value)."""

    name: str
    net: str | None = None
    layers: tuple[str, ...] | None = None
    outline: Outline | None = None
    priority: int | None = None
    clearance: float | None = None
    thermal: tuple[float, float] | None = None  # (gap, spoke_width)


def _polygon(value: Any) -> tuple[Point, ...]:
    o = keys(value, {"polygon"}, "outline")
    pts = o.get("polygon")
    if not isinstance(pts, list) or not 3 <= len(pts) <= MAX_POLY_POINTS:
        raise invalid(f"outline.polygon must have 3-{MAX_POLY_POINTS} points")
    out: list[Point] = []
    for p in pts:
        q = keys(p, {"x", "y"}, "a polygon point")
        x = number(q.get("x"), "x", -MAX_COORD_MM, MAX_COORD_MM, lo_open=False)
        y = number(q.get("y"), "y", -MAX_COORD_MM, MAX_COORD_MM, lo_open=False)
        out.append((x, y))
    return tuple(out)


def _zone_args(value: Any, action: str) -> ZoneArgs:
    z = keys(value, _ZONE_KEYS, "a zone")
    name = z.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise invalid("every zone needs a name (letters, digits, ' _.+-', at most 64)")
    given = set(z) - {"name"}
    if action == "delete" and given:
        raise invalid(f"delete takes the zone name only (got {', '.join(sorted(given))})")
    if action == "update" and not given:
        raise invalid(f"update of zone {name!r} changes nothing: give a field to change")
    net = text(z["net"], "net", MAX_NET_CHARS) if "net" in z else None
    layers: tuple[str, ...] | None = None
    if "layers" in z:
        raw = z["layers"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LAYERS:
            raise invalid(f"layers must be a list of 1-{MAX_LAYERS} copper layers")
        layers = tuple(dict.fromkeys(text(n, "layer", MAX_LAYER_CHARS) for n in raw))
    outline: Outline | None = None
    if "outline" in z:
        outline = "board" if z["outline"] == "board" else _polygon(z["outline"])
    priority: int | None = None
    if "priority" in z:
        p = z["priority"]
        if not isinstance(p, int) or isinstance(p, bool) or not 0 <= p <= MAX_PRIORITY:
            raise invalid(f"priority must be an integer 0-{MAX_PRIORITY}")
        priority = p
    clearance = (
        number(z["clearance"], "clearance", 0.0, MAX_CLEARANCE_MM) if "clearance" in z else None
    )
    thermal: tuple[float, float] | None = None
    if "thermal" in z:
        t = keys(z["thermal"], {"gap", "spoke_width"}, "thermal")
        if set(t) != {"gap", "spoke_width"}:
            raise invalid("thermal needs gap and spoke_width")
        thermal = (
            number(t["gap"], "thermal.gap", 0.0, MAX_THERMAL_MM),
            number(t["spoke_width"], "thermal.spoke_width", 0.0, MAX_THERMAL_MM),
        )
    if action == "add":
        missing = [k for k, v in (("net", net), ("layers", layers)) if v is None]
        if missing:
            raise invalid(f"add of zone {name!r} needs {' and '.join(missing)}")
        outline = "board" if outline is None else outline
        priority = 0 if priority is None else priority
    return ZoneArgs(name, net, layers, outline, priority, clearance, thermal)


def parse_zones_args(args: Any) -> tuple[str, list[ZoneArgs] | None]:
    """(action, zones); zones None = the default stack (`add` only)."""
    a = keys(args, {"action", "zones"})
    action = a.get("action")
    if action not in ACTIONS:
        raise invalid(f"action must be one of {', '.join(ACTIONS)}")
    raw = a.get("zones")
    if raw is None:
        if action != "add":
            raise invalid(f"{action} needs zones (by name)")
        return action, None
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ZONES:
        raise invalid(f"zones must be a list of 1-{MAX_ZONES} zones")
    zones = [_zone_args(z, action) for z in raw]
    names = [z.name for z in zones]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise invalid(f"zone names must be unique in one call: {', '.join(dupes[:5])}")
    return action, zones


# -- planning ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    action: str
    name: str
    kiid: str | None  # update / delete: the zone
    net: str
    layers: tuple[str, ...]
    polygon: tuple[Point, ...] | None  # None: keep (update)
    priority: int | None
    clearance: float | None
    thermal: tuple[float, float] | None


def _copper_zones(model: BoardModel) -> dict[str, list[Zone]]:
    out: dict[str, list[Zone]] = {}
    for z in model.zones:
        if not z.rule_area:
            out.setdefault(z.name, []).append(z)
    return out


def _board_polygon(model: BoardModel) -> tuple[Point, ...]:
    o = model.outline
    if o.kind != "rect":
        raise invalid(
            "outline 'board' needs a rectangular board outline"
            + (" (the board has none)" if o.kind == "none" else " (this one is not)"),
            "Set the outline with pcb_set_outline, or pass outline.polygon.",
        )
    return tuple(rect_poly(o.x, o.y, o.w, o.h, o.corner_radius))


def plan_zones(model: BoardModel, action: str, zones: Sequence[ZoneArgs]) -> list[_Plan]:
    """Check `zones` against the board (unknown net / layer / zone → `not_found` with the
    closest names; `add` of an existing name → `invalid_argument`) and resolve defaults
    (clearance = the net's class clearance)."""
    by_name = _copper_zones(model)
    plans: list[_Plan] = []
    for z in zones:
        found = by_name.get(z.name, [])
        if action == "add" and found:
            raise invalid(
                f"zone {z.name!r} already exists",
                "Use action 'update' to change it, or pick another name.",
            )
        if action != "add":
            if not found:
                near = closest(z.name, list(by_name))
                raise StructuredToolError(
                    f"Zone {z.name!r} not found",
                    f"the board has no copper zone named {z.name!r}",
                    f"Closest: {', '.join(near)}" if near else "List the zones with pcb_status.",
                    code="not_found",
                    details={"zones": [z.name], "closest": {z.name: near}},
                )
            if len(found) > 1:
                raise invalid(
                    f"{len(found)} zones are named {z.name!r}",
                    "Rename them in KiCad first; a name must pick one zone.",
                )
        current = found[0] if found else None
        net = z.net if z.net is not None else (current.net if current else "")
        layers = z.layers if z.layers is not None else (current.layers if current else ())
        if z.net is not None:
            check_net(model, z.net)
        if z.layers is not None:
            check_layers(model, z.layers)
        polygon: tuple[Point, ...] | None = None
        if z.outline == "board":
            polygon = _board_polygon(model)
        elif z.outline is not None:
            polygon = z.outline
        clearance = z.clearance
        if action == "add" and clearance is None:
            clearance = class_params(model, net).clearance
        plans.append(
            _Plan(
                action,
                z.name,
                current.kiid if current else None,
                net,
                layers,
                polygon,
                z.priority,
                clearance,
                z.thermal,
            )
        )
    return plans


# -- KiCad writes --------------------------------------------------------------------------


def _outline(polygon: Sequence[Point]) -> PolygonWithHoles:
    line = PolyLine()
    for x, y in polygon:
        line.append(PolyLineNode.from_xy(mm_to_nm(x), mm_to_nm(y)))
    line.closed = True
    poly = PolygonWithHoles()
    poly.outline = line
    return poly


def _apply_fields(z: KipyZone, p: _Plan, nets: Mapping[str, Any]) -> None:
    if p.net and (p.action == "add" or z.net is None or z.net.name != p.net):
        net = nets.get(p.net)
        if net is None:
            raise StructuredToolError(
                "Net not on the board",
                f"KiCad has no net {p.net!r} (the board changed since it was read)",
                "Re-read the board (pcb_status) and retry.",
                code="not_found",
            )
        z.net = net
    if p.layers:
        z.layers = [layer_from_canonical_name(n) for n in p.layers]
    if p.polygon is not None:
        z.outline = _outline(p.polygon)
    if p.priority is not None:
        z.priority = p.priority
    if p.clearance is not None:
        z.clearance = mm_to_nm(p.clearance)
    if p.thermal is not None:
        conn = z.connection
        if conn is not None:
            conn.zone_connection = ZoneConnectionStyle.ZCS_THERMAL
            conn.thermal_spokes.gap = mm_to_nm(p.thermal[0])
            conn.thermal_spokes.width = mm_to_nm(p.thermal[1])


def _write(h: KicadHandle, plans: Sequence[_Plan], saved: dict[str, bytes]) -> list[str]:
    """Stage 1 (inside its commit): store the pre-call protobufs of the zones it updates
    or deletes (`saved`, for the undo), then create / update / delete. Returns the
    created KIIDs."""
    board = h.board()
    nets = {n.name: n for n in board.get_nets()}
    live = {str(z.id.value): z for z in board.get_zones()}
    for p in plans:
        if p.kiid is not None:
            z = live.get(p.kiid)
            if z is None:
                raise StructuredToolError(
                    "Zone changed",
                    f"zone {p.name!r} is no longer on the board",
                    "Re-read the board (pcb_status) and retry.",
                    code="not_found",
                )
            saved.setdefault(p.kiid, z.proto.SerializeToString())
    new: list[KipyZone] = []
    for p in plans:
        if p.action != "add":
            continue
        z = KipyZone()
        z.type = ZoneType.ZT_COPPER
        z.name = p.name
        _apply_fields(z, p, nets)
        new.append(z)
    updates = [live[p.kiid] for p in plans if p.action == "update" and p.kiid is not None]
    for p in plans:
        if p.action == "update" and p.kiid is not None:
            _apply_fields(live[p.kiid], p, nets)
    if updates:
        board.update_items(updates)
    gone = [live[p.kiid] for p in plans if p.action == "delete" and p.kiid is not None]
    if gone:
        board.remove_items(gone)
    made = board.create_items(new) if new else []
    return [str(z.id.value) for z in made]


def _rebuild(raw: bytes) -> KipyZone:
    z = KipyZone()
    z.proto.ParseFromString(raw)
    return z


def restore_zones(h: KicadHandle, saved: Mapping[str, bytes]) -> None:
    """Undo of stage 1 (inside a commit): every stored zone back as it was - updated ones
    overwritten, deleted ones re-created with their KIID."""
    board = h.board()
    live = {str(z.id.value) for z in board.get_zones()}
    back = [_rebuild(raw) for k, raw in saved.items() if k in live]
    missing = [_rebuild(raw) for k, raw in saved.items() if k not in live]
    if back:
        board.update_items(back)
    if missing:
        board.create_items(missing)


# -- pcb_zones -----------------------------------------------------------------------------


@dataclass(kw_only=True)
class ZonesChangeSet(BoardChangeSet):
    plans: list[_Plan] = field(default_factory=list)
    read_back: list[BoardModel] = field(default_factory=list)


def _zone_entry(z: Zone, action: str) -> dict[str, Any]:
    return {
        "name": z.name,
        "action": action,
        "kiid": z.kiid,
        "net": z.net,
        "layers": list(z.layers),
        "priority": z.priority,
        "filled_area_mm2": round(z.filled_area_mm2, 2),
    }


def _verify(cs: ZonesChangeSet, made: list[str]) -> Any:
    def verify(m: BoardModel, rep: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        live = {z.kiid: z for z in m.zones}
        entries: list[dict[str, Any]] = []
        adds = iter(made)
        for p in cs.plans:
            if p.action == "delete":
                if p.kiid in live:
                    problems.append(f"zone {p.name} is still on the board")
                entries.append({"name": p.name, "action": "delete", "kiid": p.kiid})
                continue
            kiid = next(adds, None) if p.action == "add" else p.kiid
            z = live.get(kiid or "")
            if z is None:
                problems.append(f"zone {p.name} is missing")
                continue
            if z.name != p.name or (p.net and z.net != p.net):
                problems.append(f"zone {p.name} has name {z.name!r} / net {z.net!r}")
            if p.layers and tuple(z.layers) != tuple(p.layers):
                problems.append(f"zone {p.name} is on {', '.join(z.layers)}")
            if p.priority is not None and z.priority != p.priority:
                problems.append(f"zone {p.name} has priority {z.priority}")
            got = dict(z.settings).get("clearance", "")
            if p.clearance is not None and (not got or abs(float(got) - p.clearance) > 1e-3):
                problems.append(f"zone {p.name} has clearance {got or 'none'}")
            entries.append(_zone_entry(z, p.action))
        rep["zones"] = entries
        cs.read_back[:] = [m]
        return problems

    return verify


async def prepare_zones(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_zones` (module docstring); refusals before any write."""
    require_board_tools(ctx)
    action, zones = parse_zones_args(args)
    require_board(ctx)
    model = await board_model(ctx)
    await require_placement_approved(ctx, model)
    if zones is None:
        doc = await read_board_doc(ctx)
        cls = await asyncio.to_thread(classify, model, doc)
        specs = default_planes(model, cls)
        if not specs:
            raise invalid(
                "no default plane stack for this board: "
                + (
                    "it has no ground net"
                    if not any(k == "ground" for k in cls.net_kind.values())
                    else f"{len(model.copper_layers)} copper layers (2, 4 or 6 have one)"
                ),
                "Pass zones explicitly.",
            )
        zones = [ZoneArgs(s.name, s.net, s.layers, "board", s.priority) for s in specs]
    plans = plan_zones(model, action, zones)
    saved: dict[str, bytes] = {}
    made: list[str] = []

    async def write(sctx: BoardStageContext) -> None:
        got = await sctx.call(lambda h: _write(h, plans, saved), timeout=BOARD_READ_TIMEOUT_S)
        made[:] = got
        sctx.created.update(got)

    async def undo(sctx: BoardStageContext) -> None:
        if not saved:
            return  # nothing updated or deleted (or the stage never read the board)
        async with sctx.ctx.session.commit("copperchat: pcb_zones: undo", doc="pcb"):
            await sctx.call(lambda h: restore_zones(h, saved), timeout=BOARD_READ_TIMEOUT_S)
        await sctx.refill(timeout_s=REFILL_TIMEOUT_S)

    async def refill(sctx: BoardStageContext) -> None:
        await sctx.refill(timeout_s=REFILL_TIMEOUT_S)

    cs = ZonesChangeSet(
        tool="pcb_zones",
        logical=Logical(len(plans)),
        kind="board_zones",
        board_stages=[
            BoardStage(f"zones {action}", write, undo=undo),
            BoardStage("refill", refill, commit=False),
        ],
        report={"action": action, "logical_changes": len(plans), "budget": None},
        plans=plans,
    )
    cs.verify = _verify(cs, made)
    return cs


async def apply_zones(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    """Apply a `pcb_zones` change set; the report gets M11 of the refilled board."""
    assert isinstance(cs, ZonesChangeSet)
    result = await apply_board_change_set(ctx, cs)
    data = result.data if isinstance(result.data, dict) else {}
    data["m11"] = await m11_report(ctx, cs.read_back[0]) if cs.read_back else None
    data["next"] = "pcb_fanout"
    return result
