"""publish_view: changed view files from hashes, live publishes, version consistency."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from copperchat.agent.protocol import ViewUpdatedFrame, dump_frame
from copperchat.kicad_session import DocumentModified, KicadSession
from copperchat.projects import Project
from copperchat.views import snapshot, write_kicanvas_snapshot
from copperchat.views.snapshot import (
    _hash_regular,
    copy_disk_schematic,
    snapshot_path,
    view_dir,
    view_file_hashes,
    view_file_list,
)
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


async def test_view_file_hashes_skip_symlinks_and_never_fail_on_one_file(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    _, project, session = hier
    await write_kicanvas_snapshot(session, "sch")
    root = view_dir(project)
    outside = project.dir.parent / "outside.kicad_sch"
    shutil.copyfile(BLANK_SCH, outside)
    (root / "link.kicad_sch").symlink_to(outside)
    (root / "fifo.kicad_sch").mkdir()  # not a regular file
    (root / SUB).chmod(0)
    try:
        hashes = view_file_hashes(project, {"sch"})
    finally:
        (root / SUB).chmod(0o644)
    assert set(hashes) == {ROOT, SUB}
    assert len(hashes[ROOT]) == 64
    if os.geteuid() != 0:  # root reads it anyway
        assert hashes[SUB] == ""  # unreadable: unknown, listed as changed


async def test_view_file_hashes_read_a_large_file_in_chunks(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    _, project, _ = hier
    data = os.urandom(3 * (1 << 16) + 123)  # several chunks plus a partial one
    path = view_dir(project) / "big.kicad_sch"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    assert view_file_hashes(project, {"sch"}) == {"big.kicad_sch": hashlib.sha256(data).hexdigest()}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
async def test_a_file_swapped_after_the_lstat_is_never_followed_nor_blocks(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    """The lstat/open race: a FIFO opens without blocking and is skipped; a symlink
    is never followed (O_NOFOLLOW)."""
    _, project, _ = hier
    root = view_dir(project)
    root.mkdir(parents=True, exist_ok=True)
    fifo = root / "fifo.kicad_sch"
    os.mkfifo(fifo)
    assert await asyncio.wait_for(asyncio.to_thread(_hash_regular, fifo), 5) is None
    link = root / "link.kicad_sch"
    link.symlink_to(BLANK_SCH)
    assert _hash_regular(link) is None  # ELOOP from O_NOFOLLOW: skipped, not "changed"


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


async def test_live_publish_never_saves_a_modified_schematic(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    """A failed partial edit leaves KiCad's document modified: the live publish neither
    saves it nor sends a frame; the hashes stay what the clients have."""
    env, project, session = hier
    svc = env.svc
    assert await svc.publish_view(project, session, {"sch"}) == 1
    replace_with_kicad_file(project.dir / SUB, BLANK_SCH)  # would be listed if written
    env.server.modified = True
    with pytest.raises(DocumentModified):
        await svc.publish_view(project, session, {"sch"}, live=True)
    assert env.server.saved == [] and env.server.modified is True
    assert session.view_version == 1 and len(env.broadcaster.frames) == 1
    env.server.modified = False
    assert await svc.publish_view(project, session, {"sch"}, live=True) == 2
    assert frames(env)[-1] == (2, [SUB], True)


async def test_failed_first_publish_keeps_the_baseline(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    """Nothing published yet and the write fails half-way: the next publish still lists
    what that write changed (the baseline is the view directory before it)."""
    env, project, session = hier
    svc = env.svc
    await svc.write_open_snapshots(session)
    svc._view_hashes.clear()  # e.g. a restart: no frame sent yet in this process
    replace_with_kicad_file(project.dir / SUB, BLANK_SCH)

    async def half(session: KicadSession, doc: str, *, save_modified: bool = True) -> object:
        await write_kicanvas_snapshot(session, "sch")
        raise OSError("disk full")

    svc.write_view = half  # type: ignore[assignment]
    with pytest.raises(OSError):
        await svc.publish_view(project, session, {"sch"})
    svc.write_view = write_kicanvas_snapshot
    await svc.publish_view(project, session, {"sch"}, live=True)
    assert frames(env) == [(1, [SUB], True)]


# ---- publish_disk_view: the turn end's catch-up from the files on disk, never KiCad


def no_kicad(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def call(self: KicadSession, *a: object, **kw: object) -> None:
        calls.append("call")
        raise AssertionError("publish_disk_view must not call KiCad")

    async def save_copy(self: KicadSession, *a: object, **kw: object) -> None:
        calls.append("save_copy")
        raise AssertionError("publish_disk_view must not call KiCad")

    monkeypatch.setattr(KicadSession, "call", call)
    monkeypatch.setattr(KicadSession, "save_copy", save_copy)
    return calls


async def test_disk_view_copies_the_last_save_without_kicad(
    hier: tuple[Env, Project, KicadSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    env, project, session = hier
    svc = env.svc
    assert await svc.publish_view(project, session, {"sch"}) == 1  # what the clients have
    starts = env.server.starts
    (project.dir / ROOT).write_bytes(b"(kicad_sch root E2)")  # saved after the last frame
    (project.dir / SUB).write_bytes(b"(kicad_sch sub E2)")
    calls = no_kicad(monkeypatch)
    assert await svc.publish_disk_view(project) == 2
    assert frames(env)[-1] == (2, sorted([ROOT, SUB]), None)  # non-live
    assert (view_dir(project) / ROOT).read_bytes() == b"(kicad_sch root E2)"
    assert (view_dir(project) / SUB).read_bytes() == b"(kicad_sch sub E2)"
    assert calls == [] and env.server.starts == starts and env.server.saved == []
    assert await svc.publish_disk_view(project) == 3  # always sent, even unchanged
    assert frames(env)[-1] == (3, [], None)


async def test_disk_view_refuses_paths_escaping_the_project_or_view_dir(
    hier: tuple[Env, Project, KicadSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    env, project, session = hier
    await env.svc.publish_view(project, session, {"sch"})
    root = view_dir(project)
    outside = project.dir.parent / "outside.kicad_sch"
    outside.write_bytes(b"secret")
    # A project-side symlink pointing outside: never read.
    (root / "evil.kicad_sch").write_bytes(b"view copy")
    (project.dir / "evil.kicad_sch").symlink_to(outside)
    # A view-side symlink pointing outside: never written through.
    (root / "link.kicad_sch").symlink_to(outside)
    (project.dir / "link.kicad_sch").write_bytes(b"overwrite")
    # A symlinked view sub-directory: refused as a whole.
    (project.dir / "sub").mkdir()
    (project.dir / "sub" / "x.kicad_sch").write_bytes(b"via dir link")
    elsewhere = project.dir.parent / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.kicad_sch").write_bytes(b"untouched")
    (root / "sub").symlink_to(elsewhere, target_is_directory=True)
    no_kicad(monkeypatch)
    await env.svc.publish_disk_view(project)
    assert outside.read_bytes() == b"secret"
    assert (root / "evil.kicad_sch").read_bytes() == b"view copy"
    assert (root / "link.kicad_sch").is_symlink()
    assert (elsewhere / "x.kicad_sch").read_bytes() == b"untouched"
    assert (root / ROOT).read_bytes() == (project.dir / ROOT).read_bytes()


async def test_disk_view_refuses_a_symlinked_agent_or_view_dir(
    hier: tuple[Env, Project, KicadSession], caplog: pytest.LogCaptureFixture
) -> None:
    """A symlinked `.kicad-agent` or `view` directory: nothing is copied (nor written
    through it), the refusal is logged."""
    _, project, _ = hier
    (project.dir / ROOT).write_bytes(b"(kicad_sch disk)")
    elsewhere = project.dir.parent / "elsewhere"
    (elsewhere / "view").mkdir(parents=True)
    agent = view_dir(project).parent
    shutil.rmtree(agent, ignore_errors=True)
    agent.symlink_to(elsewhere, target_is_directory=True)
    assert copy_disk_schematic(project) == []
    assert list((elsewhere / "view").iterdir()) == []
    agent.unlink()
    agent.mkdir()
    view_dir(project).symlink_to(elsewhere / "view", target_is_directory=True)
    assert copy_disk_schematic(project) == []
    assert list((elsewhere / "view").iterdir()) == []
    assert caplog.text.count("symlinked view directory") == 2


async def test_disk_view_refuses_a_view_dir_outside_the_project(
    hier: tuple[Env, Project, KicadSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, project, _ = hier
    outside = project.dir.parent / "outside-view"
    monkeypatch.setattr(snapshot, "view_dir", lambda p: outside)
    assert copy_disk_schematic(project) == []
    assert not any(outside.iterdir())
    assert "is outside the project" in caplog.text


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
async def test_disk_view_skips_a_fifo_source_without_blocking(
    hier: tuple[Env, Project, KicadSession], caplog: pytest.LogCaptureFixture
) -> None:
    env, project, session = hier
    await env.svc.publish_view(project, session, {"sch"})
    view_sub = (view_dir(project) / SUB).read_bytes()
    (project.dir / SUB).unlink()
    os.mkfifo(project.dir / SUB)
    copied = await asyncio.wait_for(asyncio.to_thread(copy_disk_schematic, project), 5)
    assert copied == [ROOT]
    assert (view_dir(project) / SUB).read_bytes() == view_sub
    assert f"refused for {SUB} (no regular file)" in caplog.text


async def test_disk_view_keeps_a_view_only_sub_sheet(
    hier: tuple[Env, Project, KicadSession], caplog: pytest.LogCaptureFixture
) -> None:
    """A sub-sheet only the view directory holds (not saved yet): skipped silently,
    its view copy stays."""
    env, project, session = hier
    await env.svc.publish_view(project, session, {"sch"})
    view_sub = (view_dir(project) / SUB).read_bytes()
    (project.dir / SUB).unlink()
    assert copy_disk_schematic(project) == [ROOT]
    assert (view_dir(project) / SUB).read_bytes() == view_sub
    assert SUB not in caplog.text


async def test_disk_view_skips_a_file_that_fails_to_copy(
    hier: tuple[Env, Project, KicadSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env, project, session = hier
    await env.svc.publish_view(project, session, {"sch"})
    real = snapshot.atomic_write_bytes

    def failing(path: Path, data: bytes) -> None:
        if path.name == SUB:
            raise OSError(28, "No space left on device")
        real(path, data)

    monkeypatch.setattr(snapshot, "atomic_write_bytes", failing)
    assert copy_disk_schematic(project) == [ROOT]
    assert f"view copy from disk failed for {SUB}" in caplog.text


async def test_disk_view_failure_keeps_the_baseline_and_sends_nothing(
    hier: tuple[Env, Project, KicadSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """copy_disk_schematic raises: the error propagates, no frame is sent and the
    baseline stays what the clients have."""
    env, project, session = hier
    svc = env.svc
    await svc.publish_view(project, session, {"sch"})
    baseline = dict(svc._view_hashes[project.id])

    def boom(project: Project) -> list[str]:
        raise OSError("disk gone")

    monkeypatch.setattr("copperchat.history.service.copy_disk_schematic", boom)
    no_kicad(monkeypatch)
    with pytest.raises(OSError, match="disk gone"):
        await svc.publish_disk_view(project)
    assert len(frames(env)) == 1
    assert svc._view_hashes[project.id] == baseline
    assert session.view_version == 1


async def test_disk_view_without_an_open_session_sends_nothing(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    env, project, session = hier
    await env.svc.publish_view(project, session, {"sch"})
    await env.sessions.close_all()
    (project.dir / ROOT).write_bytes(b"(kicad_sch later)")
    assert await env.svc.publish_disk_view(project) is None
    assert len(frames(env)) == 1
    assert snapshot_path(project, "sch").read_bytes() != b"(kicad_sch later)"


# ---- viewFiles: every frame carries the full list of view files per doc


def view_files(env: Env) -> list[dict[str, list[str]] | None]:
    return [f.view_files for f in env.broadcaster.frames]


def add_sheet(env: Env, project: Project, name: str = "sub/new.kicad_sch") -> str:
    (project.dir / name).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ECC83 / "ecc83-pp.kicad_sch", project.dir / name)
    env.server.hierarchy = [*env.server.hierarchy, name]
    return name


async def test_view_file_list_puts_the_root_first_then_sorts(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    _, project, _ = hier
    names = ["z.kicad_sch", "complex_hierarchy.kicad_pcb", "a/b.kicad_sch", ROOT]
    assert view_file_list(project, "sch", names) == [ROOT, "a/b.kicad_sch", "z.kicad_sch"]
    assert view_file_list(project, "pcb", names) == ["complex_hierarchy.kicad_pcb"]
    # The root is always listed, even when the directory does not hold it (yet).
    assert view_file_list(project, "sch", []) == [ROOT]
    assert view_file_list(project, "pcb", ["x.kicad_sch"]) == ["complex_hierarchy.kicad_pcb"]


async def test_frames_carry_the_view_files_of_added_and_removed_sheets(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    env, project, session = hier
    svc = env.svc
    await svc.publish_view(project, session, {"sch"})
    new = add_sheet(env, project)
    await svc.publish_view(project, session, {"sch"})
    await svc.publish_view(project, session, {"sch"})  # unchanged sheet set
    env.server.hierarchy = [ROOT, new]
    await svc.publish_view(project, session, {"sch"})
    assert view_files(env) == [
        {"sch": [ROOT, SUB]},
        {"sch": [ROOT, SUB, new]},
        {"sch": [ROOT, SUB, new]},
        {"sch": [ROOT, new]},
    ]
    assert [f.changed_files for f in env.broadcaster.frames][1:] == [[new], [], [SUB]]


async def test_view_files_have_one_entry_per_published_doc(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    env, project, session = hier
    await env.svc.publish_view(project, session, {"sch", "pcb"})
    await env.svc.publish_view(project, session, {"pcb"})
    assert view_files(env) == [
        {"sch": [ROOT, SUB], "pcb": ["complex_hierarchy.kicad_pcb"]},
        {"pcb": ["complex_hierarchy.kicad_pcb"]},
    ]


async def test_live_frame_carries_a_new_sheet_and_no_change_sends_nothing(
    hier: tuple[Env, Project, KicadSession],
) -> None:
    env, project, session = hier
    svc = env.svc
    await svc.publish_view(project, session, {"sch"})
    new = add_sheet(env, project)
    assert await svc.publish_view(project, session, {"sch"}, live=True) == 2
    assert await svc.publish_view(project, session, {"sch"}, live=True) == 2  # nothing new
    assert len(env.broadcaster.frames) == 2
    last = env.broadcaster.frames[-1]
    assert last.live is True and last.changed_files == [new]
    assert last.view_files == {"sch": [ROOT, SUB, new]}


async def test_disk_view_frame_carries_the_view_files(
    hier: tuple[Env, Project, KicadSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    env, project, session = hier
    await env.svc.publish_view(project, session, {"sch", "pcb"})
    no_kicad(monkeypatch)
    assert await env.svc.publish_disk_view(project) == 2
    assert view_files(env)[-1] == {"sch": [ROOT, SUB]}


def test_view_files_serialise_as_camel_case_and_are_omitted_when_none() -> None:
    base = {"project_id": "a1b2c3d4e5f6", "docs": ["sch"], "version": 1}
    wire = json.loads(dump_frame(ViewUpdatedFrame(**base, view_files={"sch": [ROOT]})))
    assert wire["viewFiles"] == {"sch": [ROOT]} and "view_files" not in wire
    assert "viewFiles" not in json.loads(dump_frame(ViewUpdatedFrame(**base)))
