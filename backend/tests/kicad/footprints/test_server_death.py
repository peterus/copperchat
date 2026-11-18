"""The api-server dies between S2 and S3 of an `fp_create` (F4a Task 4, spec §9.3 server
death): no compensation, the journal row stays `pending` with the seed copy's hash; the
read tools only flag it (and write nothing); the next writing footprint call cleans the
untouched copy up first, so a retry with the same name succeeds. The server is killed
with SIGKILL (pattern of `tests/kicad/test_server_kill.py`, through the engine's
`kill_after` hook); every library file is written by KiCad."""

from __future__ import annotations

import json
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadUnavailable
from copperchat.kicad_tools.footprints.read import fp_check, fp_get, fp_list_local
from copperchat.tools.spec import ToolContext
from tests.kicad.footprints.test_fp_create import (
    LIB_ID,
    NAME,
    NICK,
    journal,
    l201_args,
    lib_dir,
    run_create,
    sha,
    tree,
)
from tests.support.edit import edit_ctx

pytestmark = pytest.mark.kicad

OTHER = "L_Other_3030"


async def test_server_death_after_copy_seed_then_cleanup(
    repo_session: Any, edit_db: Database
) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    unavailable: list[KicadUnavailable] = []

    def ctx() -> ToolContext:
        return edit_ctx(session, edit_db, on_kicad_unavailable=unavailable.append)

    # -- the call dies right after S2 saved the seed copy ---------------------------------
    pid = session.process.pid if session.process is not None else None
    out, err = await run_create(ctx(), l201_args(), kill_after="copy_seed")
    assert err, out
    assert len(unavailable) == 1 and isinstance(unavailable[0], KicadUnavailable)
    assert pid is not None and session.process is not None
    assert session.process.returncode is not None  # SIGKILLed, nothing restarted it yet
    copy = lib_dir(root) / f"{NAME}.kicad_mod"
    assert copy.is_file()
    rows = await journal(edit_db, session)
    assert [(r.op, r.state) for r in rows] == [("row_add", "done"), ("create", "pending")]
    assert rows[1].lib_id == LIB_ID and rows[1].post_s2_sha256 == sha(copy)

    # -- next turn: a fresh server; the read tools flag it and write nothing --------------
    await session.restart()
    assert session.process is not None and session.process.pid != pid
    before, rows_before = tree(root), await journal(edit_db, session)

    got = await fp_get(ctx(), {"lib_id": LIB_ID})
    assert got.data is not None and got.data["review"] == "unreviewed"
    listed = await fp_list_local(ctx(), {})
    assert listed.data is not None
    by_name = {f["name"]: f for f in listed.data["footprints"]}
    assert by_name[NAME]["needs_user_review"] == "unreviewed"
    checked = await fp_check(ctx(), {"lib_id": LIB_ID})
    assert checked.data is not None
    review = [f for f in checked.data["findings"] if f["rule"] == "needs_user_review"]
    assert len(review) == 1 and review[0]["severity"] == "info", checked.data["findings"]
    assert "unreviewed" in review[0]["message"]

    assert tree(root) == before
    assert await journal(edit_db, session) == rows_before

    # -- a write of another name cleans the untouched copy up first -----------------------
    out, err = await run_create(ctx(), l201_args(name=OTHER))
    if err:
        pytest.fail(json.dumps(out, indent=1))
    cleaned = [n for n in out["notes"] if n["kind"] == "pending_cleaned"]
    assert [n["lib_id"] for n in cleaned] == [LIB_ID], out["notes"]
    assert not [n for n in out["notes"] if n["kind"] == "pending_leftover"]
    assert not copy.exists()
    assert (lib_dir(root) / f"{OTHER}.kicad_mod").is_file()
    rows = await journal(edit_db, session)
    assert [(r.lib_id, r.op, r.state) for r in rows] == [
        (NICK, "row_add", "done"),
        (LIB_ID, "create", "compensated"),
        (f"{NICK}:{OTHER}", "create", "done"),
    ]
    assert rows[1].post_sha256 == "absent"

    # -- a retry with the first name succeeds ---------------------------------------------
    out, err = await run_create(ctx(), l201_args())
    if err:
        pytest.fail(json.dumps(out, indent=1))
    assert out["created"] == [LIB_ID]
    assert not [n for n in out["notes"] if n["kind"].startswith("pending_")]
    assert copy.is_file()
    rows = await journal(edit_db, session)
    assert [(r.lib_id, r.state) for r in rows][-1] == (LIB_ID, "done")
    assert not [r for r in rows if r.state == "pending"]
    got = await fp_get(ctx(), {"lib_id": LIB_ID})
    assert got.data is not None and got.data["review"] == "needs_user_review"
