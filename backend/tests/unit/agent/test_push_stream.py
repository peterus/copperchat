# backend/tests/unit/agent/test_push_stream.py
import asyncio

from copperchat.agent.events import QueuedItem
from copperchat.agent.push_stream import PushStream


def item(i: int) -> QueuedItem:
    return QueuedItem(message_id=i, client_message_id=f"c{i}", conversation_id="c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0", text=f"m{i}")


async def _next(it, timeout: float = 0.2):  # type: ignore[no-untyped-def]
    return await asyncio.wait_for(it.__anext__(), timeout)


async def test_first_item_immediately_next_only_after_release():
    s: PushStream[QueuedItem] = PushStream()
    s.push(item(1))
    s.push(item(2))
    it = aiter(s)
    assert (await _next(it)).message_id == 1
    pending = asyncio.ensure_future(it.__anext__())
    await asyncio.sleep(0.05)
    assert not pending.done(), "second item must wait for release_next()"
    assert s.pending_count() == 1
    s.release_next()
    assert (await asyncio.wait_for(pending, 0.2)).message_id == 2


async def test_queue_first_drain_before_close():
    s: PushStream[QueuedItem] = PushStream()
    s.push(item(1))
    s.request_close()
    it = aiter(s)
    assert (await _next(it)).message_id == 1
    s.release_next()
    try:
        await _next(it)
        raise AssertionError("expected end of stream")
    except StopAsyncIteration:
        pass
    assert s.closed is True
    assert s.push(item(9)) is False, "push after close must fail so the caller starts a new burst"


async def test_push_after_close_request_reopens():
    s: PushStream[QueuedItem] = PushStream()
    s.push(item(1))
    it = aiter(s)
    await _next(it)
    s.request_close()
    assert s.push(item(2)) is True
    s.release_next()
    assert (await _next(it)).message_id == 2
    assert s.closed is False


async def test_drop_removes_only_unpulled_items():
    s: PushStream[QueuedItem] = PushStream()
    for i in (1, 2, 3):
        s.push(item(i))
    it = aiter(s)
    await _next(it)
    assert s.drop(1) is False, "already pulled"
    assert s.drop(2) is True
    assert s.drop(2) is False
    assert s.pending_ids() == [3]
    s.release_next()
    assert (await _next(it)).message_id == 3


async def test_close_request_wakes_waiting_consumer():
    s: PushStream[QueuedItem] = PushStream()
    it = aiter(s)
    waiter = asyncio.ensure_future(it.__anext__())
    await asyncio.sleep(0.01)
    s.request_close()
    try:
        await asyncio.wait_for(waiter, 0.2)
        raise AssertionError("expected end of stream")
    except StopAsyncIteration:
        pass


async def test_seal_ends_after_drain_and_refuses_pushes():
    s: PushStream[QueuedItem] = PushStream("c" * 32)
    assert s.conversation_id == "c" * 32
    s.push(item(1))
    s.seal()
    assert s.sealed and s.push(item(2)) is False, "a sealed stream never reopens"
    it = aiter(s)
    assert (await _next(it)).message_id == 1
    s.release_next()
    try:
        await _next(it)
    except StopAsyncIteration:
        pass
    else:
        raise AssertionError("a sealed stream ends once drained")
    assert s.closed
