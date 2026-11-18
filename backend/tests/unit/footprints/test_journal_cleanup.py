"""Pending cleanup after a server death (F4a Task 4, spec §9.3, FP-C8): the pure
classification of `pending` journal rows, the cleanup at the start of every writing
footprint `apply`, and `prepare` validating against the state after the cleanup (a
cleanable copy is free for `fp_create`, `not_found` for `replace`)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.db.repos.footprints import ABSENT, FootprintWrite, FootprintWriteRepo
from copperchat.editing.service import EditingService
from copperchat.errors import StructuredToolError
from copperchat.history.library_lock import library_lock
from copperchat.kicad_session import KicadSession, KicadUnavailable
from copperchat.kicad_tools.footprints import engine as fp_engine
from copperchat.kicad_tools.footprints.create import prepare_create
from copperchat.kicad_tools.footprints.engine import (
    FpStage,
    PendingOutcome,
    classify_pending,
    cleanup_pending,
)
from copperchat.kicad_tools.footprints.journal import footprint_rel, table_rel
from copperchat.tools import ToolContext
from tests.unit.footprints.test_build import l201_args
from tests.unit.footprints.test_create_prepare import NAME, CreateSession, _make, _pretty
from tests.unit.footprints.test_engine_unit import PID, _engine_setup
from tests.unit.library.test_service import LOCAL_URI, REPO

SHA_A = "a" * 64
SHA_B = "b" * 64
FILE_REL = f"footprints/{REPO}.pretty/X.kicad_mod"


def _write(
    op: str,
    post_s2: str | None,
    *,
    lib_id: str = f"{REPO}:X",
    file_rel: str = FILE_REL,
    run_id: str = "run1",
    row_id: int = 1,
) -> FootprintWrite:
    return FootprintWrite(
        id=row_id,
        project_id=PID,
        run_id=run_id,
        lib_id=lib_id,
        file_rel=file_rel,
        op=op,
        state="pending",
        pre_sha256=ABSENT,
        post_s2_sha256=post_s2,
        post_sha256=None,
        geometry_hash=None,
        created_at="2026-11-18T00:00:00Z",
        finalised_at=None,
    )


# -- the classification table (pure) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "post_s2", "current", "action"),
    [
        ("create", SHA_A, SHA_A, "delete"),  # an untouched seed copy
        ("create", SHA_A, SHA_B, "leftover"),  # changed since S2 (S3 may have saved)
        ("create", None, SHA_A, "leftover"),  # missing hash
        ("create", SHA_A, ABSENT, "leftover"),  # the file is gone
        ("replace", SHA_A, SHA_A, "leftover"),  # a replace target is never destroyed
        ("approve", SHA_A, SHA_A, "leftover"),
        ("delete", SHA_A, SHA_A, "leftover"),
        ("row_add", None, SHA_A, "leftover"),  # rows are never removed by the cleanup
    ],
)
def test_classification_table(op: str, post_s2: str | None, current: str, action: str) -> None:
    row = _write(op, post_s2)
    seen: list[str] = []

    def current_sha(file_rel: str) -> str:
        seen.append(file_rel)
        return current

    out = classify_pending([row], current_sha)
    assert out == [PendingOutcome(row=row, action=action, current_sha256=current)]
    assert seen == [FILE_REL]


def test_classification_keeps_row_order() -> None:
    rows = [_write("create", SHA_A, row_id=1), _write("replace", SHA_A, row_id=2)]
    out = classify_pending(rows, lambda rel: SHA_A)
    assert [(o.row.id, o.action) for o in out] == [(1, "delete"), (2, "leftover")]
    assert classify_pending([], lambda rel: SHA_A) == []


def test_generation_change_irrelevant() -> None:
    """A row journalled under one api-server generation (1) and classified under
    another (3): only the hashes count, nothing about the run or server."""
    under_gen1 = _write("create", SHA_A, run_id="run-gen1")
    under_gen3 = _write("create", SHA_A, run_id="run-gen3")
    first = classify_pending([under_gen1], lambda rel: SHA_A)
    later = classify_pending([under_gen3], lambda rel: SHA_A)
    assert [o.action for o in first] == [o.action for o in later] == ["delete"]
    assert [o.action for o in classify_pending([under_gen1], lambda rel: SHA_B)] == ["leftover"]


# -- prepare (FP-C8 index: validated against the state after the cleanup) ----------------


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "db.sqlite")
    yield d
    await d.close()


@pytest.fixture
def prep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CreateSession:
    s = _make(tmp_path, monkeypatch)
    s.project.id = PID  # type: ignore[attr-defined]
    return s


def _prep_ctx(session: CreateSession, db: Database) -> ToolContext:
    return ToolContext(
        project=cast(Any, session.project),
        session=cast(KicadSession, session),
        settings=cast(Any, session.settings),
        editing=cast(Any, SimpleNamespace(db=db)),
    )


async def _seed_copy(session: CreateSession, db: Database, *, post_s2: str | None = None) -> Path:
    """`<NAME>.kicad_mod` on disk (KiCad lists it) with a `pending` create row whose
    `post_s2_sha256` is the file's hash (or `post_s2`)."""
    session.rows = {REPO: LOCAL_URI}
    session.names = [NAME]
    _pretty(session).mkdir(parents=True)
    file = _pretty(session) / f"{NAME}.kicad_mod"
    file.write_bytes(b"(footprint seed copy)")
    repo = FootprintWriteRepo(db)
    rel = f"footprints/{REPO}.pretty/{NAME}.kicad_mod"
    row = await repo.insert_pending(PID, "run-dead", f"{REPO}:{NAME}", rel, "create", ABSENT)
    await repo.set_post_s2(row, post_s2 or hashlib.sha256(file.read_bytes()).hexdigest())
    return file


