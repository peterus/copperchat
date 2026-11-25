"""Router performance budgets (P5a Task 5). Measured values go into the plan index's
"As built (P5a)"."""

from __future__ import annotations

import random
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from copperchat.board import _selftest
from copperchat.board.router import RouteTooLarge, prune_model, solve
from copperchat.board.worker import WORKER_MEMORY_LIMIT_BYTES, run_cpu, shutdown_worker
from copperchat.errors import StructuredToolError

from .router_maps import add_path, exact_clearance_ok, perf_board

RecordProperty = Callable[[str, Any], None]


@pytest.fixture
async def _clean_worker() -> AsyncIterator[None]:
    shutdown_worker()
    yield
    shutdown_worker()


def test_15_short_nets_budget(record_property: RecordProperty) -> None:
    m, reqs = perf_board(
        random.Random(4242), size=30, n_parts=20, n_nets=15, layers=("F.Cu", "B.Cu"), net_len=(3, 10)
    )
    assert len(reqs) == 15
    per_net: list[float] = []
    t0 = time.monotonic()
    for k, r in enumerate(reqs):
        t = time.monotonic()
        path = solve(m, r)
        per_net.append(round(time.monotonic() - t, 3))
        assert path is not None, r.net
        assert exact_clearance_ok(m, path, r)
        m = add_path(m, r, path, f"n{k}")
    total = time.monotonic() - t0
    record_property("router_15_nets_s", round(total, 2))
    record_property("router_15_nets_per_net_s", per_net)
    assert total < 10.0


@pytest.mark.usefixtures("_clean_worker")
async def test_large_board_budget(record_property: RecordProperty) -> None:
    layers = ("F.Cu", "B.Cu", "In1.Cu", "In2.Cu")
    m, reqs = perf_board(random.Random(777), size=100, n_parts=80, n_nets=40, layers=layers, net_len=(5, 40))
    assert len(m.parts) == 80 and len(reqs) == 40
    routed = 0
    t0 = time.monotonic()
    for k, r in enumerate(reqs):
        try:
            path = await run_cpu(solve, prune_model(m, r), r, timeout_s=60)
        except StructuredToolError as exc:  # board_compute_timeout
            pytest.fail(f"{r.net}: {exc}")
        except RouteTooLarge as exc:
            pytest.fail(f"{r.net}: window of {exc.cells} cells")
        if path is not None:
            assert exact_clearance_ok(m, path, r)
            m = add_path(m, r, path, f"n{k}")
            routed += 1
    total = time.monotonic() - t0
    peak = await run_cpu(_selftest.peak_rss, timeout_s=30)
    record_property("router_large_s", round(total, 1))
    record_property("router_large_routed", routed)
    record_property("router_worker_peak_rss_mb", round(peak / 2**20, 1))
    print(f"large board: {total:.1f}s, {routed}/40 routed, worker peak RSS {peak / 2**20:.1f} MB")
    assert total < 120.0
    assert routed >= 36
    assert peak < WORKER_MEMORY_LIMIT_BYTES
