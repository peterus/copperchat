"""ERC baseline/delta and the diff service on the real api-server (E8a Tasks 2 and 4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from copperchat.changes import erc as erc_mod
from copperchat.changes.erc import current_erc, erc_baseline, erc_delta
from copperchat.db import Database
from copperchat.editing.state import EditTurnState
from copperchat.history.service import HistoryService
from copperchat.history.store import Snapshot
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import checks
from copperchat.kicad_tools.edit.scene import resolve_sheet, sheet_api
from tests.kicad.edit.helpers import v2
from tests.support.edit import edit_ctx, place_raw

pytestmark = pytest.mark.kicad


class OneSession:
    """The fixture's session as the 'current' one of a HistoryService."""

    def __init__(self, session: KicadSession) -> None:
        self.session = session

    def current(self) -> KicadSession:
        return self.session

    def add_save_listener(self, fn: Any) -> None:
        return None


async def _no_preview(session: Any, doc: Any) -> None:
    return None


def history(session: KicadSession, db: Database) -> HistoryService:
    svc = HistoryService(
        db=db,
        sessions=OneSession(session),  # type: ignore[arg-type]
        broadcaster=None,
        runtime=None,
    )
    svc.render_preview = _no_preview  # type: ignore[assignment]
    return svc


async def _snapshot(svc: HistoryService, session: KicadSession, run_id: str) -> Snapshot:
    return await svc.store(session.project).snapshot(run_id, "turn")


def _uuids(v: dict[str, Any]) -> set[str]:
    return {i["uuid"] for i in v["items"]}


async def _pin_ids(session: KicadSession, symbol_uuid: str) -> set[str]:
    """ERC names a pin by the pin's own KIID (E-C2: the placed symbol's definition)."""

    def run(h: KicadHandle) -> set[str]:
        sym = next(x for x in h.schematic().get_symbols() if str(x.id.value) == symbol_uuid)
        return {str(p.id.value) for p in sym.definition.pins}

    return await session.call(run)


