"""Board setup tools (spec §7.2 "Setup"): `pcb_set_outline` (rounded-rectangle outline,
mounting holes, keepout rule areas) and `pcb_set_rules` (layer count, fab preset rules,
net classes, custom rules). Every write goes through the board engine (B-C4): one commit
per stage, compensated on failure; non-item state (rules, classes, layer count) is
restored by each stage's `undo`."""

from __future__ import annotations

import asyncio
import copy
import difflib
import math
import os
import re
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from kipy.board_rules import BoardDesignRules, CustomRule
from kipy.board_types import ArcTrack, BoardRectangle, FootprintInstance, Zone
from kipy.errors import ApiError
from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode, Vector2
from kipy.project_types import NetClass, NetClassAssignments, NetClassPatternAssignment
from kipy.proto.board import board_rules_pb2
from kipy.proto.board.board_commands_pb2 import CustomRulesStatus
from kipy.proto.board.board_types_pb2 import BoardLayer, ZoneType
from kipy.proto.common.types import project_settings_pb2
from kipy.proto.common.types.base_types_pb2 import MapMergeMode
from kipy.util.board_layer import canonical_name, layer_from_canonical_name

from copperchat.board.model import BoardModel, Point, board_hash, placement_hash
from copperchat.board.presets import PRESETS, FabPreset
from copperchat.board.staging import natural_key
from copperchat.errors import StructuredToolError
from copperchat.history.restore import remove_created
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.board.read import BOARD_READ_TIMEOUT_S, board_model, require_board
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical
from copperchat.kicad_tools.units import mm_to_nm
from copperchat.projects import Project
from copperchat.tools.spec import ToolContext, ToolResult


def require_board_tools(ctx: ToolContext) -> None:
    """Refuse a board write while COPPERCHAT_BOARD_TOOLS is off (B-C14). The profiles
    already drop the tools then; this guards every other path."""
    if not ctx.settings.board_tools:
        raise StructuredToolError(
            "Board tools are disabled",
            "COPPERCHAT_BOARD_TOOLS is off on this server",
            "Tell the user; an admin can enable board tools.",
            code="board_tools_disabled",
        )


# -- pcb_set_outline -------------------------------------------------------------------

OUTLINE_COST = 2
DEFAULT_HOLE = "MountingHole:MountingHole_2.2mm_M2"
DEFAULT_ORIGIN = (100.0, 100.0)  # top-left corner of the outline
DEFAULT_INSET = 3.0
EDGE_STROKE_NM = 50_000  # 0.05 mm
OUTLINE_TOL_MM = 0.001  # read-back tolerance (outline geometry, hole positions)
KEEPOUT_FLAGS = ("tracks", "vias", "copper", "footprints")
ALL_COPPER = "all_copper"
HOLE_LIB_PREFIX = "MountingHole"
# Library lookups (lazy per-library load) and the hole stage's placements.
LIBRARY_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class HolesArgs:
    footprint: str
    inset: float
    positions: tuple[Point, ...] | None  # None: the 4 corners inset by `inset`


@dataclass(frozen=True)
class KeepoutArgs:
    name: str
    polygon: tuple[Point, ...]
    layers: tuple[str, ...]  # layer names or ALL_COPPER, resolved in prepare
    no: frozenset[str]


@dataclass(frozen=True)
class OutlineArgs:
    x: float
    y: float
    w: float
    h: float
    corner_radius: float
    holes: HolesArgs | None  # None: no hole is touched
    keepouts: tuple[KeepoutArgs, ...]


