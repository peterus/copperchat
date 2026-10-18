"""Library service: symbol index (eager), symbol detail incl. derived-symbol pins, and
footprint libraries (lazy, one library per request) - E-C9, spike 2B, owner decision 1.

Data comes only from KiCad's library API: listing = `GetLibraryItems` (names only),
details = `GetItemsFromLibrary` in batches of 500 ids, pins of derived (`extends`)
symbols = the flattened definition of a placement inside a dropped commit.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadSession, KicadUnavailable
from copperchat.library.index import Index, Scope, SymbolRow, tokenize
from copperchat.library.packages import FootprintDetail, footprint_from_proto
from copperchat.library.symbols import SymbolDetail, detail_from_proto, has_drawing, row_from_proto

log = logging.getLogger(__name__)

BATCH = 500  # ids per GetItemsFromLibrary request (spike 2B: 6.5 s for all symbols)
MAX_LIMIT = 50
DETAIL_CACHE_SIZE = 512
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


def _invalid(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


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


def _build_index(rows: Iterable[tuple[str, str, str, Scope]]) -> Index:
    index = Index()
    for lib_id, description, keywords, scope in rows:
        index.add(lib_id, description, keywords, scope)
    index.finish()
    return index


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

    @property
    def _session(self) -> KicadSession:
        session = self._session_ref()
        if session is None:
            raise KicadUnavailable("the KiCad session of this library service is gone")
        return session

    # -- warm-up -------------------------------------------------------------------
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
        q = query.strip()
        if not q:
            raise _invalid("Empty query", "query is required", "e.g. 'LDO 3.3V' or 'LM358'")
        self._check_library(library, self._libraries, "Symbol")
        hits = index.search(q, max(1, min(int(limit), MAX_LIMIT)), library)
        return [self._rows[lib_id].to_dict() for lib_id, _ in hits]

    async def get_symbol(self, lib_id: str) -> SymbolDetail:
        """Detail of one library symbol; pins of derived symbols come from a probe
        placement in a dropped commit (`session.commit(dry_run=True)`). Must not be
        called from inside an open commit of the same task."""
        index = self._ready_index()
        lib_id = lib_id.strip()
        cached = self._details.get(lib_id)
        if cached is not None:
            self._details.move_to_end(lib_id)
            return cached
        row = self._rows.get(lib_id)
        if row is None:
            close = self._closest(index, lib_id)
            raise StructuredToolError(
                f"Symbol {lib_id!r} is not in the libraries",
                "unknown lib_id (format <library nickname>:<symbol name>, case-sensitive)",
                f"Closest matches: {close}" if close else "Use lib_search_symbols.",
                code="not_found",
            )
        session = self._session
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
        from kipy.errors import ApiError

        await session.ensure_symbol_libraries()  # placing needs loaded libraries
        try:
            async with session.commit(f"probe {lib_id}", dry_run=True):
                return await session.call(partial(_place_probe, lib_id))
        except ApiError as exc:
            raise StructuredToolError(
                f"Could not read the pins of {lib_id}",
                f"KiCad refused the probe placement: {exc}",
                "Use another symbol or ask the user.",
                code="not_found",
            ) from exc

    # -- footprints ----------------------------------------------------------------
    def footprints_available(self) -> bool:
        """KiCad serves footprint libraries only with a board open (spike 2B, Q4);
        without a `.kicad_pcb` they are never load-all'ed (owner decision 3)."""
        return self._session.project.pcb_file is not None

    def _require_footprints(self) -> None:
        if not self.footprints_available():
            raise StructuredToolError(
                "Footprint libraries are not available",
                "this project has no board (.kicad_pcb), and KiCad serves footprint "
                "libraries only with a board open",
                "Pick the footprint by its usual KiCad name (e.g. "
                "Resistor_SMD:R_0805_2012Metric) and tell the user it is unverified.",
                code="footprints_unavailable",
            )

    async def footprint_libraries(self) -> dict[str, Scope]:
        """Footprint library nicknames from the tables (no library is loaded)."""
        self._require_footprints()
        scopes = self._fp_scopes
        if scopes is None:
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
            # GetLibraryItems(nickname) loads just this library on demand (spike 2B, Q4).
            names = await session.call(partial(_footprint_names, nickname))
            details: dict[str, FootprintDetail] = {}
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
        `footprints_unavailable` without a board."""
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
        scopes = await self.footprint_libraries()
        q = query.strip()
        if not q:
            raise _invalid("Empty query", "query is required", "e.g. '0805' or 'SOT-23-5'")
        self._check_library(library, scopes, "Footprint")
        limit = max(1, min(int(limit), MAX_LIMIT))
        if library is not None:
            nicknames = [library]
        else:
            suggested = await self.suggest_footprint_libraries(q, FOOTPRINT_SEARCH_LIBRARIES)
            nicknames = list(dict.fromkeys([*suggested, *self._fp_libs]))
        hits: list[tuple[float, str, FootprintDetail]] = []
        for nickname in nicknames:
            lib = await self._footprint_library(nickname)
            for lib_id, score in lib.index.search(q, limit):
                hits.append((score, lib_id, lib.details[lib_id]))
        hits.sort(key=lambda h: (-h[0], h[1]))
        return [d.to_dict() for _, _, d in hits[:limit]]
