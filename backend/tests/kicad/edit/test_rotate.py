"""sch_move rotate / mirror beyond symbols (v0.5.0 #2, spec §6.1/§6.2/§6.4) and carried
stub chains on a rotation / mirror (#20, spec §6.3). Real KiCad."""

from __future__ import annotations

import re
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.geometry import orthogonal, same_point
from copperchat.kicad_tools.edit.orient import Frame, compose, matrix_of
from copperchat.kicad_tools.edit.scene import resolve_sheet, sheet_api
from tests.kicad.edit.connect_support import scene, state
from tests.kicad.edit.helpers import local_label, wire
from tests.support.edit import call_tool, edit_ctx

pytestmark = pytest.mark.kicad

SHEET_AT = (50.8, 50.8)  # 'Power', 50.8 x 38.1 mm: box (50.8, 50.8)-(101.6, 88.9)


async def _ok(ctx: Any, tool: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, tool, **args)
    assert not err, __import__("json").dumps(out, indent=1)
    return out


async def _commit(s: KicadSession, *items: Any) -> None:
    def run(h: KicadHandle) -> None:
        sheet_api(h, resolve_sheet(h, "/")).create_items(list(items))

    async with s.commit("fixture"):
        await s.call(run)


async def _power_sheet(ctx: Any, *pins: tuple[str, str]) -> None:
    await _ok(ctx, "sch_add_sheet", name="Power", file="power.kicad_sch", at=list(SHEET_AT))
    for name, side in pins:
        await _ok(
            ctx, "sch_add_sheet_pin", sheet="/Power/", name=name, direction="input", side=side
        )


async def _sheet(s: KicadSession) -> dict[str, Any]:
    def run(h: KicadHandle) -> dict[str, Any]:
        (sym,) = sheet_api(h, resolve_sheet(h, "/")).get_sheet_symbols()
        return {
            "at": (sym.position.x / 1e6, sym.position.y / 1e6),
            "size": (sym.size.x / 1e6, sym.size.y / 1e6),
            "pins": {
                p.text.value: ((p.position.x / 1e6, p.position.y / 1e6), int(p.side))
                for p in sym.pins
            },
            "name_at": (
                sym.name_field.text.position.x / 1e6,
                sym.name_field.text.position.y / 1e6,
            ),
            "name_angle": sym.name_field.text.attributes.angle,
        }

    return await s.call(run)


async def _labels(s: KicadSession) -> dict[str, tuple[tuple[float, float], int]]:
    def run(h: KicadHandle) -> dict[str, tuple[tuple[float, float], int]]:
        return {
            access.kiid(lb): ((lb.position.x / 1e6, lb.position.y / 1e6), int(lb.spin_style))
            for lb in sheet_api(h, resolve_sheet(h, "/")).get_labels()
        }

    return await s.call(run)


async def _saved(s: KicadSession) -> str:
    await s.save("sch")
    assert s.project.sch_file is not None
    return s.project.sch_file.read_text()


def _label_at(text: str, name: str) -> str:
    m = re.search(rf'\(label "{name}"\n\t\t\(at ([^)]*)\)', text)
    assert m, text[:2000]
    return m.group(1)


async def _resistor(ctx: Any, at: list[float], rotation: int = 90) -> str:
    out = await _ok(ctx, "sch_place_symbol", lib_id="Device:R", at=at, rotation=rotation)
    return str(out["placed"]["ref"])


async def _pin(ctx: Any, pin: str) -> tuple[float, float]:
    at = (await _ok(ctx, "sch_get_pin", pin=pin))["at_mm"]
    return (at[0], at[1])


# -- labels (#2) ---------------------------------------------------------------------------


