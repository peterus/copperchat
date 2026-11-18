from __future__ import annotations

from collections.abc import Iterable
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


def attach_tool_notes(exc: BaseException, notes: list[dict[str, Any]]) -> None:
    """Carry notes of work a failing tool call already did (e.g. the footprint pending
    cleanup, spec §9.3) on its exception; the tool adapter puts them into the error's
    `details.notes`, whatever error it turns the exception into."""
    if notes:
        exc.tool_notes = [*tool_notes(exc), *notes]  # type: ignore[attr-defined]


def tool_notes(exc: BaseException) -> list[dict[str, Any]]:
    """The notes `attach_tool_notes` put on `exc` (empty if none)."""
    return list(getattr(exc, "tool_notes", None) or [])


class DocumentInconsistent(CopperChatError):
    """A failed edit could not be undone exactly (E-C4): the turn ends without autosave
    and the session is reopened from the last save - the state just before that edit,
    since every successful edit saved itself (#6). `last_save_partial`: the failed edit
    had saved part of itself already (a shared-sheet placement) and undoing that failed,
    so the last save may still hold it (the recovery notice says so).
    `leftovers`: what a failed footprint compensation left behind (lib ids, "row <nick>";
    FP-C8); `replaced`: the leftovers that are `replace` targets (an existing footprint
    that may now differ - never to be deleted); `footprint_only`: only the footprint
    library is affected - the schematic is intact, so the turn end need not reopen it."""

    code = "document_inconsistent"

    def __init__(
        self,
        message: str = "",
        *,
        last_save_partial: bool = False,
        leftovers: list[str] | None = None,
        replaced: list[str] | None = None,
        footprint_only: bool = False,
    ) -> None:
        self.last_save_partial = last_save_partial
        self.leftovers: list[str] = list(leftovers) if leftovers else []
        self.replaced: list[str] = list(replaced) if replaced else []
        self.footprint_only = footprint_only
        super().__init__(message)


def leftover_advice(leftovers: list[str] | tuple[str, ...], replaced: Iterable[str]) -> str:
    """What the user can do about the leftovers of a failed footprint compensation (spec
    §9.3, §9.5), per kind: a new footprint or library row goes with a History undo (a
    footprint also with fp_delete, once that tool exists); a `replace` target is an
    existing footprint of the user - History undo brings its earlier version back, and
    it must never be deleted."""
    kept = set(replaced)
    removable = [x for x in leftovers if x not in kept]
    restorable = [x for x in leftovers if x in kept]
    parts: list[str] = []
    if removable:
        parts.append(
            f"Undoing this turn in History removes {', '.join(removable)}; a footprint "
            "left behind can also be removed with fp_delete once that tool is available."
        )
    if restorable:
        parts.append(
            f"{', '.join(restorable)} replaced an existing footprint: undoing this turn in "
            "History brings back its earlier version. Do not delete it - that would lose "
            "the user's footprint."
        )
    return " ".join(parts)


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
