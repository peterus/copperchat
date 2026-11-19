"""`fp_delete` without KiCad (F4b Task 2, spec §7.8, §12): the budget (1 logical change,
never a pre-existing symbol delete), the prepare refusals, the sibling-project note and
the apply bookkeeping (journal, compensation) against a fake session. The footprint
"files" here are opaque bytes the fake serves nothing from (never parsed); the scanned
sibling files are opaque bytes too."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.db.repos.footprints import FootprintWriteRepo
from copperchat.editing.gate import MAX_PREEXISTING_SYMBOL_DELETES, BudgetGate
from copperchat.editing.service import EditingService
from copperchat.errors import DocumentInconsistent, StructuredToolError
from copperchat.history.library_lock import library_lock
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.kicad_tools.footprints import engine as engine_mod
from copperchat.kicad_tools.footprints.delete import apply_delete, prepare_delete
from copperchat.kicad_tools.footprints.engine import FootprintChangeSet
from copperchat.kicad_tools.footprints.model import FootprintGeometry, geometry_hash
from copperchat.kicad_tools.footprints.specs import TOOL_SPECS
from copperchat.library import service as service_mod
from copperchat.tools import ToolContext
from tests.support.conversations import ensure_test_conversation
from tests.unit.footprints.test_approve_guards import (
    LIB_ID,
    PID,
    RUN1,
    ApproveSession,
    Rig,
    _target,
)
from tests.unit.footprints.test_create_prepare import NAME
from tests.unit.library.test_service import REPO, _policy

FILE_BYTES = b"opaque footprint bytes"


class DeleteSession(ApproveSession):
    """ApproveSession plus the open schematic's Footprint users (`users`: lib_id ->
    refs) and a fake `DeleteLibraryItem` (removes the name and the file, or raises
    `delete_error`)."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.users: dict[str, list[str]] = {}
        self.deleted: list[tuple[str, str]] = []
        self.delete_error: Exception | None = None
        self.keep_file = False

    async def call(self, fn: Any) -> Any:
        func = getattr(fn, "func", fn)
        if getattr(func, "__name__", "") == "footprint_users":
            return {k: list(v) for k, v in self.users.items()}
        if func is engine_mod._delete_item:
            nickname, name = fn.args
            self.deleted.append((nickname, name))
            if self.delete_error is not None:
                raise self.delete_error
            if name in self.names:
                self.names.remove(name)
            file = self.root / "footprints" / f"{nickname}.pretty" / f"{name}.kicad_mod"
            if not self.keep_file:
                file.unlink(missing_ok=True)
            return None
        return await super().call(fn)


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    """The fp_approve rig (`test_approve_guards`) with a DeleteSession and the
    footprint file on disk."""
    s = DeleteSession(tmp_path / REPO)

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

    monkeypatch.setattr(service_mod.LibraryService, "footprint_geometry", geometry)
    pretty = r.lib().dir
    pretty.mkdir(parents=True)
    (pretty / f"{NAME}.kicad_mod").write_bytes(FILE_BYTES)
    try:
        yield r
    finally:
        await db.close()


def session(rig: Rig) -> DeleteSession:
    assert isinstance(rig.session, DeleteSession)
    return rig.session


async def _refused(rig: Rig, args: dict[str, Any] | None = None) -> StructuredToolError:
    with pytest.raises(StructuredToolError) as err:
        await prepare_delete(rig.ctx(), args if args is not None else {"lib_id": LIB_ID})
    return err.value


async def _apply(ctx: ToolContext, cs: FootprintChangeSet) -> Any:
    assert cs.library is not None
    async with library_lock(cs.library.dir):
        return await apply_delete(ctx, cs)


# -- budget (spec §12) -------------------------------------------------------------------


async def test_delete_budget_one_not_preexisting_delete(rig: Rig) -> None:
    """`Logical(1)` and no `deleted_symbols`: four fp_delete calls cost 4 and never
    reach MAX_PREEXISTING_SYMBOL_DELETES."""
    cs = await prepare_delete(rig.ctx(), {"lib_id": LIB_ID})
    assert isinstance(cs, FootprintChangeSet)
    assert cs.tool == "fp_delete" and cs.logical == Logical(1)
    assert cs.logical.deleted_symbols == []
    assert MAX_PREEXISTING_SYMBOL_DELETES < 4
    gate = BudgetGate(rig.editing)
    ctx = rig.ctx()
    for _ in range(4):
        await gate.check_budget(ctx, cs)
        await gate.record(ctx, cs)
    state = rig.editing.turn(PID, ctx.run_id)
    assert state.logical_used == 4 and state.deleted_preexisting == 0
    assert await gate.budget_info(ctx) == {"used": 4, "limit": 20}


