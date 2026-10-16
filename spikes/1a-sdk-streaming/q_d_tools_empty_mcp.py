"""(d) tools=[] disables all built-ins while an in-process SDK MCP tool still works."""

from __future__ import annotations

import asyncio
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    SystemMessage,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from harness import Recorder, base_options, collect, require_token, user_msg

MAGIC = "MAGIC-7731"


@tool("get_magic", "Returns the magic value.", {})
async def get_magic(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": MAGIC}]}


async def main() -> int:
    require_token()
    rec = Recorder("d_tools_empty_mcp")
    server = create_sdk_mcp_server("spike", tools=[get_magic])

    async def gen():
        yield user_msg(
            "First run the shell command `ls /` with any tool you have. "
            "Then call get_magic and reply with its value."
        )

    client = ClaudeSDKClient(
        base_options(
            tools=[], mcp_servers={"spike": server}, allowed_tools=["mcp__spike__get_magic"]
        )
    )
    await client.connect(gen())
    got = await collect(client, until_results=1)
    await client.disconnect()

    init = next(m for _, m in got.messages if isinstance(m, SystemMessage) and m.subtype == "init")
    tools = init.data.get("tools", [])
    rec.note("d_init_tools", tools)
    rec.check("d1_no_builtin_tools", set(tools) == {"mcp__spike__get_magic"}, tools)
    used = [
        b.name
        for _, m in got.messages
        if isinstance(m, AssistantMessage)
        for b in m.content
        if isinstance(b, ToolUseBlock)
    ]
    rec.check("d2_no_builtin_tool_called", all(n.startswith("mcp__") for n in used), used)
    rec.check("d3_mcp_tool_called", "mcp__spike__get_magic" in used, used)
    rec.check("d4_mcp_result_in_answer", MAGIC in got.texts_per_turn[0], got.texts_per_turn)
    return rec.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
