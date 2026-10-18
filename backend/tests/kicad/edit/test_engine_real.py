"""Apply / verify / compensate engine against a real api-server (E2 Task 3)."""

from __future__ import annotations

from typing import Any

import pytest
from kipy.schematic_types import Junction, NoConnectMarker

from copperchat.db import Database
from copperchat.errors import DocumentInconsistent, StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Create,
    Delete,
    Logical,
    NetIntent,
    Place,
    SheetRef,
    Stage,
    StageInput,
    Update,
    static_stage,
)
from copperchat.kicad_tools.edit.engine import apply_change_set, edit_result, root_sheet
from copperchat.kicad_tools.edit.geometry import pin_ends
from copperchat.kicad_tools.edit.scene import resolve_sheet, sheet_api, sheet_refs
from tests.kicad.edit.helpers import item_ids, local_label, v2, wire
from tests.support.edit import (
    RUN_ID,
    edit_ctx,
    free_area,
    get_item,
    kiids,
    place_direct,
    two_resistors,
)

pytestmark = pytest.mark.kicad


def _forced(*_a: Any) -> list[str]:
    return ["forced mismatch"]


async def _state(s: KicadSession) -> tuple[set[str], frozenset[Any]]:
    def run(h: KicadHandle) -> tuple[set[str], frozenset[Any]]:
        return item_ids(h), access.live_netlist(h).partition()

    return await s.call(run)


async def _root(s: KicadSession) -> SheetRef:
    return await s.call(root_sheet)


def _cs(sheet: SheetRef, *stages: Stage, intent: NetIntent | None = None, changes: int = 1,
        deleted: list[str] | None = None) -> ChangeSet:  # fmt: skip
    return ChangeSet(
        tool="test_tool",
        sheet=sheet,
        stages=list(stages),
        pre_images={},
        intent=intent or NetIntent(),
        logical=Logical(changes, list(deleted or [])),
    )


async def test_create_verified_and_logged(ecc83_session: KicadSession, edit_db: Database) -> None:
    s = ecc83_session
    r = await two_resistors(s)
    a2, b2 = r["a2"].at, r["b2"].at
    mid_y = a2[1] + 2.54
    wires = [wire(a2, (a2[0], mid_y)), wire((a2[0], mid_y), (b2[0], mid_y)),
             wire((b2[0], mid_y), b2)]  # fmt: skip
    ctx = edit_ctx(s, edit_db)
    cs = _cs(await _root(s), static_stage("wire", Create(wires)),
             intent=NetIntent(merge=[frozenset({r["a2"].key, r["b2"].key})]))  # fmt: skip
    applied = await apply_change_set(ctx, cs)
    assert cs.applied is applied
    assert {access.kiid(w) for w in applied.created} == {access.kiid(w) for w in wires}
    assert applied.netlist.net_of(r["a2"].key) == applied.netlist.net_of(r["b2"].key)
    rows = await ctx.editing.changes.for_run(s.project.id, RUN_ID)  # type: ignore[union-attr]
    assert [(e.op, e.kind) for e in rows] == [("add", "wire")] * 3
    assert {e.sheet_path for e in rows} == {cs.sheet.path}
    assert not any(e.side_effect for e in rows)


async def test_mismatch_compensates_create(ecc83_session: KicadSession, edit_db: Database) -> None:
    s = ecc83_session
    r = await two_resistors(s)
    a2 = r["a2"].at
    before = await _state(s)
    dangling = [
        wire(a2, (a2[0], a2[1] + 2.54)),
        wire((a2[0], a2[1] + 2.54), (a2[0] + 5.08, a2[1] + 2.54)),
    ]
    ctx = edit_ctx(s, edit_db)
    cs = _cs(await _root(s), static_stage("wire", Create(dangling)),
             intent=NetIntent(merge=[frozenset({r["a2"].key, r["b2"].key})]))  # fmt: skip
    with pytest.raises(StructuredToolError) as err:
        await apply_change_set(ctx, cs)
    assert err.value.code == "verification_failed"
    assert err.value.details is not None
    assert err.value.details["expected"]["merge"] == [["R901.2", "R902.2"]]
    assert err.value.details["found"]
    assert await _state(s) == before
    assert await ctx.editing.changes.for_run(s.project.id, RUN_ID) == []  # type: ignore[union-attr]