async def _rows(db: Database) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in await db.fetchall("SELECT * FROM footprint_writes ORDER BY id")]


async def test_prepare_counts_cleanable_copy_as_free(prep: CreateSession, db: Database) -> None:
    file = await _seed_copy(prep, db)
    before, data = await _rows(db), file.read_bytes()
    cs = await prepare_create(_prep_ctx(prep, db), l201_args())
    assert cs.target is not None and cs.target.lib_id == f"{REPO}:{NAME}"
    # read-only: the copy and the journal are untouched (the cleanup runs in apply)
    assert await _rows(db) == before and file.read_bytes() == data


async def test_prepare_changed_copy_stays_taken(prep: CreateSession, db: Database) -> None:
    await _seed_copy(prep, db, post_s2=SHA_B)  # the file changed since S2
    with pytest.raises(StructuredToolError) as err:
        await prepare_create(_prep_ctx(prep, db), l201_args())
    assert err.value.code == "already_exists"


async def test_prepare_cleanable_copy_is_not_found_for_replace(
    prep: CreateSession, db: Database
) -> None:
    await _seed_copy(prep, db)
    before = await _rows(db)
    with pytest.raises(StructuredToolError) as err:
        await prepare_create(_prep_ctx(prep, db), l201_args(replace=True))
    assert err.value.code == "not_found"
    assert f"{REPO}:{NAME}" in err.value.what
    assert prep.documents == []  # no pre-image read
    assert await _rows(db) == before


async def test_prepare_other_projects_row_does_not_count(prep: CreateSession, db: Database) -> None:
    """Only this project's pending rows are classified (spec §9.3)."""
    await _seed_copy(prep, db)
    await db.execute("UPDATE footprint_writes SET project_id = 'other'")
    with pytest.raises(StructuredToolError) as err:
        await prepare_create(_prep_ctx(prep, db), l201_args())
    assert err.value.code == "already_exists"


# -- cleanup_pending (engine fake: KiCad deletes the file) ----------------------------------