# -- prepare -----------------------------------------------------------------------------


async def test_prepare_plans_one_delete_stage(rig: Rig) -> None:
    cs = await prepare_delete(rig.ctx(), {"lib_id": LIB_ID})
    assert [(s.kind, s.lib_id) for s in cs.fp_stages] == [("delete", LIB_ID)]
    assert cs.library is not None and cs.library.nickname == REPO
    assert cs.summary["lib_id"] == LIB_ID and cs.summary["geometry_hash"] == rig.digest
    assert cs.summary["notes"] == []


async def test_unknown_key_invalid(rig: Rig) -> None:
    err = await _refused(rig, {"lib_id": LIB_ID, "force": True})
    assert err.code == "invalid_argument"


async def test_not_found(rig: Rig) -> None:
    err = await _refused(rig, {"lib_id": f"{REPO}:Nope"})
    assert err.code == "not_found"


async def test_stock_not_local(rig: Rig) -> None:
    err = await _refused(rig, {"lib_id": "Inductor_SMD:L_Bourns-SRN4018"})
    assert err.code == "not_local"


async def test_no_row_not_local(rig: Rig) -> None:
    session(rig).rows = {}
    err = await _refused(rig)
    assert err.code == "not_local"


async def test_row_conflict(rig: Rig) -> None:
    session(rig).rows = {REPO: "${KIPRJMOD}/elsewhere.pretty"}
    err = await _refused(rig)
    assert err.code == "library_conflict"


async def test_in_use_names_refs(rig: Rig) -> None:
    session(rig).users = {LIB_ID: ["L1", "L7"], f"{REPO}:Other": ["L2"]}
    err = await _refused(rig)
    assert err.code == "in_use"
    assert err.details is not None and err.details["refs"] == ["L1", "L7"]
    assert "L1" in err.why


async def test_cleanable_copy_not_found(rig: Rig) -> None:
    """An untouched seed copy of a call the server died in: the next write's cleanup
    removes it, so prepare reports `not_found` (never deletes it itself)."""
    file = rig.lib().dir / f"{NAME}.kicad_mod"
    sha = engine_mod.file_sha256(file)
    repo = FootprintWriteRepo(rig.db)
    row = await repo.insert_pending(
        PID, RUN1, LIB_ID, f"footprints/{REPO}.pretty/{NAME}.kicad_mod", "create", "absent"
    )
    await repo.set_post_s2(row, sha)
    err = await _refused(rig)
    assert err.code == "not_found"
    assert file.read_bytes() == FILE_BYTES


async def test_symlinked_dir_refused(rig: Rig, tmp_path: Path) -> None:
    footprints = rig.session.root / "footprints"
    moved = tmp_path / "moved"
    footprints.rename(moved)
    footprints.symlink_to(moved, target_is_directory=True)
    err = await _refused(rig)
    assert err.code == "library_conflict"


async def test_symlinked_file_refused(rig: Rig, tmp_path: Path) -> None:
    file = rig.lib().dir / f"{NAME}.kicad_mod"
    file.rename(tmp_path / "other.kicad_mod")
    file.symlink_to(tmp_path / "other.kicad_mod")
    err = await _refused(rig)
    assert err.code == "library_conflict"
    assert file.is_symlink()


async def test_possibly_in_use_elsewhere_note(rig: Rig) -> None:
    io = rig.session.root / "hw" / "io"
    io.mkdir(parents=True)
    (io / "io.kicad_pro").write_bytes(b"")
    (io / "io.kicad_sch").write_bytes(b'opaque "' + LIB_ID.encode() + b'" bytes')
    cs = await prepare_delete(rig.ctx(), {"lib_id": LIB_ID})
    [note] = cs.summary["notes"]
    assert note["kind"] == "possibly_in_use_elsewhere"
    assert note["files"] == ["hw/io/io.kicad_sch"] and note["complete"] is True
    assert str(rig.session.root) not in str(note)


