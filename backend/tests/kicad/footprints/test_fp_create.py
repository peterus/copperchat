"""`fp_create` on a real api-server (F4a Task 3, spec §7.6, §9): S1 library row, API name
re-check, S2 seed copy, S3 rebuild, read-back, journal, refusals. Every library file is
written by KiCad (rows via `AddLibraryTableEntry`, footprints via `SaveCopyOfDocument`);
the only byte copy is of a KiCad-saved footprint file."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from kipy.proto.common.types.library_types_pb2 import LibraryTableScope, LibraryType

from copperchat.agent.sdk_tools import build_sdk_tool_list
from copperchat.db import Database
from copperchat.db.repos.footprints import FootprintWrite, FootprintWriteRepo
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_session.footprint_doc import SEED_LIB_ID
from copperchat.kicad_tools.footprints.create import apply_footprint_create, prepare_create
from copperchat.kicad_tools.footprints.engine import (
    FootprintChangeSet,
    stock_footprint_nicknames,
)
from copperchat.kicad_tools.footprints.model import RESERVED_FIELDS
from copperchat.kicad_tools.footprints.read import fp_get
from copperchat.kicad_tools.footprints.specs import FP_CREATE_SCHEMA
from copperchat.library.service import library_for
from copperchat.tools.spec import ToolContext, edit_spec
from tests.support.edit import edit_ctx

pytestmark = pytest.mark.kicad

NICK = "OpenDriveHub"
NAME = "L_Bourns_SRN3012BTA"
LIB_ID = f"{NICK}:{NAME}"
URI = "${KIPRJMOD}/../../footprints/OpenDriveHub.pretty"
URL = "https://www.bourns.com/docs/product-datasheets/srn3012bta.pdf"
FP = LibraryType.LT_FOOTPRINT
PROJECT = LibraryTableScope.LTS_PROJECT


def l201_args(**kw: Any) -> dict[str, Any]:
    """The owner's L201 (spike 3A): 1.3 x 3.5 mm pads at x = +-1.1, a 3 x 3 mm body."""
    args: dict[str, Any] = {
        "name": NAME,
        "description": "Bourns SRN3012BTA shielded power inductor, 3.0x3.0 mm",
        "mounting": "smd",
        "pads": [
            {"number": "1", "type": "smd", "shape": "rect", "at": [-1.1, 0], "size": [1.3, 3.5]},
            {"number": "2", "type": "smd", "shape": "rect", "at": [1.1, 0], "size": [1.3, 3.5]},
        ],
        "body": {"rect": [3.0, 3.0]},
        "source": {"document": URL, "location": "p.2, 'Recommended layout'"},
    }
    args.update(kw)
    return args


Hook = Callable[[ToolContext, FootprintChangeSet], Awaitable[None]]


async def run_create(
    ctx: ToolContext, args: dict[str, Any], *, after_prepare: Hook | None = None, **hooks: Any
) -> tuple[dict[str, Any], bool]:
    """`fp_create` through the real adapter; `hooks` set the change set's test hooks
    (`fail_after`, `kill_after`, `fail_compensation`), `after_prepare` runs between
    prepare and apply (a concurrent change)."""

    async def prepare(c: ToolContext, a: dict[str, Any]) -> Any:
        cs = await prepare_create(c, a)
        for key, value in hooks.items():
            setattr(cs, key, value)
        if after_prepare is not None:
            await after_prepare(c, cs)
        return cs

    spec = edit_spec("fp_create", "d", FP_CREATE_SCHEMA, prepare, apply_footprint_create)
    raw = await build_sdk_tool_list([spec], lambda: ctx)[0].handler(args)
    payload: dict[str, Any] = json.loads(raw["content"][0]["text"])
    return payload, bool(raw.get("is_error"))


def project_rows(session: KicadSession) -> Awaitable[dict[str, str]]:
    def read(h: KicadHandle) -> dict[str, str]:
        return {r.nickname: r.uri for r in h.kicad.get_library_table(FP, PROJECT).rows}

    return session.call(read)