async def test_baseline_from_snapshot_files_cached(
    blank_session: KicadSession, edit_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    r1 = await place_raw(s, "Device:R", (50.8, 50.8), "R1")
    await s.save("sch")
    svc = history(s, edit_db)
    snap = await _snapshot(svc, s, "run-b")
    base = await erc_baseline(svc, s.project, snap)
    unconnected = [v for v in base if v["type"] == "pin_not_connected"]
    pins = await _pin_ids(s, r1)
    assert len(unconnected) == 2 and all(_uuids(v) <= pins for v in unconnected), base
    # The copy carried the project file and the library tables: no library noise.
    assert not {v["type"] for v in base} & {"lib_symbol_issues", "lib_symbol_mismatch"}
    cache = svc.store(s.project).snapshot_dir(snap.id) / "erc.json"
    assert json.loads(cache.read_text()) == base

    async def boom(*_a: Any, **_k: Any) -> str:
        raise AssertionError("baseline must come from the cache")

    monkeypatch.setattr(erc_mod, "run_kicad_check_on_dir", boom)
    assert await erc_baseline(svc, s.project, snap) == base


async def test_delta_after_edit_reports_unconnected_pin(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    svc = history(s, edit_db)
    snap = await _snapshot(svc, s, "run-d")  # the blank sheet
    r1 = await place_raw(s, "Device:R", (50.8, 50.8), "R1")
    state = EditTurnState(call_seq=1, last_mutation_seq=1)
    delta = erc_delta(await erc_baseline(svc, s.project, snap), await current_erc(s, state))
    hits = [e for e in delta.new_errors if e["type"] == "pin_not_connected"]
    pins = await _pin_ids(s, r1)
    assert len(hits) == 2 and set().union(*(_uuids(e) for e in hits)) == pins, delta
    assert delta.resolved == []
    assert state.last_erc is not None  # remembered for the turn end (no double runs)


def _add_sheet(h: KicadHandle, project_dir: Path, name: str, filename: str) -> str:
    """Spike 2A Q7 workaround: create with an absolute Sheetfile, then set it relative."""
    from kipy.schematic_types import SheetSymbol

    sch = h.schematic()
    sheet = SheetSymbol()
    sheet.name_field.name = "Sheetname"
    sheet.name_field.text.value = name
    sheet.name_field.text.position = v2(25.4, 149.86)
    sheet.filename_field.name = "Sheetfile"
    sheet.filename_field.text.value = str(project_dir / filename)
    sheet.filename_field.text.position = v2(25.4, 175.26)
    sheet.position = v2(25.4, 152.4)
    sheet.size = v2(25.4, 20.32)
    commit = sch.begin_commit()
    created = sch.create_items([sheet])[0]
    sch.push_commit(commit, "add sheet")
    from tests.support.edit import kiids

    cur = sch.get_items_by_id(kiids(str(created.id.value)))[0]
    cur.filename_field.text.value = filename
    commit = sch.begin_commit()
    sch.update_items([cur])
    sch.push_commit(commit, "relative sheet file")
    return str(created.id.value)


async def test_erc_freshness_subsheet_and_new_sheet(
    hier_session: KicadSession, edit_db: Database
) -> None:
    """Spec §12: an edit on a sub-sheet and a new sheet are both in the next run_erc."""
    s = hier_session
    await s.ensure_symbol_libraries()
    sub_r = await place_raw(s, "Device:R", (25.4, 25.4), "R299", sheet="ampli_ht_vertical")
    await s.call(lambda h: _add_sheet(h, s.project.dir, "Extra", "extra.kicad_sch"))

    def place_on_new(h: KicadHandle) -> str:
        target = resolve_sheet(h, "Extra")
        sym = sheet_api(h, target).place_symbol_from_library(
            "Device:R", v2(50.8, 50.8), reference="R401"
        )
        return str(sym.id.value)

    async with s.commit("place on new sheet"):
        new_r = await s.call(place_on_new)
    ctx = edit_ctx(s, edit_db)
    ctx.editing.turn(s.project.id, ctx.run_id).last_mutation_seq = 0  # type: ignore[union-attr]
    res = await checks.run_erc(ctx, {"type": "pin_not_connected"})
    assert res.data is not None
    seen = set().union(*(_uuids(v) for v in res.data["violations"]))
    assert await _pin_ids(s, sub_r) <= seen, res.data  # the edited sub-sheet
    assert await _pin_ids(s, new_r) <= seen, res.data  # the new sheet
    state = ctx.editing.turn(s.project.id, ctx.run_id)  # type: ignore[union-attr]
    assert state.last_erc is not None and state.last_erc_seq == state.call_seq


# -- diff (Task 4) -----------------------------------------------------------------


def _l_route(a: list[float], b: list[float]) -> list[list[float]]:
    mid = a[1] + 5.08
    return [list(a), [a[0], mid], [b[0], mid], list(b)]


async def _value_update(s: KicadSession, ctx: Any, sheet_file: str | None = None) -> str:
    """A raw value edit through apply_change_set (logged like any edit tool's)."""
    from copperchat.kicad_tools.edit.changeset import (
        ChangeSet,
        Logical,
        NetIntent,
        Update,
        static_stage,
    )
    from copperchat.kicad_tools.edit.engine import apply_change_set
    from copperchat.kicad_tools.edit.scene import sheet_refs

    def pick(h: KicadHandle) -> tuple[Any, Any]:
        refs = sheet_refs(h)
        sheet = next(r for r in refs if r.file == sheet_file) if sheet_file else refs[0]
        sym = next(x for x in sheet_api(h, sheet).get_symbols() if str(x.reference).startswith("R"))
        sym.value_field.text.value = "4k7-E8A"
        sym.proto.ClearField("definition")
        return sym, sheet

    sym, sheet = await s.call(pick)
    cs = ChangeSet(
        tool="sch_set_fields",
        sheet=sheet,
        stages=[static_stage("value", Update([sym], sheet=sheet))],
        pre_images={},
        intent=NetIntent(keep_partition=True),
        logical=Logical(1),
    )
    await apply_change_set(ctx, cs)
    return str(sym.id.value)


async def test_diff_cross_checks_change_log(ecc83_session: KicadSession, edit_db: Database) -> None:
    from copperchat.changes.diff import diff_against_snapshot
    from tests.support.edit import RUN_ID, call_tool, two_resistors

    s = ecc83_session
    await two_resistors(s)  # not logged (no editing service): part of the baseline
    await s.save("sch")
    svc = history(s, edit_db)
    snap = await _snapshot(svc, s, RUN_ID)
    ctx = edit_ctx(s, edit_db)
    pa, _ = await call_tool(ctx, "sch_get_pin", pin="R901.2")
    pb, _ = await call_tool(ctx, "sch_get_pin", pin="R902.2")
    out, is_error = await call_tool(ctx, "sch_add_wire", points=_l_route(pa["at_mm"], pb["at_mm"]))
    assert not is_error, out
    value_uuid = await _value_update(s, ctx)
    log = await ctx.editing.changes.since(s.project.id, snap.created_at)  # type: ignore[union-attr]
    own = [e for e in log if not e.side_effect]
    assert {e.uuid for e in own} >= {value_uuid, *(c["uuid"] for c in out["created"])}
    diff = await diff_against_snapshot(svc, s.project, snap, log)
    entries = [e for es in diff["sheets"].values() for e in es]
    by_uuid: dict[str, set[str]] = {}
    for e in entries:
        by_uuid.setdefault(e["uuid"], set()).add(e["kind"])
    kinds = {"add": "added", "modify": "modified", "delete": "removed"}
    for e in own:
        assert kinds[e.op] in by_uuid.get(e.uuid, set()), (e, entries)
    logged = {e.uuid for e in own}
    for e in entries:
        assert e["side_effect"] is (
            e["kind"] not in {kinds[x.op] for x in own if x.uuid == e["uuid"]}
        )
        if e["uuid"] not in logged:
            assert e["side_effect"] is True
    value = next(e for e in entries if e["uuid"] == value_uuid)
    assert value["kind"] == "modified" and value["box_mm"] is not None
    assert any(p["name"] == "Value" for p in value["properties"])
    added = next(e for e in entries if e["kind"] == "added")
    assert all(set(p) == {"name"} for p in added["properties"])


async def test_diff_exit_code_5_is_success(ecc83_session: KicadSession, edit_db: Database) -> None:
    from copperchat.changes.diff import diff_against_snapshot, run_sch_diff

    s = ecc83_session
    svc = history(s, edit_db)
    snap = await _snapshot(svc, s, "run-x")
    same = await diff_against_snapshot(svc, s.project, snap)
    assert same["count"] == 0  # exit 0: identical (a save rewrites nothing the differ sees)
    await _value_update(s, edit_ctx(s))
    raw = await run_sch_diff(svc, s.project, snap)  # exit 5 = differences, not a failure
    assert raw["changes"]
    diff = await diff_against_snapshot(svc, s.project, snap)
    assert diff["count"] == 1
    [entry] = [e for es in diff["sheets"].values() for e in es]
    assert entry["side_effect"] is False  # no log given: nothing is marked


async def test_diff_hierarchy_per_instance(hier_session: KicadSession, edit_db: Database) -> None:
    from copperchat.changes.diff import diff_against_snapshot
    from copperchat.kicad_tools.edit.scene import sheet_refs

    s = hier_session
    svc = history(s, edit_db)
    snap = await _snapshot(svc, s, "run-h")
    uid = await _value_update(s, edit_ctx(s), "ampli_ht.kicad_sch")
    diff = await diff_against_snapshot(svc, s.project, snap)
    hits = [(p, e) for p, es in diff["sheets"].items() for e in es if e["uuid"] == uid]
    instances = await s.call(
        lambda h: {r.path for r in sheet_refs(h) if r.file == "ampli_ht.kicad_sch"}
    )
    assert len(instances) == 2
    assert {p for p, _ in hits} == instances  # one entry per sheet instance path
    assert len({e["ref"] for _, e in hits}) == 2  # instance refs (R2xx / R3xx)
    assert all(e["id"] == f"{p}/{uid}" for p, e in hits)


async def test_sch_changes_tool_lists_turn(ecc83_session: KicadSession, edit_db: Database) -> None:
    from tests.support.edit import call_tool, two_resistors

    s = ecc83_session
    await two_resistors(s)
    ctx = edit_ctx(s, edit_db)
    pa, _ = await call_tool(ctx, "sch_get_pin", pin="R901.2")
    pb, _ = await call_tool(ctx, "sch_get_pin", pin="R902.2")
    out, is_error = await call_tool(ctx, "sch_add_wire", points=_l_route(pa["at_mm"], pb["at_mm"]))
    assert not is_error, out
    listed, is_error = await call_tool(ctx, "sch_changes")
    assert not is_error, listed
    own = [c for c in listed["changes"] if not c.get("side_effect")]
    assert {c["uuid"] for c in own} == {c["uuid"] for c in out["created"]}
    assert all(c["op"] == "add" and c["tool"] == "sch_add_wire" for c in own)
    assert listed["count"] == len(listed["changes"])