async def test_rotate_label_keeps_anchor_and_reads_correctly(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """A label at its stub end turned to 90: anchor and connection kept, written as the
    GUI writes an upward label (angle 90)."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _resistor(ctx, [101.6, 76.2])
    right = max([await _pin(ctx, f"{r}.1"), await _pin(ctx, f"{r}.2")])
    pin = f"{r}.1" if right == await _pin(ctx, f"{r}.1") else f"{r}.2"
    await _ok(ctx, "sch_add_label", at=[pin], net="SDA")
    ((uid, (at, spin)),) = (await _labels(s)).items()
    assert spin == 3  # RIGHT: the text runs away from the stub
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[uid], rotation=90)
    assert out["moved"][0]["rotation"] == 90
    assert (await _labels(s))[uid] == (at, 2)  # UP, same anchor
    assert (await state(s))[1] == partition
    assert _label_at(await _saved(s), "SDA") == f"{at[0]:g} {at[1]:g} 90"


async def test_mirror_label(blank_session: KicadSession, edit_db: Database) -> None:
    """mirror 'y' flips the text left-right (KiCad's MirrorSpinStyle): RIGHT -> LEFT,
    written with angle 180; a second mirror flips it back."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    lb = local_label("FREE", (63.5, 63.5))
    lb.spin_style = 3  # RIGHT
    await _commit(s, lb)
    uid = access.kiid(lb)
    await _ok(ctx, "sch_move", items=[uid], mirror="y")
    assert (await _labels(s))[uid] == ((63.5, 63.5), 1)  # LEFT
    assert _label_at(await _saved(s), "FREE") == "63.5 63.5 180"
    await _ok(ctx, "sch_move", items=[uid], mirror="y")
    assert (await _labels(s))[uid] == ((63.5, 63.5), 3)


async def test_turned_label_text_over_its_wire_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Turning a label at a stub end so its text would run back over its own wire is
    refused before writing (a placement would not put text there either)."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _resistor(ctx, [101.6, 76.2])
    a, b = await _pin(ctx, f"{r}.1"), await _pin(ctx, f"{r}.2")
    await _ok(ctx, "sch_add_label", at=[f"{r}.1" if a > b else f"{r}.2"], net="SDA")
    ((uid, before),) = (await _labels(s)).items()
    ids, partition = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=[uid], mirror="y")
    assert err and out["error"]["code"] == "overlap", out
    assert "SDA" in out["error"]["what"]
    assert (await _labels(s))[uid] == before
    assert await state(s) == (ids, partition)


async def test_rotate_no_connect_refused(blank_session: KicadSession, edit_db: Database) -> None:
    """No orientation: refused before anything is written (spec §6.4)."""
    from copperchat.kicad_tools.edit.labels import make_no_connect

    s = blank_session
    ctx = edit_ctx(s, edit_db)
    nc = make_no_connect((63.5, 63.5))
    await _commit(s, wire((60.96, 63.5), (63.5, 63.5)), nc)
    ids, partition = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=[access.kiid(nc)], rotation=90)
    assert err and out["error"]["code"] == "invalid_argument", out
    assert "applies to symbols, labels and sheet symbols" in out["error"]["what"], out
    assert await state(s) == (ids, partition)


