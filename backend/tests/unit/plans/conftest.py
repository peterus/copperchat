"""Plan test harness: EditingService + PlanService + PlanGate on a real SQLite db, the
two-phase tool adapter with fake specs (as E2's adapter tests)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.agent.sdk_tools import build_sdk_tool_list
from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.editing.service import EditingService
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical, NetIntent, SheetRef
from copperchat.plans import machine
from copperchat.plans.gate import PlanGate
from copperchat.plans.model import Plan
from copperchat.plans.service import PlanService
from copperchat.projects import Project
from copperchat.tools import ToolContext, ToolResult, ToolSpec, edit_spec
from tests.support.history import FakeEngine, FakeRuntime, RecordingBroadcaster

PID = "a1b2c3d4e5f6"
RUN = "run1"
SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
SHEET = SheetRef(path="/root", page="1", file="demo.kicad_sch", name="demo")
NOW = "2026-10-18T10:00:00Z"


def step_spec(title: str = "step", expected: int = 10) -> dict[str, Any]:
    return {
        "title": title,
        "intent": "do it",
        "expected_changes": expected,
        "touches": {"refs": ["R1"], "nets": ["VCC"]},
        "verification": "look",
    }


def make_plan(expected: tuple[int, ...] = (10, 5), plan_id: str = "f" * 32) -> Plan:
    return machine.new_plan(
        plan_id=plan_id,
        project_id=PID,
        title="LED circuit",
        summary="R + LED",
        steps=machine.build_steps(plan_id, [step_spec(f"s{i}", e) for i, e in enumerate(expected)]),
        run_id="run0",
        now=NOW,
    )


class Harness:
    def __init__(self, tmp_path: Path, db: Database) -> None:
        self.db = db
        self.log: list[str] = []
        self.settings = EditingSettingsRepo(db)
        self.engine = FakeEngine()
        self.broadcaster = RecordingBroadcaster()
        self.editing = EditingService(db=db, broadcaster=None, settings_repo=self.settings)
        self.plans = PlanService(
            db=db,
            editing=self.editing,
            history=None,
            runtime=FakeRuntime(self.engine),
            broadcaster=self.broadcaster,
        )
        self.editing.gate = PlanGate(self.editing, self.plans)
        self.editing.plans = self.plans
        self.read_only = False
        self.tmp_path = tmp_path
        self.run_id: str | None = RUN

    def project(self) -> Project:
        return Project(
            id=PID,
            name="demo",
            rel_path="demo/demo.kicad_pro",
            dir=self.tmp_path,
            pro_file=self.tmp_path / "demo.kicad_pro",
            sch_file=None,
            pcb_file=None,
        )

    def ctx(self) -> ToolContext:
        async def on_mutation() -> None:
            self.log.append("on_mutation")

        return ToolContext(
            project=self.project(),
            session=cast(Any, SimpleNamespace(read_only=self.read_only)),
            settings=cast(Any, None),
            run_id=self.run_id,
            on_mutation=on_mutation,
            editing=self.editing,
        )

    def spec(self, name: str = "sch_fake", *, changes: int = 1) -> ToolSpec:
        async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
            self.log.append("prepare")
            return ChangeSet(
                tool=name,
                sheet=SHEET,
                stages=[],
                pre_images={},
                intent=NetIntent(),
                logical=Logical(int(args.get("changes", changes)), []),
            )

        async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
            self.log.append("apply")
            return ToolResult(data={"ok": True, "budget": None}, changed_docs={"sch"})

        return edit_spec(name, "d", SCHEMA, prepare, apply)

    def read_spec(self) -> ToolSpec:
        async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            self.log.append("read")
            return ToolResult(data={"ok": True})

        return ToolSpec("sch_read", "d", SCHEMA, handler)

    async def call(self, spec: ToolSpec, **args: Any) -> dict[str, Any]:
        tool = build_sdk_tool_list([spec], self.ctx)[0]
        raw = await tool.handler(args)
        payload: dict[str, Any] = json.loads(raw["content"][0]["text"])
        if raw.get("is_error"):
            return {"error": payload["error"]}
        return payload

    @property
    def state(self) -> Any:
        return self.editing.turn(PID, RUN)

    async def store(self, plan: Plan) -> Plan:
        return await self.plans.repo.insert(plan)

    async def running(self, expected: tuple[int, ...] = (10, 5)) -> Plan:
        """An approved plan whose step 1 was begun in this turn (no history)."""
        plan = machine.transition(make_plan(expected), "approve", now=NOW)
        await self.store(plan)
        await self.plans.step_begin(self.ctx(), 1)
        self.log.clear()  # step_begin's base snapshot (on_mutation)
        return await self.plans.repo.get(PID, plan.id)

    async def plan(self) -> Plan:
        p = await self.plans.repo.open_for(PID)
        assert p is not None
        return p

    async def set_status(self, plan: Plan, status: str) -> Plan:
        return await self.plans.repo.save(replace(plan, status=status))  # type: ignore[arg-type]


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "db.sqlite")
    yield database
    await database.close()


@pytest.fixture
def h(tmp_path: Path, db: Database) -> Harness:
    return Harness(tmp_path, db)
