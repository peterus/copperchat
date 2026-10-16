"""(b) interrupt() mid-turn (text streaming and inside an MCP tool), then continue."""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    create_sdk_mcp_server,
    tool,
)

from harness import Recorder, base_options, log, now, require_token, text_of, user_msg


async def run_case(
    rec: Recorder,
    case: str,
    first_prompt: str,
    opts_extra: dict[str, Any],
    trigger: asyncio.Event | None,
) -> None:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    turn_done = asyncio.Event()

    async def gen():
        first = True
        while True:
            if not first:
                await turn_done.wait()
                turn_done.clear()
            first = False
            item = await queue.get()
            if item is None:
                return
            yield item

    client = ClaudeSDKClient(base_options(include_partial_messages=True, **opts_extra))
    await queue.put(user_msg(first_prompt))
    await client.connect(gen())
    results: list[ResultMessage] = []
    texts: list[str] = []
    buf: list[str] = []
    interrupt_t: float | None = None
    first_delta = asyncio.Event()

    async def interrupter() -> None:
        nonlocal interrupt_t
        await (trigger.wait() if trigger else first_delta.wait())
        await asyncio.sleep(1.0)
        interrupt_t = now()
        log(f"{case}: calling interrupt()")
        await client.interrupt()
        await queue.put(user_msg("Reply with exactly AFTER-INTERRUPT and nothing else."))

    task = asyncio.create_task(interrupter())
    result_ts: list[float] = []
    async with asyncio.timeout(240):
        async for msg in client.receive_messages():
            if isinstance(msg, StreamEvent):
                first_delta.set()
            if isinstance(msg, AssistantMessage):
                buf.append(text_of(msg))
            if isinstance(msg, ResultMessage):
                results.append(msg)
                result_ts.append(now())
                texts.append("".join(buf))
                buf = []
                turn_done.set()
                if len(results) == 2:
                    break
    await queue.put(None)
    await task
    await client.disconnect()
    r1 = results[0]
    rec.note(
        f"{case}_first_result",
        {
            "subtype": r1.subtype,
            "is_error": r1.is_error,
            "stop_reason": r1.stop_reason,
            "terminal_reason": getattr(r1, "terminal_reason", None),
            "latency_interrupt_to_result_s": round(result_ts[0] - (interrupt_t or 0), 3),
            "partial_text_len": len(texts[0]),
        },
    )
    rec.check(
        f"{case}_interrupt_produces_result",
        interrupt_t is not None and result_ts[0] >= interrupt_t,
        {"interrupt_t": interrupt_t, "result_t": result_ts[0]},
    )
    rec.check(
        f"{case}_next_item_runs_after_interrupt",
        len(results) == 2 and "AFTER-INTERRUPT" in texts[1],
        texts,
    )
    rec.check(
        f"{case}_same_session_after_interrupt",
        len(results) == 2 and results[0].session_id == results[1].session_id,
        [r.session_id for r in results],
    )


async def main() -> int:
    require_token()
    rec = Recorder("b_interrupt")
    await run_case(
        rec, "b1_text", "Count from 1 to 400, one number per line, no other text.", {}, None
    )

    tool_started = asyncio.Event()
    cancelled = {"value": False}

    @tool("slow_wait", "Waits 30 seconds and returns 'done'.", {})
    async def slow_wait(args: dict[str, Any]) -> dict[str, Any]:
        tool_started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["value"] = True
            raise
        return {"content": [{"type": "text", "text": "done"}]}

    server = create_sdk_mcp_server("spike", tools=[slow_wait])
    await run_case(
        rec,
        "b2_in_tool",
        "Call slow_wait, then say its result.",
        {"mcp_servers": {"spike": server}, "allowed_tools": ["mcp__spike__slow_wait"]},
        tool_started,
    )
    rec.note("b2_tool_coroutine_cancelled", cancelled["value"])
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
