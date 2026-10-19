"""A two-step plan end to end on the real api-server (E8b Task 3).

E4's `sch_place_symbol` / E5's `sch_connect` run in parallel phases: step 1 places R + LED
through a test tool with E4's two-stage placement (`tests.support.edit.place_raw`'s change
set, run through the real two-phase adapter and PlanGate), step 2 connects them with E2's
`sch_add_wire`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from copperchat.agent.sdk_tools import build_sdk_tool_list
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.editing.service import EditingService
from copperchat.history.service import HistoryService
from copperchat.history.store import compute_hashes
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Logical,
    NetIntent,
    Place,
    Stage,
    StageInput,
    Update,
    static_stage,
)
from copperchat.kicad_tools.edit.engine import apply_change_set, edit_result
from copperchat.kicad_tools.edit.scene import resolve_sheet
from copperchat.plans.gate import PlanGate
from copperchat.plans.service import PlanService
from copperchat.projects import discover_projects
from copperchat.tools import ToolContext, ToolResult, ToolSpec, edit_spec
from tests.kicad.edit.helpers import item_ids
from tests.support.conversations import TEST_CID, ensure_test_conversation
from tests.support.edit import call_tool, free_area
from tests.support.history import FakeEngine, RecordingBroadcaster
from tests.support.kicad_cli import cli_netlist, cli_pin_nets
from tests.support.kicad_project import copy_fixture, real_settings

pytestmark = pytest.mark.kicad
RUN = "run-plan"


class Runtime:
    def __init__(self) -> None:
        self.engine = FakeEngine()
        self.submitted: list[tuple[str, str]] = []

        async def submit_ex(conv: str, cid: str, text: str, atts: Any, meta: Any = None) -> Any:
            assert conv == TEST_CID  # Approve/Resume go to the plan's session
            self.submitted.append((cid, text))
            return None, True

        self.engine.submit_ex = submit_ex  # type: ignore[attr-defined]

    def get(self, project_id: str) -> FakeEngine:
        return self.engine

    async def ensure(self, project: Any) -> FakeEngine:
        return self.engine


@dataclass
class Rig:
    session: KicadSession
    history: HistoryService
    editing: EditingService
    plans: PlanService
    runtime: Runtime
    snapshots: list[str]

    def ctx(self) -> ToolContext:
        async def on_mutation() -> None:
            if not self.snapshots:  # one turn snapshot, as the run engine takes
                snap = await self.history.store(self.session.project).snapshot(RUN, "turn")
                self.snapshots.append(snap.id)

        return ToolContext(
            project=self.session.project,
            session=self.session,
            settings=self.session.settings,
            run_id=RUN,
            on_mutation=on_mutation,
            editing=self.editing,
        )


@pytest.fixture
async def rig(make_settings: Callable[..., Settings], tmp_path: Path) -> AsyncIterator[Rig]:
    settings = real_settings(make_settings)
    copy_fixture("blank", settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "blank")
    sessions = KicadSessionManager(settings)
    db = await Database.open(tmp_path / "db.sqlite")
    await ensure_test_conversation(db)
    runtime = Runtime()
    history = HistoryService(
        db=db, sessions=sessions, broadcaster=RecordingBroadcaster(), runtime=runtime
    )

    async def no_preview(session: Any, doc: Any) -> None:
        return None

    history.render_preview = no_preview  # type: ignore[assignment]
    editing = EditingService(
        db=db, broadcaster=None, settings_repo=EditingSettingsRepo(db), history=history
    )
    plans = PlanService(db=db, editing=editing, history=history, runtime=runtime, broadcaster=None)
    editing.gate = PlanGate(editing, plans)
    editing.plans = plans
    try:
        session = await sessions.open(project)
        await session.ensure_symbol_libraries()
        yield Rig(session, history, editing, plans, runtime, [])
    finally:
        await sessions.close_all()
        await db.close()


def place_spec() -> ToolSpec:
    """E4-style placement (place, then re-send the placed symbol) as a two-phase tool."""

    async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
        target = await ctx.session.call(lambda h: resolve_sheet(h, None))

        def resend(inp: StageInput) -> list[Any]:
            return [Update(list(inp.placed), sheet=target)]

        at = (float(args["at"][0]), float(args["at"][1]))
        return ChangeSet(
            tool="test_place",
            sheet=target,
            stages=[
                static_stage("place", Place(args["lib_id"], at, 0, 1, args["ref"], sheet=target)),
                Stage("fields", resend),
            ],
            pre_images={},
            intent=NetIntent(),
            logical=Logical(1),
            summary={},
        )

    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        return edit_result(cs, await apply_change_set(ctx, cs))

    return edit_spec(
        "test_place", "test placement", {"type": "object", "properties": {}}, prepare, apply
    )


async def place(rig: Rig, lib_id: str, at: tuple[float, float], ref: str) -> dict[str, Any]:
    import json

    tool = build_sdk_tool_list([place_spec()], rig.ctx)[0]
    raw = await tool.handler({"lib_id": lib_id, "at": list(at), "ref": ref})
    payload: dict[str, Any] = json.loads(raw["content"][0]["text"])
    assert not raw.get("is_error"), payload
    return payload


def steps() -> list[dict[str, Any]]:
    return [
        {
            "title": "Place R and LED",
            "intent": "series resistor and LED",
            "expected_changes": 2,
            "touches": {"refs": ["R1", "D1"], "nets": []},
            "verification": "both parts on the sheet",
        },
        {
            "title": "Connect R to LED",
            "intent": "wire R1.2 to D1.2",
            "expected_changes": 1,
            "touches": {"refs": ["R1", "D1"], "nets": []},
            "verification": "R1.2 and D1.2 on one net",
        },
    ]


async def test_two_step_plan_end_to_end(rig: Rig, tmp_path: Path) -> None:
    s, project = rig.session, rig.session.project
    ctx = rig.ctx()
    out, err = await call_tool(ctx, "plan_propose", title="LED", summary="R + LED", steps=steps())
    assert not err, out
    plan_id = out["plan_id"]
    # A mutating call while the plan is proposed is refused before any snapshot.
    before_items = await s.call(item_ids)
    x, y = await s.call(lambda h: free_area(h, 30.48, 15.24))
    refused, is_err = await call_tool(ctx, "sch_add_wire", points=[[x, y], [x + 5.08, y]])
    assert is_err and refused["error"]["code"] == "plan_open"
    assert rig.snapshots == [] and await s.call(item_ids) == before_items

    await rig.plans.approve(project, plan_id)
    assert rig.runtime.submitted == [
        (
            f"plan-{plan_id}-approve-1-1",
            f"The user approved plan {plan_id} (revision 1). Execute it step by step.",
        )
    ]

    # Step 1: place R + LED.
    out, err = await call_tool(ctx, "plan_step_begin", step=1)
    assert not err, out
    assert len(rig.snapshots) == 1  # the pre-plan turn snapshot (roll back to start)
    r = await place(rig, "Device:R", (x + 2.54, y + 5.08), "R1")
    d = await place(rig, "Device:LED", (x + 20.32, y + 5.08), "D1")
    assert r["budget"] == {"used": 1, "limit": 3, "scope": "step", "step": 1}
    assert d["budget"]["used"] == 2
    # The new parts' open pins are new ERC errors: refused until justified (§8.3).
    out, err = await call_tool(ctx, "plan_step_done", step=1, summary="placed R1, D1")
    assert err and out["error"]["code"] == "erc_errors_open", out
    open_ids = [e["id"] for e in out["error"]["details"]["errors"]]
    assert open_ids
    just = {i: "the connections follow in step 2" for i in open_ids}
    out, err = await call_tool(
        ctx, "plan_step_done", step=1, summary="placed R1, D1", erc_justifications=just
    )
    assert not err, out
    assert sorted(out["erc"]["justified"]) == sorted(open_ids)
    assert out["change_count"] >= 2 and out["next_step"] == 2
    cp1 = out["checkpoint_id"]
    step1_files = compute_hashes(project)

    # Between steps a mutating call is refused (plan_step_required).
    pa, _ = await call_tool(ctx, "sch_get_pin", pin="R1.2")
    pb, _ = await call_tool(ctx, "sch_get_pin", pin="D1.2")
    route = [pa["at_mm"], [pa["at_mm"][0], pa["at_mm"][1] + 7.62],
             [pb["at_mm"][0], pa["at_mm"][1] + 7.62], pb["at_mm"]]  # fmt: skip
    between, is_err = await call_tool(ctx, "sch_add_wire", points=route)
    assert is_err and between["error"]["code"] == "plan_step_required"

    # Step 2: connect.
    out, err = await call_tool(ctx, "plan_step_begin", step=2)
    assert not err, out
    wired, err = await call_tool(ctx, "sch_add_wire", points=route)
    assert not err, wired
    assert wired["connectivity_verified"] is True
    # Measured against step 1's checkpoint: the joined pins resolve errors, none is new.
    out, err = await call_tool(ctx, "plan_step_done", step=2, summary="connected")
    assert not err, out
    assert out["erc"]["new_errors"] == 0 and out["erc"]["resolved"] >= 1
    assert out["plan_status"] == "done" and out["next_step"] is None
    plan = await rig.plans.repo.get(project.id, plan_id)
    assert [st.status for st in plan.steps] == ["done", "done"]
    assert plan.steps[0].checkpoint_id == cp1

    # kicad-cli's netlist of a saved copy: R1.2 and D1.2 share a net.
    root = project.sch_file
    assert root is not None
    dest = tmp_path / "copy" / root.name
    await s.save_copy("sch", dest)
    nets = cli_pin_nets(cli_netlist(s.settings.kicad_cli, dest, tmp_path))
    assert nets[("R1", "2")] == nets[("D1", "2")]

    # Restoring step 1's checkpoint gives byte-identical files to those of step 1.
    from copperchat.history.restore import restore_snapshot

    await restore_snapshot(rig.history, project, cp1, conversation_id=TEST_CID)
    assert compute_hashes(project) == step1_files