async def test_incomplete_scan_noted(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from copperchat.kicad_tools.footprints import usage

    monkeypatch.setattr(usage, "MAX_DIRS", 1)
    (rig.session.root / "more").mkdir()
    cs = await prepare_delete(rig.ctx(), {"lib_id": LIB_ID})
    [note] = cs.summary["notes"]
    assert note["kind"] == "possibly_in_use_elsewhere"
    assert note["files"] == [] and note["complete"] is False


async def test_own_board_in_use_note(rig: Rig) -> None:
    """`in_use` reads only the open schematic: the own board placing the footprint is an
    `in_use_on_board` note (no refusal); its autosave is not searched."""
    project_dir = rig.session.project.dir
    (project_dir / "main.kicad_pro").write_bytes(b"")
    (project_dir / "main.kicad_pcb").write_bytes(b'opaque "' + LIB_ID.encode() + b'" bytes')
    (project_dir / "_autosave-main.kicad_pcb").write_bytes(b'"' + LIB_ID.encode() + b'"')
    cs = await prepare_delete(rig.ctx(), {"lib_id": LIB_ID})
    [note] = cs.summary["notes"]
    assert note["kind"] == "in_use_on_board" and note["complete"] is True
    assert note["files"] == ["hw/main/main.kicad_pcb"]
    assert "main.kicad_pcb" in note["message"]
    assert str(rig.session.root) not in str(note)


async def test_own_board_without_footprint_no_note(rig: Rig) -> None:
    project_dir = rig.session.project.dir
    (project_dir / "main.kicad_pro").write_bytes(b"")
    (project_dir / "main.kicad_pcb").write_bytes(b"opaque bytes")
    cs = await prepare_delete(rig.ctx(), {"lib_id": LIB_ID})
    assert cs.summary["notes"] == []


# -- apply -------------------------------------------------------------------------------


async def test_apply_deletes_and_journals(rig: Rig) -> None:
    ctx = rig.ctx()
    file = rig.lib().dir / f"{NAME}.kicad_mod"
    pre = engine_mod.file_sha256(file)
    cs = await prepare_delete(ctx, {"lib_id": LIB_ID})
    result = await _apply(ctx, cs)
    assert session(rig).deleted == [(REPO, NAME)]
    assert not file.exists()
    assert result.library_changed == {REPO} and result.changed_docs == set()
    assert result.data["deleted"] == LIB_ID
    assert result.data["file"] == f"footprints/{REPO}.pretty/{NAME}.kicad_mod"
    assert result.data["logical_changes"] == 1
    assert file in ctx.library_touched
    rows = await FootprintWriteRepo(rig.db).after_id(PID, 0)
    assert [(r.op, r.state, r.lib_id) for r in rows] == [("delete", "done", LIB_ID)]
    assert rows[0].pre_sha256 == pre and rows[0].post_sha256 == "absent"
    assert rows[0].geometry_hash == rig.digest


async def test_apply_kicad_refuses_compensated(rig: Rig) -> None:
    from kipy.errors import ApiError

    session(rig).delete_error = ApiError("cannot delete")
    ctx = rig.ctx()
    file = rig.lib().dir / f"{NAME}.kicad_mod"
    cs = await prepare_delete(ctx, {"lib_id": LIB_ID})
    with pytest.raises(StructuredToolError) as err:
        await _apply(ctx, cs)
    assert err.value.code == "library_error"
    assert file.read_bytes() == FILE_BYTES
    rows = await FootprintWriteRepo(rig.db).after_id(PID, 0)
    assert [(r.op, r.state) for r in rows] == [("delete", "compensated")]
    assert rows[0].post_sha256 == rows[0].pre_sha256


async def test_apply_still_listed_is_leftover(rig: Rig) -> None:
    """KiCad answered, but the file is still there with other bytes: not verifiable ->
    `leftover` and `document_inconsistent` (footprint only)."""
    s = session(rig)
    s.keep_file = True
    file = rig.lib().dir / f"{NAME}.kicad_mod"
    ctx = rig.ctx()
    cs = await prepare_delete(ctx, {"lib_id": LIB_ID})
    real_call = s.call

    async def call(fn: Any) -> Any:
        out = await real_call(fn)
        if getattr(fn, "func", None) is engine_mod._delete_item:
            file.write_bytes(b"other opaque bytes")
        return out

    s.call = call  # type: ignore[method-assign]
    with pytest.raises(DocumentInconsistent) as err:
        await _apply(ctx, cs)
    assert err.value.leftovers == [LIB_ID] and err.value.footprint_only
    rows = await FootprintWriteRepo(rig.db).after_id(PID, 0)
    assert [(r.op, r.state) for r in rows] == [("delete", "leftover")]


async def test_apply_file_gone_before_apply_not_found(rig: Rig) -> None:
    ctx = rig.ctx()
    cs = await prepare_delete(ctx, {"lib_id": LIB_ID})
    (rig.lib().dir / f"{NAME}.kicad_mod").unlink()
    with pytest.raises(StructuredToolError) as err:
        await _apply(ctx, cs)
    assert err.value.code == "not_found"
    assert session(rig).deleted == []
    assert await FootprintWriteRepo(rig.db).after_id(PID, 0) == []


async def test_apply_geometry_changed_since_prepare_refused(rig: Rig) -> None:
    """The footprint changed between prepare and apply (KiCad GUI, another project):
    re-read under the lock, refused before the journal and the KiCad call."""
    ctx = rig.ctx()
    cs = await prepare_delete(ctx, {"lib_id": LIB_ID})
    rig.geometry = _target(body={"rect": [3.2, 3.2]})
    assert geometry_hash(rig.geometry) != cs.summary["geometry_hash"]
    with pytest.raises(StructuredToolError) as err:
        await _apply(ctx, cs)
    assert err.value.code == "verification_failed" and "nothing was written" in err.value.why
    assert session(rig).deleted == []
    assert await FootprintWriteRepo(rig.db).after_id(PID, 0) == []
    assert (rig.lib().dir / f"{NAME}.kicad_mod").read_bytes() == FILE_BYTES


async def test_apply_symlink_swapped_in_refused(rig: Rig, tmp_path: Path) -> None:
    ctx = rig.ctx()
    cs = await prepare_delete(ctx, {"lib_id": LIB_ID})
    file = rig.lib().dir / f"{NAME}.kicad_mod"
    file.rename(tmp_path / "x.kicad_mod")
    file.symlink_to(tmp_path / "x.kicad_mod")
    with pytest.raises(StructuredToolError) as err:
        await _apply(ctx, cs)
    assert err.value.code == "library_conflict"
    assert session(rig).deleted == []


@pytest.mark.parametrize("rows", [{}, {REPO: "${KIPRJMOD}/elsewhere.pretty"}])
async def test_apply_row_changed_refused(rig: Rig, rows: dict[str, str]) -> None:
    """The row is read again under the library lock: gone or another URI ->
    `library_conflict`, nothing deleted, nothing journalled."""
    ctx = rig.ctx()
    cs = await prepare_delete(ctx, {"lib_id": LIB_ID})
    session(rig).rows = rows
    with pytest.raises(StructuredToolError) as err:
        await _apply(ctx, cs)
    assert err.value.code == "library_conflict"
    assert session(rig).deleted == []
    assert (rig.lib().dir / f"{NAME}.kicad_mod").read_bytes() == FILE_BYTES
    assert await FootprintWriteRepo(rig.db).after_id(PID, 0) == []


async def test_apply_row_changed_refused_before_the_cleanup(rig: Rig) -> None:
    """The FP-C8 re-checks run before `cleanup_pending`: with a re-pointed row the
    refusal comes first, so an untouched seed copy of an earlier call the server died
    in is not deleted (the cleanup would delete it) and its row stays pending."""
    import hashlib

    ctx = rig.ctx()
    cs = await prepare_delete(ctx, {"lib_id": LIB_ID})
    copy = rig.lib().dir / "Seed.kicad_mod"
    copy.write_bytes(b"seed copy")
    repo = FootprintWriteRepo(rig.db)
    wid = await repo.insert_pending(
        PID, "run-dead", f"{REPO}:Seed", f"footprints/{REPO}.pretty/Seed.kicad_mod",
        "create", "absent",
    )  # fmt: skip
    await repo.set_post_s2(wid, hashlib.sha256(b"seed copy").hexdigest())
    session(rig).rows = {REPO: "${KIPRJMOD}/elsewhere.pretty"}
    with pytest.raises(StructuredToolError) as err:
        await _apply(ctx, cs)
    assert err.value.code == "library_conflict"
    assert session(rig).deleted == [] and copy.read_bytes() == b"seed copy"
    rows = await repo.after_id(PID, 0)
    assert [(r.op, r.state) for r in rows] == [("create", "pending")]


async def test_apply_library_moved_refused(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """`_recheck_row`'s "library moved" branch: the local library derived under the lock
    differs from the planned one (e.g. the repo root changed) -> `library_conflict`,
    nothing deleted, nothing journalled."""
    ctx = rig.ctx()
    cs = await prepare_delete(ctx, {"lib_id": LIB_ID})
    real = service_mod.LibraryService.local_library

    async def moved(self: Any, **kw: Any) -> Any:
        info = await real(self, **kw)
        lib = dataclasses.replace(info.lib, dir=info.lib.dir.parent / "Moved.pretty")
        return dataclasses.replace(info, lib=lib)

    monkeypatch.setattr(service_mod.LibraryService, "local_library", moved)
    with pytest.raises(StructuredToolError) as err:
        await _apply(ctx, cs)
    assert err.value.code == "library_conflict"
    assert "moved" in err.value.why
    assert session(rig).deleted == []
    assert (rig.lib().dir / f"{NAME}.kicad_mod").read_bytes() == FILE_BYTES
    assert await FootprintWriteRepo(rig.db).after_id(PID, 0) == []


def _listed_after_delete(s: DeleteSession) -> None:
    """KiCad deletes the file but still lists the name: the read-back fails."""
    real_call = s.call

    async def call(fn: Any) -> Any:
        out = await real_call(fn)
        if getattr(fn, "func", None) is engine_mod._delete_item:
            s.names.append(NAME)
        return out

    s.call = call  # type: ignore[method-assign]


async def test_apply_deleted_unconfirmed_worded_by_outcome(rig: Rig) -> None:
    """The file is gone but the read-back failed: the leftover is labelled "deleted, not
    confirmed" (never "remains ... do not use"), and History undo restores it."""
    from copperchat.agent.sdk_tools import _inconsistent_error
    from copperchat.errors import deleted_unconfirmed

    _listed_after_delete(session(rig))
    ctx = rig.ctx()
    cs = await prepare_delete(ctx, {"lib_id": LIB_ID})
    with pytest.raises(DocumentInconsistent) as err:
        await _apply(ctx, cs)
    label = deleted_unconfirmed(LIB_ID)
    assert err.value.leftovers == [label] and err.value.replaced == [label]
    assert not (rig.lib().dir / f"{NAME}.kicad_mod").exists()
    rows = await FootprintWriteRepo(rig.db).after_id(PID, 0)
    assert [(r.op, r.state) for r in rows] == [("delete", "leftover")]
    text = _inconsistent_error("fp_delete", {"lib_id": LIB_ID}, err.value)
    assert (
        f"{LIB_ID} was deleted from the local footprint library, but the deletion "
        "could not be confirmed" in text.why
    )
    assert "remain" not in text.why and "do not use" not in text.why
    assert f"Undoing this turn in History restores {LIB_ID}" in text.suggestion
    assert "existing footprint" not in text.suggestion
    assert text.details is not None and text.details["leftovers"] == [label]


@pytest.mark.parametrize("gone", [False, True])
async def test_real_cancel_during_settle_completes_it(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, gone: bool
) -> None:
    """A real `task.cancel()` while `_settle` is blocked: the settling finishes, the
    CancelledError propagates; a leftover is reported through `on_document_inconsistent`."""
    from kipy.errors import ApiError

    from copperchat.kicad_tools.footprints import delete as delete_mod

    s = session(rig)
    if gone:
        _listed_after_delete(s)
    else:
        s.delete_error = ApiError("cannot delete")
    entered, release = asyncio.Event(), asyncio.Event()
    real_sha = delete_mod._sha_or_absent

    async def blocked_sha(path: Path) -> str:
        entered.set()
        await release.wait()
        return await real_sha(path)

    monkeypatch.setattr(delete_mod, "_sha_or_absent", blocked_sha)
    ctx = rig.ctx()
    seen: list[Exception] = []
    ctx.on_document_inconsistent = seen.append
    cs = await prepare_delete(ctx, {"lib_id": LIB_ID})
    task = asyncio.ensure_future(_apply(ctx, cs))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()  # settling still runs
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = await FootprintWriteRepo(rig.db).after_id(PID, 0)
    assert [(r.op, r.state) for r in rows] == [("delete", "leftover" if gone else "compensated")]
    if gone:
        [failure] = seen
        assert isinstance(failure, DocumentInconsistent)
        assert failure.leftovers == [f"{LIB_ID} (deleted, not confirmed)"]
    else:
        assert seen == []


# -- registration ------------------------------------------------------------------------


def test_registered_and_in_fp_writes() -> None:
    from copperchat.agent.profiles import FP_WRITES, PROFILES

    assert "fp_delete" in {s.name for s in TOOL_SPECS}
    assert "fp_delete" in FP_WRITES
    assert "fp_delete" in PROFILES["schematic"] and "fp_delete" in PROFILES["free"]
    assert "fp_delete" not in PROFILES["board"] | PROFILES["requirements"]
