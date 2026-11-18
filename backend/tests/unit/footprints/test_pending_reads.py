"""`pending` of the read tools comes from the write journal (F4a Task 1, FP-C3/FP-C7): a
`pending` row whose lib_id is the footprint makes it `unreviewed` in `fp_get`,
`fp_list_local` and `fp_check`; the reads never write the journal."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from kipy.proto.board import board_types_pb2 as bt

from copperchat.db import Database
from copperchat.db.repos.footprints import FootprintWriteRepo
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.footprints import read
from copperchat.kicad_tools.footprints.model import REVIEW_FIELD, SOURCE_FIELD
from copperchat.library import service as service_mod
from copperchat.tools import ToolContext
from tests.unit.footprints.test_fp_list_local import ListSession
from tests.unit.library.test_service import LOCAL_URI, REPO, _policy

PID = "a1b2c3d4e5f6"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "db.sqlite")
    yield d
    await d.close()


class ReviewedSession(ListSession):
    """Every footprint carries `needs_user_review = <review>` (source via `sources`)."""

    review = "yes"

    async def call(self, fn: Any) -> Any:
        out = await super().call(fn)
        if getattr(fn, "func", fn) is service_mod._fetch_footprint_protos:
            for proto in out:
                field = bt.Field()
                field.name = REVIEW_FIELD
                field.text.text.text = self.review
                proto.items.add().Pack(field)
        return out


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReviewedSession:
    s = ReviewedSession(tmp_path / REPO)
    s.project.id = PID  # type: ignore[attr-defined]

    async def policy(project: Any, projects_root: Path) -> Any:
        return _policy(s.root, project.dir)

    monkeypatch.setattr(service_mod, "repo_policy", policy)
    return s


def _ctx(session: Any, db: Database | None) -> ToolContext:
    return ToolContext(
        project=cast(Any, session.project),
        session=cast(KicadSession, session),
        settings=cast(Any, session.settings),
        editing=cast(Any, SimpleNamespace(db=db)) if db is not None else None,
    )


async def _journal(db: Database) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in await db.fetchall("SELECT * FROM footprint_writes ORDER BY id")]


async def test_fp_list_local_pending_row_is_unreviewed(
    session: ReviewedSession, db: Database
) -> None:
    session.rows = {REPO: LOCAL_URI}
    session.footprints = {"A_Coil": "a", "B_Coil": "b"}
    session.sources = {"A_Coil": "ds p. 1", "B_Coil": "ds p. 2"}
    repo = FootprintWriteRepo(db)
    await repo.insert_pending(PID, "run", f"{REPO}:A_Coil", "footprints/x", "create", "absent")
    # another project's pending row and a row_add row (lib_id = nickname) do not count
    await repo.insert_pending("ffffffffffff", "run", f"{REPO}:B_Coil", "f", "create", "absent")
    await repo.insert_pending(PID, "run", REPO, "hw/main/fp-lib-table", "row_add", "absent")
    before = await _journal(db)
    r = await read.fp_list_local(_ctx(session, db), {})
    assert r.data is not None
    states = {f["name"]: f["needs_user_review"] for f in r.data["footprints"]}
    assert states == {"A_Coil": "unreviewed", "B_Coil": "needs_user_review"}
    assert await _journal(db) == before
    # without the editing service (plain tool context) nothing is pending
    r = await read.fp_list_local(_ctx(session, None), {})
    assert r.data is not None
    assert {f["needs_user_review"] for f in r.data["footprints"]} == {"needs_user_review"}


async def test_finalised_row_is_not_pending(session: ReviewedSession, db: Database) -> None:
    session.rows = {REPO: LOCAL_URI}
    session.footprints = {"A_Coil": "a"}
    session.sources = {"A_Coil": "ds p. 1"}
    repo = FootprintWriteRepo(db)
    row = await repo.insert_pending(PID, "run", f"{REPO}:A_Coil", "f", "create", "absent")
    await repo.finalise(row, "done", "abc")
    r = await read.fp_list_local(_ctx(session, db), {})
    assert r.data is not None
    assert r.data["footprints"][0]["needs_user_review"] == "needs_user_review"


@pytest.fixture
def local_geometry(monkeypatch: pytest.MonkeyPatch) -> Any:
    """`read._geometry` -> an approved local footprint (source + needs_user_review=no)."""
    from tests.unit.footprints.test_fp_get import _large

    g = dataclasses.replace(
        _large(),
        lib_id=f"{REPO}:L_Local",
        scope="local",
        pads=_large().pads[:2],
        shapes=(),
        fields={SOURCE_FIELD: "ds p. 4", REVIEW_FIELD: "no"},
    )

    async def geometry(ctx: Any, lib_id: str, *_: Any) -> Any:
        return dataclasses.replace(g, lib_id=lib_id)

    monkeypatch.setattr(read, "_geometry", geometry)
    return g


async def test_fp_get_pending_row_is_unreviewed(
    session: ReviewedSession, db: Database, local_geometry: Any
) -> None:
    ctx = _ctx(session, db)
    lib_id = f"{REPO}:L_Local"
    r = await read.fp_get(ctx, {"lib_id": lib_id})
    assert r.data is not None and r.data["review"] == "approved"
    await FootprintWriteRepo(db).insert_pending(PID, "run", lib_id, "f", "replace", "abc")
    before = await _journal(db)
    r = await read.fp_get(ctx, {"lib_id": lib_id})
    assert r.data is not None and r.data["review"] == "unreviewed"
    assert await _journal(db) == before


async def test_fp_check_pending_row_reports_needs_review(
    session: ReviewedSession, db: Database, local_geometry: Any
) -> None:
    ctx = _ctx(session, db)
    lib_id = f"{REPO}:L_Local"

    def review_findings(data: dict[str, Any]) -> list[Any]:
        return [f.get("measured") for f in data["findings"] if f["rule"] == "needs_user_review"]

    r = await read.fp_check(ctx, {"lib_id": lib_id})
    assert r.data is not None and review_findings(r.data) == []
    await FootprintWriteRepo(db).insert_pending(PID, "run", lib_id, "f", "create", "absent")
    before = await _journal(db)
    r = await read.fp_check(ctx, {"lib_id": lib_id})
    assert r.data is not None and review_findings(r.data) == ["unreviewed"]
    assert await _journal(db) == before
