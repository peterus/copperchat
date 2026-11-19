"""History undo of footprint writes and the shared library across projects on a real
api-server (F5 Task 3, spec §10, §16.2): the run-engine rig of `test_turn_end.py` with
the real `HistoryService` (library locator + listener) and `restore_snapshot`.

`main` and `io` share the repo's library `footprints/OpenDriveHub.pretty`; one api-server
at a time (C14), so switching projects closes the other one. Every footprint file is
written by KiCad (`fp_create`, `fp_approve`, `fp_delete`); restore copies back only
KiCad-written bytes from its blobs."""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from copperchat.config import Settings
from copperchat.history import library_lock as library_lock_module
from copperchat.history.hooks import HistoryHooks
from copperchat.history.restore import restore_snapshot
from copperchat.history.store import Snapshot
from copperchat.kicad_session import KicadSessionManager
from copperchat.kicad_tools.footprints import engine as fp_engine
from copperchat.library.service import library_for
from copperchat.projects import Project, discover_projects
from tests.kicad.footprints.test_fp_approve import OK_QUOTE, refused, tool, user_says
from tests.kicad.footprints.test_fp_create import (
    LIB_ID,
    NAME,
    NICK,
    journal,
    l201_args,
    lib_dir,
    project_rows,
    run_create,
)
from tests.kicad.footprints.test_turn_end import RUN1, RUN2, Rig, open_rig
from tests.kicad.footprints.test_turn_end import rig as rig
from tests.support.conversations import TEST_CID
from tests.support.edit import call_tool, edit_ctx
from tests.support.kicad_project import real_settings

pytestmark = pytest.mark.kicad

RUN3 = "run-fp-3"
IO1, IO2 = "run-io-1", "run-io-2"
IO_NAME = "L_IO_Part"
IO_LIB_ID = f"{NICK}:{IO_NAME}"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project_named(settings: Settings, root: Path, name: str) -> Project:
    project_dir = (root / "hardware" / name).resolve()
    return next(
        p for p in discover_projects(settings.projects_root) if p.dir.resolve() == project_dir
    )


async def switch(rig: Rig, name: str) -> Rig:
    """The rig on project `hardware/<name>` of the same repo, history service and
    database: its api-server is opened (closing the other one, C14) with its own history
    hooks, as the run engine of that project would have them."""
    project = project_named(rig.session.settings, rig.root, name)
    session = await rig.history.sessions.open(project)
    hooks = HistoryHooks(rig.history, project, cast(Any, rig.runtime.engine))
    await hooks.on_engine_start()
    return dataclasses.replace(rig, session=session, hooks=hooks, snapshotted=set())


async def turn_with_create(rig: Rig, run_id: str, **kw: Any) -> dict[str, Any]:
    """One turn: start, `fp_create` (the turn snapshot is taken first), end."""
    await rig.hooks.on_turn_start(rig.turn(run_id))
    out = await tool(rig, run_id, "fp_create", **l201_args(**kw))
    await rig.end_turn(run_id)
    return out


async def snapshot_of(rig: Rig, run_id: str) -> Snapshot:
    snap = await rig.history.store(rig.session.project).latest_for_run(run_id)
    assert snap is not None
    return snap


async def restore(rig: Rig, project: Project, snap: Snapshot) -> Rig:
    """Restore `snap` of `project`; returns the rig on the session the restore left open
    (a reopened one if `project` was open)."""
    await restore_snapshot(rig.history, project, snap.id, conversation_id=TEST_CID)
    current = rig.history.sessions.current()
    assert current is not None
    return dataclasses.replace(rig, session=current)


def notices(rig: Rig) -> list[tuple[str, dict[str, Any]]]:
    return [(text, meta or {}) for text, meta in rig.runtime.engine.notices]


def restore_notice(rig: Rig) -> str:
    texts = [text for text, meta in notices(rig) if meta.get("action") == "restored"]
    assert texts, rig.runtime.engine.notices
    return texts[-1]


# -- undo of a single project ---------------------------------------------------------------