async def add_row(session: KicadSession, *, create: bool = True) -> None:
    def add(h: KicadHandle) -> None:
        h.kicad.add_library_table_entry(
            FP, PROJECT, NICK, URI, description="CopperChat local footprints", create_library=create
        )

    await session.call(add)


async def journal(db: Database, session: KicadSession) -> list[FootprintWrite]:
    return await FootprintWriteRepo(db).after_id(session.project.id, 0)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path) -> dict[str, str]:
    """sha256 of every file, a marker for every directory and symlink below `root`."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if ".git" in p.relative_to(root).parts:
            continue
        key = str(p.relative_to(root))
        if p.is_symlink():
            out[key] = f"symlink:{p.readlink()}"
        elif p.is_dir():
            out[key] = "dir"
        elif p.is_file():
            out[key] = sha(p)
    return out


def lib_dir(root: Path) -> Path:
    return root.resolve() / "footprints" / f"{NICK}.pretty"


async def library_via_io(open_session: Any, copies: dict[str, str] | None = None) -> None:
    """The repo's library directory made by the sibling project `io` (its own row,
    KiCad creates the directory; `copies` are SaveCopyOfDocument of stock footprints)."""
    io = await open_session("io")
    await add_row(io)
    for name, source in (copies or {}).items():
        async with io.footprint_document(source) as doc:
            await doc.save_copy(f"{NICK}:{name}")


# -- success --------------------------------------------------------------------------------


async def test_l201_create_basic(repo_session: Any, edit_db: Database) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    ctx = edit_ctx(session, edit_db)
    out, err = await run_create(ctx, l201_args(fields={"Manufacturer": "Bourns"}))
    if err:
        pytest.fail(json.dumps(out, indent=1))
    file = lib_dir(root) / f"{NAME}.kicad_mod"
    assert file.is_file()
    assert out["created"] == [LIB_ID]
    assert out["file"] == f"footprints/{NICK}.pretty/{NAME}.kicad_mod"
    assert out["library_created"] is True and out["needs_user_review"] is True
    assert out["check"]["passed"], out["check"]
    assert out["geometry"]["pad_count"] == 2
    assert out["logical_changes"] == 5
    assert "fp_render" in out["suggestion"]
    assert (await project_rows(session))[NICK] == URI

    table = session.project.dir.resolve() / "fp-lib-table"
    rows = await journal(edit_db, session)
    assert [(r.op, r.state) for r in rows] == [("row_add", "done"), ("create", "done")]
    row_add, create = rows
    assert (row_add.lib_id, row_add.file_rel) == (NICK, "hardware/main/fp-lib-table")
    assert row_add.pre_sha256 == "absent" and row_add.post_sha256 == sha(table)
    assert (create.lib_id, create.file_rel) == (LIB_ID, out["file"])
    assert create.pre_sha256 == "absent" and create.post_s2_sha256 not in (None, "absent")
    assert create.post_sha256 == sha(file) != create.post_s2_sha256
    assert {table, file} <= set(ctx.library_touched)

    got = await fp_get(edit_ctx(session, edit_db), {"lib_id": LIB_ID})
    assert got.data is not None and got.data["review"] == "needs_user_review"
    assert got.data["fields"]["Manufacturer"] == "Bourns"
    text = file.read_text()
    assert "SRN4018" not in text and "(model " not in text


async def test_pad_kinds_round_trip(repo_session: Any, edit_db: Database) -> None:
    """Every pad type and shape the builder writes, a B-side pad, a model link, keywords
    and extra fields read back equal to the target (geometry_equal at 1 um)."""
    open_session, _root = repo_session
    session = await open_session("main")
    pads = [
        {"number": "1", "type": "tht", "shape": "rect", "at": [-2.54, 0], "size": [1.7, 1.7],
         "drill": {"size": [1.0, 1.0]}},
        {"number": "2", "type": "tht", "shape": "circle", "at": [0, 0], "size": [1.7, 1.7],
         "drill": {"size": [1.0, 1.0]}},
        {"number": "3", "type": "tht", "shape": "oval", "at": [2.54, 0], "size": [1.7, 2.4],
         "drill": {"size": [0.8, 1.4], "shape": "oval"}},
        {"number": "", "type": "npth", "shape": "circle", "at": [0, 3], "size": [1.2, 1.2],
         "drill": {"size": [1.2, 1.2]}},
        {"number": "4", "type": "smd", "shape": "roundrect", "at": [0, -3], "size": [1.0, 0.6],
         "roundrect_ratio": 0.2, "rotation": 90, "layers": ["B.Cu", "B.Mask", "B.Paste"]},
        {"number": "5", "type": "connector", "shape": "rect", "at": [4.5, 3], "size": [1, 2]},
        {"number": "6", "type": "smd", "shape": "rect", "at": [-4.5, 3], "size": [1, 1],
         "layers": ["F.Paste", "F.Cu", "F.Mask"]},
        {"number": "7", "type": "tht", "shape": "circle", "at": [-4.5, -3], "size": [1.5, 1.5],
         "drill": {"size": [0.9, 0.9]}, "layers": ["F.Mask", "*.Cu", "*.Paste"]},
    ]  # fmt: skip
    args = l201_args(
        name="J_Test",
        mounting="tht",
        keywords="test header",
        pads=pads,
        body={"rect": [8.0, 4.0], "center": [0.5, 0]},
        model={"path": "${KICAD10_3DMODEL_DIR}/x.3dshapes/x.step", "rotation": [0, 0, 90]},
        fields={"MPN": "X-1", "Manufacturer": "ACME"},
    )
    out, err = await run_create(edit_ctx(session, edit_db), args)
    if err:
        pytest.fail(json.dumps(out, indent=1))
    geometry = out["geometry"]
    assert geometry["lib_id"] == f"{NICK}:J_Test" and geometry["mounting"] == "tht"
    assert geometry["pad_count"] == 8 and geometry["keywords"] == "test header"
    assert [m["path"] for m in geometry["models"]] == ["${KICAD10_3DMODEL_DIR}/x.3dshapes/x.step"]
    by_number = {p["number"]: p for p in geometry["pads"]}
    assert by_number["3"]["drill"] == {"size": [0.8, 1.4], "shape": "oval"}
    assert by_number["4"]["layers"] == ["B.Cu", "B.Mask", "B.Paste"]
    assert by_number["4"]["rotation"] == 90.0
    assert by_number["7"]["layers"] == ["*.Cu", "F.Mask", "F.Paste", "B.Paste"]
    assert geometry["fields"]["MPN"] == "X-1"


async def test_description_extra_field(repo_session: Any, edit_db: Database) -> None:
    """A KiCad footprint has a built-in `Description` field (FOOTPRINT's mandatory
    fields). An extra field of that name is refused before anything is written."""
    open_session, root = repo_session
    session = await open_session("main")
    out, err = await run_create(edit_ctx(session, edit_db), l201_args(fields={"Description": "x"}))
    if not (err and out["error"]["code"] == "invalid_argument"):
        pytest.fail(json.dumps(out, indent=1))
    assert "description" not in RESERVED_FIELDS  # a build-side rule, not FP-C3
    assert not lib_dir(root).exists()


async def test_stock_dir_nicknames_equal_the_global_table(repo_session: Any) -> None:
    """Index author call 9: the stock-dir fallback of the library locator names exactly
    the api-server's global footprint table in the real image."""
    open_session, _root = repo_session
    session = await open_session("main")
    api = await library_for(session).global_footprint_nicknames()
    assert api and stock_footprint_nicknames(session.settings) == api


