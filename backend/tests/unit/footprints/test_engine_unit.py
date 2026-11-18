"""F4a Task 1: write-path types (FP-C8), the schematic-engine guard, the budget gates on
footprint change sets, the history library locator / listener wiring (F1 <-> F3) and the
footprint-tool profiles."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.agent.profiles import FP_READ, FP_WRITES, PROFILES, profile_specs
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.editing.gate import BudgetGate
from copperchat.editing.service import EditingService
from copperchat.errors import DocumentInconsistent, StructuredToolError
from copperchat.history.store import LibraryLocation
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical, NetIntent
from copperchat.kicad_tools.edit.engine import apply_change_set
from copperchat.kicad_tools.footprints import engine as fp_engine
from copperchat.kicad_tools.footprints.engine import (
    NO_SHEET,
    FootprintChangeSet,
    FootprintPreImage,
    FpStage,
    LibraryRowStage,
    make_library_listener,
    make_library_locator,
)
from copperchat.library import service as service_mod
from copperchat.library.local import derive_local_library
from copperchat.plans.gate import PlanGate
from copperchat.projects import Project
from copperchat.repo_context.paths import RepoPolicy
from copperchat.tools import ToolContext, ToolResult, registry

PID = "a1b2c3d4e5f6"
REPO = "OpenDriveHub"


def _project(root: Path, pid: str = PID) -> Project:
    d = root / REPO / "hw" / "main"
    d.mkdir(parents=True, exist_ok=True)
    return Project(
        pid, "main", f"{REPO}/hw/main/main.kicad_pro", d, d / "main.kicad_pro", None, None
    )


def _fp_cs(changes: int = 5) -> FootprintChangeSet:
    return FootprintChangeSet(tool="fp_create", logical=Logical(changes))


# -- types ------------------------------------------------------------------------------


def test_footprint_change_set_defaults() -> None:
    cs = _fp_cs()
    assert isinstance(cs, ChangeSet)
    assert cs.sheet is NO_SHEET and cs.sheet.name == "<footprint>"
    assert cs.stages == [] and cs.pre_images == {} and cs.intent == NetIntent()
    assert cs.fp_stages == [] and cs.target is None and cs.pre_image is None
    assert cs.library is None and cs.library_row is None
    assert cs.fail_after is None and cs.kill_after is None and not cs.fail_compensation
    assert cs.logical.changes == 5
    # the base field defaults to None for every schematic change set too
    plain = ChangeSet("sch_fake", NO_SHEET, [], {}, NetIntent(), Logical(1))
    assert plain.library_row is None


def test_stage_and_pre_image_types(tmp_path: Path) -> None:
    lib = derive_local_library(tmp_path / "hw" / "main", tmp_path, frozenset())
    row = LibraryRowStage(library=lib, create_library=True)
    assert row.library.nickname == tmp_path.name
    stage = FpStage(kind="copy_seed")
    assert stage.kind == "copy_seed"
    pre = FootprintPreImage(items=[b"a"], definition=b"d", file_sha256="", geometry_hash="")
    assert pre.items == [b"a"] and pre.definition == b"d"
    cs = FootprintChangeSet(
        tool="fp_create", logical=Logical(5), fp_stages=[stage], library=lib, pre_image=pre
    )
    assert cs.fp_stages == [stage] and cs.library is lib


def test_document_inconsistent_defaults() -> None:
    plain = DocumentInconsistent("x")
    assert plain.leftovers == [] and plain.footprint_only is False
    assert not plain.last_save_partial
    other = DocumentInconsistent("y")
    plain.leftovers.append("z")
    assert other.leftovers == []  # no shared default list
    fp = DocumentInconsistent("fp", leftovers=["L:A", "row L"], footprint_only=True)
    assert fp.leftovers == ["L:A", "row L"] and fp.footprint_only


async def test_apply_change_set_refuses_footprint_change_set() -> None:
    class Untouchable:
        def applying(self) -> Any:
            raise AssertionError("the schematic engine must not touch the session")

    ctx = ToolContext(
        project=cast(Any, None), session=cast(Any, Untouchable()), settings=cast(Any, None)
    )
    with pytest.raises(TypeError, match="FootprintChangeSet"):
        await apply_change_set(ctx, _fp_cs())


# -- budget gates -------------------------------------------------------------------------


@pytest.fixture
async def editing(tmp_path: Path) -> AsyncIterator[EditingService]:
    db = await Database.open(tmp_path / "db.sqlite")
    yield EditingService(db=db, broadcaster=None, settings_repo=EditingSettingsRepo(db))
    await db.close()


def _gate_ctx(tmp_path: Path, editing: EditingService) -> ToolContext:
    return ToolContext(
        project=_project(tmp_path),
        session=cast(Any, None),
        settings=cast(Any, None),
        run_id="run1",
        editing=editing,
    )


@pytest.mark.parametrize("kind", ["budget", "plan"])
async def test_gates_accept_footprint_change_set(
    tmp_path: Path, editing: EditingService, kind: str
) -> None:
    gate = BudgetGate(editing) if kind == "budget" else PlanGate(editing, cast(Any, None))
    ctx = _gate_ctx(tmp_path, editing)
    cs = _fp_cs(5)
    await gate.check_budget(ctx, cs)  # 5 of 20: accepted
    cs.applied = cast(Any, object())
    await gate.record(ctx, cs)
    assert editing.turn(PID, "run1").logical_used == 5
    for _ in range(3):  # 20 of 20
        await gate.record(ctx, cs)
    with pytest.raises(StructuredToolError) as err:
        await gate.check_budget(ctx, _fp_cs(5))
    assert err.value.code == "plan_required"


# -- the history library locator ------------------------------------------------------------


class _Session:
    """The open session of one project (only what the locator/listener read)."""

    def __init__(self, project: Project, settings: Settings, *, board: bool = False) -> None:
        self.project = SimpleNamespace(
            id=project.id, dir=project.dir, pcb_file=project.dir / "x.kicad_pcb" if board else None
        )
        self.settings = settings
        self.closed = False
        self.generation = 1
        self.footprints_active = False
        self.calls: list[str] = []
        self.reload_error: Exception | None = None

    async def call(self, fn: Any, *, timeout: float | None = None) -> Any:
        func = getattr(fn, "func", fn)
        self.calls.append(func.__name__)
        if func is service_mod._global_footprint_nicknames:
            return frozenset({REPO, "Device"})
        if func is fp_engine._reload_footprint_library:
            if self.reload_error is not None:
                raise self.reload_error
            return None
        raise AssertionError(f"unexpected call {fn!r}")


class _Sessions:
    def __init__(self, session: _Session | None = None) -> None:
        self.session = session

    def current(self) -> Any:
        return self.session


@pytest.fixture
def git_root(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """repo_policy -> the repo `<projects_root>/OpenDriveHub`; `state["transient"]`
    switches to the transient fallback."""
    state = {"transient": ""}

    async def policy(project: Any, projects_root: Path) -> RepoPolicy:
        if state["transient"]:
            return RepoPolicy(project.dir, project.dir, False, projects_root, transient="git")
        return RepoPolicy(projects_root / REPO, project.dir, True, projects_root)

    monkeypatch.setattr(fp_engine, "repo_policy", policy)
    return state


@pytest.fixture
def stock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stock footprint dir (the api-server env's KICAD<n>_FOOTPRINT_DIR)."""
    d = tmp_path / "stock"
    for name in ("Inductor_SMD", "Resistor_SMD"):
        (d / f"{name}.pretty").mkdir(parents=True)
    (d / "README.md").write_text("not a library")
    monkeypatch.setenv("KICAD10_FOOTPRINT_DIR", str(d))
    return d


