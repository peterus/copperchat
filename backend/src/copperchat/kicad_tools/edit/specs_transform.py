"""Move / delete / duplicate tools (E6): `sch_move`, `sch_delete`, `sch_duplicate` —
all two-phase (E-C4)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.edit.changeset import ChangeSet
from copperchat.kicad_tools.edit.delete import DeletePlan, nets_changed, plan_delete
from copperchat.kicad_tools.edit.engine import _pin_labeler, apply_change_set, edit_result
from copperchat.kicad_tools.edit.labels import World
from copperchat.kicad_tools.edit.targets import bad, resolve
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


# -- specs --------------------------------------------------------------------------------

_ITEM = {"type": "string", "maxLength": 128}
_ITEMS = {
    "anyOf": [
        {"type": "array", "items": _ITEM, "minItems": 1, "maxItems": 64},
        _ITEM,
    ]
}

TOOL_SPECS: list[ToolSpec] = [
    edit_spec(
        "sch_delete",
        "Delete items: references (every unit; '#PWR03' for a power symbol), item UUIDs "
        "(wires, labels, junctions, no-connects, sheet symbols) or 'sheet:<name>' (the sheet "
        "symbol; its file stays on disk). with_attached (default true) also removes what only "
        "served the deleted items: stub wires (cut back to a T where they branch), labels, "
        "junctions, no-connects and power symbols attached only to them; wires shared with "
        "other pins stay. Reports nets_changed. One logical change per symbol, label or "
        "sheet; at most 3 symbols that existed before the turn per turn without a plan.",
        _obj(
            {
                "items": {**_ITEMS, "description": "['R5', '#PWR03', '<uuid>', 'sheet:power']"},
                "with_attached": {"type": "boolean", "default": True},
            },
            ["items"],
        ),
        prepare_delete,
        apply_delete,
    ),
]
