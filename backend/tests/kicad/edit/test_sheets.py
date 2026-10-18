"""sch_add_sheet and sch_add_sheet_pin against a real api-server (E7)."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.errors import StructuredToolError
from copperchat.history.store import compute_hashes, sha256_file
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.annotate import Allocator
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Logical,
    NetIntent,
    Place,
    Stage,
    Update,
    static_stage,
)
from copperchat.kicad_tools.edit.engine import apply_change_set
from copperchat.kicad_tools.edit.geometry import on_grid, pin_ends
from copperchat.kicad_tools.edit.scene import resolve_sheet, sheet_api
from tests.kicad.edit.helpers import item_ids, wire
from tests.support.edit import call_tool, edit_ctx, free_area, get_item, place_raw
from tests.support.kicad_cli import cli_netlist, cli_pin_nets

pytestmark = pytest.mark.kicad


async def _add_sheet(
    ctx: Any, s: KicadSession, name: str = "Power", file: str = "power.kicad_sch", **kw: Any
) -> dict[str, Any]:
    x, y = await s.call(lambda h: free_area(h, 50.8, 38.1))
    args = {"name": name, "file": file, "at": [x, y], **kw}
    out, is_error = await call_tool(ctx, "sch_add_sheet", **args)
    assert not is_error, out
    return out


async def _hierarchy(s: KicadSession) -> list[tuple[str, str, str]]:
    def run(h: KicadHandle) -> list[tuple[str, str, str]]:
        return [
            (str(i.name), str(i.filename), str(i.page_number))
            for i in h.schematic().get_hierarchy()
        ]

    return await s.call(run)


async def test_new_sheet_file_written_and_relative(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    out = await _add_sheet(ctx, s)
    assert out["sheet"]["file"] == "power.kicad_sch"
    assert out["sheet"]["path"] == "/Power/"
    assert out["sheet"]["page"] == "2"
    assert out["logical_changes"] == 1
    assert out["connectivity_verified"] is True
    assert [c["kind"] for c in out["created"]] == ["sheet"]
    assert out["sheet"]["uuid"] == out["created"][0]["uuid"]
    hier = await _hierarchy(s)
    assert ("Power", "power.kicad_sch", "2") in hier  # relative Sheetfile, next page number
    # save() already ran inside the tool: the file exists, no absolute path leaked
    sub = s.project.dir / "power.kicad_sch"
    assert sub.is_file()
    assert str(s.project.dir) not in (s.project.dir / "blank.kicad_sch").read_text()
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    cli_netlist(s.settings.kicad_cli, dest, tmp_path)  # exits 0 = hierarchy loads


async def test_new_sheet_in_existing_hierarchy_gets_next_page(
    hier_session: KicadSession, edit_db: Database
) -> None:
    s = hier_session
    out = await _add_sheet(edit_ctx(s, edit_db), s, name="Extra", file="extra.kicad_sch")
    assert out["sheet"]["page"] == "4"
    assert (s.project.dir / "extra.kicad_sch").is_file()


async def test_place_on_new_sheet(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    out = await _add_sheet(ctx, s)
    sheet = await s.call(lambda h: resolve_sheet(h, out["sheet"]["path"]))
    alloc = await s.call(lambda h: Allocator.from_design(h, 100))
    ref = alloc.next_ref("R", sheet.page)
    assert ref == "R201"
    cs = ChangeSet(
        tool="place_raw",
        sheet=sheet,
        stages=[
            static_stage("place", Place("Device:R", (76.2, 76.2), 0, 1, ref, sheet=sheet)),
            Stage("fields", lambda inp: [Update(list(inp.placed), sheet=sheet)]),
        ],
        pre_images={},
        intent=NetIntent(),
        logical=Logical(1),
    )
    applied = await apply_change_set(edit_ctx(s), cs)
    uid = applied.placed[0]

    def read(h: KicadHandle) -> tuple[str, str]:
        sym = get_item(h, uid, sheet)
        return str(sym.reference), access.sheet_path_key(sym.path)

    got_ref, got_path = await s.call(read)
    assert got_ref == "R201" and got_path == sheet.path


async def test_save_copy_contains_new_sheet(blank_session: KicadSession, edit_db: Database,
                                            tmp_path: Path) -> None:  # fmt: skip
    s = blank_session
    await _add_sheet(edit_ctx(s, edit_db), s)
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    assert (dest.parent / "power.kicad_sch").is_file()
    res, is_error = await call_tool(edit_ctx(s), "run_erc")
    assert not is_error, res


async def test_failed_save_leaves_no_orphan_file(
    blank_session: KicadSession, edit_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    x, y = await s.call(lambda h: free_area(h, 50.8, 38.1))
    real_save = s.save
    calls = {"n": 0}

    async def flaky(doc: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("injected save failure")
        await real_save(doc)  # type: ignore[arg-type]

    monkeypatch.setattr(s, "save", flaky)
    before = await s.call(item_ids)
    out, is_error = await call_tool(
        ctx, "sch_add_sheet", name="Power", file="power.kicad_sch", at=[x, y]
    )
    assert is_error, out
    assert not (s.project.dir / "power.kicad_sch").exists()
    assert await s.call(item_ids) == before
    assert len(await _hierarchy(s)) == 1
    monkeypatch.undo()
    out, is_error = await call_tool(
        ctx, "sch_add_sheet", name="Power", file="power.kicad_sch", at=[x, y]
    )
    assert not is_error, out
    assert (s.project.dir / "power.kicad_sch").is_file()


async def test_overlap_refused(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    out = await _add_sheet(ctx, s)
    before = await s.call(item_ids)
    x, y = out["created"][0]["at_mm"]
    res, is_error = await call_tool(
        ctx, "sch_add_sheet", name="Two", file="two.kicad_sch", at=[x + 5.08, y + 5.08]
    )
    assert is_error and res["error"]["code"] == "overlap"
    assert await s.call(item_ids) == before
    assert not (s.project.dir / "two.kicad_sch").exists()


async def test_duplicate_name_and_bad_files_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _add_sheet(ctx, s)
    x, y = await s.call(lambda h: free_area(h, 50.8, 38.1))
    for args in (
        {"name": "Power", "file": "other.kicad_sch"},  # name taken
        {"name": "B", "file": "../escape.kicad_sch"},
        {"name": "B", "file": "/tmp/abs.kicad_sch"},
        {"name": "B", "file": "blank.kicad_sch"},  # the root itself
        {"name": "B", "file": "a/b/c.kicad_sch"},
    ):
        res, is_error = await call_tool(ctx, "sch_add_sheet", at=[x, y], **args)
        assert is_error, args
        assert res["error"]["code"] == "invalid_argument", (args, res)
    assert len(await _hierarchy(s)) == 2


async def test_existing_stray_file_not_overwritten(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    stray = s.project.dir / "stray.kicad_sch"
    stray.write_text("keep me")
    x, y = await s.call(lambda h: free_area(h, 50.8, 38.1))
    res, is_error = await call_tool(
        edit_ctx(s, edit_db), "sch_add_sheet", name="S", file="stray.kicad_sch", at=[x, y]
    )
    assert is_error and res["error"]["code"] == "invalid_argument"
    assert stray.read_text() == "keep me"


async def test_subdirectory_file(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    out = await _add_sheet(edit_ctx(s, edit_db), s, name="Sub", file="sub/power.kicad_sch")
    assert out["sheet"]["file"] == "sub/power.kicad_sch"
    assert (s.project.dir / "sub" / "power.kicad_sch").is_file()


async def test_hashes_recorded_after_save(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    seen: list[dict[str, str]] = []

    async def listener(project: Any, doc: str) -> None:
        seen.append(compute_hashes(project))

    s._save_listeners.append(listener)
    await _add_sheet(edit_ctx(s, edit_db), s)
    assert seen, "the save listener (history hash recording) did not run"
    assert "power.kicad_sch" in seen[-1]
    # the listener saw the final state: nothing was written after it
    assert seen[-1] == compute_hashes(s.project)
    assert seen[-1]["power.kicad_sch"] == sha256_file(s.project.dir / "power.kicad_sch")


async def test_change_rows_recorded(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _add_sheet(ctx, s)
    assert ctx.editing is not None
    rows = await ctx.editing.changes.for_run(s.project.id, ctx.run_id or "")
    assert [(r.tool, r.op, r.kind) for r in rows if not r.side_effect] == [
        ("sch_add_sheet", "add", "sheet")
    ]


async def test_existing_sheet_file_is_refused_and_untouched(
    hier_session: KicadSession, edit_db: Database
) -> None:
    """KiCad's API gives a sheet symbol for an existing file a blank screen, and save()
    then replaces the file (found in E7): the tool must never get that far."""
    s = hier_session
    reuse = next(f for _n, f, _p in await _hierarchy(s) if f == "ampli_ht.kicad_sch")
    before = (s.project.dir / reuse).read_bytes()
    x, y = await s.call(lambda h: free_area(h, 50.8, 38.1))
    res, is_error = await call_tool(
        edit_ctx(s, edit_db), "sch_add_sheet", name="Again", file=reuse, at=[x, y]
    )
    assert is_error and res["error"]["code"] == "invalid_argument"
    assert "overwrite" in res["error"]["why"]
    assert (s.project.dir / reuse).read_bytes() == before
    assert len(await _hierarchy(s)) == 3


# -- sch_add_sheet_pin -------------------------------------------------------------------


async def _sheet_with_pin(
    s: KicadSession, ctx: Any, name: str = "VIN", direction: str = "input", **kw: Any
) -> dict[str, Any]:
    out, is_error = await call_tool(
        ctx, "sch_add_sheet_pin", sheet="/Power/", name=name, direction=direction, **kw
    )
    assert not is_error, out
    return out


async def test_pin_and_label_connected(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    await _add_sheet(ctx, s)
    out = await _sheet_with_pin(s, ctx)
    assert out["logical_changes"] == 1
    assert out["pin"]["name"] == "VIN" and out["pin"]["sheet"] == "/Power/"
    assert on_grid(tuple(out["pin"]["at_mm"]))  # type: ignore[arg-type]
    assert out["label"]["sheet"] == "/Power/" and out["label"]["uuid"]
    # a part inside the sheet, wired to the label; a part outside, wired to the pin
    sheet = await s.call(lambda h: resolve_sheet(h, "/Power/"))
    inner = await place_raw(s, "Device:R", (101.6, 76.2), "R201", sheet="/Power/")
    ax, ay = await s.call(lambda h: free_area(h, 20.32, 15.24))
    outer = await place_raw(s, "Device:R", (ax + 2.54, ay + 5.08), "R1")

    def geometry(h: KicadHandle) -> dict[str, Any]:
        r_in = pin_ends(get_item(h, inner, sheet))
        r_out = pin_ends(get_item(h, outer))
        lbl = next(lb for lb in sheet_api(h, sheet).get_labels() if lb.text.value == "VIN")
        return {
            "in1": next(p.at for p in r_in if p.number == "1"),
            "out1": next(p.at for p in r_out if p.number == "1"),
            "lbl": (lbl.position.x / 1e6, lbl.position.y / 1e6),
        }

    g = await s.call(geometry)
    pin_at = tuple(out["pin"]["at_mm"])
    # Inside: wire from the label point to R201.1 (two segments via a corner)
    lx, ly = g["lbl"]
    ix, iy = g["in1"]
    inner_route = [[lx, ly], [lx, iy], [ix, iy]] if lx != ix else [[lx, ly], [ix, iy]]
    res, err = await call_tool(ctx, "sch_add_wire", sheet="/Power/", points=inner_route)
    assert not err, res
    ox, oy = g["out1"]
    # The outer wire goes in with plain commits: sch_add_wire's intent does not know that a
    # sheet pin joins the nets of the sub-sheet (see the E7 as-built note).
    route = [pin_at, (pin_at[0] - 7.62, pin_at[1]), (pin_at[0] - 7.62, oy), (ox, oy)]

    def draw(h: KicadHandle) -> None:
        h.schematic().create_items([wire(a, b) for a, b in itertools.pairwise(route)])

    async with s.commit("test wire to the sheet pin"):
        await s.call(draw)

    live = await s.call(access.live_netlist)
    n_in = next(n for n, m in live.nets.items() if any(k[1] == inner and k[2] == "1" for k in m))
    n_out = next(n for n, m in live.nets.items() if any(k[1] == outer and k[2] == "1" for k in m))
    assert n_in == n_out
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    cli = cli_pin_nets(cli_netlist(s.settings.kicad_cli, dest, tmp_path))
    assert cli[("R201", "1")] == cli[("R1", "1")]
    assert cli[("R201", "2")] != cli[("R201", "1")]


async def test_duplicate_pin_name_refused(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _add_sheet(ctx, s)
    await _sheet_with_pin(s, ctx)
    before = await s.call(item_ids)
    res, is_error = await call_tool(
        ctx, "sch_add_sheet_pin", sheet="Power", name="VIN", direction="output"
    )
    assert is_error and res["error"]["code"] == "invalid_argument"
    assert await s.call(item_ids) == before


async def test_pins_stack_without_overlap(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _add_sheet(ctx, s)
    pins = [await _sheet_with_pin(s, ctx, n, "input") for n in ("A", "B", "C")]
    ys = [p["pin"]["at_mm"][1] for p in pins]
    xs = {p["pin"]["at_mm"][0] for p in pins}
    assert len(xs) == 1
    assert all(abs(b - a) >= 2.54 - 1e-6 for a, b in itertools.pairwise(ys)) and ys == sorted(ys)
    assert all(on_grid((xs.copy().pop(), y)) for y in ys)
    right = await _sheet_with_pin(s, ctx, "OUT", "output")
    assert right["pin"]["side"] == "right" and right["pin"]["at_mm"][0] not in xs
    labels = [p["label"]["at_mm"] for p in pins]
    assert len({tuple(p) for p in labels}) == 3  # the labels do not stack on one point


async def test_pin_on_root_refused(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    res, is_error = await call_tool(
        edit_ctx(s, edit_db), "sch_add_sheet_pin", sheet="/", name="X", direction="input"
    )
    assert is_error and res["error"]["code"] == "invalid_argument"


async def test_shared_sheet_label_once(hier_session: KicadSession, edit_db: Database) -> None:
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    hier = await _hierarchy(s)
    shared = next(
        (f for f in {f for _n, f, _p in hier} if [h[1] for h in hier].count(f) >= 2), None
    )
    assert shared is not None, hier
    instances = [(n, p) for n, f, p in hier if f == shared]
    (_n1, p1), (_n2, p2) = instances[:2]
    r1, e1 = await call_tool(
        ctx, "sch_add_sheet_pin", sheet=p1, name="SHARED_NET", direction="input"
    )
    assert not e1, r1
    assert r1["label"] is not None
    r2, e2 = await call_tool(
        ctx, "sch_add_sheet_pin", sheet=p2, name="SHARED_NET", direction="input"
    )
    assert not e2, r2
    assert r2["label"] is None and any("shared sheet" in n for n in r2["notes"])

    def count(h: KicadHandle) -> tuple[int, list[int]]:
        inst = [i for i in h.schematic().get_hierarchy() if str(i.filename) == shared]
        labels = [
            sum(
                1 for lb in h.schematic().for_sheet(i).get_labels() if lb.text.value == "SHARED_NET"
            )
            for i in inst
        ]
        syms = [
            sum(
                1
                for sh in h.schematic().get_sheet_symbols()
                if any(p.text.value == "SHARED_NET" for p in sh.pins)
            )
        ]
        return syms[0], labels

    syms_with_pin, labels = await s.call(count)
    assert syms_with_pin == 2  # one pin on each chosen sheet symbol
    assert labels == [1] * len(labels)  # one label in the file


async def test_bad_geometry_refused(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    for args in ({"at": [10, 10], "size": [-5, 0]}, {"at": [-5, 10]}):
        res, is_error = await call_tool(ctx, "sch_add_sheet", name="G", file="g.kicad_sch", **args)
        assert is_error and res["error"]["code"] == "invalid_argument", args
    assert not (s.project.dir / "g.kicad_sch").exists()


async def test_small_size_is_raised_with_a_note(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    x, y = await s.call(lambda h: free_area(h, 50.8, 38.1))
    res, is_error = await call_tool(
        edit_ctx(s, edit_db), "sch_add_sheet", name="T", file="t.kicad_sch", at=[x, y], size=[2, 2]
    )
    assert not is_error, res
    assert any("minimum" in n for n in res["notes"])


async def test_file_appearing_after_prepare_is_never_overwritten_or_deleted(
    blank_session: KicadSession, edit_db: Database
) -> None:
    from copperchat.kicad_tools.edit.specs_sheets import apply_add_sheet, prepare_add_sheet

    s = blank_session
    ctx = edit_ctx(s, edit_db)
    x, y = await s.call(lambda h: free_area(h, 50.8, 38.1))
    cs = await prepare_add_sheet(ctx, {"name": "Late", "file": "late.kicad_sch", "at": [x, y]})
    late = s.project.dir / "late.kicad_sch"
    late.write_text("someone else's file")
    before = await s.call(item_ids)
    with pytest.raises(StructuredToolError) as err:
        await apply_add_sheet(ctx, cs)
    assert err.value.code == "invalid_argument"
    assert late.read_text() == "someone else's file"
    assert await s.call(item_ids) == before


async def test_failed_save_removes_a_directory_made_by_the_call(
    blank_session: KicadSession, edit_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = blank_session
    real_save = s.save
    calls = {"n": 0}

    async def flaky(doc: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            # the new file and its directory exist by the time a real save fails midway
            (s.project.dir / "newdir").mkdir()
            (s.project.dir / "newdir" / "x.kicad_sch").write_text("partial")
            raise RuntimeError("injected save failure")
        await real_save(doc)  # type: ignore[arg-type]

    monkeypatch.setattr(s, "save", flaky)
    x, y = await s.call(lambda h: free_area(h, 50.8, 38.1))
    res, is_error = await call_tool(
        edit_ctx(s, edit_db), "sch_add_sheet", name="D", file="newdir/x.kicad_sch", at=[x, y]
    )
    assert is_error and "appeared" not in str(res), res
    assert not (s.project.dir / "newdir").exists()
    assert len(await _hierarchy(s)) == 1
    assert "refused to save" in res["error"]["why"]  # a fixed message, no host paths
    assert str(s.project.dir) not in str(res)


async def test_undo_failure_after_failed_save_is_document_inconsistent(
    blank_session: KicadSession, edit_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = blank_session
    inconsistent: list[Exception] = []
    ctx = edit_ctx(s, edit_db, on_document_inconsistent=inconsistent.append)

    async def always_fail(doc: str) -> None:
        raise RuntimeError("injected save failure")

    monkeypatch.setattr(s, "save", always_fail)  # the undo's own re-save fails too
    x, y = await s.call(lambda h: free_area(h, 50.8, 38.1))
    res, is_error = await call_tool(ctx, "sch_add_sheet", name="U", file="u.kicad_sch", at=[x, y])
    assert is_error and res["error"]["code"] == "document_inconsistent", res
    assert inconsistent


async def test_sheet_on_a_sub_sheet_parent(hier_session: KicadSession, edit_db: Database) -> None:
    s = hier_session
    ctx = edit_ctx(s, edit_db)
    parent = next(p for n, f, p in await _hierarchy(s) if n == "ampli_ht_vertical")
    x, y = await s.call(lambda h: free_area(h, 50.8, 38.1, sheet=parent))
    res, is_error = await call_tool(
        ctx, "sch_add_sheet", name="Nested", file="nested.kicad_sch", at=[x, y], parent=parent
    )
    assert not is_error, res
    assert res["sheet"]["path"].endswith("/Nested/") and res["sheet"]["path"] != "/Nested/"
    assert (s.project.dir / "nested.kicad_sch").is_file()


async def test_pin_with_no_room_refused(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    x, y = await s.call(lambda h: free_area(h, 50.8, 12.7))
    await call_tool(
        ctx, "sch_add_sheet", name="Tiny", file="tiny.kicad_sch", at=[x, y], size=[25.4, 12.7]
    )
    outcomes = []
    for i in range(6):
        res, err = await call_tool(
            ctx, "sch_add_sheet_pin", sheet="Tiny", name=f"P{i}", direction="input"
        )
        outcomes.append((err, res.get("error", {}).get("code")))
    assert outcomes[0][0] is False
    assert outcomes[-1] == (True, "overlap")


async def test_bad_pin_arguments_refused(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _add_sheet(ctx, s)
    before = await s.call(item_ids)
    for args in (
        {"name": "", "direction": "input"},
        {"name": "A{B}", "direction": "input"},
        {"name": "OK", "direction": "sideways"},
        {"name": "OK", "direction": "input", "side": "top"},
    ):
        res, is_error = await call_tool(ctx, "sch_add_sheet_pin", sheet="Power", **args)
        assert is_error and res["error"]["code"] == "invalid_argument", args
    assert await s.call(item_ids) == before


async def test_change_rows_carry_the_call_state(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    assert ctx.editing is not None
    state = ctx.editing.turn(s.project.id, ctx.run_id)
    state.active_step_id = "step-1"
    state.call_seq = 7
    await _add_sheet(ctx, s)
    rows = await ctx.editing.changes.for_run(s.project.id, ctx.run_id or "")
    assert rows and all(
        r.plan_step_id == "step-1" and r.call_seq == state.call_seq > 7 for r in rows
    )
