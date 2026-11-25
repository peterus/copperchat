"""SP4 P1a Task 7 fix round 1: hooks that keep a project copy follow
`RunEngine.refresh_project` (board created / removed)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from copperchat.editing.hooks import EditingHooks
from copperchat.projects import Project
from copperchat.sessions import SessionHooks


def _project(tmp_path: Path) -> Project:
    return Project(
        id="p1",
        name="demo",
        rel_path="demo/demo.kicad_pro",
        dir=tmp_path,
        pro_file=tmp_path / "demo.kicad_pro",
        sch_file=tmp_path / "demo.kicad_sch",
        pcb_file=None,
    )


def test_editing_hooks_follow_project(tmp_path: Path) -> None:
    project = _project(tmp_path)
    hooks = EditingHooks(cast(Any, None), project, cast(Any, None))
    fresh = replace(project, pcb_file=tmp_path / "demo.kicad_pcb")
    hooks.on_project_changed(fresh)
    assert hooks.project is fresh


def test_session_hooks_follow_project(tmp_path: Path) -> None:
    project = _project(tmp_path)
    hooks = SessionHooks(cast(Any, None), project, cast(Any, None))
    fresh = replace(project, pcb_file=tmp_path / "demo.kicad_pcb")
    hooks.on_project_changed(fresh)
    assert hooks.project is fresh
