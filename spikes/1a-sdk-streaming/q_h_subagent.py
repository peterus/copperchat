"""(h) agents= sub-agent with its own tools while the main agent has no built-in tools."""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    SystemMessage,
    ToolPermissionContext,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from harness import WORKDIR, Recorder, base_options, collect, require_token, user_msg

MAGIC = "MAGIC-7731"


@tool("get_magic", "Returns the magic value.", {})
async def get_magic(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": MAGIC}]}


async def run(main_tools: list[str], helper_tools: list[str], prompt: str, guard=None):
    server = create_sdk_mcp_server("spike", tools=[get_magic])
    helper = AgentDefinition(
        description="Helper that fetches data. Use it for any data lookup.",
        prompt="You are a helper. Use your tools to answer, reply tersely.",
        tools=helper_tools,
        model="haiku",
    )

    async def gen():
        yield user_msg(prompt)

    opts = base_options(
        tools=main_tools,
        mcp_servers={"spike": server},
        agents={"helper": helper},
        allowed_tools=["mcp__spike__get_magic", *main_tools],
        can_use_tool=guard,
    )
    client = ClaudeSDKClient(opts)
    await client.connect(gen())
    got = await collect(client, until_results=1, hard_s=300)
    await client.disconnect()
    init = next(m for _, m in got.messages if isinstance(m, SystemMessage) and m.subtype == "init")
    uses = [
        (m.parent_tool_use_id, b.name, b.input)
        for _, m in got.messages
        if isinstance(m, AssistantMessage)
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]
    return init.data.get("tools", []), init.data.get("agents"), uses, got.texts_per_turn[0]


async def main() -> int:
    require_token()
    rec = Recorder("h_subagent")
    ask = "Use the helper sub-agent to get the magic value via get_magic. Reply with the value."

    tools1, agents1, uses1, _text1 = await run([], ["mcp__spike__get_magic"], ask)
    rec.note("h1_init", {"tools": tools1, "agents": agents1, "uses": uses1})
    rec.check(
        "h1_helper_reachable_with_tools_empty", any(u[1] in ("Agent", "Task") for u in uses1), uses1
    )

    delegate = "Agent"
    tools2, _agents2, uses2, text2 = await run([delegate], ["mcp__spike__get_magic"], ask)
    if delegate not in tools2:
        delegate = "Task"
        tools2, _agents2, uses2, text2 = await run([delegate], ["mcp__spike__get_magic"], ask)
    rec.note("h2_delegation_tool_name", delegate)
    rec.check(
        "h2_main_has_only_delegate_and_mcp",
        set(tools2) <= {delegate, "mcp__spike__get_magic"},
        tools2,
    )
    rec.check(
        "h2_helper_used_mcp_tool",
        any(p is not None and n == "mcp__spike__get_magic" for p, n, _ in uses2),
        uses2,
    )
    rec.check("h2_answer_contains_magic", MAGIC in text2, text2)

    seen: list[tuple[str, dict]] = []

    async def guard(name: str, inp: dict[str, Any], ctx: ToolPermissionContext):
        seen.append((name, inp))
        if name == "Write" and "docs/parts/" not in str(inp.get("file_path", "")):
            return PermissionResultDeny(message="writes only under docs/parts/")
        return PermissionResultAllow()

    ask3 = (
        "Use the helper sub-agent: it must fetch https://example.com, then write the page "
        f"title into {WORKDIR}/docs/parts/TEST/notes.md and also try to write it to "
        f"{WORKDIR}/outside.md. Reply with the title."
    )
    tools3, _, uses3, text3 = await run([delegate], ["WebFetch", "Write"], ask3, guard=guard)
    rec.check("h3_main_lacks_webfetch_write", not {"WebFetch", "Write"} & set(tools3), tools3)
    rec.check(
        "h3_helper_used_webfetch",
        any(p is not None and n == "WebFetch" for p, n, _ in uses3),
        uses3,
    )
    rec.check("h3_guard_saw_helper_write", any(n == "Write" for n, _ in seen), seen)
    rec.check("h3_inside_write_ok", (WORKDIR / "docs/parts/TEST/notes.md").exists(), str(WORKDIR))
    rec.check("h3_outside_write_denied", not (WORKDIR / "outside.md").exists(), str(WORKDIR))
    rec.note("h3_answer", text3)

    # h4: owner gate — Agent guarded by subagent_type, guard sees sub-agent calls, parent ids
    calls: list[dict[str, Any]] = []

    async def gate_guard(name: str, inp: dict[str, Any], ctx: ToolPermissionContext):
        calls.append(
            {
                "name": name,
                "subagent_type": inp.get("subagent_type"),
                "agent_id": getattr(ctx, "agent_id", "<no field>"),
            }
        )
        if name == delegate and inp.get("subagent_type") != "helper":
            return PermissionResultDeny(message="only the helper sub-agent may be used")
        return PermissionResultAllow()

    server = create_sdk_mcp_server("spike", tools=[get_magic])
    helper = AgentDefinition(
        description="Helper that fetches data. Use it for any data lookup.",
        prompt="You are a helper. Use your tools to answer, reply tersely.",
        tools=["mcp__spike__get_magic", "WebFetch"],
        model="haiku",
    )

    async def gen4():
        yield user_msg(
            "First try the general-purpose sub-agent to fetch https://example.com. "
            "Then use the helper sub-agent to get the magic value via get_magic. "
            "Reply with the value."
        )

    opts4 = base_options(
        tools=[delegate, "Skill"],
        mcp_servers={"spike": server},
        agents={"helper": helper},
        allowed_tools=[],  # nothing shadows the guard
        can_use_tool=gate_guard,
        include_partial_messages=True,
        forward_subagent_text=True,
    )
    client4 = ClaudeSDKClient(opts4)
    await client4.connect(gen4())
    got4 = await collect(client4, until_results=1, hard_s=300)
    await client4.disconnect()
    agent_ids = {
        b.id
        for _, m in got4.messages
        if isinstance(m, AssistantMessage)
        for b in m.content
        if isinstance(b, ToolUseBlock) and b.name == delegate
    }
    sub_msgs = [m for _, m in got4.messages if getattr(m, "parent_tool_use_id", None)]
    rec.note("h4_calls", calls)
    rec.check(
        "h4a_guard_sees_agent_with_subagent_type",
        any(c["name"] == delegate and c["subagent_type"] == "helper" for c in calls),
        calls,
    )
    rec.check(
        "h4a_general_purpose_denied_by_guard",
        any(c["name"] == delegate and c["subagent_type"] == "general-purpose" for c in calls)
        and not any(
            getattr(m, "parent_tool_use_id", None) and "example.com" in str(m) for m in sub_msgs
        ),
        calls,
    )
    rec.check(
        "h4b_guard_fires_for_subagent_tools",
        any(c["name"] == "mcp__spike__get_magic" for c in calls),
        calls,
    )
    rec.check(
        "h4b_agent_id_distinguishes_main_and_sub",
        all(c["agent_id"] is None for c in calls if c["name"] == delegate)
        and all(
            c["agent_id"] not in (None, "<no field>")
            for c in calls
            if c["name"] == "mcp__spike__get_magic"
        ),
        calls,
    )
    rec.check(
        "h4c_subagent_messages_carry_parent_id",
        bool(sub_msgs) and all(m.parent_tool_use_id in agent_ids for m in sub_msgs),
        [(type(m).__name__, m.parent_tool_use_id) for m in sub_msgs][:20],
    )
    rec.note(
        "h4c_stream_events_with_parent",
        sum(1 for m in sub_msgs if type(m).__name__ == "StreamEvent"),
    )
    # Unparented text deltas while an Agent call is open would leak sub-agent text into the
    # main agent's streaming segment (Phase 5); record how many arrive in that window.
    open_calls: set[str] = set()
    leaked = 0
    for _, m in got4.messages:
        if isinstance(m, AssistantMessage) and not m.parent_tool_use_id:
            open_calls |= {
                b.id for b in m.content if isinstance(b, ToolUseBlock) and b.name == delegate
            }
        if (
            type(m).__name__ == "UserMessage"
            and not getattr(m, "parent_tool_use_id", None)
            and not isinstance(m.content, str)
        ):
            open_calls -= {getattr(b, "tool_use_id", None) for b in m.content}
        if (
            type(m).__name__ == "StreamEvent"
            and open_calls
            and not m.parent_tool_use_id
            and m.event.get("type") == "content_block_delta"
        ):
            leaked += 1
    rec.check("h4c_no_unparented_deltas_during_subagent", leaked == 0, leaked)

    # h5: interrupt during the sub-agent, then a second turn; usage incl. sub-agent tokens.
    # The sub-agent uses a slow in-session MCP tool (not a built-in), so h5 does not depend on
    # whether built-ins are inherited (h3).
    @tool("slow_lookup", "Looks up a value slowly (20 s).", {"key": str})
    async def slow_lookup(args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(20)
        return {"content": [{"type": "text", "text": f"value-of-{args.get('key')}"}]}

    slow_server = create_sdk_mcp_server("spike", tools=[slow_lookup])
    slow_helper = AgentDefinition(
        description="Slow lookup helper. Use it for every lookup.",
        prompt="Look up each key with slow_lookup, one by one, then summarise.",
        tools=["mcp__spike__slow_lookup"],
        model="haiku",
    )
    second = asyncio.Event()

    async def allow_all(name: str, inp: dict[str, Any], ctx: ToolPermissionContext):
        return PermissionResultAllow()

    async def gen5():
        yield user_msg("Use the lookup sub-agent to look up the keys alpha, beta and gamma.")
        await second.wait()
        yield user_msg("Reply with exactly: AFTER")

    opts5 = base_options(
        tools=[delegate],
        mcp_servers={"spike": slow_server},
        agents={"lookup": slow_helper},
        allowed_tools=[],
        can_use_tool=allow_all,
        forward_subagent_text=True,
    )
    client5 = ClaudeSDKClient(opts5)
    await client5.connect(gen5())
    interrupted_at: float | None = None
    got5_msgs: list[tuple[float, Any]] = []
    try:
        async with asyncio.timeout(300):
            async for msg in client5.receive_messages():
                now = asyncio.get_running_loop().time()
                got5_msgs.append((now, msg))
                if interrupted_at is None and getattr(msg, "parent_tool_use_id", None):
                    interrupted_at = now
                    await client5.interrupt()
                if type(msg).__name__ == "ResultMessage":
                    if not second.is_set():
                        second.set()
                    else:
                        break
    except TimeoutError:
        rec.note("h5_timeout", len(got5_msgs))
    await client5.disconnect()
    results = [(t, m) for t, m in got5_msgs if type(m).__name__ == "ResultMessage"]
    first_result_t = results[0][0] if results else float("inf")
    rec.check(
        "h5_interrupt_ends_turn_during_subagent",
        interrupted_at is not None and bool(results),
        [str(m)[:200] for _, m in results],
    )
    rec.check(
        "h5_no_subagent_messages_after_result",
        not any(
            t > first_result_t and getattr(m, "parent_tool_use_id", None)
            for t, m in got5_msgs
            if t < (results[1][0] if len(results) > 1 else 1e18)
        ),
        len(got5_msgs),
    )
    rec.check(
        "h5_next_turn_works", len(results) > 1 and "AFTER" in str(results[1][1]), len(results)
    )
    sub_usage = [
        getattr(m, "usage", None)
        for _, m in got4.messages
        if isinstance(m, AssistantMessage) and m.parent_tool_use_id
    ]
    final4 = next((m for _, m in got4.messages if type(m).__name__ == "ResultMessage"), None)
    rec.note(
        "h5_usage_compare",
        {
            "result_usage": getattr(final4, "usage", None),
            "result_cost": getattr(final4, "total_cost_usd", None),
            "subagent_assistant_usage": sub_usage,
        },
    )
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
