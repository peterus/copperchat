from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import stat
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from kipy.errors import ApiError
from kipy.errors import ConnectionError as KipyConnectionError

from copperchat.config import Settings
from copperchat.db import utcnow_iso
from copperchat.errors import (
    CopperChatError,
    DocumentNotOpen,
    ReadOnlyProject,
    StructuredToolError,
)
from copperchat.kicad_session import footprint_doc
from copperchat.kicad_session.errors import (
    DocumentModified,
    KicadCallTimeout,
    KicadUnavailable,
    LibrariesLoading,
)
from copperchat.kicad_session.footprint_doc import FootprintDocument
from copperchat.kicad_session.launcher import ServerLauncher
from copperchat.projects import (
    LockStatus,
    Project,
    lock_file_for,
    lock_status,
    read_lock_token,
)

if TYPE_CHECKING:
    import kipy.board
    import kipy.schematic

DocKind = Literal["sch", "pcb"]
T = TypeVar("T")
ClientFactory = Callable[[str, int], Any]
SaveListener = Callable[[Project, DocKind], Awaitable[None]]
log = logging.getLogger(__name__)
_LIBRARY_POLL_S = 0.25
SOCKET_DIR = Path("/tmp/copperchat")
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    KipyConnectionError,
    ConnectionError,
    BrokenPipeError,
)


def socket_path_for(project: Project) -> Path:
    return SOCKET_DIR / f"{project.id}.sock"


def _ensure_private_dir(path: Path) -> None:
    """Create `path` 0700, or accept it only if it is a real directory we own.

    The socket lives in a fixed directory under /tmp; on a shared host another user
    could pre-create it (or a symlink) to intercept the API socket.
    """
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise KicadUnavailable(
            f"{path} is not a private directory owned by this user; remove it and retry"
        )
    os.chmod(path, 0o700)


def default_client_factory(socket_uri: str, timeout_ms: int) -> Any:
    from kipy import KiCad

    return KiCad(socket_path=socket_uri, client_name="copperchat", timeout_ms=timeout_ms)


def _document_type(doc: DocKind) -> int:
    from kipy.proto.common.types.base_types_pb2 import DocumentType

    return int(DocumentType.DOCTYPE_SCHEMATIC if doc == "sch" else DocumentType.DOCTYPE_PCB)


def _symbol_library_type() -> int:
    from kipy.proto.common.types import LibraryType

    return int(LibraryType.LT_SYMBOL)


def _symbol_libraries_loading(h: KicadHandle) -> bool:
    from kipy.proto.common.types import LibraryLoadStatus

    statuses = h.kicad.get_library_statuses(_symbol_library_type())
    return any(s.status == LibraryLoadStatus.LLS_LOADING for s in statuses)


@dataclass
class CommitScope:
    """One open KiCad commit (`KicadSession.commit`). The kipy `Commit` is kept: its id
    is what `drop_commit` needs (spike 2A, Q6)."""

    label: str
    commit: Any
    generation: int  # server start the commit belongs to; a restart discards it
    pushed: bool = False  # push_commit completed (also when the caller is cancelled after)
    doc: DocKind = "sch"  # the document the commit is open on (begin/push/drop)


async def _finish[R](coro: Coroutine[Any, Any, R]) -> tuple[R, bool]:
    """Run `coro` to completion even if the caller is cancelled meanwhile (a plain
    `shield` would return early - e.g. release the commit lock while a drop still runs,
    or lose the handle of a commit KiCad has just opened). Returns the result and
    whether a cancellation arrived; if `coro` raises after one, the cancellation wins
    (chained), so a Stop is never swallowed."""
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            if task.done():
                # The cancel can land after `coro` finished but before this frame
                # resumed: keep its outcome (e.g. the handle of a commit KiCad opened).
                if task.cancelled():
                    raise
                error = task.exception()
                if error is not None:
                    raise asyncio.CancelledError from error
                return task.result(), True
            cancelled = True
        except BaseException as exc:
            if cancelled:
                raise asyncio.CancelledError from exc
            raise


def _commit_document(h: KicadHandle, doc: DocKind) -> Any:
    return h.schematic() if doc == "sch" else h.board()


async def _uncancellable[R](coro: Coroutine[Any, Any, R]) -> R:
    """`_finish`, re-raising a cancellation that arrived meanwhile afterwards."""
    result, cancelled = await _finish(coro)
    if cancelled:
        raise asyncio.CancelledError
    return result


# How often `call` pings a server still busy with a cut-short zone refill (B-C3).
BUSY_POLL_S = 0.5
# A ping may block for the client's transport timeout (kipy `timeout_ms`, which the
# session sets from `kicad_api_timeout_s`); this is assumed when the client does not
# expose it (fakes). `call`'s busy wait keeps this margin before its own timeout.
DEFAULT_TRANSPORT_TIMEOUT_S = 30.0
IDLE_WAIT_MARGIN_S = 1.0


def _clock() -> float:  # module-level so tests can drive the busy wait with a fake clock
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def transport_timeout_s(kicad: Any) -> float:
    """How long one request (e.g. a Ping) can block: the kipy client's `timeout_ms`
    (`KiCad._client._timeout_ms`, the value the session's client factory passed), else
    DEFAULT_TRANSPORT_TIMEOUT_S."""
    ms = getattr(getattr(kicad, "_client", None), "_timeout_ms", None)
    if isinstance(ms, int | float) and not isinstance(ms, bool) and ms > 0:
        return ms / 1000
    return DEFAULT_TRANSPORT_TIMEOUT_S


class Slot[S](Protocol):
    """A get/set view onto one value (B-C3): lets a callable running in the worker
    thread read and write session state that outlives its `KicadHandle`."""

    def get(self) -> S: ...

    def set(self, value: S) -> None: ...


