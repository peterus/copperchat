"""Path guard and safe file access for ``docs/design/`` (spec §7.5, plan Review Focus 3).

Every reader and writer of design docs goes through this module: the tools, REST, the
index walk, the change scan, revisions and the templates. Rules:

- a raw path is a bare ``<name>.md`` or starts with ``docs/design/``; at most two
  lowercase segments below ``docs/design`` that ``fullmatch`` the segment pattern;
- symlinks are refused at every lexical component (``lstat``), and every open uses
  ``O_NOFOLLOW`` relative to a directory fd chain, so a component swapped for a symlink
  after the check fails the open instead of being followed;
- only regular files of at most 256 KiB are read (``O_NONBLOCK``: a FIFO never blocks);
- writes create a temp file with ``O_CREAT|O_EXCL|O_NOFOLLOW`` in the opened directory fd
  and ``os.replace`` it relative to that fd: a write never lands outside the project.

All functions here are blocking; callers run them in ``asyncio.to_thread``.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import secrets
import stat
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.projects import Project, agent_dir
from copperchat.research.paths import resolve_inside

ROOT = "docs/design"
ROOT_PARTS = ("docs", "design")
FIXED_DOCS = ("requirements.md", "architecture.md", "parts.md", "decisions.md", "board.md")
MAX_DOC_BYTES = 256 * 1024
MAX_DOCS = 50
MAX_PATH_CHARS = 200
MAX_DEPTH = 2
# Bounds of the enumeration walk besides MAX_DOCS (a cloned repo may bring junk).
MAX_SCAN_ENTRIES = 500
MAX_IGNORED = 50

_SEGMENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

# HTTP status per error code (REST); the tools return the same codes as tool errors.
HTTP_STATUS = {
    "doc_path_refused": 400,
    "doc_conflict": 409,
    "doc_user_edited": 409,
    "doc_append_only": 409,
    "doc_scope_refused": 403,
    "doc_heading_refused": 400,
    "doc_frontmatter_refused": 400,
    "doc_too_large": 413,
    "doc_read_too_large": 413,
    "doc_section_not_found": 404,
    "doc_section_ambiguous": 400,
    "doc_limit": 429,
    "doc_not_found": 404,
    "invalid_input": 400,
}


class DocError(StructuredToolError):
    """A refused or failed doc operation. ``code`` is one of the S-C6 codes; messages
    never contain absolute paths."""

    def __init__(
        self,
        code: str,
        what: str,
        why: str,
        suggestion: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(what, why, suggestion, code=code, details=details)
        self.http_status = HTTP_STATUS.get(code, 400)


def refused(why: str) -> DocError:
    return DocError(
        "doc_path_refused",
        "Design doc path refused",
        why,
        "Use a path like docs/design/requirements.md (lowercase, .md, at most two levels "
        "below docs/design).",
    )


@dataclass(frozen=True)
class DocPath:
    """A lexically valid doc path. ``segments`` are the parts below ``docs/design``."""

    segments: tuple[str, ...]

    @property
    def rel(self) -> str:
        return "/".join((*ROOT_PARTS, *self.segments))

    @property
    def name(self) -> str:
        return self.segments[-1]

    @property
    def fixed(self) -> bool:
        return len(self.segments) == 1 and self.name in FIXED_DOCS

    @property
    def doc_key(self) -> str:
        """Front matter ``doc`` value: the path below docs/design without ``.md``."""
        return "/".join(self.segments)[: -len(".md")]

    def parts(self) -> tuple[str, ...]:
        return (*ROOT_PARTS, *self.segments)


def parse_doc_path(raw: object) -> DocPath:
    """Lexical rules only (no file system access)."""
    if not isinstance(raw, str) or not raw:
        raise refused("the path is empty")
    if len(raw) > MAX_PATH_CHARS:
        raise refused(f"the path is longer than {MAX_PATH_CHARS} characters")
    if "\\" in raw or _CONTROL.search(raw):
        raise refused("the path contains a backslash or a control character")
    if raw.startswith("/"):
        raise refused("absolute paths are not allowed")
    parts = raw.split("/")
    if len(parts) == 1:
        segments: list[str] = parts
    elif tuple(parts[:2]) == ROOT_PARTS:
        segments = parts[2:]
    else:
        raise refused("design docs live under docs/design/")
    if not segments or len(segments) > MAX_DEPTH:
        raise refused("at most two levels below docs/design are allowed")
    for seg in segments:
        if not _SEGMENT.fullmatch(seg):
            raise refused("each segment must be lowercase letters, digits, '.', '_' or '-'")
    if not segments[-1].endswith(".md") or segments[-1] == ".md":
        raise refused("design docs are .md files")
    return DocPath(tuple(segments))


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        raise refused("a path component cannot be inspected") from None


def resolve_doc_path(project: Project, raw: object) -> DocPath:
    """Lexical rules plus ``lstat`` of every lexical component (``docs``, ``docs/design``,
    intermediate directories, the file): a symlink anywhere is refused, as is a target
    that exists but is not a regular file. ``resolve_inside`` double-checks containment."""
    doc = parse_doc_path(raw)
    node = project.dir
    parts = doc.parts()
    for i, part in enumerate(parts):
        node = node / part
        st = _lstat(node)
        if st is None:
            break
        if stat.S_ISLNK(st.st_mode):
            raise refused("symlinks are not allowed in docs/design")
        last = i == len(parts) - 1
        if not last and not stat.S_ISDIR(st.st_mode):
            raise refused("a path component is not a directory")
        if last and not stat.S_ISREG(st.st_mode):
            raise refused("the target is not a regular file")
    base = project.dir / ROOT
    if resolve_inside(doc.rel, project.dir, [base]) is None:
        raise refused("the path resolves outside docs/design")
    return doc


# ---- dir-fd primitives -------------------------------------------------------------------


def _race_hook(stage: str, **info: Any) -> None:
    """Test seam: called between the checks and the opens (``stage`` = ``checked``,
    ``dir_opened``). Production: no-op."""


def _open_dirs(base: Path, components: Sequence[str], *, create: bool = False) -> int | None:
    """Open ``base`` and then each component with ``O_DIRECTORY|O_NOFOLLOW`` relative to
    the previous fd. Returns the last fd (caller closes) or None when a component is
    missing (and ``create`` is False). A symlinked or non-directory component raises
    ``refused``."""
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for comp in components:
            try:
                nxt = os.open(comp, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                with contextlib.suppress(FileExistsError):
                    os.mkdir(comp, 0o755, dir_fd=fd)
                try:
                    nxt = os.open(comp, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    raise _dir_refusal(exc) from None
            except OSError as exc:
                raise _dir_refusal(exc) from None
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


def _dir_refusal(exc: OSError) -> DocError:
    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
        return refused("a directory of the path is a symlink or not a directory")
    return refused("a directory of the path cannot be opened")


@dataclass(frozen=True)
class FileData:
    data: bytes
    mtime_ns: int
    size: int


def _read_at(dir_fd: int, name: str, cap: int = MAX_DOC_BYTES) -> FileData | None:
    """Open ``name`` in ``dir_fd`` with ``O_NOFOLLOW|O_NONBLOCK|O_NOCTTY``; ``fstat`` must
    show a regular file of at most ``cap`` bytes; reads at most ``cap + 1`` bytes."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise refused("the target is a symlink") from None
        raise refused("the target cannot be opened") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise refused("the target is not a regular file")
        if st.st_size > cap:
            raise too_large_file()
        chunks: list[bytes] = []
        total = 0
        while total <= cap:
            chunk = os.read(fd, cap + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > cap:
            raise too_large_file()
        return FileData(b"".join(chunks), st.st_mtime_ns, total)
    finally:
        os.close(fd)


def too_large_file() -> DocError:
    return DocError(
        "doc_too_large",
        "Design doc is too large",
        f"design docs are limited to {MAX_DOC_BYTES // 1024} KiB",
        "Split the content into a smaller doc.",
    )


def io_failed(exc: OSError) -> DocError:
    """An unexpected OS error, reported without the file name (no absolute paths)."""
    return DocError(
        "doc_path_refused",
        "Design doc file access failed",
        exc.strerror or type(exc).__name__,
        "Tell the user; the file system refused the operation.",
    )


def safe_read(project: Project, doc: DocPath) -> FileData | None:
    """Read a doc through the guard; None if it (or a directory of it) does not exist."""
    try:
        return _safe_read(project, doc)
    except OSError as exc:
        raise io_failed(exc) from None


def _safe_read(project: Project, doc: DocPath) -> FileData | None:
    resolve_doc_path(project, doc.rel)
    _race_hook("checked", doc=doc)
    dir_fd = _open_dirs(project.dir, doc.parts()[:-1])
    if dir_fd is None:
        return None
    try:
        return _read_at(dir_fd, doc.name)
    finally:
        os.close(dir_fd)


def safe_write(project: Project, doc: DocPath, data: bytes) -> FileData:
    """Doc-only atomic writer, relative to an ``O_NOFOLLOW`` directory fd chain."""
    try:
        return _safe_write(project, doc, data)
    except OSError as exc:
        raise io_failed(exc) from None


def _safe_write(project: Project, doc: DocPath, data: bytes) -> FileData:
    if len(data) > MAX_DOC_BYTES:
        raise too_large_file()
    resolve_doc_path(project, doc.rel)
    _race_hook("checked", doc=doc)
    dir_fd = _open_dirs(project.dir, doc.parts()[:-1], create=True)
    assert dir_fd is not None
    try:
        _race_hook("dir_opened", doc=doc)
        try:
            st = os.stat(doc.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            st = None
        if st is not None:
            if stat.S_ISLNK(st.st_mode):
                raise refused("the target is a symlink")
            if not stat.S_ISREG(st.st_mode):
                raise refused("the target is not a regular file")
            if st.st_nlink > 1:
                raise refused("the target is a hard link")
        tmp = f".{doc.name}.{secrets.token_hex(6)}.tmp"
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, doc.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dir_fd)
            raise
        # best effort: the rename survives a crash; the write itself already succeeded
        with contextlib.suppress(OSError):
            os.fsync(dir_fd)
        st = os.stat(doc.name, dir_fd=dir_fd, follow_symlinks=False)
        return FileData(data, st.st_mtime_ns, st.st_size)
    finally:
        os.close(dir_fd)


# ---- enumeration -------------------------------------------------------------------------


@dataclass(frozen=True)
class Ignored:
    path: str
    reason: str  # symlink | not a regular file | too large | too many docs | bad name


@dataclass(frozen=True)
class WalkEntry:
    doc: DocPath
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class WalkResult:
    docs: tuple[WalkEntry, ...]
    ignored: tuple[Ignored, ...]


def _scan_dir(fd: int, budget: list[int], skip: frozenset[str] = frozenset()) -> list[str]:
    """Entry names of ``fd``, sorted; ``budget[0]`` bounds the entries of the whole walk."""
    names: list[str] = []
    with os.scandir(fd) as it:
        for entry in it:
            if budget[0] <= 0:
                break
            budget[0] -= 1
            if entry.name not in skip:
                names.append(entry.name)
    return sorted(names)


def walk(project: Project) -> WalkResult:
    """List docs under ``docs/design`` (depth ≤ 2, at most 50, never following
    symlinks). Nothing is opened for reading; rejected entries come back ``ignored``.
    The fixed docs are looked up by name first, so junk entries cannot crowd them out;
    at most ``MAX_SCAN_ENTRIES`` entries are inspected in total."""
    from copperchat.design_docs.sanitize import sanitize_field

    docs: list[WalkEntry] = []
    ignored: list[Ignored] = []
    budget = [MAX_SCAN_ENTRIES]

    def ignore(path: str, reason: str) -> None:
        if len(ignored) < MAX_IGNORED:
            # names come from the repository: one sanitised line, never a fake notice
            ignored.append(Ignored(sanitize_field(path, MAX_PATH_CHARS), reason))

    try:
        root_fd = _open_dirs(project.dir, ROOT_PARTS)
    except (DocError, OSError):
        return WalkResult((), (Ignored(ROOT, "symlink"),))
    if root_fd is None:
        return WalkResult((), ())

    def visit_entry(fd: int, prefix: tuple[str, ...], name: str) -> None:
        rel = "/".join((*ROOT_PARTS, *prefix, name))
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError:
            return
        if stat.S_ISLNK(st.st_mode):
            ignore(rel, "symlink")
            return
        if stat.S_ISDIR(st.st_mode):
            if prefix or not _SEGMENT.fullmatch(name):
                ignore(rel, "bad name")
                return
            try:
                sub = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except OSError:
                ignore(rel, "symlink")
                return
            try:
                for child in _scan_dir(sub, budget):
                    visit_entry(sub, (*prefix, name), child)
            finally:
                os.close(sub)
            return
        try:
            doc = parse_doc_path(rel)
        except DocError:
            ignore(rel, "bad name")
            return
        if not stat.S_ISREG(st.st_mode):
            ignore(rel, "not a regular file")
        elif st.st_size > MAX_DOC_BYTES:
            ignore(rel, "too large")
        elif len(docs) >= MAX_DOCS:
            ignore(rel, "too many docs")
        else:
            docs.append(WalkEntry(doc, st.st_mtime_ns, st.st_size))

    try:
        for name in FIXED_DOCS:
            visit_entry(root_fd, (), name)
        for name in _scan_dir(root_fd, budget, frozenset(FIXED_DOCS)):
            visit_entry(root_fd, (), name)
    except OSError:
        pass  # a directory vanished mid-walk: return what was found
    finally:
        os.close(root_fd)
    return WalkResult(tuple(docs), tuple(ignored))


def iso_from_ns(mtime_ns: int) -> str:
    return (
        datetime.fromtimestamp(mtime_ns / 1e9, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---- templates ---------------------------------------------------------------------------

TEMPLATE_DIR = ("design-docs", "templates")


def template_layers(
    settings_data_dir: Path, project: Project
) -> Iterator[tuple[Path, tuple[str, ...]]]:
    """(trusted base, components below it) per layer: project, user, built-in."""
    from copperchat.skills.store import BUILTIN_ROOT

    yield project.dir, (*agent_dir(project).relative_to(project.dir).parts, "skills", *TEMPLATE_DIR)
    yield settings_data_dir, ("skills", *TEMPLATE_DIR)
    yield BUILTIN_ROOT, TEMPLATE_DIR


def read_template(settings_data_dir: Path, project: Project, name: str) -> str | None:
    """``templates/<name>`` resolved per file: project layer, then user, then built-in.
    Every layer is read with the same safe opener; a symlinked, special or oversized
    template is skipped and the next layer is tried."""
    if name not in FIXED_DOCS:
        return None
    for base, comps in template_layers(settings_data_dir, project):
        try:
            fd = _open_dirs(base, comps)
        except (DocError, OSError):
            continue
        if fd is None:
            continue
        try:
            data = _read_at(fd, name)
        except DocError:
            data = None
        finally:
            os.close(fd)
        if data is not None:
            return data.data.decode("utf-8", errors="replace")
    return None
