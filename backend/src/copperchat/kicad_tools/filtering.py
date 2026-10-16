from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from fnmatch import fnmatchcase

from copperchat.errors import StructuredToolError


def matches(pattern: str | None, *fields: str) -> bool:
    """Case-insensitive glob on any of `fields`; an empty pattern matches everything."""
    if not pattern:
        return True
    p = pattern.lower()
    return any(fnmatchcase(f.lower(), p) for f in fields if f)


def not_found(kind: str, name: str, candidates: Iterable[str]) -> StructuredToolError:
    pool = sorted(set(candidates))
    close = difflib.get_close_matches(name, pool, n=5, cutoff=0.5)
    suggestion = (
        f"Did you mean: {', '.join(close)}?"
        if close
        else f"List the available {kind.lower()}s first (filter with a glob like 'R*')."
    )
    return StructuredToolError(
        f"{kind} {name} not found",
        f"No {kind.lower()} with that name exists in the open document",
        suggestion,
    )


_DIGITS = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple[tuple[int, int | str], ...]:
    """Sort key that orders `R2` before `R10`."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in _DIGITS.split(text) if part
    )
