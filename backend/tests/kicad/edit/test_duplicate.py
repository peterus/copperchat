"""sch_duplicate through the real tool adapter (E6 Task 3): channels copied with suffixed
local nets and shared power/global nets, method-A symbol copies (library link kept),
per-sheet annotation, overlap refusal, other target sheets, kicad-cli round trips."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from google.protobuf.json_format import MessageToDict

from copperchat.db import Database
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.geometry import NM, mm_point
from copperchat.kicad_tools.edit.scene import all_symbols
from tests.kicad.edit.connect_support import pin_nets, sheet_pin_nets, state
from tests.support.edit import call_tool, edit_ctx, get_item, kiids
from tests.support.kicad_cli import cli_components, cli_netlist, cli_pin_nets

pytestmark = pytest.mark.kicad


async def _ok(ctx: Any, tool: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, tool, **args)
    assert not err, out
    return out


async def _place(ctx: Any, lib_id: str, at: list[float], **kw: Any) -> str:
    out = await _ok(ctx, "sch_place_symbol", lib_id=lib_id, at=at, **kw)
    return str(out["placed"]["ref"])


async def _pin_at(ctx: Any, pin: str) -> list[float]:
    return list((await _ok(ctx, "sch_get_pin", pin=pin))["at_mm"])


async def _channel(ctx: Any, x: float = 50.8, y: float = 50.8) -> dict[str, str]:
    """R1 -- MID -- R2 with C1 from MID to GND; local label IN on R1.1, global label VOUT
    on R2.2 (all built with the tools)."""
    r1 = await _place(ctx, "Device:R", [x, y], rotation=90)
    r2 = await _place(ctx, "Device:R", [x + 25.4, y], rotation=90)
    c1 = await _place(ctx, "Device:C", [x + 12.7, y + 12.7])
    await _ok(ctx, "sch_add_label", net="IN", at=[f"{r1}.1"])
    await _ok(ctx, "sch_add_label", net="VOUT", kind="global", at=[f"{r2}.2"])
    await _ok(ctx, "sch_add_power", net="GND", at=[f"{c1}.2"])
    await _ok(ctx, "sch_connect", **{"from": f"{r1}.2", "to": f"{r2}.1"})
    await _ok(ctx, "sch_connect", **{"from": f"{c1}.1", "to": f"{r1}.2"})
    a = await _pin_at(ctx, f"{r1}.2")
    await _ok(ctx, "sch_add_label", net="MID", at=[a[0] + 3.81, a[1]])
    return {"r1": r1, "r2": r2, "c1": c1}


def _same_partition(nets: dict[str, str], pins: list[str], mapped: dict[str, str]) -> None:
    """Source pins and their copies are grouped alike (netlist-isomorphic)."""
    for p in pins:
        for q in pins:
            assert (nets[p] == nets[q]) == (nets[mapped[p]] == nets[mapped[q]]), (p, q)


def _mapped(pins: list[str], refs: dict[str, str]) -> dict[str, str]:
    return {p: f"{refs[p.split('.')[0]]}.{p.split('.')[1]}" for p in pins}


async def test_duplicate_channel_three_times_isomorphic(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    ch = await _channel(ctx)
    before = await pin_nets(s)
    r1, r2, c1 = ch["r1"], ch["r2"], ch["c1"]
    assert before[f"{r1}.1"] == "/IN" and before[f"{r1}.2"] == "/MID"
    assert before[f"{r2}.2"] == "VOUT" and before[f"{c1}.2"] == "GND"
    # 3 copies x 11 logical changes exceed the turn budget of 20: a context without the
    # gate (the budget itself is E2's business)
    out = await _ok(edit_ctx(s), "sch_duplicate", items=[r1, r2, c1], offset=[0, 38.1], count=3)
    assert out["connectivity_verified"] is True
    assert out["logical_changes"] == 3 * 11  # 3 parts + GND + 4 wire groups + 3 labels
    copies = out["copies"]
    assert [c["n"] for c in copies] == [2, 3, 4]
    nets = await pin_nets(s)
    pins = [f"{r}.{n}" for r in (r1, r2, c1) for n in ("1", "2")]
    seen_refs: set[str] = set()
    for c in copies:
        refs = c["refs"]
        assert set(refs) >= {r1, r2, c1} and any(k.startswith("#PWR") for k in refs)
        assert not seen_refs & set(refs.values())
        seen_refs |= set(refs.values())
        m = _mapped(pins, refs)
        _same_partition(nets, pins, m)
        n = c["n"]
        assert nets[m[f"{r1}.1"]] == f"/IN_CH{n}"
        assert nets[m[f"{r1}.2"]] == f"/MID_CH{n}"
        assert nets[m[f"{r2}.2"]] == "VOUT"  # global: shared
        assert nets[m[f"{c1}.2"]] == "GND"  # power: shared
        assert c["nets"]["/IN"] == f"/IN_CH{n}" and c["nets"]["GND"] == "GND"
    for p in pins:  # the source is untouched
        assert nets[p] == before[p]
    # kicad-cli on a saved copy agrees
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    xml = cli_netlist(s.settings.kicad_cli, dest, tmp_path)
    cli = cli_pin_nets(xml)
    comps = cli_components(xml)
    for c in copies:
        refs, n = c["refs"], c["n"]
        assert cli[(refs[r1], "1")] == f"/IN_CH{n}"
        assert cli[(refs[r1], "2")] == cli[(refs[r2], "1")] == cli[(refs[c1], "1")]
        assert cli[(refs[r1], "2")] == f"/MID_CH{n}"
        assert cli[(refs[r2], "2")] == "VOUT" and cli[(refs[c1], "2")] == "GND"
        assert comps[refs[c1]]["value"] == comps[c1]["value"]


def _sig(sym: Any) -> dict[str, Any]:
    pos = sym.position
    fields = {}
    for f in (
        sym.reference_field,
        sym.value_field,
        sym.footprint_field,
        sym.datasheet_field,
        sym.description_field,
        *sym.user_fields,
    ):
        fields[f.name] = (
            f.text.value if f.name != "Reference" else "<ref>",
            round((f.text.position.x - pos.x) / NM, 4),
            round((f.text.position.y - pos.y) / NM, 4),
            f.visible,
        )
    return {
        "lib": str(sym.definition.id),
        "unit": sym.unit,
        "body_style": sym.body_style,
        "transform": MessageToDict(sym.transform.proto),
        "attributes": MessageToDict(sym.attributes.proto),
        "fields": fields,
        "pins": (sym.show_pin_numbers, sym.show_pin_names),
    }


async def _symbols_by_ref(s: KicadSession) -> dict[str, Any]:
    def run(h: KicadHandle) -> dict[str, Any]:
        return {i.ref: i.item for i in all_symbols(h)}

    return await s.call(run)


async def test_symbols_copied_via_library_method_a(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    u = await _place(ctx, "74xx:74LS00", [50.8, 50.8], unit=2)
    c = await _place(ctx, "Device:C", [101.6, 50.8], rotation=90, mirror="x")
    await _ok(ctx, "sch_set_fields", ref=c, fields={"MPN": "GRM188R71H104KA93D"}, dnp=True)
    uid = access.kiid((await _symbols_by_ref(s))[u])

    def body_style_2(h: KicadHandle) -> None:  # De Morgan body, through the API
        sch = h.schematic()
        sym = sch.get_items_by_id(kiids(uid))[0]
        sym.body_style = 2
        sym.proto.ClearField("definition")
        sch.update_items([sym])

    async with s.commit("body style"):
        await s.call(body_style_2)
    out = await _ok(ctx, "sch_duplicate", items=[u, c], offset=[0, 38.1])
    refs = out["copies"][0]["refs"]
    syms = await _symbols_by_ref(s)
    src_u, src_c = syms[u], syms[c]
    assert src_u.body_style == 2 and src_c.transform.mirror_x
    for old in (u, c):
        new = syms[refs[old]]
        assert _sig(new) == _sig(syms[old]), old
        dx, dy = (
            b - a for a, b in zip(mm_point(syms[old].position), mm_point(new.position), strict=True)
        )
        assert (round(dx, 4), round(dy, 4)) == (0.0, 38.1)
    assert {f.name: f.text.value for f in syms[refs[c]].user_fields}["MPN"] == "GRM188R71H104KA93D"
    assert syms[refs[c]].attributes.do_not_populate
    # library link kept: the saved file has no private cache names (method B's ':C_1')
    await s.save("sch")
    root = s.project.sch_file
    assert root is not None
    lib_ids = set(re.findall(r'\(lib_id "([^"]+)"\)', root.read_text(encoding="utf-8")))
    assert lib_ids == {"74xx:74LS00", "Device:C"}


async def _two_page_channel(ctx: Any) -> dict[str, str]:
    """A sub-sheet 'ch' (page 2) first, so the root numbers per sheet: R101, C101 with a
    local label SIG and GND."""
    await _ok(ctx, "sch_add_sheet", name="ch", file="ch.kicad_sch", at=[203.2, 25.4])
    r = await _place(ctx, "Device:R", [50.8, 50.8], rotation=90)
    c = await _place(ctx, "Device:C", [63.5, 63.5])
    await _ok(ctx, "sch_connect", **{"from": f"{r}.2", "to": f"{c}.1"})
    await _ok(ctx, "sch_add_label", net="SIG", at=[f"{r}.1"])
    await _ok(ctx, "sch_add_power", net="GND", at=[f"{c}.2"])
    return {"r": r, "c": c}


async def test_copies_annotated_per_sheet_block(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    part = await _two_page_channel(ctx)
    r, c = part["r"], part["c"]
    assert (r, c) == ("R101", "C101")
    out = await _ok(edit_ctx(s), "sch_duplicate", items=[r, c], offset=[0, 30.48], count=2)
    refs = [cp["refs"] for cp in out["copies"]]
    assert (refs[0][r], refs[0][c], refs[1][r], refs[1][c]) == ("R102", "C102", "R103", "C103")
    pwr = [cp["refs"][k] for cp in out["copies"] for k in cp["refs"] if k.startswith("#PWR")]
    assert len(pwr) == 2 and len(set(pwr)) == 2
    sub = await _ok(edit_ctx(s), "sch_duplicate", items=[r, c], sheet="/ch/", offset=[0, 0])
    moved = sub["copies"][0]["refs"]
    assert (moved[r], moved[c]) == ("R201", "C201")
    assert sub["sheet"] == "/ch/"
    nets = await pin_nets(s)
    assert nets["R102.1"] == "/SIG_CH2" and nets["R103.1"] == "/SIG_CH3"
    assert nets["R102.2"] == nets["C102.1"] != nets["R101.2"]


async def test_duplicate_to_other_sheet(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    part = await _two_page_channel(ctx)
    r, c = part["r"], part["c"]
    out = await _ok(ctx, "sch_duplicate", items=[r, c], sheet="/ch/", at=[25.4, 25.4])
    assert out["source_sheet"] == "/" and out["sheet"] == "/ch/"
    copy = out["copies"][0]
    nr, nc = copy["refs"][r], copy["refs"][c]
    corner = [a + d for a, d in zip(out["anchor_mm"], copy["offset_mm"], strict=True)]
    assert all(abs(v - 25.4) <= 0.64 for v in corner)  # `at` = the block's top-left
    nets = await sheet_pin_nets(s)
    assert nets[("/ch/", f"{nr}.1")] == "/ch/SIG_CH2"
    assert nets[("/ch/", f"{nr}.2")] == nets[("/ch/", f"{nc}.1")]
    assert nets[("/ch/", f"{nc}.2")] == "GND" == nets[("/", f"{c}.2")]
    assert nets[("/", f"{r}.1")] == "/SIG"
    assert all(item.get("sheet") == "/ch/" for item in out["created"])
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    cli = cli_pin_nets(cli_netlist(s.settings.kicad_cli, dest, tmp_path))
    assert cli[(nr, "1")] == "/ch/SIG_CH2" and cli[(nc, "2")] == "GND"
    assert cli[(nr, "2")] == cli[(nc, "1")]


async def test_target_overlap_refused(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _place(ctx, "Device:R", [50.8, 50.8], rotation=90)
    await _ok(ctx, "sch_add_label", net="SIG", at=[f"{r}.1"])
    before = await state(s)
    out, err = await call_tool(ctx, "sch_duplicate", items=[r], offset=[2.54, 0])
    error = out["error"]
    assert err and error["code"] == "overlap", out
    assert "would overlap" in error["why"] or "touch" in error["why"]
    assert await state(s) == before  # nothing written
    suggested = error["details"]["suggested_offset_mm"]
    assert suggested is not None and suggested[0] > 2.54 and suggested[1] == 0
    done = await _ok(ctx, "sch_duplicate", items=[r], offset=suggested)
    assert done["connectivity_verified"] is True
    assert (await pin_nets(s))[f"{done['copies'][0]['refs'][r]}.1"] == "/SIG_CH2"


async def test_saved_copy_renders_in_kicad_cli(
    blank_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    u = await _place(ctx, "74xx:74LS00", [50.8, 50.8], unit=3)
    uid = access.kiid((await _symbols_by_ref(s))[u])

    def body_style_2(h: KicadHandle) -> None:
        sch = h.schematic()
        sym = get_item(h, uid)
        sym.body_style = 2
        sym.proto.ClearField("definition")
        sch.update_items([sym])

    async with s.commit("body style"):
        await s.call(body_style_2)
    await _ok(ctx, "sch_add_label", net="A", at=[f"{u}.9"])
    await _ok(ctx, "sch_no_connect", pins=[f"{u}.10"])
    out = await _ok(ctx, "sch_duplicate", items=[u], offset=[50.8, 0], count=2)
    assert len(out["copies"]) == 2
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    svg_dir = tmp_path / "svg"
    done = subprocess.run(
        [s.settings.kicad_cli, "sch", "export", "svg", "-o", str(svg_dir), str(dest)],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    svgs = list(svg_dir.glob("*.svg"))
    assert svgs and all(p.stat().st_size > 0 for p in svgs)


async def test_shared_target_sheet_refused(hier_session: KicadSession) -> None:
    s = hier_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s)
    sub = await s.call(
        lambda h: next(i for i in all_symbols(h) if i.sheet.human_path != "/" and not i.power)
    )
    before = await state(s)
    out, err = await call_tool(ctx, "sch_duplicate", items=[sub.uuid], offset=[0, 50.8])
    assert err and out["error"]["code"] == "invalid_argument", out
    assert "sheet instances" in out["error"]["what"]
    assert await state(s) == before


async def test_block_leaves_out_wires_to_unlisted_parts(
    blank_session: KicadSession, edit_db: Database
) -> None:
    from tests.kicad.edit.connect_support import scene

    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    ch = await _channel(ctx)
    r1, r2, c1 = ch["r1"], ch["r2"], ch["c1"]
    out = await _ok(edit_ctx(s), "sch_duplicate", items=[r1, c1], offset=[0, 38.1])
    refs = out["copies"][0]["refs"]
    assert set(refs) - {r1, c1} and all(k.startswith("#PWR") for k in set(refs) - {r1, c1})
    nets = await pin_nets(s)
    assert nets[f"{refs[r1]}.2"] == nets[f"{refs[c1]}.1"] == "/MID_CH2"
    assert nets[f"{refs[c1]}.2"] == "GND"
    assert "VOUT" not in {lb["text"] for lb in out["created"] if lb["kind"].endswith("label")}
    r2_pin = await _pin_at(ctx, f"{r2}.1")
    where = (r2_pin[0], r2_pin[1] + 38.1)  # where R2.1's copy would be: no wire goes there
    sc = await scene(s)
    assert not any(
        (abs(w.end[0] - where[0]) < 0.01 and abs(w.end[1] - where[1]) < 0.01)
        or (abs(w.start[0] - where[0]) < 0.01 and abs(w.start[1] - where[1]) < 0.01)
        for w in sc.wires.values()
    )
