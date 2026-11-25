"""`_two_phase_steps` after a call replaced the session through `reload_project` (board
creation, B-C4): the restart check is re-based on the generation captured at reload."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from copperchat.agent.sdk_tools import build_sdk_tool_list
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical, NetIntent, SheetRef
from copperchat.projects import Project
from copperchat.tools import ToolContext, ToolResult, edit_spec

SHEET = SheetRef(path="", page="", file="", name="<board>")


class FakeSession:
    def __init__(self, name: str, generation: int, saved: list[str]) -> None:
        self.name = name
        self.generation = generation
        self.read_only = False
        self.saved = saved
        self.bump_on_save = False

    async def save(self, doc: str) -> None:
        if self.bump_on_save:
            self.generation += 1
        self.saved.append(f"{self.name} {doc}")


def _ctx(tmp_path: Path, session: FakeSession, lost: list[Exception]) -> ToolContext:
    project = Project(
        id="p1",
        name="demo",
        rel_path="demo/demo.kicad_pro",
        dir=tmp_path,
        pro_file=tmp_path / "demo.kicad_pro",
        sch_file=None,
        pcb_file=None,
    )
    return ToolContext(
        project=project,
        session=cast(Any, session),
        settings=cast(Any, None),
        on_kicad_unavailable=lost.append,
    )


async def _call(tmp_path: Path, old: FakeSession, new: FakeSession, later_bump: bool) -> Any:
    lost: list[Exception] = []

    async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
        return ChangeSet("pcb_fake", SHEET, [], {}, NetIntent(), Logical(1))

    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        ctx.session = cast(Any, new)  # what reload_project does inside the engine
        ctx.reloaded_generation = new.generation
        if later_bump:  # a later stage meets a restart of the new session
            new.generation += 1
        return ToolResult(data={"ok": True}, changed_docs={"pcb"})

    spec = edit_spec("pcb_fake", "d", {"type": "object", "properties": {}}, prepare, apply)
    tool = build_sdk_tool_list([spec], lambda: _ctx(tmp_path, old, lost))[0]
    raw = await tool.handler({})
    return raw, lost


async def test_session_replaced_rebaselines(tmp_path: Path) -> None:
    saved: list[str] = []
    old, new = FakeSession("old", 2, saved), FakeSession("new", 1, saved)
    raw, lost = await _call(tmp_path, old, new, later_bump=False)
    assert not raw.get("is_error"), raw
    assert json.loads(raw["content"][0]["text"]) == {"ok": True}
    assert saved == ["new pcb"] and lost == []


async def test_restart_after_reload_still_detected(tmp_path: Path) -> None:
    saved: list[str] = []
    old, new = FakeSession("old", 2, saved), FakeSession("new", 1, saved)
    new.bump_on_save = True
    raw, lost = await _call(tmp_path, old, new, later_bump=False)
    assert raw["is_error"]
    assert "stopped responding" in json.loads(raw["content"][0]["text"])["error"]["what"]
    assert len(lost) == 1


async def test_restart_in_later_stage_detected(tmp_path: Path) -> None:
    saved: list[str] = []
    old, new = FakeSession("old", 2, saved), FakeSession("new", 1, saved)
    raw, lost = await _call(tmp_path, old, new, later_bump=True)
    assert raw["is_error"]
    assert "stopped responding" in json.loads(raw["content"][0]["text"])["error"]["what"]
    assert len(lost) == 1