async def test_rotate_refusal_names_what_turns(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power_sheet(ctx, ("VIN", "left"))
    out, err = await call_tool(ctx, "sch_move", items=["Power.VIN"], rotation=90)
    assert err and "sheet pin" in out["error"]["what"], out  # PR D's refusal comes first


# -- sheet symbols (#2) --------------------------------------------------------------------


async def _autoplaced(s: KicadSession) -> None:
    """Mark the sheet's fields autoplaced and lay them out as KiCad's AutoplaceFields
    does for a horizontal sheet (as the GUI writes them, see the reference fixture)."""
    from copperchat.kicad_tools.edit.labels import _v2

    def run(h: KicadHandle) -> list[Any]:
        (sym,) = sheet_api(h, resolve_sheet(h, "/")).get_sheet_symbols()
        sym.fields_autoplaced = True
        sym.name_field.text.position = _v2((50.8, 50.0884))  # 0.7116 above
        sym.filename_field.text.position = _v2((50.8, 89.4846))  # 0.5846 below
        return [sym]

    items = await s.call(run)
    async with s.commit("fixture"):
        await s.call(lambda h: sheet_api(h, resolve_sheet(h, "/")).update_items(items))


async def test_rotate_sheet_matches_kicad_rotate(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """90 deg CCW about the half-grid centre (76.2, 69.85), as SCH_SHEET::Rotate: the box
    becomes 38.1 x 50.8 at (57.15, 44.45); the left pin goes to the bottom edge, the right
    one to the top; with only top / bottom pins the autoplaced fields turn vertical
    (SCH_SHEET::AutoplaceFields). Written like KiCad writes such pins (angle from the
    side). No GUI-rotated reference exists (headless); the expectation is KiCad's code."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power_sheet(ctx, ("VIN", "left"), ("OUT", "right"))
    await _autoplaced(s)
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=["sheet:Power"], rotation=90)
    assert out["moved"][0]["to_mm"] == [57.15, 44.45]
    got = await _sheet(s)
    assert got["at"] == (57.15, 44.45) and got["size"] == (38.1, 50.8)
    assert got["pins"] == {"VIN": ((59.69, 95.25), 4), "OUT": ((59.69, 44.45), 3)}
    assert got["name_at"] == (pytest.approx(56.4384), 95.25) and got["name_angle"] == 90
    assert (await state(s))[1] == partition
    text = await _saved(s)
    assert re.search(r'\(pin "VIN" input\n\t\t\t\(at 59.69 95.25 270\)', text), text
    assert re.search(r'\(pin "OUT" input\n\t\t\t\(at 59.69 44.45 90\)', text), text


async def test_mirror_sheet_swaps_pin_sides(blank_session: KicadSession, edit_db: Database) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power_sheet(ctx, ("VIN", "left"), ("OUT", "right"))
    before = await _sheet(s)
    out = await _ok(ctx, "sch_move", items=["sheet:Power"], mirror="y")
    assert out["moved"][0]["mirror"] == "y"
    got = await _sheet(s)
    assert got["at"] == before["at"] and got["size"] == before["size"]
    assert got["pins"] == {"VIN": ((101.6, 53.34), 2), "OUT": ((50.8, 53.34), 1)}
    assert got["name_at"] == before["name_at"]  # a mirror shifts fields with the corner
    text = await _saved(s)
    assert re.search(r'\(pin "VIN" input\n\t\t\t\(at 101.6 53.34 0\)', text), text


async def test_mirror_sheet_drags_its_wire(blank_session: KicadSession, edit_db: Database) -> None:
    """A wire on a sheet pin that runs to another part is not owned: it is dragged, the
    netlist stays."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    await _power_sheet(ctx, ("VIN", "left"))
    r = await _resistor(ctx, [25.4, 53.34])  # its wire runs straight into the pin
    pins = sorted([await _pin(ctx, f"{r}.1"), await _pin(ctx, f"{r}.2")])
    pin = f"{r}.1" if pins[1] == await _pin(ctx, f"{r}.1") else f"{r}.2"
    await _ok(ctx, "sch_connect", **{"from": pin, "to": "Power.VIN"})
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=["sheet:Power"], mirror="x")
    assert out["connectivity_verified"] is True
    assert (await state(s))[1] == partition
    sc = await scene(s)
    assert all(orthogonal(w.start, w.end) for w in sc.wires.values())
    assert (await _sheet(s))["pins"]["VIN"] == ((50.8, 86.36), 1)  # upside down


async def test_rotate_sheet_collision_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Review Focus 5: turned, the sheet box would cross a foreign wire below it (clear of
    the box today) -> refused, nothing written."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power_sheet(ctx, ("VIN", "left"))
    await _commit(s, wire((40.64, 93.98), (111.76, 93.98)))
    before = await _sheet(s)
    ids, partition = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=["sheet:Power"], rotation=90)
    assert err and out["error"]["code"] == "overlap", out
    assert "wire" in out["error"]["what"]
    assert await _sheet(s) == before
    assert await state(s) == (ids, partition)


async def test_rotate_sheet_pin_onto_foreign_wire_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Review Focus 5: the left pin's new place (59.69, 95.25) is the end of a foreign
    wire hanging below the sheet: it would join it -> refused, nothing written."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power_sheet(ctx, ("VIN", "left"))
    await _commit(s, wire((59.69, 95.25), (59.69, 105.41)))
    before = await _sheet(s)
    ids, partition = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=["sheet:Power"], rotation=90)
    assert err and out["error"]["code"] == "overlap", out
    assert "sheet pin" in out["error"]["what"]
    assert await _sheet(s) == before
    assert await state(s) == (ids, partition)


