"""SDK message helpers shared by the main backend and the part-researcher runner."""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from copperchat.agent.events import AgentEvent, SubagentText, ToolResultEvt, ToolUse
from copperchat.tools import ImageBlock

log = logging.getLogger(__name__)


def tool_result_content(
    content: str | list[dict[str, Any]] | None,
) -> tuple[str, tuple[ImageBlock, ...]]:
    if content is None:
        return "", ()
    if isinstance(content, str):
        return content, ()
    texts: list[str] = []
    images: list[ImageBlock] = []
    for item in content:
        if item.get("type") == "text":
            texts.append(str(item.get("text", "")))
        elif item.get("type") == "image":
            src = item.get("source", {})
            if src.get("type") == "base64":
                try:
                    data = base64.b64decode(src.get("data", ""))
                except (binascii.Error, ValueError):
                    log.warning("skipping tool-result image with invalid base64 data")
                    continue
                images.append(ImageBlock(mime=str(src.get("media_type", "image/png")), data=data))
    return "\n".join(texts), tuple(images)


@dataclass(frozen=True)
class Totals:
    """Token/cost totals (cumulative per client on a ResultMessage, or a per-turn sum)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float | None = None

    def is_below(self, other: Totals) -> bool:
        return (
            self.input_tokens < other.input_tokens
            or self.output_tokens < other.output_tokens
            or self.cache_read_tokens < other.cache_read_tokens
            or self.cache_creation_tokens < other.cache_creation_tokens
            or (
                self.cost_usd is not None
                and other.cost_usd is not None
                and self.cost_usd < other.cost_usd
            )
        )

    def __add__(self, other: Totals) -> Totals:
        costs = [c for c in (self.cost_usd, other.cost_usd) if c is not None]
        return Totals(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_creation_tokens + other.cache_creation_tokens,
            sum(costs) if costs else None,
        )


def model_usage_totals(msg: ResultMessage) -> Totals | None:
    if not msg.model_usage:
        return None
    totals = [0, 0, 0, 0]
    keys = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
    for per_model in msg.model_usage.values():
        raw: dict[str, Any] = dict(per_model)
        for i, key in enumerate(keys):
            totals[i] += int(raw.get(key, 0) or 0)
    inp, out, cache_read, cache_creation = totals
    return Totals(inp, out, cache_read, cache_creation, msg.total_cost_usd)


def result_totals(msg: ResultMessage) -> Totals:
    """Everything one single-turn client consumed, from its (only) ResultMessage."""
    totals = model_usage_totals(msg)
    if totals is not None:
        return totals
    u = msg.usage or {}
    return Totals(
        int(u.get("input_tokens", 0) or 0),
        int(u.get("output_tokens", 0) or 0),
        int(u.get("cache_read_input_tokens", 0) or 0),
        int(u.get("cache_creation_input_tokens", 0) or 0),
        msg.total_cost_usd,
    )


def subagent_events(msg: object, parent: str) -> list[AgentEvent]:
    """Complete, ``parent``-tagged events for a message of a sub-agent / research query.

    Tool calls, tool results (images dropped) and non-empty text blocks; thinking, stream
    events and string-content user messages (the echoed prompt) carry nothing.
    """
    out: list[AgentEvent] = []
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                out.append(
                    ToolUse(
                        id=block.id,
                        name=block.name,
                        input=dict(block.input),
                        parent_tool_use_id=parent,
                    )
                )
            elif isinstance(block, TextBlock) and block.text.strip():
                out.append(SubagentText(parent_tool_use_id=parent, text=block.text))
    elif isinstance(msg, UserMessage) and not isinstance(msg.content, str):
        for block in msg.content:
            if isinstance(block, ToolResultBlock):
                text, _images = tool_result_content(block.content)
                out.append(
                    ToolResultEvt(
                        id=block.tool_use_id,
                        content=text,
                        is_error=bool(block.is_error),
                        parent_tool_use_id=parent,
                    )
                )
    return out
