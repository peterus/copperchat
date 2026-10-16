from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession
from copperchat.projects import Project

__all__ = ["ImageBlock", "StructuredToolError", "ToolContext", "ToolResult", "ToolSpec"]


@dataclass(frozen=True)
class ImageBlock:
    mime: str
    data: bytes
    path: Path | None = None


@dataclass
class ToolResult:
    data: dict[str, Any] | None = None
    images: list[ImageBlock] = field(default_factory=list)


@dataclass
class ToolContext:
    project: Project
    session: KicadSession
    settings: Settings
    run_id: str | None = None
    on_mutation: Callable[[], Awaitable[None]] | None = None


ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    mutating: bool = False
    changes_documents: bool = False
