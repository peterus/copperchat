"""How long may one in-process MCP tool call take under the bundled CLI (SP4 P6b Task 4
Step 2, spec §20)? `pcb_route_fill`'s worst case is 1005 s (index "Tool worst-case
durations"); a CLI limit below 1065 s would need `MCP_TOOL_TIMEOUT` or scaled deadlines.

A test-only tool sleeps; the CLI calls it once and the result (or the CLI's error text) is
recorded. Two variants, both opt-in (`-m live_sdk`):

- `test_tool_call_limit_live`: the real model (needs credentials) is asked to call the
  tool once;
- `test_tool_call_limit_mock_api`: no credentials - the bundled CLI talks to a local mock
  of the Messages API that scripts exactly one `tool_use` of the tool (set
  COPPERCHAT_TOOL_LIMIT_PROBE=1; the CLI's MCP timeout handling is the same code path).

The tool sleeps PROBE_SLEEP_S (default 1070 s: beyond the 1005 s worst case + 60 s
margin) - a limit up to that is seen as an error result instead of the tool's text.
Measured 2026-11-26 with claude-agent-sdk 0.2.168 (As built P6b): the mock variant's 1070 s
call returned normally."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from .conftest import HAVE_CREDENTIALS, TOKEN

SLEEP_S = float(os.environ.get("PROBE_SLEEP_S", "1070"))
TOOL = "mcp__probe__sleep"


def _sleep_tool(record: dict[str, Any]) -> Any:
    @tool("sleep", "Sleeps, then answers 'slept'. Call it exactly once.", {})
    async def sleep(args: dict[str, Any]) -> dict[str, Any]:
        record["start"] = time.monotonic()
        try:
            await asyncio.sleep(SLEEP_S)
        except BaseException as exc:
            record["cancelled_after_s"] = time.monotonic() - record["start"]
            record["cancelled"] = type(exc).__name__
            raise
        record["slept_s"] = time.monotonic() - record["start"]
        return {"content": [{"type": "text", "text": f"slept {SLEEP_S:g}"}]}

    return sleep


async def _prompt(text: str) -> AsyncIterator[dict[str, Any]]:
    yield {
        "type": "user",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }


async def _probe(env: dict[str, str], home: Path) -> dict[str, Any]:
    record: dict[str, Any] = {}
    opts = ClaudeAgentOptions(
        mcp_servers={"probe": create_sdk_mcp_server("probe", tools=[_sleep_tool(record)])},
        allowed_tools=[TOOL],
        tools=[],
        max_turns=3,
        env={"CLAUDE_CONFIG_DIR": str(home / "claude"), **env},
    )
    t0 = time.monotonic()
    async for msg in query(prompt=_prompt(f"Call {TOOL} once, then reply done."), options=opts):
        if isinstance(msg, UserMessage) and not isinstance(msg.content, str):
            for block in msg.content:
                if getattr(block, "tool_use_id", None):
                    record["result"] = json.dumps(getattr(block, "content", None))[:500]
                    record["is_error"] = getattr(block, "is_error", None)
                    record["result_after_s"] = time.monotonic() - t0
        if isinstance(msg, ResultMessage):
            break
    return record


def _check(record: dict[str, Any]) -> None:
    print("tool-call limit probe:", json.dumps(record, default=str))
    assert record.get("slept_s", 0) >= SLEEP_S - 1, record
    assert not record.get("is_error"), record
    assert "slept" in record.get("result", ""), record


OPT_IN = pytest.mark.skipif(
    os.environ.get("COPPERCHAT_TOOL_LIMIT_PROBE") != "1",
    reason="set COPPERCHAT_TOOL_LIMIT_PROBE=1 (runs PROBE_SLEEP_S, default 1070 s)",
)


@pytest.mark.live_sdk
@pytest.mark.skipif(not HAVE_CREDENTIALS, reason="needs model credentials")
@OPT_IN
@pytest.mark.timeout(int(SLEEP_S) + 300)
async def test_tool_call_limit_live(tmp_path: Path) -> None:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN} if TOKEN else {}
    if not TOKEN:
        env["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY", "")
    _check(await _probe(env, tmp_path))


# -- the mock Messages API --------------------------------------------------------------------


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _message(block: dict[str, Any], delta: dict[str, Any], stop: str) -> list[dict[str, Any]]:
    start = {
        "id": "msg_probe",
        "type": "message",
        "role": "assistant",
        "model": "claude-probe",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    return [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0, "content_block": block},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]


class _MockApi(BaseHTTPRequestHandler):
    """Scripts one `tool_use` of TOOL for the first request that offers tools; every other
    message request gets a short text answer."""

    served_tool = False

    def log_message(self, *args: Any) -> None:
        return None

    def _send(self, ctype: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send("application/json", b"{}")

    def do_POST(self) -> None:
        n = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if "/messages" not in self.path or "count_tokens" in self.path:
            self._send("application/json", b'{"input_tokens": 1}')
            return
        if body.get("tools") and not _MockApi.served_tool:
            _MockApi.served_tool = True
            block = {"type": "tool_use", "id": "toolu_probe", "name": TOOL, "input": {}}
            events = _message(block, {"type": "input_json_delta", "partial_json": "{}"}, "tool_use")
        else:
            events = _message(
                {"type": "text", "text": ""}, {"type": "text_delta", "text": "done"}, "end_turn"
            )
        self._send("text/event-stream", _sse(events))


@pytest.mark.live_sdk
@OPT_IN
@pytest.mark.timeout(int(SLEEP_S) + 300)
async def test_tool_call_limit_mock_api(tmp_path: Path) -> None:
    _MockApi.served_tool = False
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockApi)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{srv.server_address[1]}",
            "ANTHROPIC_API_KEY": "sk-ant-probe",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        _check(await _probe(env, tmp_path))
    finally:
        srv.shutdown()
