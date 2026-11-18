"""`fp_list_local` against a fake session (F1 Task 6 review): notes and errors of the
no-row / conflict / transient-root paths, unreadable footprints, query AND."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.footprints import read
from copperchat.library import service as service_mod
from copperchat.tools import ToolContext
from tests.unit.library.test_service import (
    LOCAL_URI,
    REPO,
    LocalSession,
    _fixture,
    _policy,
    _transient,
)


class ListSession(LocalSession):
    """Serves the local library `footprints` (name -> description); names in
    `unreadable` are listed by GetLibraryItems but not served by GetItemsFromLibrary."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.footprints: dict[str, str] = {}
        self.unreadable: set[str] = set()

    async def call(self, fn: Any) -> Any:
        func = getattr(fn, "func", fn)
        if func is service_mod._footprint_names:
            return sorted(self.footprints)
        if func is service_mod._fetch_footprint_protos:
            _nickname, names = fn.args
            out = []
            for name in names:
                if name in self.unreadable:
                    continue
                proto = _fixture("seed_srn4018")
                proto.id.entry_name = name
                proto.attributes.description = self.footprints[name]
                out.append(proto)
            return out
        if getattr(func, "__name__", "") == "footprint_users":
            return {f"{REPO}:B_Coil": ["L2", "L10"]}
        return await super().call(fn)


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ListSession:
    s = ListSession(tmp_path / REPO)

    async def policy(project: Any, projects_root: Path) -> Any:
        return _policy(s.root, project.dir)

    monkeypatch.setattr(service_mod, "repo_policy", policy)
    return s


def _ctx(session: ListSession) -> ToolContext:
    return ToolContext(
        project=cast(Any, session.project),
        session=cast(KicadSession, session),
        settings=cast(Any, session.settings),
    )


async def test_transient_root_raises(session: ListSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _transient(monkeypatch, session)
    with pytest.raises(StructuredToolError) as err:
        await read.fp_list_local(_ctx(session), {})
    assert err.value.code == "repo_root_unknown"
    assert session.activations == 0


async def test_library_conflict_note(session: ListSession) -> None:
    session.rows = {REPO: "${KIPRJMOD}/elsewhere.pretty"}
    r = await read.fp_list_local(_ctx(session), {})
    assert r.data is not None and r.data["library"] is None
    assert [n["kind"] for n in r.data["notes"]] == ["library_conflict"]
    assert "elsewhere.pretty" in r.data["notes"][0]["message"]
    assert session.activations == 0


async def test_no_row_counts_files_on_disk(session: ListSession) -> None:
    r = await read.fp_list_local(_ctx(session), {})
    assert r.data is not None
    assert "already holds" not in r.data["notes"][0]["message"]
    pretty = session.root / "footprints" / f"{REPO}.pretty"
    pretty.mkdir(parents=True)
    for name in ("A", "B"):
        (pretty / f"{name}.kicad_mod").write_bytes(b"opaque")  # never parsed
    r = await read.fp_list_local(_ctx(session), {})
    assert r.data is not None and r.data["library"] is None
    note = r.data["notes"][0]
    assert note["kind"] == "no_local_library"
    assert "already holds 2 footprint(s)" in note["message"]
    assert session.activations == 0


async def test_query_and_unreadable(session: ListSession) -> None:
    session.rows = {REPO: LOCAL_URI}
    session.footprints = {
        "A_Coil": "shielded power inductor",
        "B_Coil": "power inductor",
        "C_Cap": "shielded capacitor",
        "D_Bad": "unreadable",
    }
    session.unreadable = {"D_Bad"}
    r = await read.fp_list_local(_ctx(session), {"query": "Shielded INDUCTOR"})
    assert r.data is not None
    assert [f["name"] for f in r.data["footprints"]] == ["A_Coil"]  # every word must match
    kinds = [n["kind"] for n in r.data["notes"]]
    assert kinds == ["footprint_libraries_loaded", "footprint_unreadable"]
    bad = r.data["notes"][1]
    assert bad["count"] == 1 and bad["lib_ids"] == [f"{REPO}:D_Bad"]

    every = await read.fp_list_local(_ctx(session), {})
    assert every.data is not None
    assert [f["name"] for f in every.data["footprints"]] == ["A_Coil", "B_Coil", "C_Cap"]
    b = every.data["footprints"][1]
    assert b["used_by"] == ["L2", "L10"] and b["needs_user_review"] == "unreviewed"


def test_none_found_batch_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from kipy.errors import ApiError

    def none_found(*args: Any) -> Any:
        raise ApiError("KiCad returned error: none of the requested items were found")

    monkeypatch.setattr(service_mod, "_get_items", none_found)
    h = cast(Any, None)
    assert service_mod._fetch_footprint_protos(REPO, ["X", "Y"], h) == []
    assert service_mod._fetch_footprint_proto(REPO, "X", h) is None
    assert service_mod._fetch_footprints(REPO, ["X"], h) == []

    def other(*args: Any) -> Any:
        raise ApiError("KiCad returned error: no handler available")

    monkeypatch.setattr(service_mod, "_get_items", other)
    with pytest.raises(ApiError):
        service_mod._fetch_footprint_protos(REPO, ["X"], h)