async def test_undo_first_create(rig: Rig) -> None:
    """Snapshot without row and dir -> fp_create -> restore: the footprint, the empty
    library dir and the fp-lib-table are gone; after the reopen fp_get says not_found and
    the next turn start reports nothing."""
    project = rig.session.project
    table = project.dir.resolve() / "fp-lib-table"
    assert not table.exists() and not lib_dir(rig.root).exists()
    await turn_with_create(rig, RUN1)
    file = lib_dir(rig.root) / f"{NAME}.kicad_mod"
    assert file.is_file() and table.is_file()
    snap = await snapshot_of(rig, RUN1)

    after = await restore(rig, project, snap)
    assert after.session is not rig.session  # reopened from the restored files
    assert not file.exists() and not lib_dir(rig.root).exists()
    assert not table.exists()
    assert NICK not in await project_rows(after.session)
    error = await refused(after, RUN2, "fp_get", lib_id=LIB_ID)
    assert error["code"] == "not_found", error

    after.forget_reports()
    await after.hooks.on_turn_start(after.turn(RUN2))
    assert after.reported() == [], after.reported()


async def io_creates(settings: Settings, sessions: KicadSessionManager, root: Path) -> None:
    """`io` makes its own footprint (its row; KiCad creates the library directory)."""
    io = await sessions.open(project_named(settings, root, "io"))
    out, err = await call_tool(edit_ctx(io), "fp_create", **l201_args(name=IO_NAME))
    if err:
        pytest.fail(json.dumps(out, indent=1))


