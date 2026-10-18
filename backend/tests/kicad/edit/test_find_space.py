"""sch_find_space through the real tool adapter (E4 Task 1)."""

from __future__ import annotations

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.edit.geometry import Box, on_grid
from copperchat.kicad_tools.edit.scene import read_scene, resolve_sheet
from copperchat.kicad_tools.edit.specs_place import prepare_place
from tests.support.edit import call_tool, edit_ctx

pytestmark = pytest.mark.kicad


def _box(d: dict[str, float]) -> Box:
    return Box(d["x"], d["y"], d["x"] + d["w"], d["y"] + d["h"])


def _corridor(at: list[float], direction: str, length: float = 5.08, half: float = 1.27) -> Box:
    dx, dy = {"right": (1, 0), "left": (-1, 0), "up": (0, -1), "down": (0, 1)}[direction]
    tip = (at[0] + dx * length, at[1] + dy * length)
    return Box.around([at, tip]).grow(half)


async def test_candidates_do_not_overlap(ecc83_session: KicadSession, edit_db: Database) -> None:
    s = ecc83_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    value = "4k7 0.1% 25ppm 0.25W"  # a long value widens the Value field
    out, err = await call_tool(ctx, "sch_find_space", lib_id="Device:R", near="U1.1", value=value)
    assert not err, out
    cands = out["candidates"]
    assert 1 <= len(cands) <= 3
    assert [c["distance_mm"] for c in cands] == sorted(c["distance_mm"] for c in cands)
    assert out["size_mm"][0] > 0 and out["size_mm"][1] > 0
    for c in cands:  # every candidate passes sch_place_symbol's checks on this scene
        await prepare_place(ctx, {"lib_id": "Device:R", "at": c["at_mm"], "value": value})
    placed, err = await call_tool(
        ctx, "sch_place_symbol", lib_id="Device:R", at=cands[0]["at_mm"], value=value
    )
    assert not err, placed
    assert placed["placed"]["at_mm"] == cands[0]["at_mm"]


async def test_near_pin_keeps_pin_corridor_free(
    ecc83_session: KicadSession, edit_db: Database
) -> None:
    s = ecc83_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    pin, err = await call_tool(ctx, "sch_get_pin", pin="U1.1")
    assert not err, pin
    corridor = _corridor(pin["at_mm"], pin["direction"])
    out, err = await call_tool(ctx, "sch_find_space", lib_id="Device:C", near="U1.1", rotation=90)
    assert not err, out
    for c in out["candidates"]:
        assert not _box(c["box_mm"]).overlaps(corridor), c
    placed, err = await call_tool(
        ctx, "sch_place_symbol", lib_id="Device:C", at=out["candidates"][0]["at_mm"], rotation=90
    )
    assert not err, placed
    new = placed["placed"]["uuid"]
    scene = await s.call(lambda h: read_scene(h, resolve_sheet(h, None)))
    bodies = [x.body for x in scene.symbols.values() if x.uuid != new]
    for p in placed["placed"]["pins"]:  # the new part's own pins have room in front, too
        mine = _corridor(p["at_mm"], p["direction"])
        assert not mine.overlaps(corridor)
        assert not any(mine.overlaps(b) for b in bodies), p


async def test_on_grid(ecc83_session: KicadSession, edit_db: Database) -> None:
    s = ecc83_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    out, err = await call_tool(ctx, "sch_find_space", lib_id="Device:R", near=[101.3, 77.7])
    assert not err, out
    assert all(on_grid(c["at_mm"]) for c in out["candidates"])


async def test_unknown_near_ref(ecc83_session: KicadSession, edit_db: Database) -> None:
    s = ecc83_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    out, err = await call_tool(ctx, "sch_find_space", lib_id="Device:R", near="Q77")
    assert err and out["error"]["code"] == "not_found", out
    out, err = await call_tool(ctx, "sch_find_space", lib_id="Device:R", near="U1.99")
    assert err and out["error"]["code"] == "not_found", out
