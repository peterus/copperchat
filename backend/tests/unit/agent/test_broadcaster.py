# backend/tests/unit/agent/test_broadcaster.py
import asyncio

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.protocol import RunStartedFrame

P, Q = "a1b2c3d4e5f6", "ffffffffffff"


def frame(n: int, pid: str = P) -> RunStartedFrame:
    return RunStartedFrame(project_id=pid, conversation_id="c" * 32, run_id=f"r{n}")


async def test_fan_out_only_to_same_project():
    b = Broadcaster()
    s1, s2, other = b.subscribe(P), b.subscribe(P), b.subscribe(Q)
    await b.publish(P, frame(1))
    assert (await asyncio.wait_for(s1.__anext__(), 0.1)).run_id == "r1"
    assert (await asyncio.wait_for(s2.__anext__(), 0.1)).run_id == "r1"
    other.close()
    assert [f async for f in other] == []


async def test_slow_subscriber_is_dropped_and_closed():
    b = Broadcaster(max_queue=2)
    slow, fast = b.subscribe(P), b.subscribe(P)
    got: list[str] = []

    async def drain() -> None:
        async for f in fast:
            got.append(f.run_id)  # type: ignore[union-attr]
            if len(got) == 3:
                return

    t = asyncio.create_task(drain())
    for n in range(3):
        await b.publish(P, frame(n))
        await asyncio.sleep(0)
    await asyncio.wait_for(t, 0.5)
    assert got == ["r0", "r1", "r2"]
    assert slow.dropped is True
    assert [f async for f in slow] == []
    assert b.subscriber_count(P) == 1


async def test_close_unsubscribes():
    b = Broadcaster()
    s = b.subscribe(P)
    s.close()
    assert b.subscriber_count(P) == 0
    await b.publish(P, frame(1))
