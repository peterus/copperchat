"""sch_place_symbol through the real tool adapter (E4 Task 2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from copperchat.changes.repo import ChangeRepo
from copperchat.db import Database
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadSession, KicadUnavailable
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit import place as place_mod
from copperchat.kicad_tools.edit.fields import FieldSpot, spot_box
from copperchat.kicad_tools.edit.geometry import Box, mm_point, on_grid
from copperchat.library.prefs import seed_user_part_preferences
from tests.kicad.edit.helpers import item_ids
from tests.support.edit import RUN_ID, call_tool, edit_ctx, free_area, get_item
from tests.support.kicad_cli import cli_components, cli_netlist

pytestmark = pytest.mark.kicad


class Counter:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


async def _spot(
    s: KicadSession, w: float = 25.4, h: float = 20.32, sheet: str | None = None
) -> list[float]:
    x, y = await s.call(lambda hd: free_area(hd, w, h, sheet))
    return [x + w / 2, y + h / 2]


async def _place(ctx: Any, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, "sch_place_symbol", **args)
    assert not err, out
    return out


async def _cli(s: KicadSession, tmp_path: Path) -> dict[str, dict[str, Any]]:
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    return cli_components(cli_netlist(s.settings.kicad_cli, dest, tmp_path))


def _kinds(out: dict[str, Any]) -> list[str]:
    return [n["kind"] for n in out["notes"] if isinstance(n, dict)]


async def test_place_resistor_with_auto_ref_value_footprint(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    seed_user_part_preferences(s.settings)
    snapshots = Counter()
    ctx = edit_ctx(s, edit_db, on_mutation=snapshots)
    out = await _place(
        ctx,
        lib_id="Device:R",
        at=[101.6, 76.3],
        value="10k",
        footprint="Resistor_SMD:R_0805_2012Metric",
    )
    p = out["placed"]
    assert p["ref"] == "R1" and p["value"] == "10k"
    assert p["footprint"] == "Resistor_SMD:R_0805_2012Metric"
    assert p["at_mm"] == [101.6, 76.2] and on_grid(p["at_mm"])
    assert all(on_grid(pin["at_mm"]) for pin in p["pins"]) and len(p["pins"]) == 2
    assert out["logical_changes"] == 1 and out["budget"] == {"used": 1, "limit": 20}
    assert out["connectivity_verified"] is True
    assert [c["kind"] for c in out["created"]] == ["symbol"]
    assert snapshots.calls == 1
    assert "preference_deviation" not in _kinds(out)
    comps = await _cli(s, tmp_path)
    assert comps["R1"]["value"] == "10k"
    assert comps["R1"]["footprint"] == "Resistor_SMD:R_0805_2012Metric"


async def test_auto_ref_per_sheet_block(hier_session: KicadSession, edit_db: Database) -> None:
    s = hier_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    at = await _spot(s, sheet="2")
    out = await _place(ctx, lib_id="Device:R", at=at, sheet="2")
    ref = out["placed"]["ref"]
    assert ref.startswith("R2") and len(ref) == 4, ref
    assert out["placed"]["page"] == "2"


async def test_place_mirrored_rotated_canonical(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    plain = await _place(ctx, lib_id="Device:D", at=[50.8, 50.8])
    odd = await _place(ctx, lib_id="Device:D", at=[101.6, 50.8], rotation=180, mirror="x")
    p = odd["placed"]
    assert (p["rotation"], p["mirror"]) == (0, "y")  # 180 + x is stored as 0 + y (2A Q1)
    base = {q["pin"]: q["at_mm"] for q in plain["placed"]["pins"]}
    for q in p["pins"]:  # mirrored left-right around the origin
        bx, by = base[q["pin"]]
        assert q["at_mm"] == [round(101.6 - (bx - 50.8), 4), round(by, 4)]
        got, err = await call_tool(ctx, "sch_get_pin", pin=f"{p['ref']}.{q['pin']}")
        assert not err and got["at_mm"] == q["at_mm"]

    def transform(h: KicadHandle) -> tuple[int, bool, bool]:
        sym = get_item(h, p["uuid"])
        tr = sym.transform
        return int(tr.orientation), bool(tr.mirror_x), bool(tr.mirror_y)

    assert await s.call(transform) == (1, False, True)  # SSO_0 + mirror_y


async def test_overlap_refused_with_nearest_free_position(
    ecc83_session: KicadSession, edit_db: Database
) -> None:
    s = ecc83_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    at = await _spot(s)
    await _place(ctx, lib_id="Device:R", at=at)
    snapshots = Counter()
    ctx = edit_ctx(s, edit_db, on_mutation=snapshots)
    before = await s.call(item_ids)
    out, err = await call_tool(ctx, "sch_place_symbol", lib_id="Device:R", at=at)
    assert err and out["error"]["code"] == "overlap", out
    suggested = out["error"]["details"]["suggested_at_mm"]
    assert suggested is not None and suggested != at
    assert await s.call(item_ids) == before and snapshots.calls == 0
    found, err = await call_tool(ctx, "sch_find_space", lib_id="Device:R", near=suggested)
    assert not err, found
    assert found["candidates"][0]["at_mm"] == suggested  # sch_find_space accepts it too
    await _place(ctx, lib_id="Device:R", at=suggested)


async def test_power_lib_id_refused(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    out, err = await call_tool(
        edit_ctx(s, edit_db), "sch_place_symbol", lib_id="power:GND", at=[50.8, 50.8]
    )
    assert err and out["error"]["code"] == "invalid_argument"
    assert "sch_add_power" in out["error"]["suggestion"]


async def test_unknown_footprint_refused(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    before = await s.call(item_ids)
    out, err = await call_tool(
        edit_ctx(s, edit_db),
        "sch_place_symbol",
        lib_id="Device:R",
        at=[50.8, 50.8],
        footprint="Resistor_SMD:R_0805_2012Metrik",
    )
    assert err and out["error"]["code"] == "not_found", out
    assert "Resistor_SMD:R_0805_2012Metric" in out["error"]["details"]["closest"]
    assert await s.call(item_ids) == before


async def test_footprint_unverified_without_board(
    blank_nopcb_session: KicadSession, edit_db: Database
) -> None:
    s = blank_nopcb_session
    await s.ensure_symbol_libraries()
    seed_user_part_preferences(s.settings)
    out = await _place(
        edit_ctx(s, edit_db),
        lib_id="Device:R",
        at=[50.8, 50.8],
        footprint="Resistor_SMD:R_0603_1608Metric",
    )
    kinds = _kinds(out)
    assert "footprint_unverified" in kinds and "preference_deviation" in kinds
    assert out["placed"]["footprint"] == "Resistor_SMD:R_0603_1608Metric"


async def test_preference_deviation_noted(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    seed_user_part_preferences(s.settings)
    out = await _place(
        edit_ctx(s, edit_db),
        lib_id="Device:R",
        at=[50.8, 50.8],
        footprint="Resistor_SMD:R_0603_1608Metric",
    )
    note = next(
        n for n in out["notes"] if isinstance(n, dict) and n["kind"] == "preference_deviation"
    )
    assert note["expected"] == "0805" and note["actual"] == "0603"
    assert note["suggested_footprint"] == "Resistor_SMD:R_0805_2012Metric"
    assert "footprint_unverified" not in _kinds(out)


async def test_active_part_without_notes_noted(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    out = await _place(edit_ctx(s, edit_db), lib_id="Amplifier_Operational:LM358", at=[76.2, 76.2])
    note = next(
        n for n in out["notes"] if isinstance(n, dict) and n["kind"] == "active_part_without_notes"
    )
    assert note["ref"] == out["placed"]["ref"] == "U1" and note["reason"] == "no_mpn"


async def test_multi_unit_sets_fields_on_all_units(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    a = await _place(ctx, lib_id="Amplifier_Operational:LM358", at=[76.2, 76.2])
    ref = a["placed"]["ref"]
    b = await _place(
        ctx,
        lib_id="Amplifier_Operational:LM358",
        at=[127.0, 76.2],
        unit=2,
        reference=ref,
        value="LM358A",
        fields={"MPN": "LM358ADR"},
    )
    assert b["placed"]["ref"] == ref and b["placed"]["unit"] == 2
    assert [m["uuid"] for m in b["modified"]] == [a["placed"]["uuid"]]

    def values(h: KicadHandle) -> list[tuple[str, str, str]]:
        out = []
        for uid in (a["placed"]["uuid"], b["placed"]["uuid"]):
            sym = get_item(h, uid)
            mpn = next((str(f.text.value) for f in sym.user_fields if f.name == "MPN"), "")
            out.append((str(sym.reference), str(sym.value), mpn))
        return out

    assert await s.call(values) == [(ref, "LM358A", "LM358ADR")] * 2
    again, err = await call_tool(
        ctx,
        "sch_place_symbol",
        lib_id="Amplifier_Operational:LM358",
        at=[127.0, 127.0],
        unit=2,
        reference=ref,
    )
    assert err and out_code(again) == "invalid_argument"  # unit 2 of U1 exists
    comps = await _cli(s, tmp_path)
    assert comps[ref]["value"] == "LM358A"


def out_code(out: dict[str, Any]) -> str:
    return str(out["error"]["code"])


@pytest.mark.parametrize("rotation", [0, 90])
async def test_fields_readable(
    blank_session: KicadSession, edit_db: Database, rotation: int
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    out = await _place(edit_ctx(s, edit_db), lib_id="Device:R", at=[76.2, 76.2], rotation=rotation)
    uid = out["placed"]["uuid"]

    def read(h: KicadHandle) -> tuple[list[tuple[Box, float, bool]], Box]:
        from copperchat.kicad_tools.edit.geometry import body_box

        sym = get_item(h, uid)
        boxes = []
        for f in (sym.reference_field, sym.value_field):
            spot = FieldSpot(mm_point(f.text.position), float(f.text.attributes.angle))
            boxes.append((spot_box(spot, str(f.text.value)), spot.angle, bool(f.visible)))
        return boxes, body_box(sym, margin=0.0)

    fields, body = await s.call(read)
    for box, angle, visible in fields:
        assert visible and not box.overlaps(body), (box, body)
        assert angle == (90.0 if rotation == 90 else 0.0)
    assert not fields[0][0].overlaps(fields[1][0])


async def test_shared_sheet_gets_unique_refs(
    hier_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = hier_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    at = await _spot(s, sheet="2")
    out = await _place(ctx, lib_id="Device:R", at=at, sheet="2", value="4k7")
    inst = {i["sheet"]: i["ref"] for i in out["placed"]["instances"]}
    assert len(inst) == 2 and len(set(inst.values())) == 2
    refs = sorted(inst.values())
    assert refs[0].startswith("R2") and refs[1].startswith("R3"), refs
    comps = await _cli(s, tmp_path)
    assert comps[refs[0]]["value"] == comps[refs[1]]["value"] == "4k7"
    rows = await ChangeRepo(edit_db).for_run(s.project.id, RUN_ID)
    # The placement first, then the other instance's reference (written in that order).
    assert [(r.op, r.uuid) for r in rows] == [
        ("add", out["placed"]["uuid"]),
        ("modify", out["placed"]["uuid"]),
    ]


async def test_shared_sheet_failure_removes_placement_and_its_rows(
    hier_session: KicadSession, edit_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other instances' references fail after the save + reload: the placement is
    undone and the change log keeps no 'add' row for the removed symbol."""
    s = hier_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    at = await _spot(s, sheet="2")
    before = await s.call(item_ids)
    reloads = Counter()
    real_refresh = s.refresh_jobs

    async def refresh() -> None:
        await reloads()
        await real_refresh()

    async def failing(*_: Any, **__: Any) -> Any:
        raise StructuredToolError(
            "references failed", "injected", "retry", code="verification_failed"
        )

    monkeypatch.setattr(s, "refresh_jobs", refresh)
    monkeypatch.setattr(place_mod, "apply_change_set", failing)  # the third change set
    out, err = await call_tool(ctx, "sch_place_symbol", lib_id="Device:R", at=at, sheet="2")
    assert err and out_code(out) == "verification_failed", out
    assert reloads.calls == 1  # it failed after refresh_jobs
    assert await s.call(item_ids) == before  # the placement is removed
    assert await ChangeRepo(edit_db).for_run(s.project.id, RUN_ID) == []
    await s.restart()  # #6: the last save (refresh_jobs saved the placement) is undone too
    assert await s.call(item_ids) == before


