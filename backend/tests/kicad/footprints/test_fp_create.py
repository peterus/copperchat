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


# -- replace (F4a Task 6, spec §9.5) --------------------------------------------------------

APPROVAL = "approved 2026-11-18, conversation abcd1234, geometry 0123abcd"


async def edit_footprint(session: KicadSession, lib_id: str, change: Any) -> None:
    """Change a local footprint through the raw footprint document API (as the KiCad GUI
    or another project would): one commit, `change(doc, items)` stages the updates,
    then save. The file is never edited directly."""
    async with session.footprint_document(lib_id) as doc:
        await doc.begin()
        await change(doc, await doc.items())
        await doc.push("test edit")
        await doc.save()
    library_for(session).invalidate_footprints()


async def approve_via_api(session: KicadSession, lib_id: str) -> None:
    """What `fp_approve` will write (spec §7.7): `needs_user_review` -> `no` plus a
    `User review` field, set through the raw API."""
    from copperchat.kicad_tools.footprints import protos

    async def change(doc: Any, items: list[Any]) -> None:
        fields = {i.name: i for i in items if i.DESCRIPTOR.name == "Field"}
        flag = fields["needs_user_review"]
        flag.text.text.text = "no"
        await doc.update([flag])
        await doc.create([protos.custom_field_proto(fields["Datasheet"], "User review", APPROVAL)])

    await edit_footprint(session, lib_id, change)


async def move_pad_one(session: KicadSession, lib_id: str, dx_nm: int = 50_000) -> None:
    async def change(doc: Any, items: list[Any]) -> None:
        pad = next(i for i in items if i.DESCRIPTOR.name == "Pad" and i.number == "1")
        pad.position.x_nm += dx_nm
        await doc.update([pad])

    await edit_footprint(session, lib_id, change)


async def current(session: KicadSession, lib_id: str = LIB_ID) -> tuple[str, dict[str, str]]:
    """The geometry hash and the fields of the footprint as KiCad reads it now."""
    from copperchat.kicad_tools.footprints.model import geometry_hash

    svc = library_for(session)
    svc.invalidate_footprints()
    g = await svc.footprint_geometry(lib_id)
    assert g is not None, lib_id
    return geometry_hash(g), dict(g.fields)


def wider_pads() -> list[dict[str, Any]]:
    return [
        {"number": "1", "type": "smd", "shape": "rect", "at": [-1.15, 0], "size": [1.2, 3.4]},
        {"number": "2", "type": "smd", "shape": "rect", "at": [1.15, 0], "size": [1.2, 3.4]},
    ]


async def created_and_approved(session: KicadSession, db: Database) -> tuple[str, dict[str, str]]:
    out, err = await run_create(edit_ctx(session, db), l201_args(fields={"Manufacturer": "Bourns"}))
    if err:
        pytest.fail(json.dumps(out, indent=1))
    await approve_via_api(session, LIB_ID)
    return await current(session)


async def test_replace_resets_review(repo_session: Any, edit_db: Database) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    old_hash, old_fields = await created_and_approved(session, edit_db)
    assert old_fields["needs_user_review"] == "no" and old_fields["User review"] == APPROVAL
    got = await fp_get(edit_ctx(session, edit_db), {"lib_id": LIB_ID})
    assert got.data is not None and got.data["review"] == "approved"
    file = lib_dir(root) / f"{NAME}.kicad_mod"
    pre_sha = sha(file)

    out, err = await run_create(
        edit_ctx(session, edit_db), l201_args(pads=wider_pads(), replace=True)
    )
    if err:
        pytest.fail(json.dumps(out, indent=1))
    assert out["created"] == [LIB_ID] and out["needs_user_review"] is True
    assert out["library_created"] is False and out["check"]["passed"], out["check"]
    new_hash, fields = await current(session)
    assert new_hash != old_hash
    assert fields["needs_user_review"] == "yes" and "User review" not in fields
    assert "Manufacturer" not in fields  # rebuilt from scratch: only what this call set
    got = await fp_get(edit_ctx(session, edit_db), {"lib_id": LIB_ID})
    assert got.data is not None and got.data["review"] == "needs_user_review"
    rows = await journal(edit_db, session)
    replace = rows[-1]
    assert (replace.op, replace.state, replace.lib_id) == ("replace", "done", LIB_ID)
    assert replace.pre_sha256 == pre_sha and replace.post_sha256 == sha(file) != pre_sha
    assert replace.post_s2_sha256 not in (None, "absent", pre_sha)


