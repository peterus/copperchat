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
