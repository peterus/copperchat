"""SP4 P1a Task 5: `.kicad_dru` tracking, `absent` manifest entries, restore
re-discovery and `remove_created`.

Every file put into a project here is a byte copy of a KiCad-written fixture file
(history never looks at the content); nothing is hand-written.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from copperchat.history.restore import absent_plan, remove_created, restore_snapshot
from copperchat.history.store import MANIFEST_VERSION, CorruptSnapshot, compute_hashes
from copperchat.projects import Project, discover_projects
from tests.support.conversations import TEST_CID
from tests.support.history import ECC83, other_board
from tests.unit.history.conftest import Env

STEM = "ecc83-pp"
PCB = f"{STEM}.kicad_pcb"
DRU = f"{STEM}.kicad_dru"


def _kicad_bytes() -> Path:
    """Any KiCad-written file: history copies bytes, it never parses them."""
    return ECC83 / "sym-lib-table"


def _boardless(env: Env) -> Project:
    """The ecc83 fixture copied without its board (a schematic-only project)."""
    dest = env.settings.projects_root / ECC83.name
    shutil.copytree(ECC83, dest)
    (dest / PCB).unlink()
    return _rediscover(dest)


def _rediscover(project_dir: Path) -> Project:
    return next(p for p in discover_projects(project_dir.parent) if p.dir == project_dir)


def _manifest_path(env: Env, project: Project, sid: str) -> Path:
    return env.svc.store(project).snapshot_dir(sid) / "manifest.json"


def _manifest(env: Env, project: Project, sid: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(_manifest_path(env, project, sid).read_text())
    return data


def _edit_manifest(env: Env, project: Project, sid: str, **changes: Any) -> None:
    path = _manifest_path(env, project, sid)
    data = json.loads(path.read_text())
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path.write_text(json.dumps(data))


async def test_snapshot_boardless_records_pcb_absent(env: Env) -> None:
    project = _boardless(env)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    m = _manifest(env, project, snap.id)
    assert m["version"] == MANIFEST_VERSION == 3
    assert m["absent"] == [PCB, DRU]
    assert PCB not in {f["relPath"] for f in m["files"]}


async def test_snapshot_with_dru_tracks_it(env: Env) -> None:
    project = env.project()
    shutil.copyfile(_kicad_bytes(), project.dir / DRU)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    m = _manifest(env, project, snap.id)
    rels = {f["relPath"] for f in m["files"]}
    assert DRU in rels and PCB in rels
    assert m["absent"] == []
    assert DRU in compute_hashes(project)  # tracked for the external-change check too


async def test_snapshot_symlinked_dru_is_absent_not_tracked(env: Env, tmp_path: Path) -> None:
    project = env.project()
    target = tmp_path / "elsewhere.kicad_dru"
    shutil.copyfile(_kicad_bytes(), target)
    (project.dir / DRU).symlink_to(target)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    m = _manifest(env, project, snap.id)
    assert DRU not in {f["relPath"] for f in m["files"]}
    assert m["absent"] == [DRU]


async def test_restore_deletes_exactly_absent(env: Env) -> None:
    project = _boardless(env)
    await env.sessions.open(project)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    shutil.copyfile(other_board(), project.dir / PCB)
    shutil.copyfile(_kicad_bytes(), project.dir / DRU)
    shutil.copyfile(other_board(), project.dir / "notes.kicad_pcb.bak")

    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)

    assert not (project.dir / PCB).exists()
    assert not (project.dir / DRU).exists()
    assert (project.dir / "notes.kicad_pcb.bak").read_bytes() == other_board().read_bytes()
    assert PCB not in await env.svc.hashes.get(project.id)


@pytest.mark.parametrize("bad", ["../x.kicad_pcb", f"sub/../../{PCB}", f"/tmp/{PCB}"])
async def test_absent_outside_project_refused(env: Env, bad: str) -> None:
    project = _boardless(env)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    outside = project.dir.parent / "x.kicad_pcb"
    shutil.copyfile(other_board(), outside)
    shutil.copyfile(other_board(), project.dir / PCB)
    _edit_manifest(env, project, snap.id, absent=[bad])
    with pytest.raises(CorruptSnapshot):
        await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)
    assert outside.exists() and (project.dir / PCB).exists()


async def test_absent_wrong_suffix_refused(env: Env) -> None:
    project = _boardless(env)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    _edit_manifest(env, project, snap.id, absent=[f"{STEM}.kicad_sch"])
    assert project.sch_file is not None
    before = project.sch_file.read_bytes()
    with pytest.raises(CorruptSnapshot):
        await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)
    assert project.sch_file.read_bytes() == before


@pytest.mark.parametrize("bad", [None, "x", [1], [""], {"a": 1}])
def test_absent_plan_rejects_malformed(tmp_path: Path, bad: Any) -> None:
    if bad is None:
        assert absent_plan({"version": 3}, tmp_path) == []  # missing -> []
        return
    with pytest.raises(CorruptSnapshot):
        absent_plan({"version": 3, "absent": bad}, tmp_path)


def test_absent_plan_never_looks_at_the_file(tmp_path: Path) -> None:
    (tmp_path / "p.kicad_pcb").mkdir()  # whatever is there: decided at deletion time
    (tmp_path / "p.kicad_dru").symlink_to(tmp_path / "nowhere")
    plan = absent_plan({"absent": ["p.kicad_pcb", "sub/../p.kicad_dru"]}, tmp_path)
    assert plan == [tmp_path / "p.kicad_pcb", tmp_path / "p.kicad_dru"]


@pytest.mark.parametrize(
    "rel", ["sub/p.kicad_pcb", "..", ".", "p.kicad_pcb/..", "x/../../p.kicad_dru", "p.kicad_pro"]
)
def test_absent_plan_only_direct_board_children(tmp_path: Path, rel: str) -> None:
    with pytest.raises(CorruptSnapshot):
        absent_plan({"absent": [rel]}, tmp_path)


async def test_absent_symlink_not_followed(
    env: Env, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project = _boardless(env)
    await env.sessions.open(project)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    assert project.sch_file is not None
    sch_before = project.sch_file.read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target.kicad_pcb"
    shutil.copyfile(other_board(), target)
    (project.dir / PCB).symlink_to(target)
    shutil.copyfile(other_board(), project.sch_file)  # an external change to undo

    with caplog.at_level(logging.WARNING, logger="copperchat.history.restore"):
        await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)

    assert target.read_bytes() == other_board().read_bytes()
    assert (project.dir / PCB).is_symlink()
    assert any(PCB in r.getMessage() for r in caplog.records)
    assert project.sch_file.read_bytes() == sch_before  # the rest was restored
    new = env.sessions.current()
    assert new is not None and not new.closed and env.server.starts == 2


def test_remove_created_deletes_regular_files(tmp_path: Path) -> None:
    shutil.copyfile(other_board(), tmp_path / "p.kicad_pcb")
    shutil.copyfile(_kicad_bytes(), tmp_path / "p.kicad_dru")
    remove_created(tmp_path, ["p.kicad_pcb", "p.kicad_dru", "missing.kicad_pcb"])
    assert list(tmp_path.iterdir()) == []


def test_remove_created_skips_symlink_and_dir(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    target = tmp_path / "target.kicad_pcb"
    shutil.copyfile(other_board(), target)
    (project_dir / "p.kicad_pcb").symlink_to(target)
    (project_dir / "p.kicad_dru").mkdir()
    with caplog.at_level(logging.WARNING, logger="copperchat.history.restore"):
        remove_created(project_dir, ["p.kicad_pcb", "p.kicad_dru"])
    assert target.read_bytes() == other_board().read_bytes()
    assert (project_dir / "p.kicad_pcb").is_symlink()
    assert (project_dir / "p.kicad_dru").is_dir()
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "p.kicad_pcb" in messages and "p.kicad_dru" in messages


@pytest.mark.parametrize(
    "rels",
    [["../p.kicad_pcb"], ["p.kicad_pcb", "../p.kicad_pcb"], ["p.kicad_sch"], ["/abs/p.kicad_pcb"]],
)
def test_remove_created_outside_refused(tmp_path: Path, rels: list[str]) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    shutil.copyfile(other_board(), tmp_path / "p.kicad_pcb")
    shutil.copyfile(other_board(), project_dir / "p.kicad_pcb")
    shutil.copyfile(other_board(), project_dir / "p.kicad_sch")
    with pytest.raises(ValueError):
        remove_created(project_dir, rels)
    # Refused as a whole: nothing deleted, also not the valid name before the bad one.
    assert (tmp_path / "p.kicad_pcb").exists()
    assert (project_dir / "p.kicad_pcb").exists()
    assert (project_dir / "p.kicad_sch").exists()


async def test_v2_manifest_without_absent(env: Env) -> None:
    project = _boardless(env)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    _edit_manifest(env, project, snap.id, version=2, absent=None)
    shutil.copyfile(other_board(), project.dir / PCB)
    await restore_snapshot(env.svc, project, snap.id, conversation_id=TEST_CID)
    assert (project.dir / PCB).read_bytes() == other_board().read_bytes()


async def test_restore_rediscovers_before_reopen(env: Env) -> None:
    project = _boardless(env)
    await env.sessions.open(project)
    snap = await env.svc.store(project).snapshot("run-1", "turn")
    # The agent created a board: the engine's project now knows it.
    shutil.copyfile(other_board(), project.dir / PCB)
    with_board = _rediscover(project.dir)
    assert with_board.pcb_file is not None
    await env.sessions.reopen(with_board)
    env.engine.refresh_project(with_board)
    env.engine.refreshed.clear()
    reopened: list[Project] = []
    real_reopen = env.sessions.reopen

    async def spy(p: Project) -> Any:
        reopened.append(p)
        return await real_reopen(p)

    env.sessions.reopen = spy  # type: ignore[method-assign,assignment]

    await restore_snapshot(env.svc, with_board, snap.id, conversation_id=TEST_CID)

    assert len(reopened) == 1
    fresh = reopened[0]
    assert fresh.pcb_file is None and fresh.id == project.id
    assert env.svc.docs_of(fresh) == {"sch"}
    assert env.engine.refreshed == [fresh]
    current = env.sessions.current()
    assert current is not None and current.project.pcb_file is None
    assert not os.path.lexists(project.dir / PCB)


async def test_history_hooks_follow_the_refreshed_project(env: Env) -> None:
    project = _boardless(env)
    hooks = env.svc.hooks_factory(project, env.engine)  # type: ignore[arg-type]
    shutil.copyfile(other_board(), project.dir / PCB)
    fresh = _rediscover(project.dir)
    hooks.on_project_changed(fresh)
    assert hooks.project is fresh


async def test_own_board_save_records_the_rules_file(env: Env) -> None:
    """A board save may (re)write `<stem>.kicad_dru` (custom rules): it is owned by the
    pcb save, so the next turn start never reports it as an external change."""
    project = env.project()
    session = await env.sessions.open(project)
    hooks = env.svc.hooks_factory(project, env.engine)  # type: ignore[arg-type]
    await hooks.on_engine_start()
    shutil.copyfile(_kicad_bytes(), project.dir / DRU)
    await session.save("pcb")
    assert (await env.svc.hashes.get(project.id))[DRU] == compute_hashes(project)[DRU]
