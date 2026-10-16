"""Internal events between an agent backend (SDK adapter or FakeAgent) and the RunEngine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from copperchat.agent.protocol import AttachmentRef
from copperchat.projects import Project
from copperchat.tools import ImageBlock


@dataclass(frozen=True)
class QueuedItem:
    message_id: int
    client_message_id: str
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
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolResultEvt:
    id: str
    content: str
    images: tuple[ImageBlock, ...] = ()
    is_error: bool = False


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
    | Usage
    | RateLimited
    | TurnFinished
    | SessionId
)


class AgentBackend(Protocol):
    def start(
        self,
        project: Project,
        input_stream: AsyncIterator[QueuedItem],
        resume_session_id: str | None,
    ) -> AsyncIterator[AgentEvent]: ...

    async def interrupt(self) -> None: ...
