from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import DocKind, KicadSession
from copperchat.projects import Project

if TYPE_CHECKING:
    from copperchat.design_docs.service import DesignDocs, DocNotice
    from copperchat.editing.service import EditingService
    from copperchat.kicad_tools.edit.changeset import ChangeSet

__all__ = [
    "ImageBlock",
    "ResearchService",
    "StructuredToolError",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "edit_spec",
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
    # SP2 editing (E-C4/E-C5): turn state, budget gate and change log; None outside the
    # run engine (plain tests) - two-phase tools then run without gate and change log.
    editing: EditingService | None = None
    # A failed edit could not be undone exactly: the run engine ends the turn (like
    # on_kicad_unavailable) and the history hook reopens the session from disk.
    on_document_inconsistent: Callable[[Exception], None] | None = None
    # Sessions (S-C5): the conversation of the burst, for the doc write policy and
    # attribution. conversation_type None is Free only while conversation_id is None too
    # (plain tests); with a conversation_id it is refused (fail closed).
    conversation_id: str | None = None
    conversation_type: str | None = None
    conversation_seq: int | None = None
    conversation_title: str | None = None
    # Posts the doc-changed / changed-after-approval notice into the conversation (C5).
    post_doc_notice: Callable[[DocNotice], Awaitable[None]] | None = None
    # The DesignDocs service; None outside the run engine wiring (doc tools then refuse).
    docs: DesignDocs | None = None


ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]
PrepareFn = Callable[[ToolContext, dict[str, Any]], Awaitable["ChangeSet"]]
ApplyFn = Callable[[ToolContext, "ChangeSet"], Awaitable[ToolResult]]  # sets cs.applied


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
    # Two-phase mutating tool (E-C4): `prepare` reads and plans a ChangeSet without
    # writing, `apply` writes it. The tool adapter runs read-only check -> gate ->
    # prepare -> budget -> snapshot -> apply -> record; `handler` is unused.
    prepare: PrepareFn | None = None
    apply: ApplyFn | None = None


async def _two_phase_only(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    raise RuntimeError("two-phase tool: call it through the tool adapter (agent.sdk_tools)")


def edit_spec(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    prepare: PrepareFn,
    apply: ApplyFn,
) -> ToolSpec:
    return ToolSpec(
        name,
        description,
        input_schema,
        _two_phase_only,
        mutating=True,
        changes_documents=True,
        prepare=prepare,
        apply=apply,
    )
