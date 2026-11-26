"""The board write engine against a fake session (SP4 P1a Task 7, B-C4)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from copperchat.changes.repo import ChangeEntry
from copperchat.errors import DocumentInconsistent, StructuredToolError
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board import engine as board_engine
from copperchat.kicad_tools.board.compensate import CopperSnapshot
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.edit.changeset import Logical
from copperchat.kicad_tools.edit.engine import apply_change_set
from copperchat.projects import Project
from copperchat.tools.spec import ToolContext

EMPTY: frozenset[str] = frozenset()


def _snap(tracks: frozenset[str] = EMPTY) -> CopperSnapshot:
    return CopperSnapshot(EMPTY | tracks, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, ())


class FakeSession:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.generation = 1
        self.server_alive = True
        self.timeouts: list[float | None] = []
        self.handle = SimpleNamespace(name="handle")

    @contextlib.asynccontextmanager
    async def applying(self) -> AsyncIterator[None]:
        self.log.append("applying")
        try:
            yield
        finally:
            self.log.append("applied")

    @contextlib.asynccontextmanager
    async def commit(self, label: str, *, doc: str = "sch") -> AsyncIterator[Any]:
        self.log.append(f"begin {doc} {label}")
        try:
            yield SimpleNamespace(pushed=False)
        except BaseException:
            self.log.append("drop")
            raise
        self.log.append("push")

    async def call(self, fn: Callable[[Any], Any], *, timeout: float | None = None) -> Any:
        self.timeouts.append(timeout)
        return fn(self.handle)


class Spy:
    """take_snapshot / restore / leftovers stand-ins."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.compensations = 0
        self.left: list[str] = []
        self.fail: Exception | None = None
        self.snapshots: list[CopperSnapshot] = [_snap()]

    def take_snapshot(self, h: Any, mode: str) -> CopperSnapshot:
        self.log.append(f"snapshot {mode}")
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def restore(self, h: Any, snap: CopperSnapshot) -> dict[str, str]:
        self.compensations += 1
        self.log.append("compensate")
        if self.fail is not None:
            raise self.fail
        return {}

    def leftovers(self, h: Any, snap: CopperSnapshot, renamed: dict[str, str]) -> list[str]:
        self.log.append("leftovers")
        return list(self.left)


@pytest.fixture
def log() -> list[str]:
    return []


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> Spy:
    s = Spy(log)
    monkeypatch.setattr(board_engine, "take_snapshot", s.take_snapshot)
    monkeypatch.setattr(board_engine, "restore", s.restore)
    monkeypatch.setattr(board_engine, "leftovers", s.leftovers)
    return s


class FakeEditing:
    def __init__(self) -> None:
        self.entries: list[ChangeEntry] = []

    def turn(self, pid: str, run_id: str | None) -> Any:
        return SimpleNamespace(active_step_id=None, call_seq=4)

    async def record_changes(self, ctx: ToolContext, entries: list[ChangeEntry]) -> int:
        self.entries += entries
        return len(entries)


def _ctx(tmp_path: Path, session: FakeSession, **kw: Any) -> ToolContext:
    project = Project(
        id="p1",
        name="demo",
        rel_path="demo/demo.kicad_pro",
        dir=tmp_path,
        pro_file=tmp_path / "demo.kicad_pro",
        sch_file=tmp_path / "demo.kicad_sch",
        pcb_file=tmp_path / "demo.kicad_pcb",
    )
    return ToolContext(
        project=project,
        session=cast(Any, session),
        settings=cast(Any, None),
        run_id="run-1",
        **kw,
    )


def _stage(log: list[str], n: int, **kw: Any) -> BoardStage:
    async def run(sctx: BoardStageContext) -> None:
        log.append(f"run {n}")

    return BoardStage(f"s{n}", run, **kw)


def _cs(stages: list[BoardStage], **kw: Any) -> BoardChangeSet:
    return BoardChangeSet(
        tool="pcb_fake", logical=Logical(1), kind="board_move", board_stages=stages, **kw
    )


