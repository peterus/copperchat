"""sch_rename_net and sch_reroute (E5 Task 4, Review Focus 1)."""

from __future__ import annotations

import pytest

from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.edit.geometry import on_grid
from tests.kicad.edit.connect_support import Counter, pin_nets, place, scene, state
from tests.support.edit import call_tool, edit_ctx

pytestmark = pytest.mark.kicad

R = "Device:R"


async def test_rename_local_net_all_labels(blank_session: KicadSession) -> None:
    s = blank_session
    await place(s, (R, "R1", (101.6, 101.6), 0), (R, "R2", (116.84, 101.6), 0))
    ctx = edit_ctx(s)
    _, err = await call_tool(ctx, "sch_add_label", net="OLD", at=["R1.1", "R2.1"])
    assert not err
    _, before = await state(s)
    out, err = await call_tool(ctx, "sch_rename_net", old="OLD", new="NEW")
    assert not err, out
    assert out["logical_changes"] == 1
    assert out["old"] == "/OLD" and out["new"] == "/NEW"
    assert len(out["modified"]) == 2
    _, after = await state(s)
    assert after == before  # members identical
    sc = await scene(s)
    assert sorted(lb.text for lb in sc.labels.values()) == ["NEW", "NEW"]
    nets = await pin_nets(s)
    assert nets["R1.1"] == nets["R2.1"] == "/NEW"


async def test_rename_refuses_power_and_conflicts(blank_session: KicadSession) -> None:
    s = blank_session
    await place(
        s,
        (R, "R1", (101.6, 101.6), 0),
        (R, "R2", (116.84, 101.6), 0),
        (R, "R3", (132.08, 101.6), 0),
    )
    ctx = edit_ctx(s)
    for tool, args in (
        ("sch_add_label", {"net": "OLD", "at": ["R1.1"]}),
        ("sch_add_label", {"net": "BUS", "at": ["R2.1"], "kind": "global"}),
        ("sch_add_power", {"net": "+3V3", "at": ["R3.1"]}),
    ):
        _, err = await call_tool(ctx, tool, **args)
        assert not err, (tool, args)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_rename_net", old="+3V3", new="VMAIN")
    assert err and "power net" in out["error"]["what"]
    out, err = await call_tool(ctx, "sch_rename_net", old="OLD", new="BUS")
    assert err and out["error"]["code"] == "invalid_argument"
    assert "already used" in out["error"]["what"]
    assert await state(s) == before


async def _l_connected(s: KicadSession) -> list[str]:
    """R1.2 (101.6, 105.41) and R2.2 (116.84, 105.41) joined by an L-ish wire below them."""
    await place(s, (R, "R1", (101.6, 101.6), 0), (R, "R2", (116.84, 101.6), 0))
    ctx = edit_ctx(s)
    pts = [[101.6, 105.41], [101.6, 110.49], [116.84, 110.49], [116.84, 105.41]]
    out, err = await call_tool(ctx, "sch_add_wire", points=pts)
    assert not err, out
    return [c["uuid"] for c in out["created"]]


async def test_reroute_keeps_net_set(blank_session: KicadSession) -> None:
    s = blank_session
    old = await _l_connected(s)
    ctx = edit_ctx(s)
    _ids, part_before = await state(s)
    new_pts = [[101.6, 105.41], [101.6, 113.03], [116.84, 113.03], [116.84, 105.41]]
    out, err = await call_tool(
        ctx, "sch_reroute", wires={"from": "R1.2", "to": "R2.2"}, points=new_pts
    )
    assert not err, out
    assert out["logical_changes"] == 1
    assert sorted(d["uuid"] for d in out["deleted"]) == sorted(old)
    created = [c for c in out["created"] if c["kind"] == "wire"]
    assert created and all(on_grid(p) for c in created for p in c["points_mm"])
    ids_after, part_after = await state(s)
    assert part_after == part_before
    assert not set(old) & ids_after
    sc = await scene(s)
    ys = {w.start[1] for w in sc.wires.values()} | {w.end[1] for w in sc.wires.values()}
    assert 113.03 in ys and 110.49 not in ys


async def test_reroute_touching_foreign_pin_refused(blank_session: KicadSession) -> None:
    s = blank_session
    old = await _l_connected(s)
    await place(s, (R, "R3", (109.22, 120.65), 0))  # R3.1 at (109.22, 116.84)
    ctx = edit_ctx(s)
    before = await state(s)
    new_pts = [[101.6, 105.41], [101.6, 116.84], [116.84, 116.84], [116.84, 105.41]]
    out, err = await call_tool(ctx, "sch_reroute", wires=old, points=new_pts)
    assert err
    assert out["error"]["code"] == "overlap"
    assert "R3.1" in out["error"]["why"]
    assert await state(s) == before


async def _refused(s: KicadSession, wires: object, points: list[list[float]]) -> dict[str, str]:
    snapshots = Counter()
    ctx = edit_ctx(s, on_mutation=snapshots)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_reroute", wires=wires, points=points)
    assert err, out
    assert await state(s) == before and snapshots.calls == 0
    return dict(out["error"])


Z = [[101.6, 105.41], [101.6, 113.03], [116.84, 113.03], [116.84, 105.41]]


async def test_reroute_refuses_a_label_on_the_route(blank_session: KicadSession) -> None:
    s = blank_session
    old = await _l_connected(s)
    _, err = await call_tool(edit_ctx(s), "sch_add_label", net="MID", at=[109.22, 110.49])
    assert not err
    error = await _refused(s, old, Z)
    assert error["code"] == "invalid_argument" and "join" in error["what"]


async def test_reroute_refuses_a_t_join_on_the_route(blank_session: KicadSession) -> None:
    s = blank_session
    await _l_connected(s)
    await place(s, (R, "R3", (109.22, 120.65), 0))  # R3.1 at (109.22, 116.84)
    _, err = await call_tool(
        edit_ctx(s), "sch_add_wire", points=[[109.22, 116.84], [109.22, 110.49]]
    )
    assert not err
    error = await _refused(s, {"from": "R1.2", "to": "R2.2"}, Z)
    assert "join" in error["what"]


async def test_reroute_must_keep_both_ends(blank_session: KicadSession) -> None:
    s = blank_session
    old = await _l_connected(s)
    shifted = [[101.6, 105.41], [101.6, 113.03], [119.38, 113.03], [119.38, 105.41]]
    error = await _refused(s, old, shifted)
    assert "keep both ends" in error["what"]
    error = await _refused(s, old, [[101.6, 105.41], [101.6, 113.03], [101.6, 110.49]])
    assert "reverses" in error["what"]
