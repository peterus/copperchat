"""`pcb_zones` on the real api-server (SP4 P7 Task 2): the default GND stack on the routed
`sp4_synth` (`copper_rig`), add / update / delete, the refusals, the budget, the refill
timeouts and the compensation of a failed call.

The four P7 modules share one session server (`copper_rig`); this module works on test
zones of its own and keeps the default stack in place for the others."""

from __future__ import annotations

import time
from typing import Any

import pytest

from copperchat.board.geom import polygon_area, rect_poly
from copperchat.board.model import BoardModel, board_hash
from copperchat.kicad_tools.board import api, zones
from copperchat.kicad_tools.board.read import board_model
from tests.kicad.board.conftest import (
    DEFAULT_ZONES,
    PlacedSynth,
    ensure_default_zones,
    setup_call,
)
from tests.support.edit import call_tool

pytestmark = [
    pytest.mark.kicad,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.timeout(600),  # the first test of a shard builds the session rig
]

TEST_ZONE = "P7_TEST"


async def _model(rig: PlacedSynth, run: str) -> BoardModel:
    return await board_model(rig.turn(run))


def _ids(m: BoardModel) -> dict[str, set[str]]:
    return {
        "zones": {z.kiid for z in m.zones},
        "tracks": {t.kiid for t in m.tracks},
        "vias": {v.kiid for v in m.vias},
    }


def _zone(m: BoardModel, name: str) -> Any:
    return next((z for z in m.zones if z.name == name and not z.rule_area), None)


def _poly(m: BoardModel, dx: float = 0.0) -> list[dict[str, float]]:
    o = m.outline
    pts = rect_poly(o.x + 2 + dx, o.y + 2, 8, 6)
    return [{"x": x, "y": y} for x, y in pts]


async def _drop_test_zones(rig: PlacedSynth, run: str) -> None:
    m = await _model(rig, run)
    names = [z.name for z in m.zones if z.name.startswith(TEST_ZONE) and not z.rule_area]
    if names:
        await setup_call(
            rig.turn(f"{run}-del"), "pcb_zones", action="delete", zones=[{"name": n} for n in names]
        )


async def _add_test_zone(rig: PlacedSynth, run: str, name: str = TEST_ZONE) -> dict[str, Any]:
    m = await _model(rig, run)
    return await setup_call(
        rig.turn(f"{run}-add"),
        "pcb_zones",
        action="add",
        zones=[
            {
                "name": name,
                "net": "GND",
                "layers": ["B.Cu"],
                "outline": {"polygon": _poly(m)},
                "priority": 5,
                "clearance": 0.2,
            }
        ],
    )


# -- the default stack ---------------------------------------------------------------------


async def test_default_stack_4l(copper_rig: PlacedSynth, record_property: Any) -> None:
    rig = copper_rig
    m = await _model(rig, "run-def-0")
    present = [n for n in DEFAULT_ZONES if _zone(m, n) is not None]
    if present:  # another module poured it already: pour it again here
        await setup_call(
            rig.turn("run-def-del"),
            "pcb_zones",
            action="delete",
            zones=[{"name": n} for n in present],
        )
    t0 = time.monotonic()
    out = await setup_call(rig.turn("run-def"), "pcb_zones", action="add")
    record_property("pcb_zones_default_s", round(time.monotonic() - t0, 2))
    assert [z["name"] for z in out["zones"]] == list(DEFAULT_ZONES)
    by_name = {z["name"]: z for z in out["zones"]}
    assert by_name["GND_In1"]["layers"] == ["In1.Cu"] and by_name["GND_In1"]["priority"] == 10
    assert by_name["GND_F"]["layers"] == ["F.Cu"] and by_name["GND_F"]["priority"] == 1
    assert by_name["GND_B"]["layers"] == ["B.Cu"] and by_name["GND_B"]["priority"] == 1
    assert all(z["net"] == "GND" for z in out["zones"])
    assert by_name["GND_In1"]["filled_area_mm2"] > 0
    assert out["logical_changes"] == 3 and out["next"] == "pcb_fanout"
    m11 = out["m11"]
    assert m11 is not None and m11["id"] == "M11", out
    m = await _model(rig, "run-def-2")
    o = m.outline
    board_area = abs(polygon_area(rect_poly(o.x, o.y, o.w, o.h, o.corner_radius)))
    plane = _zone(m, "GND_In1")
    ratio = plane.filled_area_mm2 / board_area
    print("M11", m11["value"], m11["verdict"], m11["offenders"][:5], "In1 ratio", ratio)
    record_property("in1_fill_ratio", round(ratio, 4))
    record_property("m11_value", m11["value"])
    assert ratio >= 0.85
    settings = dict(plane.settings)
    assert settings["fill_mode"] == "solid"


