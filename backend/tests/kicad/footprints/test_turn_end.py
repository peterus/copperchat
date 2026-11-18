"""`fp_create` inside an agent turn on a real api-server (F4a Task 5, spec §9.1, §10,
§12): the run-engine rig of `tests/kicad/edit/test_plan_flow.py` (HistoryService with
the library locator and listener, EditingService, PlanService + PlanGate, the history
run hooks) drives turn start -> tool calls -> turn end -> next turn start.

CopperChat's own library writes (also a compensated call's) are never reported as an
external change; a server restart right after `apply` keeps the call a success (the
generation check is skipped for library-only results); four footprints fit the turn
budget, the fifth is refused before any write; the change log lists the writes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from copperchat.agent.engine import TurnInfo, TurnOutcome
from copperchat.agent.sdk_tools import build_sdk_tool_list
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.editing.service import EditingService
from copperchat.history.hooks import HistoryHooks
from copperchat.history.service import HistoryService
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.kicad_tools.footprints.create import apply_footprint_create, prepare_create
from copperchat.kicad_tools.footprints.engine import make_library_listener, make_library_locator
from copperchat.kicad_tools.footprints.read import fp_get
from copperchat.kicad_tools.footprints.specs import FP_CREATE_SCHEMA
from copperchat.plans.gate import PlanGate
from copperchat.plans.service import PlanService
from copperchat.projects import discover_projects
from copperchat.tools import ToolContext, ToolResult, edit_spec
from tests.kicad.edit.test_plan_flow import Runtime
from tests.kicad.footprints.conftest import make_repo
from tests.kicad.footprints.test_fp_create import (
    LIB_ID,
    NAME,
    NICK,
    journal,
    l201_args,
    lib_dir,
    run_create,
    tree,
)
from tests.support.conversations import TEST_CID, ensure_test_conversation
from tests.support.edit import call_tool
from tests.support.history import RecordingBroadcaster
from tests.support.kicad_project import real_settings

pytestmark = pytest.mark.kicad

RUN1, RUN2 = "run-fp-1", "run-fp-2"


@dataclass
class Rig:
    session: KicadSession
    history: HistoryService
    editing: EditingService
    plans: PlanService
    runtime: Runtime
    hooks: HistoryHooks
    root: Path
    db: Database
    snapshotted: set[str] = field(default_factory=set)

    @staticmethod
    def turn(run_id: str) -> TurnInfo:
        return TurnInfo(run_id, 1, "make the L201 footprint", {}, TEST_CID)

    def ctx(self, run_id: str) -> ToolContext:
        """A tool context as the run engine builds it (`_make_ctx`): one turn snapshot
        per run, the library-changed hook, a fresh `library_touched`."""
        info = self.turn(run_id)

        async def on_mutation() -> None:
            if run_id not in self.snapshotted:
                self.snapshotted.add(run_id)
                await self.hooks.on_first_mutation(info)

        return ToolContext(
            project=self.session.project,
            session=self.session,
            settings=self.session.settings,
            run_id=run_id,
            on_mutation=on_mutation,
            editing=self.editing,
            library_touched=[],
            on_library_changed=self.hooks.on_library_changed,
        )

    async def end_turn(self, run_id: str) -> None:
        outcome = TurnOutcome(reason="completed", conversation_id=TEST_CID, mutated=True)
        await self.hooks.on_turn_end(self.turn(run_id), outcome)

    def reported(self) -> list[Any]:
        """Everything the turn hooks told the user or the agent."""
        eng = self.runtime.engine
        return [*eng.notices, *eng.errors, *eng.notes]

    def forget_reports(self) -> None:
        eng = self.runtime.engine
        eng.notices.clear()
        eng.errors.clear()
        eng.notes.clear()


@pytest.fixture
async def rig(make_settings: Callable[..., Settings], tmp_path: Path) -> AsyncIterator[Rig]:
    settings = real_settings(make_settings)
    root = make_repo(settings)
    main = (root / "hardware" / "main").resolve()
    project = next(p for p in discover_projects(settings.projects_root) if p.dir.resolve() == main)
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
    # as install_history wires it (F1 <-> F3, FP-C6)
    history.locate_library = make_library_locator(settings, sessions)
    history.library_listeners.append(make_library_listener(settings, sessions))
    editing = EditingService(
        db=db, broadcaster=None, settings_repo=EditingSettingsRepo(db), history=history
    )
    plans = PlanService(db=db, editing=editing, history=history, runtime=runtime, broadcaster=None)
    editing.gate = PlanGate(editing, plans)
    editing.plans = plans
    history.on_external_change.append(plans.on_external_change)  # as install_plans
    try:
        session = await sessions.open(project)
        hooks = HistoryHooks(history, project, cast(Any, runtime.engine))
        await hooks.on_engine_start()
        yield Rig(session, history, editing, plans, runtime, hooks, root, db)
    finally:
        await sessions.close_all()
        await db.close()


def _step(expected: int) -> dict[str, Any]:
    return {
        "title": "Create the L201 footprint",
        "intent": "the Bourns SRN3012BTA land pattern from its datasheet",
        "expected_changes": expected,
        "touches": {"refs": ["L201"], "nets": []},
        "verification": "fp_check passes on the read-back",
    }


async def _change_log(rig: Rig, run_id: str) -> list[dict[str, Any]]:
    out, err = await call_tool(rig.ctx(run_id), "sch_changes", scope="turn")
    assert not err, out
    changes: list[dict[str, Any]] = out["changes"]
    return changes


async def test_turn_end_after_fp_create(rig: Rig) -> None:
    """Inside a running plan step: the turn-end autosave succeeds, the next turn start
    reports no external change and the plan keeps running; the change log lists the
    row and the footprint (ref = lib id, after = the read-back's geometry hash)."""
    project = rig.session.project
    await rig.hooks.on_turn_start(rig.turn(RUN1))
    ctx = rig.ctx(RUN1)
    out, err = await call_tool(ctx, "plan_propose", title="L201", summary="fp", steps=[_step(5)])
    assert not err, out
    plan_id = out["plan_id"]
    await rig.plans.approve(project, plan_id)
    out, err = await call_tool(rig.ctx(RUN1), "plan_step_begin", step=1)
    assert not err, out
    rig.forget_reports()  # the plan's own notices

    out, err = await call_tool(rig.ctx(RUN1), "fp_create", **l201_args())
    if err:
        pytest.fail(json.dumps(out, indent=1))
    assert out["created"] == [LIB_ID] and out["library_created"] is True
    assert out["budget"] == {"used": 5, "limit": 7, "scope": "step", "step": 1}
    assert (lib_dir(rig.root) / f"{NAME}.kicad_mod").is_file()

    got = await fp_get(rig.ctx(RUN1), {"lib_id": LIB_ID})
    assert got.data is not None
    log = await _change_log(rig, RUN1)
    assert [(c["op"], c["kind"], c.get("ref")) for c in log] == [
        ("add", "fp_lib_table_row_added", NICK),
        ("add", "footprint_created", LIB_ID),
    ]
    assert log[1]["after"] == {"geometry_hash": got.data["geometry_hash"]}
    assert {c["tool"] for c in log} == {"fp_create"}
    plan = await rig.plans.repo.get(project.id, plan_id)
    assert all(c.get("plan_step_id") == plan.steps[0].id for c in log)

    await rig.end_turn(RUN1)
    assert rig.reported() == []
    await rig.hooks.on_turn_start(rig.turn(RUN2))
    assert rig.reported() == [], rig.reported()
    plan = await rig.plans.repo.get(project.id, plan_id)
    assert plan.status == "running"


@pytest.mark.parametrize("stage", ["ensure_library", "rebuild_saved"])
async def test_failed_create_no_external_change(rig: Rig, stage: str) -> None:
    """A compensated fp_create (S1 added the row and created the directory, both
    undone) rewrote fp-lib-table and wrote/deleted a footprint: the next turn start
    reports nothing, and nothing went into the change log."""
    await rig.hooks.on_turn_start(rig.turn(RUN1))
    out, err = await run_create(rig.ctx(RUN1), l201_args(), fail_after=stage)
    assert err and out["error"]["code"] == "injected", out
    rows = await journal(rig.db, rig.session)
    assert rows and all(r.state == "compensated" for r in rows)
    assert not lib_dir(rig.root).exists()
    assert await _change_log(rig, RUN1) == []

    await rig.end_turn(RUN1)
    await rig.hooks.on_turn_start(rig.turn(RUN2))
    assert rig.reported() == [], rig.reported()


async def test_restart_after_apply_is_success(rig: Rig) -> None:
    """The server restarts right after `apply` returned (before the adapter's generation
    check): the footprint is saved and journalled `done`, so the call is a success
    (spec §9.1 generation skip) and the file is on disk; nothing is reported later."""
    await rig.hooks.on_turn_start(rig.turn(RUN1))
    generation = rig.session.generation

    async def apply_then_restart(ctx: ToolContext, cs: Any) -> ToolResult:
        result = await apply_footprint_create(ctx, cs)
        await ctx.session.restart()
        return result

    spec = edit_spec("fp_create", "d", FP_CREATE_SCHEMA, prepare_create, apply_then_restart)
    raw = await build_sdk_tool_list([spec], lambda: rig.ctx(RUN1))[0].handler(l201_args())
    out = json.loads(raw["content"][0]["text"])
    if raw.get("is_error"):
        pytest.fail(json.dumps(out, indent=1))
    assert rig.session.generation != generation
    assert out["created"] == [LIB_ID]
    assert (lib_dir(rig.root) / f"{NAME}.kicad_mod").is_file()
    rows = await journal(rig.db, rig.session)
    assert [(r.op, r.state) for r in rows] == [("row_add", "done"), ("create", "done")]
    got = await fp_get(rig.ctx(RUN1), {"lib_id": LIB_ID})  # the fresh server has it
    assert got.data is not None and got.data["review"] == "needs_user_review"

    await rig.end_turn(RUN1)
    await rig.hooks.on_turn_start(rig.turn(RUN2))
    assert rig.reported() == [], rig.reported()


async def test_budget_weight(rig: Rig) -> None:
    """fp_create costs 5 of the turn budget of 20 (spec §12): four fit, the fifth is
    refused (plan_required) before any write; the change log has four
    `footprint_created` rows (and the one row added by the first call)."""
    await rig.hooks.on_turn_start(rig.turn(RUN1))
    names = [f"L_Budget_{i}" for i in range(1, 6)]
    for i, name in enumerate(names[:4], start=1):
        out, err = await call_tool(rig.ctx(RUN1), "fp_create", **l201_args(name=name))
        if err:
            pytest.fail(json.dumps(out, indent=1))
        assert out["budget"] == {"used": 5 * i, "limit": 20}
    files, rows = tree(rig.root), await journal(rig.db, rig.session)

    out, err = await call_tool(rig.ctx(RUN1), "fp_create", **l201_args(name=names[4]))
    assert err and out["error"]["code"] == "plan_required", out
    assert out["error"]["details"] == {"needed": 5, "used": 20, "limit": 20}
    assert tree(rig.root) == files
    assert await journal(rig.db, rig.session) == rows

    log = await _change_log(rig, RUN1)
    created = [c["ref"] for c in log if c["kind"] == "footprint_created"]
    assert created == [f"{NICK}:{n}" for n in names[:4]]
    assert [c["kind"] for c in log].count("fp_lib_table_row_added") == 1
    assert all(c["op"] == "add" for c in log)

    await rig.end_turn(RUN1)
    await rig.hooks.on_turn_start(rig.turn(RUN2))
    assert rig.reported() == [], rig.reported()