class LocalSlot[S]:
    """A slot holding its own value (a handle built without a session, e.g. in tests)."""

    def __init__(self, value: S) -> None:
        self._value = value

    def get(self) -> S:
        return self._value

    def set(self, value: S) -> None:
        self._value = value


class _AttrSlot[S]:
    """A slot onto `getattr(owner, name)` - the session's `pending_drc` / `refill_busy`.
    Only callables inside `session.call` use it; the session lock serialises them."""

    def __init__(self, owner: Any, name: str) -> None:
        self._owner = owner
        self._name = name

    def get(self) -> S:
        value: S = getattr(self._owner, self._name)
        return value

    def set(self, value: S) -> None:
        setattr(self._owner, self._name, value)


DrcSlot = Slot[tuple[int, str] | None]  # (server generation, DRC task id)
BusySlot = Slot[int | None]  # server generation still busy with a zone refill


# Busy probes (B-C3) wait at most this long for a Ping reply, so a bounded wait can
# afford several of them. Set on the kipy client's socket only for the probe.
PROBE_PING_TIMEOUT_S = 2.0


def _probe_client(kicad: Any) -> Any:
    """kipy's `KiCadClient` behind `kicad` if it has the private parts `_short_timeout`
    needs (kipy pin e0c9c8e: `KiCad._client`, `KiCadClient._connect()` /
    `._connected` / `._conn`, a pynng `Req0` with `recv_timeout` / `send_timeout` in
    ms), else None."""
    client = getattr(kicad, "_client", None)
    if client is None or not all(hasattr(client, a) for a in ("_connect", "_connected")):
        return None
    return client


def probe_timeout_s(kicad: Any) -> float:
    """How long one busy probe can block: PROBE_PING_TIMEOUT_S when the short timeout
    can be applied, else the client's normal transport timeout."""
    return PROBE_PING_TIMEOUT_S if _probe_client(kicad) is not None else transport_timeout_s(kicad)


@contextlib.contextmanager
def _short_timeout(kicad: Any, seconds: float) -> Iterator[None]:
    """Temporarily give the kipy client's socket a `seconds` send/receive timeout.

    Only inside `KicadSession.call` (its lock serialises every use of the client). A
    timed-out request leaves kipy's client disconnected (kipy reconnects with its normal
    timeout on the next request, but never closes that socket): the old socket is closed
    here; otherwise the previous timeouts are restored. Without the private attributes
    the probe runs with the normal timeout (logged)."""
    client = _probe_client(kicad)
    if client is None:
        log.info("busy probe without a short timeout: the kipy client lacks the private parts")
        yield
        return
    if not client._connected:
        client._connect()  # kipy would connect on the probe's send anyway
    conn = getattr(client, "_conn", None)
    if conn is None or not hasattr(conn, "recv_timeout"):
        log.info("busy probe without a short timeout: no pynng socket on the kipy client")
        yield
        return
    old = (conn.recv_timeout, conn.send_timeout)
    ms = max(1, round(seconds * 1000))
    conn.recv_timeout = conn.send_timeout = ms
    try:
        yield
    finally:
        if client._connected and client._conn is conn:
            conn.recv_timeout, conn.send_timeout = old
        else:
            with contextlib.suppress(Exception):
                conn.close()


def kicad_busy(kicad: Any) -> bool:
    """One busy probe: a `Ping` with a receive timeout of PROBE_PING_TIMEOUT_S. False =
    idle; True = busy (`AS_BUSY`, or no reply in time while KiCad blocks on a long
    operation). Any other `ApiError` is raised."""
    from kipy.proto.common import ApiStatusCode

    try:
        with _short_timeout(kicad, PROBE_PING_TIMEOUT_S):
            kicad.ping()
    except KipyConnectionError:
        return True
    except ApiError as exc:
        if exc.code == ApiStatusCode.AS_BUSY:
            return True
        raise
    return False


class _IdleWait:
    """How long `_after_idle` waited (`_clock` seconds) before `fn` started; None until
    then. `call` extends its deadline by it, so `fn` keeps its full budget."""

    spent: float | None = None


def _after_idle[R](
    fn: Callable[[KicadHandle], R],
    deadline: float,
    limit: float,
    waited: _IdleWait | None = None,
) -> Callable[[KicadHandle], R]:
    """`fn`, preceded by pinging until KiCad is idle again (a refill cut short by
    `RefillTimeout` may still be running, B-C3). Runs in the call's worker thread. A
    Ping is only sent while it fits before `deadline` even if it blocks for the whole
    transport timeout, so the wait never runs into the call's own timeout (no restart);
    still busy then → `refill_timeout` without running `fn`, the busy slot kept. The
    time waited is recorded in `waited` right before `fn` runs."""

    def run(h: KicadHandle) -> R:
        start = _clock()
        while True:
            if _clock() + probe_timeout_s(h.kicad) > deadline:
                raise StructuredToolError(
                    "KiCad is still filling zones",
                    "an earlier zone refill had not finished, and this call's "
                    f"{limit:g} s budget cannot wait for it",
                    "retry in a moment, or with more time (e.g. a refill or DRC step)",
                    code="refill_timeout",
                    details={"timeout_s": limit},
                )
            if not kicad_busy(h.kicad):
                break
            _sleep(BUSY_POLL_S)
        h.busy_slot.set(None)
        if waited is not None:
            waited.spent = max(0.0, _clock() - start)
        return fn(h)

    return run