async def test_metrics_budget_plane_filled(copper_rig: PlacedSynth, record_property: Any) -> None:
    """`pcb_metrics {}` (M1-M16 incl. M11 on the filled In1.Cu plane and M14 silk over
    pads) on the plane-filled routed rig within 5 s (pins P3a's bucketed geometry)."""
    await ensure_default_zones(copper_rig)
    t0 = time.monotonic()
    out = await setup_call(copper_rig.turn("run-metrics"), "pcb_metrics")
    wall = time.monotonic() - t0
    record_property("pcb_metrics_wall_s", round(wall, 3))
    record_property("pcb_metrics_seconds", out["seconds"])
    print("pcb_metrics", wall, out["seconds"])
    ids = [m["id"] for m in out["metrics"]]
    assert ids == [f"M{i}" for i in range(1, 17)]
    assert wall < 5.0


# -- add / update / delete -------------------------------------------------------------------


async def test_add_update_delete(copper_rig: PlacedSynth) -> None:
    rig = copper_rig
    await _drop_test_zones(rig, "run-aud-0")
    out = await _add_test_zone(rig, "run-aud-1")
    assert out["zones"][0]["name"] == TEST_ZONE and out["zones"][0]["action"] == "add"
    z = _zone(await _model(rig, "run-aud-2"), TEST_ZONE)
    assert z is not None and dict(z.settings)["clearance"] == "0.2" and z.priority == 5
    out = await setup_call(
        rig.turn("run-aud-3"),
        "pcb_zones",
        action="update",
        zones=[{"name": TEST_ZONE, "clearance": 0.3, "thermal": {"gap": 0.4, "spoke_width": 0.3}}],
    )
    assert out["zones"][0]["kiid"] == z.kiid
    z2 = _zone(await _model(rig, "run-aud-4"), TEST_ZONE)
    s = dict(z2.settings)
    assert s["clearance"] == "0.3" and s["thermal_gap"] == "0.4" and s["connection"] == "thermal"
    await setup_call(
        rig.turn("run-aud-5"), "pcb_zones", action="delete", zones=[{"name": TEST_ZONE}]
    )
    assert _zone(await _model(rig, "run-aud-6"), TEST_ZONE) is None


async def test_add_existing_name_refused(copper_rig: PlacedSynth) -> None:
    await ensure_default_zones(copper_rig)
    before = board_hash(await _model(copper_rig, "run-dup-0"))
    out, err = await call_tool(
        copper_rig.turn("run-dup"),
        "pcb_zones",
        action="add",
        zones=[{"name": "GND_In1", "net": "GND", "layers": ["In1.Cu"]}],
    )
    assert err and out["error"]["code"] == "invalid_argument", out
    assert "update" in out["error"]["suggestion"]
    assert board_hash(await _model(copper_rig, "run-dup-2")) == before


async def test_unknown_net(copper_rig: PlacedSynth) -> None:
    out, err = await call_tool(
        copper_rig.turn("run-unk"),
        "pcb_zones",
        action="add",
        zones=[{"name": "X1", "net": "GNDD", "layers": ["B.Cu"]}],
    )
    assert err and out["error"]["code"] == "not_found", out
    assert "GND" in out["error"]["details"]["closest"]["GNDD"]


async def test_unknown_zone_and_layer(copper_rig: PlacedSynth) -> None:
    out, err = await call_tool(
        copper_rig.turn("run-unkz"), "pcb_zones", action="delete", zones=[{"name": "GND_In9"}]
    )
    assert err and out["error"]["code"] == "not_found", out
    out, err = await call_tool(
        copper_rig.turn("run-unkl"),
        "pcb_zones",
        action="add",
        zones=[{"name": "X2", "net": "GND", "layers": ["In7.Cu"]}],
    )
    assert err and out["error"]["code"] == "not_found", out


