"""``DesignDocs``: versions, conflicts, approvals, revisions and write policies (plan S-C6,
spec §7.2-§7.6).

- The version is ``"v" + sha256(body)[:16]``; every conflict check hashes the actual file
  bytes, read under the per-project doc lock.
- Approval is ``doc_approvals`` state; the front matter only mirrors it.
- All file I/O goes through ``design_docs.paths`` in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import builtins
import difflib
import html
import re
import unicodedata
from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.protocol import DocUpdatedFrame
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.design_docs import frontmatter as fm
from copperchat.design_docs import paths, sections
from copperchat.design_docs.paths import DocError, DocPath, Ignored
from copperchat.design_docs.repo import DocRepo, RevisionRow
from copperchat.design_docs.sanitize import sanitize_field
from copperchat.projects import Project

MAX_BODY_BYTES = 64 * 1024
MAX_APPEND_BYTES = 8 * 1024
MAX_READ_BYTES = 64 * 1024
MAX_SUMMARY_CHARS = 200
MAX_TITLE_CHARS = 120
MAX_MUTATIONS_PER_TURN = 40
# Conflicts on one doc in one turn after which the agent is told to stop and ask (C5).
MAX_CONFLICTS_PER_TURN = 3
DIFF_LINE_LIMIT = 4000
_EXACT_DIFF_LINES = 200
CONFLICT_DIFF_BYTES = 8 * 1024
DECISIONS = "docs/design/decisions.md"
PARTS = "docs/design/parts.md"
BOARD = "docs/design/board.md"
TRUNCATED = "… (truncated)"
_TURN_KEYS = 512
_LINE_BREAK = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_DECISION_HEADING = re.compile(r"^ {0,3}#{1,2}(?:[ \t]|$)")
# an emphasised "Session:" claim (*Session:, _Session:, **Session**:, ...); linear: the
# emphasis run separates the two whitespace runs
_SESSION_LINE = re.compile(r"^[ \t\\]*[*_]+[ \t]*session[*_]*[ \t]*:", re.IGNORECASE)
_HTML_BLOCK = re.compile(r"^[ \t]*<(?:h[1-6]\b|!--|em\b|i\b|strong\b|b\b)", re.IGNORECASE)
# a setext underline turns the previous line into a heading for Markdown renderers
_SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")

Author = Literal["agent", "user", "external"]
Op = Literal["write", "patch", "append"]


# ---- context types -----------------------------------------------------------------------


@dataclass(frozen=True)
class ConversationInfo:
    """What C3 needs of a conversation (built from ``ToolContext`` fields, S-C5)."""

    id: str
    seq: int
    title: str
    type: str

    @property
    def label(self) -> str:
        return f"{self.title} — {self.type.capitalize()}"


@dataclass(frozen=True)
class DocNotice:
    kind: Literal["changed", "approval_lost"]
    path: str
    revision_id: int | None
    added: int
    removed: int
    sections: tuple[str, ...]
    conversation_id: str | None
    run_id: str | None
    # The body version after the change (C5: the conversation's ``docs_seen``).
    version: str | None = None


def scope_refused(why: str) -> DocError:
    return DocError(
        "doc_scope_refused",
        "This change is outside this session's write scope",
        why,
        "Record the point under 'Open points' or in decisions.md instead, or ask the user "
        "to make the change in a Requirements or Free session.",
    )


@dataclass(frozen=True)
class WritePolicy:
    """Doc write policy of a session type (spec §8.2)."""

    name: str

    def check(self, op: Op, doc: DocPath, section: str | None = None) -> None:
        path = doc.rel
        if self.name == "free":
            return
        if self.name == "requirements":
            if path == BOARD:
                raise scope_refused("board.md is not written in requirements sessions")
            return
        if self.name == "schematic":
            if path == DECISIONS and op == "append" and section is None:
                return
            if op != "write" and (
                section == "Open points" or (section == "Key parts" and path == PARTS)
            ):
                return
            raise scope_refused(
                "schematic sessions may only append to decisions.md and update the "
                "'Open points' sections and the 'Key parts' section of parts.md"
            )
        if self.name == "board":
            if op == "append" and section is None and path in (DECISIONS, BOARD):
                return
            raise scope_refused("board sessions may only append to decisions.md and board.md")
        raise scope_refused(f"unknown session type {self.name!r}")

    def check_create(self, doc: DocPath, exists: bool) -> None:
        """Schematic sessions update existing docs (and the decision log); they never
        create a new doc."""
        if self.name == "schematic" and not exists and doc.rel != DECISIONS:
            raise scope_refused("schematic sessions only update existing docs")


_POLICIES = {name: WritePolicy(name) for name in ("free", "requirements", "schematic", "board")}
FREE = _POLICIES["free"]


def policy_for(conversation_type: str | None, conversation_id: str | None = None) -> WritePolicy:
    """Fail closed: no conversation at all (plain tests) is Free; a conversation without a
    known type is refused."""
    if conversation_type is None:
        if conversation_id is None:
            return FREE
        raise scope_refused("the session type is unknown")
    policy = _POLICIES.get(conversation_type)
    if policy is None:
        raise scope_refused(f"unknown session type {conversation_type!r}")
    return policy


# ---- results -----------------------------------------------------------------------------


@dataclass(frozen=True)
class DocState:
    doc: DocPath
    exists: bool
    text: str
    parsed: fm.Parsed
    version: str | None
    bytes: int
    mtime_ns: int | None

    @property
    def body(self) -> str:
        return self.parsed.body


@dataclass(frozen=True)
class DocRead:
    path: str
    exists: bool
    version: str | None
    status: str
    user_edited: bool
    approved: bool
    approved_version: str | None
    approved_at: str | None
    body: str
    headings: list[str]
    template: str | None

    @property
    def changed_since_approval(self) -> bool:
        return self.exists and self.approved_version is not None and not self.approved

    def meta(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "userEdited": self.user_edited,
            "approved": self.approved,
            "approvedVersion": self.approved_version,
            "approvedAt": self.approved_at,
            "changedSinceApproval": self.changed_since_approval,
        }


@dataclass(frozen=True)
class DocWriteResult:
    path: str
    version: str
    revision_id: int | None
    added: int
    removed: int
    notices: tuple[DocNotice, ...] = ()
    stale_base: bool = False


@dataclass(frozen=True)
class DocIndexEntry:
    path: str
    fixed: bool
    exists: bool
    approved: bool
    changed_since_approval: bool
    approved_version: str | None
    approved_at: str | None
    user_edited: bool
    version: str | None
    bytes: int
    updated_at: str | None
    headings: list[str]
    open_points: int
    last_author: str | None
    last_conversation_title: str | None

    def dto(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "fixed": self.fixed,
            "exists": self.exists,
            "approved": self.approved,
            "changedSinceApproval": self.changed_since_approval,
            "approvedVersion": self.approved_version,
            "approvedAt": self.approved_at,
            "userEdited": self.user_edited,
            "version": self.version,
            "bytes": self.bytes,
            "updatedAt": self.updated_at,
            "headings": self.headings,
            "openPoints": self.open_points,
            "lastAuthor": self.last_author,
            "lastConversationTitle": self.last_conversation_title,
        }

    def snake(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass(frozen=True)
class DocIndex:
    root: str
    docs: list[DocIndexEntry]
    ignored: list[Ignored] = field(default_factory=list)

    def dto(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "docs": [d.dto() for d in self.docs],
            "ignored": [{"path": i.path, "reason": i.reason} for i in self.ignored],
        }

    def snake(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "docs": [d.snake() for d in self.docs],
            "ignored": [{"path": i.path, "reason": i.reason} for i in self.ignored],
        }


@dataclass(frozen=True)
class RevisionContent:
    content: str
    prev_content: str | None


# ---- helpers -----------------------------------------------------------------------------


def invalid(what: str, why: str) -> DocError:
    return DocError("invalid_input", what, why, "Fix the argument and call again.")


def _has_format_chars(text: str) -> bool:
    """Zero-width and bidi controls (category Cf) can disguise a one-line field."""
    return any(unicodedata.category(ch) == "Cf" for ch in text)


def _strip_cf(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _single_line(
    value: object, name: str, limit: int, *, required: bool = True, allow_format: bool = False
) -> str:
    if not isinstance(value, str):
        raise invalid(f"{name} must be a string", f"got {type(value).__name__}")
    text = value.strip()
    if required and not text:
        raise invalid(f"{name} is empty", f"{name} needs 1-{limit} characters")
    if (
        len(text) > limit
        or _LINE_BREAK.search(text)
        or (not allow_format and _has_format_chars(text))
    ):
        raise invalid(f"{name} is invalid", f"{name}: one line, at most {limit} characters")
    return text


def _too_large(what: str, limit: int) -> DocError:
    return DocError(
        "doc_too_large",
        f"{what} is too large",
        f"the limit is {limit // 1024} KiB",
        "Split the change into smaller calls, or move content into another doc.",
    )


def _check_text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise invalid(f"{name} must be a string", f"got {type(value).__name__}")
    if len(value.encode("utf-8")) > limit:
        raise _too_large(name, limit)
    return value


def line_counts(old: str, new: str) -> tuple[int, int]:
    """(added, removed) lines: exact (difflib, no autojunk) up to 200 lines, a linear
    multiset count above (exact except for moved lines)."""
    a, b = old.splitlines(), new.splitlines()
    if max(len(a), len(b)) > _EXACT_DIFF_LINES:
        ca, cb = Counter(a), Counter(b)
        return sum((cb - ca).values()), sum((ca - cb).values())
    added = removed = 0
    # small inputs only: difflib's worst case on repetitive lines is far beyond linear
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def conflict_diff(base: str | None, current: str, base_version: str, version: str) -> str | None:
    """Unified diff base -> current (bodies), capped at 8 KiB; None above 4,000 lines."""
    if base is None:
        return None
    a, b = base.splitlines(keepends=True), current.splitlines(keepends=True)
    if max(len(a), len(b)) > DIFF_LINE_LIMIT:
        return None
    text = "".join(difflib.unified_diff(a, b, fromfile=base_version, tofile=version))
    data = text.encode("utf-8")
    if len(data) <= CONFLICT_DIFF_BYTES:
        return text
    marker = "\n… (diff truncated)\n"
    cut = data[: CONFLICT_DIFF_BYTES - len(marker.encode("utf-8"))]
    return cut.decode("utf-8", errors="ignore") + marker


def truncate_utf8(text: str, limit: int, marker: str = TRUNCATED) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    room = limit - len(marker.encode("utf-8")) - 1
    return data[: max(room, 0)].decode("utf-8", errors="ignore").rstrip() + "\n" + marker


def escape_entry_text(text: str) -> str:
    """Escape headings of level ≤ 2 and ``*Session:`` lines (server-written entries), and
    close a code fence the text leaves open."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fence = sections.open_fence(text)
    if fence is not None:
        text = text.rstrip("\n") + "\n" + fence
    out: list[str] = []
    prev = ""
    for line in text.split("\n"):
        if _forged_line(line, prev):
            i = len(line) - len(line.lstrip())
            line = line[:i] + "\\" + line[i:]
        out.append(line)
        prev = line
    return "\n".join(out)


