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
        return proc


class FakeDocument:
    """Stands in for kipy.schematic.Schematic / kipy.board.Board."""

    def __init__(self, server: FakeServer, path: str) -> None:
        self._server = server
        self._path = Path(path)

    def save(self) -> None:
        self._server.saved.append(self._path.name)

    def save_as(self, filename: str, overwrite: bool = False, include_project: bool = True) -> None:
        # The real api-server writes the document; the fake copies the source file.
        shutil.copyfile(self._path, filename)

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
        self._docs[type] = path
        self._server.opened.append((Path(path).name, type))
        return SimpleNamespace(path=path)

    def get_schematic(self) -> FakeDocument:
        self._check()
        return FakeDocument(self._server, self._docs[DocumentType.DOCTYPE_SCHEMATIC])

    def get_board(self) -> FakeDocument:
        self._check()
        return FakeDocument(self._server, self._docs[DocumentType.DOCTYPE_PCB])

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
