"""Step checkpoints in the history store (E8a Task 1, E-C10, spec §5.4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from copperchat.changes.checkpoints import take_step_checkpoint
from copperchat.db import Database
from copperchat.history import store as store_mod
from copperchat.history.store import HistoryStore, compute_hashes
from copperchat.kicad_session import KicadUnavailable
from copperchat.projects import agent_dir
from tests.support.history import (
    ECC83,
    HIERARCHY,
    copy_project,
    other_board,
    replace_with_kicad_file,
)
from tests.unit.history.conftest import Opened
from tests.unit.history.test_document_inconsistent import inconsistent
from tests.unit.history.test_history_hooks import done, turn


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "db.sqlite")
    yield d
    await d.close()


async def test_snapshot_kind_and_step_id(db: Database, tmp_path: Path) -> None:
    store = HistoryStore(db, copy_project(ECC83, tmp_path / "projects"))
    turn_snap = await store.snapshot("run-1", "turn")
    step = await store.snapshot("run-1", "step 1", kind="step", plan_step_id="s1")
    assert (turn_snap.kind, turn_snap.plan_step_id) == ("turn", None)
    assert (step.kind, step.plan_step_id) == ("step", "s1")
    got = await store.get(step.id)
    assert (got.kind, got.plan_step_id) == ("step", "s1")
    with pytest.raises(ValueError):
        await store.snapshot("run-1", "bad", kind="other")


async def test_latest_for_run_ignores_steps(db: Database, tmp_path: Path) -> None:
    store = HistoryStore(db, copy_project(ECC83, tmp_path / "projects"))
    turn_snap = await store.snapshot("run-1", "turn")
    step1 = await store.snapshot("run-1", "step 1", kind="step", plan_step_id="s1")
    step2 = await store.snapshot("run-1", "step 2", kind="step", plan_step_id="s2")
    latest = await store.latest_for_run("run-1")
    assert latest is not None and latest.id == turn_snap.id
    cp = await store.latest_step_checkpoint("run-1")
    assert cp is not None and cp.id == step2.id and step1.id != step2.id
    assert await store.latest_step_checkpoint("run-2") is None


async def test_prune_counts_turns_only_and_cascades_steps(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "HISTORY_KEEP", 3)
    project = copy_project(ECC83, tmp_path / "projects")
    store = HistoryStore(db, project)
    first = await store.snapshot("run-0", "t0")
    first_steps = [
        (await store.snapshot("run-0", f"s{i}", kind="step", plan_step_id=f"s{i}")).id
        for i in range(2)
    ]
    kept_turns = [(await store.snapshot(f"run-{i}", f"t{i}")).id for i in (1, 2)]
    kept_step = (await store.snapshot("run-2", "s", kind="step", plan_step_id="x")).id
    # Three turns: still nothing pruned, step checkpoints do not count.
    assert len(await store.list()) == 6
    newest = (await store.snapshot("run-3", "t3")).id
    listed = {s.id for s in await store.list()}
    assert listed == {*kept_turns, kept_step, newest}
    for gone in (first.id, *first_steps):
        assert not (agent_dir(project) / "history" / gone).exists()


async def test_store_lists_steps_with_turns(db: Database, tmp_path: Path) -> None:
    # The REST mapping is covered in test_routes_history; here the store's list order.
    store = HistoryStore(db, copy_project(ECC83, tmp_path / "projects"))
    await store.snapshot("run-1", "turn")
    await store.snapshot("run-1", "step", kind="step", plan_step_id="s1")
    kinds = [(s.kind, s.plan_step_id) for s in await store.list()]
    assert kinds == [("step", "s1"), ("turn", None)]


async def test_take_step_checkpoint_saves_first(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, project = opened.env, opened.project
    order: list[str] = []
    real_snapshot = HistoryStore.snapshot

    async def recording_snapshot(self: HistoryStore, *a: Any, **kw: Any) -> Any:
        order.append(f"snapshot:{env.saved[-1] if env.saved else '-'}")
        return await real_snapshot(self, *a, **kw)

    monkeypatch.setattr(HistoryStore, "snapshot", recording_snapshot)
    # The fake session's save never fires the manager's listeners: hook them in here.
    real_save = type(opened.session).save

    async def save_and_record(self: Any, doc: Any) -> None:
        await real_save(self, doc)
        await env.svc._on_saved(project, doc)

    monkeypatch.setattr(type(opened.session), "save", save_and_record)
    assert project.pcb_file is not None
    await env.svc.hashes.replace(project.id, {})
    cp = await take_step_checkpoint(env.svc, project, "run-cp", "step-1", "Step 1: power")
    assert env.saved == ["sch"]
    assert order == ["snapshot:sch"]
    assert (cp.kind, cp.plan_step_id, cp.run_id, cp.label) == (
        "step",
        "step-1",
        "run-cp",
        "Step 1: power",
    )
    stored = await env.svc.hashes.get(project.id)
    assert stored and all(k.endswith((".kicad_sch", ".kicad_pro")) for k in stored)
    assert stored.items() <= compute_hashes(project).items()


async def test_take_step_checkpoint_needs_open_session(opened: Opened) -> None:
    env = opened.env
    other = env.project(HIERARCHY)
    with pytest.raises(KicadUnavailable, match="not open"):
        await take_step_checkpoint(env.svc, other, "run-x", "s", "label")


async def test_recover_uses_turn_snapshot(opened: Opened) -> None:
    env, project = opened.env, opened.project
    t = turn("run-rc")
    await opened.hooks.on_turn_start(t)
    await opened.hooks.on_first_mutation(t)
    cp = await take_step_checkpoint(env.svc, project, "run-rc", "step-1", "Step 1")
    env.server.crash()
    await opened.hooks.on_turn_end(t, done("sch", reason="error", failed=True))
    snap = await env.svc.store(project).latest_for_run("run-rc")
    assert snap is not None and snap.id != cp.id
    metas = [m for _, m in env.engine.notices]
    assert {"action": "restore", "snapshotId": snap.id} in metas
    assert metas[-1] == {"action": "restore", "snapshotId": cp.id, "checkpoint": True}


async def test_recover_without_checkpoint_posts_one_notice(opened: Opened) -> None:
    env = opened.env
    t = turn("run-rc2")
    await opened.hooks.on_turn_start(t)
    await opened.hooks.on_first_mutation(t)
    env.server.crash()
    await opened.hooks.on_turn_end(t, done("sch", reason="error", failed=True))
    restores = [m for _, m in env.engine.notices if m and m.get("action") == "restore"]
    assert len(restores) == 1 and "checkpoint" not in restores[0]


async def test_auto_restore_prefers_last_step_checkpoint(opened: Opened) -> None:
    env, project = opened.env, opened.project
    assert project.pcb_file is not None
    t = turn("run-ar2")
    await opened.hooks.on_turn_start(t)
    await opened.hooks.on_first_mutation(t)
    replace_with_kicad_file(project.pcb_file, other_board())  # finished step on disk
    at_step = compute_hashes(project)
    cp = await take_step_checkpoint(env.svc, project, "run-ar2", "step-1", "Step 1")
    replace_with_kicad_file(project.pcb_file, ECC83 / "ecc83-pp.kicad_pcb")  # partial edit
    await opened.hooks.on_turn_end(t, inconsistent("pcb", auto_restore=True))
    assert compute_hashes(project) == at_step
    text, meta = env.engine.notices[-1]
    assert meta == {"action": "restored", "snapshotId": cp.id}
    assert "plan step" in text
