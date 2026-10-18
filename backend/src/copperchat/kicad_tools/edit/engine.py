"""Apply / verify / compensate engine for ChangeSets (E-C4).

Each Stage is one `session.commit`; ops go through `for_sheet(op.sheet or cs.sheet)`
(spike 2A Q4). After every push the engine reads back by id (kipy ignores per-item
statuses, 2A Q6) plus the wire/junction neighbourhood of every touched point, so
KiCad's local cleanup (dropped junctions, merged segments, 2A Q5) is recorded as side
effects. After the last stage the live netlist is checked against the intent; a
mismatch is compensated in reverse (created items removed, updated items restored,
deleted non-symbol items re-created with their original UUIDs; deleted symbols:
re-placed from the library without a board, `DocumentInconsistent(auto_restore=True)`
with one — owner decision 4), re-verified and reported as `verification_failed`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid as uuidlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from copperchat.changes.repo import ChangeEntry
from copperchat.errors import DocumentInconsistent, StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadUnavailable
from copperchat.kicad_tools import access
from copperchat.kicad_tools.access import Netlist, PinKey
from copperchat.kicad_tools.edit import verify
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Create,
    Delete,
    EditOp,
    Place,
    SheetRef,
    StageInput,
    Update,
)
from copperchat.kicad_tools.edit.geometry import (
    DEG_SSO,
    NM,
    Point,
    mm_point,
    on_segment,
    same_point,
)
from copperchat.kicad_tools.edit.scene import item_nets, sheet_ref, sheet_refs
from copperchat.tools.spec import ToolContext, ToolResult

log = logging.getLogger(__name__)

Verifier = Callable[..., list[str]]  # (before, after, intent, label) -> problems

_KINDS = {
    "SchematicSymbolInstance": "symbol",
    "LocalLabel": "label",
    "GlobalLabel": "global_label",
    "HierarchicalLabel": "hierarchical_label",
    "DirectiveLabel": "directive_label",
    "Junction": "junction",
    "NoConnectMarker": "no_connect",
    "SheetSymbol": "sheet",
    "SchematicText": "text",
    "SchematicTextBox": "text_box",
    "BusEntry": "bus_entry",
}


def item_kind(item: Any) -> str:
    name = type(item).__name__
    if name == "SchematicLine":
        from kipy.proto.schematic.schematic_types_pb2 import SchematicLineType

        line_type = int(item.type)
        if line_type == SchematicLineType.SLT_WIRE:
            return "wire"
        return "bus" if line_type == SchematicLineType.SLT_BUS else "line"
    return _KINDS.get(name, name)


def copy_item(item: Any) -> Any:
    """Independent copy of a kipy wrapper (its proto is copied)."""
    return type(item)(proto=item.proto)


def _points(item: Any) -> list[Point]:
    """Connection-relevant points of an item (wire ends, positions)."""
    if hasattr(item, "start") and hasattr(item, "end"):
        return [mm_point(item.start), mm_point(item.end)]
    if hasattr(item, "position"):
        return [mm_point(item.position)]
    return []


def item_summary(item: Any) -> dict[str, Any]:
    """What a change row and a tool result say about an item (read from the API)."""
    kind = item_kind(item)
    out: dict[str, Any] = {"kind": kind}
    if kind == "symbol":
        out["ref"] = str(item.reference)
        out["value"] = str(item.value)
        out["lib_id"] = str(item.definition.id) if item.proto.HasField("definition") else None
        out["unit"] = int(item.unit)
        out["at_mm"] = list(mm_point(item.position))
        tr = item.transform
        out["rotation"] = {1: 0, 2: 90, 3: 180, 4: 270}.get(int(tr.orientation), 0)
        out["mirror"] = "x" if tr.mirror_x else "y" if tr.mirror_y else ""
        out["footprint"] = access.field_text(item.footprint_field)
    elif hasattr(item, "start") and hasattr(item, "end"):
        out["points_mm"] = [list(mm_point(item.start)), list(mm_point(item.end))]
    elif hasattr(item, "position"):
        out["at_mm"] = list(mm_point(item.position))
    if hasattr(item, "text") and kind.endswith("label"):
        out["text"] = str(item.text.value)
    elif kind == "sheet":
        out["name"] = access.field_text(item.name_field)
        out["file"] = access.field_text(item.filename_field)
    if out.get("lib_id") is None:
        out.pop("lib_id", None)
    return out


def _kiid(value: str) -> Any:
    from kipy.proto.common.types import KIID

    k = KIID()
    k.value = value
    return k


def by_id(sch: Any, ids: Iterable[str]) -> dict[str, Any]:
    """GetItemsById tolerant of missing ids (one missing id fails the whole call)."""
    wanted = list(dict.fromkeys(ids))
    if not wanted:
        return {}
    try:
        return {access.kiid(i): i for i in sch.get_items_by_id([_kiid(u) for u in wanted])}
    except Exception:
        out: dict[str, Any] = {}
        for u in wanted:
            try:
                for i in sch.get_items_by_id([_kiid(u)]):
                    out[access.kiid(i)] = i
            except Exception:  # a missing id is what this checks for
                continue
        return out


@dataclass
class Applied:
    created: list[Any]
    modified: list[Any]
    deleted: list[str]
    side_effects: list[ChangeEntry]
    netlist: Netlist
    # extras (E2): where each item lives, the read-back of deleted items and nets
    sheet_of: dict[str, SheetRef] = field(default_factory=dict)
    deleted_items: dict[str, Any] = field(default_factory=dict)
    placed: list[str] = field(default_factory=list)
    nets_touched: list[str] = field(default_factory=list)
    item_net: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    changes: list[ChangeEntry] = field(default_factory=list)


@dataclass
class _Neighbour:
    item: Any
    sheet: SheetRef


@dataclass
class _Run:
    """Bookkeeping of one apply_change_set call (accessed from worker threads, one call
    at a time)."""

    cs: ChangeSet
    created: dict[str, SheetRef] = field(default_factory=dict)  # uuid -> sheet, in order
    placed: list[str] = field(default_factory=list)
    modified: dict[str, SheetRef] = field(default_factory=dict)  # pre-existing items only
    deleted: dict[str, SheetRef] = field(default_factory=dict)
    pre: dict[str, Any] = field(default_factory=dict)  # uuid -> pre-image
    neighbours: dict[str, _Neighbour] = field(default_factory=dict)
    side: dict[str, tuple[str, dict[str, Any] | None, dict[str, Any] | None]] = field(
        default_factory=dict
    )  # uuid -> (op, before, after) of cleanup side effects
    side_sheet: dict[str, SheetRef] = field(default_factory=dict)
    vanished_created: dict[str, dict[str, Any]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    pushed: int = 0
    replaced: dict[str, str] = field(default_factory=dict)  # compensation: old -> new symbol

    def sheet(self, op: EditOp) -> SheetRef:
        return op.sheet or self.cs.sheet

    def ours(self, uid: str) -> bool:
        return uid in self.created or uid in self.modified or uid in self.deleted


class _Sheets:
    """`for_sheet` schematics of one worker-thread call, resolved once."""

    def __init__(self, h: KicadHandle) -> None:
        self.h = h
        self._inst: dict[str, Any] | None = None
        self._api: dict[str, Any] = {}

    def api(self, sheet: SheetRef) -> Any:
        if sheet.path not in self._api:
            if self._inst is None:
                self._inst = {sheet_ref(i).path: i for i in self.h.schematic().get_hierarchy()}
            inst = self._inst.get(sheet.path)
            if inst is None:
                raise StructuredToolError(
                    f"Sheet {sheet.human_path} not found",
                    "The sheet instance no longer exists in the schematic",
                    "Re-read the hierarchy (project_info) and retry.",
                    code="not_found",
                )
            self._api[sheet.path] = self.h.schematic().for_sheet(inst)
        return self._api[sheet.path]


def _touched_points(run: _Run, ops: list[EditOp]) -> dict[str, list[Point]]:
    """Per sheet path: every point a stage creates, moves or removes."""
    out: dict[str, list[Point]] = {}
    for op in ops:
        sheet = run.sheet(op)
        pts = out.setdefault(sheet.path, [])
        if isinstance(op, (Create, Update)):
            for item in op.items:
                pts += _points(item)
                pre = run.pre.get(access.kiid(item))
                if pre is not None:
                    pts += _points(pre)
        elif isinstance(op, Delete):
            for uid in op.uuids:
                pre = run.pre.get(uid)
                if pre is not None:
                    pts += _points(pre)
    return out


def _targets(ops: list[EditOp]) -> set[str]:
    out: set[str] = set()
    for op in ops:
        if isinstance(op, (Create, Update)):
            out |= {access.kiid(i) for i in op.items if i.proto.id.value}
        elif isinstance(op, Delete):
            out |= set(op.uuids)
    return out


def _read_neighbourhood(run: _Run, sheets: _Sheets, ops: list[EditOp]) -> None:
    """Pre-images of wires and junctions at (or through) touched points: KiCad's push
    cleanup may merge or drop them (spike 2A Q5)."""
    by_path = {run.sheet(op).path: run.sheet(op) for op in ops}
    targets = _targets(ops)
    for path, pts in _touched_points(run, ops).items():
        if not pts:
            continue
        sch = sheets.api(by_path[path])
        from kipy.proto.schematic.schematic_types_pb2 import SchematicLineType

        for line in sch.get_lines():
            uid = access.kiid(line)
            if int(line.type) != SchematicLineType.SLT_WIRE or run.ours(uid) or uid in targets:
                continue
            a, b = mm_point(line.start), mm_point(line.end)
            if any(on_segment(p, a, b) for p in pts) and uid not in run.neighbours:
                run.neighbours[uid] = _Neighbour(copy_item(line), by_path[path])
        for j in sch.get_junctions():
            uid = access.kiid(j)
            if run.ours(uid) or uid in run.neighbours or uid in targets:
                continue
            if any(same_point(p, mm_point(j.position)) for p in pts):
                run.neighbours[uid] = _Neighbour(copy_item(j), by_path[path])


def _stage(h: KicadHandle, run: _Run, ops: list[EditOp]) -> None:
    """Runs inside the stage's commit: pre-images first, then the writes."""
    sheets = _Sheets(h)
    for op in ops:  # pre-images of everything updated or deleted, before any write
        uids = (
            [access.kiid(i) for i in op.items]
            if isinstance(op, Update)
            else list(op.uuids)
            if isinstance(op, Delete)
            else []
        )
        missing = [u for u in uids if u not in run.pre and u not in run.created]
        for u in missing:
            if u in run.neighbours:  # an earlier stage's neighbour: its original state
                run.pre[u] = run.neighbours.pop(u).item
                run.side.pop(u, None)
                run.side_sheet.pop(u, None)
                continue
            if u in run.cs.pre_images:
                run.pre[u] = copy_item(run.cs.pre_images[u])
        missing = [u for u in missing if u not in run.pre]
        if missing:
            found = by_id(sheets.api(run.sheet(op)), missing)
            for u in missing:
                if u not in found:
                    raise StructuredToolError(
                        f"{run.cs.tool}: item {u} not found",
                        "It does not exist (any more) on this sheet",
                        "Re-read the sheet and retry with current UUIDs.",
                        code="not_found",
                    )
                run.pre[u] = copy_item(found[u])
    _read_neighbourhood(run, sheets, ops)
    for op in ops:
        sheet = run.sheet(op)
        sch = sheets.api(sheet)
        if isinstance(op, Create):
            for item in op.items:
                if not item.proto.id.value:
                    item.proto.id.value = str(uuidlib.uuid4())
            sch.create_items(list(op.items))
            for item in op.items:
                run.created[access.kiid(item)] = sheet
        elif isinstance(op, Update):
            for item in op.items:
                if type(item).__name__ == "SchematicSymbolInstance":
                    item.proto.ClearField("definition")  # spike 2A Q2: never with definition
            sch.update_items(list(op.items))
            for item in op.items:
                uid = access.kiid(item)
                if uid not in run.created:
                    run.modified.setdefault(uid, sheet)
        elif isinstance(op, Delete):
            sch.remove_items_by_id([_kiid(u) for u in op.uuids])
            for u in op.uuids:
                if u in run.created:  # created by an earlier stage: just gone again
                    run.created.pop(u)
                    if u in run.placed:
                        run.placed.remove(u)
                else:
                    run.modified.pop(u, None)
                    run.deleted[u] = sheet
        elif isinstance(op, Place):
            sym = sch.place_symbol_from_library(
                op.lib_id,
                _v2(op.at_mm),
                orientation=DEG_SSO[op.orientation % 360],
                unit=op.unit,
                reference=op.reference,
            )
            uid = access.kiid(sym)
            run.created[uid] = sheet
            run.placed.append(uid)


