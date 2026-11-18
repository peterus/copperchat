"""`fp_list_local` and "read paths write nothing" on a real api-server (F1 Task 6, spec
§7.3). The local library row and its footprints are made only through the KiCad API
(project row with `create_library=True`, `SaveCopyOfDocument` of stock footprints)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from kipy.proto.common.types.library_types_pb2 import LibraryTableScope, LibraryType

from copperchat.db import Database
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_session.footprint_doc import SEED_LIB_ID
from copperchat.kicad_tools.footprints.read import fp_get, fp_list_local
from copperchat.library.local import LocalLibrary
from copperchat.library.service import library_for
from copperchat.tools.spec import ToolContext
from tests.support.edit import call_tool, edit_ctx

pytestmark = pytest.mark.kicad

RESISTOR = "Resistor_SMD:R_0805_2012Metric"


def ctx(session: KicadSession) -> ToolContext:
    return ToolContext(project=session.project, session=session, settings=session.settings)


async def _local(session: KicadSession) -> LocalLibrary:
    info = await library_for(session).local_library()
    assert info is not None
    return info.lib


async def _make_library(session: KicadSession, copies: dict[str, str]) -> LocalLibrary:
    """Project row + `<nick>:<name>` copies of stock footprints (name -> stock lib_id),
    all written by KiCad (an API-made stand-in for a crash after S2)."""
    lib = await _local(session)

    def add_row(h: KicadHandle) -> None:
        h.kicad.add_library_table_entry(
            LibraryType.LT_FOOTPRINT,
            LibraryTableScope.LTS_PROJECT,
            lib.nickname,
            lib.uri,
            description="CopperChat local footprints",
            create_library=True,
        )

    await session.call(add_row)
    for name, source in copies.items():
        async with session.footprint_document(source) as doc:
            await doc.save_copy(f"{lib.nickname}:{name}")
    library_for(session).invalidate_footprints()
    return lib


async def test_no_library_yet(repo_session: Any) -> None:
    open_session, _root = repo_session
    session = await open_session("main")
    r = await fp_list_local(ctx(session), {})
    assert r.data is not None
    assert r.data["library"] is None and r.data["footprints"] == []
    assert r.data["truncated"] is False
    assert r.data["notes"] and r.data["notes"][0]["kind"] == "no_local_library"
    assert "fp_create" in r.data["notes"][0]["message"]
    assert not session.footprints_active


async def test_lists_seed_copy_as_unreviewed(repo_session: Any) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    lib = await _make_library(session, {"Seedcopy": SEED_LIB_ID})
    await session.restart()  # boardless and not activated again
    assert not session.footprints_active
    r = await fp_list_local(ctx(session), {})
    assert r.data is not None
    assert r.data["library"] == {
        "nickname": "OpenDriveHub",
        "uri": "${KIPRJMOD}/../../footprints/OpenDriveHub.pretty",
        "path_rel_repo": "footprints/OpenDriveHub.pretty",
    }
    assert lib.dir == (root / "footprints" / "OpenDriveHub.pretty").resolve()
    fp = r.data["footprints"][0]
    assert fp["name"] == "Seedcopy" and fp["lib_id"] == "OpenDriveHub:Seedcopy"
    assert fp["needs_user_review"] == "unreviewed"
    assert fp["source"] is None
    assert fp["pad_count"] == 2
    assert "SRN4018" in fp["description"]
    assert fp["used_by"] == []
    assert r.data["truncated"] is False
    # GetLibraryItems needs the footprint handler on a boardless server: activated
    assert [n["kind"] for n in r.data["notes"]] == ["footprint_libraries_loaded"]
    assert session.footprints_active


async def test_used_by(repo_session: Any, edit_db: Database) -> None:
    open_session, _root = repo_session
    session = await open_session("main")
    lib = await _make_library(session, {"Seedcopy": SEED_LIB_ID})
    await session.ensure_symbol_libraries()
    tool_ctx = edit_ctx(session, edit_db)
    placed, err = await call_tool(tool_ctx, "sch_place_symbol", lib_id="Device:L", at=[76.2, 76.2])
    assert not err, placed
    ref = placed["placed"]["ref"]
    assert ref == "L1"
    out, err = await call_tool(
        tool_ctx, "sch_set_fields", ref=ref, fields={"footprint": f"{lib.nickname}:Seedcopy"}
    )
    assert not err, out
    r = await fp_list_local(ctx(session), {})
    assert r.data is not None
    assert r.data["footprints"][0]["used_by"] == ["L1"]


def _hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


async def test_read_paths_write_nothing(repo_session: Any) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    lib = await _make_library(session, {"Seedcopy": SEED_LIB_ID})
    await session.restart()  # the reads below activate again
    before = _hashes(root)
    assert any(k.endswith("Seedcopy.kicad_mod") for k in before)

    listed = await fp_list_local(ctx(session), {})  # activates
    assert listed.data is not None and len(listed.data["footprints"]) == 1
    local = await fp_get(ctx(session), {"lib_id": f"{lib.nickname}:Seedcopy", "detail": "full"})
    assert local.data is not None and local.data["review"] == "unreviewed"
    stock = await fp_get(ctx(session), {"lib_id": SEED_LIB_ID})
    assert stock.data is not None
    await session.activate_footprints()

    assert _hashes(root) == before


async def test_limit_and_query(repo_session: Any) -> None:
    open_session, _root = repo_session
    session = await open_session("main")
    await _make_library(session, {"Seedcopy": SEED_LIB_ID, "R_Copy": RESISTOR})

    every = await fp_list_local(ctx(session), {})
    assert every.data is not None
    assert [f["name"] for f in every.data["footprints"]] == ["R_Copy", "Seedcopy"]
    assert every.data["truncated"] is False

    one = await fp_list_local(ctx(session), {"limit": 1})
    assert one.data is not None
    assert len(one.data["footprints"]) == 1 and one.data["truncated"] is True

    by_name = await fp_list_local(ctx(session), {"query": "seedcopy"})
    assert by_name.data is not None
    assert [f["name"] for f in by_name.data["footprints"]] == ["Seedcopy"]
    by_description = await fp_list_local(ctx(session), {"query": "resistor"})
    assert by_description.data is not None
    assert [f["name"] for f in by_description.data["footprints"]] == ["R_Copy"]
    none = await fp_list_local(ctx(session), {"query": "nothing-like-this"})
    assert none.data is not None and none.data["footprints"] == []
