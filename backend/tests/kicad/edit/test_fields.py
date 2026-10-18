"""sch_set_fields through the real tool adapter (E4 Task 3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.geometry import mm_point
from copperchat.library.prefs import seed_user_part_preferences
from tests.kicad.edit.helpers import item_ids
from tests.support.edit import call_tool, edit_ctx, get_item
from tests.support.kicad_cli import cli_components, cli_netlist

pytestmark = pytest.mark.kicad


async def _ok(ctx: Any, name: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, name, **args)
    assert not err, out
    return out


def _sym(uid: str) -> Any:
    def run(h: KicadHandle) -> Any:
        return get_item(h, uid)

    return run


async def test_value_footprint_dnp_round_trip(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    placed = await _ok(ctx, "sch_place_symbol", lib_id="Device:R", at=[76.2, 76.2])
    ref, uid = placed["placed"]["ref"], placed["placed"]["uuid"]
    out = await _ok(
        ctx,
        "sch_set_fields",
        ref=ref,
        fields={"value": "22k", "footprint": "Resistor_SMD:R_0805_2012Metric", "mpn": "RC0805"},
        dnp=True,
    )
    assert out["logical_changes"] == 1 and out["connectivity_verified"] is True
    assert out["changed"]["Value"] == {"before": "R", "after": "22k"}
    assert out["changed"]["dnp"] == {"before": False, "after": True}
    assert [m["uuid"] for m in out["modified"]] == [uid]
    s.jobs_stale = True
    await s.refresh_jobs()  # save + close + reopen: the values come from the file
    sym = await s.call(_sym(uid))
    assert str(sym.value) == "22k" and bool(sym.attributes.do_not_populate)
    assert access.field_text(sym.footprint_field) == "Resistor_SMD:R_0805_2012Metric"
    assert {str(f.name): str(f.text.value) for f in sym.user_fields}["MPN"] == "RC0805"
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    comp = cli_components(cli_netlist(s.settings.kicad_cli, dest, tmp_path))[ref]
    assert comp["value"] == "22k" and "dnp" in comp["properties"]  # type: ignore[operator]


async def test_all_units_updated(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    a = await _ok(ctx, "sch_place_symbol", lib_id="Amplifier_Operational:LM358", at=[76.2, 76.2])
    ref = a["placed"]["ref"]
    b = await _ok(
        ctx,
        "sch_place_symbol",
        lib_id="Amplifier_Operational:LM358",
        at=[127.0, 76.2],
        unit=2,
        reference=ref,
    )
    out = await _ok(ctx, "sch_set_fields", ref=ref, fields={"value": "TL072", "Supplier": "X"})
    assert out["units"] == 2 and out["logical_changes"] == 1
    for uid in (a["placed"]["uuid"], b["placed"]["uuid"]):
        sym = await s.call(_sym(uid))
        assert str(sym.value) == "TL072"
        assert {str(f.name): str(f.text.value) for f in sym.user_fields}["Supplier"] == "X"


async def test_custom_field_added_hidden_near_symbol(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    placed = await _ok(ctx, "sch_place_symbol", lib_id="Device:C", at=[76.2, 76.2])
    await _ok(ctx, "sch_set_fields", ref=placed["placed"]["ref"], fields={"Tolerance": "10%"})
    sym = await s.call(_sym(placed["placed"]["uuid"]))
    f = next(f for f in sym.user_fields if f.name == "Tolerance")
    assert str(f.text.value) == "10%" and not f.visible
    assert mm_point(f.text.position) == mm_point(sym.position)
    out, err = await call_tool(
        ctx, "sch_set_fields", ref=placed["placed"]["ref"], remove_fields=["Tolerance"]
    )
    assert not err, out
    sym = await s.call(_sym(placed["placed"]["uuid"]))
    assert all(f.name != "Tolerance" for f in sym.user_fields)


async def test_existing_field_position_kept(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    placed = await _ok(ctx, "sch_place_symbol", lib_id="Device:R", at=[76.2, 76.2])
    uid = placed["placed"]["uuid"]
    before = await s.call(_sym(uid))
    await _ok(ctx, "sch_set_fields", ref=placed["placed"]["ref"], fields={"value": "1M"})
    after = await s.call(_sym(uid))
    for name in ("reference_field", "value_field", "footprint_field"):
        assert mm_point(getattr(after, name).text.position) == mm_point(
            getattr(before, name).text.position
        )
        assert getattr(after, name).visible == getattr(before, name).visible


async def test_deviation_note(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    seed_user_part_preferences(s.settings)
    ctx = edit_ctx(s, edit_db)
    placed = await _ok(ctx, "sch_place_symbol", lib_id="Device:R", at=[76.2, 76.2])
    out = await _ok(
        ctx,
        "sch_set_fields",
        ref=placed["placed"]["ref"],
        fields={"footprint": "Resistor_SMD:R_1206_3216Metric"},
    )
    kinds = [n["kind"] for n in out["notes"] if isinstance(n, dict)]
    assert "preference_deviation" in kinds


async def test_unknown_ref_not_found(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    out, err = await call_tool(
        edit_ctx(s, edit_db), "sch_set_fields", ref="R77", fields={"value": "1k"}
    )
    assert err and out["error"]["code"] == "not_found"


async def test_netlist_unchanged(ecc83_session: KicadSession, edit_db: Database) -> None:
    s = ecc83_session
    await s.ensure_symbol_libraries()

    def state(h: KicadHandle) -> tuple[set[str], frozenset[Any]]:
        return item_ids(h), access.live_netlist(h).partition()

    ids, before = await s.call(state)
    out = await _ok(
        edit_ctx(s, edit_db), "sch_set_fields", ref="R1", fields={"value": "1.5k", "Note": "x"}
    )
    ids2, after = await s.call(state)
    assert after == before and ids2 == ids
    out, err = await call_tool(
        edit_ctx(s, edit_db), "sch_set_fields", ref="#PWR01", fields={"value": "VCC"}
    )
    assert err and out["error"]["code"] == "invalid_argument"