def _v2(at: tuple[float, float]) -> Any:
    from kipy.geometry import Vector2

    return Vector2.from_xy(round(at[0] * NM), round(at[1] * NM))


def _read_back(h: KicadHandle, run: _Run, stage_created: list[str]) -> StageInput:
    """After a push: presence by id, deletes gone, cleanup side effects."""
    sch = h.schematic()
    ids = [*run.created, *run.modified, *run.deleted, *run.neighbours]
    found = by_id(sch, ids)
    for u in list(run.created):
        if u not in found and u in stage_created:
            # KiCad merged a fresh segment into another one at push (spike 2A Q5).
            run.vanished_created[u] = {"kind": "wire"}
            run.created.pop(u)
    for u in run.modified:
        if u not in found:
            run.problems.append(f"modified item {u} disappeared")
    for u in run.deleted:
        if u in found:
            run.problems.append(f"item {u} was not deleted")
    for u, nb in run.neighbours.items():
        before = item_summary(nb.item)
        if u not in found:
            run.side[u] = ("delete", before, None)
            run.side_sheet[u] = nb.sheet
            continue
        after = item_summary(found[u])
        if after != before:
            run.side[u] = ("modify", before, after)
            run.side_sheet[u] = nb.sheet
        else:
            run.side.pop(u, None)
    created = [found[u] for u in run.created if u in found]
    return StageInput(
        created=created,
        placed=[found[u] for u in run.placed if u in found],
        modified=[found[u] for u in run.modified if u in found],
    )


