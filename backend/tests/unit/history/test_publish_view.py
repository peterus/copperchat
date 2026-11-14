"""publish_view: changed view files from hashes, live publishes, version consistency."""

from __future__ import annotations

import asyncio
import shutil

import pytest

from copperchat.kicad_session import KicadSession
from copperchat.projects import Project
from copperchat.views import write_kicanvas_snapshot
from copperchat.views.snapshot import view_dir, view_file_hashes
from tests.support.history import ECC83, HIERARCHY, replace_with_kicad_file
from tests.support.kicad_project import FIXTURES
from tests.unit.history.conftest import Env

ROOT, SUB = "complex_hierarchy.kicad_sch", "ampli_ht.kicad_sch"
BLANK_SCH = FIXTURES / "blank" / "blank.kicad_sch"


@pytest.fixture
async def hier(env: Env) -> tuple[Env, Project, KicadSession]:
    """complex_hierarchy with the real snapshot writer on the fake api-server."""
    env.svc.write_view = write_kicanvas_snapshot
    project = env.project(HIERARCHY)
    env.server.hierarchy = [ROOT, SUB]
    session = await env.sessions.open(project)
    return env, project, session


def frames(env: Env) -> list[tuple[int, list[str] | None, bool | None]]:
    return [(f.version, f.changed_files, f.live) for f in env.broadcaster.frames]


async def test_view_file_hashes_cover_every_sheet_and_the_board(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    _, project, session = hier
    assert view_file_hashes(project, {"sch", "pcb"}) == {}
    await write_kicanvas_snapshot(session, "sch")
    await write_kicanvas_snapshot(session, "pcb")
    stray = view_dir(project) / "old" / "gone.kicad_sch"
    stray.parent.mkdir()
    shutil.copyfile(BLANK_SCH, stray)
    assert set(view_file_hashes(project, {"sch"})) == {ROOT, SUB, "old/gone.kicad_sch"}
    assert set(view_file_hashes(project, {"pcb"})) == {"complex_hierarchy.kicad_pcb"}
    both = view_file_hashes(project, {"sch", "pcb"})
    assert len(both) == 4 and all(len(h) == 64 for h in both.values())


async def test_changed_files_come_from_before_and_after_hashes(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    env, project, session = hier
    svc = env.svc
    assert await svc.publish_view(project, session, {"sch"}) == 1  # all files appear
    sub = project.dir / SUB
    replace_with_kicad_file(sub, BLANK_SCH)
    assert await svc.publish_view(project, session, {"sch"}, live=True) == 2
    assert project.sch_file is not None
    replace_with_kicad_file(project.sch_file, BLANK_SCH)
    assert await svc.publish_view(project, session, {"sch"}, live=True) == 3
    (project.dir / "sub").mkdir()
    shutil.copyfile(ECC83 / "ecc83-pp.kicad_sch", project.dir / "sub" / "new.kicad_sch")
    env.server.hierarchy = [ROOT, SUB, "sub/new.kicad_sch"]
    assert await svc.publish_view(project, session, {"sch"}) == 4
    env.server.hierarchy = [ROOT, "sub/new.kicad_sch"]
    assert await svc.publish_view(project, session, {"sch"}) == 5
    assert frames(env) == [
        (1, [SUB, ROOT], None),
        (2, [SUB], True),
        (3, [ROOT], True),
        (4, ["sub/new.kicad_sch"], None),
        (5, [SUB], None),  # removed from the hierarchy
    ]
    assert session.view_version == 5


async def test_live_publish_without_change_sends_nothing(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    env, project, session = hier
    svc = env.svc
    assert await svc.publish_view(project, session, {"sch"}) == 1
    assert await svc.publish_view(project, session, {"sch"}, live=True) == 1
    assert session.view_version == 1 and len(env.broadcaster.frames) == 1
    # The turn end is the guaranteed final state: it always sends, even unchanged.
    assert await svc.publish_view(project, session, {"sch"}) == 2
    assert frames(env) == [(1, [SUB, ROOT], None), (2, [], None)]


async def test_board_publish_lists_the_board_snapshot(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    env, project, session = hier
    await env.svc.publish_view(project, session, {"sch", "pcb"})
    assert project.pcb_file is not None
    replace_with_kicad_file(project.pcb_file, ECC83 / "ecc83-pp.kicad_pcb")
    await env.svc.publish_view(project, session, {"pcb"})
    assert frames(env)[-1] == (2, ["complex_hierarchy.kicad_pcb"], None)
    assert env.broadcaster.frames[-1].docs == ["pcb"]


async def test_versions_stay_monotonic_across_live_and_turn_end_publishes(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    env, project, session = hier
    svc = env.svc
    session.view_version = 7
    sources = [BLANK_SCH, HIERARCHY / SUB, BLANK_SCH]
    for source in sources:
        replace_with_kicad_file(project.dir / SUB, source)
        await svc.publish_view(project, session, {"sch"}, live=True)
        await svc.publish_view(project, session, {"sch"}, live=True)  # unchanged: skipped
    await svc.publish_view(project, session, {"sch", "pcb"})
    versions = [f.version for f in env.broadcaster.frames]
    assert versions == [8, 9, 10, 11] and session.view_version == 11


async def test_open_snapshots_are_reported_by_the_next_publish(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    """A client opening mid-turn rewrites the view files without a frame: whatever it
    wrote that the other clients have not seen yet is still listed by the next publish."""
    env, project, session = hier
    svc = env.svc
    await svc.publish_view(project, session, {"sch", "pcb"})
    replace_with_kicad_file(project.dir / SUB, BLANK_SCH)  # a mid-turn edit
    files = await svc.write_open_snapshots(session)
    assert files == {"sch": [ROOT, SUB], "pcb": ["complex_hierarchy.kicad_pcb"]}
    assert len(env.broadcaster.frames) == 1
    await svc.publish_view(project, session, {"sch"}, live=True)
    assert frames(env)[-1] == (2, [SUB], True)
    assert project.pcb_file is not None
    replace_with_kicad_file(project.pcb_file, ECC83 / "ecc83-pp.kicad_pcb")
    await svc.publish_view(project, session, {"pcb"})
    assert frames(env)[-1] == (3, ["complex_hierarchy.kicad_pcb"], None)


async def test_first_open_snapshot_is_the_baseline(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    env, project, session = hier
    await env.svc.write_open_snapshots(session)
    assert await env.svc.publish_view(project, session, {"sch"}, live=True) == 0
    assert env.broadcaster.frames == []


async def test_view_lock_serialises_publishes(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    env, project, session = hier
    svc = env.svc
    assert svc.view_lock(project) is svc.view_lock(project)
    assert svc.view_lock(project) is not svc.lock(project)
    async with svc.view_lock(project):  # e.g. an open writing the snapshots
        task = asyncio.create_task(svc.publish_view(project, session, {"sch"}))
        for _ in range(50):
            await asyncio.sleep(0)
        assert not task.done() and not (view_dir(project) / ROOT).exists()
    assert await task == 1