def _invalid(why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(
        "Cannot set the board outline", why, suggestion, code="invalid_argument"
    )


def _xy(p: dict[str, Any]) -> Point:
    return (float(p["x"]), float(p["y"]))


def _keepout(k: dict[str, Any]) -> KeepoutArgs:
    name = str(k["name"])
    if ("polygon" in k) == ("rect" in k):
        raise _invalid(
            f"keepout {name!r} needs exactly one of polygon or rect",
            "Give either polygon (3-256 points) or rect {x, y, w, h}.",
        )
    if "rect" in k:
        r = k["rect"]
        x, y, w, h = (float(r[c]) for c in ("x", "y", "w", "h"))
        polygon: tuple[Point, ...] = ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
    else:
        polygon = tuple(_xy(p) for p in k["polygon"])
    return KeepoutArgs(name, polygon, tuple(dict.fromkeys(k["layers"])), frozenset(k["no"]))


def parse_outline_args(args: dict[str, Any]) -> OutlineArgs:
    """The arguments of `pcb_set_outline` with defaults applied, plus the checks the
    schema cannot express (corner radius, inset, hole positions inside the outline,
    one shape and a unique name per keepout); `invalid_argument` otherwise."""
    w, h = float(args["width"]), float(args["height"])
    r = float(args.get("corner_radius", 0.0))
    if r > min(w, h) / 2:
        raise _invalid(
            f"corner_radius {r:g} mm is more than half the shorter side ({min(w, h) / 2:g} mm)",
            "Use a corner radius of at most min(width, height) / 2.",
        )
    x, y = _xy(args["origin"]) if "origin" in args else DEFAULT_ORIGIN
    holes = None
    if "holes" in args:
        ha = args["holes"]
        inset = float(ha.get("inset", DEFAULT_INSET))
        positions = tuple(_xy(p) for p in ha["positions"]) if "positions" in ha else None
        if positions is None and 2 * inset >= min(w, h):
            raise _invalid(
                f"inset {inset:g} mm puts the corner holes past the middle of the board",
                "Use a smaller inset or give the hole positions.",
            )
        for px, py in positions or ():
            if not (x < px < x + w and y < py < y + h):
                raise _invalid(
                    f"hole position ({px:g}, {py:g}) is outside the outline",
                    "Give hole positions inside the outline (board coordinates, mm).",
                )
        holes = HolesArgs(str(ha.get("footprint", DEFAULT_HOLE)), inset, positions)
    keepouts = tuple(_keepout(k) for k in args.get("keepouts", ()))
    names = [k.name for k in keepouts]
    if len(set(names)) != len(names):
        raise _invalid("two keepouts have the same name", "Give every keepout its own name.")
    return OutlineArgs(x, y, w, h, r, holes, keepouts)


def hole_targets(o: OutlineArgs) -> list[Point]:
    """Where the holes go: the given positions, else the 4 corners inset by `inset`
    (top-left, top-right, bottom-left, bottom-right)."""
    if o.holes is None:
        return []
    if o.holes.positions is not None:
        return list(o.holes.positions)
    i = o.holes.inset
    x0, y0, x1, y1 = o.x + i, o.y + i, o.x + o.w - i, o.y + o.h - i
    return [(x0, y0), (x1, y0), (x0, y1), (x1, y1)]


def _nm_xy(p: Point) -> Vector2:
    return Vector2.from_xy(mm_to_nm(p[0]), mm_to_nm(p[1]))


def is_hole_lib(lib_id: str) -> bool:
    """A `MountingHole*` footprint (library nickname or footprint name)."""
    nickname, _, name = lib_id.partition(":")
    return nickname.startswith(HOLE_LIB_PREFIX) or name.startswith(HOLE_LIB_PREFIX)


def _is_hole(fp: FootprintInstance) -> bool:
    return is_hole_lib(str(fp.definition.id))


def _has_symbol(fp: FootprintInstance) -> bool:
    return len(fp.proto.symbol_path.path) > 0


def _is_tool_hole(fp: FootprintInstance) -> bool:
    """A hole an earlier `pcb_set_outline` placed: no symbol and the three attributes
    `_mark_tool_holes` sets. A hole the user added by hand lacks them and is kept."""
    a = fp.attributes
    return (
        _is_hole(fp)
        and not _has_symbol(fp)
        and a.not_in_schematic
        and a.exclude_from_bill_of_materials
        and a.exclude_from_position_files
    )


def _ref(fp: FootprintInstance) -> str:
    return str(fp.reference_field.text.value)


def _next_free(used: set[str]) -> str:
    n = 1
    while f"H{n}" in used:
        n += 1
    return f"H{n}"


def _write_outline(h: KicadHandle, o: OutlineArgs) -> str:
    """Stage 1: one (rounded) rectangle created on Edge.Cuts; the old Edge.Cuts shapes
    go in the last stage (`_remove_replaced`). Returns the new rectangle's KIID."""
    board = h.board()
    rect = BoardRectangle()
    rect.layer = BoardLayer.BL_Edge_Cuts
    rect.top_left = _nm_xy((o.x, o.y))
    rect.bottom_right = _nm_xy((o.x + o.w, o.y + o.h))
    rect.proto.shape.rectangle.corner_radius.value_nm = mm_to_nm(o.corner_radius)
    rect.proto.shape.attributes.stroke.width.value_nm = EDGE_STROKE_NM
    (made,) = board.create_items(rect)
    return str(made.id.value)


def _write_holes(h: KicadHandle, holes: HolesArgs, targets: list[Point]) -> dict[str, Any]:
    """Stage 2, first commit: schematic holes (with a symbol) moved to the first targets
    and locked, the remaining targets get new holes from the library; the earlier
    tool-placed holes are removed in the last stage (`_remove_replaced`), their refs are
    free for the new ones. Plans from one read (inside the open commit reads show the
    pre-commit board). Returns the report plus `created`: KIID -> planned ref of the new
    holes."""
    board = h.board()
    fps = list(board.get_footprints())
    holes_now = [fp for fp in fps if _is_hole(fp)]
    schematic = sorted(
        (fp for fp in holes_now if _has_symbol(fp)), key=lambda f: natural_key(_ref(f))
    )
    tool = sorted((fp for fp in holes_now if _is_tool_hole(fp)), key=lambda f: natural_key(_ref(f)))
    foreign = sorted(
        (_ref(fp) for fp in holes_now if not _has_symbol(fp) and not _is_tool_hole(fp)),
        key=natural_key,
    )
    moved = []
    for fp, target in zip(schematic, targets, strict=False):
        fp.position = _nm_xy(target)
        fp.locked = True
        moved.append(fp)
    if moved:
        board.update_items(moved)
    removed = {str(fp.id.value) for fp in tool}
    used = {_ref(fp) for fp in fps if str(fp.id.value) not in removed}
    created: dict[str, str] = {}
    rest = targets[len(schematic) :]
    if rest:
        h.kicad.get_library_footprint(holes.footprint, board.document)  # lazy library load
    for target in rest:
        fp = board.place_footprint_from_library(holes.footprint, _nm_xy(target))
        ref = _next_free(used)
        used.add(ref)
        created[str(fp.id.value)] = ref
    return {
        "moved": [_ref(fp) for fp in moved],
        "placed": list(created.values()),
        "removed": [_ref(fp) for fp in tool],
        "kept": foreign,  # symbol-less holes the user placed: never touched
        "created": created,
    }


def _mark_tool_holes(h: KicadHandle, created: dict[str, str]) -> None:
    """Stage 2, second commit (a placed footprint can only be updated after the push
    that created it): ref, locked, "not in schematic", excluded from BOM and position
    files, so a tool hole raises no parity marker and never reaches BOM/CPL."""
    board = h.board()
    update = []
    for fp in board.get_footprints():
        ref = created.get(str(fp.id.value))
        if ref is None:
            continue
        fp.reference_field.text.value = ref
        fp.locked = True
        attrs = fp.attributes
        attrs.not_in_schematic = True
        attrs.exclude_from_bill_of_materials = True
        attrs.exclude_from_position_files = True
        update.append(fp)
    if len(update) != len(created):
        raise RuntimeError(f"placed {len(created)} hole(s) but found {len(update)} afterwards")
    if update:
        board.update_items(update)


def _write_keepouts(h: KicadHandle, keepouts: tuple[KeepoutArgs, ...]) -> list[str]:
    """Stage 3: the rule areas created; same-named older ones go in the last stage
    (`_remove_replaced`), other rule areas are kept. Returns the created zones' KIIDs."""
    board = h.board()
    zones = []
    for k in keepouts:
        z = Zone()
        z.type = ZoneType.ZT_RULE_AREA
        z.layers = [layer_from_canonical_name(name) for name in k.layers]
        z.name = k.name
        ra = z.proto.rule_area_settings
        ra.keepout_tracks = "tracks" in k.no
        ra.keepout_vias = "vias" in k.no
        ra.keepout_copper = "copper" in k.no
        ra.keepout_footprints = "footprints" in k.no
        line = PolyLine()
        for p in k.polygon:
            line.append(PolyLineNode.from_xy(mm_to_nm(p[0]), mm_to_nm(p[1])))
        line.closed = True
        poly = PolygonWithHoles()
        poly.outline = line
        z.outline = poly
        zones.append(z)
    made = board.create_items(zones)
    return [str(z.id.value) for z in made]


def _remove_replaced(h: KicadHandle, o: OutlineArgs, created: set[str]) -> None:
    """Last stage, one commit: what the new items replace - every older Edge.Cuts
    shape, the older tool-placed holes (with `holes`) and the older rule areas of the
    same names. All deletions sit here, after every addition was pushed: a failure
    before this stage leaves only additions (compensation removes them), a failure in
    it drops its commit."""
    board = h.board()
    names = {k.name for k in o.keepouts}
    doomed: list[Any] = [
        s
        for s in board.get_shapes()
        if s.layer == BoardLayer.BL_Edge_Cuts and str(s.id.value) not in created
    ]
    if o.holes is not None:
        doomed += [
            fp
            for fp in board.get_footprints()
            if _is_tool_hole(fp) and str(fp.id.value) not in created
        ]
    doomed += [
        z
        for z in board.get_zones()
        if z.is_rule_area() and str(z.name) in names and str(z.id.value) not in created
    ]
    if doomed:
        board.remove_items(doomed)


def _closest_footprints(h: KicadHandle, lib_id: str) -> list[str]:
    """Up to 5 footprints of the requested library close to `lib_id` (the mounting hole
    library when that one is unknown or empty)."""
    nickname, _, _ = lib_id.partition(":")
    for nick in dict.fromkeys((nickname, HOLE_LIB_PREFIX)):
        try:
            names = [f"{nick}:{i.proto.entry_name}" for i in h.kicad.get_library_footprints([nick])]
        except ApiError:
            names = []
        if names:
            return difflib.get_close_matches(lib_id, names, n=5, cutoff=0.0)
    return []


def _lookup_footprint(h: KicadHandle, lib_id: str) -> list[str] | None:
    """None when `lib_id` resolves (this also loads its library), else the closest ids."""
    try:
        h.kicad.get_library_footprint(lib_id, h.board().document)
    except (ApiError, ValueError):
        return _closest_footprints(h, lib_id)
    return None


def _enabled_layers(h: KicadHandle) -> list[str]:
    return [canonical_name(layer) for layer in h.board().get_enabled_layers()]


def _resolve_layers(o: OutlineArgs, enabled: list[str], copper: tuple[str, ...]) -> OutlineArgs:
    """Keepout layer names checked against the board's enabled layers; ALL_COPPER is
    expanded to the copper layers."""
    keepouts = []
    for k in o.keepouts:
        names: list[str] = []
        for name in k.layers:
            if name == ALL_COPPER:
                names += copper
            elif name in enabled:
                names.append(name)
            else:
                raise _invalid(
                    f"keepout {k.name!r}: layer {name!r} is not enabled on this board",
                    f"Use {ALL_COPPER!r} or enabled layers: {', '.join(enabled)}.",
                )
        keepouts.append(replace(k, layers=tuple(dict.fromkeys(names))))
    return replace(o, keepouts=tuple(keepouts))


def _verify_outline(
    o: OutlineArgs, targets: list[Point], before_hash: str
) -> Callable[[BoardModel, dict[str, Any]], list[str]]:
    def verify(m: BoardModel, report: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        got = m.outline
        want = (o.x, o.y, o.w, o.h, o.corner_radius)
        have = (got.x, got.y, got.w, got.h, got.corner_radius)
        edge = [g for g in m.graphics if g.layer == "Edge.Cuts"]
        if got.kind != "rect" or len(edge) != 1:
            problems.append(f"Edge.Cuts holds {len(edge)} shape(s), not one rectangle")
        elif any(abs(a - b) > OUTLINE_TOL_MM for a, b in zip(want, have, strict=True)):
            problems.append(f"outline reads back as {have}, expected {want}")
        holes = [p for p in m.parts if is_hole_lib(p.lib_id)]
        for t in targets:
            if not any(
                abs(p.x - t[0]) <= OUTLINE_TOL_MM and abs(p.y - t[1]) <= OUTLINE_TOL_MM
                for p in holes
            ):
                problems.append(f"no mounting hole at ({t[0]:g}, {t[1]:g})")
        areas = {z.name for z in m.zones if z.rule_area}
        problems += [f"keepout {k.name!r} missing" for k in o.keepouts if k.name not in areas]
        # The report's read-back part (the model after every stage's push).
        after_hash = placement_hash(m)
        report["unplaced"] = sum(1 for p in m.parts if not m.placed(p))
        report["placement_hash"] = {
            "before": before_hash,
            "after": after_hash,
            "changed": after_hash != before_hash,
        }
        report["next"] = "pcb_set_rules"
        return problems

    return verify


async def prepare_outline(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_set_outline`: arguments, the hole footprint and the keepout layers are
    checked before anything is written (unknown footprint → `not_found` + `closest`)."""
    require_board_tools(ctx)
    require_board(ctx)
    o = parse_outline_args(args)
    before = await board_model(ctx)
    if o.holes is not None:
        lib_id = o.holes.footprint
        closest = await ctx.session.call(
            lambda h: _lookup_footprint(h, lib_id), timeout=LIBRARY_TIMEOUT_S
        )
        if closest is not None:
            raise StructuredToolError(
                f"Footprint {lib_id} not found",
                "no footprint library of this project or the global table has it",
                f"Use one of: {', '.join(closest)}"
                if closest
                else "Search the footprint libraries for a mounting hole footprint.",
                code="not_found",
                details={"closest": closest},
            )
    if o.keepouts:
        enabled = await ctx.session.call(_enabled_layers)
        o = _resolve_layers(o, enabled, before.copper_layers)
    return _plan_outline(o, placement_hash(before))


def _plan_outline(o: OutlineArgs, before_hash: str) -> BoardChangeSet:
    targets = hole_targets(o)
    report: dict[str, Any] = {
        "outline": {
            "x": o.x,
            "y": o.y,
            "width": o.w,
            "height": o.h,
            "corner_radius": o.corner_radius,
        },
        "holes": None,
        "keepouts": [k.name for k in o.keepouts],
    }

    async def outline(sctx: BoardStageContext) -> None:
        made = await sctx.call(lambda h: _write_outline(h, o), timeout=BOARD_READ_TIMEOUT_S)
        sctx.created.add(made)

    async def holes(sctx: BoardStageContext) -> None:
        assert o.holes is not None
        spec = o.holes
        # Own commits (commit=False): the new holes are updated after their push.
        session = sctx.ctx.session
        async with session.commit("copperchat: pcb_set_outline", doc="pcb"):
            result = await sctx.call(
                lambda h: _write_holes(h, spec, targets), timeout=BOARD_READ_TIMEOUT_S
            )
        created: dict[str, str] = result.pop("created")
        sctx.created.update(created)
        if created:
            async with session.commit("copperchat: pcb_set_outline", doc="pcb"):
                await sctx.call(
                    lambda h: _mark_tool_holes(h, created), timeout=BOARD_READ_TIMEOUT_S
                )
        sctx.report["holes"] = result

    async def keepouts(sctx: BoardStageContext) -> None:
        made = await sctx.call(
            lambda h: _write_keepouts(h, o.keepouts), timeout=BOARD_READ_TIMEOUT_S
        )
        sctx.created.update(made)

    async def remove_replaced(sctx: BoardStageContext) -> None:
        created = set(sctx.created)
        await sctx.call(lambda h: _remove_replaced(h, o, created), timeout=BOARD_READ_TIMEOUT_S)

    stages = [BoardStage("outline", outline)]
    if o.holes is not None:
        stages.append(BoardStage("mounting holes", holes, commit=False))
    if o.keepouts:
        stages.append(BoardStage("keepouts", keepouts))
    stages.append(BoardStage("remove replaced items", remove_replaced))
    return BoardChangeSet(
        tool="pcb_set_outline",
        logical=Logical(OUTLINE_COST),
        kind="board_set_outline",
        board_stages=stages,
        verify=_verify_outline(o, targets, before_hash),
        report=report,
    )


async def apply_outline(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    assert isinstance(cs, BoardChangeSet)
    return await apply_board_change_set(ctx, cs)


# -- pcb_set_rules ---------------------------------------------------------------------

RULES_COST = 2
RULES_KEYS = ("layers", "preset", "overrides", "net_classes", "custom_rules", "stackup")
LAYER_COUNTS = (2, 4, 6)
MAX_OVERRIDE_MM = 10.0
MAX_SIZE_MM = 100.0  # widths, clearances (plan call 6)
MAX_DRILL_MM = 10.0
MAX_NET_CLASSES = 16
MAX_PATTERNS = 50
MAX_PATTERN_CHARS = 256
MAX_CUSTOM_RULES = 32
MAX_CONDITION_CHARS = 512
MIN_REASON_CHARS, MAX_REASON_CHARS = 8, 300
MAX_STACKUP_CHARS = 64
MAX_REFUSED_ITEMS = 20
RULE_TOL_MM = 0.0005  # read-back tolerance of every rule value
# Names of rules and net classes: specs_setup.NAME_PATTERN (plan call 6; a copy, because
# specs_setup imports this module).
RULE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+-]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# FabPreset minimum -> KiCad board design rule (`MinimumConstraints` field), B-C8.
RULE_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "min_space": "min_clearance",
        "min_track": "min_track_width",
        "min_via_diameter": "min_via_size",
        "min_drill": "min_through_drill",
        "min_annular": "min_via_annular_width",
        "copper_to_edge": "copper_edge_clearance",
        "hole_to_hole": "hole_to_hole_min",
        "silk_min_height": "min_silk_text_height",
        "silk_min_line": "min_silk_text_thickness",
    }
)
# custom_rules[].constraint -> KiCad custom rule constraint type.
RULE_CONSTRAINTS: Mapping[str, str] = MappingProxyType(
    {
        "clearance": "CRCT_CLEARANCE",
        "track_width": "CRCT_TRACK_WIDTH",
        "via_diameter": "CRCT_VIA_DIAMETER",
        "hole_size": "CRCT_HOLE_SIZE",
        "hole_to_hole": "CRCT_HOLE_TO_HOLE",
        "hole_clearance": "CRCT_HOLE_CLEARANCE",
        "edge_clearance": "CRCT_EDGE_CLEARANCE",
        "annular_width": "CRCT_ANNULAR_WIDTH",
        "courtyard_clearance": "CRCT_COURTYARD_CLEARANCE",
        "silk_clearance": "CRCT_SILK_CLEARANCE",
        "text_height": "CRCT_TEXT_HEIGHT",
        "diff_pair_gap": "CRCT_DIFF_PAIR_GAP",
    }
)
_CLASS_KEYS = frozenset(
    {
        "name",
        "track_width",
        "clearance",
        "via_diameter",
        "via_drill",
        "diff_pair_width",
        "diff_pair_gap",
        "patterns",
    }
)
_CLASS_REQUIRED = frozenset({"name", "track_width", "clearance", "via_diameter", "via_drill"})
_CUSTOM_KEYS = frozenset({"name", "condition", "constraint", "min", "max", "reason"})
_CUSTOM_REQUIRED = frozenset({"name", "condition", "constraint", "reason"})


@dataclass(frozen=True)
class NetClassArgs:
    name: str
    track_width: float
    clearance: float
    via_diameter: float
    via_drill: float
    diff_pair_width: float | None
    diff_pair_gap: float | None
    patterns: tuple[str, ...]


@dataclass(frozen=True)
class CustomRuleArgs:
    name: str
    condition: str
    constraint: str  # a key of RULE_CONSTRAINTS
    min: float | None
    max: float | None
    reason: str


@dataclass(frozen=True)
class RulesArgs:
    layers: int | None
    preset: str | None
    overrides: Mapping[str, float]  # FabPreset field -> mm
    net_classes: tuple[NetClassArgs, ...]
    custom_rules: tuple[CustomRuleArgs, ...]
    stackup: str | None


def _rules_invalid(why: str, suggestion: str = "See the tool schema.") -> StructuredToolError:
    return StructuredToolError(
        "Cannot set the board rules", why, suggestion, code="invalid_argument"
    )


def _rules_number(value: Any, name: str, lo: float, hi: float, *, open_lo: bool = True) -> float:
    ok = isinstance(value, int | float) and not isinstance(value, bool)
    try:
        v = float(value) if ok else math.nan
    except OverflowError:  # a huge JSON int
        v = math.nan
    if not math.isfinite(v) or v > hi or v < lo or (open_lo and v <= lo):
        raise _rules_invalid(
            f"{name} must be a number {'>' if open_lo else '>='} {lo:g} and <= {hi:g} (mm)"
        )
    return v


def _rules_text(value: Any, name: str, lo: int, hi: int) -> str:
    if not isinstance(value, str) or not lo <= len(value) <= hi or _CONTROL.search(value):
        raise _rules_invalid(f"{name} must be {lo}-{hi} characters without control characters")
    return value


def _dru_text(value: Any, name: str, lo: int, hi: int) -> str:
    """Text KiCad writes into a `.kicad_dru` s-expression string (condition, reason):
    no double quote or backslash, so nothing can end the string early."""
    text = _rules_text(value, name, lo, hi)
    if '"' in text or "\\" in text:
        raise _rules_invalid(
            f"{name} must not contain double quotes or backslashes",
            "Use single quotes in conditions, e.g. A.NetClass == 'Power'.",
        )
    return text


def _rules_name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not RULE_NAME.match(value):
        raise _rules_invalid(f"{name} must match {RULE_NAME.pattern}")
    return value


def _rules_obj(
    value: Any, name: str, keys: frozenset[str], required: frozenset[str]
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _rules_invalid(f"{name} must be an object")
    unknown = sorted(set(value) - keys)
    missing = sorted(required - set(value))
    if unknown or missing:
        raise _rules_invalid(
            f"{name}: "
            + "; ".join(
                ([f"unknown {', '.join(unknown)}"] if unknown else [])
                + ([f"missing {', '.join(missing)}"] if missing else [])
            )
        )
    return value


def _rules_list(value: Any, name: str, lo: int, hi: int) -> list[Any]:
    if not isinstance(value, list) or not lo <= len(value) <= hi:
        raise _rules_invalid(f"{name} must be a list of {lo}-{hi} entries")
    return value


def _unique(names: list[str], what: str) -> None:
    if len(set(names)) != len(names):
        raise _rules_invalid(
            f"two {what} have the same name", f"Give every {what[:-1]} its own name."
        )


def _net_class(i: int, value: Any) -> NetClassArgs:
    at = f"net_classes[{i}]"
    c = _rules_obj(value, at, _CLASS_KEYS, _CLASS_REQUIRED)

    def size(key: str, hi: float = MAX_SIZE_MM) -> float:
        return _rules_number(c[key], f"{at}.{key}", 0.0, hi)

    def opt(key: str) -> float | None:
        return size(key) if c.get(key) is not None else None

    nc = NetClassArgs(
        name=_rules_name(c["name"], f"{at}.name"),
        track_width=size("track_width"),
        clearance=size("clearance"),
        via_diameter=size("via_diameter"),
        via_drill=size("via_drill", MAX_DRILL_MM),
        diff_pair_width=opt("diff_pair_width"),
        diff_pair_gap=opt("diff_pair_gap"),
        patterns=tuple(
            dict.fromkeys(
                _rules_text(p, f"{at}.patterns[]", 1, MAX_PATTERN_CHARS)
                for p in _rules_list(c.get("patterns", []), f"{at}.patterns", 1, MAX_PATTERNS)
            )
        ),
    )
    if nc.via_drill >= nc.via_diameter:
        raise _rules_invalid(
            f"{at}: via_drill {nc.via_drill:g} mm is not smaller than "
            f"via_diameter {nc.via_diameter:g} mm"
        )
    return nc


def _custom_rule(i: int, value: Any) -> CustomRuleArgs:
    at = f"custom_rules[{i}]"
    r = _rules_obj(value, at, _CUSTOM_KEYS, _CUSTOM_REQUIRED)
    constraint = r["constraint"]
    if constraint not in RULE_CONSTRAINTS:
        raise _rules_invalid(f"{at}.constraint must be one of {', '.join(RULE_CONSTRAINTS)}")
    lo = (
        _rules_number(r["min"], f"{at}.min", 0.0, MAX_SIZE_MM, open_lo=False)
        if r.get("min") is not None
        else None
    )
    hi = (
        _rules_number(r["max"], f"{at}.max", 0.0, MAX_SIZE_MM, open_lo=False)
        if r.get("max") is not None
        else None
    )
    if lo is None and hi is None:
        raise _rules_invalid(f"{at} needs min or max (mm)")
    if lo is not None and hi is not None and lo > hi:
        raise _rules_invalid(f"{at}: min {lo:g} mm is above max {hi:g} mm")
    reason = _dru_text(r["reason"], f"{at}.reason", MIN_REASON_CHARS, MAX_REASON_CHARS)
    if len(reason.strip()) < MIN_REASON_CHARS:
        raise _rules_invalid(
            f"{at}.reason must say why the exception is needed ({MIN_REASON_CHARS}+ characters)"
        )
    return CustomRuleArgs(
        name=_rules_name(r["name"], f"{at}.name"),
        condition=_dru_text(r["condition"], f"{at}.condition", 1, MAX_CONDITION_CHARS),
        constraint=constraint,
        min=lo,
        max=hi,
        reason=reason.strip(),
    )


def parse_rules_args(args: Any) -> RulesArgs:
    """The arguments of `pcb_set_rules`, checked completely (the adapter does not
    validate the schema): bounds, closed objects, names, at least one field; plus what
    JSON Schema cannot express (unique names, via drill < diameter, min <= max, a
    meaningful reason, no control characters in KiCad rule text). `invalid_argument`
    otherwise. The preset-floor check of `overrides` needs the board's layer count:
    `design_rule_values`."""
    a = _rules_obj(args, "arguments", frozenset(RULES_KEYS), frozenset())
    if not any(a.get(k) is not None for k in RULES_KEYS):
        raise _rules_invalid(
            "no field given",
            f"Give at least one of {', '.join(RULES_KEYS)}.",
        )
    layers = a.get("layers")
    if layers is not None and (
        isinstance(layers, bool) or layers not in LAYER_COUNTS or int(layers) != layers
    ):
        raise _rules_invalid("layers must be 2, 4 or 6")
    if layers is not None:
        layers = int(layers)  # JSON Schema "integer" also admits 4.0
    preset = a.get("preset")
    if preset is not None and preset not in PRESETS:
        raise _rules_invalid(f"preset must be one of {', '.join(PRESETS)}")
    raw = a.get("overrides") or {}
    over = _rules_obj(raw, "overrides", frozenset(RULE_FIELDS), frozenset())
    overrides = {
        k: _rules_number(v, f"overrides.{k}", 0.0, MAX_OVERRIDE_MM) for k, v in over.items()
    }
    classes = tuple(
        _net_class(i, c)
        for i, c in enumerate(
            _rules_list(a.get("net_classes") or [], "net_classes", 0, MAX_NET_CLASSES)
        )
    )
    _unique([c.name for c in classes], "net classes")
    rules = tuple(
        _custom_rule(i, r)
        for i, r in enumerate(
            _rules_list(a.get("custom_rules") or [], "custom_rules", 0, MAX_CUSTOM_RULES)
        )
    )
    _unique([r.name for r in rules], "custom rules")
    stackup = a.get("stackup")
    if stackup is not None:
        stackup = _rules_text(stackup, "stackup", 1, MAX_STACKUP_CHARS)
    return RulesArgs(layers, preset, overrides, classes, rules, stackup)


def plan_stackup(req: RulesArgs) -> None:
    """The stackup branch. P2: KiCad at the pin has no UpdateBoardStackup handler (spike
    G1), so a stackup is always refused; P2b adds the stackup presets here."""
    if req.stackup is None:
        return
    raise StructuredToolError(
        "Cannot set the stackup",
        "setting a stackup needs the patched KiCad base image (phase P2b); this server "
        "uses KiCad's default stackup",
        "Leave stackup out; the board keeps KiCad's default stackup for its layer count.",
        code="stackup_unavailable",
    )


def design_rule_values(
    req: RulesArgs, copper_layers: int
) -> tuple[FabPreset | None, dict[str, float]]:
    """(base preset, {KiCad rule field: mm}) for the design rules stage. The base is the
    given preset, else the JLCPCB preset of the target layer count (`layers`, else the
    board's): it is the floor of every override (an override below it is refused - an
    exception goes into custom_rules with a reason). Without a preset only the
    overrides are written."""
    n = req.layers or copper_layers
    base = PRESETS.get(req.preset or f"jlcpcb-{n}l")
    if base is None:
        if req.overrides:
            raise _rules_invalid(
                f"there is no preset for {n} copper layers to check the overrides against",
                "Pass preset (jlcpcb-2l, -4l or -6l) with the overrides.",
            )
        return None, {}
    for key, value in req.overrides.items():
        floor = float(getattr(base, key))
        if value < floor - 1e-9:
            raise _rules_invalid(
                f"overrides.{key} {value:g} mm is below the {base.id} minimum {floor:g} mm",
                "Overrides may only tighten the fab preset. For an exception below it use "
                "custom_rules with a condition and a reason.",
            )
    values = {RULE_FIELDS[k]: float(getattr(base, k)) for k in RULE_FIELDS} if req.preset else {}
    values.update({RULE_FIELDS[k]: v for k, v in req.overrides.items()})
    return base, values


def _is_copper(layer: Any) -> bool:
    return str(canonical_name(layer)).endswith(".Cu")


def removed_layers(current: int, target: int) -> set[str]:
    """The inner copper layers a layer count change from `current` to `target` drops
    (KiCad keeps In1..In(target-2))."""
    return {f"In{i}.Cu" for i in range(max(target - 1, 1), current - 1)}


def _items_on(h: KicadHandle, layers: set[str]) -> list[dict[str, str]]:
    """Tracks, arcs, zones, graphics and texts on any of `layers` (kind, KIID, layer)."""
    board = h.board()
    out: list[dict[str, str]] = []

    def add(kind: str, item: Any, names: list[str]) -> None:
        out.extend(
            {"kind": kind, "kiid": str(item.id.value), "layer": n} for n in names if n in layers
        )

    for t in board.get_tracks():
        add("arc" if isinstance(t, ArcTrack) else "track", t, [str(canonical_name(t.layer))])
    for z in board.get_zones():
        add("zone", z, [str(canonical_name(x)) for x in z.layers])
    for s in board.get_shapes():
        add("graphic", s, [str(canonical_name(s.layer))])
    for x in board.get_text():
        add("text", x, [str(canonical_name(x.layer))])
    return out


def _layers_refused(target: int, items: list[dict[str, str]]) -> StructuredToolError:
    shown = items[:MAX_REFUSED_ITEMS]
    return StructuredToolError(
        "Cannot set the board rules",
        f"going down to {target} copper layers would delete {len(items)} item(s) on the "
        "removed inner layers: "
        + ", ".join(f"{i['kind']} {i['kiid'][:8]} on {i['layer']}" for i in shown[:6]),
        "Nothing was changed. Move or delete those items first (or keep the layer count).",
        code="invalid_argument",
        details={"items": shown, "count": len(items)},
    )


def _set_layers(h: KicadHandle, target: int, state: dict[str, Any]) -> None:
    """Stage: the copper layer count (non-copper layers kept). Refuses - again, under the
    session lock right before the write - when the drop would delete items."""
    board = h.board()
    current = board.get_copper_layer_count()
    non_copper = [layer for layer in board.get_enabled_layers() if not _is_copper(layer)]
    if target < current:
        items = _items_on(h, removed_layers(current, target))
        if items:
            raise _layers_refused(target, items)
    state["layers"] = (current, non_copper)
    board.set_enabled_layers(target, non_copper)


def _undo_layers(h: KicadHandle, state: dict[str, Any]) -> None:
    if "layers" in state:
        count, non_copper = state["layers"]
        h.board().set_enabled_layers(count, non_copper)


def _set_design_rules(h: KicadHandle, values: dict[str, float], state: dict[str, Any]) -> None:
    """Stage: board design rules (the other rules and sizes are kept)."""
    board = h.board()
    pre = board_rules_pb2.BoardDesignRules()
    pre.CopyFrom(board.get_design_rules().rules.proto)
    new = board_rules_pb2.BoardDesignRules()
    new.CopyFrom(pre)
    for field, mm in values.items():
        getattr(new.constraints, field).value_nm = mm_to_nm(mm)
    state["rules"] = pre
    board.set_design_rules(BoardDesignRules(proto=new))


def _undo_design_rules(h: KicadHandle, state: dict[str, Any]) -> None:
    if "rules" in state:
        h.board().set_design_rules(BoardDesignRules(proto=state["rules"]))


def _set_net_classes(
    h: KicadHandle, classes: tuple[NetClassArgs, ...], state: dict[str, Any]
) -> None:
    """Stage: the given net classes (merged: classes not named are kept, `Default` only
    changes when given) and their patterns (a given class's other patterns removed)."""
    project = api.board_project(h)
    pre = [copy.deepcopy(nc.proto) for nc in project.get_net_classes()]
    pre_assign = project.get_net_class_assignments()
    state["classes"] = (pre, pre_assign)
    existing = {p.name: p for p in pre}
    top = max((p.priority for p in pre if p.name != "Default"), default=-1)
    out: list[NetClass] = []
    for i, c in enumerate(classes):
        if c.name in existing:
            nc = NetClass(copy.deepcopy(existing[c.name]))
        else:
            nc = NetClass(project_settings_pb2.NetClass(name=c.name, priority=top + 1 + i))
        nc.track_width = mm_to_nm(c.track_width)
        nc.clearance = mm_to_nm(c.clearance)
        nc.via_diameter = mm_to_nm(c.via_diameter)
        nc.via_drill = mm_to_nm(c.via_drill)
        if c.diff_pair_width is not None:
            nc.diff_pair_track_width = mm_to_nm(c.diff_pair_width)
        if c.diff_pair_gap is not None:
            nc.diff_pair_gap = mm_to_nm(c.diff_pair_gap)
        out.append(nc)
    project.set_net_classes(out, MapMergeMode.MMM_MERGE)
    named = {c.name: set(c.patterns) for c in classes}
    patterns = [
        NetClassPatternAssignment(
            project_settings_pb2.NetClassPatternAssignment(pattern=p.pattern, netclass="")
        )
        for p in pre_assign.pattern_assignments
        if p.netclass in named and p.pattern not in named[p.netclass]
    ]
    patterns += [
        NetClassPatternAssignment(
            project_settings_pb2.NetClassPatternAssignment(pattern=p, netclass=c.name)
        )
        for c in classes
        for p in c.patterns
    ]
    if patterns:
        project.set_net_class_assignments(
            NetClassAssignments(pattern_assignments=patterns), MapMergeMode.MMM_MERGE
        )


def _undo_net_classes(h: KicadHandle, state: dict[str, Any]) -> None:
    if "classes" not in state:
        return
    pre, pre_assign = state["classes"]
    project = api.board_project(h)
    project.set_net_classes([NetClass(p) for p in pre], MapMergeMode.MMM_REPLACE)
    project.set_net_class_assignments(pre_assign, MapMergeMode.MMM_REPLACE)


def _dru_rel(project: Project) -> str:
    return f"{project.pro_file.stem}.kicad_dru"


def dru_exists(project: Project) -> bool:
    """Whether `<stem>.kicad_dru` exists as the project's own rules file (lstat of the
    unresolved path): missing -> False (this call creates it); a regular file -> True
    (exactly what `document_files` lists as the rules file, without its tree walk);
    anything else (a symlink, a directory, a FIFO) -> `output_path_unsafe`, before any
    rules write."""
    rel = _dru_rel(project)
    path = project.pro_file.parent / rel
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISREG(st.st_mode):  # what document_files lists as the rules file
        return True
    raise StructuredToolError(
        "Cannot write the custom rules",
        f"{rel} in the project directory is not the project's rules file (a symlink, a "
        "directory or an unknown file)",
        "Nothing was changed. Ask the user to move it away, then retry.",
        code="output_path_unsafe",
        details={"path": rel},
    )


def _custom_rule_proto(r: CustomRuleArgs) -> board_rules_pb2.CustomRule:
    rule = board_rules_pb2.CustomRule(name=r.name, condition=r.condition, comments=r.reason)
    con = rule.constraints.add()
    con.type = board_rules_pb2.CustomRuleConstraintType.Value(RULE_CONSTRAINTS[r.constraint])
    if r.min is not None:
        con.numeric.min = mm_to_nm(r.min)
    if r.max is not None:
        con.numeric.max = mm_to_nm(r.max)
    return rule


def _set_custom_rules(
    h: KicadHandle, project: Project, rules: tuple[CustomRuleArgs, ...], state: dict[str, Any]
) -> None:
    """Stage: same-named custom rules replaced, new ones appended (comment = reason)."""
    state["dru_created"] = not dru_exists(project)  # again, under the session lock
    board = h.board()
    current = board.get_custom_design_rules()
    if current.status == CustomRulesStatus.CRS_INVALID:
        raise _rules_invalid(
            f"the project's {_dru_rel(project)} does not parse: {current.error_text}",
            "Nothing was changed. Ask the user to fix the rules file in KiCad first.",
        )
    pre = [copy.deepcopy(r.proto) for r in current.rules]
    given = {r.name: _custom_rule_proto(r) for r in rules}
    merged = [given.pop(p.name, p) for p in pre]
    merged += given.values()
    res = board.set_custom_design_rules([CustomRule(proto=p) for p in merged])
    if res.status == CustomRulesStatus.CRS_VALID:
        # Only an accepted write needs the undo: re-writing the old rules after a
        # rejected one would make KiCad re-serialise an untouched .kicad_dru.
        state["custom"] = pre
    else:
        raise _rules_invalid(
            f"KiCad rejects the custom rules: {res.error_text or 'invalid'}",
            "Fix the condition (KiCad custom rule syntax, e.g. A.NetClass == 'Power').",
        )


async def _undo_custom_rules(sctx: BoardStageContext, state: dict[str, Any]) -> None:
    """The pre-call rules back in KiCad; a `.kicad_dru` this call created is removed
    (`remove_created`: a regular file only, never through a symlink)."""
    if "custom" not in state:
        return
    pre = state["custom"]
    await sctx.call(lambda h: h.board().set_custom_design_rules([CustomRule(proto=p) for p in pre]))
    if state.get("dru_created"):
        project = sctx.ctx.project
        await asyncio.to_thread(remove_created, project.pro_file.parent, [_dru_rel(project)])


_RULE_HEAD = r'\(rule\s+(?:"{0}"|{0})[\s)]'


def _verify_rules(
    req: RulesArgs, values: dict[str, float], before_hash: str
) -> Callable[[BoardModel, dict[str, Any]], list[str]]:
    def verify(m: BoardModel, report: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        if req.layers is not None and len(m.copper_layers) != req.layers:
            problems.append(f"{len(m.copper_layers)} copper layers, expected {req.layers}")
        rules = dict(m.rules)
        for field, want in values.items():
            got = rules.get(field)
            if got is None or abs(got - want) > RULE_TOL_MM:
                problems.append(f"rule {field} reads back as {got}, expected {want:g}")
        classes = {c.name: c for c in m.net_classes}
        for c in req.net_classes:
            have = classes.get(c.name)
            if have is None:
                problems.append(f"net class {c.name!r} missing")
            elif any(
                abs(a - b) > RULE_TOL_MM
                for a, b in (
                    (have.track_width, c.track_width),
                    (have.clearance, c.clearance),
                    (have.via_diameter, c.via_diameter),
                    (have.via_drill, c.via_drill),
                )
            ):
                problems.append(f"net class {c.name!r} reads back with other sizes")
            elif set(have.patterns) != set(c.patterns):
                problems.append(
                    f"net class {c.name!r} reads back with patterns "
                    f"{sorted(have.patterns)}, expected {sorted(c.patterns)}"
                )
        for r in req.custom_rules:
            if not re.search(_RULE_HEAD.format(re.escape(r.name)), m.dru_text):
                problems.append(f"custom rule {r.name!r} missing from the rules file")
        after = board_hash(m)
        report["layers"] = len(m.copper_layers)
        report["board_hash"] = {"before": before_hash, "after": after}
        report["board_hash_changed"] = after != before_hash
        return problems

    return verify


def _rules_report(
    req: RulesArgs, preset: FabPreset | None, values: dict[str, float], copper: int
) -> dict[str, Any]:
    target = req.layers or copper
    notes: list[str] = []
    if req.preset is not None and preset is not None and preset.layers != target:
        notes.append(
            f"preset {preset.id} is for {preset.layers} copper layers; the board keeps "
            f"{target} layers (pass layers: {preset.layers} to change the layer count)"
        )
    by_rule = {v: k for k, v in RULE_FIELDS.items()}
    return {
        "preset": req.preset,
        "layers": target,
        "rules": [
            {
                "rule": field,
                "value": value,
                "source": "override" if by_rule[field] in req.overrides else "preset",
            }
            for field, value in values.items()
        ],
        "net_classes": [
            {
                "name": c.name,
                "track_width": c.track_width,
                "clearance": c.clearance,
                "via_diameter": c.via_diameter,
                "via_drill": c.via_drill,
                "diff_pair_width": c.diff_pair_width,
                "diff_pair_gap": c.diff_pair_gap,
                "patterns": list(c.patterns),
            }
            for c in req.net_classes
        ],
        "custom_rules": [
            {
                "name": r.name,
                "condition": r.condition,
                "constraint": r.constraint,
                "min": r.min,
                "max": r.max,
                "reason": r.reason,
            }
            for r in req.custom_rules
        ],
        "notes": notes,
        "next": "pcb_analyze",
    }


async def prepare_rules(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_set_rules`: everything is checked before the first write (arguments,
    the stackup branch, override floors, a layer drop that would delete items, the
    `.kicad_dru` path). One stage per given part, each with an `undo` that restores the
    pre-call value; compensation runs them in reverse."""
    require_board_tools(ctx)
    req = parse_rules_args(args)
    plan_stackup(req)
    require_board(ctx)
    before = await board_model(ctx)
    copper = len(before.copper_layers)
    preset, values = design_rule_values(req, copper)
    target = req.layers
    if target is not None and target < copper:
        dropped = removed_layers(copper, target)
        items = await ctx.session.call(
            lambda h: _items_on(h, dropped), timeout=BOARD_READ_TIMEOUT_S
        )
        if items:
            raise _layers_refused(target, items)
    if req.custom_rules:
        dru_exists(ctx.project)
    return _plan_rules(req, preset, values, copper, board_hash(before))


def _plan_rules(
    req: RulesArgs,
    preset: FabPreset | None,
    values: dict[str, float],
    copper: int,
    before_hash: str,
) -> BoardChangeSet:
    state: dict[str, Any] = {}

    def staged(
        label: str,
        write: Callable[[KicadHandle], None],
        undo: Callable[[BoardStageContext], Awaitable[None]],
    ) -> BoardStage:
        async def run(sctx: BoardStageContext) -> None:
            sctx.ctx.session.modified_docs.add("pcb")
            await sctx.call(write, timeout=BOARD_READ_TIMEOUT_S)

        # Rules, classes and the layer count are no board items: no commit (the engine's
        # KIID diff has nothing to do for them); `undo` restores the pre-call values.
        return BoardStage(label, run, commit=False, undo=undo)

    def undo_with(
        fn: Callable[[KicadHandle, dict[str, Any]], None],
    ) -> Callable[[BoardStageContext], Awaitable[None]]:
        async def undo(sctx: BoardStageContext) -> None:
            await sctx.call(lambda h: fn(h, state), timeout=BOARD_READ_TIMEOUT_S)

        return undo

    stages: list[BoardStage] = []
    if req.layers is not None and req.layers != copper:
        target = req.layers
        stages.append(
            staged("layer count", lambda h: _set_layers(h, target, state), undo_with(_undo_layers))
        )
    if values:
        stages.append(
            staged(
                "design rules",
                lambda h: _set_design_rules(h, values, state),
                undo_with(_undo_design_rules),
            )
        )
    if req.net_classes:
        stages.append(
            staged(
                "net classes",
                lambda h: _set_net_classes(h, req.net_classes, state),
                undo_with(_undo_net_classes),
            )
        )
    report = _rules_report(req, preset, values, copper)
    if req.custom_rules:

        async def custom(sctx: BoardStageContext) -> None:
            sctx.ctx.session.modified_docs.add("pcb")
            project = sctx.ctx.project
            await sctx.call(
                lambda h: _set_custom_rules(h, project, req.custom_rules, state),
                timeout=BOARD_READ_TIMEOUT_S,
            )
            sctx.report["dru_created"] = bool(state.get("dru_created"))

        async def undo_custom(sctx: BoardStageContext) -> None:
            await _undo_custom_rules(sctx, state)

        stages.append(BoardStage("custom rules", custom, commit=False, undo=undo_custom))
    return BoardChangeSet(
        tool="pcb_set_rules",
        logical=Logical(RULES_COST if stages else 0),
        kind="board_set_rules",
        board_stages=stages,
        verify=_verify_rules(req, values, before_hash),
        report=report,
    )


async def apply_rules(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    assert isinstance(cs, BoardChangeSet)
    return await apply_board_change_set(ctx, cs)
