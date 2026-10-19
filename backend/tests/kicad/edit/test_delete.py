"""sch_delete through the real tool adapter (E6 Task 2): attached stubs, power symbols
and labels, T-branches, sheets, budget and compensation (owner decision 4)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from copperchat.agent.engine import TurnOutcome
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.errors import DocumentInconsistent, StructuredToolError
from copperchat.history.service import HistoryService
from copperchat.kicad_session import KicadHandle, KicadSession, KicadSessionManager
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.engine import apply_change_set
from copperchat.kicad_tools.edit.geometry import same_point
from copperchat.kicad_tools.edit.scene import read_scene, resolve_sheet
from copperchat.kicad_tools.edit.specs_transform import prepare_delete
from tests.kicad.edit.connect_support import pin_nets, state
from tests.kicad.test_history_real import Real, turn
from tests.support.edit import call_tool, edit_ctx, get_item, place_direct
from tests.support.history import ECC83, FakeEngine, FakeRuntime, RecordingBroadcaster
from tests.support.kicad_cli import cli_netlist, cli_pin_nets
from tests.support.kicad_project import real_settings
from tests.support.conversations import ensure_test_conversation

pytestmark = pytest.mark.kicad


async def _ok(ctx: Any, tool: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, tool, **args)
    assert not err, out
    return out


async def _place(ctx: Any, lib_id: str, at: list[float], rotation: int = 0) -> str:
    out = await _ok(ctx, "sch_place_symbol", lib_id=lib_id, at=at, rotation=rotation)
    return str(out["placed"]["ref"])


async def _pin_at(ctx: Any, pin: str) -> list[float]:
    return list((await _ok(ctx, "sch_get_pin", pin=pin))["at_mm"])


def _forced(*_a: Any, **_k: Any) -> list[str]:
    return ["forced mismatch (test)"]


async def _scene(s: KicadSession, sheet: str | None = None) -> Any:
    return await s.call(lambda h: read_scene(h, resolve_sheet(h, sheet), boxes=False))


async def _transistor_circuit(ctx: Any) -> dict[str, Any]:
    """Q1 with +5V on C and a label SIG on B (both only on Q1), E wired to R2.1, which
    is also wired to R3.1."""
    q = await _place(ctx, "Device:Q_NPN", [101.6, 76.2])
    r2 = await _place(ctx, "Device:R", [127.0, 101.6])
    r3 = await _place(ctx, "Device:R", [152.4, 101.6])
    await _ok(ctx, "sch_add_power", net="+5V", at=[f"{q}.C"])
    await _ok(ctx, "sch_add_label", net="SIG", at=[f"{q}.B"])
    await _ok(ctx, "sch_connect", **{"from": f"{q}.E", "to": f"{r2}.1"})
    await _ok(ctx, "sch_connect", **{"from": f"{r2}.1", "to": f"{r3}.1"})
    pins = {n: await _pin_at(ctx, f"{q}.{n}") for n in ("B", "C", "E")}
    return {"q": q, "r2": r2, "r3": r3, "pins": pins}


async def test_delete_with_attached_stubs_power_labels(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    c = await _transistor_circuit(ctx)
    out = await _ok(ctx, "sch_delete", items=[c["q"]])
    assert out["connectivity_verified"] is True
    summary = out["deleted_summary"]
    assert summary["symbols"] == [c["q"]]
    assert summary["attached"]["power_symbols"] == 1
    assert summary["attached"]["labels"] == 1
    assert summary["attached"]["wires"] >= 2
    sc = await _scene(s)
    assert not any(sym.ref == c["q"] for sym in sc.symbols.values())
    assert not any(sym.power for sym in sc.symbols.values())  # +5V went with Q1
    assert not any(lb.text == "SIG" for lb in sc.labels.values())
    for at in c["pins"].values():  # nothing dangles where Q1's pins were
        assert not any(same_point(at, w.start) or same_point(at, w.end) for w in sc.wires.values())
    nets = await pin_nets(s)
    assert nets[f"{c['r2']}.1"] == nets[f"{c['r3']}.1"]  # the shared wire stays
    # kicad-cli on a saved copy agrees
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    cli = cli_pin_nets(cli_netlist(s.settings.kicad_cli, dest, tmp_path))
    assert cli[(c["r2"], "1")] == cli[(c["r3"], "1")]
    assert not any(ref == c["q"] for ref, _pin in cli)
    rows = await ctx.editing.changes.for_run(s.project.id, ctx.run_id)  # type: ignore[union-attr]
    mine = [r for r in rows if r.tool == "sch_delete"]
    assert {r.op for r in mine} <= {"delete", "modify"}
    assert any(r.op == "delete" and r.kind == "symbol" and r.ref == c["q"] for r in mine)
    assert any(r.op == "delete" and r.kind == "label" and not r.side_effect for r in mine)


async def test_delete_power_symbol_by_ref(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _place(ctx, "Device:R", [101.6, 76.2])
    added = await _ok(ctx, "sch_add_power", net="GND", at=[f"{r}.2"])
    pwr = added["placed"][0]["reference"]
    pin = await _pin_at(ctx, f"{r}.2")
    out = await _ok(ctx, "sch_delete", items=[pwr])
    assert out["deleted_summary"]["power_symbols"] == [pwr]
    assert out["logical_changes"] == 1
    sc = await _scene(s)
    assert not any(sym.power for sym in sc.symbols.values())
    assert not sc.wires  # its stub went with it
    assert not any(same_point(pin, w.start) for w in sc.wires.values())
    data = await _ok(ctx, "sch_get_pin", pin=f"{r}.2")
    assert data["free"] is True


async def test_nets_changed_reported(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r1 = await _place(ctx, "Device:R", [101.6, 76.2], 90)
    r2 = await _place(ctx, "Device:R", [127.0, 76.2], 90)
    await _ok(ctx, "sch_add_label", net="MID", at=[f"{r1}.2"])
    await _ok(ctx, "sch_connect", **{"from": f"{r1}.2", "to": f"{r2}.1"})
    nets = await pin_nets(s)
    assert nets[f"{r1}.2"] == nets[f"{r2}.1"] == "/MID"
    out = await _ok(ctx, "sch_delete", items=[r2])
    changed = {n["net"]: n for n in out["nets_changed"]}
    assert "/MID" in changed
    assert f"{r2}.1" in changed["/MID"]["before"] and f"{r1}.2" in changed["/MID"]["before"]
    assert changed["/MID"]["after"] == [{"net": "/MID", "pins": [f"{r1}.2"]}]
    sc = await _scene(s)
    assert any(lb.text == "MID" for lb in sc.labels.values())  # R1's label stays


async def test_cleanup_side_effects_reported(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """R3 T-joins the wire R1-R2; deleting R3 removes its branch, KiCad drops the
    junction (and merges the split segments) — reported as side effects."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r1 = await _place(ctx, "Device:R", [101.6, 76.2], 90)
    r2 = await _place(ctx, "Device:R", [127.0, 76.2], 90)
    r3 = await _place(ctx, "Device:R", [114.3, 88.9])
    a, b = await _pin_at(ctx, f"{r1}.2"), await _pin_at(ctx, f"{r2}.1")
    if a[0] > b[0]:
        a = await _pin_at(ctx, f"{r1}.1")
    await _ok(ctx, "sch_add_wire", points=[a, b])
    top = await _pin_at(ctx, f"{r3}.1")
    await _ok(ctx, "sch_add_wire", points=[top, [top[0], a[1]]])
    sc = await _scene(s)
    assert len(sc.junctions) == 1
    out = await _ok(ctx, "sch_delete", items=[r3])
    kinds = {(e["kind"], e["op"]) for e in out["side_effects"]}
    assert ("junction", "delete") in kinds
    sc = await _scene(s)
    assert not sc.junctions
    assert not any(same_point(top, w.start) or same_point(top, w.end) for w in sc.wires.values())
    nets = await pin_nets(s)
    r1_pin = f"{r1}.2" if a == await _pin_at(ctx, f"{r1}.2") else f"{r1}.1"
    assert nets[r1_pin] == nets[f"{r2}.1"]