async def test_mismatch_compensates_update_and_delete(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    x, y = await s.call(lambda h: free_area(h, 20.32, 10.16))
    keep = local_label("KEEP", (x, y))
    gone = local_label("GONE", (x + 10.16, y))
    nc_item = NoConnectMarker()
    nc_item.position = v2(x, y + 5.08)
    nc_item.proto.id.value = "2f0e3c8e-4c1d-4c55-9a43-6f5bb1e3a001"
    async with s.commit("setup"):
        await s.call(lambda h: h.schematic().create_items([keep, gone, nc_item]))
    keep_id, gone_id, nc_id = access.kiid(keep), access.kiid(gone), access.kiid(nc_item)
    before = await _state(s)

    def renamed(h: KicadHandle) -> Any:
        item = h.schematic().get_items_by_id(kiids(keep_id))[0]
        item.text.value = "RENAMED"
        return item

    updated = await s.call(renamed)
    cs = _cs(await _root(s), static_stage("edit", Update([updated]), Delete([gone_id, nc_id])))
    with pytest.raises(StructuredToolError) as err:
        await apply_change_set(edit_ctx(s), cs, _verify=_forced)
    assert err.value.code == "verification_failed"
    assert await _state(s) == before

    def texts(h: KicadHandle) -> dict[str, str]:
        return {
            access.kiid(i): str(i.text.value)
            for i in h.schematic().get_items_by_id(kiids(keep_id, gone_id))
        }

    assert await s.call(texts) == {keep_id: "KEEP", gone_id: "GONE"}


async def _resistor_with_fields(s: KicadSession, ref: str, at: tuple[float, float]) -> str:
    uid = await place_direct(s, "Device:R", at, ref, transform=(90, False, False))

    def fields(h: KicadHandle) -> None:
        sch = h.schematic()
        sym = get_item(h, uid)
        sym.value_field.text.value = "4k7"
        sym.footprint_field.text.value = "Resistor_SMD:R_0805_2012Metric"
        sym.proto.ClearField("definition")
        sch.update_items([sym])

    async with s.commit("fields"):
        await s.call(fields)
    return uid


async def test_compensated_symbol_delete_is_replaced_from_library(
    blank_nopcb_session: KicadSession,
) -> None:
    s = blank_nopcb_session
    assert s.project.pcb_file is None
    await s.ensure_symbol_libraries()
    r1 = await _resistor_with_fields(s, "R1", (50.8, 50.8))
    await _resistor_with_fields(s, "R2", (76.2, 50.8))
    async with s.commit("label"):  # R1.1 on a named net
        p1 = await s.call(lambda h: {p.number: p for p in pin_ends(get_item(h, r1))})
        await s.call(lambda h: h.schematic().create_items([local_label("N1", p1["1"].at)]))
    _ids, partition = await _state(s)
    cs = _cs(await _root(s), static_stage("delete", Delete([r1])), deleted=[r1])
    with pytest.raises(StructuredToolError) as err:
        await apply_change_set(edit_ctx(s), cs, _verify=_forced)
    assert err.value.code == "verification_failed"

    def r1_now(h: KicadHandle) -> list[Any]:
        return [x for x in h.schematic().get_symbols() if str(x.reference) == "R1"]

    found = await s.call(r1_now)
    assert len(found) == 1
    new = found[0]
    assert access.kiid(new) != r1
    assert str(new.value) == "4k7"
    assert access.field_text(new.footprint_field) == "Resistor_SMD:R_0805_2012Metric"
    assert int(new.unit) == 1 and int(new.transform.orientation) == 2  # SSO_90
    assert err.value.details is not None and err.value.details["replaced"] == {r1: access.kiid(new)}
    _ids2, partition2 = await _state(s)
    remapped = frozenset(
        frozenset((p, r1 if u == access.kiid(new) else u, n) for p, u, n in g) for g in partition2
    )
    assert remapped == partition
    await s.save("sch")
    assert ":R_1" not in s.project.sch_file.read_text()  # type: ignore[union-attr]


async def test_compensated_symbol_delete_with_board_requests_restore(
    ecc83_session: KicadSession,
) -> None:
    s = ecc83_session
    r = await two_resistors(s)
    a, _b = r["uuids"]
    cs = _cs(await _root(s), static_stage("delete", Delete([a])), deleted=[a])
    with pytest.raises(DocumentInconsistent) as err:
        await apply_change_set(edit_ctx(s), cs, _verify=_forced)
    assert err.value.auto_restore is True


async def test_one_stage_writes_two_sheet_instances(hier_session: KicadSession) -> None:
    s = hier_session
    root = await _root(s)

    def pick(h: KicadHandle) -> tuple[Any, SheetRef]:
        sub = next(r for r in sheet_refs(h) if r.file == "ampli_ht.kicad_sch")
        sym = next(
            x for x in sheet_api(h, root).get_symbols() if not str(x.reference).startswith("#")
        )
        sym.value_field.text.value = "CHANGED"
        return sym, sub

    sym, sub = await s.call(pick)
    x, y = await s.call(lambda h: free_area(h, 10.16, 5.08, sub.path))
    label = local_label("TWO_SHEETS", (x, y))
    cs = _cs(root, static_stage("both", Update([sym]), Create([label], sheet=sub)))
    applied = await apply_change_set(edit_ctx(s), cs)
    assert applied.sheet_of[access.kiid(label)] == sub
    assert applied.sheet_of[access.kiid(sym)] == root

    def check(h: KicadHandle) -> tuple[str, list[str]]:
        root_sym = get_item(h, access.kiid(sym), root)
        labels = [str(lb.text.value) for lb in sheet_api(h, sub).get_labels()]
        return str(root_sym.value), labels

    value, labels = await s.call(check)
    assert value == "CHANGED" and "TWO_SHEETS" in labels


async def test_staging_error_drops_commit(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    before = await _state(s)
    root = await _root(s)

    def boom(_inp: StageInput) -> list[Any]:
        raise RuntimeError("build failed")

    with pytest.raises(RuntimeError, match="build failed"):
        await apply_change_set(edit_ctx(s), _cs(root, Stage("bad", boom)))
    with pytest.raises(StructuredToolError) as err:  # unknown UUID: refused before writing
        await apply_change_set(
            edit_ctx(s),
            _cs(root, static_stage("bad", Create([wire((10.16, 10.16), (20.32, 10.16))]),
                                   Delete(["00000000-0000-0000-0000-000000000001"]))),
        )  # fmt: skip
    assert err.value.code == "not_found"
    assert await _state(s) == before


async def test_cleanup_side_effects_recorded(
    ecc83_session: KicadSession, edit_db: Database
) -> None:
    s = ecc83_session
    x, y = await s.call(lambda h: free_area(h, 20.32, 10.16))
    t = (x + 10.16, y)
    left, right = wire((x, y), t), wire(t, (x + 20.32, y))
    branch = wire(t, (t[0], y + 7.62))
    j = Junction()
    j.position = v2(*t)
    j.proto.id.value = "7a0e3c8e-4c1d-4c55-9a43-6f5bb1e3a002"
    async with s.commit("T"):
        await s.call(lambda h: h.schematic().create_items([left, right, branch, j]))
    survivors = await s.call(item_ids)
    assert {access.kiid(left), access.kiid(right), access.kiid(j)} <= survivors
    ctx = edit_ctx(s, edit_db)
    cs = _cs(await _root(s), static_stage("delete branch", Delete([access.kiid(branch)])))
    applied = await apply_change_set(ctx, cs)
    side = {(e.op, e.kind, e.uuid) for e in applied.side_effects}
    assert ("delete", "junction", access.kiid(j)) in side
    merged = {e.uuid for e in applied.side_effects if e.kind == "wire"}
    assert merged & {access.kiid(left), access.kiid(right)}
    rows = await ctx.editing.changes.for_run(s.project.id, RUN_ID)  # type: ignore[union-attr]
    assert sum(e.side_effect for e in rows) == len(applied.side_effects)
    assert [(e.op, e.kind) for e in rows if not e.side_effect] == [("delete", "wire")]
    result = edit_result(cs, applied)
    assert result.data is not None
    assert {e["uuid"] for e in result.data["side_effects"]} >= {access.kiid(j)}


async def test_two_stage_change_set(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    await s.ensure_symbol_libraries()
    x, y = await s.call(lambda h: free_area(h, 7.62, 12.7))

    def set_value(inp: StageInput) -> list[Any]:
        sym = inp.placed[0]
        sym.value_field.text.value = "22k"
        return [Update([sym])]

    cs = _cs(
        await _root(s),
        static_stage("place", Place("Device:R", (x + 2.54, y + 5.08), 0, 1, "R998")),
        Stage("fields", set_value),
    )
    applied = await apply_change_set(edit_ctx(s), cs)
    assert len(applied.placed) == 1 and cs.logical.placed_symbols == applied.placed
    sym = await s.call(lambda h: get_item(h, applied.placed[0]))
    assert str(sym.reference) == "R998" and str(sym.value) == "22k"


async def test_subsheet_stage_uses_for_sheet(hier_session: KicadSession) -> None:
    s = hier_session

    def pick(h: KicadHandle) -> tuple[Any, SheetRef]:
        sub = next(r for r in sheet_refs(h) if r.file == "ampli_ht.kicad_sch")
        sym = next(x for x in sheet_api(h, sub).get_symbols() if str(x.reference).startswith("R2"))
        sym.value_field.text.value = "SUBSHEET"
        return sym, sub

    sym, sub = await s.call(pick)
    cs = _cs(sub, static_stage("value", Update([sym])))
    await apply_change_set(edit_ctx(s), cs)
    value = await s.call(lambda h: str(get_item(h, access.kiid(sym), sub).value))
    assert value == "SUBSHEET"


async def test_compensation_failure_raises_document_inconsistent(
    ecc83_session: KicadSession,
) -> None:
    s = ecc83_session

    def fail() -> None:
        raise RuntimeError("injected")

    w = wire((10.16, 10.16), (20.32, 10.16))
    cs = _cs(await _root(s), static_stage("wire", Create([w])))
    with pytest.raises(DocumentInconsistent):
        await apply_change_set(edit_ctx(s), cs, _verify=_forced, _before_compensate=fail)


async def test_resolve_sheet_for_page(hier_session: KicadSession) -> None:
    sheets = await hier_session.call(sheet_refs)
    page = sheets[1].page
    assert (await hier_session.call(lambda h: resolve_sheet(h, page))) == sheets[1]
