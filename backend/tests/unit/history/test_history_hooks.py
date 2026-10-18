from typing import Any, cast

import pytest

from copperchat.agent.engine import TurnInfo, TurnOutcome
from copperchat.agent.sdk_tools import build_sdk_tool_list
from copperchat.history.store import compute_hashes
from copperchat.kicad_session import KicadUnavailable
from copperchat.tools import StructuredToolError, ToolContext, ToolResult, ToolSpec
from tests.unit.history.conftest import Opened


def turn(run_id: str = "run-1", text: str = "move R1 next to U1") -> TurnInfo:
    return TurnInfo(run_id=run_id, message_id=1, text=text, metadata={})


def done(*docs: str, reason: Any = "completed", failed: bool = False) -> TurnOutcome:
    return TurnOutcome(
        reason=reason,
        changed_docs=frozenset(cast(Any, docs)),
        mutated=bool(docs),
        kicad_failed=failed,
    )


async def test_engine_start_records_hashes_and_gitignore(opened: Opened) -> None:
    svc, project = opened.env.svc, opened.project
    assert await svc.hashes.get(project.id) == compute_hashes(project)
    assert (project.dir / ".kicad-agent" / ".gitignore").is_file()


async def test_test_only_mutating_tool_triggers_one_snapshot(opened: Opened) -> None:
    hooks, project, session = opened.hooks, opened.project, opened.session
    t = turn(text="  move R1\nnext to U1  ")
    await hooks.on_turn_start(t)
    fired = False

    async def on_mutation() -> None:  # Phase 5 engine semantics: first call per turn only
        nonlocal fired
        if not fired:
            fired = True
            await hooks.on_first_mutation(t)

    async def mutate(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(data={"ok": True}, changed_docs={"pcb"})

    spec = ToolSpec(
        name="test_mutate",
        description="test only",
        input_schema={"type": "object"},
        handler=mutate,
        mutating=True,
        changes_documents=True,
    )
    changed: set[str] = set()
    [tool] = build_sdk_tool_list(
        [spec],
        lambda: ToolContext(
            project=project,
            session=session,
            settings=cast(Any, None),
            run_id="run-1",
            on_mutation=on_mutation,
            on_documents_changed=changed.update,
        ),
    )
    await tool.handler({})
    await tool.handler({})
    snaps = await opened.env.svc.store(project).list()
    assert len(snaps) == 1 and snaps[0].run_id == "run-1" and snaps[0].has_preview
    assert snaps[0].label == "move R1 next to U1" and changed == {"pcb"}


async def test_failed_snapshot_fails_the_mutating_call(opened: Opened) -> None:
    async def broken(session: Any, doc: str) -> bytes:
        raise OSError("disk full")

    opened.env.svc.render_preview = broken  # type: ignore[assignment]
    ran: list[bool] = []

    async def mutate(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        ran.append(True)
        return ToolResult(changed_docs={"pcb"})

    spec = ToolSpec("test_mutate", "t", {"type": "object"}, mutate, mutating=True)
    [tool] = build_sdk_tool_list(
        [spec],
        lambda: ToolContext(
            project=opened.project,
            session=opened.session,
            settings=cast(Any, None),
            on_mutation=lambda: opened.hooks.on_first_mutation(turn()),
        ),
    )
    result = await tool.handler({})
    assert result["is_error"] is True and ran == []


async def test_snapshot_write_failure_is_a_structured_tool_error(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copperchat.history.store import HistoryStore

    async def broken(self: Any, *a: Any, **k: Any) -> Any:
        raise OSError("No space left on device")

    monkeypatch.setattr(HistoryStore, "snapshot", broken)
    with pytest.raises(StructuredToolError) as err:
        await opened.hooks.on_first_mutation(turn())
    assert "undo snapshot" in err.value.what and "Nothing was changed" in err.value.suggestion


async def test_turn_end_autosaves_changed_docs_and_publishes_view(opened: Opened) -> None:
    env, session = opened.env, opened.session
    session.view_version = 2
    await opened.hooks.on_turn_end(turn(), done("pcb"))
    assert env.saved == ["pcb"] and env.server.saved == ["ecc83-pp.kicad_pcb"]
    frame = env.broadcaster.frames[-1]
    assert frame.type == "view-updated" and frame.docs == ["pcb"] and frame.version == 3
    assert session.view_version == 3 and env.written == ["pcb"]
    assert await env.svc.hashes.get(opened.project.id) == compute_hashes(opened.project)
    assert env.engine.errors == [] and env.engine.notices == []


async def test_interrupted_and_rate_limited_turns_still_autosave(opened: Opened) -> None:
    env = opened.env
    await opened.hooks.on_turn_end(turn("a"), done("sch", reason="interrupted"))
    await opened.hooks.on_turn_end(turn("b"), done("pcb", reason="rate_limited"))
    assert env.saved == ["sch", "pcb"]
    assert [f.version for f in env.broadcaster.frames] == [1, 2]


async def test_turn_without_changes_does_nothing(opened: Opened) -> None:
    env = opened.env
    await opened.hooks.on_turn_end(turn(), done())
    assert env.saved == [] and env.broadcaster.frames == [] and opened.session.view_version == 0


async def test_read_only_session_never_autosaves_or_snapshots(opened: Opened) -> None:
    env = opened.env
    opened.session.read_only = True
    await opened.hooks.on_first_mutation(turn())
    await opened.hooks.on_turn_end(turn(), done("sch"))
    assert env.saved == [] and await env.svc.store(opened.project).list() == []
    assert env.broadcaster.frames == []


async def test_failed_autosave_posts_one_error_and_offers_restore(opened: Opened) -> None:
    env = opened.env
    t = turn("run-7")
    await opened.hooks.on_first_mutation(t)
    hashes = await env.svc.hashes.get(opened.project.id)
    env.save_error = KicadUnavailable("the KiCad api-server died during the call")
    await opened.hooks.on_turn_end(t, done("pcb"))
    assert [m for _, m in env.engine.errors] == [{"code": "autosave_failed"}]
    snap = (await env.svc.store(opened.project).list())[0]
    assert env.engine.notices[-1][1] == {"action": "restore", "snapshotId": snap.id}
    assert env.broadcaster.frames == []
    assert await env.svc.hashes.get(opened.project.id) == hashes


async def test_partial_autosave_records_hashes_and_publishes_saved_docs(opened: Opened) -> None:
    env = opened.env
    env.save_error, env.save_error_doc = KicadUnavailable("save timed out"), "sch"
    await opened.hooks.on_turn_end(turn(), done("pcb", "sch"))
    assert env.saved == ["pcb"]
    [(text, meta)] = env.engine.errors
    assert meta == {"code": "autosave_failed"} and "sch" in text and "pcb" not in text
    assert await env.svc.hashes.get(opened.project.id) == compute_hashes(opened.project)
    assert [f.docs for f in env.broadcaster.frames] == [["pcb"]]
    await opened.hooks.on_turn_start(turn("next"))
    assert not any(m and m.get("action") == "external-change" for _, m in env.engine.notices)


async def test_view_refresh_failure_is_not_an_autosave_failure(opened: Opened) -> None:
    env = opened.env

    async def broken(session: Any, doc: str) -> Any:
        raise OSError("view dir not writable")

    env.svc.write_view = broken  # type: ignore[assignment]
    await opened.hooks.on_turn_end(turn(), done("pcb"))
    assert env.saved == ["pcb"] and env.engine.errors == [] and env.engine.notices == []
    assert await env.svc.hashes.get(opened.project.id) == compute_hashes(opened.project)


async def test_history_registers_hash_listener(opened: Opened) -> None:
    """Every CopperChat save records the hashes (E1): also a save outside turn end."""
    from copperchat.history.store import compute_hashes_async

    svc, project = opened.env.svc, opened.project
    await svc.hashes.replace(project.id, {"stale.kicad_sch": "0" * 64})
    await opened.session.save("sch")
    assert await svc.hashes.get(project.id) == await compute_hashes_async(project)