# -- carried stub chains (#20) -------------------------------------------------------------


async def _parts(s: KicadSession) -> dict[str, tuple[tuple[float, float], int, str]]:
    """ref -> (position, rotation, mirror) of every symbol on the root."""
    from copperchat.kicad_tools.edit.geometry import symbol_transform

    def run(h: KicadHandle) -> dict[str, tuple[tuple[float, float], int, str]]:
        return {
            str(x.reference): ((x.position.x / 1e6, x.position.y / 1e6), *symbol_transform(x))
            for x in h.schematic().get_symbols()
        }

    return await s.call(run)


async def test_rotate_symbol_carries_power_and_label_stubs(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """R horizontal with a GND stub on one pin and an SIG label stub on the other, turned
    upright: both chains keep their shape relative to their pins (the same rigid turn
    about R's anchor), GND turns with its stub (it keeps pointing away), the label's
    text follows the stub."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _resistor(ctx, [101.6, 76.2], 90)
    a, b = await _pin(ctx, f"{r}.1"), await _pin(ctx, f"{r}.2")
    left, right = (f"{r}.1", f"{r}.2") if a < b else (f"{r}.2", f"{r}.1")
    await _ok(ctx, "sch_add_power", net="GND", at=[left])
    await _ok(ctx, "sch_add_label", at=[right], net="SIG")
    sc0 = await scene(s)
    parts0 = await _parts(s)
    ((luid, (lat, lspin)),) = (await _labels(s)).items()
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[r], rotation=0)
    assert out["carried_wires"] == len(sc0.wires), out
    m = compose(matrix_of(0, ""), matrix_of(-90 % 360, ""))  # 90 -> 0: a quarter turn CW
    f = Frame(parts0[r][0], parts0[r][0], m)
    sc1 = await scene(s)
    for uid, w in sc0.wires.items():  # same uuids, turned
        moved = sc1.wires[uid]
        assert {moved.start, moved.end} == {f(w.start), f(w.end)}, (w, moved)
    gnd = next(k for k in parts0 if k.startswith("#PWR"))
    parts1 = await _parts(s)
    assert parts1[gnd][0] == f(parts0[gnd][0])
    assert parts1[gnd][1] == (parts0[gnd][1] - 90) % 360  # turned with its stub
    from copperchat.kicad_tools.edit.orient import DIR_SPIN, SPIN_DIR

    assert (await _labels(s))[luid] == (f(lat), DIR_SPIN[f.dir(SPIN_DIR[lspin])])
    assert (await state(s))[1] == partition


async def test_mirror_symbol_carries_stubs(blank_session: KicadSession, edit_db: Database) -> None:
    """Mirrored left-right, the stubs swap sides with their pins (shape mirrored)."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _resistor(ctx, [101.6, 76.2], 90)
    a, b = await _pin(ctx, f"{r}.1"), await _pin(ctx, f"{r}.2")
    await _ok(ctx, "sch_add_power", net="GND", at=[f"{r}.1" if a < b else f"{r}.2"])
    sc0 = await scene(s)
    at = (await _parts(s))[r][0]
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[r], mirror="y")
    assert out["carried_wires"] == len(sc0.wires)
    f = Frame(at, at, matrix_of(0, "y"))
    sc1 = await scene(s)
    for uid, w in sc0.wires.items():
        assert {sc1.wires[uid].start, sc1.wires[uid].end} == {f(w.start), f(w.end)}
    assert (await state(s))[1] == partition


