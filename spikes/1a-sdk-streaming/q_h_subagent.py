"""(h) agents= sub-agent with its own tools while the main agent has no built-in tools."""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    AgentDefinition,
    AssistantMessage,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    SystemMessage,
    ToolPermissionContext,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from harness import (
    CONFIG_DIR,
    WORKDIR,
    Recorder,
    base_options,
    collect,
    jsonable,
    require_token,
    user_msg,
)

MAGIC = "MAGIC-7731"


@tool("get_magic", "Returns the magic value.", {})
async def get_magic(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": MAGIC}]}


CALL_NAMES = {"Agent", "Task"}
# Runs 1-2 showed the model sometimes launches the sub-agent in the background (tool result
# "Async agent launched"), which ends the main turn before the sub-agent finishes. Every h run
# disables background tasks, the setting Phase 8 uses (see results/h_foreground_probe.log).
FG_ENV = {"CLAUDE_CONFIG_DIR": str(CONFIG_DIR), "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}
BUILTINS = ("WebFetch", "Write")


async def gate_run(rec: Recorder, mech: str):
    """h4 rows for one guard mechanism; row names get a `_hook` suffix for the run where a
    PreToolUse hook (matcher "Agent") guards delegations and can_use_tool guards the rest."""
    sfx = "" if mech == "can_use_tool" else "_hook"
    calls: list[dict[str, Any]] = []

    def decide(name: str, inp: dict[str, Any], agent_id: Any) -> bool:
        calls.append(
            {
                "name": name,
                "subagent_type": inp.get("subagent_type"),
                "run_in_background": inp.get("run_in_background"),
                "agent_id": agent_id,
            }
        )
        return not (name in CALL_NAMES and inp.get("subagent_type") != "helper")

    async def gate_guard(name: str, inp: dict[str, Any], ctx: ToolPermissionContext):
        if decide(name, inp, getattr(ctx, "agent_id", "<no field>")):
            return PermissionResultAllow()
        return PermissionResultDeny(message="only the helper sub-agent may be used")

    async def gate_hook(inp: Any, tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        ok = decide(inp["tool_name"], inp.get("tool_input") or {}, inp.get("agent_id"))
        if ok:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "only the helper sub-agent",
            }
        }

    server = create_sdk_mcp_server("spike", tools=[get_magic])
    helper = AgentDefinition(
        description="Helper that fetches data. Use it for any data lookup.",
        prompt="You are a helper. Use your tools to answer, reply tersely.",
        tools=["mcp__spike__get_magic"],
        model="haiku",
    )

    async def gen4():
        yield user_msg(
            "First try the general-purpose sub-agent to fetch https://example.com. "
            "Then use the helper sub-agent (in the foreground, not in the background) to get "
            "the magic value via get_magic. Reply with the value."
        )

    extra: dict[str, Any] = (
        {"can_use_tool": gate_guard}
        if mech == "can_use_tool"
        else {
            "hooks": {"PreToolUse": [HookMatcher(matcher="Agent", hooks=[gate_hook])]},
            "can_use_tool": gate_guard,
        }
    )
    opts4 = base_options(
        env=FG_ENV,
        tools=["Agent", "Skill"],
        mcp_servers={"spike": server},
        agents={"helper": helper},
        allowed_tools=[],  # nothing shadows the guard
        include_partial_messages=True,
        forward_subagent_text=True,
        **extra,
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
        if isinstance(b, ToolUseBlock) and b.name in CALL_NAMES
    }
    sub_msgs = [m for _, m in got4.messages if getattr(m, "parent_tool_use_id", None)]
    rec.note(f"h4_calls{sfx}", calls)
    rec.note(f"h4_answer{sfx}", got4.texts_per_turn)
    rec.check(
        f"h4a_guard_sees_agent_with_subagent_type{sfx}",
        any(c["name"] in CALL_NAMES and c["subagent_type"] == "helper" for c in calls),
        calls,
    )
    rec.check(
        f"h4a_general_purpose_denied_by_guard{sfx}",
        any(c["name"] in CALL_NAMES and c["subagent_type"] == "general-purpose" for c in calls)
        and not any("example.com" in str(m) for m in sub_msgs),
        calls,
    )
    rec.check(
        f"h4b_guard_fires_for_subagent_tools{sfx}",
        any(c["name"] == "mcp__spike__get_magic" for c in calls),
        calls,
    )
    rec.check(
        f"h4b_agent_id_distinguishes_main_and_sub{sfx}",
        any(c["name"] in CALL_NAMES for c in calls)
        and all(c["agent_id"] is None for c in calls if c["name"] in CALL_NAMES)
        and any(c["name"] == "mcp__spike__get_magic" for c in calls)
        and all(
            c["agent_id"] not in (None, "<no field>")
            for c in calls
            if c["name"] == "mcp__spike__get_magic"
        ),
        calls,
    )
    rec.check(
        f"h4c_subagent_messages_carry_parent_id{sfx}",
        bool(sub_msgs) and all(m.parent_tool_use_id in agent_ids for m in sub_msgs),
        [(type(m).__name__, m.parent_tool_use_id) for m in sub_msgs][:20],
    )
    rec.note(
        f"h4c_stream_events_with_parent{sfx}",
        sum(1 for m in sub_msgs if type(m).__name__ == "StreamEvent"),
    )
    # Unparented text deltas while an Agent call is open would leak sub-agent text into the
    # main agent's streaming segment (Phase 5); record how many arrive in that window.
    open_calls: set[str] = set()
    leaked = 0
    for _, m in got4.messages:
        if isinstance(m, AssistantMessage) and not m.parent_tool_use_id:
            open_calls |= {
                b.id for b in m.content if isinstance(b, ToolUseBlock) and b.name in CALL_NAMES
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
    rec.check(f"h4c_no_unparented_deltas_during_subagent{sfx}", leaked == 0, leaked)
    rec.note(f"h4_answer_contains_magic{sfx}", MAGIC in "".join(got4.texts_per_turn))
    return got4


async def run(
    main_tools: list[str], helper_tools: list[str], prompt: str, guard=None, allow: bool = True
):
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
        env=FG_ENV,
        tools=main_tools,
        mcp_servers={"spike": server},
        agents={"helper": helper},
        allowed_tools=["mcp__spike__get_magic", *main_tools] if allow else [],
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
    # ids of the main agent's delegations to "helper": a built-in use only counts for h3 when
    # its parent is one of these (run 3 showed a general-purpose fallback otherwise)
    helper_ids = {
        b.id
        for _, m in got.messages
        if isinstance(m, AssistantMessage) and m.parent_tool_use_id is None
        for b in m.content
        if isinstance(b, ToolUseBlock) and b.input.get("subagent_type") == "helper"
    }
    tools, agents = init.data.get("tools", []), init.data.get("agents")
    return (
        tools,
        agents,
        [u for u in uses if u[0] is None or u[0] in helper_ids],
        got.texts_per_turn[0],
    )


async def main() -> int:
    require_token()
    rec = Recorder("h_subagent")
    ask = "Use the helper sub-agent to get the magic value via get_magic. Reply with the value."

    tools1, agents1, uses1, _text1 = await run([], ["mcp__spike__get_magic"], ask)
    rec.note("h1_init", {"tools": tools1, "agents": agents1, "uses": uses1})
    rec.check(
        "h1_helper_reachable_with_tools_empty", any(u[1] in ("Agent", "Task") for u in uses1), uses1
    )

    # The `tools=` option takes "Agent"; the init tool list reports it as "Task" while the
    # model's ToolUseBlock is named "Agent" (CLI 2.1.301). Match calls by either name.
    delegate = "Agent"
    tools2, _agents2, uses2, text2 = await run([delegate], ["mcp__spike__get_magic"], ask)
    rec.note("h2_init_tools_for_tools_agent", tools2)
    rec.note("h2_delegation_call_names", sorted({n for p, n, _ in uses2 if p is None} & CALL_NAMES))
    rec.check(
        "h2_main_has_only_delegate_and_mcp",
        set(tools2) <= CALL_NAMES | {"mcp__spike__get_magic"},
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
        any(p is not None and n == "WebFetch" for p, n, _ in uses3),  # p: a helper call id
        uses3,
    )
    rec.check("h3_guard_saw_helper_write", any(n == "Write" for n, _ in seen), seen)
    rec.check("h3_inside_write_ok", (WORKDIR / "docs/parts/TEST/notes.md").exists(), str(WORKDIR))
    rec.check("h3_outside_write_denied", not (WORKDIR / "outside.md").exists(), str(WORKDIR))
    rec.note("h3_answer", text3)

    # h3b (added in the run, for the "built-ins in session" gate outcome): the researcher's
    # built-ins are in the session `tools`; the session guard denies them for main-agent calls
    # (agent_id None) and path-guards the sub-agent's Write.
    seen3b: list[dict[str, Any]] = []

    async def session_guard(name: str, inp: dict[str, Any], ctx: ToolPermissionContext):
        aid = getattr(ctx, "agent_id", "<no field>")
        main_builtin = name in BUILTINS and aid is None
        outside_write = name == "Write" and "docs/parts/" not in str(inp.get("file_path", ""))
        decision = "deny" if main_builtin or outside_write else "allow"
        seen3b.append(
            {"name": name, "agent_id": aid, "decision": decision, "file_path": inp.get("file_path")}
        )
        if decision == "deny":
            return PermissionResultDeny(message="denied by session guard")
        return PermissionResultAllow()

    ask3b = (
        "Step 1: fetch https://example.com yourself with WebFetch (no sub-agent). "
        "Step 2: whatever happened, use the helper sub-agent: it must fetch https://example.com, "
        f"write the page title into {WORKDIR}/docs/parts/TEST2/notes.md and also try to write "
        f"it to {WORKDIR}/outside2.md. Reply with the title."
    )
    tools3b, _, uses3b, text3b = await run(
        [delegate, *BUILTINS], ["WebFetch", "Write"], ask3b, guard=session_guard, allow=False
    )
    rec.note("h3b_init_tools", tools3b)
    rec.note("h3b_guard_calls", seen3b)
    rec.check(
        "h3b_main_builtin_call_denied",
        any(
            c["name"] in BUILTINS and c["agent_id"] is None and c["decision"] == "deny"
            for c in seen3b
        ),
        seen3b,
    )
    rec.check(
        "h3b_helper_used_webfetch",
        any(p is not None and n == "WebFetch" for p, n, _ in uses3b),
        uses3b,
    )
    rec.check(
        "h3b_guard_saw_helper_write_with_agent_id",
        any(c["name"] == "Write" and c["agent_id"] not in (None, "<no field>") for c in seen3b),
        seen3b,
    )
    rec.check("h3b_inside_write_ok", (WORKDIR / "docs/parts/TEST2/notes.md").exists(), str(WORKDIR))
    rec.check("h3b_outside_write_denied", not (WORKDIR / "outside2.md").exists(), str(WORKDIR))
    rec.note("h3b_answer", text3b)

    # h4: owner gate. Run twice: with the planned guard mechanism (`can_use_tool`) and with a
    # PreToolUse hook (the plan's named alternative if can_use_tool misses a call). The helper
    # only gets the in-session MCP tool (h3: built-ins outside the session `tools` do not resolve
    # and the helper would refuse to start).
    got4 = None
    for mech in ("can_use_tool", "hook"):
        got = await gate_run(rec, mech)
        if mech == "hook":
            got4 = got

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
        env=FG_ENV,
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
    # One API message can arrive as several AssistantMessages (one per block) sharing a
    # message_id and usage; count each message_id once.
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )

    def usage_sum(sub: bool) -> dict[str, int]:
        per_id: dict[str, dict[str, Any]] = {}
        for _, m in got4.messages:
            if isinstance(m, AssistantMessage) and bool(m.parent_tool_use_id) == sub and m.usage:
                per_id[m.message_id or str(id(m))] = m.usage
        return {k: sum(int(u.get(k) or 0) for u in per_id.values()) for k in keys}

    final4 = next((m for _, m in got4.messages if type(m).__name__ == "ResultMessage"), None)
    result_usage = getattr(final4, "usage", None) or {}
    main_sum, sub_sum = usage_sum(False), usage_sum(True)
    rec.note(
        "h5_usage_compare",
        {
            "result_usage": {k: result_usage.get(k) for k in keys},
            "result_cost": getattr(final4, "total_cost_usd", None),
            "result_model_usage": jsonable(getattr(final4, "model_usage", None)),
            "main_assistant_usage_sum": main_sum,
            "subagent_assistant_usage_sum": sub_sum,
            "result_equals_main_only": all(result_usage.get(k) == main_sum[k] for k in keys),
            "result_equals_main_plus_sub": all(
                result_usage.get(k) == main_sum[k] + sub_sum[k] for k in keys
            ),
        },
    )
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
