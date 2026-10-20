"""Server-owned front matter of design docs (spec §7.2).

The flat ``key: value`` block between ``---`` lines is parsed like
``skills.store.parse_front_matter``. The server rebuilds it on every write: ``doc``,
``status``, ``user_edited`` and ``approved_at`` come from server state (``status`` only
mirrors ``doc_approvals``); any other line a user wrote by hand is kept verbatim.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

SERVER_KEYS = ("doc", "status", "user_edited", "approved_at")


@dataclass(frozen=True)
class Parsed:
    meta: dict[str, str]
    extra_lines: tuple[str, ...] = field(default=())  # unknown key lines, verbatim
    body: str = ""
    has_block: bool = False

    @property
    def user_edited(self) -> bool:
        return self.meta.get("user_edited", "").strip().lower() == "true"


def _lines(text: str) -> list[str]:
    """Split on ``\\n`` only (keeping ends); ``\\r`` stays with its line."""
    out = text.split("\n")
    lines = [line + "\n" for line in out[:-1]]
    if out[-1]:
        lines.append(out[-1])
    return lines


def newline_of(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def split(text: str) -> Parsed:
    text = text.removeprefix("\ufeff")  # a UTF-8 BOM must not hide the front matter
    lines = _lines(text)
    if not lines or lines[0].strip() != "---":
        return Parsed({}, (), text, False)
    meta: dict[str, str] = {}
    extra: list[str] = []
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return Parsed(meta, tuple(extra), "".join(lines[i + 1 :]), True)
        key, sep, value = line.partition(":")
        k = key.strip()
        if sep and k and not line[:1].isspace():
            meta[k] = value.strip().strip('"').strip("'")
            if k in SERVER_KEYS:
                continue
        extra.append(line if line.endswith("\n") else line + "\n")
    return Parsed({}, (), text, False)  # unterminated block: all body


def build(
    *,
    doc_key: str,
    approved: bool,
    approved_at: str | None,
    user_edited: bool,
    body: str,
    extra_lines: tuple[str, ...] = (),
    newline: str = "\n",
) -> str:
    def line(text: str) -> str:
        return text + newline

    head = [
        line("---"),
        line(f"doc: {doc_key}"),
        line(f"status: {'approved' if approved else 'draft'}"),
        line(f"user_edited: {'true' if user_edited else 'false'}"),
        line(f"approved_at: {approved_at}" if approved and approved_at else "approved_at:"),
    ]
    extras = [e[:-1].rstrip("\r") + newline for e in extra_lines]
    return "".join([*head, *extras, line("---")]) + body


def body_version(body: str) -> str:
    return "v" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
