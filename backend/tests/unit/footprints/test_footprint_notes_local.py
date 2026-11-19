"""`footprint_notes` for the local library (F4b Task 3, spec §7.9): row present -> API
existence check + review note, row missing -> disk check + `library_row` stage +
`footprint_review_unknown`, footprint tools off -> disk check + `footprint_unverified`
only; stock nicknames keep the old behaviour. A fake `LibraryService` records every call
(no KiCad); no KiCad file is written (a symlink is made with `os.symlink`)."""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.footprints import FootprintWriteRepo
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.specs_place import footprint_notes
from copperchat.kicad_tools.footprints.engine import LibraryRowStage
from copperchat.kicad_tools.footprints.model import REVIEW_FIELD, SOURCE_FIELD
from copperchat.library.local import LocalLibrary, derive_local_library
from copperchat.library.service import LocalLibraryInfo
from copperchat.projects import Project
from copperchat.tools import ToolContext

PID = "a1b2c3d4e5f6"
REPO = "OpenDriveHub"
NAME = "L_Bourns_SRN3012BTA"
LIB_ID = f"{REPO}:{NAME}"
STOCK = "Resistor_SMD:R_0805_2012Metric"


def _geometry(review: str) -> Any:
    from tests.unit.footprints.test_fp_get import _large

    g = _large()
    return dataclasses.replace(
        g,
        lib_id=LIB_ID,
        scope="local",
        pads=g.pads[:2],
        shapes=(),
        fields={SOURCE_FIELD: "ds p. 2", REVIEW_FIELD: review},
    )


class FakeLibrary:
    """The `LibraryService` calls `footprint_notes` makes, recorded by name."""

    def __init__(self, lib: LocalLibrary) -> None:
        self.lib = lib
        self.row_present = False
        self.row_conflict: str | None = None
        self.on_disk: list[str] = []
        self.geometry: Any = None
        self.ready = False  # footprint_libraries_ready (board open or activated)
        self.calls: list[str] = []

    async def derived_local_library(self) -> LocalLibrary | None:
        self.calls.append("derive")
        return self.lib

    async def local_library(self, **_: Any) -> LocalLibraryInfo:
        self.calls.append("table")
        return LocalLibraryInfo(self.lib, self.row_present, self.row_conflict)

    def local_names_on_disk(self, lib: LocalLibrary) -> list[str]:
        self.calls.append("disk")
        return list(self.on_disk)

    async def activate_footprints(self, notes: list[dict[str, Any]] | None = None) -> None:
        self.calls.append("activate")

    async def footprint_geometry(
        self, lib_id: str, notes: list[dict[str, Any]] | None = None
    ) -> Any:
        self.calls.append("api")
        return self.geometry

    def footprint_libraries_ready(self) -> bool:
        return self.ready

    async def footprint(self, lib_id: str) -> Any:
        self.calls.append("api")
        return None

    async def footprint_libraries(self) -> dict[str, str]:
        self.calls.append("api")
        return {self.lib.nickname: "project"}

    async def search_footprints(self, query: str, library: str | None, limit: int) -> list[Any]:
        self.calls.append("api")
        return [{"lib_id": f"{self.lib.nickname}:L_Other"}]


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "db.sqlite")
    yield d
    await d.close()


@pytest.fixture
def setup(
    make_settings: Callable[..., Settings], db: Database
) -> Callable[..., tuple[ToolContext, FakeLibrary]]:
    def make(**overrides: Any) -> tuple[ToolContext, FakeLibrary]:
        settings = make_settings(**overrides)
        root = settings.projects_root / REPO
        project_dir = root / "hw" / "io"
        project_dir.mkdir(parents=True, exist_ok=True)
        project = Project(
            PID, "io", f"{REPO}/hw/io/io.kicad_pro", project_dir, project_dir / "io.kicad_pro",
            None, None,
        )  # fmt: skip
        lib = derive_local_library(project_dir, root, frozenset())
        ctx = ToolContext(
            project=project,
            session=cast(Any, SimpleNamespace()),
            settings=settings,
            run_id="run1",
            editing=cast(Any, SimpleNamespace(db=db)),
        )
        return ctx, FakeLibrary(lib)

    return make


def _kinds(notes: list[dict[str, Any]]) -> list[str]:
    return [n["kind"] for n in notes]


async def _notes(
    ctx: ToolContext, svc: FakeLibrary, footprint: str = LIB_ID, *, explicit: bool = True
) -> tuple[list[dict[str, Any]], LibraryRowStage | None]:
    return await footprint_notes(ctx, cast(Any, svc), "Device:L", footprint, explicit=explicit)


async def _journal(db: Database) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in await db.fetchall("SELECT * FROM footprint_writes ORDER BY id")]


def _hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            out[str(p)] = f"link:{p.readlink()}"
        elif p.is_file():
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            out[str(p)] = "dir"
    return out


# -- row present (footprint tools on): API path ----------------------------------------------