# -- refusals -------------------------------------------------------------------------------


async def test_refusals_write_nothing(repo_session: Any, edit_db: Database) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    ctx = edit_ctx(session, edit_db)
    out, err = await run_create(ctx, l201_args())
    if err:
        pytest.fail(json.dumps(out, indent=1))
    before, rows_before = tree(root), await journal(edit_db, session)

    cases = [
        (l201_args(), "already_exists"),
        (l201_args(name="L_Other", source={"document": "docs/missing.pdf", "location": "p.1"}),
         "invalid_argument"),
        (l201_args(name="L_Other", pads=[{"number": "1", "type": "smd", "shape": "rect",
                                          "at": [0, 0], "size": [1, 1], "layers": ["F.Cu"]}]),
         "check_failed"),
    ]  # fmt: skip
    for args, code in cases:
        out, err = await run_create(edit_ctx(session, edit_db), args)
        assert err and out["error"]["code"] == code, out
    assert tree(root) == before
    assert await journal(edit_db, session) == rows_before


async def test_file_on_disk_before_s2_refused(repo_session: Any, edit_db: Database) -> None:
    """A regular `<name>.kicad_mod` appears between prepare and apply (a byte copy of a
    KiCad-saved footprint), unknown to this project: `already_exists` after S1, the file
    untouched, S1 compensated (row gone; the directory existed, so it stays)."""
    open_session, root = repo_session
    await library_via_io(open_session, {"Other": SEED_LIB_ID})
    session = await open_session("main")
    target = lib_dir(root) / f"{NAME}.kicad_mod"

    async def appear(_ctx: ToolContext, _cs: FootprintChangeSet) -> None:
        shutil.copyfile(lib_dir(root) / "Other.kicad_mod", target)

    ctx = edit_ctx(session, edit_db)
    out, err = await run_create(ctx, l201_args(), after_prepare=appear)
    assert err and out["error"]["code"] == "already_exists", out
    assert sha(target) == sha(lib_dir(root) / "Other.kicad_mod")
    assert NICK not in await project_rows(session)
    assert lib_dir(root).is_dir()
    rows = await journal(edit_db, session)
    assert [(r.op, r.state) for r in rows] == [("row_add", "compensated")]