@pytest.mark.parametrize("stage", ["copy_seed", "rebuild_saved", "verify"])
async def test_replace_compensation_rebuilds_preimage(
    repo_session: Any, edit_db: Database, stage: str
) -> None:
    """A failure after S2 / S3 / in the read-back of a replace: the old footprint is
    rebuilt from the API pre-image (never deleted) with the same geometry hash and
    fields - including the approval - and the journal row is `compensated` with the
    restored file's hash. Spec §16 allows `document_inconsistent` instead; this pins
    that the pinned KiCad restores it exactly."""
    open_session, root = repo_session
    session = await open_session("main")
    old_hash, old_fields = await created_and_approved(session, edit_db)
    file = lib_dir(root) / f"{NAME}.kicad_mod"
    pre_sha = sha(file)

    out, err = await run_create(
        edit_ctx(session, edit_db), l201_args(pads=wider_pads(), replace=True), fail_after=stage
    )
    code = "verification_failed" if stage == "verify" else "injected"
    if not (err and out["error"]["code"] == code):
        pytest.fail(json.dumps(out, indent=1))
    assert file.is_file()
    assert await current(session) == (old_hash, old_fields)
    rows = await journal(edit_db, session)
    replace = rows[-1]
    assert (replace.op, replace.state) == ("replace", "compensated")
    assert replace.pre_sha256 == pre_sha and replace.post_sha256 == sha(file)
    assert NICK in await project_rows(session)  # the row was there before: never touched


async def test_replace_compensation_failure_keeps_the_footprint(
    repo_session: Any, edit_db: Database
) -> None:
    """The rebuild of the pre-image fails: `document_inconsistent` naming the footprint,
    which is never deleted (the original must not vanish); the row is `leftover`."""
    open_session, root = repo_session
    session = await open_session("main")
    await created_and_approved(session, edit_db)
    file = lib_dir(root) / f"{NAME}.kicad_mod"
    out, err = await run_create(
        edit_ctx(session, edit_db),
        l201_args(pads=wider_pads(), replace=True),
        fail_after="rebuild_saved",
        fail_compensation=True,
    )
    assert err and out["error"]["code"] == "document_inconsistent", out
    assert out["error"]["details"]["leftovers"] == [LIB_ID]
    assert LIB_ID in out["error"]["why"]
    assert file.is_file() and NICK in await project_rows(session)
    rows = await journal(edit_db, session)
    assert (rows[-1].op, rows[-1].state, rows[-1].post_sha256) == ("replace", "leftover", sha(file))


async def test_replace_preimage_changed_between_prepare_and_apply(
    repo_session: Any, edit_db: Database
) -> None:
    """The footprint is changed through the raw API after prepare (the GUI, another
    project): the re-check under the library lock refuses before any write, the
    change stays, no `replace` journal row."""
    open_session, root = repo_session
    session = await open_session("main")
    await created_and_approved(session, edit_db)
    file = lib_dir(root) / f"{NAME}.kicad_mod"
    rows_before = await journal(edit_db, session)
    edited: list[str] = []

    async def gui_edit(c: ToolContext, _cs: FootprintChangeSet) -> None:
        await move_pad_one(c.session, LIB_ID)
        edited.append(sha(file))

    out, err = await run_create(
        edit_ctx(session, edit_db), l201_args(pads=wider_pads(), replace=True),
        after_prepare=gui_edit,
    )  # fmt: skip
    assert err and out["error"]["code"] == "library_conflict", out
    assert "changed since prepare" in out["error"]["why"]
    assert [sha(file)] == edited
    g = await library_for(session).footprint_geometry(LIB_ID)
    assert g is not None
    assert next(p for p in g.pads if p.number == "1").at[0] == pytest.approx(-1.05)
    assert await journal(edit_db, session) == rows_before