async def test_locator_none_while_transient(
    make_settings: Callable[..., Settings], git_root: dict[str, str], stock: Path
) -> None:
    settings = make_settings()
    project = _project(settings.projects_root)
    locate = make_library_locator(settings, cast(Any, _Sessions()))
    git_root["transient"] = "git timed out"
    assert await locate(project) is None
    git_root["transient"] = ""
    assert await locate(project) is not None


async def test_locator_for_project_not_open(
    make_settings: Callable[..., Settings], git_root: dict[str, str], stock: Path
) -> None:
    settings = make_settings()
    project = _project(settings.projects_root)
    other = _Session(_project(settings.projects_root, pid="ffffffffffff"), settings)
    for sessions in (_Sessions(), _Sessions(other)):  # none open / another project open
        location = await make_library_locator(settings, cast(Any, sessions))(project)
        root = settings.projects_root / REPO
        assert location == LibraryLocation(
            dir=root / "footprints" / f"{REPO}.pretty",
            repo_root=root,
            table=project.dir / "fp-lib-table",
            nickname=REPO,
        )
    assert other.calls == []  # another project's server is never asked


async def test_locator_stock_collision_appends_local(
    make_settings: Callable[..., Settings], git_root: dict[str, str], stock: Path
) -> None:
    (stock / f"{REPO}.pretty").mkdir()
    settings = make_settings()
    project = _project(settings.projects_root)
    location = await make_library_locator(settings, cast(Any, _Sessions()))(project)
    assert location is not None and location.nickname == f"{REPO}_local"


