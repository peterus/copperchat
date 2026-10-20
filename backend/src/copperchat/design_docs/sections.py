"""Section addressing in Markdown bodies (spec §7.4 "Section addressing").

ATX headings of level 1-4 address sections; a section runs from its heading to the next
heading of the same or a higher level. Headings inside fenced code blocks are not
headings. ``"Parent/Child"`` disambiguates duplicate headings. Line endings are kept.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from copperchat.design_docs.frontmatter import _lines, newline_of
from copperchat.design_docs.paths import DocError

MAX_LEVEL = 4
# Matched against a right-stripped line: no ambiguous quantifiers (no backtracking blowup).
_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*))?$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_WS = re.compile(r"\s+")
_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+\S")


@dataclass(frozen=True)
class Heading:
    index: int  # line index
    level: int
    text: str  # normalised
    ancestors: tuple[str, ...]  # normalised texts of the enclosing headings


_JOINERS = frozenset("\u200c\u200d")  # ZWNJ/ZWJ carry meaning (scripts, emoji)


def display_title(text: str) -> str:
    """A created heading: whitespace collapsed, invisible controls dropped, but ZWJ/ZWNJ
    kept (emoji sequences, Persian/Indic text)."""
    text = "".join(ch for ch in text if ch in _JOINERS or unicodedata.category(ch) != "Cf")
    return _WS.sub(" ", text).strip()


def normalize(text: str) -> str:
    """Collapse whitespace; format characters (zero-width, soft hyphen, ZWJ) are dropped
    so a heading matches the sanitised form doc_list shows."""
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return _WS.sub(" ", text).strip()


def heading_of(line: str) -> tuple[int, str] | None:
    """(level, normalised text) when ``line`` is an ATX heading, else None."""
    m = _ATX.match(line.rstrip())
    if m is None:
        return None
    text = m.group(2) or ""
    # optional closing sequence: a run of '#' preceded by a space (or the whole text)
    bare = text.rstrip("#")
    if len(bare) < len(text) and (not bare or bare[-1] in " \t"):
        text = bare
    return len(m.group(1)), normalize(text)


def _scan(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line index, level, text) of every ATX heading outside fenced code."""
    return _scan_fenced(lines)[0]


def _scan_fenced(lines: list[str]) -> tuple[list[tuple[int, int, str]], str | None]:
    """Headings outside fences, and the fence marker still open at the end (or None)."""
    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    for i, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        fm = _FENCE.match(bare)
        if fence is not None:
            if (
                fm is not None
                and fm.group(1)[0] == fence[0]
                and len(fm.group(1)) >= len(fence)
                and not bare.strip().lstrip(fence[0])
            ):
                fence = None
            continue
        if fm is not None and not (fm.group(1)[0] == "`" and "`" in bare[fm.end() :]):
            # CommonMark: a backtick fence's info string may not contain a backtick
            fence = fm.group(1)
            continue
        h = heading_of(bare)
        if h is not None:
            found.append((i, h[0], h[1]))
    return found, fence


def headings(body: str, max_level: int = MAX_LEVEL) -> list[Heading]:
    out: list[Heading] = []
    stack: list[tuple[int, str]] = []
    for idx, level, text in _scan(_lines(body)):
        if level > MAX_LEVEL:
            continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        if level <= max_level:
            out.append(Heading(idx, level, text, tuple(t for _, t in stack)))
        stack.append((level, text))
    return out


def heading_texts(body: str, level: int | None = None) -> list[str]:
    return [h.text for h in headings(body) if level is None or h.level == level]


def _not_found(query: str, body: str) -> DocError:
    names = heading_texts(body)
    return DocError(
        "doc_section_not_found",
        f"Section {query!r} not found",
        "no heading with that text: " + (", ".join(names[:60]) or "the doc has no headings"),
        "Use one of the listed headings, or create=true to add the section.",
        details={"headings": names[:200]},
    )


def find(body: str, query: str) -> Heading:
    q = normalize(query)
    hs = headings(body)
    exact = [h for h in hs if h.text == q]
    if len(exact) == 1:
        return exact[0]
    if not exact and "/" in q:
        # path form: the query is "<ancestor>/.../<heading>" (heading texts may contain
        # '/' themselves, so every ancestor-suffix path is compared as a whole)
        forms = {q, re.sub(r"\s*/\s*", "/", q)}
        exact = [
            h
            for h in hs
            if any(
                "/".join((*h.ancestors[len(h.ancestors) - k :], h.text)) in forms
                for k in range(1, len(h.ancestors) + 1)
            )
        ]
        if len(exact) == 1:
            return exact[0]
    if not exact:
        raise _not_found(query, body)
    paths = ["/".join((*h.ancestors, h.text)) for h in exact]
    raise DocError(
        "doc_section_ambiguous",
        f"Section {query!r} is ambiguous",
        f"{len(exact)} headings match",
        "Address it with the parent heading: " + ", ".join(repr(p) for p in paths),
        details={"candidates": paths},
    )