@pytest.fixture
async def rig_dir_from_io(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> AsyncIterator[Rig]:
    settings = real_settings(make_settings)
    before = partial(io_creates, settings)
    async with open_rig(settings, tmp_path / "db.sqlite", "main", before=before) as r:
        yield r


async def test_undo_create_with_existing_dir(rig_dir_from_io: Rig) -> None:
    """The library dir exists from `io` (with io's footprint): restoring main's turn
    removes only main's footprint and main's table; the dir, io's footprint and io's
    table stay byte-identical."""
    rig = rig_dir_from_io
    project = rig.session.project
    io_file = lib_dir(rig.root) / f"{IO_NAME}.kicad_mod"
    io_table = rig.root.resolve() / "hardware" / "io" / "fp-lib-table"
    kept = {io_file: sha(io_file), io_table: sha(io_table)}
    await turn_with_create(rig, RUN1)
    file = lib_dir(rig.root) / f"{NAME}.kicad_mod"
    assert file.is_file()

    after = await restore(rig, project, await snapshot_of(rig, RUN1))
    assert not file.exists()
    assert {p: sha(p) for p in kept} == kept
    assert lib_dir(rig.root).is_dir()
    assert not (project.dir.resolve() / "fp-lib-table").exists()
    assert NICK not in await project_rows(after.session)


async def test_undo_delete(rig: Rig) -> None:
    """fp_delete -> restore of that turn: the footprint is back byte-identical (the
    journal recorded `absent` after the delete) and readable after the reopen."""
    project = rig.session.project
    await turn_with_create(rig, RUN1)
    file = lib_dir(rig.root) / f"{NAME}.kicad_mod"
    before = sha(file)
    await rig.hooks.on_turn_start(rig.turn(RUN2))
    digest = (await tool(rig, RUN2, "fp_get", lib_id=LIB_ID))["geometry_hash"]
    await tool(rig, RUN2, "fp_delete", lib_id=LIB_ID)
    await rig.end_turn(RUN2)
    assert not file.exists()
    delete = (await journal(rig.db, rig.session))[-1]
    assert (delete.op, delete.state, delete.post_sha256) == ("delete", "done", "absent")

    after = await restore(rig, project, await snapshot_of(rig, RUN2))
    assert sha(file) == before
    got = await tool(after, RUN3, "fp_get", lib_id=LIB_ID)
    assert got["geometry_hash"] == digest and got["review"] == "needs_user_review"


async def test_leftover_removed_by_undo(rig: Rig) -> None:
    """A failed compensation (F4a hook `fail_compensation`) leaves the footprint and the
    row as `leftover` (`document_inconsistent`); restoring the turn snapshot removes both,
    the table and the directory."""
    project = rig.session.project
    await rig.hooks.on_turn_start(rig.turn(RUN1))
    out, err = await run_create(
        rig.ctx(RUN1), l201_args(), fail_after="rebuild_saved", fail_compensation=True
    )
    assert err and out["error"]["code"] == "document_inconsistent", out
    file = lib_dir(rig.root) / f"{NAME}.kicad_mod"
    assert file.is_file() and NICK in await project_rows(rig.session)
    rows = await journal(rig.db, rig.session)
    assert [(r.op, r.state) for r in rows] == [("row_add", "leftover"), ("create", "leftover")]
    await rig.end_turn(RUN1)

    after = await restore(rig, project, await snapshot_of(rig, RUN1))
    assert not file.exists() and not lib_dir(rig.root).exists()
    assert not (project.dir.resolve() / "fp-lib-table").exists()
    assert NICK not in await project_rows(after.session)


# -- across projects ------------------------------------------------------------------------


@pytest.mark.parametrize("io_approves", [True, False], ids=["io-approves", "io-only-creates"])
async def test_cross_project(rig: Rig, io_approves: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    """`main` creates A (L201); `io` creates B and - in one variant - approves A. Main's
    next turn start sees io's writes as a library-only change (notice, the libraries
    reloaded, no plan pause / external-change listener). Restoring main to before A then
    skips A when io approved it ("changed outside this project, not restored") and removes
    it otherwise; B is never touched."""
    main_project = rig.session.project
    await turn_with_create(rig, RUN1)
    a_file = lib_dir(rig.root) / f"{NAME}.kicad_mod"
    b_file = lib_dir(rig.root) / f"{IO_NAME}.kicad_mod"
    snap = await snapshot_of(rig, RUN1)

    io = await switch(rig, "io")
    await io.hooks.on_turn_start(io.turn(IO1))
    created = await tool(io, IO1, "fp_create", **l201_args(name=IO_NAME))
    assert created["created"] == [IO_LIB_ID]
    if io_approves:
        digest = (await tool(io, IO1, "fp_render", lib_id=LIB_ID))["geometry_hash"]
        assert (await tool(io, IO1, "fp_check", lib_id=LIB_ID))["passed"]
    await io.end_turn(IO1)
    if io_approves:
        await user_says(io, IO2)
        args = {"lib_id": LIB_ID, "geometry_hash": digest, "user_quote": OK_QUOTE}
        assert (await tool(io, IO2, "fp_approve", **args))["review"] == "approved"
        await io.end_turn(IO2)
    a_sha, b_sha = sha(a_file), sha(b_file)

    # main's next turn: a library-only change, the loaded libraries are reloaded
    main = await switch(rig, "main")
    main = dataclasses.replace(main, hooks=rig.hooks)  # main's run engine hooks
    await library_for(main.session).activate_footprints()
    reloaded: list[str] = []
    real_reload = fp_engine._reload_footprint_library

    def spy(nickname: str, h: Any, *args: Any) -> None:
        reloaded.append(nickname)
        real_reload(nickname, h, *args)

    monkeypatch.setattr(fp_engine, "_reload_footprint_library", spy)
    external: list[list[str]] = []

    async def on_external(_project: Project, files: list[str]) -> None:
        external.append(files)

    rig.history.on_external_change.append(on_external)
    main.forget_reports()
    await main.hooks.on_turn_start(main.turn(RUN2))
    actions = [meta.get("action") for _text, meta in notices(main)]
    assert actions == ["library-external-change"], main.reported()
    changed = {Path(f).stem for _t, m in notices(main) for f in m["files"]}
    assert changed == ({IO_NAME, NAME} if io_approves else {IO_NAME})
    assert reloaded == [NICK] and external == []
    got = await tool(main, RUN2, "fp_get", lib_id=LIB_ID)
    assert got["review"] == ("approved" if io_approves else "needs_user_review")
    await main.end_turn(RUN2)

    main.forget_reports()
    after = await restore(main, main_project, snap)
    text = restore_notice(after)
    assert sha(b_file) == b_sha
    if io_approves:
        assert f"{LIB_ID}: changed outside this project, not restored" in text, text
        assert sha(a_file) == a_sha
    else:
        assert "changed outside" not in text, text
        assert not a_file.exists()
    assert lib_dir(rig.root).is_dir()  # B keeps it
    assert NICK not in await project_rows(after.session)

    after.forget_reports()
    await after.hooks.on_turn_start(after.turn(RUN3))
    assert after.reported() == [], after.reported()


# -- restore and create running at the same time --------------------------------------------

HOLDER: contextvars.ContextVar[str] = contextvars.ContextVar("holder", default="?")


class RecordingLock(asyncio.Lock):
    """The library lock, recording `acquire` / `wait` / `release` per holder (the task's
    `HOLDER`). The first `create` acquire keeps the lock until the `restore` waits for it,
    so the two always contend (deterministic order)."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, str]] = []
        self.owner: str | None = None
        self.first_create = asyncio.Event()
        self.restore_waits = asyncio.Event()

    async def acquire(self) -> Literal[True]:
        who = HOLDER.get()
        if self.locked():
            self.events.append(("wait", who))
            if who == "restore":
                self.restore_waits.set()
        await super().acquire()
        self.owner = who
        self.events.append(("acquire", who))
        if who == "create" and not self.first_create.is_set():
            self.first_create.set()
            try:
                await asyncio.wait_for(self.restore_waits.wait(), 30)
            except BaseException:
                self.release()  # never leak the held lock on a handshake timeout
                raise
        return True

    def release(self) -> None:
        self.events.append(("release", self.owner or "?"))
        self.owner = None
        super().release()


async def test_concurrent_restore_and_create(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """A restore of `main` (project not open) and an `fp_create` in `io` at the same time:
    they serialise on the library lock (never both inside), both succeed, and no KiCad
    call of io's runs while the restore holds the lock."""
    main_project = rig.session.project
    await turn_with_create(rig, RUN1)
    a_file = lib_dir(rig.root) / f"{NAME}.kicad_mod"
    snap = await snapshot_of(rig, RUN1)

    io = await switch(rig, "io")  # main's server is closed now
    lock = RecordingLock()
    monkeypatch.setitem(library_lock_module._LOCKS, lib_dir(rig.root), lock)
    calls: list[str | None] = []  # the lock's owner at the start of each io KiCad call
    real_call = io.session.call

    async def call(fn: Any, **kw: Any) -> Any:
        calls.append(lock.owner)
        return await real_call(fn, **kw)

    monkeypatch.setattr(io.session, "call", call)
    await io.hooks.on_turn_start(io.turn(IO1))

    async def create() -> dict[str, Any]:
        HOLDER.set("create")
        return await tool(io, IO1, "fp_create", **l201_args(name=IO_NAME))

    async def restore_main() -> Snapshot:
        HOLDER.set("restore")
        await asyncio.wait_for(lock.first_create.wait(), 120)
        return await restore_snapshot(rig.history, main_project, snap.id, conversation_id=TEST_CID)

    created, restored = await asyncio.gather(create(), restore_main())
    assert created["created"] == [IO_LIB_ID] and restored.id == snap.id

    # never two holders: every acquire is followed by its own release
    held: str | None = None
    for kind, who in lock.events:
        if kind == "acquire":
            assert held is None, lock.events
            held = who
        elif kind == "release":
            assert held == who, lock.events
            held = None
    assert held is None, lock.events
    order = [who for kind, who in lock.events if kind == "acquire"]
    assert order.count("restore") == 1 and order[0] == "create", lock.events
    assert ("wait", "restore") in lock.events
    assert "create" in order[order.index("restore") + 1 :], lock.events  # apply after it
    assert calls and "restore" not in calls, calls
    assert "create" in calls  # io's write stages ran under the lock

    # main had no open server: its own footprint is removed, its row stays (reported)
    assert not a_file.exists()
    assert (lib_dir(rig.root) / f"{IO_NAME}.kicad_mod").is_file()
    text = restore_notice(rig)
    assert f"library row {NICK} not removed (project not open)" in text, text
