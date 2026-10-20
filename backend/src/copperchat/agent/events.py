"""Internal events between an agent backend (SDK adapter or FakeAgent) and the RunEngine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from copperchat.agent.protocol import AttachmentRef
from copperchat.projects import Project
from copperchat.tools import ImageBlock

if TYPE_CHECKING:
    from copperchat.db.repos.conversations import ConversationRow


@dataclass(frozen=True)
class QueuedItem:
    message_id: int
    client_message_id: str
    conversation_id: str
    text: str
    attachments: tuple[AttachmentRef, ...] = ()


@dataclass(frozen=True)
class TurnStarted:
    """Informational; the engine treats the gated input pull as the authoritative turn start."""


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True)
class ToolUse:
    """A tool call. ``parent_tool_use_id`` is set for calls made by an SDK sub-agent: the
    id of the main agent's ``Agent`` call that started it (spike 1A (h4c))."""

    id: str
    name: str
    input: dict[str, Any]
    parent_tool_use_id: str | None = None


@dataclass(frozen=True)
class ToolResultEvt:
    id: str
    content: str
    images: tuple[ImageBlock, ...] = ()
    is_error: bool = False
    parent_tool_use_id: str | None = None


@dataclass(frozen=True)
class SubagentText:
    """A complete text block of an SDK sub-agent (sub-agents never stream deltas, X20 (7))."""

    parent_tool_use_id: str
    text: str


@dataclass(frozen=True)
class Usage:
    """Usage of one turn (tokens and cost include sub-agents, see spike 1A (f)/(h5))."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float | None = None
    rate_limit_status: Literal["allowed", "allowed_warning", "rejected"] | None = None
    rate_limit_resets_at: datetime | None = None


@dataclass(frozen=True)
class RateLimited:
    resets_at: datetime


@dataclass(frozen=True)
class TurnFinished:
    """End of a turn. ``unsolicited`` marks the end of a turn the CLI started on its own
    (e.g. a background task notification, X20 (3)): it answers no gated input item, so
    the engine must neither finish the current turn nor release the next item on it."""

    reason: Literal["completed", "interrupted", "error"]
    error: str | None = field(default=None)
    unsolicited: bool = False


@dataclass(frozen=True)
class SessionId:
    id: str


AgentEvent = (
    TurnStarted
    | TextDelta
    | ThinkingDelta
    | ToolUse
    | ToolResultEvt
    | SubagentText
    | Usage
    | RateLimited
    | TurnFinished
    | SessionId
)


class AgentBackend(Protocol):
    def start(
        self,
        project: Project,
        conversation: ConversationRow,
        input_stream: AsyncIterator[QueuedItem],
        resume_session_id: str | None,
    ) -> AsyncIterator[AgentEvent]: ...

    async def interrupt(self) -> None: ...
