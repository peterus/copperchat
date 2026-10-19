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
from tests.support.conversations import TEST_CID, ensure_test_conversation
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
    await ensure_test_conversation(d)
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
    # Out of scope here: the listener itself (E1, tests/kicad/test_history_real.py); this
    # test checks that the checkpoint saves through the session before the snapshot.
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


async def test_prune_drops_step_only_runs_older_than_kept_turns(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "HISTORY_KEEP", 2)
    store = HistoryStore(db, copy_project(ECC83, tmp_path / "projects"))
    old_step = (await store.snapshot("run-s", "s", kind="step", plan_step_id="a")).id
    await store.snapshot("run-0", "t0")
    kept_turn = (await store.snapshot("run-1", "t1")).id
    # A step-only run newer than the oldest kept turn snapshot stays.
    newer_step = (await store.snapshot("run-n", "s", kind="step", plan_step_id="b")).id
    last = (await store.snapshot("run-2", "t2")).id
    listed = {s.id for s in await store.list()}
    assert listed == {kept_turn, newer_step, last}
    assert old_step not in listed


async def test_failed_checkpoint_restore_falls_back_to_turn_snapshot(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, project = opened.env, opened.project
    assert project.pcb_file is not None
    before = compute_hashes(project)
    t = turn("run-fb")
    await opened.hooks.on_turn_start(t)
    await opened.hooks.on_first_mutation(t)
    cp = await take_step_checkpoint(env.svc, project, "run-fb", "step-1", "Step 1")
    (env.svc.store(project).snapshot_dir(cp.id) / "manifest.json").write_text("{}")  # corrupt
    replace_with_kicad_file(project.pcb_file, other_board())
    await opened.hooks.on_turn_end(t, inconsistent("pcb", auto_restore=True))
    snap = await env.svc.store(project).latest_for_run("run-fb")
    assert snap is not None
    assert compute_hashes(project) == before
    assert env.engine.errors == []  # the fallback succeeded: no manual-restore advice
    assert env.engine.notices[-1][1] == {"action": "restored", "snapshotId": snap.id}


async def test_both_restores_failing_reports_the_turn_snapshot(opened: Opened) -> None:
    env, project = opened.env, opened.project
    t = turn("run-fb2")
    await opened.hooks.on_turn_start(t)
    await opened.hooks.on_first_mutation(t)
    cp = await take_step_checkpoint(env.svc, project, "run-fb2", "step-1", "Step 1")
    snap = await env.svc.store(project).latest_for_run("run-fb2")
    assert snap is not None
    for sid in (cp.id, snap.id):
        (env.svc.store(project).snapshot_dir(sid) / "manifest.json").write_text("{}")
    await opened.hooks.on_turn_end(t, inconsistent("pcb", auto_restore=True))
    [(text, meta)] = env.engine.errors
    assert "before this turn" in text and meta == {"code": "restore_failed"}


async def test_step_checkpoints_are_capped_per_project(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E8a residual (E8b): step checkpoints of one run no longer grow without bound."""
    monkeypatch.setattr(store_mod, "STEP_KEEP", 3)
    project = copy_project(ECC83, tmp_path / "projects")
    store = HistoryStore(db, project)
    turn_snap = await store.snapshot("run-1", "turn")
    steps = [
        (await store.snapshot("run-1", f"s{i}", kind="step", plan_step_id=f"s{i}")).id
        for i in range(5)
    ]
    listed = [s.id for s in await store.list()]
    assert listed == [*reversed(steps[2:]), turn_snap.id]
    for gone in steps[:2]:
        assert not (agent_dir(project) / "history" / gone).exists()


async def test_open_plan_snapshots_are_never_pruned(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from copperchat.plans import machine
    from copperchat.plans.repo import PlanRepo

    monkeypatch.setattr(store_mod, "STEP_KEEP", 1)
    monkeypatch.setattr(store_mod, "HISTORY_KEEP", 2)
    project = copy_project(ECC83, tmp_path / "projects")
    store = HistoryStore(db, project)
    base = await store.snapshot("run-0", "pre-plan turn")
    first = await store.snapshot("run-1", "Plan step 1", kind="step", plan_step_id="x")
    plan = machine.new_plan(
        plan_id="f" * 32,
        project_id=project.id,
        title="t",
        summary="s",
        steps=machine.build_steps(
            "f" * 32,
            [{"title": "a", "intent": "b", "expected_changes": 1, "verification": "c"}] * 2,
        ),
        run_id="run-0",
        conversation_id=TEST_CID,
        now="2026-10-18T10:00:00Z",
    )
    plan = machine.transition(plan, "approve", now="t")
    plan = machine.transition(plan, "step_begin", now="t", idx=1, run_id="run-1")
    plan = machine.transition(plan, "step_done", now="t", idx=1, checkpoint_id=first.id)
    await PlanRepo(db).insert(replace(plan, base_snapshot_id=base.id))
    for i in range(3):
        await store.snapshot(f"run-{i + 2}", f"t{i}")
        await store.snapshot(f"run-{i + 2}", f"s{i}", kind="step", plan_step_id=f"y{i}")
    ids = {s.id for s in await store.list()}
    assert {base.id, first.id} <= ids  # the open plan can still roll back to them
    # Once the plan is closed they are pruned like any other snapshot.
    await PlanRepo(db).save(
        machine.transition(machine.transition(plan, "interrupt", now="t"), "abort", now="t")
    )
    await store.snapshot("run-9", "t9")
    ids = {s.id for s in await store.list()}
    assert base.id not in ids and first.id not in ids


async def test_latest_for_runs_batches_turn_snapshots(db: Database, tmp_path: Path) -> None:
    store = HistoryStore(db, copy_project(ECC83, tmp_path / "projects"))
    await store.snapshot("run-1", "turn a")
    newer = await store.snapshot("run-1", "turn b")
    await store.snapshot("run-1", "step", kind="step", plan_step_id="s1")
    other = await store.snapshot("run-2", "turn c")
    await store.snapshot("run-3", "step only", kind="step", plan_step_id="s2")
    got = await store.latest_for_runs(["run-1", "run-2", "run-3", "nope"])
    assert {k: v.id for k, v in got.items()} == {"run-1": newer.id, "run-2": other.id}
    for run in ("run-1", "run-2", "run-3"):
        one = await store.latest_for_run(run)
        assert (one.id if one else None) == (got[run].id if run in got else None)
    assert await store.latest_for_runs([]) == {}