class KicadHandle:
    """What callables passed to KicadSession.call receive. Runs in a worker thread.

    `generation` is the server start the call runs against; `drc_slot` / `busy_slot`
    view the session's `pending_drc` / `refill_busy` (B-C3) - a handle built without
    them (unit tests) gets private slots."""

    def __init__(
        self,
        kicad: Any,
        open_docs: frozenset[DocKind] = frozenset(),
        *,
        generation: int = 0,
        drc_slot: DrcSlot | None = None,
        busy_slot: BusySlot | None = None,
    ) -> None:
        self.kicad = kicad
        self._open_docs = open_docs
        self.generation = generation
        self.drc_slot: DrcSlot = drc_slot if drc_slot is not None else LocalSlot(None)
        self.busy_slot: BusySlot = busy_slot if busy_slot is not None else LocalSlot(None)

    def board(self) -> kipy.board.Board:
        if "pcb" not in self._open_docs:
            raise DocumentNotOpen("the project has no board open in the api-server")
        board: kipy.board.Board = self.kicad.get_board()
        return board

    def schematic(self) -> kipy.schematic.Schematic:
        # Spike 1C: get_schematic() clears the sheet path, so queries cover all sheets.
        if "sch" not in self._open_docs:
            raise DocumentNotOpen("the project has no schematic open in the api-server")
        schematic: kipy.schematic.Schematic = self.kicad.get_schematic()
        return schematic