async def test_apply_change_set_refuses_board_cs() -> None:
    class Untouchable:
        def applying(self) -> Any:
            raise AssertionError("the schematic engine must not touch the session")

    ctx = ToolContext(
        project=cast(Any, None), session=cast(Any, Untouchable()), settings=cast(Any, None)
    )
    with pytest.raises(TypeError, match="BoardChangeSet"):
        await apply_change_set(ctx, _cs([]))


async def test_success_one_commit_per_stage(tmp_path: Path, log: list[str], spy: Spy) -> None:
    session = FakeSession(log)

    async def plain(sctx: BoardStageContext) -> None:
        log.append("run plain")

    cs = _cs([_stage(log, 1), BoardStage("own", plain, commit=False)])
    cs.report["moved"] = 2
    result = await apply_board_change_set(_ctx(tmp_path, session), cs)
    assert result.changed_docs == {"pcb"}
    assert result.data == {
        "moved": 2,
        "step_report": [{"stage": "s1", "status": "done"}, {"stage": "own", "status": "done"}],
    }
    assert log == ["applying", "snapshot ids", "begin pcb copperchat: pcb_fake", "run 1", "push",
                   "run plain", "snapshot ids", "applied"]  # fmt: skip
    assert spy.compensations == 0


async def test_fail_after_compensates(tmp_path: Path, log: list[str], spy: Spy) -> None:
    session = FakeSession(log)
    cs = _cs([_stage(log, 1), _stage(log, 2), _stage(log, 3)], fail_after=2)
    with pytest.raises(RuntimeError, match="fail_after"):
        await apply_board_change_set(_ctx(tmp_path, session), cs)
    assert spy.compensations == 1
    assert "run 3" not in log
    i = log.index("compensate")
    assert log[i - 1] == "begin pcb copperchat: pcb_fake: undo"
    assert log[i + 1 : i + 3] == ["push", "leftovers"]


async def test_full_snapshot_mode_is_passed(tmp_path: Path, log: list[str], spy: Spy) -> None:
    cs = _cs([_stage(log, 1)], fail_after=1, snapshot="full")
    with pytest.raises(RuntimeError):
        await apply_board_change_set(_ctx(tmp_path, FakeSession(log)), cs)
    assert log[1] == "snapshot full"


