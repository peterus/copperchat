"""Sanitiser for doc text and doc-derived index fields injected into the model input
(spec §9.1; used by C5 for injection and by ``doc_list`` for its fields)."""

from __future__ import annotations

import html
import re
import secrets
import unicodedata

NOTICE_PREFIX = "[CopperChat notice]"
_KEEP = frozenset("\t\n\r\x0b\x0c\x1c\x1d\x1e\x85")  # tab and the Cc line breaks
_WS = re.compile(r"\s+")


def _strip_controls(text: str) -> str:
    # Cs: lone surrogates (non-UTF-8 file names via surrogateescape) cannot be encoded
    return "".join(
        ch for ch in text if ch in _KEEP or unicodedata.category(ch) not in ("Cc", "Cf", "Cs")
    )


def _skeleton(line: str) -> str:
    """ASCII letters of the line after HTML-entity decoding and NFKD (full-width forms
    fold, accents split off), case-folded: spacing, punctuation, escapes and markup
    cannot split the token."""
    decoded = unicodedata.normalize("NFKD", html.unescape(line)).casefold()
    return "".join(ch for ch in decoded if "a" <= ch <= "z")


def _defuse(line: str) -> str:
    """Quote a line that could read as an application notice: its letter skeleton
    contains both ``copperchat`` and ``notice``. (Cross-script look-alikes are a known
    residual; injected doc text is also nonce-fenced and marked as project data.)"""
    skeleton = _skeleton(line)
    if "copperchat" in skeleton and "notice" in skeleton:
        return "> " + line
    return line


def sanitize_inline(text: str) -> str:
    """Remove control (except tab and line breaks) and format characters, split on every
    Unicode line break, and prefix ``> `` to lines that would pose as an application
    notice. Lines are joined with ``\\n``."""
    return "\n".join(_defuse(line) for line in _strip_controls(text).splitlines())


def fence(label: str, name: str, clean: str) -> str:
    """Wrap already sanitised text in a random per-call nonce fence (spec §9.1)."""
    nonce = secrets.token_hex(8)
    # a name with ">>>" and a fake nonce must not make the header look closed early
    name = name.replace("<", "").replace(">", "")
    return f"<<<{label} {name} {nonce}>>>\n{clean}\n<<<end {nonce}>>>"


def sanitize_field(text: str, limit: int = 80) -> str:
    """One line of at most ``limit`` characters (for headings, titles and other fields)."""
    flat = _WS.sub(" ", " ".join(_strip_controls(text).splitlines())).strip()
    if len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    out = _defuse(flat)
    return out if len(out) <= limit else out[: limit - 1] + "…"