async def test_zones_m11_via_run_cpu(
    copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = copper_rig
    await ensure_default_zones(rig)
    await _drop_test_zones(rig, "run-cpu-0")
    calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []
    real = zones.run_cpu

    async def spy(fn: Any, *args: Any, **kw: Any) -> Any:
        calls.append((fn, args, kw))
        return await real(fn, *args, **kw)

    monkeypatch.setattr(zones, "run_cpu", spy)
    await _add_test_zone(rig, "run-cpu-1")
    monkeypatch.undo()
    m11 = [c for c in calls if c[0] is zones.metrics_job]
    assert len(m11) == 1
    _fn, args, kw = m11[0]
    assert kw["only"] == frozenset({"M11"}) and kw["timeout_s"] == 30
    model = args[0]
    assert any(z.filled for z in model.zones if not z.rule_area)  # fills kept for M11
    await _drop_test_zones(rig, "run-cpu-2")


async def test_requires_placement_approval(copper_rig: PlacedSynth) -> None:
    """A 0.5 mm move of a part without copper (a mounting hole) makes the approval stale →
    refused `placement_not_approved`; moved back exactly → it holds again (as P5b)."""
    rig = copper_rig
    m = await _model(rig, "run-na-0")
    hole = next(p for p in m.parts if p.ref.startswith("H"))
    await setup_call(rig.turn("run-na-1"), "pcb_move", items=[{"ref": hole.ref, "x": hole.x + 0.5}])
    try:
        out, err = await call_tool(rig.turn("run-na-2"), "pcb_zones", action="add")
        assert err and out["error"]["code"] == "placement_not_approved", out
        assert out["error"]["details"]["stale"] is True
    finally:
        await setup_call(rig.turn("run-na-3"), "pcb_move", items=[{"ref": hole.ref, "x": hole.x}])


async def test_cost_per_zone(copper_rig: PlacedSynth) -> None:
    rig = copper_rig
    await _drop_test_zones(rig, "run-cost-0")
    m = await _model(rig, "run-cost-1")
    cs = await zones.prepare_zones(
        rig.turn("run-cost"),
        {
            "action": "add",
            "zones": [
                {
                    "name": f"{TEST_ZONE}{i}",
                    "net": "GND",
                    "layers": ["B.Cu"],
                    "outline": {"polygon": _poly(m, 10.0 * i)},
                }
                for i in range(3)
            ],
        },
    )
    assert cs.logical.changes == 3


# -- refills, compensation ---------------------------------------------------------------


async def test_refill_passes_explicit_timeout(
    copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = copper_rig
    await _drop_test_zones(rig, "run-rto-0")
    refills: list[float] = []
    timeouts: list[float | None] = []
    real_refill = api.refill

    def spy_refill(h: Any, *, timeout_s: float) -> None:
        refills.append(timeout_s)
        real_refill(h, timeout_s=timeout_s)

    drcs: list[float] = []
    session = rig.session
    real_call = session.call

    async def spy_call(fn: Any, *, timeout: float | None = None) -> Any:
        def wrapped(h: Any) -> Any:
            n = len(refills) + len(drcs)
            try:
                return fn(h)
            finally:
                if len(refills) + len(drcs) > n:  # this call ran a refill or a DRC
                    timeouts.append(timeout)

        return await real_call(wrapped, timeout=timeout)

    monkeypatch.setattr(api, "refill", spy_refill)
    monkeypatch.setattr(session, "call", spy_call)
    real_drc = api.run_drc_task

    def spy_drc(h: Any, *, timeout_s: float = 60.0) -> Any:
        drcs.append(timeout_s)
        return real_drc(h, timeout_s=timeout_s)

    monkeypatch.setattr(api, "run_drc_task", spy_drc)
    await _add_test_zone(rig, "run-rto-1")
    monkeypatch.undo()
    assert refills == [api.REFILL_TIMEOUT_S]
    assert api.refill_call_timeout(api.REFILL_TIMEOUT_S) == 105
    assert timeouts == [105]  # the refill's session.call, never the 30 s default
    assert drcs == []  # pcb_zones runs no DRC
    await _drop_test_zones(rig, "run-rto-2")


async def test_zones_refill_timeout(
    copper_rig: PlacedSynth, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = copper_rig
    await _drop_test_zones(rig, "run-rt-0")
    before = await _model(rig, "run-rt-1")

    def boom(h: Any, *, timeout_s: float) -> None:
        raise api.RefillTimeout(timeout_s)

    monkeypatch.setattr(api, "refill", boom)
    m = before
    out, err = await call_tool(
        rig.turn("run-rt-2"),
        "pcb_zones",
        action="add",
        zones=[
            {"name": TEST_ZONE, "net": "GND", "layers": ["B.Cu"], "outline": {"polygon": _poly(m)}}
        ],
    )
    monkeypatch.undo()
    assert err and out["error"]["code"] == "refill_timeout", out
    after = await _model(rig, "run-rt-3")
    assert _ids(after) == _ids(before)
    assert board_hash(after) == board_hash(before)


async def test_fail_after_1_restores(copper_rig: PlacedSynth) -> None:
    """An update and a delete, each with `fail_after=1` (after the write stage): zone set,
    settings and board_hash equal pre-call (deleted zone re-created with its KIID)."""
    rig = copper_rig
    await _drop_test_zones(rig, "run-fa-0")
    await _add_test_zone(rig, "run-fa-1")
    for n, args in enumerate(
        (
            {"action": "update", "zones": [{"name": TEST_ZONE, "clearance": 0.35, "priority": 7}]},
            {"action": "delete", "zones": [{"name": TEST_ZONE}]},
        )
    ):
        ctx = rig.turn(f"run-fa-{n}-go")
        before = await board_model(ctx)
        cs = await zones.prepare_zones(ctx, args)
        cs.fail_after = 1
        if ctx.on_mutation is not None:
            await ctx.on_mutation()
        with pytest.raises(RuntimeError, match="fail_after"):
            await zones.apply_zones(ctx, cs)
        after = await _model(rig, f"run-fa-{n}-after")
        assert _ids(after) == _ids(before), args
        assert board_hash(after) == board_hash(before), args
        z = _zone(after, TEST_ZONE)
        assert z is not None and dict(z.settings) == dict(_zone(before, TEST_ZONE).settings)
    await _drop_test_zones(rig, "run-fa-9")