def _forged_line(line: str, prev: str) -> bool:
    """A line that would start another decisions entry (ATX heading of level ≤ 2, or a
    setext underline below text) or claim a session (``*Session:`` in any emphasis
    form). Zero-width/bidi characters are ignored for the check."""
    bare = _strip_cf(line)
    if bare.lstrip().startswith("\\"):
        return False  # already escaped
    if _DECISION_HEADING.match(bare) or _SESSION_LINE.match(bare) or _HTML_BLOCK.match(bare):
        return True
    if _session_lookalike(bare):
        return True
    return bool(_SETEXT.match(bare)) and bool(_strip_cf(prev).strip())


_FILLERS = frozenset("\u3164\u115f\u1160\uffa0\u2800\u00a0")  # blank-looking letters
_TAG = re.compile(r"<[^<>]{0,60}>")
# after tags/entities are gone: optional quote/list container, then an emphasised
# "Session:" claim; bounded input (200 chars), so the overlapping blanks stay cheap
_CLAIM = re.compile(
    r"^[ \t>]*(?:(?:[-+]|\d{1,9}[.)])[ \t]+)?(?P<pre>[*_]*)[ \t]*session(?P<post>[*_]*)[ \t]*:",
    re.IGNORECASE,
)


def _session_lookalike(line: str) -> bool:
    """An emphasised or tagged 'Session:' claim in any container (``1. *Session:*``,
    ``> _Session_:``, ``<em>Session:``, entities, full-width forms, blank-looking filler
    letters). Prose such as 'Session handling: JWT' or a plain 'Session: …' is fine.
    (Cross-script look-alikes are a residual; this guards the rendered log only.)"""
    text = unicodedata.normalize("NFKC", html.unescape(line[:1000]))
    text = "".join(ch for ch in text if ch not in _FILLERS and unicodedata.category(ch) != "Cf")
    stripped = _TAG.sub("", text)
    m = _CLAIM.match(stripped[:200])
    if m is None:
        return False
    # tagged only if a tag stood in front of the claim (tags later in the line are prose)
    return bool(m.group("pre") or m.group("post")) or not text.startswith(stripped[: m.end()])


