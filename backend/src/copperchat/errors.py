from __future__ import annotations

from typing import Any


class CopperChatError(Exception):
    """Base class for all expected CopperChat errors (mapped to HTTP by web.app)."""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str = "") -> None:
        self.message = message or type(self).__name__
        super().__init__(self.message)


class ConfigError(CopperChatError):
    code = "config_error"


class MissingCredentialsError(ConfigError):
    code = "missing_credentials"


class ProjectNotFound(CopperChatError):
    code = "project_not_found"
    http_status = 404


class InvalidProjectId(CopperChatError):
    code = "invalid_project_id"
    http_status = 400


class ReadOnlyProject(CopperChatError):
    code = "read_only"
    http_status = 409


class DocumentNotOpen(CopperChatError):
    code = "document_not_open"
    http_status = 409


class StructuredToolError(CopperChatError):
    """A tool failure the agent can act on: what failed, why, and what to do instead.

    `code` (E-C4: `read_only`, `plan_required`, `verification_failed`, ...) and `details`
    (structured extras, e.g. expected vs. found) are optional and only serialized when set.
    """

    code = "tool_error"
    http_status = 422

    def __init__(
        self,
        what: str,
        why: str,
        suggestion: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.what = what
        self.why = why
        self.suggestion = suggestion
        self._explicit_code = code
        if code is not None:
            self.code = code
        self.details = details
        super().__init__(f"{what}: {why}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"what": self.what, "why": self.why, "suggestion": self.suggestion}
        if self._explicit_code is not None:
            out["code"] = self._explicit_code
        if self.details is not None:
            out["details"] = self.details
        return out


class DocumentInconsistent(CopperChatError):
    """A failed edit could not be undone exactly (E-C4): the turn ends without autosave
    and the session is reopened from the last save - the state just before that edit,
    since every successful edit saved itself (#6). `last_save_partial`: the failed edit
    had saved part of itself already (a shared-sheet placement) and undoing that failed,
    so the last save may still hold it (the recovery notice says so)."""

    code = "document_inconsistent"

    def __init__(self, message: str = "", *, last_save_partial: bool = False) -> None:
        self.last_save_partial = last_save_partial
        super().__init__(message)


class ConversationNotFound(CopperChatError):
    """A conversation id that does not exist or belongs to another project (spec §6.1)."""

    code = "conversation_not_found"
    http_status = 404


class ConversationClosed(CopperChatError):
    """A message for a conversation that is not open (spec §6.2)."""

    code = "conversation_closed"
    http_status = 409


class ConversationStateConflict(CopperChatError):
    """A status change whose expected current status no longer holds."""

    code = "conversation_state_conflict"
    http_status = 409


class InvalidConversation(CopperChatError):
    """A conversation title or type that fails validation."""

    code = "invalid_conversation"
    http_status = 422
