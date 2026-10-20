"""WebSocket/REST wire protocol (index contract C7). camelCase on the wire."""

from __future__ import annotations

import json
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    StringConstraints,
    TypeAdapter,
    model_serializer,
    model_validator,
)
from pydantic.alias_generators import to_camel

ProjectId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12}$")]
# Conversation ids are uuid4 hex (32 lowercase hex characters, spec §4.2).
ConversationId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
ConversationType = Literal["free", "requirements", "schematic", "board"]
ConversationStatus = Literal["open", "closing", "closed"]
Role = Literal["user", "assistant", "system"]
Kind = Literal["text", "thinking", "tool_use", "tool_result", "image", "error", "notice"]
Status = Literal["queued", "streaming", "done", "cancelled", "error"]
FinishReason = Literal["completed", "interrupted", "error", "rate_limited"]


def _require_project_key(data: Any) -> Any:
    """ping/pong frames carry an explicit `"projectId": null`; the key must be present."""
    if isinstance(data, dict) and "projectId" not in data and "project_id" not in data:
        raise ValueError("projectId must be present (null)")
    return data


class Wire(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )
    # Optional fields that are left out of the JSON when None (all others are always emitted).
    OMIT_IF_NONE: ClassVar[frozenset[str]] = frozenset()

    @model_serializer(mode="wrap")
    def _omit_none(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for name in self.OMIT_IF_NONE:
            for key in (name, to_camel(name)):
                if key in data and data[key] is None:
                    del data[key]
        return data


class AttachmentRef(Wire):
    id: str = Field(min_length=1, max_length=100)
    kind: Literal["image", "pdf"]
    name: str = Field(min_length=1, max_length=255)


class MessageDTO(Wire):
    id: int
    project_id: ProjectId
    conversation_id: ConversationId
    client_message_id: str | None
    run_id: str | None
    role: Role
    kind: Kind
    content: str
    status: Status
    metadata: dict[str, Any]
    created_at: str


class ConversationUsage(Wire):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)


class ConversationDTO(Wire):
    """A session of a project (spec §6.1; plan S-C4)."""

    id: ConversationId
    seq: int = Field(ge=1)
    title: str
    type: ConversationType
    status: ConversationStatus
    archived: bool
    created_at: str
    last_message_at: str | None
    closed_at: str | None
    usage: ConversationUsage


# ---- client → server
class SubscribeFrame(Wire):
    """Replays one conversation; live frames of every conversation of the project arrive
    (spec §6.2)."""

    type: Literal["subscribe"] = "subscribe"
    project_id: ProjectId
    conversation_id: ConversationId
    since_message_id: int = Field(ge=0)


class SendFrame(Wire):
    type: Literal["send"] = "send"
    project_id: ProjectId
    conversation_id: ConversationId
    client_message_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=100_000)
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=20)


class InterruptFrame(Wire):
    """``conversation_id``: the session the user pressed Stop for; it scopes the
    cancellation of waiting messages between turns (spec §5.2)."""

    type: Literal["interrupt"] = "interrupt"
    project_id: ProjectId
    conversation_id: ConversationId
    run_id: str | None


class CancelQueuedFrame(Wire):
    type: Literal["cancel-queued"] = "cancel-queued"
    project_id: ProjectId
    message_id: int = Field(ge=1)


class PingFrame(Wire):
    type: Literal["ping"] = "ping"
    project_id: None = None

    @model_validator(mode="before")
    @classmethod
    def _require_project_key(cls, data: Any) -> Any:
        return _require_project_key(data)


class VisibilityFrame(Wire):
    """Whether this connection's page is in the foreground (push only goes out while no
    connection is visible, Phase 9)."""

    type: Literal["visibility"] = "visibility"
    project_id: ProjectId
    visible: StrictBool


ClientFrame = Annotated[
    SubscribeFrame | SendFrame | InterruptFrame | CancelQueuedFrame | PingFrame | VisibilityFrame,
    Field(discriminator="type"),
]


# ---- server → client
class MessageFrame(Wire):
    type: Literal["message"] = "message"
    project_id: ProjectId
    message: MessageDTO


class MessageDeltaFrame(Wire):
    OMIT_IF_NONE: ClassVar[frozenset[str]] = frozenset(
        {"content_delta", "status_patch", "metadata_patch"}
    )
    type: Literal["message-delta"] = "message-delta"
    project_id: ProjectId
    conversation_id: ConversationId
    message_id: int
    content_delta: str | None = None
    status_patch: Status | None = None
    metadata_patch: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _one_patch(self) -> MessageDeltaFrame:
        if self.content_delta is None and self.status_patch is None and self.metadata_patch is None:
            raise ValueError("message-delta needs contentDelta, statusPatch or metadataPatch")
        return self


class RunStartedFrame(Wire):
    type: Literal["run-started"] = "run-started"
    project_id: ProjectId
    conversation_id: ConversationId
    run_id: str


class RunFinishedFrame(Wire):
    type: Literal["run-finished"] = "run-finished"
    project_id: ProjectId
    conversation_id: ConversationId
    run_id: str
    reason: FinishReason


class ViewUpdatedFrame(Wire):
    type: Literal["view-updated"] = "view-updated"
    project_id: ProjectId
    docs: list[Literal["sch", "pcb"]] = Field(min_length=1)
    version: int = Field(ge=0)


