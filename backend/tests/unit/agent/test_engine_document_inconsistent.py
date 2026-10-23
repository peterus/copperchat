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
CID = "c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0"


class RecordingHooks(RunHooks):
    def __init__(self) -> None:
        self.outcomes: list[TurnOutcome] = []

    async def on_turn_end(self, turn: TurnInfo, outcome: TurnOutcome) -> None:
        self.outcomes.append(outcome)


def saving_edit() -> Any:
    async def prepare(ctx: ToolContext, args: dict[str, Any]) -> Any:
        return cast(Any, object())

    async def apply(ctx: ToolContext, cs: Any) -> ToolResult:
        return ToolResult(data={"ok": True}, changed_docs={"sch"})

    return edit_spec("sch_ok", "d", {"type": "object"}, prepare, apply)


def broken_edit(*, last_save_partial: bool = False) -> Any:
    async def prepare(ctx: ToolContext, args: dict[str, Any]) -> Any:
        return cast(Any, object())

    async def apply(ctx: ToolContext, cs: Any) -> ToolResult:
        raise DocumentInconsistent("rollback diverged", last_save_partial=last_save_partial)

    return edit_spec("sch_broken", "d", {"type": "object"}, prepare, apply)


async def test_document_inconsistent_ends_turn_with_one_error_row(make_engine, db, until):
    hooks = RecordingHooks()
    engine, agent = await make_engine(
        lambda item: [Tool("sch_broken"), Text("after")] if item.text == "m1" else [Text("two")],
        specs=[broken_edit(last_save_partial=True)],
        with_session=True,
        hooks=hooks,
    )
    m1, _ = await engine.submit_ex(CID, "a", "m1", [])
    await engine.submit_ex(CID, "b", "m2", [])
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)

    rows, _ = await MessageRepo(db).page(PID, CID, limit=50)
    errors = [r for r in rows if r.kind == "error"]
    assert len(errors) == 1 and errors[0].metadata == {"code": "document_inconsistent"}
    assert not any(r.kind == "text" and r.content == "after" for r in rows)
    assert any(r.kind == "text" and r.content == "two" for r in rows)
    first = await MessageRepo(db).get(m1.id)
    assert first is not None and first.run_id is not None
    run = await RunRepo(db).get(first.run_id)
    assert run is not None and run["reason"] == "error"
    outcome = hooks.outcomes[0]
    assert outcome.document_inconsistent and outcome.last_save_partial and outcome.mutated
    assert not outcome.kicad_failed
    assert not hooks.outcomes[1].document_inconsistent  # the next turn is unaffected


async def test_report_halts_further_edits_of_the_turn(make_engine, db):
    """RunEngine._on_document_inconsistent (also reached from the engine's cancel path,
    not only through the adapter) marks the turn's editing state halted."""
    from types import SimpleNamespace

    from copperchat.db.repos.editing_settings import EditingSettingsRepo
    from copperchat.editing.service import EditingService

    engine, _agent = await make_engine(lambda item: [Text("x")])
    svc = EditingService(db=db, broadcaster=None, settings_repo=EditingSettingsRepo(db))
    engine.editing = svc
    engine._turn = cast(
        Any,
        SimpleNamespace(info=SimpleNamespace(run_id="r1"), document_inconsistent=False,
                        last_save_partial=False),
    )  # fmt: skip
    engine._on_document_inconsistent(DocumentInconsistent("x", last_save_partial=True))
    assert svc.turn(PID, "r1").halted
    assert engine._turn.document_inconsistent and engine._turn.last_save_partial
    engine._turn = None


async def test_failed_per_edit_save_ends_turn_with_one_error_row(make_engine, db, until):
    """#6 (spec §5.3): a save after a successful edit that fails is reported like a
    KiCad failure: one error row naming the save, interrupt, reason "error"."""

    async def disk_full(doc: str) -> None:
        raise OSError("No space left on device")

    hooks = RecordingHooks()
    engine, agent = await make_engine(
        lambda item: [Tool("sch_ok"), Text("after")] if item.text == "m1" else [Text("two")],
        specs=[saving_edit()],
        with_session=True,
        session_save=disk_full,
        hooks=hooks,
    )
    m1, _ = await engine.submit_ex(CID, "a", "m1", [])
    await engine.submit_ex(CID, "b", "m2", [])
    await until(lambda: len(agent.prompts) == 2 and not engine.is_busy)

    rows, _ = await MessageRepo(db).page(PID, CID, limit=50)
    errors = [r for r in rows if r.kind == "error"]
    assert len(errors) == 1 and errors[0].metadata == {"code": "save_failed"}
    assert "No space left on device" in errors[0].content
    assert not any(r.kind == "text" and r.content == "after" for r in rows)
    first = await MessageRepo(db).get(m1.id)
    assert first is not None and first.run_id is not None
    run = await RunRepo(db).get(first.run_id)
    assert run is not None and run["reason"] == "error"
    outcome = hooks.outcomes[0]
    assert outcome.changed_docs == {"sch"} and not outcome.document_inconsistent
