import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from copperchat.db.repos.footprints import ABSENT, FootprintWriteRepo
from copperchat.errors import ReadOnlyProject
from copperchat.history.library_lock import library_lock
from copperchat.history.restore import AgentBusy, restore_snapshot
from copperchat.history.store import CorruptSnapshot, LibraryLocation, compute_hashes, sha256_file
from copperchat.projects import Project
from tests.support.conversations import TEST_CID
from tests.support.history import ECC83, other_board, replace_with_kicad_file
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


# --- the local footprint library (F3 Task 6, spec §10, index calls 4 and 14) -----------

# KiCad-saved footprints and table, copied (never hand-written) into the test repo.
_FOOTPRINTS = sorted((ECC83 / "footprints.pretty").glob("*.kicad_mod"))
_TABLE = ECC83 / "fp-lib-table"
LIB_REL = "footprints/X.pretty"


@dataclass
class Lib:
    project: Project
    repo: Path
    dir: Path
    journal: FootprintWriteRepo
    reads: list[object] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    rows: list[tuple[str, str]] = field(default_factory=list)  # what the fake API reports
    fail: dict[str, str] = field(default_factory=dict)  # nick -> API error
    calls: list[tuple[Project, list[str], str]] = field(default_factory=list)

    @property
    def table_rel(self) -> str:
        return f"{self.project.dir.relative_to(self.repo).as_posix()}/fp-lib-table"

    def put(self, name: str, src: int) -> str:
        shutil.copyfile(_FOOTPRINTS[src], self.dir / name)
        return sha256_file(self.dir / name)

    async def write(
        self, name: str, op: str, post: str, state: str = "done", lib_id: str | None = None
    ) -> int:
        wid = await self.journal.insert_pending(
            self.project.id, "run-1", lib_id or f"X:{name[:-10]}", f"{LIB_REL}/{name}", op, None
        )
        await self.journal.finalise(wid, state, post)
        return wid

    async def row_add(self, nick: str) -> None:
        wid = await self.journal.insert_pending(
            self.project.id, "run-1", nick, self.table_rel, "row_add", None
        )
        await self.journal.finalise(wid, "done", None)


@pytest.fixture
def lib(env: Env, monkeypatch: pytest.MonkeyPatch) -> Lib:
    from copperchat.history import restore as restore_mod

    project = env.project()
    repo = project.dir.parent.resolve()
    lib_dir = repo / "footprints" / "X.pretty"
    lib_dir.mkdir(parents=True)
    state = Lib(project, repo, lib_dir, FootprintWriteRepo(env.svc.db))
    state.put("A.kicad_mod", 0)
    state.put("B.kicad_mod", 1)

    async def locate(p: Project) -> LibraryLocation | None:
        return LibraryLocation(
            dir=lib_dir, repo_root=repo, table=p.dir / "fp-lib-table", nickname="X"
        )

    async def read_rows(session: object) -> list[tuple[str, str]] | None:
        state.reads.append(session)
        return list(state.rows)

    async def remove_rows(session: object, nicks: list[str]) -> list[tuple[str, str]]:
        failed = []
        for nick in nicks:
            if nick in state.fail:
                failed.append((nick, state.fail[nick]))
            else:
                state.removed.append(nick)
                state.rows = [r for r in state.rows if r[0] != nick]
        return failed

    async def listener(p: Project, rels: list[str], reason: str) -> None:
        state.calls.append((p, rels, reason))

    env.svc.locate_library = locate
    env.svc.library_listeners.append(listener)
    monkeypatch.setattr(restore_mod, "_read_table_rows", read_rows)
    monkeypatch.setattr(restore_mod, "_remove_table_rows", remove_rows)
    return state


def _notice(env: Env) -> str:
    return env.engine.notices[-1][0]


async def test_restore_copies_back_and_deletes_own_library_writes(env: Env, lib: Lib) -> None:
    project = lib.project
    assert project.pcb_file is not None
    sha_a = sha256_file(lib.dir / "A.kicad_mod")
    sha_b = sha256_file(lib.dir / "B.kicad_mod")
    pcb = compute_hashes(project)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    # after the snapshot this project replaced A, deleted B and created C
    await lib.write("A.kicad_mod", "replace", lib.put("A.kicad_mod", 2))
    (lib.dir / "B.kicad_mod").unlink()
    await lib.write("B.kicad_mod", "delete", ABSENT)
    await lib.write("C.kicad_mod", "create", lib.put("C.kicad_mod", 3))
    replace_with_kicad_file(project.pcb_file, other_board())

    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)

    assert compute_hashes(project) == pcb
    assert sha256_file(lib.dir / "A.kicad_mod") == sha_a
    assert sha256_file(lib.dir / "B.kicad_mod") == sha_b
    assert not (lib.dir / "C.kicad_mod").exists()
    stored = await env.svc.hashes.get(project.id)
    assert stored == {k: v.sha256 for k, v in (await env.svc.current_hashes(project)).items()}
    assert "../footprints/X.pretty/A.kicad_mod" in stored
    assert "not restored" not in _notice(env)


async def test_restore_notice_lists_skipped_and_unremoved_rows(env: Env, lib: Lib) -> None:
    project = lib.project
    await env.sessions.open(project)
    snap = await env.svc.store(project).snapshot("run-1", "turn")  # the table existed
    await lib.write("A.kicad_mod", "replace", lib.put("A.kicad_mod", 2))
    gui = lib.put("A.kicad_mod", 3)  # then the KiCad GUI changed A again
    await lib.row_add("X")
    await lib.row_add("Y")
    lib.rows = [("X", "${KIPRJMOD}/../footprints/X.pretty"), ("Y", "y"), ("Other", "o")]
    lib.fail = {"Y": "table is read-only"}

    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)

    assert sha256_file(lib.dir / "A.kicad_mod") == gui  # left alone
    assert lib.removed == ["X"]
    assert [n for n, _ in lib.rows] == ["Y", "Other"]
    text = _notice(env)
    assert text.startswith("Restored the design to the snapshot from")
    assert "X:A: changed outside this project, not restored" in text
    assert "library row Y not removed (table is read-only)" in text
    assert "library row X" not in text and "Other" not in text
    assert env.engine.notices[-1][1] == {"action": "restored", "snapshotId": snap.id}


