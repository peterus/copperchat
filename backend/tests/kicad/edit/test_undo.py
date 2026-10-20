"""Undo and round-trip (E10 Task 3, spec §12): golden scenario s2 run as a two-step plan
through `PlanService` on the real api-server. Restoring each step checkpoint and the
turn snapshot gives byte-identical files; a half-executed plan rolls back to its base.
"""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.history.restore import restore_snapshot
from copperchat.history.store import compute_hashes
from copperchat.kicad_session import KicadSession
from tests.kicad.edit import golden
from tests.kicad.edit.test_plan_flow import Rig, rig  # noqa: F401  (fixture)
from tests.kicad.edit.test_scenarios import S2, run_script
from tests.support.conversations import TEST_CID
from tests.support.edit import call_tool

pytestmark = pytest.mark.kicad

PLACE, POWER = S2[:4], S2[4:]  # 4 placements; 11 pins given a power symbol / flag


def s2_steps() -> list[dict[str, Any]]:
    return [
        {
            "title": "Place the regulator block",
            "intent": "input connector, AMS1117-3.3, 10 uF input and 22 uF output caps",
            "expected_changes": 4,
            "touches": {"refs": [], "nets": []},
            "verification": "four parts on the sheet",
        },
        {
            "title": "Rails",
            "intent": "+5V, GND, +3V3 power symbols and the PWR_FLAGs",
            "expected_changes": 11,
            "touches": {"refs": [], "nets": ["+5V", "GND", "+3V3"]},
            "verification": "golden s2 connectivity, ERC clean",
        },
    ]


async def start_plan(rig: Rig) -> str:  # noqa: F811
    ctx = rig.ctx()
    out, err = await call_tool(ctx, "plan_propose", title="LDO", summary="s2", steps=s2_steps())
    assert not err, out
    await rig.plans.approve(rig.session.project, out["plan_id"])
    return str(out["plan_id"])


async def finish_step(rig: Rig, idx: int) -> str:  # noqa: F811
    """plan_step_done, justifying new ERC errors (open pins wired in the next step)."""
    ctx = rig.ctx()
    out, err = await call_tool(ctx, "plan_step_done", step=idx, summary=f"step {idx}")
    if err:
        assert out["error"]["code"] == "erc_errors_open", out
        just = {e["id"]: "wired in the next step" for e in out["error"]["details"]["errors"]}
        out, err = await call_tool(
            ctx, "plan_step_done", step=idx, summary=f"step {idx}", erc_justifications=just
        )
    assert not err, out
    return str(out["checkpoint_id"])


async def run_step(rig: Rig, idx: int, script: list[Any], refs: dict[str, str]) -> str:  # noqa: F811
    out, err = await call_tool(rig.ctx(), "plan_step_begin", step=idx)
    assert not err, out
    await run_script(rig.ctx(), script, refs)
    return await finish_step(rig, idx)


def current(rig: Rig) -> KicadSession:  # noqa: F811
    """The open session (a restore reopens the project in a new one)."""
    session = rig.history.sessions.current()
    assert session is not None
    return session


async def live_golden(s: KicadSession) -> dict[str, Any]:
    live = await s.call(golden.live_components)
    return golden.check("s2_ldo", golden.nets_of(live), live)


async def test_step_checkpoints_and_turn_snapshot_restore_byte_identical(
    rig: Rig,  # noqa: F811
) -> None:
    s, project = rig.session, rig.session.project
    base_files = compute_hashes(project)
    plan_id = await start_plan(rig)

    refs: dict[str, str] = {}
    cp1 = await run_step(rig, 1, PLACE, refs)
    step1_files = compute_hashes(project)
    assert len(rig.snapshots) == 1  # the turn snapshot plan_step_begin took (plan base)
    cp2 = await run_step(rig, 2, POWER, refs)
    step2_files = compute_hashes(project)
    assert (await live_golden(s))["golden"]
    plan = await rig.plans.repo.get(project.id, plan_id)
    assert plan.status == "done" and plan.base_snapshot_id == rig.snapshots[0]
    assert [st.checkpoint_id for st in plan.steps] == [cp1, cp2]
    assert len({str(base_files), str(step1_files), str(step2_files)}) == 3

    await restore_snapshot(rig.history, project, cp1, conversation_id=TEST_CID)
    assert compute_hashes(project) == step1_files
    await restore_snapshot(rig.history, project, rig.snapshots[0], conversation_id=TEST_CID)
    assert compute_hashes(project) == base_files
    await restore_snapshot(rig.history, project, cp2, conversation_id=TEST_CID)
    assert compute_hashes(project) == step2_files
    # the restored design is the edited one again (session reloaded from the files)
    assert (await live_golden(current(rig)))["golden"]


async def test_rollback_half_executed_plan(rig: Rig) -> None:  # noqa: F811
    project = rig.session.project
    base_files = compute_hashes(project)
    plan_id = await start_plan(rig)
    await run_step(rig, 1, PLACE, {})
    assert compute_hashes(project) != base_files
    await rig.plans.pause(project, "interrupted")  # e.g. the user pressed Stop
    plan = await rig.plans.rollback(project, plan_id, 0)
    assert plan.status == "paused"
    assert [st.status for st in plan.steps] == ["pending", "pending"]
    assert compute_hashes(project) == base_files
    live = await current(rig).call(golden.live_components)
    assert live == {}  # the blank sheet again