class ChangesUpdatedFrame(Wire):
    """Change rows of a run (and plan step) were written (E-C12)."""

    type: Literal["changes-updated"] = "changes-updated"
    project_id: ProjectId
    run_id: str = Field(min_length=1)
    step_id: str | None
    count: int = Field(ge=0)


PlanStatusWire = Literal["proposed", "approved", "running", "paused", "done", "rejected", "aborted"]
StepStatusWire = Literal["pending", "running", "done", "failed", "interrupted"]


class PlanTouches(Wire):
    refs: list[str]
    nets: list[str]


class PlanStepErc(Wire):
    errors: int = Field(ge=0)
    warnings: int = Field(ge=0)
    justifications: dict[str, str]
    result_id: str | None


class PlanStepDTO(Wire):
    idx: int = Field(ge=1)
    title: str
    intent: str
    expected_changes: int = Field(ge=1)
    touches: PlanTouches
    verification: str
    status: StepStatusWire
    change_count: int = Field(ge=0)
    checkpoint_id: str | None
    summary: str | None
    failure: str | None
    erc: PlanStepErc | None


class PlanDTO(Wire):
    """E-C11 PlanDTO (camelCase on the wire)."""

    id: str = Field(min_length=1)
    project_id: ProjectId
    conversation_id: ConversationId
    title: str
    summary: str
    status: PlanStatusWire
    revision: int = Field(ge=1)
    pause_reason: str | None
    steps: list[PlanStepDTO]
    created_at: str
    updated_at: str


class PlanUpdatedFrame(Wire):
    """A plan changed state (E-C12, E8b)."""

    type: Literal["plan-updated"] = "plan-updated"
    project_id: ProjectId
    plan: PlanDTO


class DocUpdatedFrame(Wire):
    """A design doc was written, approved, unapproved or cleared (sessions S-C4, spec
    §6.2). ``conversationId`` is omitted for user edits; ``revisionId`` is null when no
    revision was recorded (front-matter-only changes)."""

    OMIT_IF_NONE: ClassVar[frozenset[str]] = frozenset({"conversation_id"})
    type: Literal["doc-updated"] = "doc-updated"
    project_id: ProjectId
    path: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1)
    status: Literal["draft", "approved"]
    user_edited: StrictBool
    revision_id: int | None = Field(ge=1)
    author: Literal["agent", "user", "external"]
    conversation_id: ConversationId | None = None


class RateLimitPayload(Wire):
    OMIT_IF_NONE: ClassVar[frozenset[str]] = frozenset({"resets_at"})
    status: Literal["allowed", "allowed_warning", "rejected"]
    resets_at: str | None = None


class UsagePayload(Wire):
    OMIT_IF_NONE: ClassVar[frozenset[str]] = frozenset({"cost_usd", "rate_limit"})
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cache_creation_tokens: int = Field(ge=0)
    cost_usd: float | None = None
    rate_limit: RateLimitPayload | None = None


class UsageFrame(Wire):
    type: Literal["usage"] = "usage"
    project_id: ProjectId
    conversation_id: ConversationId
    usage: UsagePayload


class ConversationUpdatedFrame(Wire):
    """A conversation was created, renamed, archived, changed status, or got its first or
    a turn's last message (spec §6.2)."""

    type: Literal["conversation-updated"] = "conversation-updated"
    project_id: ProjectId
    conversation: ConversationDTO


class RateLimitedFrame(Wire):
    type: Literal["rate-limited"] = "rate-limited"
    project_id: ProjectId
    resets_at: str


class ErrorFrame(Wire):
    OMIT_IF_NONE: ClassVar[frozenset[str]] = frozenset({"client_message_id"})
    type: Literal["error"] = "error"
    project_id: ProjectId | None
    code: str = Field(min_length=1)
    message: str
    client_message_id: str | None = None


class ServerPingFrame(Wire):
    type: Literal["ping"] = "ping"
    project_id: None = None

    @model_validator(mode="before")
    @classmethod
    def _require_project_key(cls, data: Any) -> Any:
        return _require_project_key(data)


class PongFrame(Wire):
    type: Literal["pong"] = "pong"
    project_id: None = None

    @model_validator(mode="before")
    @classmethod
    def _require_project_key(cls, data: Any) -> Any:
        return _require_project_key(data)


ServerFrame = Annotated[
    MessageFrame
    | MessageDeltaFrame
    | RunStartedFrame
    | RunFinishedFrame
    | ViewUpdatedFrame
    | ChangesUpdatedFrame
    | PlanUpdatedFrame
    | DocUpdatedFrame
    | UsageFrame
    | RateLimitedFrame
    | ErrorFrame
    | ServerPingFrame
    | PongFrame
    | ConversationUpdatedFrame,
    Field(discriminator="type"),
]

client_frame_adapter: TypeAdapter[ClientFrame] = TypeAdapter(ClientFrame)
server_frame_adapter: TypeAdapter[ServerFrame] = TypeAdapter(ServerFrame)


def dump_frame(frame: Wire) -> str:
    return json.dumps(
        frame.model_dump(mode="json", by_alias=True), separators=(",", ":"), ensure_ascii=False
    )
