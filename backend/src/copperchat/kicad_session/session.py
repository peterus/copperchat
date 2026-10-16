from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from kipy.errors import ConnectionError as KipyConnectionError

from copperchat.config import Settings
from copperchat.errors import CopperChatError, DocumentNotOpen, ReadOnlyProject
from copperchat.kicad_session.errors import KicadCallTimeout, KicadUnavailable
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
SOCKET_DIR = Path("/tmp/copperchat")
_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    KipyConnectionError,
    ConnectionError,
    BrokenPipeError,
)


def socket_path_for(project: Project) -> Path:
    return SOCKET_DIR / f"{project.id}.sock"


def default_client_factory(socket_uri: str, timeout_ms: int) -> Any:
    from kipy import KiCad

    return KiCad(socket_path=socket_uri, client_name="copperchat", timeout_ms=timeout_ms)


def _document_type(doc: DocKind) -> int:
    from kipy.proto.common.types.base_types_pb2 import DocumentType

    return int(DocumentType.DOCTYPE_SCHEMATIC if doc == "sch" else DocumentType.DOCTYPE_PCB)


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

    @property
    def settings(self) -> Settings:
        return self._settings

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
        handlers exist only after `open_document`.
        """
        SOCKET_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(SOCKET_DIR, 0o700)
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        self.process = await self._launcher.start(self.project, self.socket_path)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.kicad_start_timeout_s
        timeout_ms = int(self._settings.kicad_api_timeout_s * 1000)
        last_error: BaseException | None = None
        client: Any = None
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
                await self._kill()
                raise KicadUnavailable(
                    f"kicad-cli api-server not ready after "
                    f"{self._settings.kicad_start_timeout_s:.0f}s: {last_error}"
                )
            try:
                candidate = self._client_factory(f"ipc://{self.socket_path}", timeout_ms)
                await asyncio.wait_for(
                    asyncio.to_thread(candidate.ping), timeout=min(2.0, remaining)
                )
                client = candidate
            except Exception as exc:  # not ready yet: socket missing, refused, slow
                last_error = exc
                await asyncio.sleep(0.2)
        opened: set[DocKind] = set()
        for doc, path in self._documents():
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(client.open_document, str(path), _document_type(doc)),
                    timeout=max(0.1, deadline - loop.time()),
                )
            except Exception as exc:
                await self._kill()
                raise KicadUnavailable(
                    f"kicad-cli api-server could not open {path.name}: {exc or type(exc).__name__}"
                ) from exc
            opened.add(doc)
        self._kicad = client
        self._open_docs = frozenset(opened)
        if self.on_started is not None:
            self.on_started()

    async def _kill(self) -> None:
        proc, self.process, self._kicad = self.process, None, None
        self._open_docs = frozenset()
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
                await self.start()
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

    async def save(self, doc: DocKind) -> None:
        if self.read_only:
            raise ReadOnlyProject(
                "project is open in KiCad (lock file present) - close it there first"
            )

        def _save(h: KicadHandle) -> None:
            (h.schematic() if doc == "sch" else h.board()).save()  # type: ignore[no-untyped-call]

        await self.call(_save)

    async def save_copy(self, doc: DocKind, dest: Path) -> Path:
        """SaveCopyOfDocument to `dest` without touching the project's own files.

        Spike 1C (Q4): for a schematic the API writes only the root sheet, so the
        sub-sheet files named by the hierarchy are byte-copied from the project
        directory next to it (same relative paths). Never calls `save()`.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _copy(h: KicadHandle) -> list[str]:
            if doc == "pcb":
                h.board().save_as(str(dest), overwrite=True, include_project=False)
                return []
            schematic = h.schematic()
            schematic.save_as(str(dest), overwrite=True, include_project=False)
            return [str(sheet.filename) for sheet in schematic.get_hierarchy()]

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
            from copperchat.kicad_session.launcher import KicadCliLauncher

            launcher = KicadCliLauncher(
                settings.kicad_cli, settings.data_dir / "logs", settings.data_dir / "kicad-config"
            )
        self._settings = settings
        self._launcher = launcher
        self._client_factory = client_factory or default_client_factory
        self._current: KicadSession | None = None
        self._lock = asyncio.Lock()
        self._tokens_dir = settings.data_dir / "kicad-locks"

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
        return lock_status(project, ignore_tokens=self._own_tokens(project))

    # -- sessions ------------------------------------------------------------------
    async def open(self, project: Project) -> KicadSession:
        async with self._lock:
            if self._current is not None and self._current.project.id == project.id:
                return self._current
            if self._current is not None:
                await self._current.close()
                self._current = None
            # Evaluated before our own server starts: it creates the same lock file.
            locks = self.lock_status(project)
            session = KicadSession(
                project,
                self._settings,
                self._launcher,
                self._client_factory,
                read_only=locks.locked,
                lock_files=locks.lock_files,
            )
            if not session.read_only:
                # No foreign lock existed, so the lock our server writes is ours. A
                # read-only session never records one: the lock may be KiCad's.
                session.on_started = lambda: self._remember_own_token(project)
            await session.start()
            self._current = session
            return session

    def current(self) -> KicadSession | None:
        return self._current

    async def close_all(self) -> None:
        async with self._lock:
            if self._current is not None:
                await self._current.close()
                self._current = None