async def test_replace_missing_at_apply(repo_session: Any, edit_db: Database) -> None:
    open_session, root = repo_session
    session = await open_session("main")
    await created_and_approved(session, edit_db)
    rows_before = await journal(edit_db, session)

    async def gone(c: ToolContext, _cs: FootprintChangeSet) -> None:
        def delete(h: KicadHandle) -> None:
            h.kicad.delete_library_item(FP, NICK, NAME)

        await c.session.call(delete)
        library_for(c.session).invalidate_footprints()

    out, err = await run_create(
        edit_ctx(session, edit_db), l201_args(replace=True), after_prepare=gone
    )
    assert err and out["error"]["code"] == "not_found", out
    assert not (lib_dir(root) / f"{NAME}.kicad_mod").exists()
    assert await journal(edit_db, session) == rows_before


async def test_replace_symlinked_file_refused(repo_session: Any, edit_db: Database) -> None:
    """`<name>.kicad_mod` replaced by a symlink (to the same KiCad-saved bytes) after
    prepare: `library_conflict` under the library lock, nothing written; a later call
    is refused in prepare already."""
    open_session, root = repo_session
    session = await open_session("main")
    await created_and_approved(session, edit_db)
    file = lib_dir(root) / f"{NAME}.kicad_mod"
    elsewhere = root / "elsewhere.kicad_mod"
    rows_before = await journal(edit_db, session)
    snapshot: dict[str, dict[str, str]] = {}

    async def swap(_c: ToolContext, _cs: FootprintChangeSet) -> None:
        file.rename(elsewhere)
        file.symlink_to(elsewhere)
        snapshot["tree"] = tree(root)

    out, err = await run_create(
        edit_ctx(session, edit_db), l201_args(replace=True), after_prepare=swap
    )
    assert err and out["error"]["code"] == "library_conflict", out
    assert file.is_symlink() and tree(root) == snapshot["tree"]
    out, err = await run_create(edit_ctx(session, edit_db), l201_args(replace=True))
    assert err and out["error"]["code"] == "library_conflict", out
    assert tree(root) == snapshot["tree"]
    assert await journal(edit_db, session) == rows_before


async def test_replace_journal_pending_never_deleted(repo_session: Any, edit_db: Database) -> None:
    """The server dies right after S2 of a replace (the seed copy overwrote the name):
    the row stays `pending`; the next writing call marks it `leftover` and never deletes
    the file (spec §9.3: a replace target is never destroyed; History restores it)."""
    from copperchat.kicad_session import KicadUnavailable

    open_session, root = repo_session
    session = await open_session("main")
    await created_and_approved(session, edit_db)
    unavailable: list[KicadUnavailable] = []

    def ctx() -> ToolContext:
        return edit_ctx(session, edit_db, on_kicad_unavailable=unavailable.append)

    out, err = await run_create(ctx(), l201_args(replace=True), kill_after="copy_seed")
    assert err and len(unavailable) == 1, out
    file = lib_dir(root) / f"{NAME}.kicad_mod"
    seed_copy = sha(file)
    rows = await journal(edit_db, session)
    assert (rows[-1].op, rows[-1].state) == ("replace", "pending")
    assert rows[-1].post_s2_sha256 == seed_copy

    await session.restart()
    out, err = await run_create(ctx(), l201_args(name="L_Other_3030"))
    if err:
        pytest.fail(json.dumps(out, indent=1))
    leftover = [n for n in out["notes"] if n["kind"] == "pending_leftover"]
    assert [n["lib_id"] for n in leftover] == [LIB_ID], out["notes"]
    assert not [n for n in out["notes"] if n["kind"] == "pending_cleaned"]
    assert file.is_file() and sha(file) == seed_copy
    rows = await journal(edit_db, session)
    replace = next(r for r in rows if r.op == "replace")
    assert (replace.state, replace.post_sha256) == ("leftover", seed_copy)