# -- compensation -----------------------------------------------------------------------


def _copy_symbol_onto(src: Any, dst: Any) -> None:
    """Everything but identity from the deleted symbol onto its re-placed copy (spike
    2A Q10 method A)."""
    for name in ("value_field", "footprint_field", "datasheet_field", "description_field"):
        getattr(dst, name).proto.CopyFrom(getattr(src, name).proto)
    ref = dst.reference
    dst.reference_field.proto.CopyFrom(src.reference_field.proto)
    dst.reference = ref
    dst.proto.ClearField("user_fields")
    for uf in src.user_fields:
        dst.proto.user_fields.add().CopyFrom(uf.proto)
    dst.attributes = src.attributes
    dst.transform = src.transform
    dst.body_style = src.body_style
    dst.show_pin_numbers = src.show_pin_numbers
    dst.show_pin_names = src.show_pin_names
    dst.position = src.position
    dst.proto.ClearField("definition")


def _has_board(ctx: ToolContext) -> bool:
    pcb = ctx.project.pcb_file
    return pcb is not None and pcb.exists()


async def _compensate(
    ctx: ToolContext, run: _Run, before: Netlist, *, has_board: bool
) -> dict[str, str]:
    """Undo everything pushed so far; returns old -> new UUIDs of re-placed symbols.
    Raises DocumentInconsistent when the document cannot be brought back exactly."""
    session = ctx.session
    symbols_deleted = {
        u: s
        for u, s in run.deleted.items()
        if type(run.pre.get(u)).__name__ == "SchematicSymbolInstance"
    }
    replace = {} if has_board else symbols_deleted
    placed_new: dict[str, str] = {}

    def undo(h: KicadHandle) -> None:
        sheets = _Sheets(h)
        by_sheet: dict[str, tuple[SheetRef, list[str]]] = {}
        for u, s in run.created.items():
            by_sheet.setdefault(s.path, (s, []))[1].append(u)
        for s, uids in by_sheet.values():
            sheets.api(s).remove_items_by_id([_kiid(u) for u in uids])
        restore: dict[str, tuple[SheetRef, list[Any]]] = {}
        for u, s in run.modified.items():
            restore.setdefault(s.path, (s, []))[1].append(copy_item(run.pre[u]))
        recreate: dict[str, tuple[SheetRef, list[Any]]] = {}
        for u, s in run.deleted.items():
            if u not in symbols_deleted:
                recreate.setdefault(s.path, (s, []))[1].append(copy_item(run.pre[u]))
        for u, (op, _b, _a) in run.side.items():
            nb = run.neighbours[u]
            target = restore if op == "modify" else recreate
            target.setdefault(nb.sheet.path, (nb.sheet, []))[1].append(copy_item(nb.item))
        for s, items in restore.values():
            for item in items:
                if type(item).__name__ == "SchematicSymbolInstance":
                    item.proto.ClearField("definition")
            sheets.api(s).update_items(items)
        for s, items in recreate.values():
            sheets.api(s).create_items(items)
        for u, s in replace.items():
            pre = run.pre[u]
            sym = sheets.api(s).place_symbol_from_library(
                str(pre.definition.id),
                pre.position,
                orientation=int(pre.transform.orientation) or DEG_SSO[0],
                unit=int(pre.unit),
                reference=str(pre.reference),
            )
            placed_new[u] = access.kiid(sym)

    def finish_replace(h: KicadHandle) -> None:
        sheets = _Sheets(h)
        for old, new in placed_new.items():
            sch = sheets.api(replace[old])
            dst = by_id(sch, [new])[new]
            _copy_symbol_onto(run.pre[old], dst)
            sch.update_items([dst])

    try:
        async with session.commit(f"{run.cs.tool}: undo"):
            await session.call(undo)
        if placed_new:
            async with session.commit(f"{run.cs.tool}: undo (fields)"):
                await session.call(finish_replace)
    except (asyncio.CancelledError, KicadUnavailable):
        raise
    except Exception as exc:
        raise DocumentInconsistent(f"compensating {run.cs.tool} failed: {exc}") from exc
    if has_board and symbols_deleted:
        raise DocumentInconsistent(
            f"{run.cs.tool}: deleted symbols cannot be restored losslessly in a project with "
            "a board; the last checkpoint is restored at turn end",
            auto_restore=True,
        )
    # Re-verify: original UUIDs back, created ones gone, netlist identical.
    expect_present = [u for u in [*run.modified, *run.deleted] if u not in replace]
    expect_present += [u for u, (op, _b, _a) in run.side.items()]
    expect_present += list(placed_new.values())

    def recheck(h: KicadHandle) -> tuple[set[str], Netlist]:
        found = by_id(h.schematic(), [*expect_present, *run.created])
        return set(found), access.live_netlist(h)

    present, after = await session.call(recheck)
    missing = [u for u in expect_present if u not in present]
    leftover = [u for u in run.created if u in present]
    back = {new: old for old, new in placed_new.items()}
    if missing or leftover or verify.remap(after, back).partition() != before.partition():
        raise DocumentInconsistent(
            f"compensating {run.cs.tool} did not restore the document "
            f"(missing {missing}, left over {leftover})"
        )
    return placed_new


