"""Assigning a local footprint through `sch_set_fields` on a real api-server (F4b Task 3,
spec §7.9, §16.2): `main` creates L201 with `fp_create`; the sibling project `io` assigns
it, which adds io's row for the repo's library on the way (the adapter's `library_row`
pre-stage, journalled `row_add`). Its own row write is never an external change at the
next turn start, and History undo of the turn removes the row again. Every KiCad file
is written by KiCad."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.history.restore import restore_snapshot
from copperchat.kicad_session import KicadSession, KicadSessionManager
from copperchat.projects import discover_projects
from copperchat.tools.spec import ToolContext
from tests.kicad.footprints.test_fp_create import (
    LIB_ID,
    NAME,
    NICK,
    URI,
    journal,
    l201_args,
    lib_dir,
    project_rows,
)
from tests.kicad.footprints.test_turn_end import RUN1, RUN2, Rig, _change_log, _step, open_rig
from tests.support.conversations import TEST_CID
from tests.support.edit import call_tool, edit_ctx
from tests.support.kicad_project import real_settings

pytestmark = pytest.mark.kicad

IO_TABLE = "hardware/io/fp-lib-table"


async def ok(ctx: ToolContext, name: str, /, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, name, **args)
    if err:
        pytest.fail(json.dumps(out, indent=1))
    return out


def kinds(out: dict[str, Any]) -> list[str]:
    return [n["kind"] for n in out["notes"] if isinstance(n, dict)]


async def place_inductor(ctx: ToolContext, session: KicadSession, x: float = 76.2) -> str:
    await session.ensure_symbol_libraries()
    placed = await ok(ctx, "sch_place_symbol", lib_id="Device:L", at=[x, 76.2])
    ref: str = placed["placed"]["ref"]
    return ref


async def main_creates_l201(settings: Settings, sessions: KicadSessionManager, root: Path) -> None:
    """`main` makes L201 (its row, the library directory and the file, all by KiCad)."""
    main_dir = (root / "hardware" / "main").resolve()
    project = next(
        p for p in discover_projects(settings.projects_root) if p.dir.resolve() == main_dir
    )
    session = await sessions.open(project)
    out = await ok(edit_ctx(session), "fp_create", **l201_args())
    assert out["created"] == [LIB_ID]


@pytest.fixture
async def io_rig(make_settings: Callable[..., Settings], tmp_path: Path) -> AsyncIterator[Rig]:
    settings = real_settings(make_settings)
    before = partial(main_creates_l201, settings)
    async with open_rig(settings, tmp_path / "db.sqlite", "io", before=before) as r:
        yield r


async def test_second_project_assigns(repo_session: Any, edit_db: Database) -> None:
    open_session, root = repo_session
    main = await open_session("main")
    await ok(edit_ctx(main, edit_db), "fp_create", **l201_args())
    io = await open_session("io")
    assert NICK not in await project_rows(io)
    ctx = edit_ctx(io, edit_db)
    ref = await place_inductor(ctx, io)

    out = await ok(ctx, "sch_set_fields", ref=ref, fields={"footprint": LIB_ID})
    effects = [e for e in out["side_effects"] if e["kind"] == "fp_lib_table_row_added"]
    assert effects == [
        {"uuid": NICK, "op": "add", "kind": "fp_lib_table_row_added", "before": None,
         "after": {"uri": URI}}
    ]  # fmt: skip
    assert "footprint_review_unknown" in kinds(out)
    assert out["budget"] == {"used": 2, "limit": 20}  # place 1 + set fields 1, row 0
    assert (await project_rows(io))[NICK] == URI
    rows = [r for r in await journal(edit_db, io)]
    assert [(r.op, r.state, r.lib_id, r.file_rel) for r in rows] == [
        ("row_add", "done", NICK, IO_TABLE)
    ]
    io_sch = root / "hardware" / "io" / "blank.kicad_sch"
    assert f'"{LIB_ID}"' in io_sch.read_text()

    # once the row is there, the next assignment takes the API path (no second row)
    ref2 = await place_inductor(ctx, io, 101.6)
    again = await ok(ctx, "sch_set_fields", ref=ref2, fields={"footprint": LIB_ID})
    assert not [e for e in again["side_effects"] if e["kind"] == "fp_lib_table_row_added"]
    assert "footprint_needs_review" in kinds(again)
    assert len(await journal(edit_db, io)) == 1


async def test_same_turn_create_then_assign(repo_session: Any, edit_db: Database) -> None:
    """`fp_create`, then `sch_set_fields` with the new lib id in the same turn: the
    existence check sees it (caches invalidated by the create)."""
    open_session, _ = repo_session
    session = await open_session("main")
    ctx = edit_ctx(session, edit_db)
    ref = await place_inductor(ctx, session)
    await ok(ctx, "fp_create", **l201_args())
    out = await ok(ctx, "sch_set_fields", ref=ref, fields={"footprint": LIB_ID})
    assert "footprint_needs_review" in kinds(out)
    assert not [e for e in out["side_effects"] if e["kind"] == "fp_lib_table_row_added"]
    out, err = await call_tool(
        ctx, "sch_set_fields", ref=ref, fields={"footprint": f"{NICK}:L_Nope"}
    )
    assert err and out["error"]["code"] == "not_found", out


async def test_row_stage_no_external_change(io_rig: Rig) -> None:
    """In io with a running plan step: the row added by sch_set_fields is recorded as
    io's own write - the next turn start reports nothing and the plan keeps running;
    the change log lists the row as a side effect of the edit."""
    rig = io_rig
    project = rig.session.project
    await rig.hooks.on_turn_start(rig.turn(RUN1))
    out, err = await call_tool(
        rig.ctx(RUN1), "plan_propose", title="L201", summary="assign", steps=[_step(2)]
    )
    assert not err, out
    plan_id = out["plan_id"]
    await rig.plans.approve(project, plan_id)
    out, err = await call_tool(rig.ctx(RUN1), "plan_step_begin", step=1)
    assert not err, out
    rig.forget_reports()

    ref = await place_inductor(rig.ctx(RUN1), rig.session)
    out = await ok(rig.ctx(RUN1), "sch_set_fields", ref=ref, fields={"footprint": LIB_ID})
    assert [e["kind"] for e in out["side_effects"]] == ["fp_lib_table_row_added"]
    log = await _change_log(rig, RUN1)
    row = [c for c in log if c["kind"] == "fp_lib_table_row_added"]
    assert len(row) == 1 and row[0]["ref"] == NICK and row[0]["after"] == {"uri": URI}

    await rig.end_turn(RUN1)
    assert rig.reported() == [], rig.reported()
    await rig.hooks.on_turn_start(rig.turn(RUN2))
    assert rig.reported() == [], rig.reported()
    plan = await rig.plans.repo.get(project.id, plan_id)
    assert plan.status == "running"


async def test_history_undo_removes_the_row(io_rig: Rig) -> None:
    """Acceptance: restoring the turn's snapshot removes the row sch_set_fields added
    (journal `row_add`); main's footprint stays."""
    rig = io_rig
    project = rig.session.project
    await rig.hooks.on_turn_start(rig.turn(RUN1))
    ref = await place_inductor(rig.ctx(RUN1), rig.session)
    await ok(rig.ctx(RUN1), "sch_set_fields", ref=ref, fields={"footprint": LIB_ID})
    assert NICK in await project_rows(rig.session)
    await rig.end_turn(RUN1)
    snap = await rig.history.store(project).latest_for_run(RUN1)
    assert snap is not None

    await restore_snapshot(rig.history, project, snap.id, conversation_id=TEST_CID)
    session = rig.history.open_session(project)
    assert session is not None
    assert NICK not in await project_rows(session)
    assert (lib_dir(rig.root) / f"{NAME}.kicad_mod").is_file()
    io_sch = rig.root / "hardware" / "io" / "blank.kicad_sch"
    assert f'"{LIB_ID}"' not in io_sch.read_text()
