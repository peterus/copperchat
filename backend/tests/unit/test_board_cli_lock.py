"""BOARD_CLI_LOCK is one semaphore per event loop (R2: a module-level asyncio.Semaphore
that once had a waiter stays bound to that loop, so the next test's loop failed)."""

from __future__ import annotations

import asyncio

from copperchat.views.board_render import BOARD_CLI_LOCK


async def _contend() -> list[str]:
    order: list[str] = []

    async def hold(name: str) -> None:
        async with BOARD_CLI_LOCK:
            order.append(f"{name}+")
            await asyncio.sleep(0.01)
            order.append(f"{name}-")

    await asyncio.gather(hold("a"), hold("b"))
    assert not BOARD_CLI_LOCK.locked()
    return order


def test_serialises_within_a_loop_and_works_across_loops() -> None:
    for _ in range(2):  # a second loop after one with a waiter must not fail
        order = asyncio.run(_contend())
        assert order in (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"])
    assert not BOARD_CLI_LOCK.locked()  # outside any loop