def section_range(body: str, h: Heading) -> tuple[int, int]:
    """Line range [start, end) of the section, the heading line included."""
    end = len(_lines(body))
    for idx, level, _ in _scan(_lines(body)):
        if idx > h.index and level <= h.level:
            end = idx
            break
    return h.index, end


def spans_whole_doc(body: str, h: Heading) -> bool:
    """True when the section holds every heading of the body and runs to its end, so a
    replace of it equals a whole-doc write."""
    start, end = section_range(body, h)
    first = min((x.index for x in headings(body)), default=start)
    return first == start and end == len(_lines(body))


def section_text(body: str, h: Heading) -> str:
    lines = _lines(body)
    start, end = section_range(body, h)
    return "".join(lines[start:end])


def open_fence(text: str) -> str | None:
    """The fence marker still open at the end of ``text`` (after line-ending
    normalisation), or None."""
    return _scan_fenced(_lines(_convert(text, "\n")))[1]


def check_fences(text: str) -> None:
    if open_fence(text) is not None:
        raise DocError(
            "doc_heading_refused",
            "The text has an unclosed code fence",
            "an open ``` or ~~~ block would hide the following headings of the doc",
            "Close every code fence in the text.",
        )


def check_headings(text: str, max_level: int) -> None:
    """Refuse ATX headings at a level ≤ ``max_level`` outside code fences, and an
    unclosed fence (it would swallow the following headings of the doc)."""
    found, still_open = _scan_fenced(_lines(text))
    if still_open is not None:
        raise DocError(
            "doc_heading_refused",
            "The text has an unclosed code fence",
            "an open ``` or ~~~ block would hide the following headings of the doc",
            "Close every code fence in the text.",
        )
    for _, level, title in found:
        if level <= max_level:
            raise DocError(
                "doc_heading_refused",
                "The text contains a heading that would end the section",
                f"heading {title!r} (level {level}) is at or above the target level {max_level}",
                f"Use headings deeper than level {max_level} inside the section, or patch "
                "another section.",
            )


def _convert(text: str, nl: str) -> str:
    """Incoming text in the doc's line endings; a lone CR (a line break for Markdown
    renderers) becomes a line break too, so it cannot hide a heading from the scan."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", nl)


def _with_end(text: str, nl: str) -> str:
    return text if not text or text.endswith(nl) else text + nl


def patch(body: str, query: str, new: str, *, mode: str, create: bool) -> tuple[str, str]:
    """Replace or append to a section. Returns (new body, heading text)."""
    nl = newline_of(body) if body else "\n"
    new = _convert(new, nl)
    try:
        h = find(body, query)
    except DocError as err:
        if err.code != "doc_section_not_found" or not create:
            raise
        title = display_title(query)
        check_headings(new, 2)
        prefix = _with_end(body, nl)
        if prefix.strip():
            prefix += nl
        stripped = new.strip("\r\n")
        return prefix + f"## {title}{nl}" + (nl + stripped + nl if stripped else ""), title
    check_headings(new, h.level)
    lines = _lines(body)
    start, end = section_range(body, h)
    heading_line = _with_end(lines[start], nl)
    content = lines[start + 1 : end]
    after = lines[end:]
    stripped = new.strip("\r\n")
    if mode == "append":
        tail: list[str] = []
        while content and not content[-1].strip():
            tail.insert(0, content.pop())
        if content:
            content[-1] = _with_end(content[-1], nl)
        block = [stripped + nl] if stripped else []
        if not tail and after:
            tail = [nl]
        merged = [heading_line, *content, *block, *tail]
    else:
        lead = [nl] if content and not content[0].strip() else []
        merged = [heading_line, *lead, *([stripped + nl] if stripped else [])]
        if after:
            merged.append(nl)
    return "".join(lines[:start]) + "".join(merged) + "".join(after), h.text


def append_end(body: str, text: str) -> str:
    """Append ``text`` at the end of the body, separated by a blank line."""
    nl = newline_of(body) if body else "\n"
    stripped = _convert(text, nl).strip("\r\n")
    prefix = _with_end(body, nl)
    if prefix.strip() and not prefix.endswith(nl + nl):
        prefix += nl
    return prefix + stripped + nl


def count_bullets(body: str, heading: str = "Open points") -> int:
    for h in headings(body):
        if h.text == heading:
            lines = _lines(section_text(body, h))[1:]
            return sum(1 for line in lines if _BULLET.match(line))
    return 0
