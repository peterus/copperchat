"""`pcb_metrics` on the `sp4_synth` board (SP4 P3a Task 5): the metrics of the freshly
imported board (no outline, nothing routed), the time budget, and `only` without M2/M3
skipping the DRC task. The tests share one api-server (`synth_board`, module scope)."""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from typing import Any

import pytest

from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.board import analysis, api
from copperchat.kicad_tools.board.analysis import pcb_metrics
from tests.support.edit import edit_ctx

pytestmark = pytest.mark.kicad


async def _metrics(s: KicadSession, **args: Any) -> dict[str, Any]:
    data = (await pcb_metrics(edit_ctx(s), args)).data
    assert data is not None
    return data


def _by_id(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in data["metrics"]}


@pytest.mark.asyncio(loop_scope="module")
async def test_metrics_on_synth(synth_board: KicadSession) -> None:
    data = await _metrics(synth_board)
    assert data["stage"] == "placement"
    m = _by_id(data)
    assert list(m) == [f"M{i}" for i in range(1, 17)]
    assert m["M1"]["verdict"] == "n/a"  # no outline yet
    drc = await synth_board.call(
        lambda h: api.run_drc_task(h, timeout_s=60.0), timeout=api.drc_call_timeout(60.0)
    )
    assert drc.unconnected > 0
    assert m["M2"]["value"] == drc.unconnected
    assert m["M2"]["verdict"] == "fail"
    assert m["M4"]["value"] == 0
    assert m["M4"]["verdict"] == "pass"
    assert data["hard_failures"] == sum(1 for x in m.values() if x["verdict"] == "fail")
    assert len(data["placement_hash"]) == len(data["board_hash"]) == 64


@pytest.mark.asyncio(loop_scope="module")
async def test_metrics_budget(
    synth_board: KicadSession, record_property: Callable[[str, object], None]
) -> None:
    await _metrics(synth_board)  # warm-up: worker start, imports
    times = []
    for _ in range(5):
        start = time.monotonic()
        await _metrics(synth_board)
        times.append(time.monotonic() - start)
    median = statistics.median(times)
    record_property("pcb_metrics_s", round(median, 3))
    print(f"pcb_metrics median {median:.3f} s over {[round(t, 3) for t in times]}")
    assert median < 2.0  # CI-generous; spec target 0.1-0.5 s


@pytest.mark.asyncio(loop_scope="module")
async def test_metrics_only_m4(synth_board: KicadSession, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    real = api.run_drc_task

    def spy(*a: Any, **kw: Any) -> Any:
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(analysis.api, "run_drc_task", spy)
    data = await _metrics(synth_board, only=["M4"])
    assert [x["id"] for x in data["metrics"]] == ["M4"]
    assert calls == []
    await _metrics(synth_board, only=["M2"])
    assert len(calls) == 1