async def test_shared_sheet_inconsistent_references_are_never_saved(
    hier_session: KicadSession, edit_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The references change set itself ends DocumentInconsistent: the in-memory
    document is not trustworthy, so it is not saved over the last save; the turn is
    told that save may still hold part of the placement."""
    from copperchat.errors import DocumentInconsistent

    s = hier_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    at = await _spot(s, sheet="2")
    seen: list[Exception] = []
    ctx.on_document_inconsistent = seen.append
    saves: list[str] = []
    real_save = s.save

    async def broken(*_: Any, **__: Any) -> Any:
        async def recording(doc: Any) -> None:
            saves.append(doc)
            await real_save(doc)

        monkeypatch.setattr(s, "save", recording)
        raise DocumentInconsistent("forced: the undo diverged")

    monkeypatch.setattr(place_mod, "apply_change_set", broken)
    out, err = await call_tool(ctx, "sch_place_symbol", lib_id="Device:R", at=at, sheet="2")
    assert err and out_code(out) == "document_inconsistent", out
    assert saves == []
    assert len(seen) == 1 and getattr(seen[0], "last_save_partial", False)


async def test_shared_sheet_failure_after_a_restart_undoes_the_saved_placement(
    hier_session: KicadSession, edit_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#6: KiCad restarts after refresh_jobs saved the placement: the reloaded document
    holds it, so the undo runs there and is saved - the last save is again the state
    before this edit, and the call fails normally (no document_inconsistent)."""
    s = hier_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    at = await _spot(s, sheet="2")
    before = await s.call(item_ids)

    async def crashed(*_: Any, **__: Any) -> Any:
        await s.restart()
        raise KicadUnavailable("the api-server died")

    monkeypatch.setattr(place_mod, "apply_change_set", crashed)  # the third change set
    out, err = await call_tool(ctx, "sch_place_symbol", lib_id="Device:R", at=at, sheet="2")
    assert err and out["error"].get("code") != "document_inconsistent", out
    assert "stopped responding" in out["error"]["what"]
    assert await s.call(item_ids) == before
    await s.restart()
    assert await s.call(item_ids) == before


async def test_new_pins_do_not_change_existing_nets(
    ecc83_session: KicadSession, edit_db: Database
) -> None:
    s = ecc83_session
    await s.ensure_symbol_libraries()

    def partition(h: KicadHandle) -> frozenset[frozenset[Any]]:
        return access.live_netlist(h).partition()

    before = await s.call(partition)
    old = {k for g in before for k in g}
    at = await _spot(s)
    out = await _place(edit_ctx(s, edit_db), lib_id="Device:C", at=at)
    after = await s.call(partition)
    restricted = {frozenset(k for k in g if k in old) for g in after} - {frozenset()}
    assert restricted == set(before)
    new = {k for g in after for k in g} - old
    assert {k[1] for k in new} <= {out["placed"]["uuid"]}


async def test_unused_reference_refused(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    out, err = await call_tool(
        edit_ctx(s, edit_db), "sch_place_symbol", lib_id="Device:R", at=[50.8, 50.8], reference="C5"
    )
    assert err and out["error"]["code"] == "invalid_argument", out
    assert "R1" in out["error"]["suggestion"]
