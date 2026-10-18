"""sch_get_pin and sch_add_wire through the real tool adapter (E2 Task 4, Review Focus 1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.geometry import on_grid
from tests.kicad.edit.helpers import item_ids
from tests.support.edit import call_tool, edit_ctx, two_resistors
from tests.support.kicad_cli import cli_netlist, cli_pin_nets

pytestmark = pytest.mark.kicad


class Counter:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


async def _state(s: KicadSession) -> tuple[set[str], frozenset[Any]]:
    def run(h: KicadHandle) -> tuple[set[str], frozenset[Any]]:
        return item_ids(h), access.live_netlist(h).partition()

    return await s.call(run)


def _l_route(a: list[float], b: list[float], dy: float = 2.54) -> list[list[float]]:
    y = a[1] + dy
    return [a, [a[0], y], [b[0], y], b]


async def test_add_wire_connects_two_pins(
    ecc83_session: KicadSession, edit_db: Database, tmp_path: Path
) -> None:
    s = ecc83_session
    await two_resistors(s)
    snapshots = Counter()
    ctx = edit_ctx(s, edit_db, on_mutation=snapshots)
    pa, err_a = await call_tool(ctx, "sch_get_pin", pin="R901.2")
    pb, err_b = await call_tool(ctx, "sch_get_pin", pin="R902.2")
    assert not err_a and not err_b
    out, is_error = await call_tool(ctx, "sch_add_wire", points=_l_route(pa["at_mm"], pb["at_mm"]))
    assert not is_error, out
    assert out["connectivity_verified"] is True
    assert out["logical_changes"] == 1
    assert out["budget"] == {"used": 1, "limit": 20}
    assert [c["kind"] for c in out["created"]] == ["wire"] * 3
    assert all(on_grid(p) for c in out["created"] for p in c["points_mm"])
    assert snapshots.calls == 1
    pa2, _ = await call_tool(ctx, "sch_get_pin", pin="R901.2")
    pb2, _ = await call_tool(ctx, "sch_get_pin", pin="R902.2")
    assert pa2["net"] is not None and pa2["net"] == pb2["net"]
    assert pa2["net"] in out["nets_touched"]
    # Independent cross-check: kicad-cli's netlist of a saved copy.
    root = s.project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    nets = cli_pin_nets(cli_netlist(s.settings.kicad_cli, dest, tmp_path))
    assert nets[("R901", "2")] == nets[("R902", "2")]
    assert nets[("R901", "1")] != nets[("R901", "2")]


async def test_points_are_snapped(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    r = await two_resistors(s)
    a2 = r["a2"].at
    ctx = edit_ctx(s)
    out, is_error = await call_tool(
        ctx, "sch_add_wire", points=[list(a2), [a2[0], a2[1] + 2.54 + 0.3]]
    )
    assert not is_error, out
    assert out["created"][0]["points_mm"] == [list(a2), [a2[0], round(a2[1] + 2.54, 4)]]
    assert any("snapped" in n for n in out["notes"])
    assert any("not connected" in n for n in out["notes"])


async def test_end_off_pin_by_0_01mm_refused(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    r = await two_resistors(s)
    a2, b2 = list(r["a2"].at), list(r["b2"].at)
    before = await _state(s)
    snapshots = Counter()
    off = [b2[0] + 0.01, b2[1]]
    out, is_error = await call_tool(
        edit_ctx(s, on_mutation=snapshots), "sch_add_wire", points=_l_route(a2, off)
    )
    assert is_error
    assert out["error"]["code"] in ("invalid_argument", "verification_failed")
    assert "R902.2" in out["error"]["why"]
    assert await _state(s) == before
    assert snapshots.calls == 0


async def test_interior_over_foreign_pin_refused_before_write(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    r = await two_resistors(s)
    a2 = r["a2"].at
    before = await _state(s)
    snapshots = Counter()
    points = [[a2[0] - 2.54, a2[1]], [a2[0] + 17.78, a2[1]]]  # straight over R901.2, R902.2
    out, is_error = await call_tool(edit_ctx(s, on_mutation=snapshots), "sch_add_wire",
                                    points=points)  # fmt: skip
    assert is_error
    assert out["error"]["code"] == "overlap"
    assert "R901.2" in out["error"]["why"]
    assert await _state(s) == before
    assert snapshots.calls == 0


async def test_diagonal_segment_refused(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    r = await two_resistors(s)
    a2 = r["a2"].at
    out, is_error = await call_tool(
        edit_ctx(s), "sch_add_wire", points=[list(a2), [a2[0] + 2.54, a2[1] + 2.54]]
    )
    assert is_error and out["error"]["code"] == "invalid_argument"
    assert "diagonal" in out["error"]["what"]


async def test_get_pin_reports_net_and_free(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    r = await two_resistors(s)
    ctx = edit_ctx(s)
    free, _ = await call_tool(ctx, "sch_get_pin", pin="R901.2")
    assert free["free"] is True and free["connected"] == []
    assert free["net"] is None
    assert free["direction"] == "down" and free["at_mm"] == list(r["a2"].at)
    assert free["sheet"] == "/" and free["type"] == "passive"
    out, is_error = await call_tool(
        ctx, "sch_add_wire", points=_l_route(free["at_mm"], list(r["b2"].at))
    )
    assert not is_error, out
    wired, _ = await call_tool(ctx, "sch_get_pin", pin="R901.2")
    assert wired["free"] is False
    assert {c["uuid"] for c in wired["connected"]} & {c["uuid"] for c in out["created"]}
    assert wired["net"] is not None
    missing, is_error = await call_tool(ctx, "sch_get_pin", pin="R901.7")
    assert is_error and missing["error"]["code"] == "not_found"


async def test_wire_end_on_existing_wire_gets_junction(ecc83_session: KicadSession) -> None:
    """An end on an existing wire's interior is a T-joint: a junction is added and the
    connectivity check passes (the free other end changes no pin's net)."""
    s = ecc83_session
    r = await two_resistors(s)
    ctx = edit_ctx(s)
    a2, b2 = list(r["a2"].at), list(r["b2"].at)
    first, is_error = await call_tool(ctx, "sch_add_wire", points=_l_route(a2, b2, 5.08))
    assert not is_error, first
    mid_x = round((a2[0] + b2[0]) / 2, 4)
    run_y = round(a2[1] + 5.08, 4)
    out, is_error = await call_tool(
        ctx, "sch_add_wire", points=[[mid_x, run_y + 5.08], [mid_x, run_y]]
    )
    assert not is_error, out
    kinds = sorted(c["kind"] for c in out["created"])
    assert kinds == ["junction", "wire"]
    assert out["side_effects"] == []
