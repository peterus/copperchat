"""(a) Does the SDK pull streaming input lazily at turn boundaries?"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    create_sdk_mcp_server,
    tool,
)

from harness import (
    Recorder,
    base_options,
    collect,
    log,
    now,
    require_token,
    sdk_versions,
    text_of,
    user_msg,
)


async def variant_prequeued(rec: Recorder) -> None:
    pulls: list[float] = []

    async def gen():
        for i in range(3):
            pulls.append(now())
            log(f"a1 generator: yielding item {i}")
            yield user_msg(f"Reply with exactly TOKEN-{i} and nothing else.")

    client = ClaudeSDKClient(base_options())
    await client.connect(gen())
    got = await collect(client, until_results=3)
    await client.disconnect()
    result_ts = [t for t, _ in got.results]
    lazy = len(result_ts) >= 2 and all(pulls[i + 1] >= result_ts[i] for i in range(2))
    rec.check("a1_pull_after_previous_result", lazy, {"pulls": pulls, "results": result_ts})
    rec.check(
        "a1_one_result_per_item",
        len(got.results) == 3,
        {"n_results": len(got.results), "texts": got.texts_per_turn},
    )
    rec.check(
        "a1_turn_i_answers_item_i",
        [f"TOKEN-{i}" in t for i, t in enumerate(got.texts_per_turn)] == [True, True, True],
        got.texts_per_turn,
    )


def slow_tool_server(started: asyncio.Event):
    @tool("slow_echo", "Waits 6 seconds, then echoes the given word.", {"word": str})
    async def slow_echo(args: dict[str, Any]) -> dict[str, Any]:
        log("a2 slow_echo started")
        started.set()
        await asyncio.sleep(6)
        log("a2 slow_echo finished")
        return {"content": [{"type": "text", "text": f"echo:{args['word']}"}]}

    return create_sdk_mcp_server("spike", tools=[slow_echo])


async def variant_midturn(rec: Recorder) -> None:
    started = asyncio.Event()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    pulls: list[float] = []

    async def gen():
        while (item := await queue.get()) is not None:
            pulls.append(now())
            log(f"a2 generator: yielded {item['message']['content'][:40]!r}")
            yield item

    opts = base_options(
        mcp_servers={"spike": slow_tool_server(started)},
        allowed_tools=["mcp__spike__slow_echo"],
    )
    client = ClaudeSDKClient(opts)
    await queue.put(user_msg("Call slow_echo with word=alpha, then reply with its output only."))
    await client.connect(gen())
    reader = asyncio.create_task(collect(client, until_results=2, quiet_s=40))
    await asyncio.wait_for(started.wait(), 120)
    push_t = now()
    await queue.put(user_msg("Reply with exactly TOKEN-B and nothing else."))
    got = await reader
    await queue.put(None)
    await client.disconnect()
    texts = got.texts_per_turn
    rec.note(
        "a2_timeline",
        {"push_t": push_t, "pulls": pulls, "results": [t for t, _ in got.results], "texts": texts},
    )
    rec.check("a2_second_item_gets_own_turn", len(got.results) == 2, len(got.results))
    rec.check(
        "a2_turn1_not_absorbing_item2",
        bool(texts) and "TOKEN-B" not in texts[0] and "echo:alpha" in texts[0],
        texts,
    )


async def variant_gated(rec: Recorder) -> None:
    turn_done = asyncio.Event()
    pulls: list[float] = []

    async def gen():
        for i in range(3):
            if i > 0:
                await turn_done.wait()
                turn_done.clear()
            pulls.append(now())
            yield user_msg(f"Reply with exactly GATED-{i} and nothing else.")

    client = ClaudeSDKClient(base_options())
    await client.connect(gen())
    results: list[float] = []
    texts: list[str] = []
    buf: list[str] = []
    async with asyncio.timeout(240):
        async for msg in client.receive_messages():
            if isinstance(msg, AssistantMessage):
                buf.append(text_of(msg))
            if isinstance(msg, ResultMessage):
                results.append(now())
                texts.append("".join(buf))
                buf = []
                turn_done.set()
                if len(results) == 3:
                    break
    await client.disconnect()
    rec.check(
        "a3_gated_pull_after_result",
        all(pulls[i + 1] >= results[i] for i in range(2)),
        {"pulls": pulls, "results": results},
    )
    rec.check(
        "a3_gated_one_turn_per_item",
        [f"GATED-{i}" in t for i, t in enumerate(texts)] == [True, True, True],
        texts,
    )


async def main() -> int:
    require_token()
    rec = Recorder("a_lazy_pull")
    rec.note("versions", sdk_versions())
    for variant in (variant_prequeued, variant_midturn, variant_gated):
        try:
            await variant(rec)
        except Exception as exc:  # noqa: BLE001 — a crash is a finding, keep going
            rec.check(f"{variant.__name__}_no_exception", False, repr(exc))
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
