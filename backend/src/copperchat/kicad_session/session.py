from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import stat
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from kipy.errors import ConnectionError as KipyConnectionError

from copperchat.config import Settings
from copperchat.db import utcnow_iso
from copperchat.errors import CopperChatError, DocumentNotOpen, ReadOnlyProject
from copperchat.kicad_session.errors import KicadCallTimeout, KicadUnavailable, LibrariesLoading
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


async def _uncancellable[R](coro: Coroutine[Any, Any, R]) -> R:
    """Run `coro` to completion even if the caller is cancelled meanwhile; the
    cancellation is re-raised afterwards (a plain `shield` would return early and
    release the commit lock while the drop still runs)."""
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


class KicadHandle:
    """What callables passed to KicadSession.call receive. Runs in a worker thread."""

    def __init__(self, kicad: Any, open_docs: frozenset[DocKind] = frozenset()) -> None:
        self.kicad = kicad
        self._open_docs = open_docs

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
        # Editing (SP2, E-C1). A pushed commit makes the export jobs (PNG, netlist)
        # stale - they keep their own copy of the schematic until close + reopen
        # (spike 2A, Q3) - and the documents unsaved.
        self.jobs_stale = False
        self.modified = False
        self.last_saved_at: str | None = None
        self._save_listeners = save_listeners if save_listeners is not None else []
        self._commit_lock = asyncio.Lock()
        self._commit_owner: asyncio.Task[Any] | None = None
        self._active_scope: CommitScope | None = None
        self._generation = 0  # counts successful server starts
        self._libraries_lock = asyncio.Lock()
        self._libraries_ready_generation: int | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def closed(self) -> bool:
        return self._closed

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
        # A fresh server loaded the files on disk: nothing unsaved, jobs current.
        self.jobs_stale = False
        self.modified = False
        await self._kick_off_symbol_libraries(client, deadline - loop.time())
        self.server_token = read_lock_token(lock_file_for(self.project.pro_file))
        if self.on_started is not None:
            self.on_started()

    async def _kick_off_symbol_libraries(self, client: Any, remaining: float) -> None:
        """Start the background load of every symbol library (owner decision 1: symbols
        eagerly, footprints never load-all'ed). A refused request only logs: placing parts
        then fails with `libraries_loading` instead of the whole session. A hang fails the
        start - the worker thread would keep using the client concurrently otherwise."""
        try:
            await asyncio.wait_for(
                asyncio.to_thread(client.load_all_libraries, _symbol_library_type()),
                timeout=max(1.0, remaining),
            )
        except TimeoutError as exc:
            raise KicadUnavailable(
                "kicad-cli api-server did not answer the symbol library load request"
            ) from exc
        except Exception as exc:
            log.warning("could not start loading the symbol libraries: %s", exc)

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
            work = asyncio.ensure_future(
                asyncio.to_thread(fn, KicadHandle(self._kicad, self._open_docs))
            )
            died = asyncio.ensure_future(proc.wait()) if proc is not None else None
            try:
                waiting = {work} if died is None else {work, died}
                await asyncio.wait(waiting, timeout=limit, return_when=asyncio.FIRST_COMPLETED)
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
    async def commit(self, label: str, *, dry_run: bool = False) -> AsyncIterator[CommitScope]:
        """Group the schematic writes made inside the scope into one KiCad commit.

        Normal exit pushes it (`dry_run`: drops it); any exception, cancellation
        included, drops it, shielded - a commit left open on the server silently
        captures every later write of the same client (spike 2A, Q6). A failing drop
        restarts the server. `save`, `save_copy` and `refresh_jobs` wait for the scope.
        The lock is not re-entrant: never call them from inside a scope.
        """
        if self.read_only:
            raise ReadOnlyProject(
                "project is open in KiCad (lock file present) - close it there first"
            )
        self._check_not_in_commit()
        async with self._commit_lock:
            self._commit_owner = asyncio.current_task()
            try:
                handle = await self.call(lambda h: h.schematic().begin_commit())
                # Read after the call: a lazy (re)start inside call() bumps it first.
                scope = CommitScope(label, handle, self._generation)
                self._active_scope = scope
                try:
                    yield scope
                except BaseException as exc:
                    await _uncancellable(self._drop(scope, exc))
                    raise
                try:
                    if dry_run:
                        await self.call(lambda h: h.schematic().drop_commit(handle))
                    else:
                        await self.call(lambda h: h.schematic().push_commit(handle, label))
                except BaseException as exc:
                    await _uncancellable(self._drop(scope, exc))
                    raise
                if not dry_run:
                    self.jobs_stale = True
                    self.modified = True
            finally:
                self._commit_owner = None
                self._active_scope = None

    async def _drop(self, scope: CommitScope, cause: BaseException) -> None:
        if self._kicad is None or scope.generation != self._generation:
            return  # the server that held the commit is gone, and its staged items with it
        try:
            await self.call(lambda h: h.schematic().drop_commit(scope.commit))
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
        async with self._commit_lock:
            await self._save_locked(doc)

    async def _save_locked(self, doc: DocKind) -> None:
        def _save(h: KicadHandle) -> None:
            (h.schematic() if doc == "sch" else h.board()).save()  # type: ignore[no-untyped-call]

        await self.call(_save)
        if doc == "sch":
            self.modified = False
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
        open_document (spike 2A, Q3)."""
        self._check_not_in_commit()
        async with self._commit_lock:
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

    async def save_copy(self, doc: DocKind, dest: Path) -> Path:
        """SaveCopyOfDocument to `dest`.

        Spike 1C (Q4): for a schematic the API writes only the root sheet, so the
        sub-sheet files named by the hierarchy are byte-copied from the project
        directory next to it (same relative paths). Those files are only current
        after a save, so a modified schematic is saved first (spike 2A, Q8: else
        stale sub-sheets, and a crash on a new sheet).

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

        async with self._commit_lock:
            if (
                doc == "sch"
                and not self.read_only
                and await self.call(lambda h: h.schematic().is_document_modified())
            ):
                await self._save_locked("sch")
            sheet_files = await self.call(_copy)
        if sheet_files:
            await asyncio.to_thread(self._copy_sub_sheets, sheet_files, dest.parent)
        return dest

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
        keeps the old view version: clients take the maximum per document (X2)."""
        version = 0
        async with self._lock:
            current = self._current
            if current is not None and current.project.id == project.id:
                version = current.view_version
                await current.close()
                self._current = None
        session = await self.open(project)
        session.view_version = max(session.view_version, version)
        return session

    def current(self) -> KicadSession | None:
        return self._current

    async def close_all(self) -> None:
        async with self._lock:
            if self._current is not None:
                await self._current.close()
                self._current = None
