from __future__ import annotations


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
    """A tool failure the agent can act on: what failed, why, and what to do instead."""

    code = "tool_error"
    http_status = 422

    def __init__(self, what: str, why: str, suggestion: str) -> None:
        self.what = what
        self.why = why
        self.suggestion = suggestion
        super().__init__(f"{what}: {why}")

    def to_dict(self) -> dict[str, str]:
        return {"what": self.what, "why": self.why, "suggestion": self.suggestion}
