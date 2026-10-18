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

    `code` (E-C4: `libraries_loading`, `not_found`, `invalid_argument`, ...) and
    `details` are optional and only appear in `to_dict` when set."""

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
