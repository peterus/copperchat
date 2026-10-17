from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import DocKind, KicadSession
from copperchat.projects import Project

__all__ = [
    "ImageBlock",
    "ResearchService",
    "StructuredToolError",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
]


@dataclass(frozen=True)
class ImageBlock:
    mime: str
    data: bytes
    path: Path | None = None


@dataclass
class ToolResult:
    data: dict[str, Any] | None = None
    images: list[ImageBlock] = field(default_factory=list)
    # Documents this call changed (mutating tools); the adapter reports them to the turn.
    changed_docs: set[DocKind] = field(default_factory=set)
    # Plain-text result for the model (and the chat row) instead of JSON ``data``; used by
    # research_part, whose summary is rendered as Markdown in the "Part research" card.
    text: str | None = None


class ResearchService(Protocol):
    """Runs the part-researcher for one research_part call (Phase 8)."""

    async def run(self, args: dict[str, Any]) -> str: ...


@dataclass
class ToolContext:
    project: Project
    session: KicadSession
    settings: Settings
    run_id: str | None = None
    on_mutation: Callable[[], Awaitable[None]] | None = None
    # Text of the user message that started the current turn (Phase 5).
    user_message: str | None = None
    # Turn callbacks wired by the run engine (both synchronous).
    on_documents_changed: Callable[[set[DocKind]], None] | None = None
    on_kicad_unavailable: Callable[[Exception], None] | None = None
    # Set by the SDK backend for its tool calls; None e.g. under the fake agent.
    research: ResearchService | None = None


ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    mutating: bool = False
    changes_documents: bool = False
    # False: the tool works without an open KiCad session (research_part); its context
    # then has no usable ``session``.
    requires_kicad: bool = True
