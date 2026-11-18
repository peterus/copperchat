"""LibraryService state machine and error mapping with a fake session (no KiCad)."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession, LibrariesLoading
from copperchat.library import service as service_mod
from copperchat.library.index import SymbolRow
from copperchat.library.service import LibraryService


class FakeSession:
    read_only = False

    def __init__(self) -> None:
        self.fail_with: Exception | None = None
        self.release = asyncio.Event()
        self.release.set()
        self.calls = 0

    async def ensure_symbol_libraries(self) -> None:
        await self.release.wait()
        if self.fail_with is not None:
            raise self.fail_with

    async def call(self, fn: Any) -> Any:
        self.calls += 1
        name = getattr(fn, "__name__", "") or getattr(getattr(fn, "func", None), "__name__", "")
        if name == "run":  # _scopes(...)
            return {"Device": "global"}
        if name == "_symbol_ids":
            return [("Device", "R"), ("Device", "C")]
        if name == "_fetch_rows":
            return [SymbolRow("Device:R", "Resistor", "R res resistor", 1, 2)]
        raise AssertionError(f"unexpected call {fn!r}")


def _svc(session: FakeSession) -> LibraryService:
    return LibraryService(cast(KicadSession, session))


async def test_warm_up_ready_and_omitted_ids_indexed() -> None:
    session = FakeSession()  # the service holds the session weakly
    svc = _svc(session)
    assert not svc.started
    assert await svc.wait_ready(5)
    assert svc.started and svc.state == "ready"
    ids = [r["lib_id"] for r in await svc.search_symbols("resistor")]
    assert ids == ["Device:R"]
    assert [r["lib_id"] for r in await svc.search_symbols("C")] == ["Device:C"]


async def test_loading_then_wait_timeout() -> None:
    session = FakeSession()
    session.release.clear()
    svc = _svc(session)
    assert await svc.wait_ready(0.05) is False
    assert svc.state == "loading"
    with pytest.raises(StructuredToolError) as err:
        await svc.search_symbols("resistor")
    assert err.value.code == "libraries_loading"
    session.release.set()
    assert await svc.wait_ready(5)


async def test_failed_warm_up_is_retried() -> None:
    session = FakeSession()
    session.fail_with = LibrariesLoading("still loading after 120s")
    svc = _svc(session)
    assert await svc.wait_ready(5) is False
    assert svc.state == "failed" and "120s" in (svc.error or "")
    session.fail_with = None
    with pytest.raises(StructuredToolError) as err:
        await svc.search_symbols("resistor")  # restarts the warm-up
    assert err.value.code == "libraries_loading" and "failed" in err.value.why
    assert await svc.wait_ready(5)


async def test_invalid_arguments() -> None:
    session = FakeSession()
    svc = _svc(session)
    assert await svc.wait_ready(5)
    for kwargs in ({"query": " "}, {"query": "x" * 201}, {"query": "r", "limit": "5"}):
        with pytest.raises(StructuredToolError) as err:
            await svc.search_symbols(**kwargs)  # type: ignore[arg-type]
        assert err.value.code == "invalid_argument"
    with pytest.raises(StructuredToolError) as err:
        await svc.search_symbols("r", library="Devic")
    assert err.value.code == "not_found"


async def test_kicad_errors_are_structured() -> None:
    from kipy.errors import ApiError

    with pytest.raises(StructuredToolError) as err, service_mod._kicad_errors("Could not read X"):
        raise ApiError("boom")
    assert err.value.code == "library_error" and "boom" in err.value.why
    with pytest.raises(StructuredToolError) as err, service_mod._kicad_errors("Could not read X"):
        raise LibrariesLoading("slow")
    assert err.value.code == "libraries_loading"


class FootprintSession(FakeSession):
    """Two footprint libraries; `Bad` fails to load."""

    def __init__(self, failing: Exception | None) -> None:
        super().__init__()
        self.project = type("P", (), {"pcb_file": "x.kicad_pcb"})()
        self.failing = failing
        self.loads: list[str] = []

    async def call(self, fn: Any) -> Any:
        func = getattr(fn, "func", None)
        if func is service_mod._footprint_names:
            nickname = fn.args[0]
            self.loads.append(nickname)
            if nickname == "Bad_SMD" and self.failing is not None:
                raise self.failing
            return ["R_0805_2012Metric"]
        if func is service_mod._fetch_footprints:
            from copperchat.library.packages import FootprintDetail

            nickname, names = fn.args
            return [
                FootprintDetail(f"{nickname}:{n}", "Resistor SMD 0805", 2, 2, "0805") for n in names
            ]
        return await super().call(fn)


@pytest.mark.parametrize("failing", ["api", "doc"])
async def test_failing_footprint_library_is_skipped(failing: str) -> None:
    from kipy.errors import ApiError

    from copperchat.errors import DocumentNotOpen

    error = ApiError("bad table row") if failing == "api" else DocumentNotOpen("no board")
    session = FootprintSession(error)
    svc = _svc(session)
    svc._fp_scopes = {"Bad_SMD": "global", "Good_SMD": "global"}
    rows, failed = await svc.search_footprints_detailed("smd 0805")
    assert [r["lib_id"] for r in rows] == ["Good_SMD:R_0805_2012Metric"]
    assert set(failed) == {"Bad_SMD"}
    # remembered: an unscoped search does not retry the broken library
    rows2, failed2 = await svc.search_footprints_detailed("smd 0805")
    assert rows2 == rows and set(failed2) == {"Bad_SMD"}
    assert session.loads.count("Bad_SMD") == 1
    # an explicit library request retries and raises
    with pytest.raises(StructuredToolError) as err:
        await svc.search_footprints("0805", library="Bad_SMD")
    assert err.value.code == "library_error"
    assert session.loads.count("Bad_SMD") == 2


async def test_failed_library_recovers_and_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    from kipy.errors import ApiError

    session = FootprintSession(ApiError("busy"))
    svc = _svc(session)
    svc._fp_scopes = {"Bad_SMD": "global", "Good_SMD": "global"}
    _, failed = await svc.search_footprints_detailed("smd 0805")
    assert set(failed) == {"Bad_SMD"}
    monkeypatch.setattr(service_mod, "FAILED_RETRY_S", -1.0)  # entry expired
    _, failed = await svc.search_footprints_detailed("smd 0805")
    assert set(failed) == {"Bad_SMD"} and session.loads.count("Bad_SMD") == 2
    session.failing = None  # type: ignore[assignment]
    monkeypatch.setattr(service_mod, "FAILED_RETRY_S", 300.0)
    rows = await svc.search_footprints("0805", library="Bad_SMD")  # explicit retry succeeds
    assert [r["lib_id"] for r in rows] == ["Bad_SMD:R_0805_2012Metric"]
    _, failed = await svc.search_footprints_detailed("smd 0805")
    assert failed == {}


class ProbeSession(FakeSession):
    """KiCad returns `Device:R` without items (a derived symbol whose parent it cannot
    resolve): get_symbol falls back to the probe placement."""

    def __init__(self) -> None:
        super().__init__()
        self.commits: list[tuple[str, bool]] = []

    def commit(self, message: str, *, dry_run: bool = False) -> Any:
        self.commits.append((message, dry_run))
        return contextlib.nullcontext()

    async def call(self, fn: Any) -> Any:
        from kipy.proto.schematic import schematic_types_pb2 as st

        name = getattr(getattr(fn, "func", None), "__name__", "")
        if name not in ("_fetch_symbol", "_place_probe"):
            return await super().call(fn)
        self.calls += 1
        sy = st.SchematicSymbol()
        sy.id.library_nickname, sy.id.entry_name = "Device", "R"
        if name == "_place_probe":
            for number in ("1", "2"):
                sy.items.add().item.Pack(st.SchematicPin(number=number, name="~"))
        return sy


async def test_item_less_symbol_gets_its_pins_from_a_dropped_probe() -> None:
    session = ProbeSession()
    svc = _svc(session)
    assert await svc.wait_ready(5)
    detail = await svc.get_symbol("Device:R")
    assert detail.pins_known and sorted(p.number for p in detail.pins) == ["1", "2"]
    assert session.commits == [("probe Device:R", True)]  # always a dropped commit
    [row] = [r for r in await svc.search_symbols("resistor") if r["lib_id"] == "Device:R"]
    assert row["pin_count"] == 2


async def test_item_less_symbol_on_read_only_has_unknown_pins() -> None:
    session = ProbeSession()
    session.read_only = True  # a probe is a (dropped) write: refused on read-only
    svc = _svc(session)
    assert await svc.wait_ready(5)
    detail = await svc.get_symbol("Device:R")
    assert detail.pins_known is False and detail.pins == []
    assert session.commits == []


# -- footprint access (F1 Task 4, FP-C1) -------------------------------------------------

FP_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "footprints"
SEED = "Inductor_SMD:L_Bourns-SRN4018"
REPO = "OpenDriveHub"
LOCAL_URI = "${KIPRJMOD}/../../footprints/OpenDriveHub.pretty"


def _fixture(name: str) -> Any:
    from kipy.proto.common.commands import editor_commands_pb2 as ec
    from kipy.util.proto import unpack_any

    response = ec.GetItemsResponse()
    response.ParseFromString((FP_FIXTURES / f"{name}.pb").read_bytes())
    return unpack_any(response.items[0])


class FakeDocument:
    def __init__(self, instance: Any) -> None:
        self._instance = instance

    async def footprint(self) -> Any:
        return self._instance


class LocalSession(FakeSession):
    """A project `<repo>/hw/main` of repo `OpenDriveHub`; the project table rows are
    `rows` (nickname -> raw URI); footprints are served from the recorded fixtures."""

    def __init__(self, root: Path, *, board: bool = False) -> None:
        super().__init__()
        self.root = root
        project_dir = root / "hw" / "main"
        project_dir.mkdir(parents=True, exist_ok=True)
        self.project = SimpleNamespace(
            dir=project_dir, pcb_file=project_dir / "main.kicad_pcb" if board else None
        )
        self.settings = SimpleNamespace(projects_root=root.parent, footprint_tools=True)
        self.generation = 1
        self.footprints_active = False
        self.rows: dict[str, str] = {}
        self.activations = 0
        self.documents: list[str] = []
        self.library_reads: list[tuple[str, str]] = []
        self.fp_lock_held = False

    def _substitute(self, uri: str) -> str:
        return uri.replace("${KIPRJMOD}", str(self.project.dir))

    async def activate_footprints(self) -> float | None:
        if self.footprints_active:
            return None
        self.activations += 1
        self.footprints_active = True
        return 2.5

    @contextlib.asynccontextmanager
    async def footprint_document(self, lib_id: str) -> Any:
        from kipy.errors import ApiError

        assert not self.fp_lock_held, "one footprint document at a time"
        self.documents.append(lib_id)
        if not lib_id.endswith((":L_Local", ":My FP")):
            raise ApiError(f"Footprint not found: {lib_id}")
        self.footprints_active = True
        self.fp_lock_held = True
        try:
            yield FakeDocument(_fixture("seed_srn4018.instance"))
        finally:
            self.fp_lock_held = False

    async def call(self, fn: Any) -> Any:
        func = getattr(fn, "func", fn)
        if func is service_mod._global_footprint_nicknames:
            return frozenset({"Device", "Inductor_SMD"})
        if func is service_mod._project_footprint_rows:
            return [(n, uri, self._substitute(uri)) for n, uri in self.rows.items()]
        if getattr(func, "__name__", "") == "run":  # _scopes(LT_FOOTPRINT)
            return {"Inductor_SMD": "global", **{n: "project" for n in self.rows}}
        if func is service_mod._fetch_footprint_proto:
            nickname, name = fn.args
            self.library_reads.append((nickname, name))
            if f"{nickname}:{name}" != SEED:
                return None
            return _fixture("seed_srn4018")
        return await super().call(fn)


def _policy(root: Path, project_dir: Path, transient: str = "") -> Any:
    from copperchat.repo_context.paths import RepoPolicy

    return RepoPolicy(root, project_dir, True, root.parent, transient=transient)


@pytest.fixture
def local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalSession:
    session = LocalSession(tmp_path / REPO)

    async def policy(project: Any, projects_root: Path) -> Any:
        return _policy(session.root, project.dir)

    monkeypatch.setattr(service_mod, "repo_policy", policy)
    return session


def _transient(monkeypatch: pytest.MonkeyPatch, session: LocalSession) -> None:
    async def policy(project: Any, projects_root: Path) -> Any:
        return _policy(project.dir, project.dir, transient="git timed out")

    monkeypatch.setattr(service_mod, "repo_policy", policy)


async def test_invalidate_clears_all(local: LocalSession) -> None:
    from copperchat.library.index import Index

    svc = _svc(local)

    def fill() -> None:
        svc._fp_scopes = {"X": "global", "Y": "global"}
        svc._fp_libs["X"] = service_mod._FootprintLibrary({}, Index())
        svc._fp_libs["Y"] = service_mod._FootprintLibrary({}, Index())
        svc._fp_failed["X"] = ("broken", 0.0)
        svc._fp_failed["Y"] = ("broken", 0.0)
        g = cast(Any, object())
        svc._fp_geometry["X:a"] = g
        svc._fp_geometry["Y:b"] = g

    fill()
    svc.invalidate_footprints("X")
    assert svc._fp_scopes is None
    assert set(svc._fp_libs) == {"Y"} and set(svc._fp_failed) == {"Y"}
    assert list(svc._fp_geometry) == ["Y:b"]
    fill()
    svc.invalidate_footprints(None)
    assert svc._fp_scopes is None
    assert not svc._fp_libs and not svc._fp_failed and not svc._fp_geometry


async def test_ready_board_or_active(local: LocalSession, tmp_path: Path) -> None:
    svc = _svc(local)
    assert svc.footprint_libraries_ready() is False
    local.footprints_active = True
    assert svc.footprint_libraries_ready() is True
    board = LocalSession(tmp_path / "board", board=True)
    assert _svc(board).footprint_libraries_ready() is True


async def test_local_names_on_disk(
    local: LocalSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _svc(local)
    info = await svc.local_library()
    assert info is not None
    lib = info.lib
    assert svc.local_names_on_disk(lib) == []  # directory missing
    lib.dir.mkdir(parents=True)
    for name in ("B.kicad_mod", "A.kicad_mod", "c.txt"):
        (lib.dir / name).write_bytes(b"opaque")  # never parsed: content irrelevant
    (lib.dir / "D.kicad_mod").symlink_to(lib.dir / "A.kicad_mod")
    (lib.dir / "E.kicad_mod").mkdir()

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("file content must not be read")

    monkeypatch.setattr(Path, "read_bytes", boom)
    monkeypatch.setattr(Path, "read_text", boom)
    monkeypatch.setattr(Path, "open", boom)
    monkeypatch.setattr("builtins.open", boom)
    names = svc.local_names_on_disk(lib)
    monkeypatch.undo()
    assert names == ["A", "B"]


async def test_local_library_info(local: LocalSession) -> None:
    svc = _svc(local)
    info = await svc.local_library()
    assert info is not None
    assert info.lib.nickname == REPO and info.lib.uri == LOCAL_URI
    assert info.lib.dir == local.root / "footprints" / f"{REPO}.pretty"
    assert info.lib.table == local.project.dir / "fp-lib-table"
    assert (info.row_present, info.row_conflict) == (False, None)
    local.rows = {REPO: LOCAL_URI}
    info = await svc.local_library()
    assert info is not None and (info.row_present, info.row_conflict) == (True, None)


async def test_for_write_symlinked_dir_refused(local: LocalSession, tmp_path: Path) -> None:
    from copperchat.library.service import local_library_dir_safe

    svc = _svc(local)
    info = await svc.local_library(for_write=True)  # nothing exists yet: fine
    assert info is not None
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / f"{REPO}.pretty").mkdir(parents=True)
    footprints = local.root / "footprints"

    async def refused() -> None:
        with pytest.raises(StructuredToolError) as err:
            await svc.local_library(for_write=True)
        assert err.value.code == "library_conflict"
        with pytest.raises(StructuredToolError):
            local_library_dir_safe(info.lib)
        assert await svc.local_library() is not None  # reads still work

    footprints.symlink_to(elsewhere)  # <root>/footprints is a symlink
    await refused()
    footprints.unlink()
    footprints.mkdir()
    (footprints / f"{REPO}.pretty").symlink_to(elsewhere / f"{REPO}.pretty")
    await refused()
    (footprints / f"{REPO}.pretty").unlink()
    (footprints / f"{REPO}.pretty").write_bytes(b"")  # a regular file, not a directory
    await refused()
    (footprints / f"{REPO}.pretty").unlink()
    (footprints / f"{REPO}.pretty").mkdir()
    assert await svc.local_library(for_write=True) is not None
    (elsewhere / "fp-lib-table").write_bytes(b"")
    info.lib.table.symlink_to(elsewhere / "fp-lib-table")  # the project table a symlink
    await refused()


async def test_local_footprint_file_safe(local: LocalSession) -> None:
    from copperchat.library.service import local_footprint_file_safe

    info = await _svc(local).local_library()
    assert info is not None
    lib = info.lib
    local_footprint_file_safe(lib, "L1")  # nothing there yet
    lib.dir.mkdir(parents=True)
    (lib.dir / "L1.kicad_mod").write_bytes(b"")
    local_footprint_file_safe(lib, "L1")  # a regular file
    (lib.dir / "L2.kicad_mod").symlink_to(lib.dir / "L1.kicad_mod")
    (lib.dir / "L3.kicad_mod").mkdir()
    for name in ("L2", "L3"):
        with pytest.raises(StructuredToolError) as err:
            local_footprint_file_safe(lib, name)
        assert err.value.code == "library_conflict"
    with pytest.raises(StructuredToolError) as err:
        local_footprint_file_safe(lib, "../x")
    assert err.value.code == "invalid_argument"


async def test_local_library_transient_returns_none(
    local: LocalSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _transient(monkeypatch, local)
    assert await _svc(local).local_library() is None


async def test_local_library_for_write_transient_raises(
    local: LocalSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _transient(monkeypatch, local)
    with pytest.raises(StructuredToolError) as err:
        await _svc(local).local_library(for_write=True)
    assert err.value.code == "repo_root_unknown"


async def test_row_conflict(local: LocalSession) -> None:
    svc = _svc(local)
    local.rows = {REPO: "${KIPRJMOD}/vendor/other.pretty"}
    info = await svc.local_library()
    assert info is not None and not info.row_present
    assert info.row_conflict is not None
    assert "${KIPRJMOD}/vendor/other.pretty" in info.row_conflict
    assert LOCAL_URI in info.row_conflict
    local.rows = {"MyParts": LOCAL_URI}  # another nickname at the local directory
    info = await svc.local_library()
    assert info is not None and not info.row_present
    assert info.row_conflict is not None
    assert "MyParts" in info.row_conflict
    assert "rename" in info.row_conflict and "remove" in info.row_conflict
    local.rows = {"Rel": "../../footprints/OpenDriveHub.pretty"}  # relative to the table dir
    info = await svc.local_library()
    assert info is not None and info.row_conflict is not None and "Rel" in info.row_conflict
    local.rows = {REPO: LOCAL_URI, "Vendor": "${KIPRJMOD}/vendor/v.pretty"}
    info = await svc.local_library()
    assert info is not None and (info.row_present, info.row_conflict) == (True, None)


# -- footprint_geometry ------------------------------------------------------------------


async def test_geometry_global_boardless_activates_and_flags_incomplete(
    local: LocalSession,
) -> None:
    svc = _svc(local)
    notes: list[dict[str, Any]] = []
    g = await svc.footprint_geometry(SEED, notes)
    assert g is not None and g.scope == "global" and g.mounting == "unspecified"
    assert local.activations == 1 and local.library_reads == [("Inductor_SMD", "L_Bourns-SRN4018")]
    assert local.documents == []  # stock footprints are not opened as documents
    kinds = [n["kind"] for n in notes]
    assert kinds == ["footprint_libraries_loaded", "footprint_attributes_unavailable"]
    assert notes[0]["seconds"] == 2.5
    # cached (LRU): no second read, no second activation note, still flagged incomplete
    notes2: list[dict[str, Any]] = []
    assert await svc.footprint_geometry(SEED, notes2) is g
    assert local.library_reads == [("Inductor_SMD", "L_Bourns-SRN4018")]
    assert [n["kind"] for n in notes2] == ["footprint_attributes_unavailable"]
    svc.invalidate_footprints("Inductor_SMD")
    assert await svc.footprint_geometry(SEED) is not None
    assert len(local.library_reads) == 2


async def test_geometry_board_server_does_not_activate(tmp_path: Path) -> None:
    session = LocalSession(tmp_path / REPO, board=True)
    svc = _svc(session)
    notes: list[dict[str, Any]] = []
    assert await svc.footprint_geometry(SEED, notes) is not None
    assert session.activations == 0
    assert [n["kind"] for n in notes] == ["footprint_attributes_unavailable"]


async def test_geometry_unknown_and_invalid(local: LocalSession) -> None:
    svc = _svc(local)
    assert await svc.footprint_geometry("Inductor_SMD:Nope") is None
    assert await svc.footprint_geometry("NoSuchLib:X") is None
    with pytest.raises(StructuredToolError) as err:
        await svc.footprint_geometry("no-colon")
    assert err.value.code == "invalid_argument"


async def test_geometry_local_reads_document(local: LocalSession, tmp_path: Path) -> None:
    from copperchat.kicad_tools.footprints.model import geometry_from_proto, geometry_hash

    local.rows = {REPO: LOCAL_URI}
    info = await _svc(local).local_library()
    assert info is not None
    info.lib.dir.mkdir(parents=True)
    (info.lib.dir / "L_Local.kicad_mod").write_bytes(b"")
    for board in (False, True):
        session = local
        if board:  # a board server activates too: the document open loads every library
            session = LocalSession(local.root, board=True)
            session.rows = dict(local.rows)
        svc = _svc(session)
        notes: list[dict[str, Any]] = []
        g = await svc.footprint_geometry(f"{REPO}:L_Local", notes)
        assert g is not None and g.scope == "local" and g.mounting == "smd"
        assert session.documents == [f"{REPO}:L_Local"] and session.library_reads == []
        assert [n["kind"] for n in notes] == ["footprint_libraries_loaded"]
        instance = _fixture("seed_srn4018.instance")
        expected = geometry_from_proto(instance, instance.definition.items, "x", "local")
        assert geometry_hash(g) == geometry_hash(expected)
        notes.clear()
        assert await svc.footprint_geometry(f"{REPO}:L_Local", notes) is g  # cached
        assert notes == []
    # not on disk: no document is opened
    assert await _svc(local).footprint_geometry(f"{REPO}:Missing") is None
    assert local.documents == [f"{REPO}:L_Local"]


async def test_geometry_local_gui_name_readable_unsafe_name_not(local: LocalSession) -> None:
    local.rows = {REPO: LOCAL_URI}
    svc = _svc(local)
    info = await svc.local_library()
    assert info is not None
    info.lib.dir.mkdir(parents=True)
    (info.lib.dir / "My FP.kicad_mod").write_bytes(b"")  # a name the KiCad GUI allows
    g = await svc.footprint_geometry(f"{REPO}:My FP")
    assert g is not None and g.scope == "local"
    assert local.documents == [f"{REPO}:My FP"]
    for name in ("../My FP", "sub/x", "a\\b", "..", "a\0b"):
        assert await svc.footprint_geometry(f"{REPO}:{name}") is None
    assert local.documents == [f"{REPO}:My FP"]


async def test_geometry_local_without_row_is_not_found(local: LocalSession) -> None:
    info = await _svc(local).local_library()
    assert info is not None
    info.lib.dir.mkdir(parents=True)
    (info.lib.dir / "L_Local.kicad_mod").write_bytes(b"")
    assert await _svc(local).footprint_geometry(f"{REPO}:L_Local") is None
    assert local.documents == []


async def test_geometry_local_cache_revalidated(local: LocalSession) -> None:
    local.rows = {REPO: LOCAL_URI}
    svc = _svc(local)
    info = await svc.local_library()
    assert info is not None
    info.lib.dir.mkdir(parents=True)
    file = info.lib.dir / "L_Local.kicad_mod"
    file.write_bytes(b"v1")
    lib_id = f"{REPO}:L_Local"
    g = await svc.footprint_geometry(lib_id)
    assert g is not None and len(local.documents) == 1
    assert await svc.footprint_geometry(lib_id) is g  # unchanged file: cached
    assert len(local.documents) == 1
    file.write_bytes(b"version 2")  # rewritten (size and mtime change)
    g2 = await svc.footprint_geometry(lib_id)
    assert g2 is not None and g2 is not g and len(local.documents) == 2
    file.unlink()  # removed: the stale entry is dropped and the read finds nothing
    assert await svc.footprint_geometry(lib_id) is None
    assert len(local.documents) == 2 and lib_id not in svc._fp_geometry


async def test_geometry_transient_repo_root(
    local: LocalSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _transient(monkeypatch, local)
    svc = _svc(local)
    for lib_id in (f"{REPO}:L_Local", "main:L_Local", "Vendor:X"):  # maybe the local library
        with pytest.raises(StructuredToolError) as err:
            await svc.footprint_geometry(lib_id)
        assert err.value.code == "repo_root_unknown"
    assert local.documents == [] and local.library_reads == []
    notes: list[dict[str, Any]] = []
    g = await svc.footprint_geometry(SEED, notes)  # a global library still reads
    assert g is not None and g.scope == "global"
    assert "footprint_attributes_unavailable" in [n["kind"] for n in notes]
    assert svc._fp_geometry == {}  # nothing read while transient is cached


@pytest.mark.parametrize("changed", [False, True])
async def test_geometry_cache_entry_dropped_during_revalidation(
    local: LocalSession, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    """An invalidate (or LRU eviction) while the hit's lstat runs: no KeyError, re-read."""
    local.rows = {REPO: LOCAL_URI}
    svc = _svc(local)
    info = await svc.local_library()
    assert info is not None
    info.lib.dir.mkdir(parents=True)
    (info.lib.dir / "L_Local.kicad_mod").write_bytes(b"v1")
    lib_id = f"{REPO}:L_Local"
    g = await svc.footprint_geometry(lib_id)
    assert g is not None and len(local.documents) == 1
    real = service_mod._file_validator

    def racing(path: Path) -> Any:
        svc.invalidate_footprints()  # concurrent invalidate while the lstat runs
        result = real(path)
        return (0, 0, 0) if changed else result

    monkeypatch.setattr(service_mod, "_file_validator", racing)
    g2 = await svc.footprint_geometry(lib_id)
    assert g2 is not None and g2 is not g and len(local.documents) == 2
