"""A failed edit that could not be undone exactly ends the turn like a KiCad failure
(E2 Task 5, spec §5.1 step 8): one error row, interrupt, reason "error"."""

from __future__ import annotations

from typing import Any, cast

from copperchat.agent.engine import RunHooks, TurnInfo, TurnOutcome
from copperchat.agent.fake import Text, Tool
from copperchat.db.repos.agent_state import RunRepo
from copperchat.db.repos.messages import MessageRepo
from copperchat.errors import DocumentInconsistent
from copperchat.tools import ToolContext, ToolResult, edit_spec

PID = "a1b2c3d4e5f6"


class RecordingHooks(RunHooks):
    def __init__(self) -> None:
        self.outcomes: list[TurnOutcome] = []

    async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
        self.outcomes.append(outcome)


def broken_edit(*, auto_restore: bool = False) -> Any:
    async def prepare(ctx: ToolContext, args: dict[str, Any]) -> Any:
        return cast(Any, object())

    async def apply(ctx: ToolContext, cs: Any) -> ToolResult:
        raise DocumentInconsistent("rollback diverged", auto_restore=auto_restore)

    return edit_spec("sch_broken", "d", {"type": "object"}, prepare, apply)


async def test_document_inconsistent_ends_turn_with_one_error_row(make_engine, db, until):
    hooks = RecordingHooks()
    engine, agent = await make_engine(
        lambda item: [Tool("sch_broken"), Text("after")] if item.text == "m1" else [Text("two")],
        specs=[broken_edit(auto_restore=True)],
        with_session=True,
        hooks=hooks,
    )
    m1, _ = await engine.submit_ex("a", "m1", [])
    await engine.submit_ex("b", "m2", [])
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)

    rows, _ = await MessageRepo(db).page(PID, limit=50)
    errors = [r for r in rows if r.kind == "error"]
    assert len(errors) == 1 and errors[0].metadata == {"code": "document_inconsistent"}
    assert not any(r.kind == "text" and r.content == "after" for r in rows)
    assert any(r.kind == "text" and r.content == "two" for r in rows)
    first = await MessageRepo(db).get(m1.id)
    assert first is not None and first.run_id is not None
    run = await RunRepo(db).get(first.run_id)
    assert run is not None and run["reason"] == "error"
    outcome = hooks.outcomes[0]
    assert outcome.document_inconsistent and outcome.auto_restore and outcome.mutated
    assert not outcome.kicad_failed
    assert not hooks.outcomes[1].document_inconsistent  # the next turn is unaffected
