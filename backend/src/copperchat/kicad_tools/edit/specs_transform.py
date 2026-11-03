"""Move / delete / duplicate tools (E6): `sch_move`, `sch_delete`, `sch_duplicate`, and
`sch_shift_sheet` (a whole sheet) — all two-phase (E-C4)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.edit.changeset import ChangeSet
from copperchat.kicad_tools.edit.delete import DeletePlan, nets_changed, plan_delete
from copperchat.kicad_tools.edit.duplicate import DUPLICATE_SPECS
from copperchat.kicad_tools.edit.engine import _pin_labeler, apply_change_set, edit_result
from copperchat.kicad_tools.edit.geometry import Point, same_point, snap_point
from copperchat.kicad_tools.edit.labels import World, no_change
from copperchat.kicad_tools.edit.scene import resolve_sheet, sheet_api, sheet_refs, short
from copperchat.kicad_tools.edit.shift import TOOL as SHIFT_TOOL
from copperchat.kicad_tools.edit.shift import (
    inside,
    item_boxes,
    overlaps,
    plan_centring,
    plan_shift,
    read_page,
    sheet_item_types,
    shift_change_set,
    shifted,
    snap_offset,
    union,
)
from copperchat.kicad_tools.edit.specs_core import _coord, apply_change
from copperchat.kicad_tools.edit.targets import bad, resolve
from copperchat.kicad_tools.edit.transform import MOVE_KINDS, plan_move
from copperchat.tools.spec import ToolContext, ToolResult, ToolSpec, edit_spec


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


def _placed_this_turn(ctx: ToolContext) -> set[str]:
    if ctx.editing is None:
        return set()
    return set(ctx.editing.turn(ctx.project.id, ctx.run_id).placed_this_turn)


# -- sch_move -----------------------------------------------------------------------------


def _xy(args: dict[str, Any], key: str, notes: list[str]) -> Point | None:
    raw = args.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 2 or not all(_coord(c) for c in raw):
        raise bad(
            f"sch_move: invalid {key}",
            f"{key} was {short(raw)}",
            f"Pass {key}=[x, y] in mm.",
        )
    point = (float(raw[0]), float(raw[1]))
    snapped = snap_point(point)
    if not same_point(point, snapped, 1e-4):
        notes.append(f"{key} snapped to the 1.27 mm grid: {list(point)} -> {list(snapped)}")
    return snapped


async def prepare_move(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    tool = "sch_move"
    notes: list[str] = []
    to, by = _xy(args, "to", notes), _xy(args, "by", notes)
    rotation, mirror = args.get("rotation"), args.get("mirror")
    if rotation is not None and (type(rotation) is not int or rotation not in (0, 90, 180, 270)):
        raise bad(f"{tool}: invalid rotation", f"rotation was {short(rotation)}",
                  "Use 0, 90, 180 or 270 (degrees counter-clockwise).")  # fmt: skip
    if mirror is not None and mirror not in ("", "x", "y"):
        raise bad(f"{tool}: invalid mirror", f"mirror was {short(mirror)}",
                  "Use '' (none), 'x' (upside down) or 'y' (left-right).")  # fmt: skip
    if to is not None and by is not None:
        raise bad(f"{tool}: pass either to or by", "both were given",
                  "to=[x, y] places one item there; by=[dx, dy] shifts all items.")  # fmt: skip
    expect_net = args.get("expect_net")
    if expect_net is not None and (not isinstance(expect_net, str) or not expect_net.strip()):
        raise bad(f"{tool}: invalid expect_net", f"expect_net was {short(expect_net)}",
                  "Pass a net name as sch_get_net / sch_list_items report it.")  # fmt: skip
    if to is None and by is None and rotation is None and mirror is None:
        raise bad(f"{tool} has nothing to do", "no to, by, rotation or mirror was given",
                  "Pass to=[x, y], by=[dx, dy] and/or rotation / mirror.")  # fmt: skip

    def run(h: KicadHandle) -> ChangeSet:
        world = World(h)
        turning = rotation is not None or mirror is not None  # the §6.4 refusal names them
        allow = (*MOVE_KINDS, "junction", "no_connect", "wire") if turning else MOVE_KINDS
        targets = resolve(world, args.get("items"), tool, allow=allow)
        cs, _plan = plan_move(world, tool, targets, to, by, rotation, mirror, expect_net)
        cs.notes = [*notes, *cs.notes]
        return cs

    return await ctx.session.call(run)


# -- sch_delete ---------------------------------------------------------------------------


@dataclass
class DeleteChangeSet(ChangeSet):
    plan: DeletePlan | None = None
    before: Any = None  # the prepare-time netlist (nets_changed)
    refs: dict[tuple[str, str], str] | None = None


async def prepare_delete(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    tool = "sch_delete"
    with_attached = args.get("with_attached", True)
    if not isinstance(with_attached, bool):
        raise bad(
            f"{tool}: with_attached must be true or false",
            f"with_attached was {with_attached!r}",
            "Omit it to delete attached stubs, labels and power symbols too.",
        )
    placed = _placed_this_turn(ctx)

    def run(h: KicadHandle) -> DeleteChangeSet:
        world = World(h)
        targets = resolve(world, args.get("items"), tool)
        cs, plan = plan_delete(world, tool, targets, with_attached, placed)
        refs = {(s.sheet.path, s.uuid): s.ref for s in world.symbols}
        return DeleteChangeSet(
            **{f: getattr(cs, f) for f in _CS_FIELDS},
            plan=plan,
            before=world.netlist,
            refs=refs,
        )

    return await ctx.session.call(run)


_CS_FIELDS = ("tool", "sheet", "stages", "pre_images", "intent", "logical", "summary", "notes")


async def apply_delete(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    assert isinstance(cs, DeleteChangeSet) and cs.plan is not None
    applied = await apply_change_set(ctx, cs)
    plan = cs.plan
    label = _pin_labeler(cs.refs or {})
    deleted = {
        "symbols": sorted(
            {t.ref for t in plan.targets if t.kind == "symbol" and t.ref and not t.power}
        ),
        "power_symbols": sorted({t.ref for t in plan.targets if t.power and t.ref}),
        "sheets": [
            {"name": t.child.name if t.child else None, "file": t.child.file if t.child else None}
            for t in plan.targets
            if t.kind == "sheet"
        ],
        "sheet_pins": [
            {"sheet": t.pin.pin.ref, "pin": t.pin.pin.name}
            for t in plan.targets
            if t.kind == "sheet_pin" and t.pin is not None
        ],
        "child_labels": len(plan.child_labels),
        "attached": {
            "wires": sum(len(p.wires) for p in plan.attached.values()),
            "wires_shortened": sum(len(p.shortened) for p in plan.attached.values()),
            "labels": sum(len(p.labels) for p in plan.attached.values()),
            "junctions": sum(len(p.junctions) for p in plan.attached.values()),
            "no_connects": sum(len(p.no_connects) for p in plan.attached.values()),
            "power_symbols": sum(len(p.power) for p in plan.attached.values()),
        },
    }
    notes = [*cs.notes, *applied.notes]
    for t in plan.targets:
        if t.kind == "sheet" and t.child is not None:
            still = plan.sheet_users.get(t.uuid, [])
            notes.append(
                f"sheet {t.child.name}: the sheet symbol is deleted; its file {t.child.file} "
                "stays on disk"
                + (f" (still used by {', '.join(still)})" if still else " (no longer used)")
            )
    return edit_result(
        cs,
        applied,
        deleted_summary=deleted,
        nets_changed=nets_changed(cs.before, applied.netlist, plan.nets, label),
        notes=notes,
    )


# -- sch_shift_sheet ----------------------------------------------------------------------


def _mm(p: Point) -> list[float]:
    return [round(p[0], 4) + 0.0, round(p[1], 4) + 0.0]


def _box(b: Any) -> list[float]:
    return [round(v, 2) + 0.0 for v in (b.x0, b.y0, b.x1, b.y1)]


def _shift_args(args: dict[str, Any]) -> tuple[str | None, Point | None]:
    tool = SHIFT_TOOL
    sheet, by, center = args.get("sheet"), args.get("by"), args.get("center")
    if sheet is not None and not isinstance(sheet, str):
        raise bad(f"{tool}: sheet must be a string", f"sheet was {short(sheet)}",
                  "Pass a sheet instance path like '/amp/', a name or a page number.")  # fmt: skip
    if center is not None and not isinstance(center, bool):
        raise bad(f"{tool}: center must be true or false", f"center was {short(center)}",
                  "Pass center=true to centre the sheet's content on the page.")  # fmt: skip
    if by is not None and (
        not isinstance(by, list) or len(by) != 2 or not all(_coord(c) for c in by)
    ):
        raise bad(f"{tool}: invalid by", f"by was {short(by)}",
                  "Pass by=[dx, dy] in mm.")  # fmt: skip
    if (by is None) == (not center):
        raise bad(f"{tool}: pass exactly one of by and center",
                  "both were given" if by is not None else "neither was given",
                  "by=[dx, dy] shifts by an offset; center=true centres the content "
                  "on the page.")  # fmt: skip
    return sheet, (float(by[0]), float(by[1])) if by is not None else None


async def prepare_shift_sheet(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    tool = SHIFT_TOOL
    sheet_arg, by = _shift_args(args)

    def run(h: KicadHandle) -> ChangeSet:
        sheet = resolve_sheet(h, sheet_arg)
        sch = sheet_api(h, sheet)
        items = list(sch.get_items(types=sheet_item_types()))
        page = read_page(sch.get_page_settings())
        notes: list[str] = []
        unknown: list[str] = []
        content = union(b for i in items for b in item_boxes(i, unknown))
        if unknown:
            notes.append(f"the extent of {', '.join(unknown[:8])} could not be read: only "
                         "its anchor was counted for centring and the page check")  # fmt: skip
        summary: dict[str, Any] = {"sheet": sheet.as_dict(), "page": page.as_dict()}
        if content is None:
            return no_change(tool, sheet, {**summary, "offset_mm": [0.0, 0.0]},
                             [f"sheet {sheet.human_path} is empty: nothing to shift"])  # fmt: skip
        if by is not None:
            d = snap_offset(by)
            if not same_point(by, d, 1e-4):
                notes.append(f"by snapped to the 1.27 mm grid: {_mm(by)} -> {_mm(d)}")
        else:
            c = plan_centring(content, page)
            d = c.offset
            if not c.fits_frame:
                notes.append("the content is larger than the drawing frame: centred on it, "
                             "it reaches into the frame margins")  # fmt: skip
            elif not c.clear_of_title_block:
                notes.append("the content cannot be centred clear of the title block: "
                             "centred in the frame, it overlaps the title block")  # fmt: skip
            elif c.area != "frame":
                notes.append(f"centred {c.area} (centred in the frame it would overlap "
                             "the title block)")  # fmt: skip
        after = shifted(content, d)
        summary |= {"offset_mm": _mm(d), "bbox_before_mm": _box(content),
                    "bbox_after_mm": _box(after)}  # fmt: skip
        if d == (0.0, 0.0):
            msg = ("the content is already centred (offset 0 after snapping to the 1.27 mm "
                   "grid)" if by is None else "the offset is 0 after snapping to the 1.27 mm "
                   "grid: nothing to shift")  # fmt: skip
            return no_change(tool, sheet, summary, [*notes, msg])
        if not inside(after, page.paper):
            raise bad(
                f"{tool}: the content would leave the page",
                f"shifted by {_mm(d)} mm its box {_box(after)} is not inside the "
                f"{page.name} paper {_box(page.paper)}",
                "Shift by less, or use center=true; enlarge the page in KiCad if the "
                "content does not fit."
                if by is not None
                else "The content is larger than the paper: enlarge the page in KiCad "
                "(Page Settings) or make the drawing smaller first.",
                offset_mm=_mm(d),
                bbox_before_mm=_box(content),
                bbox_after_mm=_box(after),
                page=page.as_dict(),
            )
        if by is not None and not inside(after, page.frame):
            notes.append("the shifted content reaches into the frame margins")
        if by is not None and overlaps(after, page.title_block):
            notes.append("the shifted content overlaps the title block")
        if page.custom_sheet:
            notes.append("the page uses a custom drawing sheet: its frame and title block "
                         "were assumed to be KiCad's default ones")  # fmt: skip
        users = [r.human_path for r in sheet_refs(h) if r.file == sheet.file]
        if len(users) > 1:
            notes.append(f"{sheet.file} is used by {len(users)} sheet instances "
                         f"({', '.join(users[:8])}): all of them show the shift")  # fmt: skip
        plan = plan_shift(items, d)
        summary |= {"shifted": plan.counts}
        if plan.skipped:
            summary |= {"not_shifted": plan.skipped}  # groups: member lists, no position
        return shift_change_set(sheet, items, plan, summary, notes)

    return await ctx.session.call(run)


async def apply_shift_sheet(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    """Like `apply_change`, but without one row per shifted item or touched net (hundreds
    on a large sheet; a rigid shift changes no net): `shifted` counts the items by kind,
    `modified_count` / `nets_touched_count` the rest. Created / deleted rows (KiCad's
    cleanup) and side effects stay listed."""
    applied = await apply_change_set(ctx, cs)
    compact = replace(applied, modified=[], nets_touched=[])  # cs.applied stays complete
    return edit_result(cs, compact, modified_count=len(applied.modified),
                       nets_touched_count=len(applied.nets_touched))  # fmt: skip


# -- specs --------------------------------------------------------------------------------

_ITEM = {"type": "string", "maxLength": 128}
_ITEMS = {
    "anyOf": [
        {"type": "array", "items": _ITEM, "minItems": 1, "maxItems": 64},
        _ITEM,
    ]
}

_XY = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}

TOOL_SPECS: list[ToolSpec] = [
    edit_spec(
        "sch_move",
        "Move, rotate or mirror symbols (every unit of a reference, or one unit by UUID), "
        "labels and sheet symbols, and move sheet pins, keeping every connection. A moved "
        "symbol takes along every stub chain hanging only on its pins (wires with no "
        "junction, branch or other pin, any length) with the power symbols, labels and "
        "no-connects at its end or on the pin: by the same offset, or on a rotation / mirror "
        "turned with the part (a power symbol keeps pointing away, a label's text follows its"
        " stub); if a carried wire or item would hit something, the whole call is refused. "
        "Other attached wires are dragged (a diagonal becomes an L). A label or power symbol "
        "moved alone gets its stub laid anew from the pin that stays. Fields move with the "
        "part. to=[x, y] (one item) or by=[dx, dy] (mm, snapped to 1.27 mm). rotation is "
        "degrees CCW: absolute for a symbol and for a label's text direction (0 right, 90 up,"
        " 180 left, 270 down; about its anchor); for a sheet symbol a turn by that much about "
        "its centre (its pins change edges with it). mirror 'x' = upside down, 'y' = "
        "left-right: absolute for a symbol, a flip for a label's text or a sheet. Junctions, "
        "no-connects and wires do not rotate. A sheet pin (its UUID or '<sheet name>.<pin "
        "name>') lands on the nearest edge of its sheet symbol (at most 2.54 mm off it, on the"
        " grid, one pitch clear of the corners, not on another pin); on another edge its side"
        " and orientation change, its wires are dragged; rotation / mirror are refused for "
        "it. Refused before writing when a part would overlap (the nearest free position is "
        "suggested), a dragged wire would touch another net, or a moved / turned sheet box "
        "would cross or run along a wire that stays or cover a label, junction or "
        "no-connect; verified: the netlist stays identical — or, with expect_net, exactly one "
        "net of the moved items joins that net "
        "(e.g. a label dropped onto a wire) and nothing else changes; anything else is undone"
        " and named. Budget: 1 logical change per moved symbol, label, sheet or sheet pin; "
        "carried stubs and their end items are free.",
        _obj(
            {
                "items": {
                    **_ITEMS,
                    "description": "['R5', '<label uuid>', 'sheet:power', 'Power.VIN']",
                },
                "to": {**_XY, "description": "New position [x, y] in mm (one item)."},
                "by": {**_XY, "description": "Shift [dx, dy] in mm."},
                "rotation": {"type": "integer", "enum": [0, 90, 180, 270]},
                "mirror": {"type": "string", "enum": ["", "x", "y"]},
                "expect_net": {
                    "type": "string",
                    "description": "Net the move is meant to connect to (as sch_get_net / "
                    "sch_list_items name it); omit to keep every connection.",
                },
            },
            ["items"],
        ),
        prepare_move,
        apply_change,
    ),
    edit_spec(
        "sch_delete",
        "Delete items: references (every unit; '#PWR03' for a power symbol), item UUIDs "
        "(wires, labels, junctions, no-connects, sheet symbols, sheet pins), "
        "'<sheet name>.<pin name>' (a sheet pin) or 'sheet:<name>' (the sheet "
        "symbol; its file stays on disk). with_attached (default true) also removes what only "
        "served the deleted items: stub wires (cut back to a T where they branch), labels, "
        "junctions, no-connects and power symbols attached only to them; wires shared with "
        "other pins stay. A deleted sheet pin also takes the sub-sheet's hierarchical label "
        "of its name when that label is connected to nothing and no other sheet symbol of "
        "the same file still has such a pin (else a note says why it stays). Reports "
        "nets_changed. Budget: 1 logical change per symbol (all "
        "units), sheet or sheet pin, plus 1 per net for all wires, junctions, labels and "
        "no-connects listed on it; at most 3 symbols that existed before the turn per turn "
        "without a plan.",
        _obj(
            {
                "items": {
                    **_ITEMS,
                    "description": "['R5', '#PWR03', '<uuid>', 'sheet:power', 'Power.VIN']",
                },
                "with_attached": {"type": "boolean", "default": True},
            },
            ["items"],
        ),
        prepare_delete,
        apply_delete,
    ),
    *DUPLICATE_SPECS,
    edit_spec(
        "sch_shift_sheet",
        "Shift EVERYTHING on one sheet rigidly by one offset: symbols with their fields, "
        "wires, buses, bus entries, junctions, no-connects, all labels, sheet symbols with "
        "their pins and fields, texts, text boxes, graphics, images, tables. Use it when "
        "the whole drawing sits glued to a page edge or off-centre; to rearrange single "
        "parts use sch_move. center=true centres the content box (bodies, visible fields "
        "and label texts, wires, graphics) in the page's drawing frame (paper minus the "
        "10 mm border), clear of the title block in the bottom-right corner when it fits "
        "above or left of it (else centred over it, and a note says so); by=[dx, dy] (mm) "
        "shifts by an explicit offset. Exactly one of the two. The offset is always "
        "snapped to the 1.27 mm grid, so pins and wires stay on grid; 0 after snapping is "
        "a no-op. Sub-sheet contents and the frame / title block do not move. Refused "
        "when the content would leave the paper. Verified: the netlist stays identical, "
        "else undone. Reports offset_mm, bbox_before_mm / bbox_after_mm, the page frame "
        "and shifted counts per kind. Budget: 1 logical change.",
        _obj(
            {
                "sheet": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "Sheet instance path like '/amp/', name or page; default root.",
                },
                "by": {**_XY, "description": "Shift [dx, dy] in mm (snapped to 1.27 mm)."},
                "center": {
                    "type": "boolean",
                    "description": "true: centre the content in the page frame.",
                },
            }
        ),
        prepare_shift_sheet,
        apply_shift_sheet,
    ),
]
