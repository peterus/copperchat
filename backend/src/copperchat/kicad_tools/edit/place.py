"""Placing library symbols (E4 Task 2; port of spike 2D `sch_place_symbol`).

`prepare` reads the library detail (E3, never inside an open commit), probes the part
in a dropped commit for its real geometry (pins of every unit, body graphics), lays it
out locally at the target (canonical transform, fields), refuses overlaps with the
nearest free position, allocates the reference (E-C8) and checks the footprint. The
change set has two stages (plan-author call: KiCad ignores updates staged with the
placement): (1) `Place` with the canonical orientation, (2) an `Update` of the placed
symbol with mirror flags, value, footprint, fields and field positions (definition
cleared by the engine), plus the other units of the same reference. A part placed on a
sheet used by several instances gets its other instances' references in a third,
separate change set after a save + reload (spike 2A Q7).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal

from copperchat.errors import DocumentInconsistent, StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.access import PinKey
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Logical,
    NetIntent,
    Place,
    SheetRef,
    Stage,
    StageInput,
    Update,
    static_stage,
)
from copperchat.kicad_tools.edit.engine import Applied, apply_change_set, copy_item
from copperchat.kicad_tools.edit.fields import (
    FieldSpot,
    apply_fields,
    apply_flags,
    apply_layout,
    field_layout,
    field_template,
    field_values,
    flag_values,
    laid_out_boxes,
)
from copperchat.kicad_tools.edit.geometry import (
    DEG_SSO,
    NM,
    Box,
    PinEnd,
    Point,
    body_box,
    canonical_transform,
    pin_ends,
)
from copperchat.kicad_tools.edit.scene import Scene, SymbolInfo, sheet_api
from copperchat.kicad_tools.edit.space import (
    BODY_CLEARANCE_MM,
    Candidate,
    find_space,
    named_obstacles,
    pin_corridor,
    shift,
)
from copperchat.tools.spec import ToolContext

PROBE_AT = (25.4, 25.4)
Mirror = Literal["", "x", "y"]
REF_RE = re.compile(r"^[A-Za-z][A-Za-z_]{0,7}[0-9]{1,6}$")


def _v2(at: Sequence[float]) -> Any:
    from kipy.geometry import Vector2

    return Vector2.from_xy(round(at[0] * NM), round(at[1] * NM))


def _fmt(p: Sequence[float]) -> str:
    return f"[{p[0]:g}, {p[1]:g}]"


# -- probe and layout ---------------------------------------------------------------------


def _probe(lib_id: str, unit: int, h: KicadHandle) -> Any:
    sym = h.schematic().place_symbol_from_library(lib_id, _v2(PROBE_AT), unit=unit)
    return copy_item(sym)


async def probe_symbol(ctx: ToolContext, lib_id: str, unit: int) -> Any:
    """The part as KiCad would place it (definition with every unit's pins), from a
    placement in a dropped commit: the document stays unchanged."""
    from kipy.errors import ApiError

    session = ctx.session
    if session.read_only:
        raise StructuredToolError(
            "The project is open read-only",
            "measuring a part needs a (dropped) placement, which a read-only session refuses",
            "Ask the user to open the project for editing.",
            code="read_only",
        )
    await session.ensure_symbol_libraries()
    try:
        async with session.commit(f"probe {lib_id}", dry_run=True):
            return await session.call(partial(_probe, lib_id, unit))
    except ApiError as exc:
        raise StructuredToolError(
            f"KiCad cannot place {lib_id} (unit {unit})",
            str(exc)[:200],
            "Check lib_id and unit with lib_get_symbol.",
            code="invalid_argument",
        ) from exc


@dataclass
class Layout:
    """A part laid out at `at` (a local copy, nothing written)."""

    item: Any
    at: Point
    rotation: int
    mirror: Mirror
    pins: list[PinEnd]
    body: Box  # body + pins, no margin
    spots: tuple[FieldSpot, FieldSpot]
    fields: list[Box]  # Reference/Value as laid out

    @property
    def box(self) -> Box:
        boxes = [self.body, *self.fields]
        return Box(
            min(b.x0 for b in boxes),
            min(b.y0 for b in boxes),
            max(b.x1 for b in boxes),
            max(b.y1 for b in boxes),
        )

    def outline(self) -> list[Box]:
        """Body and fields, relative to the origin."""
        return [shift(b, -self.at[0], -self.at[1]) for b in (self.body, *self.fields)]

    def shape(self) -> list[Box]:
        """The free-space shape relative to the origin: body + clearance, fields, and
        the room in front of the part's own pins (2D v2.1)."""
        boxes = [self.body.grow(BODY_CLEARANCE_MM), *self.fields]
        boxes += [pin_corridor(p) for p in self.pins]
        return [shift(b, -self.at[0], -self.at[1]) for b in boxes]


def set_transform(sym: Any, rotation: int, mirror: Mirror) -> None:
    deg, mx, my = canonical_transform(rotation, mirror)
    sym.transform.orientation = DEG_SSO[deg]
    sym.transform.mirror_x = mx
    sym.transform.mirror_y = my


def lay_out_fields(sym: Any) -> tuple[tuple[FieldSpot, FieldSpot], list[Box], Box, list[PinEnd]]:
    """Reference/Value layout of a placed (or probed) symbol, written onto `sym`."""
    pins = pin_ends(sym)
    body = body_box(sym, margin=0.0)
    deg = {1: 0, 2: 90, 3: 180, 4: 270}.get(int(sym.transform.orientation), 0)
    spots = field_layout(
        body, {p.direction for p in pins}, deg, (str(sym.reference), str(sym.value))
    )
    apply_layout(sym, spots)
    return spots, laid_out_boxes(sym, spots), body, pins


def layout(
    template: Any,
    at: Point,
    rotation: int,
    mirror: Mirror,
    *,
    reference: str | None = None,
    value: str | None = None,
) -> Layout:
    sym = copy_item(template)
    sym.position = _v2(at)
    set_transform(sym, rotation, mirror)
    if reference:
        sym.reference = reference
    if value:
        sym.value = value
    spots, fields, body, pins = lay_out_fields(sym)
    return Layout(sym, at, rotation, mirror, pins, body, spots, fields)


# -- checks -------------------------------------------------------------------------------


def clashes(scene: Scene, lay: Layout, exclude: frozenset[str] = frozenset()) -> list[str]:
    """Why `lay` cannot go where it is: overlaps of its body/fields with the scene, and
    pins landing on an existing connection point (that would connect them)."""
    out: list[str] = []
    mine = [lay.body, *lay.fields]
    seen: set[str] = set()
    for name, box in named_obstacles(scene, exclude):
        if name not in seen and any(b.overlaps(box) for b in mine):
            seen.add(name)
            out.append(f"overlaps {name}")
    for pin in lay.pins:
        items = scene.items_at(pin.at)
        if items:
            kinds = ", ".join(sorted({i["kind"] for i in items}))
            out.append(f"pin {pin.number} at {_fmt(pin.at)} would land on a {kinds}")
    return out


def nearest_free(scene: Scene, lay: Layout, limit: int = 1) -> list[Candidate]:
    return find_space(scene, lay.shape(), lay.at, outline=lay.outline(), limit=limit)


def overlap_error(scene: Scene, lay: Layout, problems: list[str], what: str) -> StructuredToolError:
    spots = nearest_free(scene, lay)
    suggestion = (
        f"Nearest free position: at={_fmt(spots[0].at)} (or ask sch_find_space)."
        if spots
        else "No free position nearby: use sch_find_space with another anchor or sheet."
    )
    shown = problems[:6] + ([f"... {len(problems) - 6} more"] if len(problems) > 6 else [])
    return StructuredToolError(
        f"{what} at {_fmt(lay.at)} would overlap",
        "; ".join(shown),
        suggestion,
        code="overlap",
        details={
            "problems": problems[:20],
            "suggested_at_mm": list(spots[0].at) if spots else None,
            "size_mm": [round(lay.box.width, 2), round(lay.box.height, 2)],
        },
    )


def check_reference(ref: str) -> str:
    ref = ref.strip()
    if not REF_RE.match(ref):
        raise StructuredToolError(
            f"Invalid reference {ref[:20]!r}",
            "a reference is a letter prefix followed by a number (e.g. R5, U12, SW3)",
            "Omit reference to let CopperChat assign the next free one.",
            code="invalid_argument",
        )
    return ref


def ref_prefix(detail_prefix: str, probed_ref: str) -> str:
    prefix = (detail_prefix or re.sub(r"[0-9?]+$", "", probed_ref) or "U").strip()
    return prefix if re.match(r"^[A-Za-z][A-Za-z_]{0,7}$", prefix) else "U"


# -- results ------------------------------------------------------------------------------


def pin_json(p: PinEnd) -> dict[str, Any]:
    return {
        "pin": p.number,
        "name": p.name,
        "type": p.type,
        "at_mm": list(p.at),
        "direction": p.direction,
    }


def placed_json(sym: Any, sheet: SheetRef, lib_id: str) -> dict[str, Any]:
    from copperchat.kicad_tools.edit.geometry import mm_point, symbol_transform

    rotation, mirror = symbol_transform(sym)
    pins = pin_ends(sym)
    body = body_box(sym, margin=0.0)
    return {
        "ref": str(sym.reference),
        "uuid": access.kiid(sym),
        "lib_id": lib_id,
        "unit": int(sym.unit),
        "value": str(sym.value),
        "footprint": access.field_text(sym.footprint_field),
        "at_mm": list(mm_point(sym.position)),
        "rotation": rotation,
        "mirror": mirror,
        "sheet": sheet.human_path,
        "page": sheet.page,
        "box_mm": body.as_dict(),
        "pins": [pin_json(p) for p in pins],
    }


@dataclass
class ToolChangeSet(ChangeSet):
    """A ChangeSet carrying the tool's own plan and structured notes to its `apply`."""

    plan: Any = None
    extra_notes: list[dict[str, Any]] = field(default_factory=list)


# -- the placement change set -------------------------------------------------------------


@dataclass
class PlacePlan:
    lib_id: str
    sheet: SheetRef
    at: Point
    rotation: int
    mirror: Mirror
    unit: int
    reference: str
    fields: dict[str, str]  # KiCad field names (Value, Footprint, MPN, ...) to set
    siblings: list[SymbolInfo]  # other units of the same reference on this sheet instance
    others: dict[str, str]  # other instances of a shared sheet: sheet path -> reference
    other_sheets: list[SheetRef]


def _sibling_state(sib: Any) -> tuple[dict[str, str], dict[str, bool], dict[str, Any]]:
    return field_values(sib), flag_values(sib), field_template(sib)


def build_change_set(plan: PlacePlan, tool: str, summary: dict[str, Any]) -> ToolChangeSet:
    """Stages: (1) Place, (2) transform/fields/layout of the placed unit + sibling units."""
    intent = NetIntent()
    sib_items = [copy_item(s.item) for s in plan.siblings]
    inherited: dict[str, str] = {}
    flags: dict[str, bool] = {}
    template: dict[str, Any] = {}
    if sib_items:
        inherited, flags, template = _sibling_state(sib_items[0])
    wanted = {**inherited, **plan.fields}

    def finish(inp: StageInput) -> list[Any]:
        sym = copy_item(inp.placed[0])
        _, mx, my = canonical_transform(plan.rotation, plan.mirror)
        sym.transform.mirror_x = mx
        sym.transform.mirror_y = my
        apply_fields(sym, wanted, template=template)
        if flags:
            apply_flags(sym, flags)
        lay_out_fields(sym)
        uid = access.kiid(sym)
        keys: set[PinKey] = {p.key for p in pin_ends(sym, unit_only=False)}
        keys |= {(plan.sheet.path, uid, k[2]) for k in keys}
        intent.may_change = frozenset(keys)  # the new pins only (E4 Task 2)
        ops: list[Any] = [Update([sym], sheet=plan.sheet)]
        changed = [s for s in sib_items if _needs(s, plan.fields)]
        for s in changed:
            apply_fields(s, plan.fields)
        if changed:
            ops.append(Update(changed, sheet=plan.sheet))
        return ops

    stages = [
        static_stage(
            "place",
            Place(
                plan.lib_id,
                plan.at,
                canonical_transform(plan.rotation, plan.mirror)[0],
                plan.unit,
                plan.reference,
                sheet=plan.sheet,
            ),
        ),
        Stage("fields", finish),
    ]
    return ToolChangeSet(
        tool=tool,
        sheet=plan.sheet,
        stages=stages,
        pre_images={s.uuid: copy_item(s.item) for s in plan.siblings},
        intent=intent,
        logical=Logical(1),
        summary=summary,
        plan=plan,
    )


def _needs(sym: Any, fields: dict[str, str]) -> bool:
    current = field_values(sym)
    return any(current.get(k) != v for k, v in fields.items())


# -- shared sheets ------------------------------------------------------------------------


async def fix_shared_instances(
    ctx: ToolContext, plan: PlacePlan, uid: str, tool: str
) -> tuple[Applied, list[dict[str, str]]]:
    """Give the new symbol its own reference on the other instances of a shared sheet:
    save + reload (it is invisible there before), then set each instance's reference via
    `for_sheet(other)` (spike 2A Q7). On failure the placement is removed again."""
    session = ctx.session
    await session.refresh_jobs()  # save + close/reopen: the other instances now show it

    def read(h: KicadHandle) -> list[tuple[SheetRef, Any]]:
        out = []
        for other in plan.other_sheets:
            items = sheet_api(h, other).get_items_by_id([_kiid(uid)])
            if not items:
                raise StructuredToolError(
                    f"{tool}: the new symbol is missing on {other.human_path}",
                    "after the reload the shared sheet instance does not show it",
                    "Re-read the sheet; the placement was undone.",
                    code="verification_failed",
                )
            sym = copy_item(items[0])
            sym.reference = plan.others[other.path]
            out.append((other, sym))
        return out

    try:
        items = await session.call(read)
        keys = frozenset(
            (other.path, uid, p.key[2]) for other, sym in items for p in pin_ends(sym, False)
        )
        cs = ChangeSet(
            tool=tool,
            sheet=plan.sheet,
            stages=[
                static_stage(
                    "references of the other sheet instances",
                    *(Update([sym], sheet=other) for other, sym in items),
                )
            ],
            pre_images={},
            intent=NetIntent(may_change=keys),
            logical=Logical(0),
        )
        applied = await apply_change_set(ctx, cs)
    except BaseException:
        await _remove_placed(ctx, plan.sheet, uid, tool)
        raise
    renamed = [{"sheet": other.human_path, "ref": str(sym.reference)} for other, sym in items]
    return applied, renamed


def _kiid(value: str) -> Any:
    from kipy.proto.common.types import KIID

    k = KIID()
    k.value = value
    return k


async def _remove_placed(ctx: ToolContext, sheet: SheetRef, uid: str, tool: str) -> None:
    session = ctx.session

    def remove(h: KicadHandle) -> None:
        sheet_api(h, sheet).remove_items_by_id([_kiid(uid)])

    try:
        async with session.commit(f"{tool}: undo placement"):
            await session.call(remove)
    except Exception as exc:
        raise DocumentInconsistent(f"undoing the partial {tool} placement failed") from exc
