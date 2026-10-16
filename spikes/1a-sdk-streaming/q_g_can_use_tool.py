"""(g) Is can_use_tool invoked for in-process MCP tools, and how do allow rules shadow it?"""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
    ToolResultBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from harness import Recorder, base_options, collect, require_token, user_msg


@tool("get_magic", "Returns the magic value.", {})
async def get_magic(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "MAGIC-7731"}]}


@tool("deny_me", "Writes a file at the given path.", {"path": str})
async def deny_me(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "written"}]}


async def run(allowed: list[str]) -> tuple[list[tuple[str, dict]], list[Any]]:
    calls: list[tuple[str, dict]] = []

    async def can_use_tool(name: str, inp: dict[str, Any], ctx: ToolPermissionContext):
        calls.append((name, inp))
        if name.endswith("deny_me"):
            return PermissionResultDeny(message="path outside docs/parts/ is not allowed")
        return PermissionResultAllow()

    async def gen():
        yield user_msg(
            "Call get_magic. Then call deny_me with path=/etc/passwd. "
            "Then report both outcomes in one line."
        )

    server = create_sdk_mcp_server("spike", tools=[get_magic, deny_me])
    client = ClaudeSDKClient(
        base_options(
            mcp_servers={"spike": server}, allowed_tools=allowed, can_use_tool=can_use_tool
        )
    )
    await client.connect(gen())
    got = await collect(client, until_results=1)
    await client.disconnect()
    results = [
        b
        for _, m in got.messages
        if isinstance(m, UserMessage) and isinstance(m.content, list)
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    return calls, results


async def main() -> int:
    require_token()
    rec = Recorder("g_can_use_tool")
    calls, results = await run(allowed=[])
    names = [n for n, _ in calls]
    rec.check("g1_called_for_mcp_tool", "mcp__spike__get_magic" in names, calls)
    rec.check(
        "g2_called_with_input",
        any(n.endswith("deny_me") and i.get("path") == "/etc/passwd" for n, i in calls),
        calls,
    )
    rec.check(
        "g3_denial_reaches_model_as_error",
        any(r.is_error and "docs/parts" in str(r.content) for r in results),
        [(r.is_error, str(r.content)[:200]) for r in results],
    )
    calls2, _ = await run(allowed=["mcp__spike__get_magic"])
    rec.check(
        "g4_allowed_tools_shadow_callback",
        "mcp__spike__get_magic" not in [n for n, _ in calls2],
        calls2,
    )
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