async def test_row_present_api_path_needs_review(setup: Any) -> None:
    ctx, svc = setup()
    svc.row_present = True
    svc.geometry = _geometry("yes")
    notes, stage = await _notes(ctx, svc)
    assert stage is None
    assert "footprint_needs_review" in _kinds(notes)
    assert "activate" in svc.calls and "api" in svc.calls
    assert "disk" not in svc.calls


async def test_row_present_approved_has_no_review_note(setup: Any) -> None:
    ctx, svc = setup()
    svc.row_present = True
    svc.geometry = _geometry("no")
    notes, stage = await _notes(ctx, svc)
    assert stage is None and _kinds(notes) == []


async def test_pending_journal_row_makes_an_approved_footprint_need_review(
    setup: Any, db: Database
) -> None:
    """`Footprint source` present, `needs_user_review = no`, but a pending journal row of
    this project for the lib id: `review_state(pending=True)` -> the note (the wiring)."""
    ctx, svc = setup()
    svc.row_present = True
    svc.geometry = _geometry("no")
    await FootprintWriteRepo(db).insert_pending(PID, "r", LIB_ID, "f", "replace", "abc")
    notes, _ = await _notes(ctx, svc)
    assert "footprint_needs_review" in _kinds(notes)


async def test_row_present_unknown_name_not_found_with_closest(setup: Any) -> None:
    ctx, svc = setup()
    svc.row_present = True
    svc.geometry = None
    with pytest.raises(StructuredToolError) as err:
        await _notes(ctx, svc, f"{REPO}:L_Missing")
    assert err.value.code == "not_found"
    assert err.value.details["closest"] == [f"{REPO}:L_Other"]


# -- row missing: disk path + library_row ----------------------------------------------------


async def test_row_missing_disk_path_and_stage(setup: Any, db: Database) -> None:
    ctx, svc = setup()
    svc.on_disk = [NAME]
    await FootprintWriteRepo(db).insert_pending(PID, "r", f"{REPO}:X", "f", "create", "absent")
    rows, files = await _journal(db), _hashes(ctx.settings.projects_root)
    notes, stage = await _notes(ctx, svc)
    assert stage == LibraryRowStage(library=svc.lib, create_library=False, footprint=LIB_ID)
    assert "footprint_review_unknown" in _kinds(notes)
    assert "disk" in svc.calls and "api" not in svc.calls
    assert "activate" not in svc.calls  # the row stage activates in apply
    # a prepare-only call writes nothing: journal and files unchanged
    assert await _journal(db) == rows
    assert _hashes(ctx.settings.projects_root) == files


async def test_row_missing_name_not_on_disk(setup: Any) -> None:
    ctx, svc = setup()
    svc.on_disk = ["L_Bourns_SRN3012"]
    with pytest.raises(StructuredToolError) as err:
        await _notes(ctx, svc)
    assert err.value.code == "not_found"
    assert err.value.details["closest"] == [f"{REPO}:L_Bourns_SRN3012"]


async def test_row_missing_not_explicit_is_a_note(setup: Any) -> None:
    ctx, svc = setup()
    notes, stage = await _notes(ctx, svc, explicit=False)
    assert stage is None and "footprint_not_found" in _kinds(notes)


async def test_symlinked_table_is_library_conflict(setup: Any) -> None:
    ctx, svc = setup()
    svc.on_disk = [NAME]
    elsewhere = ctx.settings.projects_root / "elsewhere"
    svc.lib.table.symlink_to(elsewhere)
    with pytest.raises(StructuredToolError) as err:
        await _notes(ctx, svc)
    assert err.value.code == "library_conflict"


async def test_row_conflict_is_library_conflict(setup: Any) -> None:
    ctx, svc = setup()
    svc.on_disk = [NAME]
    svc.row_conflict = "the project footprint table has a row with another URI"
    with pytest.raises(StructuredToolError) as err:
        await _notes(ctx, svc)
    assert err.value.code == "library_conflict"


# -- footprint tools off --------------------------------------------------------------------


@pytest.mark.parametrize("row_present", [True, False])
async def test_tools_off_disk_check_only(setup: Any, row_present: bool) -> None:
    ctx, svc = setup(footprint_tools=False)
    svc.row_present = row_present
    svc.on_disk = [NAME]
    notes, stage = await _notes(ctx, svc)
    assert stage is None
    assert "footprint_unverified" in _kinds(notes)
    assert "disk" in svc.calls
    assert "activate" not in svc.calls and "api" not in svc.calls


async def test_tools_off_unknown_name_not_found(setup: Any) -> None:
    ctx, svc = setup(footprint_tools=False)
    with pytest.raises(StructuredToolError) as err:
        await _notes(ctx, svc)
    assert err.value.code == "not_found"
    assert "activate" not in svc.calls and "api" not in svc.calls


# -- stock nickname ------------------------------------------------------------------------


async def test_stock_footprint_on_boardless_server_unverified(setup: Any) -> None:
    ctx, svc = setup()
    notes, stage = await _notes(ctx, svc, STOCK)
    assert stage is None
    assert "footprint_unverified" in _kinds(notes)
    assert "activate" not in svc.calls and "api" not in svc.calls and "disk" not in svc.calls
