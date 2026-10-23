"""Tool provider (E4): `sch_find_space`, `sch_place_symbol`, `sch_set_fields`,
`sch_annotate`."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from functools import partial
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.annotate import Allocator, Block
from copperchat.kicad_tools.edit.annotate_tool import plan_annotation
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Logical,
    NetIntent,
    SheetRef,
    Update,
    static_stage,
)
from copperchat.kicad_tools.edit.engine import (
    apply_change_set,
    copy_item,
    edit_result,
    record_applied,
)
from copperchat.kicad_tools.edit.fields import (
    FLAGS,
    STANDARD,
    apply_fields,
    apply_flags,
    check_text,
    field_values,
    flag_values,
    normalize_fields,
)
from copperchat.kicad_tools.edit.geometry import Box, Point, same_point, snap_point
from copperchat.kicad_tools.edit.place import (
    Mirror,
    PlacePlan,
    ToolChangeSet,
    build_change_set,
    check_reference,
    clashes,
    fix_shared_instances,
    layout,
    overlap_error,
    placed_json,
    probe_symbol,
    ref_prefix,
)
from copperchat.kicad_tools.edit.scene import (
    Scene,
    SymbolInfo,
    all_symbols,
    read_scene,
    resolve_pin,
    resolve_sheet,
    sheet_refs,
    short,
    symbol_info,
)
from copperchat.kicad_tools.edit.space import find_space, pin_corridor
from copperchat.kicad_tools.edit.specs_core import apply_change
from copperchat.kicad_tools.filtering import natural_key
from copperchat.library.prefs import (
    active_part_note,
    effective_preferences,
    is_passive,
    preference_notes,
)
from copperchat.library.service import LibraryService, library_for
from copperchat.library.specs import READY_WAIT_S
from copperchat.library.symbols import SymbolDetail
from copperchat.tools.spec import ToolContext, ToolResult, ToolSpec, edit_spec

MAX_LIB_ID = 256
ROTATIONS = (0, 90, 180, 270)


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


def _bad(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


# -- argument parsing ---------------------------------------------------------------------


def _lib_id(args: dict[str, Any]) -> str:
    lib_id = args.get("lib_id")
    if (
        not isinstance(lib_id, str)
        or not 3 <= len(lib_id.strip()) <= MAX_LIB_ID
        or ":" not in lib_id
    ):
        raise _bad(
            "Invalid lib_id",
            f"lib_id was {short(lib_id)}",
            "Pass <library>:<symbol> as found by lib_search_symbols, e.g. 'Device:R'.",
        )
    return lib_id.strip()


def _number(c: Any) -> bool:
    if not isinstance(c, (int, float)) or isinstance(c, bool):
        return False
    try:
        return abs(c) < 10_000 and math.isfinite(c)
    except OverflowError:
        return False


def _point(raw: Any, name: str) -> Point:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2 or not all(_number(c) for c in raw):
        raise _bad(f"Invalid {name}", f"{name} was {short(raw)}", f"Pass {name}=[x_mm, y_mm].")
    return (float(raw[0]), float(raw[1]))


def _rotation(args: dict[str, Any]) -> int:
    rot = args.get("rotation", 0)
    if rot is None:
        return 0
    if not isinstance(rot, int) or isinstance(rot, bool) or rot % 360 not in ROTATIONS:
        raise _bad("Invalid rotation", f"rotation was {short(rot)}", "Use 0, 90, 180 or 270.")
    return rot % 360


def _mirror(args: dict[str, Any]) -> Mirror:
    m = args.get("mirror") or ""
    if m in ("none",):
        return ""
    if m not in ("", "x", "y"):
        raise _bad(
            "Invalid mirror",
            f"mirror was {short(m)}",
            "Use 'x' (mirror about the X axis, upside down) or 'y' (left-right).",
        )
    return "x" if m == "x" else "y" if m == "y" else ""


def _unit(args: dict[str, Any]) -> int:
    unit = args.get("unit", 1)
    if unit is None:
        return 1
    if not isinstance(unit, int) or isinstance(unit, bool) or not 1 <= unit <= 64:
        raise _bad("Invalid unit", f"unit was {short(unit)}", "Units are numbered from 1.")
    return unit


def _sheet_arg(args: dict[str, Any]) -> str | None:
    sheet = args.get("sheet")
    if sheet is None or sheet == "":
        return None
    if not isinstance(sheet, str) or len(sheet) > 256:
        raise _bad("Invalid sheet", f"sheet was {short(sheet)}", "Pass a path like '/amp/'.")
    return sheet


def _text(args: dict[str, Any], key: str, limit: int = 256) -> str | None:
    v = args.get(key)
    if v is None:
        return None
    return check_text(v, key, limit)


# -- shared helpers -----------------------------------------------------------------------


async def _library(ctx: ToolContext) -> LibraryService:
    svc = library_for(ctx.session)
    await svc.wait_ready(READY_WAIT_S)
    return svc


async def _annotation_block(ctx: ToolContext) -> Block:
    if ctx.editing is None:
        return 100
    block = (await ctx.editing.settings_repo.get(ctx.project.id)).annotation_block
    return 1000 if block == 1000 else 100


async def footprint_notes(
    ctx: ToolContext, svc: LibraryService, lib_id: str, footprint: str, *, explicit: bool
) -> list[dict[str, Any]]:
    """Existence (with a board) and part-preference checks of a footprint name. An
    explicit unknown footprint is refused (closest matches); without a board the
    footprint is `unverified` (owner decision 3) and only the name is checked."""
    notes: list[dict[str, Any]] = []
    prefs = effective_preferences(ctx.settings, ctx.project)
    if not footprint:
        if is_passive(lib_id) and prefs.passives_package:
            notes.append(
                {
                    "kind": "no_footprint",
                    "lib_id": lib_id,
                    "message": f"{lib_id} has no footprint; the part preferences ask "
                    f"for {prefs.passives_package} passives.",
                }
            )
        return notes
    nickname, _, name = footprint.partition(":")
    if not nickname or not name:
        raise _bad(
            f"Invalid footprint {footprint[:80]!r}",
            "expected <library>:<footprint>",
            "e.g. Resistor_SMD:R_0805_2012Metric",
        )
    detail = None
    if svc.footprints_available():
        detail = await svc.footprint(footprint)
        if detail is None:
            close = await _closest_footprints(svc, nickname, name)
            if explicit:
                raise StructuredToolError(
                    f"Footprint {footprint!r} is not in the libraries",
                    "unknown footprint (format <library nickname>:<footprint name>)",
                    f"Closest matches: {close}" if close else "Use lib_search_footprints.",
                    code="not_found",
                    details={"closest": close},
                )
            notes.append(
                {
                    "kind": "footprint_not_found",
                    "footprint": footprint,
                    "closest": close,
                    "message": f"The library default footprint {footprint} was not "
                    "found; set one with sch_set_fields.",
                }
            )
    else:
        notes.append(
            {
                "kind": "footprint_unverified",
                "footprint": footprint,
                "message": f"{footprint} is unverified: this project has no board, so "
                "footprint libraries are not checked. Tell the user.",
            }
        )
    notes += preference_notes(prefs, lib_id, footprint, detail)
    return notes


async def _closest_footprints(svc: LibraryService, nickname: str, name: str) -> list[str]:
    query = " ".join(name.replace("_", " ").split()[:6])[:200] or name[:200]
    try:
        libs = await svc.footprint_libraries()
        rows = await svc.search_footprints(query, nickname if nickname in libs else None, 5)
    except StructuredToolError:
        return []
    return [str(r.get("lib_id")) for r in rows]


def _structured(cs: ChangeSet) -> list[dict[str, Any]]:
    return list(cs.extra_notes) if isinstance(cs, ToolChangeSet) else []


# -- design reads -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Unit:
    """A placed unit, without geometry (references and pages for the allocator)."""

    ref: str
    page: str
    uuid: str
    lib_id: str
    unit: int
    sheet: SheetRef


@dataclass
class _Design:
    """Everything a placement plan reads from KiCad; the geometry runs on it afterwards,
    outside the session lock (a slow search must not look like a KiCad hang)."""

    sheet: SheetRef
    scene: Scene
    refs: list[SheetRef]
    units: list[_Unit]
    siblings: list[SymbolInfo]  # units with the requested reference (with geometry)

    def allocator(self, block: Block) -> Allocator:
        return Allocator([(u.ref, u.page) for u in self.units], [r.page for r in self.refs], block)


def _read_design(h: KicadHandle, sheet: SheetRef, reference: str | None = None) -> _Design:
    refs = sheet_refs(h)
    by_path = {r.path: r for r in refs}
    units: list[_Unit] = []
    siblings: list[SymbolInfo] = []
    for sym in h.schematic().get_symbols():
        where = by_path.get(access.sheet_path_key(sym.path))
        if where is None:
            continue
        ref = str(sym.reference)
        units.append(
            _Unit(ref, where.page, access.kiid(sym), str(sym.definition.id), int(sym.unit), where)
        )
        if reference and ref == reference:
            siblings.append(symbol_info(sym, where))
    return _Design(sheet, read_scene(h, sheet), refs, units, siblings)


# -- sch_find_space -----------------------------------------------------------------------


@dataclass
class _Anchor:
    sheet: Any
    point: Point
    direction: Any = None
    keep_free: tuple[Box, ...] = ()


def _anchor(h: KicadHandle, near: Any, sheet_arg: str | None) -> _Anchor:
    if isinstance(near, str) and near.strip():
        spec = near.strip()[:64]
        if "." in spec:
            pin = resolve_pin(h, spec)
            owner = next(
                s
                for s in all_symbols(h, ref=pin.ref)
                if s.uuid == pin.key[1] and s.sheet.path == pin.key[0]
            )
            return _Anchor(owner.sheet, pin.at, pin.direction, (pin_corridor(pin),))
        units = all_symbols(h, ref=spec)
        if not units:
            resolve_pin(h, spec + ".1")  # raises not_found with the closest references
        if sheet_arg:
            sheet = resolve_sheet(h, sheet_arg)
            units = [u for u in units if u.sheet.path == sheet.path] or units
        b = units[0].body
        return _Anchor(units[0].sheet, ((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2))
    point = _point(near, "near")
    return _Anchor(resolve_sheet(h, sheet_arg), point)


async def sch_find_space(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    lib_id = _lib_id(args)
    rotation, mirror, unit = _rotation(args), _mirror(args), _unit(args)
    value = _text(args, "value")
    near = args.get("near")
    if near is None:
        raise _bad(
            "sch_find_space needs near",
            "no anchor was given",
            "Pass near='U1.3' (a pin), 'U1' (a symbol) or [x, y] in mm.",
        )
    sheet_arg = _sheet_arg(args)
    svc = await _library(ctx)
    detail = await svc.get_symbol(lib_id)
    _check_unit(detail, unit)
    template = await probe_symbol(ctx, lib_id, unit)
    prefix = ref_prefix(detail.reference_prefix, str(template.reference))
    block = await _annotation_block(ctx)

    def read(h: KicadHandle) -> tuple[_Anchor, _Design]:
        anchor = _anchor(h, near, sheet_arg)
        return anchor, _read_design(h, anchor.sheet)

    anchor, design = await ctx.session.call(read)

    def search() -> dict[str, Any]:
        # Lay out with the reference and value a placement would get: same field boxes.
        ref = design.allocator(block).next_ref(prefix, anchor.sheet.page)
        lay = layout(template, (0.0, 0.0), rotation, mirror, reference=ref, value=value)
        found = find_space(
            design.scene,
            lay.shape(),
            anchor.point,
            direction=anchor.direction,
            keep_free=anchor.keep_free,
            outline=lay.outline(),
        )
        if not found:
            raise StructuredToolError(
                f"No free space for {lib_id} near {short(near, 40)}",
                "every position within ~15 cm is blocked",
                "Try another anchor, another sheet, or make room first.",
                code="overlap",
            )
        return {
            "lib_id": lib_id,
            "sheet": anchor.sheet.human_path,
            "rotation": rotation,
            "mirror": mirror,
            "size_mm": [round(lay.box.width, 2), round(lay.box.height, 2)],
            "candidates": [
                {"at_mm": list(c.at), "box_mm": c.box.as_dict(), "distance_mm": c.distance}
                for c in found
            ],
        }

    return ToolResult(data=await asyncio.to_thread(search))


def _check_unit(detail: SymbolDetail, unit: int) -> None:
    count = max(detail.unit_count, 1)
    if unit > count:
        raise _bad(
            f"{detail.lib_id} has no unit {unit}",
            f"it has {count} unit(s)",
            f"Use a unit from 1 to {count}.",
        )


# -- sch_place_symbol ---------------------------------------------------------------------


def _ref_in_use(ref: str, users: list[_Unit], suggestion: str) -> StructuredToolError:
    where = ", ".join(sorted({f"{u.lib_id} unit {u.unit} on {u.sheet.human_path}" for u in users}))
    return _bad(
        f"Reference {ref} is already used",
        f"{ref} is {where}",
        f"Omit reference to get the next free one ({suggestion}), or place another "
        "unit of the same multi-unit part with its reference.",
    )


def _plan_place(
    d: _Design,
    lib_id: str,
    detail: SymbolDetail,
    template: Any,
    at: Point,
    rotation: int,
    mirror: Mirror,
    unit: int,
    reference: str | None,
    fields: dict[str, str],
    block: Block,
) -> tuple[PlacePlan, dict[str, str]]:
    """Reference, overlap check and plan; pure computation on the read design."""
    sheet = d.sheet
    alloc = d.allocator(block)
    prefix = ref_prefix(detail.reference_prefix, str(template.reference))
    siblings: list[SymbolInfo] = []
    if reference:
        ref = check_reference(reference)
        users = [u for u in d.units if u.ref == ref]
        if not users:
            raise _bad(
                f"Reference {ref} is not in use",
                "reference is only for another unit of an existing multi-unit part; a new "
                "part gets the next free reference of its sheet",
                f"Omit reference (the next free one is {alloc.next_ref(prefix, sheet.page)}).",
            )
        same = [s for s in d.siblings if s.lib_id == lib_id and s.sheet.path == sheet.path]
        if len(same) != len(users) or detail.unit_count < 2 or any(s.unit == unit for s in same):
            raise _ref_in_use(ref, users, alloc.next_ref(prefix, sheet.page))
        siblings = same
    else:
        ref = alloc.next_ref(prefix, sheet.page)
    other_sheets = [r for r in d.refs if r.file == sheet.file and r.path != sheet.path]
    others: dict[str, str] = {}
    for o in other_sheets:
        sib = next(
            (
                u
                for u in d.units
                if siblings and u.uuid == siblings[0].uuid and u.sheet.path == o.path
            ),
            None,
        )
        others[o.path] = sib.ref if sib is not None else alloc.next_ref(prefix, o.page)
    inherited = field_values(siblings[0].item) if siblings else {}
    value = fields.get("Value") or inherited.get("Value")
    lay = layout(template, at, rotation, mirror, reference=ref, value=value)
    problems = clashes(d.scene, lay)
    if problems:
        raise overlap_error(d.scene, lay, problems, f"{lib_id}")
    plan = PlacePlan(
        lib_id, sheet, at, rotation, mirror, unit, ref, fields, siblings, others, other_sheets
    )
    return plan, {**inherited, **fields}


async def prepare_place(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    lib_id = _lib_id(args)
    raw_at = _point(args.get("at"), "at")
    rotation, mirror, unit = _rotation(args), _mirror(args), _unit(args)
    sheet_arg = _sheet_arg(args)
    reference = _text(args, "reference", 16)
    if reference is not None:
        reference = check_reference(reference)
    fields = normalize_fields(args.get("fields"))
    for key, name in (("value", "Value"), ("footprint", "Footprint")):
        text = _text(args, key)
        if text is not None:
            fields[name] = text
    svc = await _library(ctx)
    detail = await svc.get_symbol(lib_id)
    if detail.is_power:
        net = lib_id.partition(":")[2]
        raise _bad(
            f"{lib_id} is a power symbol",
            "power symbols are attached to pins, not placed loose",
            f"Use sch_add_power(net='{net}', pins=['REF.PIN', ...]).",
        )
    _check_unit(detail, unit)

    def read(h: KicadHandle) -> _Design:
        return _read_design(h, resolve_sheet(h, sheet_arg), reference)

    design = await ctx.session.call(read)
    sibling = next(
        (s for s in design.siblings if s.lib_id == lib_id and s.sheet.path == design.sheet.path),
        None,
    )
    # The footprint the part really gets: given, else the sibling unit's, else the library's.
    if "Footprint" in fields:
        footprint = fields["Footprint"]
    elif sibling is not None:
        footprint = field_values(sibling.item)["Footprint"]
    else:
        footprint = detail.footprint
    extra = await footprint_notes(ctx, svc, lib_id, footprint, explicit="Footprint" in fields)
    template = await probe_symbol(ctx, lib_id, unit)
    block = await _annotation_block(ctx)
    at = snap_point(raw_at)
    notes: list[str] = []
    if not same_point(at, raw_at, 1e-4):
        notes.append(f"position snapped to the 1.27 mm grid: {list(raw_at)} -> {list(at)}")
    plan, final_fields = await asyncio.to_thread(
        _plan_place,
        design,
        lib_id,
        detail,
        template,
        at,
        rotation,
        mirror,
        unit,
        reference,
        fields,
        block,
    )
    active = active_part_note(ctx.project, plan.reference, final_fields)
    if active is not None:
        extra.append(active)
    cs = build_change_set(plan, "sch_place_symbol", {"sheet": plan.sheet.human_path})
    cs.notes = notes
    cs.extra_notes = extra
    return cs


async def apply_place(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    assert isinstance(cs, ToolChangeSet) and isinstance(cs.plan, PlacePlan)
    plan = cs.plan
    # On a shared sheet the placement's rows are written only once the other instances
    # have their references: a failure there removes the symbol again (no 'add' row).
    applied = await apply_change_set(ctx, cs, record=not plan.other_sheets)
    uid = applied.placed[0]
    sym = next(i for i in applied.created if access.kiid(i) == uid)
    placed = placed_json(sym, plan.sheet, plan.lib_id)
    extra: dict[str, Any] = {}
    if plan.other_sheets:
        # The other instances' references are reported in placed.instances only (their
        # rows would carry this sheet: the engine maps one UUID to one sheet).
        shared, renamed = await fix_shared_instances(ctx, plan, uid, cs.tool)
        await record_applied(ctx, cs.tool, [*applied.changes, *shared.changes])
        placed["instances"] = [{"sheet": plan.sheet.human_path, "ref": plan.reference}, *renamed]
        extra["shared_sheet"] = (
            f"{plan.sheet.file} is used by {len(plan.other_sheets) + 1} sheet instances: the "
            "part appears in each, with its own reference (the document was saved and reloaded)"
        )
    notes = [*cs.notes, *applied.notes, *_structured(cs)]
    return edit_result(cs, applied, placed=placed, notes=notes, **extra)


# -- sch_set_fields -----------------------------------------------------------------------


def _flags(args: dict[str, Any]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for name in FLAGS:
        if name in args and args[name] is not None:
            if not isinstance(args[name], bool):
                raise _bad(f"Invalid {name}", f"{name} must be true or false", f"Pass {name}=true.")
            out[name] = args[name]
    return out


def _remove(args: dict[str, Any]) -> list[str]:
    raw = args.get("remove_fields") or []
    if (
        not isinstance(raw, list)
        or len(raw) > 32
        or not all(isinstance(n, str) and 0 < len(n) <= 64 for n in raw)
    ):
        raise _bad(
            "Invalid remove_fields",
            "remove_fields is a list of field names",
            "Pass remove_fields=['Tolerance'].",
        )
    out = []
    for n in raw:
        if n.strip().lower() in STANDARD or n.strip().lower() in ("reference", "ref"):
            raise _bad(
                f"Field {n} cannot be removed",
                "Reference, Value, Footprint, Datasheet and Description always exist",
                "Set it to an empty string instead.",
            )
        out.append(n.strip())
    return out


def _units_of(h: KicadHandle, ref: str) -> tuple[list[SymbolInfo], list[str]]:
    """The units of `ref`, and the other instances of their sheets when a sheet is used
    more than once (a shared sheet's symbols carry the same fields in every instance)."""
    units = all_symbols(h, ref=ref)
    if not units:
        resolve_pin(h, ref + ".1")  # raises not_found with the closest references
    refs = sheet_refs(h)
    mine = {u.sheet.path for u in units}
    files = {u.sheet.file for u in units}
    others = [r.human_path for r in refs if r.file in files and r.path not in mine]
    return sorted(units, key=lambda u: (natural_key(u.sheet.page), u.unit)), others


async def prepare_set_fields(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    ref = args.get("ref")
    if not isinstance(ref, str) or not 1 <= len(ref.strip()) <= 32:
        raise _bad("Invalid ref", f"ref was {short(ref)}", "Pass a reference like 'R5'.")
    ref = ref.strip()
    fields = normalize_fields(args.get("fields"))
    flags = _flags(args)
    remove = _remove(args)
    if not fields and not flags and not remove:
        raise _bad(
            "sch_set_fields has nothing to change",
            "no fields, flags or removals given",
            "Pass fields={...}, dnp=true, ... or remove_fields=[...].",
        )
    if ref.startswith("#") and "Value" in fields:
        raise _bad(
            f"The value of {ref} names a net",
            "changing a power symbol's value renames its net",
            "Use sch_rename_net to rename the net.",
        )
    units, shared = await ctx.session.call(partial(_units_of, ref=ref))
    lib_id = units[0].lib_id
    extra: list[dict[str, Any]] = []
    if "Footprint" in fields:
        svc = await _library(ctx)
        extra += await footprint_notes(ctx, svc, lib_id, fields["Footprint"], explicit=True)
    items: dict[str, tuple[Any, list[Any]]] = {}
    changed: dict[str, dict[str, Any]] = {}
    for u in units:  # units may be out of sync (2A Q2): compare each one
        before = {**field_values(u.item), **flag_values(u.item)}
        sym = copy_item(u.item)
        apply_fields(sym, fields, remove)
        if flags:
            apply_flags(sym, flags)
        after = {**field_values(sym), **flag_values(sym)}
        for k in sorted(set(before) | set(after)):
            if before.get(k) != after.get(k):
                changed.setdefault(k, {"before": before.get(k), "after": after.get(k)})
        items.setdefault(u.sheet.path, (u.sheet, []))[1].append(sym)
    sample = next(iter(items.values()))[1][0]
    if not changed:
        raise _bad(
            f"sch_set_fields changes nothing on {ref}",
            "every field already has that value",
            "Read the symbol with sch_get_symbol and pass the fields that differ.",
        )
    if not ref.startswith("#"):
        note = active_part_note(ctx.project, ref, field_values(sample))
        if note is not None and any(k in ("MPN", "Manufacturer Part Number") for k in fields):
            extra.append(note)
    notes: list[str] = []
    if shared:
        notes.append(
            f"{ref} is on a sheet used by several instances: the same part on "
            f"{', '.join(shared[:8])} (under its own reference there) gets these fields too"
        )
    return ToolChangeSet(
        tool="sch_set_fields",
        sheet=units[0].sheet,
        stages=[static_stage("set fields", *(Update(syms, sheet=s) for s, syms in items.values()))],
        pre_images={u.uuid: copy_item(u.item) for u in units},
        intent=NetIntent(keep_partition=True),
        logical=Logical(1),
        summary={"ref": ref, "units": len(units), "changed": changed},
        notes=notes,
        extra_notes=extra,
    )


async def apply_noted(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    applied = await apply_change_set(ctx, cs)
    return edit_result(cs, applied, notes=[*cs.notes, *applied.notes, *_structured(cs)])


# -- sch_annotate -------------------------------------------------------------------------


async def prepare_annotate(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    scope = args.get("scope", "new")
    mode = args.get("mode", "keep")
    if scope not in ("new", "sheet", "all"):
        raise _bad("Invalid scope", f"scope was {short(scope)}", "Use 'new', 'sheet' or 'all'.")
    if mode not in ("keep", "renumber"):
        raise _bad("Invalid mode", f"mode was {short(mode)}", "Use 'keep' or 'renumber'.")
    sheet_arg = _sheet_arg(args)
    placed: set[str] = set()
    if ctx.editing is not None:
        placed = set(ctx.editing.turn(ctx.project.id, ctx.run_id).placed_this_turn)
    block = await _annotation_block(ctx)
    return await ctx.session.call(
        partial(
            plan_annotation, scope=scope, mode=mode, sheet_arg=sheet_arg, placed=placed, block=block
        )
    )


# -- specs --------------------------------------------------------------------------------

_SHEET = {
    "sheet": {
        "type": "string",
        "maxLength": 256,
        "description": "Sheet instance path like '/amp/' or page number; default root.",
    }
}
_ROT = {"rotation": {"type": "integer", "enum": list(ROTATIONS), "description": "Degrees CCW."}}
_MIRROR = {
    "mirror": {
        "type": "string",
        "enum": ["", "x", "y"],
        "description": "'x' = upside down (about the X axis), 'y' = left-right.",
    }
}
_UNIT = {"unit": {"type": "integer", "minimum": 1, "description": "Unit of a multi-unit part."}}
_FIELDS = {
    "type": "object",
    "maxProperties": 32,
    "additionalProperties": {"type": "string", "maxLength": 1024},
}
_POINT = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "sch_find_space",
        "Free positions for a library part (at a rotation/mirror) near an anchor: a pin "
        "'REF.PIN' (the part goes on the side the pin points to, the room in front of the "
        "pin stays free), a symbol 'REF', or [x, y] mm. Returns up to 3 candidates nearest "
        "first: at_mm (pass to sch_place_symbol), box_mm (body + fields), distance_mm; "
        "plus size_mm. Free = no overlap with bodies, fields, labels, wires or the room "
        "in front of unconnected pins.",
        _obj(
            {
                "lib_id": {"type": "string", "maxLength": MAX_LIB_ID},
                "near": {
                    "anyOf": [{"type": "string", "maxLength": 64}, _POINT],
                    "description": "'U1.3', 'U1' or [x, y] in mm.",
                },
                "value": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "The value it will get (sizes the Value field).",
                },
                **_ROT,
                **_MIRROR,
                **_UNIT,
                **_SHEET,
            },
            ["lib_id", "near"],
        ),
        sch_find_space,
    ),
    edit_spec(
        "sch_place_symbol",
        "Place a library symbol at [x, y] mm (snapped to the grid) with rotation/mirror, "
        "unit, value, footprint and custom fields; the reference is assigned per sheet "
        "(pass reference only for another unit of an existing multi-unit part). Refused "
        "with the nearest free position when it would overlap anything (use "
        "sch_find_space first). Fields are placed readable. Returns placed: ref, uuid, "
        "box_mm and every pin end (at_mm, direction) for wiring, and notes (part "
        "preference deviations, footprint unverified, active part without notes) to "
        "relay to the user. Power symbols: use sch_add_power. One logical change.",
        _obj(
            {
                "lib_id": {"type": "string", "maxLength": MAX_LIB_ID},
                "at": {**_POINT, "description": "[x, y] in mm."},
                **_ROT,
                **_MIRROR,
                **_UNIT,
                "reference": {"type": "string", "maxLength": 16},
                "value": {"type": "string", "maxLength": 256},
                "footprint": {"type": "string", "maxLength": 256},
                "fields": {**_FIELDS, "description": "Custom fields, e.g. {'MPN': '...'}."},
                **_SHEET,
            },
            ["lib_id", "at"],
        ),
        prepare_place,
        apply_place,
    ),
    edit_spec(
        "sch_set_fields",
        "Edit fields of one reference (all its units). Pass the fields as a map, "
        "fields={\"Value\": \"100k\", \"Footprint\": ..., \"Datasheet\": ..., \"MPN\": ..., "
        "\"Manufacturer\": ...} - standard or custom field names as keys. Also dnp, "
        "exclude_from_bom, exclude_from_board; remove_fields removes custom fields. New "
        "custom fields are hidden at the symbol; existing fields keep their position. "
        "Connectivity is verified unchanged. One logical change.",
        _obj(
            {
                "ref": {"type": "string", "maxLength": 32},
                "fields": _FIELDS,
                "dnp": {"type": "boolean"},
                "exclude_from_bom": {"type": "boolean"},
                "exclude_from_board": {"type": "boolean"},
                "remove_fields": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 64},
                    "maxItems": 32,
                },
            },
            ["ref"],
        ),
        prepare_set_fields,
        apply_noted,
    ),
    edit_spec(
        "sch_annotate",
        "Assign references. mode='keep' (default) numbers only unannotated (R?) and "
        "duplicate references; mode='renumber' renumbers everything in scope by sheet "
        "page, then position (top-left first), per-sheet blocks (R1xx on page 1, R2xx on "
        "page 2, ...) unless the design numbers flat. scope: 'new' (unannotated and parts "
        "placed this turn), 'sheet' (needs sheet), 'all'. Multi-unit parts keep one "
        "reference (units sharing a reference, lib_id and sheet; unannotated units are "
        "numbered one by one, so give the units of a new multi-unit part its reference "
        "when placing them). One logical change per renamed reference.",
        _obj(
            {
                "scope": {"type": "string", "enum": ["new", "sheet", "all"]},
                "mode": {"type": "string", "enum": ["keep", "renumber"]},
                **_SHEET,
            }
        ),
        prepare_annotate,
        apply_change,
    ),
]