class KicadSession:
    def __init__(
        self,
        project: Project,
        settings: Settings,
        launcher: ServerLauncher,
        client_factory: ClientFactory,
        read_only: bool,
        lock_files: tuple[Path, ...] = (),
        save_listeners: list[SaveListener] | None = None,
    ) -> None:
        self.project = project
        self.read_only = read_only
        self.lock_files = lock_files  # foreign lock files seen when the session opened
        self.restart_count = 0
        self.view_version = 0
        self.socket_path = socket_path_for(project)
        self.process: asyncio.subprocess.Process | None = None
        self._settings = settings
        self._launcher = launcher
        self._client_factory = client_factory
        self._kicad: Any = None
        self._open_docs: frozenset[DocKind] = frozenset()
        self._lock = asyncio.Lock()
        self._closed = False
        # Called after every successful (re)start; the manager records its lock token.
        self.on_started: Callable[[], None] | None = None
        # Token in `~STEM.kicad_pro.lck` after our server started (spike 1C, Q7).
        self.server_token: str | None = None
        # Editing (SP2, E-C1; per document since SP4 P1a). A pushed schematic commit
        # makes the export jobs (PNG, netlist) stale - they keep their own copy of the
        # schematic until close + reopen (spike 2A, Q3); a pushed commit on `doc` leaves
        # `doc` unsaved (in `modified_docs`) until `save(doc)`.
        self.jobs_stale = False
        self.modified_docs: set[DocKind] = set()
        self.last_saved_at: str | None = None
        self._save_listeners = save_listeners if save_listeners is not None else []
        self._commit_lock = asyncio.Lock()
        self._apply_lock = asyncio.Lock()  # applying(): one edit's stages, as a whole
        self._commit_owner: asyncio.Task[Any] | None = None
        self._active_scope: CommitScope | None = None
        self._generation = 0  # counts successful server starts
        # Board API state that outlives a call (B-C3); both die with the server, so
        # `_kill()` and every start clear them. Written only by callables inside `call`.
        self.pending_drc: tuple[int, str] | None = None  # timed-out DRC task (generation, id)
        self.refill_busy: int | None = None  # generation still filling zones (RefillTimeout)
        self._libraries_lock = asyncio.Lock()
        self._libraries_ready_generation: int | None = None
        self._libraries_kicked_generation: int | None = None
        # Footprints (F1, FP-C2): one footprint document per api-server at a time.
        self._fp_lock = asyncio.Lock()
        self._footprints_generation: int | None = None  # server start that loaded them
        # A footprint document could not be closed: the server holds a stale one. F4a
        # restarts the server at the turn end when this is set; a (re)start clears it.
        self.restart_pending = False

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def server_alive(self) -> bool:
        """A client to a running server exists (False after a kill until the next call
        lazily starts a fresh server from the last save)."""
        proc = self.process
        return self._kicad is not None and (proc is None or proc.returncode is None)

    @property
    def generation(self) -> int:
        """Counts server starts (also the lazy start after a kill on cancellation): an
        edit that sees it change knows its staged/pushed state is gone (E2 engine)."""
        return self._generation

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def footprints_active(self) -> bool:
        """The current api-server has loaded the footprint libraries (a footprint
        document was opened since it started); False again after every restart."""
        return self._footprints_generation is not None and (
            self._footprints_generation == self._generation
        )

    def _documents(self) -> list[tuple[DocKind, Path]]:
        docs: list[tuple[DocKind, Path]] = []
        if self.project.sch_file is not None:
            docs.append(("sch", self.project.sch_file))
        if self.project.pcb_file is not None:
            docs.append(("pcb", self.project.pcb_file))
        return docs

    # -- lifecycle -----------------------------------------------------------------
    async def start(self) -> None:
        """Launch the api-server, wait until it answers, then open sch + pcb.

        Spike 1C: the server only preloads the project; schematic and board
        handlers exist only after `open_document`. Any failure or cancellation
        kills the process again, so no api-server outlives a failed start.
        """
        try:
            await self._start()
        except BaseException:
            await asyncio.shield(self._kill())
            raise

    async def _start(self) -> None:
        _ensure_private_dir(SOCKET_DIR)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        self.process = await self._launcher.start(self.project, self.socket_path)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.kicad_start_timeout_s
        timeout_ms = int(self._settings.kicad_api_timeout_s * 1000)
        last_error: BaseException | None = None
        candidate: Any = None
        client: Any = None
        opened: set[DocKind] = set()
        try:
            while client is None:
                if self.process.returncode is not None:
                    code = self.process.returncode
                    self.process = None
                    raise KicadUnavailable(
                        f"kicad-cli api-server exited with code {code} during startup "
                        f"(log: {self._settings.data_dir / 'logs'})"
                    )
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise KicadUnavailable(
                        f"kicad-cli api-server not ready after "
                        f"{self._settings.kicad_start_timeout_s:.0f}s: {last_error}"
                    )
                try:
                    candidate = self._client_factory(f"ipc://{self.socket_path}", timeout_ms)
                    await asyncio.wait_for(
                        asyncio.to_thread(candidate.ping), timeout=min(2.0, remaining)
                    )
                    client, candidate = candidate, None
                except Exception as exc:  # not ready yet: socket missing, refused, slow
                    last_error = exc
                    _close_client(candidate)
                    candidate = None
                    await asyncio.sleep(0.2)
            for doc, path in self._documents():
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(client.open_document, str(path), _document_type(doc)),
                        timeout=max(0.1, deadline - loop.time()),
                    )
                except Exception as exc:
                    raise KicadUnavailable(
                        f"kicad-cli api-server could not open {path.name}: "
                        f"{exc or type(exc).__name__}"
                    ) from exc
                opened.add(doc)
        except BaseException:  # incl. cancellation: no client outlives a failed start
            _close_client(candidate)
            _close_client(client)
            raise
        self._kicad = client
        self._open_docs = frozenset(opened)
        self._generation += 1
        self.pending_drc = None
        self.refill_busy = None
        # A fresh server loaded the files on disk: nothing unsaved, jobs current, no
        # footprint document left open.
        self.jobs_stale = False
        self.modified_docs.clear()
        self.restart_pending = False
        await self._kick_off_symbol_libraries(client, deadline - loop.time())
        self.server_token = read_lock_token(lock_file_for(self.project.pro_file))
        if self.on_started is not None:
            self.on_started()

    async def _kick_off_symbol_libraries(self, client: Any, remaining: float) -> None:
        """Start the background load of every symbol library (owner decision 1: symbols
        eagerly, footprints never load-all'ed). A refused request only logs - the session
        still works; `ensure_symbol_libraries` asks again. A hang fails the start: the
        worker thread would keep using the client concurrently otherwise."""
        try:
            await asyncio.wait_for(
                asyncio.to_thread(client.load_all_libraries, _symbol_library_type()),
                timeout=max(1.0, remaining),
            )
            self._libraries_kicked_generation = self._generation
        except TimeoutError as exc:
            raise KicadUnavailable(
                "kicad-cli api-server did not answer the symbol library load request"
            ) from exc
        except Exception as exc:
            log.warning("could not start loading the symbol libraries: %s", exc)

    async def _retry_library_kick_off(self, generation: int) -> None:
        try:
            await self.call(lambda h: h.kicad.load_all_libraries(_symbol_library_type()))
        except KicadUnavailable:
            raise
        except Exception as exc:
            raise LibrariesLoading(f"KiCad refused to load the symbol libraries: {exc}") from exc
        if generation == self._generation:
            self._libraries_kicked_generation = generation

    async def ensure_symbol_libraries(self, timeout: float = 120.0) -> None:
        """Wait until the symbol libraries started in `start()` have loaded; returns at
        once on later calls for the same server start. Polls the load status in short
        calls, so other KiCad calls are not blocked meanwhile."""
        if self._libraries_ready_generation == self._generation:
            return
        async with self._libraries_lock:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                generation = self._generation
                if self._libraries_ready_generation == generation:
                    return
                if self._libraries_kicked_generation != generation:
                    await self._retry_library_kick_off(generation)
                    continue
                if not await self.call(_symbol_libraries_loading):
                    if generation == self._generation:  # no restart in between
                        self._libraries_ready_generation = generation
                        return
                    continue
                if loop.time() >= deadline:
                    raise LibrariesLoading(
                        f"the symbol libraries are still loading after {timeout:g}s"
                    )
                await asyncio.sleep(_LIBRARY_POLL_S)

    async def _kill(self) -> None:
        proc, self.process = self.process, None
        client, self._kicad = self._kicad, None
        self._open_docs = frozenset()
        self._footprints_generation = None  # the libraries die with the server
        self.pending_drc = None  # a DRC task or zone fill dies with the server too
        self.refill_busy = None
        _close_client(client)
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def _restart_locked(self) -> None:
        await self._kill()
        self.restart_count += 1
        await self.start()

    async def restart(self) -> None:
        async with self._lock:
            await self._restart_locked()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._kill()

    # -- calls ---------------------------------------------------------------------
    async def call(self, fn: Callable[[KicadHandle], T], *, timeout: float | None = None) -> T:
        """Run `fn` in a worker thread against the live api-server.

        Spike 1C: kipy's `timeout_ms` does not bound every failure mode, so the
        session keeps its own deadline and watches the process; a hang, a dead
        process or a lost connection restarts the server and raises - the call is
        never retried.
        """
        limit = timeout if timeout is not None else self._settings.kicad_api_timeout_s
        async with self._lock:
            if self._closed:
                raise KicadUnavailable("the KiCad session for this project is closed")
            if self._kicad is None:  # an earlier restart failed: try once more now
                await self._kill()
                await self.start()
            scope = self._active_scope
            if (
                scope is not None
                and scope.generation != self._generation
                and self._commit_owner is asyncio.current_task()
            ):
                # The server holding this commit restarted: its staged writes are gone,
                # and a write now would land outside any commit (Review Focus 2).
                raise KicadUnavailable(
                    "the KiCad api-server restarted during an open commit; its changes "
                    "were discarded"
                )
            proc = self.process
            if proc is not None and proc.returncode is not None:
                await self._restart_after_failure(f"exited with code {proc.returncode}")
                raise KicadUnavailable(
                    "the KiCad api-server had stopped; it was restarted with the last saved state"
                )
            handle = KicadHandle(
                self._kicad,
                self._open_docs,
                generation=self._generation,
                drc_slot=_AttrSlot(self, "pending_drc"),
                busy_slot=_AttrSlot(self, "refill_busy"),
            )
            target = fn
            waited: _IdleWait | None = None
            if self.refill_busy is not None and self.refill_busy == self._generation:
                # A refill cut short by RefillTimeout may still run: ping until idle
                # first, bounded inside this call's own timeout (B-C3), so the callable
                # never meets AS_BUSY. The callable then still gets its full `limit`:
                # the deadline is extended by the time the idle wait took.
                waited = _IdleWait()
                target = _after_idle(fn, _clock() + limit - IDLE_WAIT_MARGIN_S, limit, waited)
            work = asyncio.ensure_future(asyncio.to_thread(target, handle))
            died = asyncio.ensure_future(proc.wait()) if proc is not None else None
            try:
                waiting = {work} if died is None else {work, died}
                done, _ = await asyncio.wait(
                    waiting, timeout=limit, return_when=asyncio.FIRST_COMPLETED
                )
                if not done and waited is not None and waited.spent:
                    await asyncio.wait(
                        waiting, timeout=waited.spent, return_when=asyncio.FIRST_COMPLETED
                    )
            except asyncio.CancelledError:
                # The caller gave up (e.g. an interrupted turn). The worker thread
                # cannot be stopped and must not share the client with later calls,
                # so stop the server; the next call starts a fresh one.
                if not work.done():
                    work.add_done_callback(_consume_result)
                    await asyncio.shield(self._kill())
                raise
            finally:
                if died is not None:
                    died.cancel()
            if work.done():
                return await self._result(work)
            # The worker thread cannot be interrupted; leave it to finish on its own.
            work.add_done_callback(_consume_result)
            if died is not None and died.done() and not died.cancelled():
                await self._restart_after_failure("process died")
                raise KicadUnavailable(
                    "the KiCad api-server died during the call; it was restarted "
                    "with the last saved state"
                )
            await self._restart_after_failure("timed out")
            raise KicadCallTimeout(
                f"KiCad API call timed out after {limit:g}s; the api-server was "
                "restarted with the last saved state"
            )

    async def _result(self, work: asyncio.Future[T]) -> T:
        try:
            return work.result()
        except _CONNECTION_ERRORS as exc:
            await self._restart_after_failure(str(exc))
            raise KicadUnavailable(
                f"lost connection to the KiCad api-server ({exc}); it was restarted "
                "with the last saved state"
            ) from exc
        except Exception:
            if self.process is not None and self.process.returncode is not None:
                await self._restart_after_failure("process died")
                raise KicadUnavailable(
                    "the KiCad api-server died during the call; it was restarted"
                ) from None
            raise

    async def _restart_after_failure(self, reason: str) -> None:
        try:
            await self._restart_locked()
        except KicadUnavailable as exc:
            raise KicadUnavailable(
                f"KiCad api-server failed ({reason}) and could not be restarted: {exc.message}"
            ) from exc

    # -- editing -------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def applying(self) -> AsyncIterator[None]:
        """Held while one edit's stages are pushed, verified and compensated (the edit
        engine): `refresh_jobs` waits for it, so it never saves a half-applied edit into
        the last save a later failure falls back to (#6); `save` and `save_copy`
        likewise. Not re-entrant: never save inside it."""
        async with self._apply_lock:
            yield

    @property
    def modified(self) -> bool:
        """The schematic has pushed commits not saved yet (read-only; see `modified_docs`)."""
        return "sch" in self.modified_docs

    @contextlib.asynccontextmanager
    async def commit(
        self, label: str, *, doc: DocKind = "sch", dry_run: bool = False
    ) -> AsyncIterator[CommitScope]:
        """Group the writes to `doc` (schematic or board) made inside the scope into one
        KiCad commit.

        Normal exit pushes it (`dry_run`: drops it); any exception, cancellation
        included, drops it, shielded - a commit left open on the server silently
        captures every later write of the same client (spike 2A, Q6). A failing drop
        restarts the server. `save`, `save_copy` and `refresh_jobs` wait for the scope.
        The lock is not re-entrant: never call them from inside a scope. A pushed commit
        adds `doc` to `modified_docs`; only a schematic one makes the jobs stale. A board
        commit without a board raises `DocumentNotOpen` before waiting for the lock.
        """
        if self.read_only:
            raise ReadOnlyProject(
                "project is open in KiCad (lock file present) - close it there first"
            )
        if doc == "pcb" and (
            self.project.pcb_file is None
            or (self._kicad is not None and "pcb" not in self._open_docs)
        ):
            raise DocumentNotOpen("the project has no board open in the api-server")
        self._check_not_in_commit()
        async with self._commit_lock:
            self._commit_owner = asyncio.current_task()
            try:
                # begin/push run to completion even on a cancel: a cancel landing right
                # after KiCad opened the commit would otherwise lose its handle (the
                # commit stays open), one after the push would drop an applied commit.
                handle, cancelled = await _finish(
                    self.call(lambda h: _commit_document(h, doc).begin_commit())
                )
                # Read after the call: a lazy (re)start inside call() bumps it first.
                scope = CommitScope(label, handle, self._generation, doc=doc)
                self._active_scope = scope
                if cancelled:
                    await _uncancellable(self._drop(scope, asyncio.CancelledError()))
                    raise asyncio.CancelledError
                try:
                    yield scope
                except BaseException as exc:
                    await _uncancellable(self._drop(scope, exc))
                    raise
                try:
                    if dry_run:
                        _, cancelled = await _finish(
                            self.call(lambda h: _commit_document(h, doc).drop_commit(handle))
                        )
                    else:
                        _, cancelled = await _finish(
                            self.call(lambda h: _commit_document(h, doc).push_commit(handle, label))
                        )
                except BaseException as exc:
                    await _uncancellable(self._drop(scope, exc))
                    raise
                if not dry_run:
                    scope.pushed = True
                    self.modified_docs.add(doc)
                    if doc == "sch":
                        self.jobs_stale = True
                if cancelled:
                    raise asyncio.CancelledError
            finally:
                self._commit_owner = None
                self._active_scope = None

    async def _drop(self, scope: CommitScope, cause: BaseException) -> None:
        if self._kicad is None or scope.generation != self._generation:
            return  # the server that held the commit is gone, and its staged items with it
        try:
            await self.call(lambda h: _commit_document(h, scope.doc).drop_commit(scope.commit))
        except Exception as exc:
            if self._kicad is None or scope.generation != self._generation:
                return  # the failed drop already restarted the server
            with contextlib.suppress(KicadUnavailable):
                await self.restart()
            if isinstance(cause, asyncio.CancelledError):
                log.warning("dropping a cancelled commit failed (%s); server restarted", exc)
                return  # the caller re-raises the cancellation
            raise KicadUnavailable(
                "could not drop an open KiCad commit; the server was restarted"
            ) from (cause if isinstance(cause, Exception) else exc)

    def _check_not_in_commit(self) -> None:
        if self._commit_owner is not None and self._commit_owner is asyncio.current_task():
            raise RuntimeError(
                "save/save_copy/refresh_jobs/commit called inside an open commit or save listener"
            )

    async def save(self, doc: DocKind) -> None:
        """Save `doc`, then await every save listener (history records the hashes)."""
        if self.read_only:
            raise ReadOnlyProject(
                "project is open in KiCad (lock file present) - close it there first"
            )
        self._check_not_in_commit()
        async with self._apply_lock, self._commit_lock:  # never a half-applied edit (#6)
            await self._save_locked(doc)

    async def _save_locked(self, doc: DocKind) -> None:
        def _save(h: KicadHandle) -> None:
            (h.schematic() if doc == "sch" else h.board()).save()  # type: ignore[no-untyped-call]

        await self.call(_save)
        self.modified_docs.discard(doc)
        self.last_saved_at = utcnow_iso()
        # Listeners run under the commit lock: mark the task, so a listener calling
        # save/save_copy/refresh_jobs/commit fails instead of deadlocking.
        owner, self._commit_owner = self._commit_owner, asyncio.current_task()
        try:
            for listener in list(self._save_listeners):
                try:
                    await listener(self.project, doc)
                except Exception:
                    log.exception("save listener failed for %s", self.project.rel_path)
        finally:
            self._commit_owner = owner

    async def refresh_jobs(self) -> None:
        """Make the api-server's export jobs (PNG, netlist) see the edits: they keep
        their own copy of the schematic, refreshed only by save + close_document +
        open_document (spike 2A, Q3). Waits for an edit being applied (`applying`)."""
        self._check_not_in_commit()
        async with self._apply_lock, self._commit_lock:
            if not self.jobs_stale or self.read_only or "sch" not in self._open_docs:
                return
            # Reopening loads the files: unsaved edits are saved first. An unmodified
            # document is never saved here - that could overwrite an external change.
            if await self.call(lambda h: h.schematic().is_document_modified()):
                await self._save_locked("sch")
            sch_file = str(self.project.sch_file)
            doc_type = _document_type("sch")

            def _reopen(h: KicadHandle) -> None:
                for spec in h.kicad.get_open_documents(doc_type):
                    h.kicad.close_document(spec)
                h.kicad.open_document(sch_file, doc_type)

            try:
                await self.call(_reopen)
            except KicadUnavailable:
                raise  # call() already restarted the server
            except Exception as exc:
                # The schematic may be closed now; a restart opens it again from disk.
                await self.restart()
                raise KicadUnavailable(
                    f"reopening the schematic failed ({exc}); the api-server was restarted"
                ) from exc
            self.jobs_stale = False

    async def save_copy(self, doc: DocKind, dest: Path, *, save_modified: bool = True) -> Path:
        """SaveCopyOfDocument to `dest`.

        Spike 1C (Q4): for a schematic the API writes only the root sheet, so the
        sub-sheet files named by the hierarchy are byte-copied from the project
        directory next to it (same relative paths). Those files are only current
        after a save, so a modified schematic is saved first (spike 2A, Q8: else
        stale sub-sheets, and a crash on a new sheet). `save_modified=False` (the live
        view: the document may hold a failed partial edit that must never be saved)
        raises `DocumentModified` instead, without saving or copying anything.

        For a board, KiCad copies the project's `.kicad_dru` next to the copy and
        refuses ("board could not be saved to ...") when `<dest>.kicad_dru` already
        exists - its own previous copy - so that stale copy is removed first. A rules
        file in the project directory itself is never touched.
        """
        self._check_not_in_commit()
        dest.parent.mkdir(parents=True, exist_ok=True)
        stale_rules = dest.parent.resolve() != self.project.dir.resolve()

        def _copy(h: KicadHandle) -> list[str]:
            if doc == "pcb":
                if stale_rules:  # under the session lock, right before the save
                    dest.with_suffix(".kicad_dru").unlink(missing_ok=True)
                h.board().save_as(str(dest), overwrite=True, include_project=False)
                return []
            schematic = h.schematic()
            schematic.save_as(str(dest), overwrite=True, include_project=False)
            return [str(sheet.filename) for sheet in schematic.get_hierarchy()]

        async with self._apply_lock, self._commit_lock:  # never a half-applied edit (#6)
            if (
                doc == "sch"
                and not self.read_only
                and await self.call(lambda h: h.schematic().is_document_modified())
            ):
                if not save_modified:
                    raise DocumentModified("the schematic has unsaved edits")
                await self._save_locked("sch")
            sheet_files = await self.call(_copy)
        if sheet_files:
            await asyncio.to_thread(self._copy_sub_sheets, sheet_files, dest.parent)
        return dest

    # -- footprints (F1, FP-C2) -----------------------------------------------------
    def _check_footprint_tools(self) -> None:
        if not self._settings.footprint_tools:
            raise StructuredToolError(
                "footprint tools are unavailable",
                "COPPERCHAT_FOOTPRINT_TOOLS is off on this CopperChat instance",
                "work without footprint tools, or ask the administrator to enable them",
                code="footprint_tools_disabled",
            )

    def _server_rss(self) -> str:
        """The api-server's resident memory (VmRSS of /proc/<pid>/status), for logs."""
        proc = self.process
        if proc is None:
            return "unknown"
        try:
            with open(f"/proc/{proc.pid}/status", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return " ".join(line.split()[1:])
        except OSError:
            pass
        return "unknown"

    async def activate_footprints(self) -> float | None:
        """Load the footprint libraries by opening and closing the seed footprint (the
        first footprint document of a server loads every library in the tables, spike
        3A Q5). Returns the seconds spent, or None when the current server has them.

        Takes `_fp_lock` (not re-entrant): never call it while holding `_fp_lock`.
        Holding `_apply_lock` (inside `applying()`, as the footprint engine does) or the
        caller's own open schematic commit (`_commit_lock` → `_fp_lock`) is safe only
        while nothing holding `_fp_lock` waits on `_apply_lock` or `_commit_lock` (plan
        "As built")."""
        self._check_footprint_tools()
        if self.footprints_active:
            return None
        async with self._fp_lock:
            if self.footprints_active:
                return None
            lib_id = footprint_doc.SEED_LIB_ID
            rss_before = self._server_rss()
            started = time.monotonic()
            try:
                async with self._footprint_document_locked(lib_id):
                    pass
            except ApiError as exc:
                if "not found" in str(exc).lower():  # "Footprint not found: <lib_id>"
                    raise StructuredToolError(
                        f"could not load the footprint libraries (seed {lib_id})",
                        f"KiCad could not find the seed footprint {lib_id}: {exc}",
                        "check that the KiCad stock footprint libraries are installed",
                        code="seed_missing",
                        details={"lib_id": lib_id},
                    ) from exc
                raise StructuredToolError(
                    "could not load the footprint libraries",
                    f"KiCad refused to open the seed footprint {lib_id}: {exc}",
                    "retry later; if it keeps failing, check the KiCad api-server log",
                    code="library_error",
                    details={"lib_id": lib_id},
                ) from exc
            seconds = time.monotonic() - started
            log.info(
                "footprint libraries loaded in %.2f s (api-server RSS %s -> %s)",
                seconds,
                rss_before,
                self._server_rss(),
            )
            return seconds

    @contextlib.asynccontextmanager
    async def footprint_document(self, lib_id: str) -> AsyncIterator[FootprintDocument]:
        """Open `lib_id` ("Lib:Name") as the server's single footprint document, under
        the footprint lock; it is closed on exit, also on an exception or cancellation.
        A failed close sets `restart_pending`. Never save the project inside it."""
        self._check_footprint_tools()
        async with self._fp_lock, self._footprint_document_locked(lib_id) as doc:
            yield doc

    @contextlib.asynccontextmanager
    async def _footprint_document_locked(self, lib_id: str) -> AsyncIterator[FootprintDocument]:
        # The open runs to completion even on a cancel, so its handle is never lost (a
        # footprint document left open breaks the next open/close, spike 3A Q4).
        # Only the open that loads the footprint libraries needs the long timeout; a
        # later hung open must not hold off a restart or a Stop for minutes.
        timeout = None if self.footprints_active else footprint_doc.OPEN_TIMEOUT_S
        spec, cancelled = await _finish(
            self.call(lambda h: footprint_doc.open_footprint(h, lib_id), timeout=timeout)
        )
        generation = self._generation  # read after the call: a lazy start bumps it
        self._footprints_generation = generation
        doc = FootprintDocument(self, spec, lib_id)
        try:
            if cancelled:
                raise asyncio.CancelledError
            yield doc
        finally:
            await self._close_footprint(doc, generation)

    async def _close_footprint(self, doc: FootprintDocument, generation: int) -> None:
        """Close `doc`, shielded; a failure only logs and sets `restart_pending` (the
        caller's own outcome wins). Never closes the schematic: closing the last open
        document can unload the project (spike 3A Q4)."""

        def gone() -> bool:
            return self._kicad is None or generation != self._generation

        if gone():
            return  # the server that held the document is gone, and the document with it

        async def close() -> None:
            if doc.open_commit:
                with contextlib.suppress(Exception):
                    await doc.drop()
            await self.call(lambda h: footprint_doc.close_footprint(h, doc.document))

        try:
            _, cancelled = await _finish(close())
        except asyncio.CancelledError as exc:
            if exc.__cause__ is not None:
                self._footprint_close_failed(doc, exc.__cause__, gone())
            raise
        except Exception as exc:
            self._footprint_close_failed(doc, exc, gone())
            return
        if cancelled:
            raise asyncio.CancelledError

    def _footprint_close_failed(
        self, doc: FootprintDocument, exc: BaseException, server_gone: bool
    ) -> None:
        if server_gone:  # the failed call restarted the server: nothing is left open
            log.warning("closing footprint %s failed (%s); server restarted", doc.lib_id, exc)
            return
        self.restart_pending = True
        log.warning(
            "closing footprint %s failed (%s); the api-server restarts at the turn end",
            doc.lib_id,
            exc,
        )

    def _copy_sub_sheets(self, sheet_files: list[str], dest_dir: Path) -> None:
        root_name = self.project.sch_file.name if self.project.sch_file else None
        project_dir = self.project.dir.resolve()
        dest_root = dest_dir.resolve()
        for name in dict.fromkeys(sheet_files):
            if not name or name == root_name:
                continue
            source = (project_dir / name).resolve()
            target = (dest_root / name).resolve()
            if not source.is_relative_to(project_dir) or not target.is_relative_to(dest_root):
                raise CopperChatError(f"sub-sheet {name!r} lies outside the project directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def _close_client(client: Any) -> None:
    """Close a kipy client's socket; an abandoned worker blocked on it is released."""
    close = getattr(client, "close", None)
    if close is not None:
        with contextlib.suppress(Exception):
            close()


def _consume_result(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()  # mark as retrieved: an abandoned call's error is moot


class KicadSessionManager:
    """Owns the one open KiCad session (one user per instance, one project at a time)."""

    def __init__(
        self,
        settings: Settings,
        launcher: ServerLauncher | None = None,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if launcher is None:
            from copperchat.kicad_session.launcher import KicadCliLauncher, kicad_config_home

            launcher = KicadCliLauncher(
                settings.kicad_cli, settings.data_dir / "logs", kicad_config_home(settings)
            )
        self._settings = settings
        self._launcher = launcher
        self._client_factory = client_factory or default_client_factory
        self._current: KicadSession | None = None
        self._lock = asyncio.Lock()
        self._tokens_dir = settings.data_dir / "kicad-locks"
        self._save_listeners: list[SaveListener] = []

    def add_save_listener(self, fn: SaveListener) -> None:
        """`fn(project, doc)` runs after every successful `KicadSession.save` of any
        session, under the session's commit lock: it must not save, copy or commit."""
        self._save_listeners.append(fn)

    # -- own lock tokens -----------------------------------------------------------
    # The api-server writes the same `~STEM.kicad_pro.lck` the KiCad GUI uses and
    # leaves it behind on kill -9 (spike 1C, Q7). Remembering the token our own
    # server wrote lets `lock_status` tell our (possibly stale) lock from KiCad's.
    def _own_tokens(self, project: Project) -> frozenset[str]:
        try:
            token = (self._tokens_dir / project.id).read_text(encoding="utf-8").strip()
        except OSError:
            return frozenset()
        return frozenset({token}) if token else frozenset()

    def _remember_own_token(self, project: Project) -> None:
        token = read_lock_token(lock_file_for(project.pro_file))
        if token is None or token in self._own_tokens(project):
            return
        self._tokens_dir.mkdir(parents=True, exist_ok=True)
        (self._tokens_dir / project.id).write_text(token, encoding="utf-8")

    def lock_status(self, project: Project) -> LockStatus:
        """`projects.lock_status` minus locks written by CopperChat's own api-server."""
        ignore = set(self._own_tokens(project))
        current = self._current
        if (
            current is not None
            and current.project.id == project.id
            and current.server_token is not None
            and lock_file_for(project.pro_file).resolve() not in current.lock_files
        ):
            # The running (possibly read-only) session's own lock. Not trusted when
            # a foreign project lock existed at open: the server may have replaced it.
            ignore.add(current.server_token)
        return lock_status(project, ignore_tokens=ignore)

    # -- sessions ------------------------------------------------------------------
    async def open(self, project: Project) -> KicadSession:
        async with self._lock:
            # Evaluated before our own server starts: it creates the same lock file.
            locks = self.lock_status(project)
            current = self._current
            if (
                current is not None
                and not current.closed
                and current.project.id == project.id
                and current.read_only == locks.locked
            ):
                return current
            if current is not None:  # other project, or KiCad GUI opened/closed it
                await current.close()
                self._current = None
            session = KicadSession(
                project,
                self._settings,
                self._launcher,
                self._client_factory,
                read_only=locks.locked,
                lock_files=locks.lock_files,
                save_listeners=self._save_listeners,
            )
            if not session.read_only:
                # No foreign lock existed, so the lock our server writes is ours. A
                # read-only session never records one: the lock may be KiCad's.
                session.on_started = lambda: self._remember_own_token(project)
            await session.start()
            self._current = session
            return session

    async def reopen(self, project: Project) -> KicadSession:
        """Close the project's session (if open) and start a fresh one, so KiCad loads
        the files on disk again (external change, snapshot restore). The new session
        keeps the old view version: clients take the maximum per document (X2), and the
        time of the last CopperChat save (recovery notices name it, spec 5.4)."""
        version = 0
        last_saved_at: str | None = None
        async with self._lock:
            current = self._current
            if current is not None and current.project.id == project.id:
                version = current.view_version
                last_saved_at = current.last_saved_at
                await current.close()
                self._current = None
        session = await self.open(project)
        session.view_version = max(session.view_version, version)
        session.last_saved_at = session.last_saved_at or last_saved_at
        return session

    def current(self) -> KicadSession | None:
        return self._current

    async def close_all(self) -> None:
        async with self._lock:
            if self._current is not None:
                await self._current.close()
                self._current = None
