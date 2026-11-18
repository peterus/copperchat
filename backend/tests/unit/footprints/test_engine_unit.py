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
from copperchat.tools import ToolContext, ToolResult, ToolSpec, registry

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


def test_reload_handle_function_reloads_and_waits() -> None:
    seen: list[tuple[str, Any]] = []

    class KiCad:
        def reload_library(self, type: Any, scope: Any, nicknames: Any) -> None:
            seen.append(("reload", (type, scope, list(nicknames))))

        def wait_for_libraries(self, types: Any = None, **kw: Any) -> list[Any]:
            seen.append(("wait", types))
            return []

    fp_engine._reload_footprint_library("Lib", cast(Any, SimpleNamespace(kicad=KiCad())))
    assert [s[0] for s in seen] == ["reload", "wait"]
    assert seen[0][1][2] == ["Lib"]


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


def _spec(name: str) -> ToolSpec:
    async def handler(_ctx: Any, _args: dict[str, Any]) -> ToolResult:
        return ToolResult(text="ok")

    return ToolSpec(name=name, description="d", input_schema={"type": "object"}, handler=handler)


def test_fp_writes_in_schematic_and_free_only() -> None:
    assert not FP_WRITES & FP_READ
    assert all(n.startswith("fp_") for n in FP_WRITES)
    for kind in ("schematic", "free"):
        assert PROFILES[kind] >= FP_WRITES, kind
    for kind in ("requirements", "board"):
        assert not FP_WRITES & PROFILES[kind], kind


def test_tools_off_no_fp_tool_in_any_profile() -> None:
    specs = list(registry.all_specs())
    if "fp_create" not in {s.name for s in specs}:  # registered from F4a Task 3 on
        specs.append(_spec("fp_create"))
    fp_names = {s.name for s in specs if s.name.startswith("fp_")}
    assert {"fp_create"} | set(FP_READ) | set(FP_WRITES) <= fp_names
    for kind in PROFILES:
        on = {s.name for s in profile_specs(kind, specs, footprint_tools=True)}
        off = {s.name for s in profile_specs(kind, specs, footprint_tools=False)}
        assert not {n for n in off if n.startswith("fp_")}, kind
        assert off == {n for n in on if not n.startswith("fp_")}, kind
    free_on = {s.name for s in profile_specs("free", specs)}
    assert fp_names <= free_on
