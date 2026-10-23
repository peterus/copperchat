"""HistoryHooks after a failed edit that could not be undone exactly (E2 Task 5,
spec §5.1 step 8 / §5.4): no autosave, reopen from disk, recovery notice naming the
last save (= the state just before the failed edit, #6), the turn snapshot and the
last plan-step checkpoint offered as optional undo."""

from __future__ import annotations

from typing import Any, cast

import pytest

from copperchat.agent.engine import TurnOutcome
from copperchat.changes.checkpoints import take_step_checkpoint
from copperchat.history.store import compute_hashes
from copperchat.kicad_session import KicadSession, KicadUnavailable
from tests.support.history import other_board, replace_with_kicad_file
from tests.unit.history.conftest import Opened
from tests.unit.history.test_history_hooks import done, turn


def inconsistent(*docs: str) -> TurnOutcome:
    return TurnOutcome(
        reason="error",
        changed_docs=frozenset(cast(Any, docs)),
        mutated=True,
        document_inconsistent=True,
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


async def test_recovery_notice_says_nothing_lost(opened: Opened, reopens: list[str]) -> None:
    """#6 (spec §5.4): every successful edit was saved, so the notice says the design is
    at the state just before the failed edit, nothing else was lost and no restore is
    needed; the turn snapshot stays offered as an optional undo."""
    env = opened.env
    await opened.session.save("sch")
    stamp = opened.session.last_saved_at
    assert stamp is not None
    env.saved.clear()
    t = await _mutating_turn(opened, "run-di2")
    await opened.hooks.on_turn_end(t, inconsistent("sch"))
    snap = await env.svc.store(opened.project).latest_for_run("run-di2")
    assert snap is not None
    (text, meta), (undo, undo_meta) = env.engine.notices[-2:]
    assert stamp in text and "just before the edit that failed" in text
    assert "nothing else was lost" in text and "no restore is needed" in text
    assert meta == {"action": "recovered"}
    assert undo.startswith("Optional:") and "undo every change of this turn" in undo
    assert undo_meta == {"action": "restore", "snapshotId": snap.id}
    assert env.saved == []


async def test_recovery_notice_without_a_save_says_last_save(opened: Opened) -> None:
    t = await _mutating_turn(opened, "run-di3")
    await opened.hooks.on_turn_end(t, inconsistent("sch"))
    text, _ = next(n for n in opened.env.engine.notices if n[1] == {"action": "recovered"})
    assert "its last save:" in text and "nothing else was lost" in text


async def test_kicad_failed_editing_turn_says_successful_edits_are_kept(opened: Opened) -> None:
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
    (text, meta), (_undo, undo_meta) = env.engine.notices[-2:]
    assert stamp in text and "nothing that succeeded was lost" in text
    assert "edit that failed" not in text  # KiCad failed, not necessarily an edit
    assert meta == {"action": "recovered"}
    assert undo_meta == {"action": "restore", "snapshotId": snap.id}


async def test_kicad_failed_turn_without_changes_keeps_old_text(opened: Opened) -> None:
    env = opened.env
    t = await _mutating_turn(opened, "run-kf2")  # snapshot taken, but nothing changed
    env.server.crash()
    outcome = TurnOutcome(reason="error", kicad_failed=True)
    await opened.hooks.on_turn_end(t, outcome)
    text, _ = env.engine.notices[-1]
    assert "nothing else was lost" not in text and "snapshot" in text


async def test_auto_restore_uses_last_save(opened: Opened, reopens: list[str]) -> None:
    """Owner decision (#6): the former auto-restore cases (a deleted symbol that cannot
    be re-placed losslessly, a deleted sheet symbol) reopen the last save too - schematic
    and board - never an older turn snapshot or plan-step checkpoint automatically."""
    env, project = opened.env, opened.project
    assert project.pcb_file is not None
    t = await _mutating_turn(opened, "run-ar")
    cp = await take_step_checkpoint(env.svc, project, "run-ar", "step-1", "Step 1")
    env.saved.clear()  # the checkpoint saves first
    replace_with_kicad_file(project.pcb_file, other_board())  # a later edit's save
    saved = compute_hashes(project)
    env.engine.is_busy = True  # the turn is still ending
    await opened.hooks.on_turn_end(t, inconsistent("sch", "pcb"))
    assert compute_hashes(project) == saved  # nothing older restored
    assert reopens == [project.id]
    assert env.saved == []
    snap = await env.svc.store(project).latest_for_run("run-ar")
    assert snap is not None
    metas = [m for _, m in env.engine.notices]
    assert {"action": "recovered"} in metas
    assert not any(m and m.get("action") == "restored" for m in metas)
    assert {"action": "restore", "snapshotId": snap.id} in metas  # optional undo
    assert metas[-1] == {"action": "restore", "snapshotId": cp.id, "checkpoint": True}
    assert env.engine.errors == []


async def test_partial_last_save_is_said_plainly(opened: Opened, reopens: list[str]) -> None:
    """A shared-sheet placement saved part of itself and could not undo it: the notice
    must not claim the state before the failed edit; it says the last save may still
    hold part of it and that the turn snapshot removes it (with this turn's edits)."""
    env = opened.env
    t = await _mutating_turn(opened, "run-psp")
    outcome = TurnOutcome(
        reason="error",
        changed_docs=frozenset({"sch"}),
        mutated=True,
        document_inconsistent=True,
        last_save_partial=True,
    )
    await opened.hooks.on_turn_end(t, outcome)
    assert reopens == [opened.project.id]
    snap = await env.svc.store(opened.project).latest_for_run("run-psp")
    assert snap is not None
    (text, meta), (undo, undo_meta) = env.engine.notices[-2:]
    assert "may still contain part of the edit that failed" in text
    assert "nothing else was lost" not in text and meta == {"action": "recovered"}
    assert "also undoes this turn's earlier edits" in undo
    assert undo_meta == {"action": "restore", "snapshotId": snap.id}


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
