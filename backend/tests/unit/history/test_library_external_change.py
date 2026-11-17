"""Own library writes are re-recorded (F3 Task 7); library-only external changes neither
reload nor pause (F3 Task 8). Library files are copies of KiCad-saved footprints."""

from __future__ import annotations

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
from tests.support.history import ECC83, replace_with_kicad_file
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
