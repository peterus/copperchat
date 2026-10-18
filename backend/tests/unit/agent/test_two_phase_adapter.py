"""Two-phase mutating tool adapter, turn state and budget gate (E2 Task 2, Review Focus 4)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.agent.engine import ChainedHooks, RunHooks, TurnInfo, TurnOutcome
from copperchat.agent.sdk_tools import build_sdk_tool_list
from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.editing.gate import BudgetGate
from copperchat.editing.service import EditingService
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical, NetIntent, SheetRef
from copperchat.projects import Project
from copperchat.tools import ToolContext, ToolResult, ToolSpec, edit_spec, registry
from copperchat.tools.registry import ToolRegistryError

PID = "a1b2c3d4e5f6"
SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
SHEET = SheetRef(path="/root", page="1", file="demo.kicad_sch", name="demo")


def _project(tmp_path: Path) -> Project:
    return Project(
        id=PID,
        name="demo",
        rel_path="demo/demo.kicad_pro",
        dir=tmp_path,
        pro_file=tmp_path / "demo.kicad_pro",
        sch_file=None,
        pcb_file=None,
    )


class LoggingGate(BudgetGate):
    def __init__(self, service: EditingService, log: list[str]) -> None:
        super().__init__(service)
        self.log = log

    async def before_prepare(self, ctx: ToolContext, tool: str) -> None:
        self.log.append("before_prepare")
        await super().before_prepare(ctx, tool)

    async def check_budget(self, ctx: ToolContext, cs: ChangeSet) -> None:
        self.log.append("check_budget")
        await super().check_budget(ctx, cs)

    async def record(self, ctx: ToolContext, cs: ChangeSet) -> None:
        self.log.append("record")
        await super().record(ctx, cs)


class Harness:
    def __init__(self, tmp_path: Path, db: Database) -> None:
        self.log: list[str] = []
        self.settings = EditingSettingsRepo(db)
        self.svc = EditingService(db=db, broadcaster=None, settings_repo=self.settings)
        self.svc.gate = LoggingGate(self.svc, self.log)
        self.read_only = False
        self.tmp_path = tmp_path

    def ctx(self) -> ToolContext:
        async def on_mutation() -> None:
            self.log.append("on_mutation")

        return ToolContext(
            project=_project(self.tmp_path),
            session=cast(Any, SimpleNamespace(read_only=self.read_only)),
            settings=cast(Any, None),
            run_id="run1",
            on_mutation=on_mutation,
            editing=self.svc,
        )

    def spec(
        self,
        name: str = "sch_fake",
        *,
        changes: int = 1,
        deleted: list[str] | None = None,
        placed: list[str] | None = None,
    ) -> ToolSpec:
        async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
            self.log.append("prepare")
            return ChangeSet(
                tool=name,
                sheet=SHEET,
                stages=[],
                pre_images={},
                intent=NetIntent(),
                logical=Logical(int(args.get("changes", changes)), list(deleted or [])),
            )

        async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
            self.log.append("apply")
            cs.logical.placed_symbols = list(placed or [])
            return ToolResult(data={"ok": True, "budget": None}, changed_docs={"sch"})

        return edit_spec(name, "d", SCHEMA, prepare, apply)

    async def call(self, spec: ToolSpec, **args: Any) -> dict[str, Any]:
        tool = build_sdk_tool_list([spec], self.ctx)[0]
        raw = await tool.handler(args)
        payload: dict[str, Any] = json.loads(raw["content"][0]["text"])
        if raw.get("is_error"):
            return {"error": payload["error"]}
        return payload

    @property
    def state(self):  # type: ignore[no-untyped-def]
        return self.svc.turn(PID, "run1")


@pytest.fixture
def h(tmp_path: Path, db: Database) -> Harness:
    return Harness(tmp_path, db)


async def test_read_only_refused_before_prepare_and_snapshot(h: Harness) -> None:
    h.read_only = True
    out = await h.call(h.spec())
    assert out["error"]["code"] == "read_only"
    assert h.log == []


async def test_budget_refusal_takes_no_snapshot(h: Harness) -> None:
    out = await h.call(h.spec(changes=21))
    assert out["error"]["code"] == "plan_required"
    assert "plan_propose" in out["error"]["suggestion"]
    assert "on_mutation" not in h.log and "apply" not in h.log
    assert h.state.logical_used == 0


async def test_order_prepare_budget_snapshot_apply(h: Harness) -> None:
    out = await h.call(h.spec(changes=2))
    assert h.log == ["before_prepare", "prepare", "check_budget", "on_mutation", "apply", "record"]
    assert out["budget"] == {"used": 2, "limit": 20}


async def test_budget_accumulates_over_calls(h: Harness) -> None:
    spec = h.spec()
    assert "error" not in await h.call(spec, changes=8)
    assert "error" not in await h.call(spec, changes=8)
    out = await h.call(spec, changes=5)
    assert out["error"]["code"] == "plan_required"
    assert out["error"]["details"] == {"needed": 5, "used": 16, "limit": 20}
    assert h.state.logical_used == 16


async def test_custom_budget_from_settings(h: Harness) -> None:
    await h.settings.put(PID, change_budget=3)
    assert (await h.call(h.spec(changes=4)))["error"]["code"] == "plan_required"


async def test_deleting_own_placement_is_free(h: Harness) -> None:
    await h.call(h.spec("place", placed=["u"]))
    assert h.state.placed_this_turn == {"u"} and h.state.logical_used == 1
    out = await h.call(h.spec("delete", deleted=["u"]))
    assert "error" not in out
    assert h.state.logical_used == 0
    assert h.state.deleted_preexisting == 0
    assert h.state.placed_this_turn == set()


async def test_fourth_preexisting_delete_refused(h: Harness) -> None:
    for uid in ("a", "b", "c"):
        assert "error" not in await h.call(h.spec("delete", deleted=[uid]))
    out = await h.call(h.spec("delete", deleted=["d"]))
    assert out["error"]["code"] == "plan_required"
    assert h.state.deleted_preexisting == 3
    assert h.log.count("on_mutation") == 3


async def test_power_symbol_deletes_not_limited(h: Harness) -> None:
    # power symbols never appear in Logical.deleted_symbols (E-C6)
    for _ in range(5):
        assert "error" not in await h.call(h.spec("delete", changes=1, deleted=[]))
    assert h.state.deleted_preexisting == 0


async def test_policy_never_has_no_budget(h: Harness) -> None:
    await h.settings.put(PID, plan_policy="never")
    out = await h.call(h.spec(changes=100, deleted=["a", "b", "c", "d"]))
    assert "error" not in out
    assert out["budget"] is None


async def test_policy_always_requires_step(h: Harness) -> None:
    await h.settings.put(PID, plan_policy="always")
    out = await h.call(h.spec())
    assert out["error"]["code"] == "plan_step_required"
    assert h.log == ["before_prepare"]
    h.state.active_step_id = "step1"
    assert "error" not in await h.call(h.spec())


async def test_note_call_counts_every_tool(h: Harness) -> None:
    async def read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(data={})

    reader = ToolSpec("sch_list_symbols", "d", SCHEMA, read)
    render = ToolSpec("render_view", "d", SCHEMA, read)
    await h.call(reader)
    await h.call(render, doc="pcb")
    assert h.state.call_seq == 2 and h.state.last_render_seq is None
    await h.call(render, doc="sch")
    assert h.state.last_render_seq == 3
    await h.call(h.spec())
    assert h.state.call_seq == 4 and h.state.last_mutation_seq == 4


async def test_two_phase_handler_is_never_called_directly(h: Harness) -> None:
    with pytest.raises(RuntimeError):
        await h.spec().handler(h.ctx(), {})


async def test_chained_hooks_call_in_order_and_propagate_first_error() -> None:
    calls: list[str] = []

    class Hook(RunHooks):
        def __init__(self, name: str, fail: bool = False) -> None:
            self.name, self.fail = name, fail

        async def on_first_mutation(self, turn: TurnInfo) -> None:
            calls.append(f"{self.name}.mutation")
            if self.fail:
                raise RuntimeError(self.name)

        async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
            calls.append(f"{self.name}.end")
            if self.fail:
                raise RuntimeError(self.name)

    turn = TurnInfo(run_id="r", message_id=1, text="t", metadata={})
    chain = ChainedHooks([Hook("a"), Hook("b", fail=True), Hook("c", fail=True)])
    with pytest.raises(RuntimeError, match=r"^b$"):
        await chain.on_first_mutation(turn)
    assert calls == ["a.mutation", "b.mutation"]  # a failed snapshot stops the chain
    calls.clear()
    with pytest.raises(RuntimeError, match=r"^b$"):
        await chain.on_turn_end(turn, TurnOutcome(reason="done"))
    assert calls == ["a.end", "b.end", "c.end"]  # every turn-end hook runs


def test_two_phase_spec_requires_apply_and_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys
    import types

    async def prepare(ctx: ToolContext, args: dict[str, Any]) -> Any:
        return None

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult()

    bad = [
        ToolSpec(
            "half", "d", SCHEMA, handler, mutating=True, changes_documents=True, prepare=prepare
        ),
        ToolSpec("flags", "d", SCHEMA, handler, prepare=prepare, apply=cast(Any, prepare)),
    ]
    for spec in bad:
        module = types.ModuleType("fake_two_phase")
        module.TOOL_SPECS = [spec]  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "fake_two_phase", module)
        monkeypatch.setattr(registry, "_PROVIDERS", ("fake_two_phase",))
        with pytest.raises(ToolRegistryError, match="two-phase"):
            registry.all_specs()


async def test_own_placement_delete_allowed_at_the_limit(h: Harness) -> None:
    await h.call(h.spec("place", placed=["u"]))
    await h.call(h.spec(changes=19))
    assert h.state.logical_used == 20
    assert "error" not in await h.call(h.spec("delete", deleted=["u"]))
    assert h.state.logical_used == 19


async def test_document_inconsistent_halts_further_edits(h: Harness) -> None:
    from copperchat.errors import DocumentInconsistent

    seen: list[Exception] = []

    async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
        return ChangeSet("sch_bad", SHEET, [], {}, NetIntent(), Logical(1))

    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        raise DocumentInconsistent("boom")

    bad = edit_spec("sch_bad", "d", SCHEMA, prepare, apply)

    def ctx() -> ToolContext:
        c = Harness.ctx(h)
        c.on_document_inconsistent = seen.append
        return c

    tool = build_sdk_tool_list([bad], ctx)[0]
    raw = await tool.handler({})
    assert raw["is_error"] and len(seen) == 1
    assert json.loads(raw["content"][0]["text"])["error"]["code"] == "document_inconsistent"
    h.log.clear()
    out = await h.call(h.spec())
    assert out["error"]["code"] == "document_inconsistent"
    assert h.log == []  # refused before the gate, prepare and the snapshot


async def test_failing_budget_record_does_not_fail_an_applied_call(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(ctx: ToolContext) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(h.svc.gate, "budget_info", broken)
    out = await h.call(h.spec())
    assert out["ok"] is True and out["budget"] is None


async def test_calls_without_run_share_no_state(h: Harness) -> None:
    ctx = h.ctx()
    ctx.run_id = None
    h.svc.note_call(ctx, "sch_list_symbols")
    assert h.svc.turn(PID, None).call_seq == 0
    assert h.svc.turn(PID, None) is not h.svc.turn(PID, None)


async def test_edit_lock_serialises_two_phase_calls(h: Harness) -> None:
    import asyncio

    inside: list[str] = []
    release = asyncio.Event()

    async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
        inside.append(f"prepare {args['n']}")
        if args["n"] == 1:
            await release.wait()
        return ChangeSet("sch_slow", SHEET, [], {}, NetIntent(), Logical(1))

    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        inside.append("apply")
        return ToolResult(data={})

    slow = edit_spec("sch_slow", "d", SCHEMA, prepare, apply)
    first = asyncio.create_task(h.call(slow, n=1))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(h.call(slow, n=2))
    await asyncio.sleep(0.05)
    assert inside == ["prepare 1"]  # the second call waits for the first one's record
    release.set()
    await asyncio.gather(first, second)
    assert inside == ["prepare 1", "apply", "prepare 2", "apply"]