async def test_rotate_carried_stub_collides_refused_nothing_written(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """The GND stub would swing onto a foreign part above R: the whole call is refused
    (O8), the error names the pin, the carried item and the obstacle."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r = await _resistor(ctx, [101.6, 76.2], 90)
    a, b = await _pin(ctx, f"{r}.1"), await _pin(ctx, f"{r}.2")
    left = f"{r}.1" if a < b else f"{r}.2"
    await _ok(ctx, "sch_add_power", net="GND", at=[left])
    # turned 90 -> 180 is a half turn: the left stub swings to the right. Block it there.
    pin_at = min(a, b)
    sc0 = await scene(s)
    far = next(p for w in sc0.wires.values() for p in (w.start, w.end) if not same_point(p, pin_at))
    tx, ty = (round(2 * 101.6 - far[0], 4), round(2 * 76.2 - far[1], 4))
    if abs(far[1] - pin_at[1]) < 1e-3:  # a horizontal stub: block it with a vertical wire
        await _commit(s, wire((tx, ty - 5.08), (tx, ty + 5.08)))
    else:
        await _commit(s, wire((tx - 5.08, ty), (tx + 5.08, ty)))
    ids, partition = await state(s)
    parts = await _parts(s)
    out, err = await call_tool(ctx, "sch_move", items=[r], rotation=270)
    assert err and out["error"]["code"] == "overlap", out
    assert left in out["error"]["what"] or left in str(out["error"].get("details")), out
    assert out["error"]["details"]["carried"]["uuid"], out
    assert out["error"]["details"]["obstacles"], out
    assert await state(s) == (ids, partition)
    assert await _parts(s) == parts


async def test_not_owned_chain_rubber_banded_on_rotate(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """A wire to another part's pin is not R1's own: it is dragged on a rotation."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    r1 = await _resistor(ctx, [101.6, 76.2], 90)
    r2 = await _resistor(ctx, [127.0, 76.2], 90)
    a, b = await _pin(ctx, f"{r1}.1"), await _pin(ctx, f"{r1}.2")
    out_pin = f"{r1}.1" if a > b else f"{r1}.2"
    c, d = await _pin(ctx, f"{r2}.1"), await _pin(ctx, f"{r2}.2")
    await _ok(ctx, "sch_connect", **{"from": out_pin, "to": f"{r2}.1" if c < d else f"{r2}.2"})
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=[r1], rotation=0)
    assert "carried_wires" not in out
    assert out["dragged_wires"] >= 1
    assert (await state(s))[1] == partition


async def _pin_stub(s: KicadSession) -> tuple[str, str]:
    """A 5.08 mm stub left of 'Power.VIN' (50.8, 53.34) with the label VIN_EXT at its end."""
    w = wire((50.8, 53.34), (45.72, 53.34))
    lb = local_label("VIN_EXT", (45.72, 53.34))
    lb.spin_style = 1  # LEFT, away from the sheet
    await _commit(s, w, lb)
    return access.kiid(w), access.kiid(lb)


async def test_move_sheet_carries_its_pin_stub(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power_sheet(ctx, ("VIN", "left"))
    wid, lid = await _pin_stub(s)
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=["sheet:Power"], by=[0, 2.54])
    assert out["carried_wires"] == 1, out
    sc = await scene(s)
    assert {sc.wires[wid].start, sc.wires[wid].end} == {(50.8, 55.88), (45.72, 55.88)}
    assert (await _labels(s))[lid] == ((45.72, 55.88), 1)
    assert (await state(s))[1] == partition


async def test_rotate_sheet_turns_its_pin_stub(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Turned 90 CCW about (76.2, 69.85): VIN goes to the bottom edge (59.69, 95.25), its
    stub turns with it (now pointing down) and the label's text follows (BOTTOM)."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power_sheet(ctx, ("VIN", "left"))
    wid, lid = await _pin_stub(s)
    _ids, partition = await state(s)
    out = await _ok(ctx, "sch_move", items=["sheet:Power"], rotation=90)
    assert out["carried_wires"] == 1, out
    sc = await scene(s)
    assert {sc.wires[wid].start, sc.wires[wid].end} == {(59.69, 95.25), (59.69, 100.33)}
    assert (await _labels(s))[lid] == ((59.69, 100.33), 4)
    assert (await state(s))[1] == partition


async def test_rotate_sheet_over_label_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Turned, the sheet box (57.15, 44.45)-(95.25, 95.25) would cover a label below it."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    await _power_sheet(ctx, ("VIN", "left"))
    await _commit(s, local_label("LOOSE", (66.04, 92.71)))
    before = await _sheet(s)
    ids, partition = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=["sheet:Power"], rotation=90)
    assert err and out["error"]["code"] == "overlap", out
    assert "label" in out["error"]["what"], out
    assert await _sheet(s) == before
    assert await state(s) == (ids, partition)