# -- apply ------------------------------------------------------------------------------


def _pin_labeler(symbols: dict[tuple[str, str], str]) -> verify.PinLabel:
    def label(key: PinKey) -> str:
        ref = symbols.get((key[0], key[1]))
        return f"{ref}.{key[2]}" if ref else f"{key[1][:8]}.{key[2]}"

    return label


def _before(h: KicadHandle) -> tuple[Netlist, dict[tuple[str, str], str]]:
    symbols = h.schematic().get_symbols()
    refs = {(access.sheet_path_key(s.path), access.kiid(s)): str(s.reference) for s in symbols}
    return access.live_netlist(h, symbols), refs


def _change(
    ctx: ToolContext,
    run: _Run,
    op: str,
    uid: str,
    sheet: SheetRef,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    net: str | None,
    side: bool = False,
) -> ChangeEntry:
    state = ctx.editing.turn(ctx.project.id, ctx.run_id) if ctx.editing is not None else None
    info = after or before or {}
    return ChangeEntry(
        project_id=ctx.project.id,
        run_id=ctx.run_id or "",
        plan_step_id=state.active_step_id if state else None,
        call_seq=state.call_seq if state else 0,
        tool=run.cs.tool,
        op=op,
        kind=str(info.get("kind", "item")),
        uuid=uid,
        sheet_path=sheet.path,
        ref=info.get("ref"),
        net=net,
        before=before,
        after=after,
        side_effect=side,
    )


