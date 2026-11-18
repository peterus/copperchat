"""Own library writes are re-recorded (F3 Task 7); library-only external changes neither
reload nor pause (F3 Task 8). Library files are copies of KiCad-saved footprints."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.agent.sdk_tools import build_sdk_tool_list
from copperchat.history.hooks import HistoryHooks
from copperchat.history.store import LibraryLocation
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical, NetIntent, SheetRef
from copperchat.projects import Project
from copperchat.tools import ToolContext, ToolResult, edit_spec
from tests.support.history import ECC83, HIERARCHY, other_board, replace_with_kicad_file
from tests.unit.history.conftest import Env
from tests.unit.history.test_history_hooks import turn

_FOOTPRINTS = sorted((ECC83 / "footprints.pretty").glob("*.kicad_mod"))


@dataclass
class Lib:
    env: Env
    project: Project
    hooks: HistoryHooks
    location: LibraryLocation
    events: list[tuple[list[str], str]] = field(default_factory=list)
    external: list[list[str]] = field(default_factory=list)

    def file(self, stem: str) -> Path:
        return self.location.dir / f"{stem}.kicad_mod"

    def rel(self, path: Path) -> str:
        return Path(os.path.relpath(path, self.project.dir)).as_posix()

    def change(self, stem: str) -> None:
        """Another project (or the GUI) saved the footprint: swap in another KiCad file."""
        path = self.file(stem)
        other = next(f for f in _FOOTPRINTS if f.read_bytes() != path.read_bytes())
        replace_with_kicad_file(path, other)


async def _setup(env: Env, *, open_session: bool = False) -> Lib:
    project = env.project()
    repo = env.settings.projects_root
    lib_dir = repo / "footprints" / "X.pretty"
    lib_dir.mkdir(parents=True)
    for stem, src in zip("ABC", _FOOTPRINTS, strict=False):
        shutil.copyfile(src, lib_dir / f"{stem}.kicad_mod")  # byte copies of KiCad files
    location = LibraryLocation(
        dir=lib_dir, repo_root=repo, table=project.dir / "fp-lib-table", nickname="X"
    )

    async def locate(p: Project) -> LibraryLocation | None:
        return location if p.id == project.id else None

    env.svc.locate_library = locate
    if open_session:
        await env.sessions.open(project)
    hooks = env.svc.hooks_factory(project, env.engine)  # type: ignore[arg-type]
    lib = Lib(env, project, hooks, location)

    async def listener(p: Project, rels: list[str], reason: str) -> None:
        lib.events.append((rels, reason))

    async def paused(p: Project, rels: list[str]) -> None:
        lib.external.append(rels)

    env.svc.library_listeners.append(listener)
    env.svc.on_external_change.append(paused)
    await hooks.on_engine_start()
    return lib


@pytest.fixture
async def lib(env: Env) -> Lib:
    return await _setup(env)


def _ctx(lib: Lib) -> ToolContext:
    async def save(doc: str) -> None:
        raise AssertionError("a library-only result saves nothing")

    return ToolContext(
        project=lib.project,
        session=cast(Any, SimpleNamespace(read_only=False, save=save, generation=0)),
        settings=cast(Any, None),
        run_id="run-1",
        on_library_changed=lib.hooks.on_library_changed,
    )


async def _write_footprint(lib: Lib, stem: str, *, fail: bool = False) -> dict[str, Any]:
    """A fake two-phase footprint tool that "writes" `stem` (an external KiCad save
    stands in for the api-server's write)."""

    async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
        sheet = SheetRef(path="/", page="1", file="x", name="x")
        return ChangeSet("fp_fake", sheet, [], {}, NetIntent(), Logical(0, []))

    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        lib.change(stem)
        ctx.library_touched.append(lib.file(stem))
        if fail:
            raise RuntimeError("S2 failed after writing")
        return ToolResult(data={"ok": True}, library_changed={"X"})

    tool = build_sdk_tool_list([edit_spec("fp_fake", "d", {}, prepare, apply)], lambda: _ctx(lib))
    raw = await tool[0].handler({})
    return cast(dict[str, Any], json.loads(raw["content"][0]["text"]))


# ---- Task 7: re-recording own writes


async def test_success_rerecords_only_written_files(lib: Lib) -> None:
    env, project = lib.env, lib.project
    lib.change("B")  # meanwhile another project saved B
    result = await _write_footprint(lib, "A")
    assert result == {"ok": True}

    stored = await env.svc.hashes.get(project.id)
    current = await env.svc.current_hashes(project)
    a, b = lib.rel(lib.file("A")), lib.rel(lib.file("B"))
    assert stored[a] == current[a].sha256
    assert stored[b] != current[b].sha256
    assert lib.events == [([a], "own")]

    await lib.hooks.on_turn_start(turn("run-2"))
    assert env.engine.notices[-1][1] is not None
    assert env.engine.notices[-1][1]["files"] == [b]


async def test_failed_call_rerecords_in_finally(lib: Lib) -> None:
    result = await _write_footprint(lib, "A", fail=True)
    assert "unexpectedly" in result["error"]["what"]
    await lib.hooks.on_turn_start(turn("run-2"))
    assert lib.env.engine.notices == []  # the own (failed) write is no external change


async def test_rerecord_drops_row_of_deleted_file(lib: Lib) -> None:
    env, project = lib.env, lib.project
    a = lib.file("A")
    a.unlink()  # e.g. cleanup of a half-built footprint
    await env.svc.on_library_changed(project, [a])
    assert lib.rel(a) not in await env.svc.hashes.get(project.id)
    await lib.hooks.on_turn_start(turn("run-2"))
    assert env.engine.notices == []


async def test_rerecord_skips_untracked_files(env: Env) -> None:
    project = env.project()  # no library located
    hooks = env.svc.hooks_factory(project, env.engine)  # type: ignore[arg-type]
    await hooks.on_engine_start()
    stray = project.dir / "footprints.pretty" / _FOOTPRINTS[0].name
    await env.svc.on_library_changed(project, [stray])
    assert Path(os.path.relpath(stray, project.dir)).as_posix() not in (
        await env.svc.hashes.get(project.id)
    )
    await hooks.on_turn_start(turn("run-2"))
    assert env.engine.notices == []


async def test_saved_listener_keeps_library_rows(lib: Lib) -> None:
    env, project = lib.env, lib.project
    before = await env.svc.hashes.get_rows(project.id)
    library = {k: v for k, v in before.items() if k.startswith("../") or k == "fp-lib-table"}
    assert len(library) == 4  # fp-lib-table + A, B, C
    await env.svc._on_saved(project, "sch")
    after = await env.svc.hashes.get_rows(project.id)
    assert {k: after[k] for k in library} == library


# ---- Task 8: library-only external changes


@pytest.fixture
async def opened_lib(env: Env) -> Lib:
    return await _setup(env, open_session=True)


async def test_library_only_change_does_not_reload_or_pause(opened_lib: Lib) -> None:
    lib = opened_lib
    env = lib.env
    session = env.sessions.current()
    lib.change("B")
    lib.change("A")

    await lib.hooks.on_turn_start(turn("run-2"))

    assert env.sessions.current() is session and env.server.starts == 1  # no reopen
    assert lib.external == []  # on_external_change (plan pause) not called
    rels = sorted(lib.rel(lib.file(s)) for s in "AB")
    text, meta = env.engine.notices[-1]
    assert text == "Footprints changed outside this project: A, B"
    assert meta == {"action": "library-external-change", "files": rels}
    assert env.engine.notice_conversations[-1] == turn().conversation_id
    assert env.engine.notes and "A, B" in env.engine.notes[-1]
    assert lib.events == [(rels, "external")]
    # re-recorded: the next turn start reports nothing
    await lib.hooks.on_turn_start(turn("run-3"))
    assert len(env.engine.notices) == 1 and lib.events == [(rels, "external")]


async def test_deleted_library_file_is_library_only(opened_lib: Lib) -> None:
    lib = opened_lib
    lib.file("C").unlink()
    await lib.hooks.on_turn_start(turn("run-2"))
    assert lib.env.server.starts == 1 and lib.external == []
    assert lib.env.engine.notices[-1][0] == "Footprints changed outside this project: C"


async def test_mixed_change_takes_todays_path(opened_lib: Lib) -> None:
    lib = opened_lib
    env, project = lib.env, lib.project
    assert project.pcb_file is not None
    session = env.sessions.current()
    lib.change("B")
    replace_with_kicad_file(project.pcb_file, other_board())

    await lib.hooks.on_turn_start(turn("run-2"))

    changed = sorted([lib.rel(lib.file("B")), project.pcb_file.name])
    assert env.sessions.current() is not session and env.server.starts == 2  # reloaded
    assert lib.external == [changed]
    text, meta = env.engine.notices[-1]
    assert "outside CopperChat" in text
    assert meta == {"action": "external-change", "files": changed}
    # the library caches still hear about the library part
    assert lib.events == [([lib.rel(lib.file("B"))], "external")]


async def test_table_only_change_takes_todays_path(opened_lib: Lib) -> None:
    lib = opened_lib
    env, project = lib.env, lib.project
    session = env.sessions.current()
    replace_with_kicad_file(project.dir / "fp-lib-table", HIERARCHY / "fp-lib-table")

    await lib.hooks.on_turn_start(turn("run-2"))

    assert env.sessions.current() is not session and env.server.starts == 2
    assert lib.external == [["fp-lib-table"]]
    assert env.engine.notices[-1][1] == {"action": "external-change", "files": ["fp-lib-table"]}
    assert lib.events == []


async def test_no_change_does_nothing(opened_lib: Lib) -> None:
    lib = opened_lib
    session = lib.env.sessions.current()
    await lib.hooks.on_turn_start(turn("run-2"))
    assert lib.env.engine.notices == [] and lib.env.engine.notes == []
    assert lib.external == [] and lib.events == []
    assert lib.env.sessions.current() is session and lib.env.server.starts == 1


async def test_rerecord_keeps_rows_written_meanwhile(
    lib: Lib, monkeypatch: pytest.MonkeyPatch
) -> None:
    """on_library_changed / _on_saved write only their own rows: a row another path
    stored while they were hashing (or between their read and write) survives."""
    from copperchat.history import service as service_mod

    env, project = lib.env, lib.project
    loop = asyncio.get_running_loop()
    injected: list[str] = []

    async def other_path(rel: str) -> None:
        injected.append(rel)
        await env.svc.db.execute(
            "INSERT OR REPLACE INTO document_hashes(project_id, rel_path, sha256) VALUES (?, ?, ?)",
            (project.id, rel, "7" * 64),
        )

    real_compute = service_mod.compute_tracked_hashes

    def computing(files: Any, previous: Any) -> Any:
        rows = real_compute(files, previous)
        rel = f"meanwhile-{len(injected)}.kicad_sch"
        asyncio.run_coroutine_threadsafe(other_path(rel), loop).result()
        return rows

    real_get_rows = env.svc.hashes.get_rows

    async def get_rows(project_id: str) -> Any:
        rows = await real_get_rows(project_id)
        await other_path(f"between-{len(injected)}.kicad_sch")
        return rows

    monkeypatch.setattr(service_mod, "compute_tracked_hashes", computing)
    monkeypatch.setattr(env.svc.hashes, "get_rows", get_rows)
    lib.change("A")
    await env.svc.on_library_changed(project, [lib.file("A")])
    await env.svc._on_saved(project, "pcb")

    stored = await env.svc.hashes.get(project.id)
    assert injected and all(stored.get(rel) == "7" * 64 for rel in injected)
    monkeypatch.setattr(service_mod, "compute_tracked_hashes", real_compute)
    current = await env.svc.current_hashes(project)
    assert stored[lib.rel(lib.file("A"))] == current[lib.rel(lib.file("A"))].sha256


async def test_library_appearing_later_is_a_baseline(env: Env) -> None:
    """A project whose rows were recorded without a library: when the locator starts
    returning one, its table and footprints are recorded silently - no reload, no plan
    pause, no notice. Changes after that are reported as usual."""
    project = env.project()
    await env.sessions.open(project)
    hooks = env.svc.hooks_factory(project, env.engine)  # type: ignore[arg-type]
    await hooks.on_engine_start()  # default locator: no library
    stored = await env.svc.hashes.get(project.id)
    assert "fp-lib-table" not in stored and not any(k.endswith(".kicad_mod") for k in stored)

    lib_dir = env.settings.projects_root / "footprints" / "X.pretty"
    lib_dir.mkdir(parents=True)
    for stem, src in zip("ABC", _FOOTPRINTS, strict=False):
        shutil.copyfile(src, lib_dir / f"{stem}.kicad_mod")  # byte copies of KiCad files
    location = LibraryLocation(
        dir=lib_dir,
        repo_root=env.settings.projects_root,
        table=project.dir / "fp-lib-table",
        nickname="X",
    )

    async def locate(p: Project) -> LibraryLocation | None:
        return location

    env.svc.locate_library = locate
    events: list[tuple[list[str], str]] = []
    external: list[list[str]] = []

    async def listener(p: Project, rels: list[str], reason: str) -> None:
        if p.id == project.id:
            events.append((rels, reason))

    async def paused(p: Project, rels: list[str]) -> None:
        if p.id == project.id:
            external.append(rels)

    env.svc.library_listeners.append(listener)
    env.svc.on_external_change.append(paused)
    session = env.sessions.current()
    notices = len(env.engine.notices)

    await hooks.on_turn_start(turn("run-2"))

    assert env.sessions.current() is session
    assert len(env.engine.notices) == notices and events == [] and external == []
    stored = await env.svc.hashes.get(project.id)
    assert "fp-lib-table" in stored
    assert sum(k.endswith(".kicad_mod") for k in stored) == 3

    b = lib_dir / "B.kicad_mod"
    replace_with_kicad_file(b, next(f for f in _FOOTPRINTS if f.read_bytes() != b.read_bytes()))
    shutil.copyfile(_FOOTPRINTS[0], lib_dir / "D.kicad_mod")  # a new footprint is reported
    await hooks.on_turn_start(turn("run-3"))
    assert env.engine.notices[-1][0] == "Footprints changed outside this project: B, D"
    assert external == []
