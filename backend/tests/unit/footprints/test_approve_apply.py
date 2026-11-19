"""`fp_approve` apply without KiCad (F4b Task 1, spec §7.7, §9.3): the stage's
bookkeeping on success and on every failure path (compensated, leftover, server death,
cancel) against a fake footprint document. The geometry is the rig's `build_target`
(the fake document serves it, nothing is parsed); the footprint "file" is opaque bytes
the fake save overwrites."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.db.repos.footprints import FootprintWriteRepo
from copperchat.editing.service import EditingService
from copperchat.errors import DocumentInconsistent, StructuredToolError
from copperchat.history.library_lock import library_lock
from copperchat.kicad_session import KicadUnavailable
from copperchat.kicad_tools.footprints import approve as approve_mod
from copperchat.kicad_tools.footprints.approve import apply_approve, prepare_approve
from copperchat.kicad_tools.footprints.engine import FootprintChangeSet
from copperchat.kicad_tools.footprints.model import APPROVAL_FIELD, REVIEW_FIELD, FootprintGeometry
from copperchat.library import service as service_mod
from copperchat.tools import ToolContext
from tests.support.conversations import ensure_test_conversation
from tests.unit.footprints.test_approve_guards import (
    LIB_ID,
    PID,
    ApproveSession,
    Rig,
    _args,
    _target,
)
from tests.unit.footprints.test_create_prepare import NAME
from tests.unit.library.test_service import REPO, _policy

FILE_BYTES = b"opaque footprint bytes"
SAVED_BYTES = b"opaque saved bytes"


class FakeDoc:
    """The open footprint document: `begin` / `push` / `drop` / `save`, each failing
    on demand (`session.fail[step]`); `save` writes `SAVED_BYTES` first unless
    `session.save_writes` is False, and then runs `session.after_save`."""

    def __init__(self, session: ApplySession) -> None:
        self.s = session
        self.open_commit = False

    async def footprint(self) -> Any:
        return None  # `_doc_geometry` is patched: the rig's geometry

    async def begin(self) -> None:
        self.open_commit = True

    async def push(self, message: str) -> None:
        self.s.steps.append("push")
        if (exc := self.s.fail.get("push")) is not None:
            raise exc
        self.open_commit = False

    async def drop(self) -> None:
        self.s.steps.append("drop")
        self.open_commit = False

    async def save(self) -> None:
        self.s.steps.append("save")
        if self.s.save_writes:
            self.s.file.write_bytes(SAVED_BYTES)
        if self.s.after_save is not None:
            self.s.after_save()
        if (exc := self.s.fail.get("save")) is not None:
            raise exc


class ApplySession(ApproveSession):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.steps: list[str] = []
        self.fail: dict[str, BaseException] = {}
        self.save_writes = True
        self.after_save: Callable[[], None] | None = None
        self.file = root / "footprints" / f"{REPO}.pretty" / f"{NAME}.kicad_mod"

    @contextlib.asynccontextmanager
    async def footprint_document(self, lib_id: str) -> Any:
        assert lib_id == LIB_ID
        yield FakeDoc(self)


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    s = ApplySession(tmp_path / REPO)

    async def policy(project: Any, projects_root: Path) -> Any:
        return _policy(s.root, project.dir)

    monkeypatch.setattr(service_mod, "repo_policy", policy)
    db = await Database.open(tmp_path / "db.sqlite")
    await ensure_test_conversation(db, PID)
    editing = EditingService(db=db, broadcaster=None, settings_repo=EditingSettingsRepo(db))
    r = Rig(s, db, editing, _target())

    async def geometry(
        self: Any, lib_id: str, notes: list[dict[str, Any]] | None = None
    ) -> FootprintGeometry | None:
        return r.geometry if lib_id == LIB_ID else None

    async def commit(doc: Any, value: str) -> None:
        s.steps.append("commit")
        s.value = value  # type: ignore[attr-defined]

    def approved() -> None:
        """What a good save reads back: the same geometry, approved."""
        fields = {**r.geometry.fields, REVIEW_FIELD: "no", APPROVAL_FIELD: s.value}  # type: ignore[attr-defined]
        r.geometry = dataclasses.replace(r.geometry, fields=fields)

    monkeypatch.setattr(service_mod.LibraryService, "footprint_geometry", geometry)
    monkeypatch.setattr(approve_mod, "_doc_geometry", lambda instance, lib_id: r.geometry)
    monkeypatch.setattr(approve_mod, "_approve_commit", commit)
    s.after_save = approved
    s.file.parent.mkdir(parents=True)
    s.file.write_bytes(FILE_BYTES)
    try:
        yield r
    finally:
        await db.close()


def session(rig: Rig) -> ApplySession:
    assert isinstance(rig.session, ApplySession)
    return rig.session


async def _planned(rig: Rig) -> tuple[ToolContext, FootprintChangeSet]:
    await rig.shown()
    await rig.said()
    ctx = rig.ctx()
    return ctx, await prepare_approve(ctx, _args(rig))


async def _apply(ctx: ToolContext, cs: FootprintChangeSet) -> Any:
    assert cs.library is not None
    async with library_lock(cs.library.dir):
        return await apply_approve(ctx, cs)


async def _journal(rig: Rig) -> list[tuple[str, str]]:
    rows = await FootprintWriteRepo(rig.db).after_id(PID, 0)
    return [(r.op, r.state) for r in rows]


async def test_apply_approves_and_journals(rig: Rig) -> None:
    ctx, cs = await _planned(rig)
    result = await _apply(ctx, cs)
    assert session(rig).steps == ["commit", "push", "save"]
    assert result.data["approved"] == LIB_ID and result.data["review"] == "approved"
    assert result.library_changed == {REPO} and result.changed_docs == set()
    assert session(rig).file in ctx.library_touched
    rows = await FootprintWriteRepo(rig.db).after_id(PID, 0)
    assert [(r.op, r.state) for r in rows] == [("approve", "done")]
    assert rows[0].post_sha256 != rows[0].pre_sha256


async def test_save_fails_after_writing_is_leftover(rig: Rig) -> None:
    """The save changed the file and then failed: not verifiable -> `leftover` and a
    footprint-only `DocumentInconsistent` naming it as replaced (never to be deleted)."""
    from kipy.errors import ApiError

    session(rig).fail["save"] = ApiError("save failed")
    ctx, cs = await _planned(rig)
    with pytest.raises(DocumentInconsistent) as err:
        await _apply(ctx, cs)
    assert err.value.leftovers == [LIB_ID] and err.value.replaced == [LIB_ID]
    assert err.value.footprint_only
    assert await _journal(rig) == [("approve", "leftover")]


@pytest.mark.parametrize("step", ["push", "save"])
async def test_api_error_file_unchanged_compensated(rig: Rig, step: str) -> None:
    from kipy.errors import ApiError

    s = session(rig)
    s.fail[step] = ApiError(f"{step} refused")
    s.save_writes = False
    s.after_save = None
    ctx, cs = await _planned(rig)
    with pytest.raises(StructuredToolError) as err:
        await _apply(ctx, cs)
    assert err.value.code == "library_error"
    assert s.file.read_bytes() == FILE_BYTES
    if step == "push":
        assert s.steps == ["commit", "push", "drop"]  # the open commit is dropped
    rows = await FootprintWriteRepo(rig.db).after_id(PID, 0)
    assert [(r.op, r.state) for r in rows] == [("approve", "compensated")]
    assert rows[0].post_sha256 == rows[0].pre_sha256


async def test_readback_hash_differs_is_leftover(rig: Rig) -> None:
    s = session(rig)
    s.after_save = lambda: setattr(
        rig, "geometry", dataclasses.replace(rig.geometry, pads=rig.geometry.pads[:1])
    )
    ctx, cs = await _planned(rig)
    with pytest.raises(DocumentInconsistent) as err:
        await _apply(ctx, cs)
    assert err.value.leftovers == [LIB_ID] and err.value.footprint_only
    assert isinstance(err.value.__context__, StructuredToolError)
    assert err.value.__context__.code == "verification_failed"
    assert await _journal(rig) == [("approve", "leftover")]


async def test_server_dead_keeps_pending(rig: Rig) -> None:
    session(rig).fail["save"] = KicadUnavailable("the KiCad api-server had stopped")
    ctx, cs = await _planned(rig)
    with pytest.raises(KicadUnavailable):
        await _apply(ctx, cs)
    assert await _journal(rig) == [("approve", "pending")]


async def test_generation_change_keeps_pending(rig: Rig) -> None:
    """A restart during the call (generation bumped), whatever the error: no settling,
    `KicadUnavailable`, the row stays pending for the cleanup."""
    from kipy.errors import ApiError

    s = session(rig)

    def restarted() -> None:
        s.generation += 1

    s.after_save = restarted
    s.fail["save"] = ApiError("connection reset")
    ctx, cs = await _planned(rig)
    with pytest.raises(KicadUnavailable):
        await _apply(ctx, cs)
    assert await _journal(rig) == [("approve", "pending")]


async def test_cancel_during_save_settles(rig: Rig) -> None:
    """A Stop during the save: the journal is settled to completion (`leftover`, the
    file changed), the turn hears of it through `on_document_inconsistent`, and the
    cancel propagates."""
    session(rig).fail["save"] = asyncio.CancelledError()
    ctx, cs = await _planned(rig)
    seen: list[Exception] = []
    ctx.on_document_inconsistent = seen.append
    with pytest.raises(asyncio.CancelledError):
        await _apply(ctx, cs)
    [failure] = seen
    assert isinstance(failure, DocumentInconsistent) and failure.leftovers == [LIB_ID]
    assert await _journal(rig) == [("approve", "leftover")]


async def test_cancel_before_save_compensated(rig: Rig) -> None:
    s = session(rig)
    s.fail["push"] = asyncio.CancelledError()
    ctx, cs = await _planned(rig)
    seen: list[Exception] = []
    ctx.on_document_inconsistent = seen.append
    with pytest.raises(asyncio.CancelledError):
        await _apply(ctx, cs)
    assert seen == [] and s.file.read_bytes() == FILE_BYTES
    assert await _journal(rig) == [("approve", "compensated")]


@pytest.mark.parametrize("rows", [{}, {REPO: "${KIPRJMOD}/elsewhere.pretty"}])
async def test_row_changed_refused(rig: Rig, rows: dict[str, str]) -> None:
    """The project row is read again under the library lock: gone or another URI ->
    `library_conflict`, no document opened, nothing journalled."""
    ctx, cs = await _planned(rig)
    session(rig).rows = rows
    with pytest.raises(StructuredToolError) as err:
        await _apply(ctx, cs)
    assert err.value.code == "library_conflict"
    assert session(rig).steps == []
    assert session(rig).file.read_bytes() == FILE_BYTES
    assert await _journal(rig) == []


# -- review loop round 2 -------------------------------------------------------------------


async def test_real_cancel_during_settle_completes_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real `task.cancel()` while `_settle` is blocked (after a failed save that changed
    the file): the settling finishes (`leftover`), the turn hears of it through
    `on_document_inconsistent`, and the CancelledError propagates."""
    from kipy.errors import ApiError

    session(rig).fail["save"] = ApiError("save failed")
    entered, release = asyncio.Event(), asyncio.Event()
    real_sha = approve_mod._sha_or_absent

    async def blocked_sha(path: Path) -> str:
        entered.set()
        await release.wait()
        return await real_sha(path)

    monkeypatch.setattr(approve_mod, "_sha_or_absent", blocked_sha)
    ctx, cs = await _planned(rig)
    seen: list[Exception] = []
    ctx.on_document_inconsistent = seen.append
    task = asyncio.ensure_future(_apply(ctx, cs))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()  # settling still runs
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    [failure] = seen
    assert isinstance(failure, DocumentInconsistent) and failure.leftovers == [LIB_ID]
    assert await _journal(rig) == [("approve", "leftover")]
