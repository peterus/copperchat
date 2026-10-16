"""(e) RateLimitEvent shape; (f) usage data on ResultMessage (two turns, cumulative or not)."""

from __future__ import annotations

import asyncio
from collections import Counter

from claude_agent_sdk import ClaudeSDKClient, RateLimitEvent, ResultMessage, StreamEvent

from harness import Recorder, base_options, jsonable, require_token, user_msg

USAGE_KEYS = {
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
}


async def main() -> int:
    require_token()
    rec = Recorder("ef_usage_ratelimit")
    turn_done = asyncio.Event()

    async def gen():
        yield user_msg("Reply with the word ONE.")
        await turn_done.wait()  # gated: second prompt only after the first ResultMessage
        yield user_msg("Reply with the word TWO.")

    client = ClaudeSDKClient(base_options(include_partial_messages=True))
    await client.connect(gen())
    msgs: list = []
    async with asyncio.timeout(240):
        async for msg in client.receive_messages():
            msgs.append(msg)
            if isinstance(msg, ResultMessage):
                turn_done.set()
                if sum(isinstance(m, ResultMessage) for m in msgs) == 2:
                    break
    await client.disconnect()

    rec.note("ef_message_kinds", dict(Counter(type(m).__name__ for m in msgs)))
    rl = [m for m in msgs if isinstance(m, RateLimitEvent)]
    rec.note("e_rate_limit_events", [jsonable(e) for e in rl])
    rec.check("e1_rate_limit_event_observed", bool(rl), len(rl))
    if rl:
        info = rl[0].rate_limit_info
        rec.check(
            "e2_status_field",
            info.status in ("allowed", "allowed_warning", "rejected"),
            info.status,
        )
        rec.note("e3_resets_at_type", type(info.resets_at).__name__)
        rec.note("e4_raw", info.raw)
    results = [m for m in msgs if isinstance(m, ResultMessage)]
    rec.check("f1_two_results", len(results) == 2, len(results))
    for i, r in enumerate(results):
        rec.note(
            f"f_result_{i}",
            {
                "usage": r.usage,
                "total_cost_usd": r.total_cost_usd,
                "model_usage": jsonable(r.model_usage),
                "num_turns": r.num_turns,
                "duration_ms": r.duration_ms,
                "duration_api_ms": r.duration_api_ms,
            },
        )
        rec.check(
            f"f2_usage_keys_turn{i}",
            USAGE_KEYS <= set((r.usage or {}).keys()),
            sorted((r.usage or {}).keys()),
        )
    if len(results) == 2:
        c0, c1 = results[0].total_cost_usd or 0, results[1].total_cost_usd or 0
        rec.note(
            "f3_cost_cumulative_across_turns", {"turn0": c0, "turn1": c1, "cumulative": c1 > c0}
        )
    deltas = [
        m.event
        for m in msgs
        if isinstance(m, StreamEvent) and m.event.get("type") in ("message_start", "message_delta")
    ]
    rec.note(
        "f4_stream_usage_events",
        [d.get("usage") or d.get("message", {}).get("usage") for d in deltas][:6],
    )
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