def _starts_with_dashes(body: str) -> bool:
    return body.lstrip("\ufeff \t\r\n").startswith("---")


def _frontmatter_refused() -> DocError:
    return DocError(
        "doc_frontmatter_refused",
        "The body would start with '---'",
        "the front matter is owned by the server; a leading '---' block would pose as one",
        "Remove the leading '---' line.",
    )


def _decision_entry(title: str, text: str, conversation: ConversationInfo | None) -> str:
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    who = (
        f"{conversation.title} (#{conversation.seq}, {conversation.type})"
        if conversation is not None
        else "none"
    )
    return f"## {date} — {title}\n{text.strip(chr(10))}\n\n*Session: {who}*"


def _load_sync(project: Project, doc: DocPath) -> DocState:
    data = paths.safe_read(project, doc)
    if data is None:
        return DocState(doc, False, "", fm.Parsed({}), None, 0, None)
    try:
        text = data.data.decode("utf-8")
    except UnicodeDecodeError:
        # never rewrite (and so corrupt) a doc in another encoding
        raise DocError(
            "doc_path_refused",
            "Design doc is not UTF-8",
            "the file is not valid UTF-8 text",
            "Ask the user to convert the file to UTF-8.",
        ) from None
    parsed = fm.split(text)
    return DocState(doc, True, text, parsed, fm.body_version(parsed.body), data.size, data.mtime_ns)


@dataclass(frozen=True)
class _Summary:
    headings: list[str]
    open_points: int


@dataclass(frozen=True)
class _Scan:
    states: list[DocState]
    ignored: list[Ignored]
    summaries: dict[str, _Summary] = field(default_factory=dict)


def _summary(body: str) -> _Summary:
    return _Summary(
        [sanitize_field(h) for h in sections.heading_texts(body, 2)[:40]],
        sections.count_bullets(body),
    )


