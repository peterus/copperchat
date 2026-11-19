"""`fp_delete` on a real api-server (F4b Task 2, spec §7.8, §16.2): refusals (`in_use`,
`not_local`, symlinked library paths) and the delete itself (`DeleteLibraryItem`, the
read-back, the journal, the same-turn cache invalidation, the sibling-project note).
Every footprint is made by `fp_create` (KiCad writes the file); symlinks are made with
`os.symlink` (no KiCad file is written by hand)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadSession
from copperchat.library.service import library_for
from copperchat.tools.spec import ToolContext
from tests.kicad.footprints.test_fp_create import (
    LIB_ID,
    NAME,
    NICK,
    add_row,
    journal,
    l201_args,
    lib_dir,
    sha,
    tree,
)
from tests.support.edit import call_tool, edit_ctx

pytestmark = pytest.mark.kicad


async def ok(ctx: ToolContext, name: str, /, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, name, **args)
    if err:
        pytest.fail(json.dumps(out, indent=1))
    return out


async def refused(ctx: ToolContext, name: str, /, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, name, **args)
    assert err, out
    error: dict[str, Any] = out["error"]
    return error


async def created(session: KicadSession, db: Database) -> ToolContext:
    """L201 made by fp_create in `session`; returns the context of the turn."""
    ctx = edit_ctx(session, db)
    out = await ok(ctx, "fp_create", **l201_args())
    assert out["created"] == [LIB_ID]
    return ctx


async def assign(ctx: ToolContext, session: KicadSession) -> str:
    """A placed Device:L whose Footprint field names L201 (through the edit tools);
    returns its reference."""
    await session.ensure_symbol_libraries()
    placed = await ok(ctx, "sch_place_symbol", lib_id="Device:L", at=[76.2, 76.2])
    ref: str = placed["placed"]["ref"]
    await ok(ctx, "sch_set_fields", ref=ref, fields={"footprint": LIB_ID})
    return ref


async def test_delete_in_use(repo_session: Any, edit_db: Database) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    ctx = await created(session, edit_db)
    ref = await assign(ctx, session)
    assert ref == "L1"
    file = lib_dir(root) / f"{NAME}.kicad_mod"
    before, rows = sha(file), await journal(edit_db, session)
    error = await refused(ctx, "fp_delete", lib_id=LIB_ID)
    assert error["code"] == "in_use", error
    assert error["details"]["refs"] == ["L1"]
    assert sha(file) == before and await journal(edit_db, session) == rows


async def test_delete_stock_not_local(repo_session: Any, edit_db: Database) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    before = tree(root)
    error = await refused(
        edit_ctx(session, edit_db), "fp_delete", lib_id="Inductor_SMD:L_Bourns-SRN4018"
    )
    assert error["code"] == "not_local", error
    assert tree(root) == before


async def test_delete_ok(repo_session: Any, edit_db: Database) -> None:
    """File gone, KiCad no longer lists it, journal `delete` done with `absent`, and the
    caches are invalidated: `fp_get` in the same turn says `not_found`."""
    open_session, root = repo_session
    session = await open_session("main")
    ctx = await created(session, edit_db)
    file = lib_dir(root) / f"{NAME}.kicad_mod"
    pre = sha(file)
    got = await ok(ctx, "fp_get", lib_id=LIB_ID)  # warms the geometry cache
    digest = got["geometry_hash"]

    out = await ok(ctx, "fp_delete", lib_id=LIB_ID)
    assert out["deleted"] == LIB_ID
    assert out["file"] == f"footprints/{NICK}.pretty/{NAME}.kicad_mod"
    assert out["logical_changes"] == 1
    assert out["budget"] == {"used": 6, "limit": 20}  # fp_create 5 + fp_delete 1
    assert out["notes"] == [], out["notes"]
    assert not file.exists()
    assert NAME not in await library_for(session).footprint_names(NICK)
    rows = await journal(edit_db, session)
    assert [(r.op, r.state) for r in rows] == [
        ("row_add", "done"),
        ("create", "done"),
        ("delete", "done"),
    ]
    delete = rows[-1]
    assert (delete.lib_id, delete.file_rel) == (LIB_ID, out["file"])
    assert delete.pre_sha256 == pre and delete.post_sha256 == "absent"
    assert delete.geometry_hash == digest
    assert file in ctx.library_touched

    error = await refused(ctx, "fp_get", lib_id=LIB_ID)
    assert error["code"] == "not_found", error
    error = await refused(ctx, "fp_delete", lib_id=LIB_ID)
    assert error["code"] == "not_found", error
    listed = await ok(ctx, "fp_list_local")
    assert listed["footprints"] == []
    state = ctx.editing.turn(session.project.id, ctx.run_id) if ctx.editing else None
    assert state is not None and state.deleted_preexisting == 0


async def test_delete_sibling_use_noted(repo_session: Any, edit_db: Database) -> None:
    """`io` assigns L201 to its own L1 (KiCad saves its schematic); deleting it in `main`
    succeeds with a `possibly_in_use_elsewhere` note naming that file."""
    open_session, root = repo_session
    main = await open_session("main")
    await created(main, edit_db)
    io = await open_session("io")
    await add_row(io, create=False)
    await assign(edit_ctx(io, edit_db), io)
    io_sch = root / "hardware" / "io" / "blank.kicad_sch"
    assert f'"{LIB_ID}"' in io_sch.read_text()
    main = await open_session("main")
    out = await ok(edit_ctx(main, edit_db), "fp_delete", lib_id=LIB_ID)
    [note] = [n for n in out["notes"] if n["kind"] == "possibly_in_use_elsewhere"]
    assert note["files"] == ["hardware/io/blank.kicad_sch"] and note["complete"] is True
    assert str(root.resolve()) not in json.dumps(out)
    assert not (lib_dir(root) / f"{NAME}.kicad_mod").exists()


async def test_delete_symlinked_dir_refused(repo_session: Any, edit_db: Database) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    ctx = await created(session, edit_db)
    footprints = root / "footprints"
    real = root / "footprints-real"
    footprints.rename(real)
    footprints.symlink_to(real, target_is_directory=True)
    before, rows = tree(root), await journal(edit_db, session)
    error = await refused(ctx, "fp_delete", lib_id=LIB_ID)
    assert error["code"] == "library_conflict", error
    assert tree(root) == before and await journal(edit_db, session) == rows
    assert (real / f"{NICK}.pretty" / f"{NAME}.kicad_mod").is_file()


async def test_delete_symlinked_file_refused(repo_session: Any, edit_db: Database) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    ctx = await created(session, edit_db)
    file = lib_dir(root) / f"{NAME}.kicad_mod"
    elsewhere = Path(root) / "elsewhere.kicad_mod"
    file.rename(elsewhere)
    file.symlink_to(elsewhere)
    before, rows = tree(root), await journal(edit_db, session)
    error = await refused(ctx, "fp_delete", lib_id=LIB_ID)
    assert error["code"] == "library_conflict", error
    assert file.is_symlink() and tree(root) == before
    assert await journal(edit_db, session) == rows