@pytest.fixture
async def editing(tmp_path: Path) -> AsyncIterator[EditingService]:
    d = await Database.open(tmp_path / "db.sqlite")
    yield EditingService(db=d, broadcaster=None, settings_repo=EditingSettingsRepo(d))
    await d.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def _pending_copy(
    editing: EditingService, lib: Any, name: str, *, op: str = "create", touch: bool = False
) -> Path:
    """A KiCad-library footprint file `name` and its `pending` journal row (with the
    file's hash as `post_s2_sha256`; `touch` changes the file afterwards)."""
    lib.dir.mkdir(parents=True, exist_ok=True)
    file = lib.dir / f"{name}.kicad_mod"
    file.write_text(f"(footprint seed copy {name})")
    repo = FootprintWriteRepo(editing.db)
    row = await repo.insert_pending(
        PID, "run-dead", f"{lib.nickname}:{name}", footprint_rel(lib, name), op, ABSENT
    )
    await repo.set_post_s2(row, _sha(file))
    if touch:
        file.write_text(f"(footprint edited {name})")
    return file


async def _journal(editing: EditingService) -> list[FootprintWrite]:
    return await FootprintWriteRepo(editing.db).after_id(PID, 0)


async def test_cleanup_deletes_untouched_copies_and_keeps_the_rest(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    lib = cs.library
    assert lib is not None
    session.kicad.rows = {lib.nickname: lib.uri}
    clean = await _pending_copy(editing, lib, "A")
    changed = await _pending_copy(editing, lib, "B", touch=True)
    replaced = await _pending_copy(editing, lib, "C", op="replace")
    repo = FootprintWriteRepo(editing.db)
    await repo.insert_pending(PID, "run-dead", lib.nickname, table_rel(lib), "row_add", ABSENT)

    async with library_lock(lib.dir):
        notes = await cleanup_pending(ctx, lib)

    assert session.kicad.calls == ["delete_item"]
    assert not clean.exists() and changed.exists() and replaced.exists()
    rows = await _journal(editing)
    assert [(r.lib_id, r.state) for r in rows] == [
        (f"{lib.nickname}:A", "compensated"),
        (f"{lib.nickname}:B", "leftover"),
        (f"{lib.nickname}:C", "leftover"),
        (lib.nickname, "leftover"),
    ]
    assert rows[0].post_sha256 == ABSENT
    assert rows[1].post_sha256 == _sha(changed) and rows[2].post_sha256 == _sha(replaced)
    assert rows[3].post_sha256 == ABSENT  # no fp-lib-table in this fake
    assert [(n["kind"], n["lib_id"]) for n in notes] == [
        ("pending_cleaned", f"{lib.nickname}:A"),
        ("pending_leftover", f"{lib.nickname}:B"),
        ("pending_leftover", f"{lib.nickname}:C"),
        ("pending_leftover", f"row {lib.nickname}"),
    ]
    assert all(n["message"] for n in notes)
    assert ctx.library_touched == [clean]  # only what the cleanup wrote
    # a second cleanup has nothing left to do
    async with library_lock(lib.dir):
        assert await cleanup_pending(ctx, lib) == []


async def test_cleanup_needs_the_library_lock(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    settings = make_settings()
    ctx, _session, cs = _engine_setup(settings.projects_root, settings, editing)
    assert cs.library is not None
    with pytest.raises(AssertionError, match="library lock"):
        await cleanup_pending(ctx, cs.library)


async def test_cleanup_without_journal_is_a_no_op(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    assert cs.library is not None
    ctx.editing = None
    async with library_lock(cs.library.dir):
        assert await cleanup_pending(ctx, cs.library) == []
    assert session.kicad.calls == []


async def test_cleanup_leaves_a_file_another_project_wrote_later(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    """Another project of the repo journalled a write to the same file afterwards: the
    cross-project guard keeps it (leftover), even with a matching hash."""
    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    lib = cs.library
    assert lib is not None
    session.kicad.rows = {lib.nickname: lib.uri}
    file = await _pending_copy(editing, lib, "A")
    other = await FootprintWriteRepo(editing.db).insert_pending(
        "other", "run-x", f"{lib.nickname}:A", footprint_rel(lib, "A"), "create", _sha(file)
    )
    await FootprintWriteRepo(editing.db).finalise(other, "done", _sha(file))
    async with library_lock(lib.dir):
        notes = await cleanup_pending(ctx, lib)
    assert file.exists() and session.kicad.calls == []
    assert [n["kind"] for n in notes] == ["pending_leftover"]
    assert (await _journal(editing))[0].state == "leftover"


async def test_cleanup_delete_failure_is_a_leftover(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    lib = cs.library
    assert lib is not None
    file = await _pending_copy(editing, lib, "A")

    async def refuse() -> None:
        raise RuntimeError("KiCad refused DeleteLibraryItem")

    session.before_delete = refuse
    async with library_lock(lib.dir):
        notes = await cleanup_pending(ctx, lib)
    assert file.exists()
    assert [n["kind"] for n in notes] == ["pending_leftover"]
    row = (await _journal(editing))[0]
    assert (row.state, row.post_sha256) == ("leftover", _sha(file))


async def test_cleanup_server_death_keeps_rows_pending(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    lib = cs.library
    assert lib is not None
    file = await _pending_copy(editing, lib, "A")

    async def die() -> None:
        raise KicadUnavailable("the api-server died")

    session.before_delete = die
    async with library_lock(lib.dir):
        with pytest.raises(KicadUnavailable):
            await cleanup_pending(ctx, lib)
    assert file.exists()
    assert (await _journal(editing))[0].state == "pending"


async def test_cleanup_generation_change_irrelevant(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    """The copy was journalled while the api-server was at generation 1; the cleanup runs
    at generation 3 and still deletes it (only the hashes count)."""
    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    lib = cs.library
    assert lib is not None
    file = await _pending_copy(editing, lib, "A")
    session.generation = 3
    async with library_lock(lib.dir):
        notes = await cleanup_pending(ctx, lib)
    assert not file.exists() and [n["kind"] for n in notes] == ["pending_cleaned"]


async def test_apply_runs_the_cleanup_first(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    """`fp_create` of the same name after a server death: the cleanup removes the seed
    copy first, so the API re-check passes and the new call's own stages run (here
    S3 fails in the fake and is compensated)."""
    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    lib = cs.library
    assert lib is not None and cs.target is not None
    name = cs.target.lib_id.split(":", 1)[1]
    session.kicad.rows = {lib.nickname: lib.uri}
    cs.fp_stages = [FpStage("copy_seed", cs.target.lib_id), FpStage("rebuild", cs.target.lib_id)]
    await _pending_copy(editing, lib, name)
    async with library_lock(lib.dir):
        with pytest.raises(StructuredToolError) as err:
            await fp_engine.apply_footprint_change_set(ctx, cs)
    assert err.value.code == "library_error"  # S3 of the fake, not already_exists
    assert session.kicad.calls == ["delete_item", "delete_item"]  # cleanup, compensation
    rows = await _journal(editing)
    assert [(r.op, r.state) for r in rows] == [
        ("create", "compensated"),
        ("create", "compensated"),
    ]


# -- review fixes (Task 4, round 1) ---------------------------------------------------------


async def _create_through_adapter(ctx: ToolContext, cs: Any) -> dict[str, Any]:
    """`fp_create` with the planned `cs` through the real adapter (library lock, error
    payload); returns the error payload."""
    import json

    from copperchat.agent.sdk_tools import build_sdk_tool_list
    from copperchat.tools import edit_spec

    async def prepare(_c: ToolContext, _a: dict[str, Any]) -> Any:
        return cs

    async def apply(c: ToolContext, planned: Any) -> Any:
        await fp_engine.apply_footprint_change_set(c, planned)
        raise AssertionError("the fake S3 always fails")

    spec = edit_spec("fp_create", "d", {"type": "object"}, prepare, apply)
    raw = await build_sdk_tool_list([spec], lambda: ctx)[0].handler({})
    assert raw.get("is_error")
    payload: dict[str, Any] = json.loads(raw["content"][0]["text"])
    return payload["error"]


@pytest.mark.parametrize("touch", [False, True])
async def test_failing_stage_still_reports_the_cleanup(
    make_settings: Callable[..., Settings], editing: EditingService, touch: bool
) -> None:
    """The tool's own stages fail after the cleanup (S3 of the fake): the deleted copy
    (`pending_cleaned`) or the kept one (`pending_leftover`) is named in the error's
    `details.notes` - the rows are no longer pending, so this is the only notice."""
    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    lib = cs.library
    assert lib is not None and cs.target is not None
    session.kicad.rows = {lib.nickname: lib.uri}
    cs.fp_stages = [FpStage("copy_seed", cs.target.lib_id), FpStage("rebuild", cs.target.lib_id)]
    await _pending_copy(editing, lib, "A", touch=touch)
    error = await _create_through_adapter(ctx, cs)
    assert error["code"] == "library_error"
    kind = "pending_leftover" if touch else "pending_cleaned"
    notes = error["details"]["notes"]
    assert [(n["kind"], n["lib_id"]) for n in notes] == [(kind, f"{lib.nickname}:A")]
    states = [r.state for r in await _journal(editing)]
    assert states[0] == ("leftover" if touch else "compensated")


async def test_server_death_mid_cleanup_reports_finished_rows(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    """KicadUnavailable while deleting the second copy: the first row (finalised) is
    still named in the error; the second stays `pending`."""
    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    lib = cs.library
    assert lib is not None
    session.kicad.rows = {lib.nickname: lib.uri}
    await _pending_copy(editing, lib, "A", op="replace")  # leftover, finalised first
    second = await _pending_copy(editing, lib, "B")

    async def die() -> None:
        raise KicadUnavailable("the api-server died")

    session.before_delete = die
    error = await _create_through_adapter(ctx, cs)
    assert "KiCad stopped responding" in error["what"]
    assert [(n["kind"], n["lib_id"]) for n in error["details"]["notes"]] == [
        ("pending_leftover", f"{lib.nickname}:A")
    ]
    assert [r.state for r in await _journal(editing)] == ["leftover", "pending"]
    assert second.exists()


async def test_stop_during_a_cleanup_delete_still_finalises(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    """A Stop between DeleteLibraryItem and the journal update: the row still becomes
    `compensated` (the row runs to completion), then the cancellation wins, carrying
    the note."""
    import asyncio

    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    lib = cs.library
    assert lib is not None
    file = await _pending_copy(editing, lib, "A")
    deleting = asyncio.Event()
    go_on = asyncio.Event()

    async def slow() -> None:
        deleting.set()
        await go_on.wait()

    session.before_delete = slow

    async def run() -> list[dict[str, Any]]:
        async with library_lock(lib.dir):
            return await cleanup_pending(ctx, lib)

    task = asyncio.ensure_future(run())
    await deleting.wait()
    task.cancel()
    await asyncio.sleep(0)
    go_on.set()
    with pytest.raises(asyncio.CancelledError) as err:
        await task
    assert not file.exists()
    assert [r.state for r in await _journal(editing)] == ["compensated"]
    from copperchat.errors import tool_notes

    assert [n["kind"] for n in tool_notes(err.value)] == ["pending_cleaned"]


@pytest.mark.parametrize(
    ("op", "lib_id", "expected"),
    [
        ("create", f"{REPO}:X", "changed since then or cannot be matched"),
        ("replace", f"{REPO}:X", "It is not a create, so it is kept"),
        ("approve", f"{REPO}:X", "It is not a create, so it is kept"),
        ("delete", f"{REPO}:X", "It is not a create, so it is kept"),
        ("row_add", REPO, "never removes library rows"),
    ],
)
def test_leftover_note_wording(op: str, lib_id: str, expected: str) -> None:
    note = fp_engine._leftover_note(_write(op, SHA_A, lib_id=lib_id))
    assert note["kind"] == "pending_leftover" and note["op"] == op
    assert expected in note["message"]
    assert note["lib_id"] == (f"row {REPO}" if op == "row_add" else lib_id)
    if op != "row_add":
        assert lib_id in note["message"] and "fp_delete" in note["message"]
