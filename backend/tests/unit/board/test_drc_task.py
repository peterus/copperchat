"""`api.run_drc_task` against a fake protobuf client (B-C3): markers, states, the stale
task on the session's DRC slot."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from kipy.proto.board import board_rules_pb2, board_tasks_pb2
from kipy.proto.common import tasks_pb2
from kipy.proto.common.types import base_types_pb2

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.api import DRC_STALE_GRACE_S, DrcTimeout, run_drc_task

RUNNING, DONE, FAILED = tasks_pb2.TS_RUNNING, tasks_pb2.TS_COMPLETED, tasks_pb2.TS_FAILED


class Clock:
    def __init__(self) -> None:
        self.now = 50.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(api, "monotonic", c.monotonic)
    monkeypatch.setattr(api, "sleep", c.sleep)
    return c


def _marker(kind: str, severity: int, kiids: list[str], *, excluded: bool = False) -> Any:
    m = board_rules_pb2.DrcMarker(
        error_type=board_rules_pb2.DrcErrorType.Value(kind),
        description=f"{kind} text",
        severity=severity,
        excluded=excluded,
    )
    m.position.x_nm, m.position.y_nm = 1_500_000, -2_000_000
    for k in kiids:
        m.items.add(value=k)
    return m


class FakeClient:
    """`states[token]`: a function of the poll count -> task state."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.sent: list[tuple[str, str]] = []
        self.states: dict[str, Callable[[int], int]] = {}
        self.polls: dict[str, int] = {}
        self.next_state: Callable[[int], int] = lambda n: DONE
        self.violations: list[Any] = []
        self.started = 0

    def send(self, msg: Any, response_type: Any) -> Any:
        name = type(msg).__name__
        if isinstance(msg, board_tasks_pb2.StartDrcTask):
            assert msg.report_all_track_errors
            self.started += 1
            token = f"t{self.started}"
            self.states[token] = self.next_state
            self.polls[token] = 0
            self.sent.append((name, token))
            return tasks_pb2.StartTaskResponse(token=tasks_pb2.TaskToken(value=token))
        if isinstance(msg, tasks_pb2.GetTaskStatus):
            token = msg.token.value
            self.sent.append((name, token))
            n = self.polls[token]
            self.polls[token] = n + 1
            state = self.states[token](n)
            err = "DRC exploded" if state == FAILED else ""
            return tasks_pb2.TaskStatusResponse(state=state, error_message=err)
        if isinstance(msg, board_tasks_pb2.GetDrcTaskResult):
            self.sent.append((name, msg.token.value))
            return board_tasks_pb2.DrcTaskResult(violations=self.violations)
        raise AssertionError(name)


class FakeBoard:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.document = base_types_pb2.DocumentSpecifier()


class FakeKicad:
    def __init__(self, client: FakeClient) -> None:
        self.board = FakeBoard(client)

    def get_board(self) -> FakeBoard:
        return self.board


def _handle(client: FakeClient, generation: int = 4, slot: Any = None) -> KicadHandle:
    return KicadHandle(FakeKicad(client), frozenset({"pcb"}), generation=generation, drc_slot=slot)


def test_markers_and_unconnected(clock: Clock) -> None:
    client = FakeClient(clock)
    client.next_state = lambda n: RUNNING if n < 3 else DONE
    client.violations = [
        _marker("DRCET_UNCONNECTED_ITEMS", base_types_pb2.RS_ERROR, ["a", "b"]),
        _marker("DRCET_UNCONNECTED_ITEMS", base_types_pb2.RS_ERROR, ["c", "d"]),
        _marker("DRCET_CLEARANCE", base_types_pb2.RS_WARNING, ["e"]),
        _marker("DRCET_CLEARANCE", base_types_pb2.RS_ERROR, ["f"], excluded=True),
    ]
    result = run_drc_task(_handle(client), timeout_s=10)
    assert result.unconnected == 2
    assert [m.type for m in result.markers] == [
        "unconnected_items",
        "unconnected_items",
        "clearance",
        "clearance",
    ]
    assert [m.severity for m in result.markers] == ["error", "error", "warning", "exclusion"]
    assert result.markers[0].kiids == ("a", "b")
    assert result.markers[0].pos == (1.5, -2.0)
    assert result.markers[0].text == "DRCET_UNCONNECTED_ITEMS text"
    assert result.seconds == pytest.approx(0.15)


def test_failed_task_is_drc_rejected(clock: Clock) -> None:
    client = FakeClient(clock)
    client.next_state = lambda n: FAILED
    with pytest.raises(StructuredToolError) as info:
        run_drc_task(_handle(client))
    assert info.value.code == "drc_rejected" and not isinstance(info.value, DrcTimeout)
    assert "DRC exploded" in info.value.why


def test_timeout_records_pending_and_next_call_waits(clock: Clock) -> None:
    client = FakeClient(clock)
    client.next_state = lambda n: RUNNING if n < 100 else DONE  # ~5 s of polls
    h = _handle(client)
    with pytest.raises(DrcTimeout) as info:
        run_drc_task(h, timeout_s=1.0)
    assert info.value.code == "drc_timeout" and info.value.details == {"timeout_s": 1.0}
    assert h.drc_slot.get() == (4, "t1")

    # A fresh handle on the same slot (the next session.call) waits for t1 first.
    client.states["t1"] = lambda n: DONE  # the stale task finished meanwhile
    client.next_state = lambda n: DONE
    h2 = _handle(client, slot=h.drc_slot)
    result = run_drc_task(h2, timeout_s=60)
    assert result.unconnected == 0
    assert h2.drc_slot.get() is None
    assert client.started == 2
    first_start = client.sent.index(("StartDrcTask", "t2"))
    assert client.sent[first_start - 1] == ("GetTaskStatus", "t1")  # t1 left RUNNING before


def test_stale_task_still_running_raises_without_second_task(clock: Clock) -> None:
    client = FakeClient(clock)
    client.next_state = lambda n: RUNNING
    h = _handle(client)
    with pytest.raises(DrcTimeout):
        run_drc_task(h, timeout_s=0.5)
    start = clock.now
    with pytest.raises(DrcTimeout):
        run_drc_task(_handle(client, slot=h.drc_slot), timeout_s=120)
    assert client.started == 1
    assert DRC_STALE_GRACE_S <= clock.now - start < DRC_STALE_GRACE_S + 1
    assert h.drc_slot.get() == (4, "t1")  # still pending


def test_stale_grace_never_exceeds_own_timeout(clock: Clock) -> None:
    client = FakeClient(clock)
    client.next_state = lambda n: RUNNING
    h = _handle(client)
    with pytest.raises(DrcTimeout):
        run_drc_task(h, timeout_s=0.5)
    start = clock.now
    with pytest.raises(DrcTimeout):
        run_drc_task(_handle(client, slot=h.drc_slot), timeout_s=5)
    assert clock.now - start < 5.2 and client.started == 1


def test_pending_of_other_generation_is_ignored(clock: Clock) -> None:
    client = FakeClient(clock)
    h = _handle(client, generation=5)
    h.drc_slot.set((4, "old"))
    run_drc_task(h)
    assert all(token != "old" for _, token in client.sent)
    assert h.drc_slot.get() is None