async def test_no_compensation_before_the_first_stage(
    tmp_path: Path, log: list[str], spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(h: Any, mode: str) -> CopperSnapshot:
        raise ValueError("snapshot failed")

    monkeypatch.setattr(board_engine, "take_snapshot", broken)
    with pytest.raises(ValueError):
        await apply_board_change_set(_ctx(tmp_path, FakeSession(log)), _cs([_stage(log, 1)]))
    assert spy.compensations == 0 and "run 1" not in log


async def test_compensation_failure_is_document_inconsistent(
    tmp_path: Path, log: list[str], spy: Spy
) -> None:
    spy.fail = OSError("api broke")
    cs = _cs([_stage(log, 1), _stage(log, 2)], fail_after=2)
    with pytest.raises(DocumentInconsistent):
        await apply_board_change_set(_ctx(tmp_path, FakeSession(log)), cs)


async def test_leftovers_are_document_inconsistent(
    tmp_path: Path, log: list[str], spy: Spy
) -> None:
    spy.left = ["missing track abc"]
    cs = _cs([_stage(log, 1)], fail_after=1)
    with pytest.raises(DocumentInconsistent) as info:
        await apply_board_change_set(_ctx(tmp_path, FakeSession(log)), cs)
    assert info.value.leftovers == ["missing track abc"]


async def test_cancel_compensates_shielded(tmp_path: Path, log: list[str], spy: Spy) -> None:
    entered = asyncio.Event()

    async def hang(sctx: BoardStageContext) -> None:
        entered.set()
        await asyncio.Event().wait()

    cs = _cs([_stage(log, 1), BoardStage("hang", hang)])
    task = asyncio.ensure_future(apply_board_change_set(_ctx(tmp_path, FakeSession(log)), cs))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spy.compensations == 1
    assert log.index("drop") < log.index("compensate")
    assert log[-1] == "applied"


async def test_cancel_compensation_failure_reported(
    tmp_path: Path, log: list[str], spy: Spy
) -> None:
    spy.fail = OSError("api broke")
    reported: list[Exception] = []
    entered = asyncio.Event()

    async def hang(sctx: BoardStageContext) -> None:
        entered.set()
        await asyncio.Event().wait()

    ctx = _ctx(tmp_path, FakeSession(log), on_document_inconsistent=reported.append)
    task = asyncio.ensure_future(
        apply_board_change_set(ctx, _cs([_stage(log, 1), BoardStage("hang", hang)]))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(reported) == 1 and isinstance(reported[0], DocumentInconsistent)


async def test_verify_mismatch(
    tmp_path: Path, log: list[str], spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_model(ctx: ToolContext) -> Any:
        return "model"

    monkeypatch.setattr(board_engine, "board_model", fake_model)
    seen: list[Any] = []

    def verify(model: Any, report: dict[str, Any]) -> list[str]:
        seen.append((model, dict(report)))
        return ["R1 not at (10, 10)"]

    cs = _cs([_stage(log, 1)], verify=verify)
    cs.report["x"] = 1
    with pytest.raises(StructuredToolError) as info:
        await apply_board_change_set(_ctx(tmp_path, FakeSession(log)), cs)
    assert info.value.code == "verification_failed"
    assert "R1 not at (10, 10)" in info.value.why
    assert info.value.details == {"found": ["R1 not at (10, 10)"]}
    assert seen == [("model", {"x": 1})]
    assert spy.compensations == 1


async def test_single_change_log_entry(tmp_path: Path, log: list[str], spy: Spy) -> None:
    spy.snapshots = [_snap(), _snap(frozenset({"t1", "t2"}))]
    editing = FakeEditing()
    ctx = _ctx(tmp_path, FakeSession(log), editing=cast(Any, editing))
    await apply_board_change_set(ctx, _cs([_stage(log, 1), _stage(log, 2)]))
    assert len(editing.entries) == 1
    e = editing.entries[0]
    assert (e.kind, e.sheet_path, e.tool, e.call_seq) == ("board_move", "", "pcb_fake", 4)
    assert e.after == {"tracks_added": 2}


async def test_non_compensable_stage_skips_compensation(
    tmp_path: Path, log: list[str], spy: Spy
) -> None:
    async def boom(sctx: BoardStageContext) -> None:
        raise ValueError("stage 2 broke")

    cs = _cs([_stage(log, 1, compensable=False), BoardStage("s2", boom)])
    with pytest.raises(StructuredToolError) as info:
        await apply_board_change_set(_ctx(tmp_path, FakeSession(log)), cs)
    assert info.value.code == "sync_failed"
    assert "History panel" in info.value.suggestion
    assert spy.compensations == 0


async def test_undo_runs_reverse_order(tmp_path: Path, log: list[str], spy: Spy) -> None:
    def undo(n: int) -> Callable[[BoardStageContext], Any]:
        async def run(sctx: BoardStageContext) -> None:
            log.append(f"undo {n}")

        return run

    stages = [_stage(log, n, undo=undo(n)) for n in (1, 2, 3)]
    with pytest.raises(RuntimeError):
        await apply_board_change_set(_ctx(tmp_path, FakeSession(log)), _cs(stages, fail_after=3))
    i = log.index("compensate")
    assert [x for x in log[i:] if x.startswith("undo")] == ["undo 3", "undo 2", "undo 1"]


async def test_undo_runs_for_failing_stage_then_completed(
    tmp_path: Path, log: list[str], spy: Spy
) -> None:
    """The stage that started and raised is undone too (undo restores pre-call values,
    so it is safe for a partially run stage): after compensate, failing stage first."""

    async def boom(sctx: BoardStageContext) -> None:
        raise ValueError("broke")

    def undo(n: int) -> Callable[[BoardStageContext], Any]:
        async def run(sctx: BoardStageContext) -> None:
            log.append(f"undo {n}")

        return run

    cs = _cs([_stage(log, 1, undo=undo(1)), BoardStage("s2", boom, undo=undo(2))])
    with pytest.raises(ValueError):
        await apply_board_change_set(_ctx(tmp_path, FakeSession(log)), cs)
    i = log.index("compensate")
    assert [x for x in log[i:] if x.startswith("undo")] == ["undo 2", "undo 1"]


async def test_restart_during_stages_skips_compensation(
    tmp_path: Path, log: list[str], spy: Spy
) -> None:
    from copperchat.kicad_session import KicadUnavailable

    session = FakeSession(log)

    async def restart(sctx: BoardStageContext) -> None:
        session.generation += 1
        raise ValueError("lost")

    with pytest.raises(KicadUnavailable):
        await apply_board_change_set(_ctx(tmp_path, session), _cs([BoardStage("s", restart)]))
    assert spy.compensations == 0


async def test_temp_dirs_removed_on_all_paths(tmp_path: Path, log: list[str], spy: Spy) -> None:
    def make(name: str, **kw: Any) -> tuple[BoardChangeSet, Path, asyncio.Event]:
        d = tmp_path / name
        entered = asyncio.Event()

        async def mk(sctx: BoardStageContext) -> None:
            d.mkdir()
            (d / "x.dsn").write_text("x")

        async def hang(sctx: BoardStageContext) -> None:
            entered.set()
            await asyncio.Event().wait()

        stages = (
            [BoardStage("mk", mk), BoardStage("hang", hang)]
            if kw.pop("hang", False)
            else [BoardStage("mk", mk)]
        )
        cs = _cs(stages, temp_dirs=[d], **kw)
        return cs, d, entered

    cs, d, _ = make("fill-ok")
    await apply_board_change_set(_ctx(tmp_path, FakeSession(log)), cs)
    assert not d.exists()

    cs, d, _ = make("fill-fail", fail_after=1)
    with pytest.raises(RuntimeError):
        await apply_board_change_set(_ctx(tmp_path, FakeSession(log)), cs)
    assert not d.exists()

    cs, d, entered = make("fill-cancel", hang=True)
    task = asyncio.ensure_future(apply_board_change_set(_ctx(tmp_path, FakeSession(log)), cs))
    await entered.wait()
    assert d.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not d.exists()


async def test_stage_drc_passes_long_timeout(
    tmp_path: Path, log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession(log)
    asked: list[float] = []

    def fake_drc(h: Any, *, timeout_s: float = 60.0) -> Any:
        asked.append(timeout_s)
        return "result"

    monkeypatch.setattr(api, "run_drc_task", fake_drc)
    sctx = BoardStageContext(_ctx(tmp_path, session), {})
    assert await sctx.drc(timeout_s=60) == "result"
    assert await sctx.drc(timeout_s=5) == "result"
    assert session.timeouts == [105, 50]
    assert asked == [60, 5]


async def test_stage_refill_passes_long_timeout(
    tmp_path: Path, log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = FakeSession(log)
    asked: list[tuple[Any, float]] = []

    def fake_refill(h: Any, *, timeout_s: float) -> None:
        asked.append((h, timeout_s))

    monkeypatch.setattr(api, "refill", fake_refill)
    sctx = BoardStageContext(_ctx(tmp_path, session), {})
    await sctx.refill(timeout_s=60)
    assert session.timeouts == [api.refill_call_timeout(60)] == [105]
    assert asked == [(session.handle, 60)]


async def test_creates_board_refused_without_reload(tmp_path: Path, log: list[str]) -> None:
    ctx = _ctx(tmp_path, FakeSession(log))
    ctx.project = replace(ctx.project, pcb_file=None)
    with pytest.raises(RuntimeError, match="reload_project"):
        await apply_board_change_set(ctx, _cs([_stage(log, 1)], creates_board=True))
    assert log == []


async def test_creates_board_path_in_the_way_refused(tmp_path: Path, log: list[str]) -> None:
    session = FakeSession(log)
    ctx = _ctx(tmp_path, session)
    ctx.project = replace(ctx.project, pcb_file=None)

    async def reload() -> Any:
        raise AssertionError("never reloaded")

    ctx.reload_project = reload
    (tmp_path / "demo.kicad_pcb").symlink_to(tmp_path / "elsewhere.kicad_pcb")
    with pytest.raises(StructuredToolError) as info:
        await apply_board_change_set(ctx, _cs([_stage(log, 1)], creates_board=True))
    assert info.value.code == "output_path_unsafe"
    assert info.value.details == {"path": "demo.kicad_pcb"}
    assert session.timeouts == [] and log == []


# -- fix round 1 ----------------------------------------------------------------------------


class CreationRig:
    """A boardless context with a fake create (writes the file like KiCad would) and a
    fake reload (re-discovers by looking at the disk)."""

    def __init__(self, tmp_path: Path, log: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
        self.session = FakeSession(log)
        self.ctx = _ctx(tmp_path, self.session)
        self.ctx.project = replace(self.ctx.project, pcb_file=None)
        self.pcb = tmp_path / "demo.kicad_pcb"
        self.reloads = 0
        self.removed: list[list[str]] = []
        self.reported: list[Exception] = []
        self.ctx.on_document_inconsistent = self.reported.append

        def create(h: Any, path: Path) -> None:
            path.write_text("(kicad_pcb)")

        async def reload() -> Any:
            self.reloads += 1
            pcb = self.pcb if self.pcb.exists() else None
            return replace(self.ctx.project, pcb_file=pcb), self.session, 5

        real_remove = board_engine.remove_created

        def remove(project_dir: Path, rels: Any) -> None:
            self.removed.append(list(rels))
            real_remove(project_dir, rels)

        monkeypatch.setattr(api, "create_board_document", create)
        monkeypatch.setattr(board_engine, "remove_created", remove)
        self.ctx.reload_project = reload


async def test_creation_failure_removes_board_and_reloads_twice(
    tmp_path: Path, log: list[str], spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = CreationRig(tmp_path, log, monkeypatch)
    with pytest.raises(RuntimeError, match="fail_after"):
        await apply_board_change_set(
            rig.ctx, _cs([_stage(log, 1)], creates_board=True, fail_after=1)
        )
    assert rig.removed == [["demo.kicad_pcb"]]
    assert rig.reloads == 2
    assert not rig.pcb.exists() and rig.ctx.project.pcb_file is None
    assert rig.ctx.reloaded_generation == 5


async def test_cancel_with_failing_board_cleanup_keeps_cancel(
    tmp_path: Path, log: list[str], spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = CreationRig(tmp_path, log, monkeypatch)

    def broken(project_dir: Path, rels: Any) -> None:
        raise ValueError("cannot remove")

    monkeypatch.setattr(board_engine, "remove_created", broken)
    entered = asyncio.Event()

    async def hang(sctx: BoardStageContext) -> None:
        entered.set()
        await asyncio.Event().wait()

    cs = _cs([BoardStage("hang", hang)], creates_board=True)
    task = asyncio.ensure_future(apply_board_change_set(rig.ctx, cs))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(rig.reported) == 1 and isinstance(rig.reported[0], DocumentInconsistent)
    assert rig.reported[0].leftovers == ["demo.kicad_pcb"]


async def test_board_left_after_removal_is_a_leftover(
    tmp_path: Path, log: list[str], spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = CreationRig(tmp_path, log, monkeypatch)
    monkeypatch.setattr(board_engine, "remove_created", lambda d, rels: None)  # skipped
    cs = _cs([_stage(log, 1)], creates_board=True, fail_after=1)
    with pytest.raises(DocumentInconsistent) as info:
        await apply_board_change_set(rig.ctx, cs)
    assert info.value.leftovers == ["demo.kicad_pcb"]


async def test_failing_result_read_compensates(
    tmp_path: Path, log: list[str], spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def snapshot(h: Any, mode: str) -> CopperSnapshot:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("read failed")
        return _snap()

    monkeypatch.setattr(board_engine, "take_snapshot", snapshot)
    with pytest.raises(ValueError, match="read failed"):
        await apply_board_change_set(_ctx(tmp_path, FakeSession(log)), _cs([_stage(log, 1)]))
    assert spy.compensations == 1


async def test_cancel_after_non_compensable_stage_is_reported(
    tmp_path: Path, log: list[str], spy: Spy
) -> None:
    reported: list[Exception] = []
    entered = asyncio.Event()

    async def hang(sctx: BoardStageContext) -> None:
        entered.set()
        await asyncio.Event().wait()

    ctx = _ctx(tmp_path, FakeSession(log), on_document_inconsistent=reported.append)
    cs = _cs([_stage(log, 1, compensable=False), BoardStage("hang", hang)])
    task = asyncio.ensure_future(apply_board_change_set(ctx, cs))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spy.compensations == 0
    assert len(reported) == 1 and isinstance(reported[0], DocumentInconsistent)
    assert "History panel" in str(reported[0])


async def test_stage_call_passes_timeout(tmp_path: Path, log: list[str]) -> None:
    session = FakeSession(log)
    sctx = BoardStageContext(_ctx(tmp_path, session), {})
    assert await sctx.call(lambda h: 1) == 1
    assert await sctx.call(lambda h: 2, timeout=240) == 2
    assert session.timeouts == [None, 240]


async def test_board_reads_pass_the_board_read_timeout(
    tmp_path: Path, log: list[str], spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshots (before, after, compensation) and `board_model` serialise the
    whole board: each passes BOARD_READ_TIMEOUT_S, never the 30 s default."""
    from copperchat.kicad_tools.board import read

    session = FakeSession(log)
    await apply_board_change_set(_ctx(tmp_path, session), _cs([_stage(log, 1)]))
    assert session.timeouts == [read.BOARD_READ_TIMEOUT_S] * 2

    session = FakeSession(log)
    with pytest.raises(RuntimeError):
        await apply_board_change_set(_ctx(tmp_path, session), _cs([_stage(log, 1)], fail_after=1))
    assert session.timeouts == [read.BOARD_READ_TIMEOUT_S] * 3  # snapshot, restore, leftovers

    session = FakeSession(log)
    monkeypatch.setattr(read, "load_board_model", lambda h, project: "model")
    assert await read.board_model(_ctx(tmp_path, session)) == "model"
    assert session.timeouts == [read.BOARD_READ_TIMEOUT_S]


async def test_board_creation_rechecks_the_path_under_the_lock(
    tmp_path: Path, log: list[str], spy: Spy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Something appearing at the board path between the first check and the session
    call is refused inside the call, before CreateDocument; nothing is removed."""
    rig = CreationRig(tmp_path, log, monkeypatch)
    created: list[Path] = []
    monkeypatch.setattr(api, "create_board_document", lambda h, path: created.append(path))
    real_call = rig.session.call

    async def call(fn: Callable[[Any], Any], *, timeout: float | None = None) -> Any:
        if not rig.pcb.is_symlink():
            rig.pcb.symlink_to(tmp_path / "elsewhere.kicad_pcb")
        return await real_call(fn, timeout=timeout)

    monkeypatch.setattr(rig.session, "call", call)
    with pytest.raises(StructuredToolError) as info:
        await apply_board_change_set(rig.ctx, _cs([_stage(log, 1)], creates_board=True))
    assert info.value.code == "output_path_unsafe"
    assert created == [] and rig.removed == [] and rig.reloads == 0
    assert rig.pcb.is_symlink()


async def test_undo_runs_before_the_leftover_read(tmp_path: Path, log: list[str], spy: Spy) -> None:
    """P7: the read-back of the compensation comes after every stage undo — an undo may
    re-create an item a stage deleted (a zone with its KIID)."""

    async def undo(sctx: BoardStageContext) -> None:
        log.append("undo 1")

    cs = _cs([_stage(log, 1, undo=undo)], fail_after=1)
    with pytest.raises(RuntimeError):
        await apply_board_change_set(_ctx(tmp_path, FakeSession(log)), cs)
    assert log.index("compensate") < log.index("undo 1") < log.index("leftovers")