def _scan_sync(project: Project) -> _Scan:
    result = paths.walk(project)
    states: list[DocState] = []
    ignored = list(result.ignored)
    for entry in result.docs:
        try:
            states.append(_load_sync(project, entry.doc))
        except DocError as err:
            if err.code == "doc_too_large":
                reason = "too large"
            elif "UTF-8" in err.what:
                reason = "not utf-8"
            else:
                reason = "not a regular file"
            ignored.append(Ignored(entry.doc.rel, reason))
    summaries = {st.doc.rel: _summary(st.body) for st in states}
    return _Scan(states, ignored, summaries)


# ---- the service -------------------------------------------------------------------------


class DesignDocs:
    def __init__(
        self, db: Database, settings: Settings, broadcaster: Broadcaster | None = None
    ) -> None:
        self.repo = DocRepo(db)
        self.settings = settings
        self.broadcaster = broadcaster
        self._locks: dict[str, asyncio.Lock] = {}
        self._turns: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._conflicts: OrderedDict[tuple[str, str, str], int] = OrderedDict()
        self._vcache: dict[tuple[str, str], tuple[int, int, str]] = {}

    def lock(self, project_id: str) -> asyncio.Lock:
        return self._locks.setdefault(project_id, asyncio.Lock())

    # ---- reading

    async def _resolve(self, project: Project, raw: object) -> DocPath:
        return await asyncio.to_thread(paths.resolve_doc_path, project, raw)

    async def _load(self, project: Project, doc: DocPath) -> DocState:
        return await asyncio.to_thread(_load_sync, project, doc)

    async def _template(self, project: Project, doc: DocPath) -> str | None:
        if not doc.fixed:
            return None
        text = await asyncio.to_thread(
            paths.read_template, self.settings.data_dir, project, doc.name
        )
        return None if text is None else fm.split(text).body

    async def _capture_external(
        self, project: Project, state: DocState, latest: RevisionRow | None
    ) -> RevisionRow | None:
        """Record an ``external`` revision the first time a version shows up that the
        server did not write (spec §7.3). Returns the newest revision afterwards."""
        if not state.exists or state.version is None:
            return latest
        if latest is not None and latest.version == state.version:
            return latest
        prev_body = ""
        if latest is not None:
            prev = await self.repo.content_of(project.id, state.doc.rel, latest.id)
            prev_body = fm.split(prev or "").body
        added, removed = await asyncio.to_thread(line_counts, prev_body, state.body)
        await self.repo.insert_revision(
            project.id,
            state.doc.rel,
            version=state.version,
            prev_version=None if latest is None else latest.version,
            added=added,
            removed=removed,
            author="external",
            content=state.text,
            summary="changed outside CopperChat",
        )
        await self.repo.prune(project.id)
        return await self.repo.latest(project.id, state.doc.rel)

    async def list(self, project: Project) -> DocIndex:
        async with self.lock(project.id):
            scan = await asyncio.to_thread(_scan_sync, project)
            latest = await self.repo.latest_per_path(project.id)
            approvals = await self.repo.approvals(project.id)
            for st in scan.states:
                rev = latest.get(st.doc.rel)
                if rev is None or rev.version != st.version:
                    new = await self._capture_external(project, st, rev)
                    if new is not None:
                        latest[st.doc.rel] = new
        by_path = {s.doc.rel: s for s in scan.states}
        ignored_paths = {i.path for i in scan.ignored}
        order = [f"{paths.ROOT}/{n}" for n in paths.FIXED_DOCS]
        order += [p for p in by_path if p not in order]
        entries: list[DocIndexEntry] = []
        for rel in order:
            entry_state = by_path.get(rel)
            if entry_state is None and rel in ignored_paths:
                continue
            doc = paths.parse_doc_path(rel)
            appr = approvals.get(rel)
            rev = latest.get(rel)
            version = entry_state.version if entry_state is not None else None
            approved = appr is not None and version is not None and appr.body_version == version
            summary = scan.summaries.get(rel) or _Summary([], 0)
            entries.append(
                DocIndexEntry(
                    path=rel,
                    fixed=doc.fixed,
                    exists=entry_state is not None,
                    approved=approved and rel != DECISIONS,
                    changed_since_approval=entry_state is not None
                    and appr is not None
                    and not approved,
                    approved_version=None if appr is None else appr.body_version,
                    approved_at=None if appr is None else appr.approved_at,
                    user_edited=entry_state is not None and entry_state.parsed.user_edited,
                    version=version,
                    bytes=0 if entry_state is None else entry_state.bytes,
                    updated_at=None
                    if entry_state is None or entry_state.mtime_ns is None
                    else paths.iso_from_ns(entry_state.mtime_ns),
                    headings=summary.headings,
                    open_points=summary.open_points,
                    last_author=None if rev is None else rev.author,
                    last_conversation_title=None
                    if rev is None or rev.conversation_label is None
                    else sanitize_field(rev.conversation_label),
                )
            )
        return DocIndex(paths.ROOT, entries, scan.ignored)

    async def read(self, project: Project, path: object, section: str | None = None) -> DocRead:
        doc = await self._resolve(project, path)
        async with self.lock(project.id):
            state = await self._load(project, doc)
            latest = await self.repo.latest(project.id, doc.rel)
            await self._capture_external(project, state, latest)
            appr = await self.repo.approval(project.id, doc.rel)
        template = None if state.exists else await self._template(project, doc)
        source = state.body if state.exists else (template or "")

        def parse() -> tuple[str, list[str]]:
            text = state.body
            if section is not None:
                text = sections.section_text(source, sections.find(source, section))
            return text, sections.heading_texts(source)

        body, heads = await asyncio.to_thread(parse)
        approved = (
            appr is not None
            and state.version is not None
            and appr.body_version == state.version
            and doc.rel != DECISIONS
        )
        return DocRead(
            path=doc.rel,
            exists=state.exists,
            version=state.version,
            status="approved" if approved else "draft",
            user_edited=state.parsed.user_edited,
            approved=approved,
            approved_version=None if appr is None else appr.body_version,
            approved_at=None if appr is None else appr.approved_at,
            body=body,
            headings=heads,
            template=template,
        )

    async def versions(self, project: Project) -> dict[str, str]:
        """Current body versions of all readable docs, hashes cached by (mtime_ns, size).
        For change notices only - never for a conflict check."""

        def scan() -> dict[str, str]:
            out: dict[str, str] = {}
            for entry in paths.walk(project).docs:
                key = (project.id, entry.doc.rel)
                cached = self._vcache.get(key)
                if cached is not None and cached[:2] == (entry.mtime_ns, entry.size):
                    out[entry.doc.rel] = cached[2]
                    continue
                try:
                    state = _load_sync(project, entry.doc)
                except DocError:
                    continue
                if state.version is not None and state.mtime_ns is not None:
                    self._vcache[key] = (state.mtime_ns, state.bytes, state.version)
                    out[entry.doc.rel] = state.version
            return out

        return await asyncio.to_thread(scan)

    async def revisions(
        self, project: Project, path: object, limit: int = 50
    ) -> builtins.list[RevisionRow]:
        doc = paths.parse_doc_path(path)
        return await self.repo.list(project.id, doc.rel, max(1, min(int(limit), 100)))

    async def revision(self, project: Project, path: object, rid: int) -> RevisionContent:
        doc = paths.parse_doc_path(path)
        found = (
            await self.repo.get_with_prev(project.id, doc.rel, rid)
            if 1 <= rid < 2**63
            else None  # outside SQLite's INTEGER range: no such revision
        )
        if found is None:
            raise DocError(
                "doc_not_found", "Revision not found", f"no revision {rid} of {doc.rel}", ""
            )
        return RevisionContent(*found)

    # ---- writing

    def _check_budget(self, project_id: str, run_id: str) -> None:
        if self._turns.get((project_id, run_id), 0) >= MAX_MUTATIONS_PER_TURN:
            raise DocError(
                "doc_limit",
                "Too many doc changes in this turn",
                f"at most {MAX_MUTATIONS_PER_TURN} doc mutations per turn",
                "Finish this turn and continue in the next one.",
            )

    def _count(self, project_id: str, run_id: str) -> None:
        key = (project_id, run_id)
        self._turns[key] = self._turns.get(key, 0) + 1
        self._turns.move_to_end(key)
        while len(self._turns) > _TURN_KEYS:
            self._turns.popitem(last=False)

    async def _publish(
        self,
        project: Project,
        path: str,
        version: str,
        approved: bool,
        user_edited: bool,
        revision_id: int | None,
        author: Author,
        conversation_id: str | None,
    ) -> None:
        if self.broadcaster is None:
            return
        await self.broadcaster.publish(
            project.id,
            DocUpdatedFrame(
                project_id=project.id,
                path=path,
                version=version,
                status="approved" if approved else "draft",
                user_edited=user_edited,
                revision_id=revision_id,
                author=author,
                conversation_id=conversation_id,
            ),
        )

    async def _mutate(
        self,
        project: Project,
        doc: DocPath,
        *,
        author: Author,
        base_version: str | None,
        base_mode: Literal["require", "optional"],
        transform: Callable[[DocState, str], tuple[str, tuple[str, ...]]],
        summary: str,
        conversation: ConversationInfo | None,
        run_id: str | None,
        user_edited: bool | None = None,
        before: Callable[[DocState], None] | None = None,
    ) -> DocWriteResult:
        conflict: tuple[str | None, str, str, RevisionRow | None] | None = None
        stale = False
        async with self.lock(project.id):
            state = await self._load(project, doc)
            latest = await self.repo.latest(project.id, doc.rel)
            latest = await self._capture_external(project, state, latest)
            if base_version != state.version:
                if base_mode == "require":
                    base = None
                    if base_version is not None:
                        raw = await self.repo.content_by_version(project.id, doc.rel, base_version)
                        base = None if raw is None else fm.split(raw).body
                    conflict = (base, state.body, state.version or "missing", latest)
                else:
                    stale = base_version is not None
            if conflict is None:
                if before is not None:
                    before(state)
                if not state.exists:
                    count = await asyncio.to_thread(lambda: len(paths.walk(project).docs))
                    if count >= paths.MAX_DOCS:
                        raise DocError(
                            "doc_limit",
                            "Too many design docs",
                            f"docs/design holds at most {paths.MAX_DOCS} docs",
                            "Add the content to an existing doc.",
                        )
                source = state.body
                if not state.exists:
                    source = await self._template(project, doc) or ""
                if author == "agent" and run_id is not None:
                    self._check_budget(project.id, run_id)
                new_body, touched = await asyncio.to_thread(transform, state, source)
                if (
                    author == "agent"
                    and _starts_with_dashes(new_body)
                    and not (_starts_with_dashes(state.body))
                ):
                    raise _frontmatter_refused()
                result = await self._commit(
                    project,
                    state,
                    latest,
                    new_body,
                    touched,
                    author=author,
                    summary=summary,
                    conversation=conversation,
                    run_id=run_id,
                    user_edited=user_edited,
                    stale=stale,
                )
                if author == "agent" and run_id is not None:
                    self._count(project.id, run_id)  # only successful changes count
                return result
        base, current, version, newest = conflict
        diff = await asyncio.to_thread(
            conflict_diff, base, current, base_version or "missing", version
        )
        suggestion = "Re-read the doc (doc_read) and apply your change to the current text."
        if author == "agent" and run_id is not None:
            key = (project.id, run_id, doc.rel)
            self._conflicts[key] = self._conflicts.get(key, 0) + 1
            self._conflicts.move_to_end(key)
            while len(self._conflicts) > _TURN_KEYS:
                self._conflicts.popitem(last=False)
            if self._conflicts[key] >= MAX_CONFLICTS_PER_TURN:
                suggestion = (
                    f"{doc.rel} keeps changing while you edit it "
                    f"({self._conflicts[key]} conflicts in this turn): stop and ask the "
                    "user before you change it again."
                )
        raise DocError(
            "doc_conflict",
            f"{doc.rel} changed since version {base_version or '(none)'}",
            f"the current version is {version}",
            suggestion,
            details={
                "current_version": None if version == "missing" else version,
                "author": None if newest is None else newest.author,
                "conversation_title": None if newest is None else newest.conversation_label,
                "diff": diff,
            },
        )

    async def _commit(
        self,
        project: Project,
        state: DocState,
        latest: RevisionRow | None,
        new_body: str,
        touched: tuple[str, ...],
        *,
        author: Author,
        summary: str,
        conversation: ConversationInfo | None,
        run_id: str | None,
        user_edited: bool | None,
        stale: bool,
    ) -> DocWriteResult:
        doc = state.doc
        appr = await self.repo.approval(project.id, doc.rel)
        new_version = fm.body_version(new_body)
        was_approved = appr is not None and appr.body_version == state.version
        approved = appr is not None and appr.body_version == new_version and doc.rel != DECISIONS
        edited = state.parsed.user_edited if user_edited is None else user_edited
        text = fm.build(
            doc_key=doc.doc_key,
            approved=approved,
            approved_at=None if appr is None else appr.approved_at,
            user_edited=edited,
            body=new_body,
            extra_lines=state.parsed.extra_lines,
            newline=fm.newline_of(state.text or new_body),
        )
        if len(text.encode("utf-8")) > paths.MAX_DOC_BYTES:
            raise paths.too_large_file()
        conversation_id = None if conversation is None else conversation.id
        if state.exists and new_body == state.body:
            if text != state.text:
                await asyncio.to_thread(paths.safe_write, project, doc, text.encode("utf-8"))
                await self._publish(
                    project, doc.rel, new_version, approved, edited, None, author, conversation_id
                )
            return DocWriteResult(
                doc.rel, new_version, None if latest is None else latest.id, 0, 0, (), stale
            )
        await asyncio.to_thread(paths.safe_write, project, doc, text.encode("utf-8"))
        added, removed = await asyncio.to_thread(line_counts, state.body, new_body)
        rid = await self.repo.insert_revision(
            project.id,
            doc.rel,
            version=new_version,
            prev_version=state.version,
            added=added,
            removed=removed,
            author=author,
            content=text,
            summary=summary,
            conversation_id=conversation_id,
            run_id=run_id,
            conversation_label=None if conversation is None else conversation.label,
        )
        await self.repo.prune(project.id)
        notices: list[DocNotice] = []
        if author == "agent":
            notices.append(
                DocNotice(
                    "changed",
                    doc.rel,
                    rid,
                    added,
                    removed,
                    touched,
                    conversation_id,
                    run_id,
                    new_version,
                )
            )
            if was_approved and not approved:
                notices.append(
                    DocNotice(
                        "approval_lost",
                        doc.rel,
                        rid,
                        added,
                        removed,
                        touched,
                        conversation_id,
                        run_id,
                        new_version,
                    )
                )
        await self._publish(
            project, doc.rel, new_version, approved, edited, rid, author, conversation_id
        )
        return DocWriteResult(doc.rel, new_version, rid, added, removed, tuple(notices), stale)

    def _agent_checks(
        self, doc: DocPath, op: Op, run_id: str | None, policy: WritePolicy | None
    ) -> WritePolicy:
        if run_id is None:
            raise scope_refused("no active turn")
        if doc.rel == DECISIONS and op != "append":
            raise DocError(
                "doc_append_only",
                "decisions.md is append-only",
                "the decision log only grows",
                "Use doc_append with a title to add a decision.",
            )
        if policy is None:
            raise scope_refused("no write policy")
        return policy

    async def write(
        self,
        project: Project,
        path: object,
        body: object,
        base_version: str | None,
        *,
        author: Author,
        summary: str = "",
        conversation: ConversationInfo | None = None,
        run_id: str | None = None,
        policy: WritePolicy | None = None,
    ) -> DocWriteResult:
        """Replace the whole body. ``base_version`` None = create (refused if the doc
        exists). Agent: refused on decisions.md and on user-edited docs; user (PUT):
        sets ``user_edited``."""
        doc = await self._resolve(project, path)
        if author == "agent":
            pol = self._agent_checks(doc, "write", run_id, policy)
            pol.check("write", doc)
            summary = _single_line(summary, "summary", MAX_SUMMARY_CHARS)
            text = _check_text(body, "body", MAX_BODY_BYTES)
            if _starts_with_dashes(text):
                raise _frontmatter_refused()
        else:
            text = _check_text(body, "body", paths.MAX_DOC_BYTES)

        def before(state: DocState) -> None:
            if author == "agent" and state.exists and state.parsed.user_edited:
                raise DocError(
                    "doc_user_edited",
                    f"{doc.rel} was edited by the user",
                    "user-edited docs are not replaced as a whole",
                    "Change it with doc_patch_section or doc_append, or ask the user to "
                    "allow a rewrite (Docs panel: Let the agent rewrite).",
                )

        return await self._mutate(
            project,
            doc,
            author=author,
            base_version=base_version,
            base_mode="require",
            transform=lambda _s, _src: (text, ()),
            summary=summary,
            conversation=conversation,
            run_id=run_id,
            user_edited=True if author == "user" else None,
            before=before,
        )

    async def patch_section(
        self,
        project: Project,
        path: object,
        heading: object,
        body: object,
        base_version: str | None,
        *,
        mode: str = "replace",
        create: bool = False,
        summary: str = "",
        conversation: ConversationInfo | None = None,
        run_id: str | None = None,
        policy: WritePolicy | None = None,
    ) -> DocWriteResult:
        doc = await self._resolve(project, path)
        pol = self._agent_checks(doc, "patch", run_id, policy)
        if mode not in ("replace", "append"):
            raise invalid("mode is invalid", "mode is 'replace' or 'append'")
        query = _single_line(heading, "heading", 200, allow_format=True)
        # scope first (by the addressed name), so a refused call learns nothing else;
        # re-checked below with the heading the query resolves to
        pol.check("patch", doc, sections.normalize(query.rsplit("/", 1)[-1]))
        summary = _single_line(summary, "summary", MAX_SUMMARY_CHARS)
        text = _check_text(body, "body", MAX_BODY_BYTES)

        def transform(state: DocState, source: str) -> tuple[str, tuple[str, ...]]:
            pol.check_create(doc, state.exists)
            try:
                found: sections.Heading | None = sections.find(source, query)
            except DocError as err:
                if err.code != "doc_section_not_found" or not create:
                    raise
                found = None
            target = found.text if found is not None else sections.normalize(query)
            pol.check("patch", doc, target)
            if (
                found is not None
                and mode == "replace"
                and state.exists
                and state.parsed.user_edited
                and (found.level == 1 or sections.spans_whole_doc(source, found))
            ):
                # a level-1 section spans the whole doc: replacing it is a doc_write
                raise DocError(
                    "doc_user_edited",
                    f"{doc.rel} was edited by the user",
                    "this section spans the whole doc, so replacing it replaces the doc",
                    "Patch a level-2 section, or append.",
                )
            new, title = sections.patch(source, query, text, mode=mode, create=create)
            return new, (title,)

        return await self._mutate(
            project,
            doc,
            author="agent",
            base_version=base_version,
            base_mode="require",
            transform=transform,
            summary=summary,
            conversation=conversation,
            run_id=run_id,
        )

    async def append(
        self,
        project: Project,
        path: object,
        text: object,
        *,
        summary: str = "",
        section: str | None = None,
        title: str | None = None,
        base_version: str | None = None,
        conversation: ConversationInfo | None = None,
        run_id: str | None = None,
        policy: WritePolicy | None = None,
    ) -> DocWriteResult:
        doc = await self._resolve(project, path)
        pol = self._agent_checks(doc, "append", run_id, policy)
        summary = _single_line(summary, "summary", MAX_SUMMARY_CHARS)
        body = _check_text(text, "text", MAX_APPEND_BYTES)
        # one line-ending convention for the checks and the write (a lone CR is a line
        # break for Markdown renderers); an open fence would swallow later headings
        body = body.replace("\r\n", "\n").replace("\r", "\n")
        sections.check_fences(body)
        query = (
            None if section is None else _single_line(section, "section", 200, allow_format=True)
        )
        pol.check(
            "append", doc, None if query is None else sections.normalize(query.rsplit("/", 1)[-1])
        )
        if doc.rel == DECISIONS:
            if query is not None:
                raise invalid("section is not allowed", "decisions.md entries go to the end")
            if title is None:
                raise invalid("title is required", "decisions.md entries need a title")
            heading = _single_line(title, "title", MAX_TITLE_CHARS)
            prev = ""
            for line in body.split("\n"):
                # strict for the agent: escaped forms are refused too
                forged = _forged_line(_strip_cf(line).lstrip().lstrip("\\"), prev)
                prev = line
                if forged:
                    raise DocError(
                        "doc_heading_refused",
                        "The entry text contains a heading or a '*Session:' line",
                        "an entry may not start another entry or claim another session",
                        "Use headings of level 3 or deeper, no '---'/'===' underlines, and "
                        "leave out '*Session:' lines.",
                    )
            body = _decision_entry(heading, body, conversation)

        def transform(state: DocState, source: str) -> tuple[str, tuple[str, ...]]:
            pol.check_create(doc, state.exists)
            if query is None:
                pol.check("append", doc, None)
                return sections.append_end(source, body), ()
            target = sections.find(source, query).text
            pol.check("append", doc, target)
            new, name = sections.patch(source, query, body, mode="append", create=False)
            return new, (name,)

        return await self._mutate(
            project,
            doc,
            author="agent",
            base_version=base_version,
            base_mode="optional",
            transform=transform,
            summary=summary,
            conversation=conversation,
            run_id=run_id,
        )

    async def append_server_entry(
        self, project: Project, title: str, text: str, *, conversation: ConversationInfo
    ) -> DocWriteResult | None:
        """Server-written decisions.md entry (close-session handover, spec §10): escape
        first, then truncate the UTF-8 bytes to 8 KiB. None when decisions.md is full."""
        doc = paths.parse_doc_path(DECISIONS)
        heading = sanitize_field(title, MAX_TITLE_CHARS)
        # escape, truncate, and escape again: a cut can turn '##x' into a bare '##'
        entry_text = escape_entry_text(
            # cut to a bounded length first: the checks never scan a huge summary
            truncate_utf8(escape_entry_text(text[: 2 * MAX_APPEND_BYTES]), MAX_APPEND_BYTES - 64)
        )
        fence = sections.open_fence(entry_text)  # truncation may cut a closing fence
        if fence is not None:
            entry_text = entry_text + "\n" + fence
        entry = _decision_entry(heading, entry_text, conversation)
        try:
            return await self._mutate(
                project,
                doc,
                author="agent",
                base_version=None,
                base_mode="optional",
                transform=lambda _s, src: (sections.append_end(src, entry), ()),
                summary=heading,
                conversation=conversation,
                run_id=None,
            )
        except DocError as err:
            if err.code == "doc_too_large":
                return None
            raise

    # ---- user state changes (REST)

    async def _set_state(
        self,
        project: Project,
        path: object,
        version: str,
        action: Literal["approve", "unapprove", "clear"],
    ) -> str:
        doc = await self._resolve(project, path)
        if action == "approve" and doc.rel == DECISIONS:
            raise scope_refused("decisions.md has no approval")
        async with self.lock(project.id):
            state = await self._load(project, doc)
            latest = await self.repo.latest(project.id, doc.rel)
            await self._capture_external(project, state, latest)
            if not state.exists or state.version != version:
                raise DocError(
                    "doc_conflict",
                    f"{doc.rel} changed since version {version}",
                    f"the current version is {state.version or '(missing)'}",
                    "Reload the doc and try again.",
                    details={"current_version": state.version},
                )
            assert state.version is not None
            if action == "approve":
                appr = await self.repo.set_approval(project.id, doc.rel, state.version)
            elif action == "unapprove":
                await self.repo.delete_approval(project.id, doc.rel)
                appr = None
            else:
                appr = await self.repo.approval(project.id, doc.rel)
            approved = appr is not None and appr.body_version == state.version
            edited = False if action == "clear" else state.parsed.user_edited
            text = fm.build(
                doc_key=doc.doc_key,
                approved=approved,
                approved_at=None if appr is None else appr.approved_at,
                user_edited=edited,
                body=state.body,
                extra_lines=state.parsed.extra_lines,
                newline=fm.newline_of(state.text),
            )
            if text != state.text:
                await asyncio.to_thread(paths.safe_write, project, doc, text.encode("utf-8"))
        await self._publish(project, doc.rel, state.version, approved, edited, None, "user", None)
        return state.version

    async def approve(self, project: Project, path: object, version: str) -> str:
        return await self._set_state(project, path, version, "approve")

    async def unapprove(self, project: Project, path: object, version: str) -> str:
        return await self._set_state(project, path, version, "unapprove")

    async def clear_user_edited(self, project: Project, path: object, version: str) -> str:
        return await self._set_state(project, path, version, "clear")

    async def revert(
        self, project: Project, path: object, rid: int, base_version: str | None
    ) -> str:
        content = (await self.revision(project, path, rid)).content
        result = await self.write(
            project,
            path,
            fm.split(content).body,
            base_version,
            author="user",
            summary=f"reverted to revision {rid}",
        )
        return result.version