async def test_restore_not_open_project_lists_rows(env: Env, lib: Lib) -> None:
    project = lib.project
    table = project.dir / "fp-lib-table"
    aside = lib.repo / "fp-lib-table.aside"
    table.rename(aside)
    snap = await env.svc.store(project).snapshot("run-1", "turn")  # no table then
    aside.rename(table)  # this project added its table and row since
    table_bytes = table.read_bytes()
    await lib.row_add("X")
    lib.rows = [("X", "x")]

    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)

    assert table.read_bytes() == table_bytes  # remove_table not applied, table untouched
    assert lib.reads == [] and lib.removed == []
    assert "library row X not removed (project not open)" in _notice(env)
    assert env.server.starts == 0


async def test_restore_removes_table_this_project_created(env: Env, lib: Lib) -> None:
    project = lib.project
    await env.sessions.open(project)
    table = project.dir / "fp-lib-table"
    aside = lib.repo / "fp-lib-table.aside"
    table.rename(aside)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    aside.rename(table)
    await lib.row_add("X")
    lib.rows = [("X", "x")]

    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)

    assert not table.exists() and lib.removed == []
    assert "fp-lib-table" not in await env.svc.hashes.get(project.id)
    assert lib.calls and "fp-lib-table" in lib.calls[-1][1]


async def test_reopen_failure_names_unremoved_rows(env: Env, lib: Lib) -> None:
    project = lib.project
    await env.sessions.open(project)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    await lib.row_add("X")
    lib.rows = [("X", "x")]
    env.server.fail_start_with_exit_code = 2

    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)

    assert lib.removed == []
    assert "library row X not removed (KiCad did not restart)" in _notice(env)


async def test_library_lock_not_held_during_reopen(
    env: Env, lib: Lib, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copperchat.history import restore as restore_mod

    project = lib.project
    await env.sessions.open(project)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    await lib.write("C.kicad_mod", "create", lib.put("C.kicad_mod", 3))
    await lib.row_add("X")
    lib.rows = [("X", "x")]
    seen: list[str] = []
    real_reopen = env.sessions.reopen
    real_apply = restore_mod.apply_library_plan

    async def reopen(p: Project) -> object:
        assert not library_lock(lib.dir).locked()
        seen.append("reopen")
        return await real_reopen(p)

    def apply(*args: object, **kwargs: object) -> list[str]:
        assert library_lock(lib.dir).locked()
        seen.append("apply")
        return real_apply(*args, **kwargs)  # type: ignore[arg-type]

    async def remove_rows(session: object, nicks: list[str]) -> list[tuple[str, str]]:
        assert not library_lock(lib.dir).locked()
        assert env.svc.lock(project).locked()
        seen.append("remove")
        return []

    monkeypatch.setattr(env.sessions, "reopen", reopen)
    monkeypatch.setattr(restore_mod, "apply_library_plan", apply)
    monkeypatch.setattr(restore_mod, "_remove_table_rows", remove_rows)

    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)

    assert seen == ["apply", "reopen", "remove"]
    assert not (lib.dir / "C.kicad_mod").exists()


async def test_library_part_skipped_when_locator_none(env: Env, lib: Lib) -> None:
    project = lib.project
    assert project.pcb_file is not None
    pcb = compute_hashes(project)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    await lib.write("C.kicad_mod", "create", lib.put("C.kicad_mod", 3))
    replace_with_kicad_file(project.pcb_file, other_board())

    async def transient(p: Project) -> LibraryLocation | None:
        return None

    env.svc.locate_library = transient
    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)

    assert compute_hashes(project) == pcb
    assert (lib.dir / "C.kicad_mod").exists()
    assert lib.calls == []


async def test_listeners_called_with_restore(env: Env, lib: Lib) -> None:
    project = lib.project
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    await lib.write("C.kicad_mod", "create", lib.put("C.kicad_mod", 3))
    await lib.write("A.kicad_mod", "replace", lib.put("A.kicad_mod", 2))

    async def broken(p: Project, rels: list[str], reason: str) -> None:
        raise RuntimeError("listener bug")

    env.svc.library_listeners.insert(0, broken)  # logged, never raised
    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)

    assert lib.calls == [
        (
            project,
            ["../footprints/X.pretty/A.kicad_mod", "../footprints/X.pretty/C.kicad_mod"],
            "restore",
        )
    ]


async def test_v1_snapshot_restore_unchanged_behaviour(env: Env, lib: Lib) -> None:
    project = lib.project
    await env.sessions.open(project)
    store = env.svc.store(project)
    snap = await store.snapshot("run-1", "turn")
    manifest = store.snapshot_dir(snap.id) / "manifest.json"
    data = json.loads(manifest.read_text())
    for key in ("repoRootRel", "journalMaxId", "library", "table"):
        del data[key]
    data["version"] = 1
    manifest.write_text(json.dumps(data))
    await lib.write("C.kicad_mod", "create", lib.put("C.kicad_mod", 3))
    await lib.row_add("X")
    lib.rows = [("X", "x")]

    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)

    assert (lib.dir / "C.kicad_mod").exists()
    assert lib.reads == [] and lib.removed == [] and lib.calls == []
    assert _notice(env).endswith("(turn).")
