"""Test doubles for the kicad-cli api-server process and the kipy client."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from kipy.errors import ApiError
from kipy.errors import ConnectionError as KipyConnectionError
from kipy.proto.common.types.base_types_pb2 import DocumentType

from copperchat.projects import Project

# Mimics the real api-server (spike 1C, Q7): it writes ~STEM.kicad_pro.lck with a
# JSON body on start and removes it on SIGTERM; kill -9 leaves it behind.
_SERVER_SCRIPT = """
import json, os, signal, sys, time
lock, token = sys.argv[1], sys.argv[2]
if lock:
    with open(lock, "w") as fh:
        json.dump({"hostname": "fakehost", "token": token, "username": "fake"}, fh)
def _term(*_):
    if lock and os.path.exists(lock):
        os.remove(lock)
    sys.exit(0)
signal.signal(signal.SIGTERM, _term)
time.sleep(3600)
"""


@dataclass
class FakeServer:
    """Shared state between FakeLauncher and FakeKiCad clients."""

    dead: bool = False
    starts: int = 0
    clients: int = 0
    fail_start_with_exit_code: int | None = None
    write_lock: bool = True
    release: threading.Event = field(default_factory=threading.Event)
    processes: list[asyncio.subprocess.Process] = field(default_factory=list)
    opened: list[tuple[str, int]] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)
    hierarchy: list[str] = field(default_factory=list)
    closed: int = 0
    modified: bool = False  # what is_document_modified() answers
    library_loads: list[int] = field(default_factory=list)  # load_all_libraries(types)
    libraries_loading_polls: int = 0  # get_library_statuses answers "loading" this often
    closed_docs: list[str] = field(default_factory=list)
    fail_open_after: int | None = None  # open_document raises once len(opened) reaches it
    # Commits: ids open on the current server process, and what happened to them.
    open_commits: set[int] = field(default_factory=set)
    pushed: list[tuple[int, str]] = field(default_factory=list)
    dropped: list[int] = field(default_factory=list)
    next_commit: int = 0
    fail_push: bool = False
    fail_drop: bool = False
    drop_gate: threading.Event | None = None  # drop_commit waits for it when set
    commit_gate: threading.Event | None = None  # begin/push_commit wait for it when set
    gate_reached: threading.Event = field(default_factory=threading.Event)  # a call waits
    library_load_hangs: bool = False  # load_all_libraries never answers
    library_load_refusals: int = 0  # load_all_libraries raises this often

    def crash(self) -> None:
        self.dead = True
        for proc in self.processes:
            if proc.returncode is None:
                proc.kill()


class FakeLauncher:
    def __init__(self, server: FakeServer) -> None:
        self.server = server

    async def start(self, project: Project, socket_path: Path) -> asyncio.subprocess.Process:
        self.server.starts += 1
        code = self.server.fail_start_with_exit_code
        if code is not None:
            proc = await asyncio.create_subprocess_exec(sys.executable, "-c", f"exit({code})")
            self.server.processes.append(proc)
            # Simulated start failure: let the process exit before the session
            # polls it, and never mark the server alive.
            await proc.wait()
            return proc
        lock = str(project.dir / f"~{project.pro_file.name}.lck") if self.server.write_lock else ""
        token = f"fake-token-{self.server.starts}"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _SERVER_SCRIPT, lock, token
        )
        self.server.processes.append(proc)
        if lock:
            for _ in range(200):  # wait until the fake server has written its lock
                if Path(lock).exists():
                    break
                await asyncio.sleep(0.01)
        self.server.dead = False
        self.server.open_commits.clear()  # a fresh process holds no commits
        return proc


class FakeDocument:
    """Stands in for kipy.schematic.Schematic / kipy.board.Board."""

    def __init__(self, server: FakeServer, path: str) -> None:
        self._server = server
        self._path = Path(path)

    def save(self) -> None:
        self._server.saved.append(self._path.name)
        self._server.modified = False

    def is_document_modified(self) -> bool:
        return self._server.modified

    def begin_commit(self) -> SimpleNamespace:
        if self._server.commit_gate is not None:
            self._server.gate_reached.set()
            self._server.commit_gate.wait(10)
        if self._server.open_commits:
            raise ApiError("KiCad returned error: client already has a commit in progress")
        self._server.next_commit += 1
        self._server.open_commits.add(self._server.next_commit)
        return SimpleNamespace(id=self._server.next_commit)

    def push_commit(self, commit: SimpleNamespace, message: str = "") -> None:
        if self._server.commit_gate is not None:
            self._server.gate_reached.set()
            self._server.commit_gate.wait(10)
        if self._server.fail_push or commit.id not in self._server.open_commits:
            raise ApiError("KiCad returned error: no such commit")
        self._server.open_commits.discard(commit.id)
        self._server.pushed.append((commit.id, message))
        self._server.modified = True

    def drop_commit(self, commit: SimpleNamespace) -> None:
        if self._server.drop_gate is not None:
            self._server.gate_reached.set()
            self._server.drop_gate.wait(10)
        if self._server.fail_drop or commit.id not in self._server.open_commits:
            raise ApiError("KiCad returned error: no such commit")
        self._server.open_commits.discard(commit.id)
        self._server.dropped.append(commit.id)

    def save_as(self, filename: str, overwrite: bool = False, include_project: bool = True) -> None:
        # The real api-server writes the document; the fake copies the source file.
        dest = Path(filename)
        rules, dest_rules = self._path.with_suffix(".kicad_dru"), dest.with_suffix(".kicad_dru")
        if self._path.suffix == ".kicad_pcb" and rules.exists():
            # Like HEADLESS_PCB_CONTEXT::SavePcbCopy: it copies the board's design rules
            # next to the copy, and refuses when a rules file is already there.
            if dest_rules.exists():
                raise ApiError(f"KiCad returned error: board could not be saved to '{dest}'")
            shutil.copyfile(rules, dest_rules)
        shutil.copyfile(self._path, dest)

    def get_hierarchy(self) -> list[SimpleNamespace]:
        names = self._server.hierarchy or [self._path.name]
        return [SimpleNamespace(filename=n) for n in names]


class FakeKiCad:
    def __init__(self, server: FakeServer) -> None:
        self._server = server
        self._docs: dict[int, str] = {}

    def _check(self) -> None:
        if self._server.dead:
            raise KipyConnectionError("Failed to send command to KiCad: connection refused")

    def ping(self) -> None:
        self._check()

    def get_version(self) -> Any:
        self._check()
        return SimpleNamespace(full_version="11.0.0-fake", major=11, minor=0, patch=0)

    def open_document(self, path: str, type: int) -> Any:
        self._check()
        limit = self._server.fail_open_after
        if limit is not None and len(self._server.opened) >= limit:
            self._server.fail_open_after = None
            raise ApiError("KiCad returned error: could not open the document")
        self._docs[type] = path
        self._server.opened.append((Path(path).name, type))
        return SimpleNamespace(path=path)

    def get_open_documents(self, type: int) -> list[Any]:
        self._check()
        return [SimpleNamespace(path=self._docs[type])] if type in self._docs else []

    def close_document(self, document: Any) -> None:
        self._check()
        self._server.closed_docs.append(Path(document.path).name)

    def load_all_libraries(self, types: int) -> None:
        self._check()
        if self._server.library_load_hangs:
            self._server.release.wait(10)
        if self._server.library_load_refusals > 0:
            self._server.library_load_refusals -= 1
            raise ApiError("KiCad returned error: library load not possible")
        self._server.library_loads.append(types)

    def get_library_statuses(self, types: int) -> list[Any]:
        from kipy.proto.common.types import LibraryLoadStatus

        self._check()
        loading = self._server.libraries_loading_polls > 0
        self._server.libraries_loading_polls -= 1
        status = LibraryLoadStatus.LLS_LOADING if loading else LibraryLoadStatus.LLS_LOADED
        return [SimpleNamespace(status=status)]

    def get_schematic(self) -> FakeDocument:
        self._check()
        return FakeDocument(self._server, self._docs[DocumentType.DOCTYPE_SCHEMATIC])

    def get_board(self) -> FakeDocument:
        self._check()
        return FakeDocument(self._server, self._docs[DocumentType.DOCTYPE_PCB])

    def close(self) -> None:
        self._server.closed += 1

    def hang(self) -> None:
        """Simulates an api-server that never answers."""
        self._server.release.wait(10)


class FakeClientFactory:
    def __init__(self, server: FakeServer) -> None:
        self.server = server

    def __call__(self, socket_uri: str, timeout_ms: int) -> FakeKiCad:
        assert socket_uri.startswith("ipc://")
        self.server.clients += 1
        return FakeKiCad(self.server)


def read_lock_token(path: Path) -> str:
    return str(json.loads(path.read_text())["token"])
