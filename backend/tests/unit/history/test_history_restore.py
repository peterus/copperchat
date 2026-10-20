import json

import pytest

from copperchat.errors import ReadOnlyProject
from copperchat.history.restore import AgentBusy, restore_snapshot
from copperchat.history.store import CorruptSnapshot, compute_hashes
from tests.support.conversations import TEST_CID
from tests.support.history import other_board, replace_with_kicad_file
from tests.unit.history.conftest import Env


async def test_restore_brings_back_kicad_saved_bytes_and_reopens(env: Env) -> None:
    project = env.project()
    assert project.pcb_file is not None
    before = compute_hashes(project)
    session = await env.sessions.open(project)
    session.view_version = 4
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    replace_with_kicad_file(project.pcb_file, other_board())
    assert compute_hashes(project) != before

    restored = await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)

    assert restored.id == snap.id
    assert compute_hashes(project) == before
    assert await env.svc.hashes.get(project.id) == before
    assert env.server.starts == 2  # closed + reopened on the restored files
    new = env.sessions.current()
    assert new is not None and new is not session and not new.closed and session.closed
    assert new.view_version == 5
    frame = env.broadcaster.frames[-1]
    assert frame.type == "view-updated" and frame.version == 5
    assert sorted(frame.docs) == ["pcb", "sch"] and sorted(env.written) == ["pcb", "sch"]
    assert env.engine.notices[-1][1] == {"action": "restored", "snapshotId": snap.id}
    assert env.engine.notes and snap.id in env.engine.notes[-1]
    # The notice goes to the session restored from; the note is broadcast (spec §5.3).
    assert env.engine.notice_conversations[-1] == TEST_CID
    assert env.engine.note_conversations[-1] is None
    assert not list(project.dir.glob("*.copperchat-restore"))


async def test_restore_of_project_that_is_not_open_only_copies(env: Env) -> None:
    project = env.project()
    assert project.pcb_file is not None
    before = compute_hashes(project)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    replace_with_kicad_file(project.pcb_file, other_board())
    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)
    assert compute_hashes(project) == before
    assert env.server.starts == 0 and env.broadcaster.frames == []


async def test_own_api_server_lock_does_not_block_restore(env: Env) -> None:
    project = env.project()
    await env.sessions.open(project)
    assert (project.dir / f"~{project.pro_file.name}.lck").exists()  # written by our server
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)


async def test_restore_refused_for_locked_project(env: Env) -> None:
    project = env.project()
    assert project.pcb_file is not None
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    (project.dir / f"~{project.pcb_file.name}.lck").touch()  # KiCad GUI lock marker
    pcb_before = project.pcb_file.read_bytes()
    replace_with_kicad_file(project.pcb_file, other_board())
    changed = project.pcb_file.read_bytes()
    with pytest.raises(ReadOnlyProject):
        await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)
    assert project.pcb_file.read_bytes() == changed != pcb_before
    assert env.broadcaster.frames == [] and env.server.starts == 0


async def test_restore_refused_for_read_only_session(env: Env) -> None:
    project = env.project()
    session = await env.sessions.open(project)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    session.read_only = True
    with pytest.raises(ReadOnlyProject):
        await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)
    assert env.server.starts == 1


async def test_restore_refused_while_agent_busy(env: Env) -> None:
    project = env.project()
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    env.engine.is_busy = True
    with pytest.raises(AgentBusy):
        await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)


@pytest.mark.parametrize("bad", ["../../escape", "/etc/passwd", "", "."])
async def test_restore_rejects_tampered_manifest(env: Env, bad: str) -> None:
    project = env.project()
    store = env.svc.store(project)
    snap = await store.snapshot("run-1", "turn")
    manifest = store.snapshot_dir(snap.id) / "manifest.json"
    data = json.loads(manifest.read_text())
    data["files"][0]["relPath"] = bad
    manifest.write_text(json.dumps(data))
    before = compute_hashes(project)
    with pytest.raises(CorruptSnapshot):
        await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)
    assert compute_hashes(project) == before


async def test_restore_rejects_modified_snapshot_file(env: Env) -> None:
    project = env.project()
    store = env.svc.store(project)
    snap = await store.snapshot("run-1", "turn")
    replace_with_kicad_file(store.snapshot_dir(snap.id) / "ecc83-pp.kicad_pcb", other_board())
    with pytest.raises(CorruptSnapshot):
        await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)


async def test_failed_copy_back_leaves_a_live_session(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copperchat.history import restore as restore_mod

    project = env.project()
    session = await env.sessions.open(project)
    snap = await env.svc.store(project).snapshot("run-1", "turn")

    def broken(plan: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(restore_mod, "_copy_back", broken)
    with pytest.raises(OSError):
        await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)
    current = env.sessions.current()
    assert current is not None and current is not session and not current.closed
    assert env.svc.open_session(project) is current


async def test_failed_reload_after_restore_still_succeeds(env: Env) -> None:
    project = env.project()
    assert project.pcb_file is not None
    before = compute_hashes(project)
    await env.sessions.open(project)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    replace_with_kicad_file(project.pcb_file, other_board())
    env.server.fail_start_with_exit_code = 2
    restored = await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)
    assert restored.id == snap.id and compute_hashes(project) == before
    assert env.engine.notices[-1][1] == {"action": "restored", "snapshotId": snap.id}
    assert [m for _, m in env.engine.errors] == [{"code": "kicad_restart_failed"}]
