"""Library service: symbol index (eager), symbol detail incl. derived-symbol pins, and
footprint libraries (lazy, one library per request) - E-C9, spike 2B, owner decision 1.

Data comes only from KiCad's library API: listing = `GetLibraryItems` (names only),
details = `GetItemsFromLibrary` in batches of 500 ids. KiCad returns derived (`extends`)
symbols flattened since 0f5acecd (2026-10-18); a derived symbol it returns without items
(unresolvable parent) falls back to the flattened definition of a placement inside a
dropped commit (spike 2B).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import difflib
import logging
import os
import stat
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from copperchat.errors import DocumentNotOpen, StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadSession, KicadUnavailable, LibrariesLoading
from copperchat.library.index import Index, Scope, SymbolRow, tokenize
from copperchat.library.local import NAME_RE, LocalLibrary, derive_local_library
from copperchat.library.packages import FootprintDetail, footprint_from_proto
from copperchat.library.symbols import SymbolDetail, detail_from_proto, has_drawing, row_from_proto
from copperchat.repo_context.paths import repo_policy

if TYPE_CHECKING:
    from copperchat.kicad_tools.footprints.model import FootprintGeometry

log = logging.getLogger(__name__)

BATCH = 500  # ids per GetItemsFromLibrary request (spike 2B: 6.5 s for all symbols)
MAX_LIMIT = 50
MAX_QUERY_CHARS = 200
FAILED_RETRY_S = 300.0  # a footprint library that failed to load is retried after this
DETAIL_CACHE_SIZE = 512
GEOMETRY_CACHE_SIZE = 64  # footprint_geometry LRU (fp_get), by lib id
FOOTPRINT_SEARCH_LIBRARIES = 3  # libraries loaded for a search without `library`
State = Literal["loading", "ready", "failed"]

_services: weakref.WeakKeyDictionary[KicadSession, LibraryService] = weakref.WeakKeyDictionary()


def library_for(session: KicadSession) -> LibraryService:
    """The library service of `session` (one per session object; a reopened project
    gets a new session and with it a new service)."""
    service = _services.get(session)
    if service is None:
        service = LibraryService(session)
        _services[session] = service
    return service


@contextlib.contextmanager
def _kicad_errors(what: str) -> Iterator[None]:
    """KiCad refusals inside the library service become structured tool errors (the
    agent can act on them); KicadUnavailable passes through to the adapter."""
    from kipy.errors import ApiError

    try:
        yield
    except LibrariesLoading as exc:
        raise StructuredToolError(
            what,
            exc.message,
            "Wait a few seconds and call the library tool again.",
            code="libraries_loading",
        ) from exc
    except (ApiError, DocumentNotOpen) as exc:
        raise StructuredToolError(
            what,
            f"KiCad refused the library request: {exc}",
            "Use another library or symbol, or ask the user to check the library tables.",
            code="library_error",
        ) from exc


def _invalid(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


def _query(query: Any, example: str) -> str:
    q = str(query or "").strip()
    if not q:
        raise _invalid("Empty query", "query is required", example)
    if len(q) > MAX_QUERY_CHARS:
        raise _invalid("Query too long", f"at most {MAX_QUERY_CHARS} characters", example)
    return q


def _limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise _invalid("Invalid limit", "limit must be an integer", f"1..{MAX_LIMIT}")
    return max(1, min(limit, MAX_LIMIT))


def _send(h: KicadHandle, command: Any, response_type: Any) -> Any:
    # kipy has no batched GetItemsFromLibrary wrapper (get_library_symbol fetches one id
    # per request, 11 ms each); its client sends any command.
    return h.kicad._client.send(command, response_type)


def _get_items(h: KicadHandle, library_type: int, ids: Iterable[tuple[str, str]]) -> Any:
    from kipy.proto.common.commands import editor_commands_pb2, library_commands_pb2

    command = library_commands_pb2.GetItemsFromLibrary()
    command.type = library_type  # type: ignore[assignment]
    for nickname, name in ids:
        command.item_ids.add(library_nickname=nickname, entry_name=name)
    return _send(h, command, editor_commands_pb2.GetItemsResponse).items


def _symbol_type() -> int:
    from kipy.proto.common.types import LibraryType

    return int(LibraryType.LT_SYMBOL)


def _footprint_type() -> int:
    from kipy.proto.common.types import LibraryType

    return int(LibraryType.LT_FOOTPRINT)


def _global_footprint_nicknames(h: KicadHandle) -> frozenset[str]:
    from kipy.proto.common.types import LibraryTableScope, LibraryType

    table = h.kicad.get_library_table(LibraryType.LT_FOOTPRINT, LibraryTableScope.LTS_GLOBAL)
    return frozenset(row.nickname for row in table.rows)


def _scopes(library_type: int) -> Callable[[KicadHandle], dict[str, Scope]]:
    def run(h: KicadHandle) -> dict[str, Scope]:
        from kipy.proto.common.types import LibraryTableScope

        out: dict[str, Scope] = {}
        for status in h.kicad.get_library_statuses(library_type):
            project = status.scope == LibraryTableScope.LTS_PROJECT
            # A nickname in both tables resolves to the project row first.
            if project or status.nickname not in out:
                out[status.nickname] = "project" if project else "global"
        return out

    return run


def _symbol_ids(h: KicadHandle) -> list[tuple[str, str]]:
    return [(i.proto.library_nickname, i.proto.entry_name) for i in h.kicad.get_library_symbols()]


def _split(lib_id: str) -> tuple[str, str]:
    nickname, _, name = lib_id.partition(":")
    return nickname, name


def _fetch_rows(
    ids: list[tuple[str, str]], scopes: dict[str, Scope], h: KicadHandle
) -> list[SymbolRow]:
    from kipy.proto.schematic import schematic_types_pb2

    rows: list[SymbolRow] = []
    for packed in _get_items(h, _symbol_type(), ids):
        sy = schematic_types_pb2.SchematicSymbol()
        packed.Unpack(sy)
        rows.append(row_from_proto(sy, scopes.get(sy.id.library_nickname, "global")))
    return rows


def _fetch_symbol(lib_id: str, h: KicadHandle) -> Any:
    from kipy.proto.schematic import schematic_types_pb2

    for packed in _get_items(h, _symbol_type(), [_split(lib_id)]):
        sy = schematic_types_pb2.SchematicSymbol()
        packed.Unpack(sy)
        return sy
    return None


def _place_probe(lib_id: str, h: KicadHandle) -> Any:
    from kipy.geometry import Vector2
    from kipy.proto.schematic import schematic_types_pb2

    placed = h.schematic().place_symbol_from_library(lib_id, Vector2.from_xy(0, 0))
    definition = schematic_types_pb2.SchematicSymbol()
    definition.CopyFrom(placed.definition.proto)
    return definition


def _footprint_names(nickname: str, h: KicadHandle) -> list[str]:
    return [i.proto.entry_name for i in h.kicad.get_library_footprints([nickname])]


def _fetch_footprints(nickname: str, names: list[str], h: KicadHandle) -> list[FootprintDetail]:
    from kipy.proto.board import board_types_pb2

    out: list[FootprintDetail] = []
    for packed in _get_items(h, _footprint_type(), [(nickname, n) for n in names]):
        fp = board_types_pb2.Footprint()
        packed.Unpack(fp)
        out.append(footprint_from_proto(fp))
    return out


def _fetch_footprint_proto(nickname: str, name: str, h: KicadHandle) -> Any:
    """The library `Footprint` of one id (batched `GetItemsFromLibrary` with one id), or
    None when KiCad does not serve it."""
    from kipy.proto.board import board_types_pb2

    for packed in _get_items(h, _footprint_type(), [(nickname, name)]):
        fp = board_types_pb2.Footprint()
        packed.Unpack(fp)
        return fp
    return None


def _project_footprint_rows(h: KicadHandle) -> list[tuple[str, str, str]]:
    """(nickname, uri, substituted uri) of every row of the project footprint table."""
    from kipy.proto.common.types import LibraryTableScope, LibraryType

    fp, project = LibraryType.LT_FOOTPRINT, LibraryTableScope.LTS_PROJECT
    raw = h.kicad.get_library_table(fp, project).rows
    resolved = {
        r.nickname: r.uri for r in h.kicad.get_library_table(fp, project, substituted=True).rows
    }
    return [(r.nickname, r.uri, resolved.get(r.nickname, r.uri)) for r in raw]


# -- local library (FP-C1) -------------------------------------------------------------


@dataclass(frozen=True)
class LocalLibraryInfo:
    lib: LocalLibrary
    row_present: bool  # a project row with the local nickname and the local URI
    row_conflict: str | None  # why the project table conflicts with the local library


def _conflict(why: str, suggestion: str, path: Path) -> StructuredToolError:
    return StructuredToolError(
        "The local footprint library is not safe to write",
        why,
        suggestion,
        code="library_conflict",
        details={"path": str(path)},
    )


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _check_chain(base: Path, target: Path, *, leaf_file: bool) -> None:
    """lstat every existing component below `base` down to `target`: none may be a
    symlink; all but a file leaf must be directories (the leaf, with `leaf_file`, a
    regular file)."""
    parts = target.relative_to(base).parts
    current = base
    for i, part in enumerate(parts):
        current = current / part
        st = _lstat(current)
        if st is None:
            return  # nothing below a missing component exists
        if stat.S_ISLNK(st.st_mode):
            raise _conflict(
                f"{current} is a symbolic link",
                "CopperChat writes only into real directories of the repository. Ask the "
                "user to replace the link with a directory (or file) of its own.",
                current,
            )
        want_file = leaf_file and i == len(parts) - 1
        ok = stat.S_ISREG(st.st_mode) if want_file else stat.S_ISDIR(st.st_mode)
        if not ok:
            kind = "a regular file" if want_file else "a directory"
            raise _conflict(
                f"{current} is not {kind}",
                "Ask the user to move it out of the way of the local footprint library.",
                current,
            )


def local_library_dir_safe(lib: LocalLibrary) -> None:
    """Raise `library_conflict` unless every existing path component from the repo root
    to the library directory is a real directory (no symlink) and the project's
    `fp-lib-table`, if it exists, is a regular file without a symlink component."""
    _check_chain(lib.repo_root, lib.dir, leaf_file=False)
    try:
        lib.table.relative_to(lib.repo_root)
    except ValueError:  # the project lies outside the repo root: check its own directory
        _check_chain(lib.table.parent, lib.table, leaf_file=True)
    else:
        _check_chain(lib.repo_root, lib.table, leaf_file=True)


def local_footprint_file_safe(lib: LocalLibrary, name: str) -> None:
    """Raise `library_conflict` if `<dir>/<name>.kicad_mod` exists and is not a regular
    file (a symlink or a directory); `invalid_argument` for a name outside `NAME_RE`.

    Meaningful only after `local_library_dir_safe(lib)` passed: it checks the file, not
    the directories above it. Writers call both under the library lock immediately
    before the first stage."""
    if not NAME_RE.match(name):
        raise _invalid(
            f"Invalid footprint name {name[:80]!r}",
            "footprint names match ^[A-Za-z0-9][A-Za-z0-9_.+-]{0,99}$",
            "Use letters, digits and _ . + - only.",
        )
    path = lib.dir / f"{name}.kicad_mod"
    st = _lstat(path)
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise _conflict(
            f"{path} is not a regular file",
            "Ask the user to remove the link or directory in the local footprint library.",
            path,
        )


def _row_conflict(lib: LocalLibrary, rows: list[tuple[str, str, str]]) -> tuple[bool, str | None]:
    own = _norm_uri(lib.uri)
    present = False
    conflicts: list[str] = []
    local_dir = os.path.normpath(lib.dir.resolve())
    table_dir = lib.table.parent
    for nickname, uri, substituted in rows:
        if nickname == lib.nickname:
            if _norm_uri(uri) == own:
                present = True
            else:
                conflicts.append(
                    f"the project footprint table has a row {nickname!r} with URI {uri!r}, "
                    f"but CopperChat's local library {nickname!r} must point at {lib.uri!r}"
                )
        elif _same_dir(substituted, local_dir, table_dir):
            conflicts.append(
                f"the project footprint table row {nickname!r} ({uri!r}) points at the local "
                f"footprint library directory, which CopperChat uses under the nickname "
                f"{lib.nickname!r}; ask the user to rename or remove the row {nickname!r}"
            )
    return present, "; ".join(conflicts) or None


def _norm_uri(uri: str) -> str:
    return uri.strip().replace("\\", "/").rstrip("/")


def _same_dir(uri: str, local_dir: str, table_dir: Path) -> bool:
    """`uri` (substituted) names `local_dir`; a relative URI is taken relative to the
    directory of the project table."""
    if "${" in uri or "://" in uri:  # unresolved variable or a remote library
        return False
    try:
        return os.path.normpath((table_dir / uri).resolve()) == local_dir
    except (OSError, RuntimeError, ValueError):
        return False


def _build_index(rows: Iterable[tuple[str, str, str, Scope]]) -> Index:
    index = Index()
    for lib_id, description, keywords, scope in rows:
        index.add(lib_id, description, keywords, scope)
    index.finish()
    return index


@dataclass(frozen=True)
class _CachedGeometry:
    geometry: FootprintGeometry
    # local footprints: the .kicad_mod file and its lstat validator taken before the read
    # (st_ino, st_size, st_mtime_ns); a hit whose file changed or vanished is dropped
    file: Path | None = None
    validator: tuple[int, int, int] | None = None


def _validator(st: os.stat_result) -> tuple[int, int, int]:
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _file_validator(path: Path) -> tuple[int, int, int] | None:
    """The validator of a regular file (no symlink), None when missing or not regular."""
    st = _lstat(path)
    if st is None or not stat.S_ISREG(st.st_mode):
        return None
    return _validator(st)


def _repo_root_unknown(transient: str, what: str) -> StructuredToolError:
    return StructuredToolError(
        "The repository root is unknown right now",
        f"git could not answer ({transient}), so CopperChat cannot tell where the "
        "repo's footprint library lives",
        f"Try again in a moment; {what}.",
        code="repo_root_unknown",
    )


@dataclass
class _FootprintLibrary:
    details: dict[str, FootprintDetail]
    index: Index


class LibraryService:
    def __init__(self, session: KicadSession) -> None:
        self._session_ref = weakref.ref(session)
        self.state: State = "loading"
        self.error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._rows: dict[str, SymbolRow] = {}
        self._libraries: set[str] = set()
        self._index: Index | None = None
        self._details: OrderedDict[str, SymbolDetail] = OrderedDict()
        self._fp_scopes: dict[str, Scope] | None = None
        self._fp_libs: dict[str, _FootprintLibrary] = {}
        self._fp_locks: dict[str, asyncio.Lock] = {}
        # Footprint libraries that failed to load (nickname -> reason).
        # nickname -> (reason, loop time); forgotten after FAILED_RETRY_S (KiCad may have
        # been busy or restarted).
        self._fp_failed: dict[str, tuple[str, float]] = {}
        self._global_fp_nicknames: tuple[int, frozenset[str]] | None = None
        # footprint_geometry LRU (lib id -> geometry); cleared by invalidate_footprints
        self._fp_geometry: OrderedDict[str, _CachedGeometry] = OrderedDict()

    @property
    def _session(self) -> KicadSession:
        session = self._session_ref()
        if session is None:
            raise KicadUnavailable("the KiCad session of this library service is gone")
        return session

    async def global_footprint_nicknames(self) -> frozenset[str]:
        """Nicknames of the global footprint table, cached per session generation.

        Probed on a real api-server (F1 Task 1): `GetLibraryTable(LT_FOOTPRINT, LTS_GLOBAL)`
        answers on a boardless server before any footprint document was opened.
        """
        session = self._session
        generation = session.generation
        cached = self._global_fp_nicknames
        if cached is not None and cached[0] == generation:
            return cached[1]
        nicknames = await session.call(_global_footprint_nicknames)
        self._global_fp_nicknames = (generation, nicknames)
        return nicknames

    # -- warm-up -------------------------------------------------------------------
    @property
    def started(self) -> bool:
        """True once a warm-up was started (by the open route or a lib_* call)."""
        return self._task is not None

    async def start(self) -> None:
        """Start the background warm-up (idempotent; a failed warm-up is retried)."""
        self._spawn()

    def _spawn(self) -> None:
        if self.state == "ready" or (self._task is not None and not self._task.done()):
            return
        self.state, self.error = "loading", None
        self._task = asyncio.create_task(self._warm_up(), name="library-warm-up")

    async def wait_ready(self, timeout: float) -> bool:
        """Start the warm-up if needed and wait up to `timeout` s for it."""
        self._spawn()
        task = self._task
        if self.state != "ready" and task is not None:
            # Never cancels the warm-up when the caller gives up or is cancelled.
            await asyncio.wait({task}, timeout=timeout)
        return self.state == "ready"

    async def _warm_up(self) -> None:
        try:
            session = self._session
            await session.ensure_symbol_libraries()
            scopes = await session.call(_scopes(_symbol_type()))
            ids = await session.call(_symbol_ids)
            rows: dict[str, SymbolRow] = {}
            for i in range(0, len(ids), BATCH):
                batch = ids[i : i + BATCH]
                for row in await session.call(partial(_fetch_rows, batch, scopes)):
                    rows[row.lib_id] = row
            for nickname, name in ids:  # KiCad silently omits ids it cannot serve
                rows.setdefault(
                    f"{nickname}:{name}",
                    SymbolRow(f"{nickname}:{name}", scope=scopes.get(nickname, "global")),
                )
            entries = [(r.lib_id, r.description, r.keywords, r.scope) for r in rows.values()]
            index = await asyncio.to_thread(_build_index, entries)
            self._rows = rows
            self._libraries = {_split(lib_id)[0] for lib_id in rows}
            self._index = index
            self.state = "ready"
            log.info("symbol library index ready: %d symbols", len(rows))
        except asyncio.CancelledError:
            self.state, self.error = "failed", "cancelled"
            raise
        except Exception as exc:
            log.warning("symbol library warm-up failed: %s", exc)
            self.state, self.error = "failed", str(exc) or type(exc).__name__

    def _ready_index(self) -> Index:
        if self.state == "ready" and self._index is not None:
            return self._index
        failed = self.state == "failed"
        why = (
            f"the last attempt failed ({self.error}); it was restarted"
            if failed
            else "KiCad is still loading the symbol libraries and CopperChat is indexing them"
        )
        self._spawn()
        raise StructuredToolError(
            "The symbol library index is not ready yet",
            why,
            "Wait a few seconds and call the library tool again.",
            code="libraries_loading",
        )

    # -- symbols -------------------------------------------------------------------
    def _check_library(self, library: str | None, known: Iterable[str], kind: str) -> None:
        if library is None or library in known:
            return
        close = difflib.get_close_matches(library, list(known), n=5, cutoff=0.5)
        raise StructuredToolError(
            f"{kind} library {library!r} not found",
            "no library with this nickname is in the library tables",
            f"Closest nicknames: {close}" if close else "Search without `library`.",
            code="not_found",
        )

    async def search_symbols(
        self, query: str, library: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        index = self._ready_index()
        q = _query(query, "e.g. 'LDO 3.3V' or 'LM358'")
        self._check_library(library, self._libraries, "Symbol")
        # A broad query over ~23k symbols can take ~100 ms: keep it off the event loop.
        hits = await asyncio.to_thread(index.search, q, _limit(limit), library)
        return [self._rows[lib_id].to_dict() for lib_id, _ in hits]

    def has_symbol(self, lib_id: str) -> bool:
        """`lib_id` is in the index (False while the warm-up has not finished)."""
        return self.state == "ready" and lib_id in self._rows

    async def get_symbol(self, lib_id: str) -> SymbolDetail:
        """Detail of one library symbol; a symbol KiCad returns without items gets its
        pins from a probe placement in a dropped commit (`session.commit(dry_run=True)`).
        Must not be called from inside an open commit of the same task."""
        index = self._ready_index()
        lib_id = lib_id.strip()
        cached = self._details.get(lib_id)
        if cached is not None:
            self._details.move_to_end(lib_id)
            return cached
        row = self._rows.get(lib_id)
        if row is None:
            close = await asyncio.to_thread(self._closest, index, lib_id)
            raise StructuredToolError(
                f"Symbol {lib_id!r} is not in the libraries",
                "unknown lib_id (format <library nickname>:<symbol name>, case-sensitive)",
                f"Closest matches: {close}" if close else "Use lib_search_symbols.",
                code="not_found",
            )
        session = self._session
        with _kicad_errors(f"Could not read symbol {lib_id}"):
            proto = await session.call(partial(_fetch_symbol, lib_id))
        if proto is None:
            raise StructuredToolError(
                f"KiCad did not return symbol {lib_id!r}",
                "the library may have changed since CopperChat indexed it",
                "Search again with lib_search_symbols.",
                code="not_found",
            )
        if has_drawing(proto):
            detail = detail_from_proto(proto)
        elif session.read_only:  # a probe is a (dropped) write: refused on read-only
            detail = detail_from_proto(proto, pins_known=False)
        else:
            detail = detail_from_proto(proto, await self._probe(session, lib_id))
        if detail.pins_known:
            row.pin_count = len({p.number for p in detail.pins})
        self._details[lib_id] = detail
        while len(self._details) > DETAIL_CACHE_SIZE:
            self._details.popitem(last=False)
        return detail

    def _closest(self, index: Index, lib_id: str) -> list[str]:
        nickname, name = _split(lib_id)
        if nickname in self._libraries and name:
            same = [r for r in self._rows if r.startswith(f"{nickname}:")]
            close = difflib.get_close_matches(lib_id, same, n=5, cutoff=0.6)
            if close:
                return close
        return [hit for hit, _ in index.search(name or lib_id, 5)]

    async def _probe(self, session: KicadSession, lib_id: str) -> Any:
        """The flattened definition of a derived symbol (spike 2B, finding 3); the commit
        is always dropped, so the document stays unchanged and unmodified."""
        with _kicad_errors(f"Could not read the pins of {lib_id}"):
            await session.ensure_symbol_libraries()  # placing needs loaded libraries
            async with session.commit(f"probe {lib_id}", dry_run=True):
                return await session.call(partial(_place_probe, lib_id))

    # -- footprints ----------------------------------------------------------------
    def footprint_libraries_ready(self) -> bool:
        """KiCad serves footprint libraries with a board open (spike 2B, Q4) or once a
        footprint tool activated them (`footprints_active`, F1); a boardless project
        never loads them otherwise (owner decision 3)."""
        session = self._session
        return session.project.pcb_file is not None or session.footprints_active

    def _require_footprints(self) -> None:
        if not self.footprint_libraries_ready():
            raise StructuredToolError(
                "Footprint libraries are not available",
                "footprint libraries are not loaded (no board, no footprint tool used)",
                "Pick the footprint by its usual KiCad name (e.g. "
                "Resistor_SMD:R_0805_2012Metric) and tell the user it is unverified.",
                code="footprints_unavailable",
            )

    async def footprint_libraries(self) -> dict[str, Scope]:
        """Footprint library nicknames from the tables (no library is loaded)."""
        self._require_footprints()
        scopes = self._fp_scopes
        if scopes is None:
            with _kicad_errors("Could not list the footprint libraries"):
                scopes = await self._session.call(_scopes(_footprint_type()))
            self._fp_scopes = scopes
        return scopes

    async def _footprint_library(self, nickname: str) -> _FootprintLibrary:
        loaded = self._fp_libs.get(nickname)
        if loaded is not None:
            return loaded
        lock = self._fp_locks.setdefault(nickname, asyncio.Lock())
        async with lock:
            loaded = self._fp_libs.get(nickname)
            if loaded is not None:
                return loaded
            session = self._session
            scope = (await self.footprint_libraries()).get(nickname, "global")
            details: dict[str, FootprintDetail] = {}
            with _kicad_errors(f"Could not load footprint library {nickname}"):
                # GetLibraryItems(nickname) loads just this library on demand (2B, Q4).
                names = await session.call(partial(_footprint_names, nickname))
                for i in range(0, len(names), BATCH):
                    batch = names[i : i + BATCH]
                    for d in await session.call(partial(_fetch_footprints, nickname, batch)):
                        details[d.lib_id] = d
            entries = [(d.lib_id, d.description, d.keywords, scope) for d in details.values()]
            index = await asyncio.to_thread(_build_index, entries)
            loaded = _FootprintLibrary(details, index)
            self._fp_libs[nickname] = loaded
            return loaded

    async def footprint(self, lib_id: str) -> FootprintDetail | None:
        """Detail of one footprint (loads its library only); None if unknown. Raises
        `footprints_unavailable` while the footprint libraries are not loaded."""
        nickname, name = _split(lib_id.strip())
        if not nickname or not name:
            raise _invalid(
                f"Invalid footprint id {lib_id!r}",
                "expected <library nickname>:<footprint name>",
                "e.g. Resistor_SMD:R_0805_2012Metric",
            )
        if nickname not in await self.footprint_libraries():
            return None
        return (await self._footprint_library(nickname)).details.get(f"{nickname}:{name}")

    async def suggest_footprint_libraries(self, query: str, limit: int = 10) -> list[str]:
        """Nicknames whose words match the query words (prefix), best first."""
        scopes = await self.footprint_libraries()
        terms = [t for t in tokenize(query) if len(t) >= 2]
        scored: list[tuple[int, bool, str]] = []
        for nickname, scope in scopes.items():
            words = tokenize(nickname)
            score = sum(1 for t in terms if any(w.startswith(t) for w in words))
            if score:
                scored.append((score, scope == "project", nickname))
        scored.sort(key=lambda s: (-s[0], not s[1], s[2]))
        return [nickname for _, _, nickname in scored[:limit]]

    def loaded_footprint_libraries(self) -> list[str]:
        return sorted(self._fp_libs)

    async def search_footprints(
        self, query: str, library: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Search one library (loaded on demand) or, without `library`, the top 3
        libraries whose nickname matches the query words plus every loaded library.
        Never loads all footprint libraries (owner decision 1)."""
        rows, _ = await self.search_footprints_detailed(query, library, limit)
        return rows

    async def search_footprints_detailed(
        self, query: str, library: str | None = None, limit: int = 20
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """`search_footprints` plus the libraries skipped because they failed to load
        (nickname -> reason; only without `library` - with it the error is raised)."""
        scopes = await self.footprint_libraries()
        q = _query(query, "e.g. '0805' or 'SOT-23-5'")
        self._check_library(library, scopes, "Footprint")
        limit = _limit(limit)
        hits: list[tuple[float, str, FootprintDetail]] = []
        # Known-broken libraries are skipped (and reported) by unscoped searches; an
        # explicit `library` request retries them.
        failed: dict[str, str] = {}
        if library is not None:
            nicknames = [library]
        else:
            now = asyncio.get_running_loop().time()
            for nickname, (_, at) in list(self._fp_failed.items()):
                if now - at > FAILED_RETRY_S:
                    del self._fp_failed[nickname]
            candidates = await self.suggest_footprint_libraries(q, 50)
            broken = [n for n in candidates if n in self._fp_failed]
            usable = [n for n in candidates if n not in self._fp_failed]
            nicknames = list(dict.fromkeys([*usable[:FOOTPRINT_SEARCH_LIBRARIES], *self._fp_libs]))
            failed.update({n: self._fp_failed[n][0] for n in broken[:FOOTPRINT_SEARCH_LIBRARIES]})
        for nickname in nicknames:
            try:
                lib = await self._footprint_library(nickname)
            except StructuredToolError as exc:
                self._fp_failed[nickname] = (exc.why, asyncio.get_running_loop().time())
                if library is not None:
                    raise
                # One broken table row must not abort a search over several libraries.
                failed[nickname] = exc.why
                continue
            self._fp_failed.pop(nickname, None)
            for lib_id, score in await asyncio.to_thread(lib.index.search, q, limit):
                hits.append((score, lib_id, lib.details[lib_id]))
        hits.sort(key=lambda h: (-h[0], h[1]))
        return [d.to_dict() for _, _, d in hits[:limit]], failed

    # -- footprint tools (F1, FP-C1) -------------------------------------------------
    def invalidate_footprints(self, nickname: str | None = None) -> None:
        """Forget cached footprint data: the table scopes always, and the loaded library,
        its failure entry and its `footprint_geometry` entries for `nickname` (every
        library for None). Called after every footprint stage, a history restore and an
        external library change (spec §5)."""
        self._fp_scopes = None
        if nickname is None:
            self._fp_libs.clear()
            self._fp_failed.clear()
            self._fp_geometry.clear()
            return
        self._fp_libs.pop(nickname, None)
        self._fp_failed.pop(nickname, None)
        for lib_id in [k for k in self._fp_geometry if _split(k)[0] == nickname]:
            del self._fp_geometry[lib_id]

    async def local_library(self, *, for_write: bool = False) -> LocalLibraryInfo | None:
        """The repo's local footprint library (spec §5) and the state of its project row.

        None while the repo root is unknown (`repo_policy` transient); with `for_write`
        that raises `repo_root_unknown` instead, and the library path and the project
        table are checked with `local_library_dir_safe` (`library_conflict`)."""
        lib, transient = await self._derive_local()
        if lib is None:
            if for_write:
                raise _repo_root_unknown(transient, "nothing was written")
            return None
        if for_write:
            await asyncio.to_thread(local_library_dir_safe, lib)
        return await self._local_info(lib)

    async def _derive_local(self) -> tuple[LocalLibrary | None, str]:
        """The derived local library, or (None, reason) while `repo_policy` is
        transient. No API call besides the cached global nicknames."""
        session = self._session
        policy = await repo_policy(session.project, session.settings.projects_root)
        if policy.transient:
            return None, policy.transient
        nicknames = await self.global_footprint_nicknames()
        return derive_local_library(policy.project_dir, policy.root, nicknames), ""

    async def _local_info(self, lib: LocalLibrary) -> LocalLibraryInfo:
        with _kicad_errors("Could not read the project footprint table"):
            rows = await self._session.call(_project_footprint_rows)
        present, conflict = await asyncio.to_thread(_row_conflict, lib, rows)
        return LocalLibraryInfo(lib, present, conflict)

    def local_names_on_disk(self, lib: LocalLibrary) -> list[str]:
        """Stems of the regular `*.kicad_mod` files in the local library directory
        (sorted; symlinks and directories skipped; no file is opened)."""
        st = _lstat(lib.dir)
        if st is None or not stat.S_ISDIR(st.st_mode):
            return []
        names: list[str] = []
        with os.scandir(lib.dir) as entries:
            for entry in entries:
                if entry.name.endswith(".kicad_mod") and entry.is_file(follow_symlinks=False):
                    names.append(entry.name.removesuffix(".kicad_mod"))
        return sorted(names)

    async def footprint_geometry(
        self, lib_id: str, notes: list[dict[str, Any]] | None = None
    ) -> FootprintGeometry | None:
        """The geometry of one footprint, None if unknown (LRU of 64 by lib id).

        A footprint of the **local** library is read through the footprint document
        (complete `FootprintInstance`; its hash is authoritative). The footprint libraries
        are activated first, also on a board server (the document open loads every
        library anyway). Every other library is read with `GetItemsFromLibrary`, which
        lacks Reference/Value, the mounting attributes, the exclude/DNP flags and the
        overrides: `mounting` is "unspecified" and a `footprint_attributes_unavailable`
        note is added. Only a boardless server is activated for that read.

        A cached local footprint is re-validated (lstat of its `.kicad_mod`: inode, size,
        mtime) on every hit and re-read when the file changed or vanished. While the repo
        root is unknown (`repo_policy` transient) the local nickname cannot be derived:
        a nickname that is not in the global table (it may be the local library) raises
        `repo_root_unknown` instead of a silently incomplete read, and nothing read in
        that state is cached.

        Activation seconds go to `notes` as `footprint_libraries_loaded`. Must not be
        called while the caller holds the session's `_fp_lock` (activation and the
        document take it; the lock is not re-entrant)."""
        lib_id = lib_id.strip()
        nickname, name = _split(lib_id)
        if not nickname or not name:
            raise _invalid(
                f"Invalid footprint id {lib_id[:80]!r}",
                "expected <library nickname>:<footprint name>",
                "e.g. Resistor_SMD:R_0805_2012Metric",
            )
        cached = self._fp_geometry.get(lib_id)
        if cached is not None and cached.file is not None:
            current = await asyncio.to_thread(_file_validator, cached.file)
            if current != cached.validator:
                del self._fp_geometry[lib_id]  # changed or removed: read again
                cached = None
        if cached is not None:
            self._fp_geometry.move_to_end(lib_id)
            if cached.geometry.scope != "local":
                _note_incomplete(notes, lib_id)
            return cached.geometry
        lib, transient = await self._derive_local()
        if lib is None:
            if nickname not in await self.global_footprint_nicknames():
                raise _repo_root_unknown(transient, f"{lib_id} may be a local footprint")
            return await self._library_geometry(lib_id, nickname, name, notes)  # not cached
        entry: _CachedGeometry | None = None
        if nickname == lib.nickname and (await self._local_info(lib)).row_present:
            entry = await self._local_geometry(lib, lib_id, name, notes)
        else:
            geometry = await self._library_geometry(lib_id, nickname, name, notes)
            entry = None if geometry is None else _CachedGeometry(geometry)
        if entry is None:
            return None
        self._fp_geometry[lib_id] = entry
        while len(self._fp_geometry) > GEOMETRY_CACHE_SIZE:
            self._fp_geometry.popitem(last=False)
        return entry.geometry

    async def _activate(self, notes: list[dict[str, Any]] | None) -> None:
        seconds = await self._session.activate_footprints()
        if seconds is not None:
            self._fp_scopes = None  # the tables may answer differently once loaded
            if notes is not None:
                notes.append({"kind": "footprint_libraries_loaded", "seconds": round(seconds, 1)})

    async def _local_geometry(
        self, lib: LocalLibrary, lib_id: str, name: str, notes: list[dict[str, Any]] | None
    ) -> _CachedGeometry | None:
        from kipy.errors import ApiError

        from copperchat.kicad_tools.footprints.model import geometry_from_proto

        if not NAME_RE.match(name):
            return None  # never a file of the local library (and no path outside it)
        file = lib.dir / f"{name}.kicad_mod"
        validator = await asyncio.to_thread(_file_validator, file)  # before the read
        if validator is None:
            return None
        await self._activate(notes)
        session = self._session
        try:
            with _kicad_errors(f"Could not read footprint {lib_id}"):
                async with session.footprint_document(lib_id) as doc:
                    instance = await doc.footprint()
        except StructuredToolError as exc:
            cause = exc.__cause__
            if isinstance(cause, ApiError) and "not found" in str(cause).lower():
                return None
            raise
        except RuntimeError as exc:  # FootprintDocument: no parent / no FootprintInstance
            raise StructuredToolError(
                f"Could not read footprint {lib_id}",
                f"the footprint document is incomplete: {exc}",
                "Ask the user to open the footprint in KiCad and check it.",
                code="library_error",
            ) from exc
        geometry = geometry_from_proto(instance, instance.definition.items, lib_id, "local")
        return _CachedGeometry(geometry, file, validator)

    async def _library_geometry(
        self, lib_id: str, nickname: str, name: str, notes: list[dict[str, Any]] | None
    ) -> FootprintGeometry | None:
        from copperchat.kicad_tools.footprints.model import geometry_from_proto

        if not self.footprint_libraries_ready():
            await self._activate(notes)  # boardless server only
        scopes = await self.footprint_libraries()
        scope = scopes.get(nickname)
        if scope is None:
            return None
        with _kicad_errors(f"Could not read footprint {lib_id}"):
            proto = await self._session.call(partial(_fetch_footprint_proto, nickname, name))
        if proto is None:
            return None
        geometry = geometry_from_proto(proto, proto.items, lib_id, scope)
        _note_incomplete(notes, lib_id)
        return dataclasses.replace(geometry, mounting="unspecified")


def _note_incomplete(notes: list[dict[str, Any]] | None, lib_id: str) -> None:
    if notes is None:
        return
    notes.append(
        {
            "kind": "footprint_attributes_unavailable",
            "lib_id": lib_id,
            "message": "Read from the library listing, which has no mounting type "
            "(shown as 'unspecified'), no Reference/Value fields or text, no "
            "exclude-from-BOM/position or DNP flags and no footprint-level overrides.",
        }
    )
