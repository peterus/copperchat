"""Two-phase adapter: library re-recording in `finally` and the generation-check skip for
library-only results (FP-C6, F3 Task 7)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.agent.sdk_tools import build_sdk_tool_list
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical, NetIntent, SheetRef
from copperchat.projects import Project
from copperchat.tools import StructuredToolError, ToolContext, ToolResult, ToolSpec, edit_spec

SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
SHEET = SheetRef(path="/root", page="1", file="demo.kicad_sch", name="demo")


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.recorded: list[list[Path]] = []
        self.lost: list[Exception] = []
        self.saved: list[str] = []
        self.callback_error: Exception | None = None
        self.callback_gate: asyncio.Event | None = None

    def ctx(self) -> ToolContext:
        async def save(doc: str) -> None:
            self.saved.append(doc)

        async def on_library_changed(files: list[Path]) -> None:
            if self.callback_gate is not None:
                await self.callback_gate.wait()
            if self.callback_error is not None:
                raise self.callback_error
            self.recorded.append(list(files))

        project = Project(
            id="a1b2c3d4e5f6",
            name="demo",
            rel_path="demo/demo.kicad_pro",
            dir=self.tmp_path,
            pro_file=self.tmp_path / "demo.kicad_pro",
            sch_file=None,
            pcb_file=None,
        )
        return ToolContext(
            project=project,
            session=cast(Any, SimpleNamespace(read_only=False, save=save, generation=0)),
            settings=cast(Any, None),
            run_id="run1",
            on_kicad_unavailable=self.lost.append,
            on_library_changed=on_library_changed,
        )

    def spec(self, apply_body: Any) -> ToolSpec:
        async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
            return ChangeSet(
                tool="fp_fake",
                sheet=SHEET,
                stages=[],
                pre_images={},
                intent=NetIntent(),
                logical=Logical(0, []),
            )

        return edit_spec("fp_fake", "d", SCHEMA, prepare, apply_body)

    async def call(self, spec: ToolSpec) -> dict[str, Any]:
        tool = build_sdk_tool_list([spec], self.ctx)[0]
        return cast(dict[str, Any], await tool.handler({}))


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def _what(raw: dict[str, Any]) -> str:
    return str(json.loads(raw["content"][0]["text"])["error"]["what"])


async def test_success_records_touched_files(h: Harness, tmp_path: Path) -> None:
    a = tmp_path / "A.kicad_mod"

    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        ctx.library_touched.append(a)
        return ToolResult(data={"ok": True}, library_changed={"X"})

    raw = await h.call(h.spec(apply))
    assert not raw.get("is_error")
    assert h.recorded == [[a]]
    assert h.saved == []  # a library-only result has no document to save


async def test_nothing_touched_no_callback(h: Harness) -> None:
    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        return ToolResult(changed_docs={"sch"})

    await h.call(h.spec(apply))
    assert h.recorded == []


async def test_failed_call_rerecords_in_finally_structured_error(
    h: Harness, tmp_path: Path
) -> None:
    table = tmp_path / "fp-lib-table"

    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        ctx.library_touched.append(table)  # a compensated S1 rewrote the table
        raise StructuredToolError("fp_fake failed", "why", "fix", code="already_exists")

    raw = await h.call(h.spec(apply))
    assert raw["is_error"] and _what(raw) == "fp_fake failed"  # original error kept
    assert h.recorded == [[table]]


async def test_failed_call_rerecords_in_finally_unexpected_error(
    h: Harness, tmp_path: Path
) -> None:
    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        ctx.library_touched.append(tmp_path / "A.kicad_mod")
        raise RuntimeError("boom")

    raw = await h.call(h.spec(apply))
    assert raw["is_error"] and "unexpectedly" in _what(raw)
    assert len(h.recorded) == 1


async def test_failed_call_rerecords_in_finally_cancelled(h: Harness, tmp_path: Path) -> None:
    started = asyncio.Event()
    h.callback_gate = asyncio.Event()

    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        ctx.library_touched.append(tmp_path / "A.kicad_mod")
        started.set()
        await asyncio.Event().wait()  # interrupted here
        raise AssertionError("unreachable")

    task = asyncio.ensure_future(h.call(h.spec(apply)))
    await started.wait()
    task.cancel()
    for _ in range(5):  # into the shielded re-recording, which waits on the gate
        await asyncio.sleep(0)
    task.cancel()  # a second cancel while the re-recording runs: it still completes
    h.callback_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(5):
        await asyncio.sleep(0)
    assert h.recorded == [[tmp_path / "A.kicad_mod"]]


async def test_callback_error_is_logged_not_raised(
    h: Harness, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    h.callback_error = OSError("db gone")

    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        ctx.library_touched.append(tmp_path / "A.kicad_mod")
        return ToolResult(library_changed={"X"})

    raw = await h.call(h.spec(apply))
    assert not raw.get("is_error")
    assert "re-recording the library" in caplog.text


async def test_generation_skip_for_library_only(h: Harness, tmp_path: Path) -> None:
    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        ctx.library_touched.append(tmp_path / "X.kicad_mod")
        ctx.session.generation += 1  # type: ignore[attr-defined]
        return ToolResult(data={"ok": True}, library_changed={"X"})

    raw = await h.call(h.spec(apply))
    assert not raw.get("is_error") and h.lost == []
    assert len(h.recorded) == 1


async def test_generation_check_kept_with_changed_docs(h: Harness, tmp_path: Path) -> None:
    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        ctx.library_touched.append(tmp_path / "fp-lib-table")
        ctx.session.generation += 1  # type: ignore[attr-defined]
        return ToolResult(library_changed={"X"}, changed_docs={"sch"})

    raw = await h.call(h.spec(apply))
    assert raw["is_error"] and "stopped responding" in _what(raw)
    assert len(h.lost) == 1
    assert len(h.recorded) == 1  # still re-recorded


async def test_generation_check_kept_without_library_changed(h: Harness) -> None:
    async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
        ctx.session.generation += 1  # type: ignore[attr-defined]
        return ToolResult()

    raw = await h.call(h.spec(apply))
    assert raw["is_error"] and "stopped responding" in _what(raw)
