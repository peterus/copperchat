"""SP4 P4 Task 5: `sp4_synth` placed block by block through the real tools
(`place_synth`), checked by M1, KiCad's DRC and `pcb_status`, and deterministic."""

from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.read import board_model
from copperchat.projects import agent_dir
from tests.kicad.board.conftest import (
    SynthSetup,
    open_setup_db,
    place_synth,
    setup_call,
)

pytestmark = [pytest.mark.kicad]

Position = tuple[float, float, float, str]


async def _positions(rig: SynthSetup, db: Any, run: str) -> dict[str, Position]:
    m = await board_model(rig.turn(db, run))
    return {p.ref: (round(p.x, 4), round(p.y, 4), round(p.rot % 360, 2), p.side) for p in m.parts}


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def placed(
    synth_setup: SynthSetup, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[dict[str, Any]]:
    """Place the board twice from the same unplaced state: place, measure, move every part
    back where the sync put it, place again."""
    rig = synth_setup
    db = await open_setup_db(
        tmp_path_factory.mktemp("place-synth") / "p4.db", rig.session.project.id
    )
    try:
        before = await _positions(rig, db, "run-pos-0")
        first = await place_synth(rig, db, "run-a")
        after_first = await _positions(rig, db, "run-pos-1")
        ctx = rig.turn(db, "run-measure")
        metrics = await setup_call(ctx, "pcb_metrics", only=["M1", "M8", "M9", "M10"])
        status = await setup_call(ctx, "pcb_status")
        drc = await ctx.session.call(
            lambda h: api.run_drc_task(h), timeout=api.drc_call_timeout(60)
        )
        renders = {
            view: await setup_call(rig.turn(db, f"run-render-{view}"), "pcb_render", view=view)
            for view in ("top", "placement")
        }
        back = [
            {"ref": r, "x": x, "y": y, "rot": rot}
            for r, (x, y, rot, _) in sorted(before.items())
            if after_first[r] != before[r]
        ]
        await setup_call(rig.turn(db, "run-back"), "pcb_move", items=back)
        assert await _positions(rig, db, "run-pos-2") == before
        await place_synth(rig, db, "run-b")
        after_second = await _positions(rig, db, "run-pos-3")
        yield {
            "first": first,
            "positions": (after_first, after_second),
            "metrics": metrics,
            "status": status,
            "drc": drc,
            "renders": renders,
            "agent_dir": agent_dir(rig.session.project),
        }
    finally:
        await db.close()


def _metric(data: dict[str, Any], mid: str) -> dict[str, Any]:
    return next(m for m in data["metrics"] if m["id"] == mid)


def test_synth_placed_m1_pass(placed: dict[str, Any], record_property: Any) -> None:
    m1 = _metric(placed["metrics"], "M1")
    assert m1["verdict"] == "pass", m1
    overlaps = [mk for mk in placed["drc"].markers if mk.type == "overlapping_footprints"]
    assert overlaps == [], overlaps
    assert placed["status"]["unplaced"] == 0, placed["status"]["unplaced_refs"]
    last = list(placed["first"]["results"].values())[-1]["score"]["after"]
    for key in ("airwire_mm", "m8_worst_mm", "m10_worst_ratio", "max_cell_pins"):
        record_property(key, last[key])
        print(key, last[key])
    record_property("placement_seconds", round(placed["first"]["seconds"], 2))
    print("placement_seconds", round(placed["first"]["seconds"], 2))
    for mid in ("M8", "M9", "M10"):
        m = _metric(placed["metrics"], mid)
        print(mid, m["value"], m["verdict"], [o["what"] for o in m["offenders"]])
    # the owner's quality bar: decoupling caps within the review distance, M8 no fail
    assert _metric(placed["metrics"], "M8")["verdict"] != "fail"
    out = os.environ.get("P4_RENDER_DIR")
    if out:  # local runs: keep the renders (P4 hands one to the owner)
        renders = Path(placed["agent_dir"]) / "renders"
        for view, data in placed["renders"].items():
            name = data["png_url"].rsplit("/", 1)[-1]
            shutil.copy(renders / name, Path(out) / f"sp4_synth_placed_{view}.png")


def test_place_synth_deterministic(placed: dict[str, Any]) -> None:
    first, second = placed["positions"]
    assert first == second