async def test_locator_uses_the_open_sessions_global_table(
    make_settings: Callable[..., Settings], git_root: dict[str, str], stock: Path
) -> None:
    settings = make_settings()
    project = _project(settings.projects_root)
    session = _Session(project, settings)  # its global table names REPO
    location = await make_library_locator(settings, cast(Any, _Sessions(session)))(project)
    assert location is not None and location.nickname == f"{REPO}_local"
    assert session.calls == ["_global_footprint_nicknames"]


def test_stock_dir_falls_back_to_the_kicad_install(
    tmp_path: Path, make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in [k for k in os.environ if k.endswith("_FOOTPRINT_DIR")]:
        monkeypatch.delenv(key)
    share = tmp_path / "usr" / "share" / "kicad"
    (share / "template").mkdir(parents=True)
    (share / "footprints" / "Device.pretty").mkdir(parents=True)
    cli = tmp_path / "usr" / "bin" / "kicad-cli"
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    cli.chmod(0o755)
    settings = make_settings(kicad_cli=str(cli))
    assert fp_engine.stock_footprint_nicknames(settings) == frozenset({"Device"})
    assert fp_engine.stock_footprint_nicknames(make_settings()) == frozenset()


# -- the library listener -------------------------------------------------------------------


@pytest.fixture
def invalidated(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    calls: list[str | None] = []

    def record(self: Any, nickname: str | None = None) -> None:
        calls.append(nickname)

    monkeypatch.setattr(service_mod.LibraryService, "invalidate_footprints", record)
    return calls


async def _listen(
    settings: Settings, session: _Session | None, project: Project, reason: str
) -> None:
    listener = make_library_listener(settings, cast(Any, _Sessions(session)))
    await listener(project, [f"../../footprints/{REPO}.pretty/A.kicad_mod"], reason)


@pytest.mark.parametrize("reason", ["own", "restore"])
async def test_listener_own_and_restore_invalidate(
    make_settings: Callable[..., Settings],
    git_root: dict[str, str],
    stock: Path,
    invalidated: list[str | None],
    reason: str,
) -> None:
    settings = make_settings()
    project = _project(settings.projects_root)
    session = _Session(project, settings, board=True)
    await _listen(settings, session, project, reason)
    assert invalidated == [f"{REPO}_local"]
    assert "_reload_footprint_library" not in session.calls


async def test_listener_external_tools_off_no_reload(
    make_settings: Callable[..., Settings],
    git_root: dict[str, str],
    stock: Path,
    invalidated: list[str | None],
) -> None:
    settings = make_settings(footprint_tools=False)
    project = _project(settings.projects_root)
    session = _Session(project, settings, board=True)  # libraries ready, but tools off
    await _listen(settings, session, project, "external")
    assert "_reload_footprint_library" not in session.calls
    assert invalidated == [f"{REPO}_local"]


async def test_listener_external_not_ready_no_reload(
    make_settings: Callable[..., Settings],
    git_root: dict[str, str],
    stock: Path,
    invalidated: list[str | None],
) -> None:
    settings = make_settings()
    project = _project(settings.projects_root)
    session = _Session(project, settings)  # boardless, never activated
    await _listen(settings, session, project, "external")
    assert "_reload_footprint_library" not in session.calls
    assert invalidated == [f"{REPO}_local"]


async def test_listener_external_reloads_then_invalidates(
    make_settings: Callable[..., Settings],
    git_root: dict[str, str],
    stock: Path,
    invalidated: list[str | None],
) -> None:
    settings = make_settings()
    project = _project(settings.projects_root)
    session = _Session(project, settings)
    session.footprints_active = True
    await _listen(settings, session, project, "external")
    assert session.calls[-1] == "_reload_footprint_library"
    assert invalidated == [f"{REPO}_local"]
    # a failing reload still invalidates (and is raised to notify_library, which logs)
    invalidated.clear()
    session.reload_error = RuntimeError("no such row")
    with pytest.raises(RuntimeError):
        await _listen(settings, session, project, "external")
    assert invalidated == [f"{REPO}_local"]


async def test_listener_without_open_session_is_a_no_op(
    make_settings: Callable[..., Settings],
    git_root: dict[str, str],
    stock: Path,
    invalidated: list[str | None],
) -> None:
    settings = make_settings()
    project = _project(settings.projects_root)
    await _listen(settings, None, project, "external")
    assert invalidated == []


async def test_listener_transient_invalidates_everything(
    make_settings: Callable[..., Settings],
    git_root: dict[str, str],
    stock: Path,
    invalidated: list[str | None],
) -> None:
    settings = make_settings()
    project = _project(settings.projects_root)
    git_root["transient"] = "git timed out"
    await _listen(settings, _Session(project, settings), project, "own")
    assert invalidated == [None]


class _ReloadKiCad:
    """Records the reload requests of `_reload_footprint_library`."""

    def __init__(self, rows: list[str]) -> None:
        self.rows = rows
        self.seen: list[tuple[str, Any]] = []

    def get_library_table(self, type: Any, scope: Any, substituted: bool = False) -> Any:
        rows = [SimpleNamespace(nickname=n, uri=f"${{KIPRJMOD}}/{n}.pretty") for n in self.rows]
        return SimpleNamespace(rows=rows)

    def reload_library(self, type: Any, scope: Any, nicknames: Any) -> None:
        self.seen.append(("reload", (type, scope, list(nicknames))))

    def wait_for_libraries(self, types: Any = None, **kw: Any) -> list[Any]:
        self.seen.append(("wait", types))
        return []


def test_reload_handle_function_reloads_and_waits() -> None:
    kicad = _ReloadKiCad(["Lib"])
    fp_engine._reload_footprint_library("Lib", cast(Any, SimpleNamespace(kicad=kicad)))
    assert [s[0] for s in kicad.seen] == ["reload", "wait"]
    assert kicad.seen[0][1][2] == ["Lib"]


def test_reload_skipped_without_the_project_row() -> None:
    """Parked T1 minor (a): an external change in a project without the local row must
    not ask KiCad to reload a nickname it does not know (warning noise)."""
    kicad = _ReloadKiCad(["Other"])
    fp_engine._reload_footprint_library("Lib", cast(Any, SimpleNamespace(kicad=kicad)))
    assert kicad.seen == []


# -- wiring --------------------------------------------------------------------------------


async def test_install_history_wires_locator_and_listener(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    from copperchat.history.service import _no_library, install_history

    db = await Database.open(tmp_path / "db.sqlite")
    try:

        class Manager:
            def add_save_listener(self, listener: Any) -> None:
                pass

        runtime = SimpleNamespace(broadcaster=None, hooks_factory=None)
        state = SimpleNamespace(
            agent_runtime=runtime, db=db, sessions=Manager(), settings=make_settings()
        )
        svc = install_history(cast(Any, SimpleNamespace(state=state)))
        assert svc.locate_library is not _no_library
        assert len(svc.library_listeners) == 1
    finally:
        await db.close()


# -- profiles ------------------------------------------------------------------------------


def test_fp_writes_in_schematic_and_free_only() -> None:
    assert "fp_create" in FP_WRITES  # registered with the write engine (F4a Task 3)
    assert {s.name for s in registry.all_specs()} >= FP_WRITES
    assert not FP_WRITES & FP_READ
    assert all(n.startswith("fp_") for n in FP_WRITES)
    for kind in ("schematic", "free"):
        assert PROFILES[kind] >= FP_WRITES, kind
    for kind in ("requirements", "board"):
        assert not FP_WRITES & PROFILES[kind], kind


def test_tools_off_no_fp_tool_in_any_profile() -> None:
    specs = list(registry.all_specs())
    fp_names = {s.name for s in specs if s.name.startswith("fp_")}
    assert {"fp_create"} | set(FP_READ) | set(FP_WRITES) <= fp_names
    for kind in PROFILES:
        on = {s.name for s in profile_specs(kind, specs, footprint_tools=True)}
        off = {s.name for s in profile_specs(kind, specs, footprint_tools=False)}
        assert not {n for n in off if n.startswith("fp_")}, kind
        assert off == {n for n in on if not n.startswith("fp_")}, kind
    free_on = {s.name for s in profile_specs("free", specs)}
    assert fp_names <= free_on


# -- the staged write engine (F4a Task 3) -----------------------------------------------------


class _FakeKiCad:
    """Library commands of an api-server (fake KiCad of the unit tests: it writes the
    library directory and footprint files into tmp_path as KiCad would)."""

    def __init__(self, lib: Any) -> None:
        self.lib = lib
        self.rows: dict[str, str] = {}
        self.calls: list[str] = []

    def get_library_table(self, type: Any, scope: Any, substituted: bool = False) -> Any:
        return SimpleNamespace(
            rows=[SimpleNamespace(nickname=n, uri=u) for n, u in self.rows.items()]
        )

    def add_library_table_entry(
        self, type: Any, scope: Any, nickname: str, uri: str, *, description: str = "",
        create_library: bool = False,
    ) -> None:  # fmt: skip
        from copperchat.history.library_lock import assert_library_locked

        assert_library_locked(self.lib.dir)  # S1 runs inside the adapter's library lock
        self.calls.append("add_row")
        self.rows[nickname] = uri
        if create_library:
            self.lib.dir.mkdir(parents=True, exist_ok=True)

    def wait_for_libraries(self, types: Any = None, **kw: Any) -> list[Any]:
        from kipy.proto.common.types.library_types_pb2 import LibraryLoadStatus

        loaded = LibraryLoadStatus.LLS_LOADED
        return [SimpleNamespace(nickname=n, status=loaded, error_message=None) for n in self.rows]

    def get_library_footprints(self, nicknames: Any) -> list[Any]:
        files = sorted(self.lib.dir.glob("*.kicad_mod")) if self.lib.dir.is_dir() else []
        return [SimpleNamespace(proto=SimpleNamespace(entry_name=f.stem)) for f in files]

    def delete_library_item(self, type: Any, nickname: str, name: str) -> None:
        self.calls.append("delete_item")
        (self.lib.dir / f"{name}.kicad_mod").unlink()

    def delete_library_table_entry(self, type: Any, scope: Any, nickname: str) -> None:
        self.calls.append("delete_row")
        del self.rows[nickname]


class _FakeDoc:
    def __init__(self, session: _EngineSession, lib_id: str) -> None:
        self.session, self.lib_id = session, lib_id

    async def save_copy(self, target: str) -> None:
        name = target.split(":", 1)[1]
        (self.session.kicad.lib.dir / f"{name}.kicad_mod").write_text("(footprint seed copy)")

    async def begin(self) -> None:
        raise self.session.rebuild_error


class _EngineSession:
    """The KiCad session as the engine uses it (applying, call, footprint_document)."""

    def __init__(self, project: Project, settings: Settings, lib: Any) -> None:
        import asyncio

        self.project, self.settings = project, settings
        self.kicad = _FakeKiCad(lib)
        self.generation = 1
        self.process: Any = None
        self.closed = False
        self.footprints_active = True
        self.rebuild_error: BaseException = RuntimeError("no rebuild in this fake")
        self._apply = asyncio.Lock()

    @property
    def server_alive(self) -> bool:
        return self.process is None or self.process.returncode is None

    async def call(self, fn: Any, *, timeout: float | None = None) -> Any:
        from copperchat.kicad_session import KicadUnavailable

        if not self.server_alive:
            self.generation += 1  # the real session restarts the server and raises
            self.process = None
            raise KicadUnavailable("the KiCad api-server had stopped")
        return fn(SimpleNamespace(kicad=self.kicad))

    def applying(self) -> Any:
        return self._apply

    def footprint_document(self, lib_id: str) -> Any:
        import contextlib

        @contextlib.asynccontextmanager
        async def doc() -> AsyncIterator[_FakeDoc]:
            yield _FakeDoc(self, lib_id)

        return doc()


def _engine_setup(
    tmp_path: Path, settings: Settings, editing: EditingService
) -> tuple[ToolContext, _EngineSession, FootprintChangeSet]:
    from copperchat.kicad_tools.footprints.build import build_target, parse_create_args
    from tests.unit.footprints.test_build import l201_args

    project = _project(settings.projects_root)
    lib = derive_local_library(project.dir, settings.projects_root / REPO, frozenset())
    session = _EngineSession(project, settings, lib)
    ctx = ToolContext(
        project=project, session=cast(Any, session), settings=settings, run_id="run1",
        editing=editing,
    )  # fmt: skip
    target = build_target(parse_create_args(l201_args()), lib)
    cs = FootprintChangeSet(
        tool="fp_create",
        logical=Logical(5),
        fp_stages=[
            FpStage("ensure_library"),
            FpStage("copy_seed", target.lib_id),
            FpStage("rebuild", target.lib_id),
        ],
        target=target,
        library=lib,
    )
    return ctx, session, cs


async def _journal(editing: EditingService) -> list[Any]:
    from copperchat.db.repos.footprints import FootprintWriteRepo

    return await FootprintWriteRepo(editing.db).after_id(PID, 0)


async def _apply_locked(ctx: ToolContext, cs: FootprintChangeSet) -> Any:
    from copperchat.history.library_lock import library_lock

    assert cs.library is not None
    async with library_lock(cs.library.dir):
        return await fp_engine.apply_footprint_change_set(ctx, cs)


async def test_kicad_unavailable_skips_compensation(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    """Spec §9.3 server death: KicadUnavailable in S3 is re-raised without compensation -
    no DeleteLibraryItem, the row stays, the create journal row stays `pending` with
    the seed copy's hash (Task 4's cleanup decides later)."""
    from copperchat.kicad_session import KicadUnavailable

    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    session.rebuild_error = KicadUnavailable("api-server died in S3")
    with pytest.raises(KicadUnavailable, match="died in S3"):
        await _apply_locked(ctx, cs)
    assert session.kicad.calls == ["add_row"]
    assert cs.library is not None and (cs.library.dir / "L_Bourns_SRN3012BTA.kicad_mod").exists()
    rows = await _journal(editing)
    assert [(r.op, r.state) for r in rows] == [("row_add", "done"), ("create", "pending")]
    assert rows[1].post_s2_sha256 not in (None, "absent")
    assert rows[1].pre_sha256 == "absent"


async def test_failure_in_s3_compensates_in_reverse(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    with pytest.raises(StructuredToolError) as err:
        await _apply_locked(ctx, cs)
    assert err.value.code == "library_error"
    assert session.kicad.calls == ["add_row", "delete_item", "delete_row"]
    assert cs.library is not None and not cs.library.dir.exists()  # created here: rmdir
    rows = await _journal(editing)
    assert [(r.op, r.state) for r in rows] == [("row_add", "compensated"),
                                                ("create", "compensated")]  # fmt: skip
    assert {cs.library.table, cs.library.dir / "L_Bourns_SRN3012BTA.kicad_mod"} <= set(
        ctx.library_touched
    )


async def test_kill_after_skips_compensation(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    """`kill_after` SIGKILLs the api-server process after the stage; the generation
    check then raises KicadUnavailable and nothing is compensated."""
    import asyncio

    from copperchat.kicad_session import KicadUnavailable

    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    session.process = await asyncio.create_subprocess_exec("sleep", "60")
    cs.kill_after = "copy_seed"
    with pytest.raises(KicadUnavailable):
        await _apply_locked(ctx, cs)
    assert session.process.returncode is not None
    assert session.kicad.calls == ["add_row"]
    rows = await _journal(editing)
    assert [(r.op, r.state) for r in rows] == [("row_add", "done"), ("create", "pending")]


async def test_library_lock_taken_once(
    make_settings: Callable[..., Settings], editing: EditingService
) -> None:
    """The adapter runs the turn snapshot (which takes the library lock itself) first,
    then takes the lock once around apply: S1 completes without a deadlock and
    `assert_library_locked` holds inside it (the fake KiCad asserts it)."""
    import asyncio

    from copperchat.agent.sdk_tools import _two_phase_steps
    from copperchat.history.library_lock import library_lock

    settings = make_settings()
    ctx, session, cs = _engine_setup(settings.projects_root, settings, editing)
    assert cs.library is not None
    lib = cs.library
    snapshots: list[bool] = []

    async def snapshot() -> None:
        async with library_lock(lib.dir):  # as HistoryService's turn snapshot does
            snapshots.append(True)

    async def prepare(_ctx: ToolContext, _args: dict[str, Any]) -> Any:
        return cs

    async def apply(c: ToolContext, _cs: Any) -> ToolResult:
        added = await fp_engine.run_library_row(c, LibraryRowStage(lib, create_library=True))
        return ToolResult(data={"added": added}, library_changed={lib.nickname})

    from copperchat.tools import edit_spec

    ctx.on_mutation = snapshot
    ctx.editing = None
    spec = edit_spec("fp_fake", "d", {"type": "object"}, prepare, apply)
    result = await asyncio.wait_for(_two_phase_steps(spec, ctx, {}), timeout=5)
    assert result.data == {"added": True} and snapshots == [True]
    assert not library_lock(lib.dir).locked()
    assert session.kicad.rows == {lib.nickname: lib.uri}
    # without the adapter's lock the stage refuses to run
    with pytest.raises(AssertionError, match="library lock"):
        await fp_engine.run_library_row(ctx, LibraryRowStage(lib, create_library=True))