def _entries(
    ctx: ToolContext, run: _Run, applied: Applied, final: dict[str, Any]
) -> list[ChangeEntry]:
    net = applied.item_net.get
    out: list[ChangeEntry] = []
    for u, s in run.created.items():
        if u in final:
            out.append(_change(ctx, run, "add", u, s, None, item_summary(final[u]), net=net(u)))
    for u, s in run.modified.items():
        if u in final:
            pre, now = item_summary(run.pre[u]), item_summary(final[u])
            out.append(_change(ctx, run, "modify", u, s, pre, now, net=net(u)))
    for u, s in run.deleted.items():
        out.append(_change(ctx, run, "delete", u, s, item_summary(run.pre[u]), None, net=None))
    for u, (op, before, after) in run.side.items():
        sheet = run.side_sheet[u]
        out.append(_change(ctx, run, op, u, sheet, before, after, net=net(u), side=True))
    return out


async def apply_change_set(
    ctx: ToolContext,
    cs: ChangeSet,
    *,
    _verify: Verifier | None = None,
    _before_compensate: Callable[[], None] | None = None,
) -> Applied:
    """Write `cs` stage by stage, verify by read-back, compensate on mismatch.

    `_verify` / `_before_compensate` are test seams (force a mismatch, inject a
    compensation failure)."""
    session = ctx.session
    run = _Run(cs)
    before, refs = await session.call(_before)
    label = _pin_labeler(refs)
    has_board = _has_board(ctx)
    inp = StageInput()

    async def undo_after(error: BaseException) -> None:
        if run.pushed == 0:
            return
        if _before_compensate is not None:
            try:
                _before_compensate()
            except Exception as exc:
                raise DocumentInconsistent(f"compensating {cs.tool} failed: {exc}") from exc
        run.replaced = await _compensate(ctx, run, before, has_board=has_board)

    try:
        for stage in cs.stages:
            ops = stage.build(inp)
            created_before = set(run.created)

            def write(h: KicadHandle, ops: list[EditOp] = ops) -> None:
                _stage(h, run, ops)

            try:
                async with session.commit(f"{cs.tool}: {stage.label}"):
                    await session.call(write)
            except (StructuredToolError, KicadUnavailable):
                raise
            except Exception as exc:
                # The commit was dropped; KiCad refused an op (ApiError) or a build bug.
                from kipy.errors import ApiError

                if not isinstance(exc, ApiError):
                    raise
                raise StructuredToolError(
                    f"KiCad refused {cs.tool} ({stage.label})",
                    str(exc),
                    "Nothing of this step was applied; check the arguments and retry.",
                    code="invalid_argument",
                ) from exc
            run.pushed += 1
            stage_created = [u for u in run.created if u not in created_before]

            def read(h: KicadHandle, sc: list[str] = stage_created) -> StageInput:
                return _read_back(h, run, sc)

            inp = await session.call(read)
    except asyncio.CancelledError:
        if run.pushed:
            # The turn is being stopped: undo the pushed stages, then keep cancelling.
            task = asyncio.ensure_future(undo_after(asyncio.CancelledError()))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                with contextlib.suppress(BaseException):
                    await task
            except DocumentInconsistent as exc:
                if ctx.on_document_inconsistent is not None:
                    ctx.on_document_inconsistent(exc)
        raise
    except KicadUnavailable:
        raise  # the server restarted from the last save: pushed stages are gone with it
    except BaseException as exc:
        await undo_after(exc)
        raise

    def final_read(h: KicadHandle) -> tuple[Netlist, dict[str, Any], dict[str, str]]:
        found = by_id(h.schematic(), [*run.created, *run.modified])
        nets = item_nets(h)
        paths = {u: s.path for u, s in [*run.created.items(), *run.modified.items()]}
        item_net = {u: nets[(p, u)] for u, p in paths.items() if (p, u) in nets}
        return access.live_netlist(h), found, item_net

    after, final, item_net = await session.call(final_read)
    problems = list(run.problems)
    problems += (_verify or verify.check)(before, after, cs.intent, label)
    if problems:
        await undo_after(RuntimeError("verification failed"))
        details: dict[str, Any] = {"expected": _expected(cs, label), "found": problems}
        if run.replaced:  # deleted symbols re-placed from the library (new UUIDs)
            details["replaced"] = dict(run.replaced)
        raise StructuredToolError(
            f"{cs.tool} did not produce the intended connectivity; it was undone",
            "; ".join(problems[:6]),
            "Nothing was changed. Re-read the affected pins (sch_get_pin) and retry with "
            "corrected positions, or use labels instead of a wire.",
            code="verification_failed",
            details=details,
        )
    nets_touched = sorted(
        {n for k in _intent_keys(cs) if (n := after.net_of(k)) is not None} | set(item_net.values())
    )
    applied = Applied(
        created=[final[u] for u in run.created if u in final],
        modified=[final[u] for u in run.modified if u in final],
        deleted=list(run.deleted),
        side_effects=[],
        netlist=after,
        sheet_of={**run.created, **run.modified, **run.deleted, **run.side_sheet},
        deleted_items={u: run.pre[u] for u in run.deleted},
        placed=list(run.placed),
        nets_touched=nets_touched,
        item_net=item_net,
    )
    if run.vanished_created:
        applied.notes.append(
            f"KiCad merged {len(run.vanished_created)} new segment(s) into existing wires"
        )
    applied.changes = _entries(ctx, run, applied, final)
    applied.side_effects = [e for e in applied.changes if e.side_effect]
    cs.applied = applied
    cs.logical.placed_symbols = list(run.placed)
    if ctx.editing is not None:
        try:
            await ctx.editing.record_changes(ctx, applied.changes)
        except Exception:  # the edit is applied and verified; never fail it afterwards
            log.exception("recording changes of %s failed", cs.tool)
    return applied