async def test_delete_sheet_symbol_keeps_file(
    hier_session: KicadSession, edit_db: Database
) -> None:
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    file = s.project.dir / "ampli_ht.kicad_sch"
    before = await state(s)
    # its parts leave the design: more than 3 pre-existing symbols need a plan
    out, err = await call_tool(ctx, "sch_delete", items=["sheet:ampli_ht_horizontal"])
    assert err and out["error"]["code"] == "plan_required"
    assert await state(s) == before
    await EditingSettingsRepo(edit_db).put(s.project.id, plan_policy="never")
    out = await _ok(ctx, "sch_delete", items=["sheet:ampli_ht_horizontal"])
    assert out["deleted_summary"]["sheets"] == [
        {"name": "ampli_ht_horizontal", "file": "ampli_ht.kicad_sch"}
    ]
    assert out["logical_changes"] == 1
    assert any("stays on disk" in n for n in out["notes"] if isinstance(n, str))
    names = await s.call(lambda h: [str(i.name) for i in h.schematic().get_hierarchy()])
    assert "ampli_ht_horizontal" not in names and "ampli_ht_vertical" in names
    await s.save("sch")
    assert file.is_file()


async def test_delete_unknown_and_refusals_change_nothing(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    await _place(ctx, "Device:R", [101.6, 76.2])
    before = await state(s)
    out, err = await call_tool(ctx, "sch_delete", items=["R9"])
    assert err and out["error"]["code"] == "not_found"
    out, err = await call_tool(ctx, "sch_delete", items=["sheet:nope"])
    assert err and out["error"]["code"] == "not_found"
    assert await state(s) == before


async def test_budget_counts_preexisting_only(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    setup = edit_ctx(s, edit_db, run_id="run-setup")
    refs = [await _place(setup, "Device:R", [76.2 + 25.4 * i, 76.2]) for i in range(5)]
    await _ok(setup, "sch_add_power", net="+5V", at=[f"{refs[0]}.1"])
    await _ok(setup, "sch_add_power", net="GND", at=[f"{refs[0]}.2"])
    ctx = edit_ctx(s, edit_db)  # a new turn: all five are pre-existing
    own = await _place(ctx, "Device:R", [76.2, 127.0])
    out = await _ok(ctx, "sch_delete", items=[refs[0]])  # + its two power symbols: 1
    assert out["deleted_summary"]["attached"]["power_symbols"] == 2
    await _ok(ctx, "sch_delete", items=[refs[1]])
    await _ok(ctx, "sch_delete", items=[refs[2]])
    out = await _ok(ctx, "sch_delete", items=[own])  # own placement: free, refunded
    assert out["budget"]["used"] == 3  # place (+1) 3 deletes (+3), own delete (-1)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_delete", items=[refs[3]])
    assert err and out["error"]["code"] == "plan_required"
    assert await state(s) == before


async def _74ls00_body_style_2(s: KicadSession) -> str:
    uid = await place_direct(s, "74xx:74LS00", (101.6, 76.2), "U1", unit=2)

    def style(h: KicadHandle) -> None:
        sym = get_item(h, uid)
        sym.body_style = 2
        sym.value_field.text.value = "SN74LS00N"
        sym.proto.ClearField("definition")
        h.schematic().update_items([sym])

    async with s.commit("body style"):
        await s.call(style)
    return uid


async def test_compensation_replaces_symbol_from_library(
    blank_nopcb_session: KicadSession,
) -> None:
    s = blank_nopcb_session
    assert s.project.pcb_file is None
    await s.ensure_symbol_libraries()
    uid = await _74ls00_body_style_2(s)
    _ids, partition = await state(s)
    ctx = edit_ctx(s)
    cs = await prepare_delete(ctx, {"items": ["U1"]})
    with pytest.raises(StructuredToolError) as err:
        await apply_change_set(ctx, cs, _verify=_forced)
    assert err.value.code == "verification_failed"
    assert err.value.details is not None
    new_uid = err.value.details["replaced"][uid]
    assert new_uid != uid
    new = await s.call(lambda h: get_item(h, new_uid))
    assert int(new.unit) == 2 and int(new.body_style) == 2
    assert str(new.reference) == "U1" and str(new.value) == "SN74LS00N"
    assert str(new.definition.id) == "74xx:74LS00"
    _ids2, partition2 = await state(s)
    remapped = frozenset(
        frozenset((p, uid if u == new_uid else u, n) for p, u, n in g) for g in partition2
    )
    assert remapped == partition


@pytest.fixture
async def real(make_settings: Callable[..., Settings], tmp_path: Path) -> AsyncIterator[Real]:
    settings = real_settings(make_settings)
    sessions = KicadSessionManager(settings)
    db = await Database.open(tmp_path / "db.sqlite")
    await ensure_test_conversation(db)
    engine, broadcaster = FakeEngine(), RecordingBroadcaster()
    svc = HistoryService(
        db=db, sessions=sessions, broadcaster=broadcaster, runtime=FakeRuntime(engine)
    )
    try:
        yield Real(svc, sessions, engine, broadcaster, settings)
    finally:
        await sessions.close_all()
        await db.close()


async def test_compensation_with_board_restores_snapshot(real: Real) -> None:
    project, hooks = await real.open(ECC83)
    t = turn("run-del")
    await hooks.on_turn_start(t)
    await hooks.on_first_mutation(t)  # the turn snapshot
    s = real.sessions.current()
    assert s is not None
    target = await s.call(
        lambda h: next(
            (str(x.reference), access.kiid(x))
            for x in h.schematic().get_symbols()
            if not str(x.reference).startswith("#")
        )
    )
    ctx = edit_ctx(s)
    cs = await prepare_delete(ctx, {"items": [target[0]], "with_attached": False})
    with pytest.raises(DocumentInconsistent) as err:
        await apply_change_set(ctx, cs, _verify=_forced)
    assert err.value.auto_restore is True
    await hooks.on_turn_end(
        t,
        TurnOutcome(
            reason="error",
            changed_docs=frozenset({"sch"}),
            mutated=True,
            document_inconsistent=True,
            auto_restore=True,
        ),
    )
    s2 = real.sessions.current()
    assert s2 is not None
    present = await s2.call(lambda h: get_item(h, target[1]))
    assert present is not None and str(present.reference) == target[0]
    assert project.sch_file is not None


async def test_compensated_sheet_delete_restores_at_turn_end(hier_session: KicadSession) -> None:
    """A deleted sheet symbol is never re-created (blank screen, file overwrite)."""
    s = hier_session
    before = await state(s)
    ctx = edit_ctx(s)
    cs = await prepare_delete(ctx, {"items": ["sheet:ampli_ht_horizontal"]})
    with pytest.raises(DocumentInconsistent) as err:
        await apply_change_set(ctx, cs, _verify=_forced)
    assert err.value.auto_restore is True and "sheet symbol" in str(err.value)
    names = await s.call(lambda h: [str(i.name) for i in h.schematic().get_hierarchy()])
    assert "ampli_ht_horizontal" not in names  # left for the turn-end restore
    assert before[0] - (await state(s))[0]  # the sheet stays deleted until then


@pytest.fixture
async def hier_nopcb_session(make_settings: Callable[..., Settings]) -> AsyncIterator[KicadSession]:
    from tests.support.kicad_project import _open

    async for session in _open(
        real_settings(make_settings), "complex_hierarchy", "complex_hierarchy", drop_board=True
    ):
        yield session


def _shared_part(h: KicadHandle) -> tuple[str, str]:
    """A non-power symbol of the shared ampli_ht file: (reference in the first instance,
    uuid)."""
    sub = next(i for i in h.schematic().get_hierarchy() if str(i.filename) == "ampli_ht.kicad_sch")
    sym = next(
        x
        for x in h.schematic().for_sheet(sub).get_symbols()
        if not str(x.reference).startswith("#")
    )
    return str(sym.reference), access.kiid(sym)


async def test_compensated_shared_file_symbol_delete_restores_at_turn_end(
    hier_nopcb_session: KicadSession,
) -> None:
    """Without a board a symbol would be re-placed, but not in a sheet file used twice
    (invisible in the other instance until a reload): auto_restore instead."""
    s = hier_nopcb_session
    assert s.project.pcb_file is None
    ref, _uid = await s.call(_shared_part)
    ctx = edit_ctx(s)
    cs = await prepare_delete(ctx, {"items": [ref], "with_attached": False})
    with pytest.raises(DocumentInconsistent) as err:
        await apply_change_set(ctx, cs, _verify=_forced)
    assert err.value.auto_restore is True and "several instances" in str(err.value)


async def test_shared_file_symbol_counts_once_per_instance(hier_session: KicadSession) -> None:
    s = hier_session
    ref, uid = await s.call(_shared_part)
    cs = await prepare_delete(edit_ctx(s), {"items": [ref]})
    assert cs.logical.changes == 1
    assert cs.logical.deleted_symbols[0] == uid and len(cs.logical.deleted_symbols) == 2
    assert any("several sheet instances" in n or "sheet instances" in n for n in cs.notes)


async def test_unannotated_parts_count_one_each(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Four pre-existing 'R?' are four parts for the <= 3 rule (review round 2)."""
    s = blank_session
    await s.ensure_symbol_libraries()
    uids = [await place_direct(s, "Device:R", (76.2 + 25.4 * i, 76.2), "R?") for i in range(4)]
    cs = await prepare_delete(edit_ctx(s), {"items": uids})
    assert len(set(cs.logical.deleted_symbols)) == 4 and cs.logical.changes == 4
    before = await state(s)
    out, err = await call_tool(edit_ctx(s, edit_db), "sch_delete", items=uids)
    assert err and out["error"]["code"] == "plan_required"
    assert await state(s) == before
