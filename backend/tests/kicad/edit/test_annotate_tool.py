"""sch_annotate through the real tool adapter (E4 Task 4)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.annotate_tool import read_units
from copperchat.kicad_tools.edit.scene import sheet_api
from tests.kicad.edit.helpers import item_ids, v2
from tests.support.edit import call_tool, edit_ctx, kiids

pytestmark = pytest.mark.kicad


async def _raw_place(s: KicadSession, lib_id: str, points: list[tuple[float, float]]) -> list[str]:
    """Symbols placed through a plain API commit without a reference (KiCad leaves them
    unannotated, `R?`; power symbols get KiCad's own `#PWR..`)."""

    def place(h: KicadHandle) -> list[str]:
        sch = h.schematic()
        return [access.kiid(sch.place_symbol_from_library(lib_id, v2(*p))) for p in points]

    async with s.commit("raw place"):
        return await s.call(place)


async def _refs(s: KicadSession) -> dict[tuple[str, str], str]:
    """(sheet path, uuid) -> reference of every unit."""

    def run(h: KicadHandle) -> dict[tuple[str, str], str]:
        return {(u.sheet.path, u.uuid): u.ref for u in read_units(h)}

    return await s.call(run)


async def test_keep_assigns_only_unannotated(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    c1, err = await call_tool(ctx, "sch_place_symbol", lib_id="Device:C", at=[50.8, 101.6])
    assert not err, c1
    uids = await _raw_place(s, "Device:R", [(101.6, 50.8), (50.8, 50.8), (76.2, 50.8)])
    before = await _refs(s)
    assert sorted(before.values()) == ["C1", "R?", "R?", "R?"]
    out, err = await call_tool(ctx, "sch_annotate", scope="all", mode="keep")
    assert not err, out
    assert out["logical_changes"] == 3 and out["connectivity_verified"] is True
    after = await _refs(s)
    by_uuid = {u: r for (_p, u), r in after.items()}
    assert [by_uuid[u] for u in uids] == ["R3", "R1", "R2"]  # left to right
    assert by_uuid[c1["placed"]["uuid"]] == "C1"
    assert sorted(r["new"] for r in out["renamed"]) == ["R1", "R2", "R3"]
    again, err = await call_tool(ctx, "sch_annotate", scope="all", mode="keep")
    assert not err and again["renamed"] == [] and again["logical_changes"] == 0


async def test_renumber_hierarchy_per_sheet(hier_session: KicadSession) -> None:
    s = hier_session
    await s.ensure_symbol_libraries()

    def swap(h: KicadHandle) -> tuple[str, str]:
        units = [u for u in read_units(h) if not u.ref.startswith("#")]
        a = next(u for u in units if u.sheet.page == "2" and u.ref.startswith("R"))
        b = next(
            u for u in units if u.sheet.page == "3" and u.ref.startswith("R") and u.uuid != a.uuid
        )
        for u, ref in ((a, b.ref), (b, a.ref)):
            sch = sheet_api(h, u.sheet)
            sym = sch.get_items_by_id(kiids(u.uuid))[0]
            sym.reference = ref
            sym.proto.ClearField("definition")
            sch.update_items([sym])
        return a.ref, b.ref

    async with s.commit("swap two refs"):
        await s.call(swap)
    before = await s.call(lambda h: read_units(h))
    groups_before = _groups(before)
    ids = await s.call(item_ids)
    net_before = await s.call(lambda h: access.live_netlist(h).partition())
    out, err = await call_tool(edit_ctx(s), "sch_annotate", scope="all", mode="renumber")
    assert not err, out
    after = await s.call(lambda h: read_units(h))
    for u in after:
        if u.ref.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_]+)(\d+)$", u.ref)
        assert m is not None and int(m.group(2)) // 100 == int(u.sheet.page), (u.ref, u.sheet.page)
    assert _groups(after) == groups_before  # multi-unit parts keep one shared reference
    refs = {(u.sheet.path, u.ref, u.lib_id, u.unit) for u in after}
    assert len(refs) == len(after)  # no duplicate unit
    assert await s.call(item_ids) == ids
    assert await s.call(lambda h: access.live_netlist(h).partition()) == net_before
    assert out["logical_changes"] == len({(r["sheet"], r["old"], r["new"]) for r in out["renamed"]})


def _groups(units: list[Any]) -> set[frozenset[tuple[str, str]]]:
    """Units grouped by (sheet, reference): the sets of (sheet, uuid) per component."""
    out: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for u in units:
        out.setdefault((u.sheet.path, u.ref), set()).add((u.sheet.path, u.uuid))
    return {frozenset(g) for g in out.values()}


async def test_renumber_counts_against_budget(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    await EditingSettingsRepo(edit_db).put(s.project.id, change_budget=5)
    await _raw_place(s, "Device:R", [(25.4 * i, 50.8) for i in range(1, 7)])
    before = await _refs(s)
    out, err = await call_tool(edit_ctx(s, edit_db), "sch_annotate", scope="all", mode="keep")
    assert err and out["error"]["code"] == "plan_required", out
    assert await _refs(s) == before


async def test_power_symbols_numbered(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    await _raw_place(s, "power:GND", [(50.8, 50.8), (76.2, 50.8), (101.6, 50.8)])
    refs = list((await _refs(s)).values())
    out, err = await call_tool(edit_ctx(s, edit_db), "sch_annotate", scope="all", mode="keep")
    assert not err, out
    after = list((await _refs(s)).values())
    assert len(set(after)) == 3 and all(re.match(r"^#PWR\d{2,}$", r) for r in after), (refs, after)