def _intent_keys(cs: ChangeSet) -> set[PinKey]:
    keys: set[PinKey] = set(cs.intent.may_change) | set(cs.intent.names)
    for group in cs.intent.merge:
        keys |= group
    return keys


def _expected(cs: ChangeSet, label: verify.PinLabel) -> dict[str, Any]:
    return {
        "merge": [sorted(label(k) for k in g) for g in cs.intent.merge],
        "may_change": sorted(label(k) for k in cs.intent.may_change),
        "names": {label(k): v for k, v in cs.intent.names.items()},
        "keep_partition": cs.intent.keep_partition,
    }


# -- results ----------------------------------------------------------------------------


def _entry_dict(uid: str, summary: dict[str, Any], sheet: SheetRef | None) -> dict[str, Any]:
    out = {"uuid": uid, **summary}
    if sheet is not None:
        out["sheet"] = sheet.human_path
    return out


def edit_result(cs: ChangeSet, applied: Applied, **extra: Any) -> ToolResult:
    """The standard mutating result (E-C4); `budget` is filled in by the adapter."""
    sheet_of = applied.sheet_of

    def row(item: Any) -> dict[str, Any]:
        uid = access.kiid(item)
        out = _entry_dict(uid, item_summary(item), sheet_of.get(uid))
        if uid in applied.item_net:
            out["net"] = applied.item_net[uid]
        return out

    side = [
        {
            "uuid": e.uuid,
            "op": e.op,
            "kind": e.kind,
            "before": e.before,
            "after": e.after,
        }
        for e in applied.side_effects
    ]
    data: dict[str, Any] = {
        "created": [row(i) for i in applied.created],
        "modified": [row(i) for i in applied.modified],
        "deleted": [
            _entry_dict(u, item_summary(applied.deleted_items[u]), sheet_of.get(u))
            for u in applied.deleted
        ],
        "side_effects": side,
        "nets_touched": applied.nets_touched,
        "connectivity_verified": True,
        "logical_changes": cs.logical.changes,
        "budget": None,
        "notes": [*cs.notes, *applied.notes],
    }
    data.update(cs.summary)
    data.update(extra)
    return ToolResult(data=data, changed_docs={"sch"})


def root_sheet(h: KicadHandle) -> SheetRef:
    return sheet_refs(h)[0]