async def test_create_new_name_symlink_refused(repo_session: Any, edit_db: Database) -> None:
    """`<name>.kicad_mod` is a dangling symlink and the project has no row: prepare
    passes (no regular file of that name), apply refuses with `library_conflict` under
    the library lock before its first write (FP-C8) - no row, the link untouched."""
    open_session, root = repo_session
    await library_via_io(open_session)
    session = await open_session("main")
    link = lib_dir(root) / f"{NAME}.kicad_mod"
    link.symlink_to(root / "nowhere.kicad_mod")
    before = tree(root)
    out, err = await run_create(edit_ctx(session, edit_db), l201_args())
    assert err and out["error"]["code"] == "library_conflict", out
    assert link.is_symlink() and tree(root) == before
    assert NICK not in await project_rows(session)
    assert await journal(edit_db, session) == []


async def test_table_symlink_refused(repo_session: Any, edit_db: Database) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    table = session.project.dir / "fp-lib-table"
    table.symlink_to(root / "elsewhere-fp-lib-table")
    before = tree(root)
    out, err = await run_create(edit_ctx(session, edit_db), l201_args())
    assert err and out["error"]["code"] == "library_conflict", out
    assert tree(root) == before
    assert await journal(edit_db, session) == []


async def test_api_recheck_before_s2_refuses_and_rolls_back_row(
    repo_session: Any, edit_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After S1, before S2, the same name is created through the raw API (as the KiCad
    GUI would): the re-check refuses with `already_exists`, the row this call added is
    removed again, the foreign footprint stays untouched (so does its directory, which
    is not empty)."""
    from copperchat.kicad_tools.footprints import engine

    open_session, root = repo_session
    session = await open_session("main")
    real = engine._recheck_name

    async def gui_first(ctx: ToolContext, lib: Any, name: str) -> None:
        async with ctx.session.footprint_document(SEED_LIB_ID) as doc:
            await doc.save_copy(f"{NICK}:{name}")
        library_for(ctx.session).invalidate_footprints()
        await real(ctx, lib, name)

    monkeypatch.setattr(engine, "_recheck_name", gui_first)
    foreign = lib_dir(root) / f"{NAME}.kicad_mod"
    out, err = await run_create(edit_ctx(session, edit_db), l201_args())
    assert err and out["error"]["code"] == "already_exists", out
    assert NICK not in await project_rows(session)
    assert foreign.is_file() and "SRN4018" in foreign.read_text()
    rows = await journal(edit_db, session)
    assert [(r.op, r.state) for r in rows] == [("row_add", "compensated")]
