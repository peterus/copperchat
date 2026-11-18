"""`fp_create` compensation on a real api-server (F4a Task 3, spec §9.3): a failure after
S1 / S2 / S3 or in the read-back undoes every earlier stage (the call's own row and the
directory it created; a directory that existed before stays), and a failing
compensation leaves named leftovers (`document_inconsistent`)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.agent.engine import TurnOutcome
from copperchat.db import Database
from copperchat.errors import DocumentInconsistent
from copperchat.history.hooks import HistoryHooks
from tests.kicad.footprints.test_fp_create import (
    LIB_ID,
    NAME,
    NICK,
    journal,
    l201_args,
    lib_dir,
    library_via_io,
    project_rows,
    run_create,
)
from tests.support.edit import edit_ctx

pytestmark = pytest.mark.kicad


@pytest.mark.parametrize("dir_existed", [False, True], ids=["new-dir", "dir-existed"])
@pytest.mark.parametrize("stage", ["ensure_library", "copy_seed", "rebuild_saved"])
async def test_compensation_after_stage(
    repo_session: Any, edit_db: Database, stage: str, dir_existed: bool
) -> None:
    """Covers test_compensation_after_copy_seed / _after_rebuild_saved (and after S1):
    the footprint is gone, the row this call added is gone, an empty directory this
    call created is gone (with its parent `footprints/` it created), one that existed
    before is kept; the error is the injected one (never a success)."""
    open_session, root = repo_session
    if dir_existed:
        await library_via_io(open_session)  # the sibling project made the (empty) dir
    session = await open_session("main")
    ctx = edit_ctx(session, edit_db)
    out, err = await run_create(ctx, l201_args(), fail_after=stage)
    if not (err and out["error"]["code"] == "injected"):
        pytest.fail(repr(out))
    assert not (lib_dir(root) / f"{NAME}.kicad_mod").exists()
    assert NICK not in await project_rows(session)
    assert lib_dir(root).is_dir() is dir_existed
    # `footprints/` was created along with the library dir (new-dir): gone too; one
    # that existed before (dir-existed) is kept
    assert lib_dir(root).parent.is_dir() is dir_existed
    assert root.resolve().is_dir()
    rows = await journal(edit_db, session)
    expected = [("row_add", "compensated")]
    if stage != "ensure_library":
        expected.append(("create", "compensated"))
    assert [(r.op, r.state) for r in rows] == expected
    assert all(r.post_sha256 == "absent" for r in rows if r.op == "create")
    assert session.project.dir.resolve() / "fp-lib-table" in ctx.library_touched


async def test_verification_failure_compensates(repo_session: Any, edit_db: Database) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    out, err = await run_create(edit_ctx(session, edit_db), l201_args(), fail_after="verify")
    assert err and out["error"]["code"] == "verification_failed", out
    assert "pad 1" in out["error"]["why"]
    assert not lib_dir(root).exists()
    assert NICK not in await project_rows(session)
    rows = await journal(edit_db, session)
    assert [(r.op, r.state) for r in rows] == [
        ("row_add", "compensated"),
        ("create", "compensated"),
    ]


async def test_compensation_failure_reports_leftover(repo_session: Any, edit_db: Database) -> None:
    """`DeleteLibraryItem` fails while compensating S3: `document_inconsistent` naming the
    lib id and the row, both stay on disk, the journal rows are `leftover`; the turn-end
    notice names them too."""
    open_session, root = repo_session
    session = await open_session("main")
    seen: list[Exception] = []
    ctx = edit_ctx(session, edit_db, on_document_inconsistent=seen.append)
    out, err = await run_create(
        ctx, l201_args(), fail_after="rebuild_saved", fail_compensation=True
    )
    assert err and out["error"]["code"] == "document_inconsistent", out
    text = out["error"]["why"] + out["error"]["suggestion"]
    assert LIB_ID in text and f"row {NICK}" in text
    assert out["error"]["details"]["leftovers"] == [LIB_ID, f"row {NICK}"]
    assert (lib_dir(root) / f"{NAME}.kicad_mod").is_file()
    assert NICK in await project_rows(session)
    rows = await journal(edit_db, session)
    assert [(r.op, r.state) for r in rows] == [("row_add", "leftover"), ("create", "leftover")]

    (exc,) = seen
    assert isinstance(exc, DocumentInconsistent) and exc.footprint_only
    notices: list[tuple[str, dict[str, Any]]] = []

    async def post_notice(_cid: str, text: str, meta: dict[str, Any]) -> None:
        notices.append((text, meta))

    hooks = cast(Any, SimpleNamespace(engine=SimpleNamespace(post_notice=post_notice)))
    hooks._leftovers_notice = lambda o, c: HistoryHooks._leftovers_notice(hooks, o, c)
    outcome = TurnOutcome(
        reason="error",
        mutated=True,
        document_inconsistent=True,
        leftovers=tuple(exc.leftovers),
        footprint_only=True,
    )
    await HistoryHooks._recovery_notice(hooks, outcome, None, None, "c1")
    assert any(LIB_ID in t and f"row {NICK}" in t for t, _ in notices), notices
