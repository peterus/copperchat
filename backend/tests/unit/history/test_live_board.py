"""Live board view (SP4 P1c Task 7, G8): pcb changes join the debounced live publish,
the turn end catches the board up from disk, and a board tool publishes once per call."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from copperchat.agent.protocol import ViewUpdatedFrame
from copperchat.agent.sdk_tools import _make_handler
from copperchat.kicad_session import DocKind
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical
from copperchat.tools import ToolContext, edit_spec
from copperchat.views.snapshot import snapshot_path
from tests.unit.board.test_engine_unit import FakeSession, Spy, _ctx
from tests.unit.history.conftest import Env, Opened
from tests.unit.history.test_history_hooks import done, turn
from tests.unit.history.test_live_view import (
    Rig,
    disk_rig,
    kicad_calls_during_catch_up,
    make_rig,
    until,
)


@pytest.fixture
def rig(opened: Opened) -> Rig:
    return make_rig(opened)


def frames(opened: Opened) -> list[ViewUpdatedFrame]:
    return [f for f in opened.env.broadcaster.frames if isinstance(f, ViewUpdatedFrame)]


async def test_pcb_change_schedules_pcb(rig: Rig) -> None:
    rig.opened.hooks.on_documents_changed(turn(), {"pcb"})
    await rig.time.advance(2.0)
    assert rig.calls == [(2.0, {"pcb"}, True)]
    await rig.time.advance(30)
    assert len(rig.calls) == 1


async def test_sch_and_pcb_one_frame(opened: Opened) -> None:
    project = opened.project
    t, live, content = disk_rig(opened)
    content["pcb"] = b"(kicad_pcb v2)"
    opened.hooks.on_documents_changed(turn(), {"sch"})
    await t.advance(0.5)
    opened.hooks.on_documents_changed(turn(), {"pcb"})  # pending docs accumulate
    await t.advance(2.0)
    await until(lambda: len(frames(opened)) == 1)
    await until(lambda: not live._states[project.id].publishing)
    await t.advance(30)
    [frame] = frames(opened)
    assert frame.docs == ["pcb", "sch"] and frame.live is True
    assert snapshot_path(project, "pcb").read_bytes() == b"(kicad_pcb v2)"


@pytest.fixture
async def boardless(env: Env) -> Opened:
    project = env.project()
    assert project.pcb_file is not None
    project.pcb_file.unlink()
    project = replace(project, pcb_file=None)
    session = await env.sessions.open(project)
    hooks = env.svc.hooks_factory(project, env.engine)  # type: ignore[arg-type]
    await hooks.on_engine_start()
    return Opened(env, project, session, hooks)


async def test_boardless_pcb_ignored(boardless: Opened, caplog: pytest.LogCaptureFixture) -> None:
    rig = make_rig(boardless)
    rig.opened.hooks.on_documents_changed(turn(), {"pcb"})
    await rig.time.advance(10)
    assert rig.calls == [] and "failed" not in caplog.text
    assert await rig.live.finish(boardless.project) == set()
    rig.opened.hooks.on_documents_changed(turn(), {"sch", "pcb"})
    await rig.time.advance(10)
    assert rig.calls == [(10 + 2.0, {"sch"}, True)]


async def test_disk_catch_up_includes_pcb(opened: Opened, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live frames of the schematic and the board, then KiCad fails: the catch-up frame
    carries both documents as they are on disk, without a KiCad call."""
    env, project = opened.env, opened.project
    t, live, content = disk_rig(opened)
    content["pcb"] = b"(kicad_pcb live)"
    opened.hooks.on_documents_changed(turn(), {"sch", "pcb"})
    await t.advance(2.0)
    await until(lambda: len(frames(opened)) == 1)
    await until(lambda: not live._states[project.id].publishing)
    assert project.pcb_file is not None
    pcb_root = snapshot_path(project, "pcb").name
    calls, starts = kicad_calls_during_catch_up(opened, monkeypatch)
    await opened.hooks.on_turn_end(turn(), done("sch", "pcb", reason="kicad_failed", failed=True))
    catch_up = frames(opened)[1]
    assert catch_up.docs == ["pcb", "sch"] and catch_up.live is None
    assert pcb_root in (catch_up.changed_files or [])
    assert catch_up.view_files is not None and catch_up.view_files["pcb"] == [pcb_root]
    assert snapshot_path(project, "pcb").read_bytes() == project.pcb_file.read_bytes()
    assert calls == [] and starts == [0]
    assert env.saved == [] and env.server.saved == []


async def test_disk_catch_up_without_a_live_board_frame_leaves_the_board(
    opened: Opened,
) -> None:
    project = opened.project
    t, live, _ = disk_rig(opened)
    opened.hooks.on_documents_changed(turn(), {"sch"})
    await t.advance(2.0)
    await until(lambda: len(frames(opened)) == 1)
    await until(lambda: not live._states[project.id].publishing)
    await opened.hooks.on_turn_end(turn(), done("sch", reason="kicad_failed", failed=True))
    assert frames(opened)[1].docs == ["sch"]
    assert not snapshot_path(project, "pcb").exists()


async def test_board_save_failure_after_a_live_board_frame_catches_up_the_board(
    rig: Rig,
) -> None:
    rig.opened.hooks.on_documents_changed(turn(), {"pcb"})
    await rig.time.advance(2.0)
    rig.opened.env.save_error, rig.opened.env.save_error_doc = RuntimeError("full"), "pcb"
    await rig.opened.hooks.on_turn_end(turn(), done("pcb"))
    # Nothing saved, so no turn-end publish: the board on disk is announced.
    assert [(d, live) for _, d, live in rig.calls[1:]] == [({"pcb"}, False)]
    assert rig.disk == 1


# ---- a board tool publishes once per call, after apply (never per stage)


class SavingSession(FakeSession):
    async def save(self, doc: DocKind) -> None:
        self.log.append(f"save {doc}")


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> Spy:
    from copperchat.kicad_tools.board import engine as board_engine

    s = Spy(log)
    monkeypatch.setattr(board_engine, "take_snapshot", s.take_snapshot)
    monkeypatch.setattr(board_engine, "restore", s.restore)
    monkeypatch.setattr(board_engine, "leftovers", s.leftovers)
    return s


@pytest.fixture
def log() -> list[str]:
    return []


async def test_board_tool_publishes_once(tmp_path: Path, log: list[str], spy: Spy) -> None:
    session = SavingSession(log)
    published: list[set[DocKind]] = []

    def stage(n: int) -> BoardStage:
        async def run(sctx: BoardStageContext) -> None:
            log.append(f"run {n}")

        return BoardStage(f"s{n}", run)

    async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
        return BoardChangeSet(
            tool="pcb_fake",
            logical=Logical(1),
            kind="board_move",
            board_stages=[stage(1), stage(2), stage(3)],
        )

    async def apply(ctx: ToolContext, cs: ChangeSet) -> Any:
        assert isinstance(cs, BoardChangeSet)
        result = await apply_board_change_set(ctx, cs)
        log.append("apply returned")
        return result

    def changed(docs: set[DocKind]) -> None:
        log.append("documents changed")
        published.append(set(docs))

    ctx = _ctx(tmp_path, session, on_documents_changed=changed)
    handler = _make_handler(edit_spec("pcb_fake", "", {}, prepare, apply), lambda: ctx)
    out = await handler({})
    assert not out.get("is_error"), out
    assert published == [{"pcb"}]
    assert log.index("run 3") < log.index("applied") < log.index("apply returned")
    assert log.index("apply returned") < log.index("documents changed")
