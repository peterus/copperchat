"""HistoryHooks after a failed edit that could not be undone exactly (E2 Task 5,
spec §5.1 step 8 / §5.4): no autosave, reopen from disk, recovery notice naming the
last save, optional auto-restore of the turn snapshot."""

from __future__ import annotations

from typing import Any, cast

import pytest

from copperchat.agent.engine import TurnOutcome
from copperchat.history.store import compute_hashes
from copperchat.kicad_session import KicadSession, KicadUnavailable
from tests.support.history import other_board, replace_with_kicad_file
from tests.unit.history.conftest import Opened
from tests.unit.history.test_history_hooks import done, turn


def inconsistent(*docs: str, auto_restore: bool = False) -> TurnOutcome:
    return TurnOutcome(
        reason="error",
        changed_docs=frozenset(cast(Any, docs)),
        mutated=True,
        document_inconsistent=True,
        auto_restore=auto_restore,
    )


@pytest.fixture
def reopens(opened: Opened, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = opened.env.sessions.reopen

    async def recording(project: Any) -> KicadSession:
        calls.append(project.id)
        return await real(project)

    monkeypatch.setattr(opened.env.sessions, "reopen", recording)
    return calls


async def _mutating_turn(opened: Opened, run_id: str) -> Any:
    t = turn(run_id)
    await opened.hooks.on_turn_start(t)
    await opened.hooks.on_first_mutation(t)
    return t


async def test_no_autosave_and_session_reopened(opened: Opened, reopens: list[str]) -> None:
    env = opened.env
    t = await _mutating_turn(opened, "run-di1")
    await opened.hooks.on_turn_end(t, inconsistent("sch"))
    assert env.saved == []  # never autosave the inconsistent in-memory document
    assert reopens == [opened.project.id]
    current = env.sessions.current()
    assert current is not None and current is not opened.session and not current.closed
    assert opened.session.closed
    assert env.engine.errors == []  # the engine owns the error row (X18)


async def test_recovery_notice_names_last_save_time(opened: Opened, reopens: list[str]) -> None:
    env = opened.env
    await opened.session.save("sch")
    stamp = opened.session.last_saved_at
    assert stamp is not None
    env.saved.clear()
    t = await _mutating_turn(opened, "run-di2")
    await opened.hooks.on_turn_end(t, inconsistent("sch"))
    snap = await env.svc.store(opened.project).latest_for_run("run-di2")
    assert snap is not None
    text, meta = env.engine.notices[-1]
    assert stamp in text and "changes after that are lost" in text
    assert meta == {"action": "restore", "snapshotId": snap.id}
    assert env.saved == []


async def test_recovery_notice_without_a_save_says_last_save(opened: Opened) -> None:
    t = await _mutating_turn(opened, "run-di3")
    await opened.hooks.on_turn_end(t, inconsistent("sch"))
    text, _ = opened.env.engine.notices[-1]
    assert "the last save" in text and "changes after that are lost" in text


async def test_kicad_failed_editing_turn_uses_same_wording(opened: Opened) -> None:
    env = opened.env
    await opened.session.save("pcb")
    stamp = opened.session.last_saved_at
    assert stamp is not None
    t = await _mutating_turn(opened, "run-kf")
    env.server.crash()
    with pytest.raises(KicadUnavailable):
        await opened.session.call(lambda h: h.kicad.get_version())
    await opened.hooks.on_turn_end(t, done("sch", reason="error", failed=True))
    snap = await env.svc.store(opened.project).latest_for_run("run-kf")
    assert snap is not None
    text, meta = env.engine.notices[-1]
    assert stamp in text and "changes after that are lost" in text
    assert meta == {"action": "restore", "snapshotId": snap.id}


async def test_kicad_failed_turn_without_changes_keeps_old_text(opened: Opened) -> None:
    env = opened.env
    t = await _mutating_turn(opened, "run-kf2")  # snapshot taken, but nothing changed
    env.server.crash()
    outcome = TurnOutcome(reason="error", kicad_failed=True)
    await opened.hooks.on_turn_end(t, outcome)
    text, _ = env.engine.notices[-1]
    assert "changes after that are lost" not in text and "snapshot" in text


async def test_auto_restore_restores_turn_snapshot(opened: Opened, reopens: list[str]) -> None:
    env, project = opened.env, opened.project
    assert project.pcb_file is not None
    before = compute_hashes(project)
    t = await _mutating_turn(opened, "run-ar")
    replace_with_kicad_file(project.pcb_file, other_board())  # the partial edit on disk
    env.engine.is_busy = True  # the turn is still ending: no busy check here
    await opened.hooks.on_turn_end(t, inconsistent("pcb", auto_restore=True))
    snap = await env.svc.store(project).latest_for_run("run-ar")
    assert snap is not None
    assert compute_hashes(project) == before
    assert env.saved == []
    assert len(reopens) == 1  # the restore's reload is the only reopen
    text, meta = env.engine.notices[-1]
    assert meta == {"action": "restored", "snapshotId": snap.id}
    assert "before this turn" in text
    assert env.engine.errors == []


async def test_auto_restore_without_snapshot_posts_recovery_notice(
    opened: Opened, reopens: list[str]
) -> None:
    env = opened.env
    t = turn("run-ns")
    await opened.hooks.on_turn_start(t)
    await opened.hooks.on_turn_end(t, inconsistent("sch", auto_restore=True))
    assert reopens == [opened.project.id]
    text, meta = env.engine.notices[-1]
    assert "changes after that are lost" in text
    assert meta is not None and meta.get("action") != "restored"


async def test_failed_reopen_is_reported_not_raised(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = opened.env

    async def broken(project: Any) -> KicadSession:
        raise KicadUnavailable("api-server did not start")

    monkeypatch.setattr(env.sessions, "reopen", broken)
    t = await _mutating_turn(opened, "run-rf")
    await opened.hooks.on_turn_end(t, inconsistent("sch"))
    assert [m for _, m in env.engine.errors] == [{"code": "kicad_restart_failed"}]
    assert env.saved == []
