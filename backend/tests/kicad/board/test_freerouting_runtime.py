"""The real Freerouting 2.5.0 JVM under the sandboxed runner (SP4 P6a Task 3).

Marked `kicad` although Freerouting needs no KiCad: only the CI kicad job fetches the
runtime, and the DSN input is exported from the ecc83 board through the API."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from copperchat.board import freerouting as fr
from copperchat.board.freerouting import FreeroutingRuntime, cpu_limit_s, run_freerouting
from copperchat.kicad_session import KicadSession
from copperchat.kicad_tools.board import api

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def ecc83_dsn(
    ecc83_module_session: KicadSession, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[Path]:
    dest = tmp_path_factory.mktemp("dsn") / "ecc83.dsn"
    await ecc83_module_session.call(lambda h: api.export_specctra_dsn(h, dest), timeout=60)
    assert dest.read_text().lstrip().startswith("(pcb")
    yield dest


class _Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.argv: tuple[str, ...] = ()
        real = asyncio.create_subprocess_exec

        async def spy(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
            self.argv = argv
            self.proc = await real(*argv, **kwargs)
            return self.proc

        monkeypatch.setattr(fr.asyncio, "create_subprocess_exec", spy)

    async def java_pid(self, timeout: float = 10.0) -> int:
        """The PID once `prlimit` (and `unshare`) have exec'd into the JVM."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc is not None:
                with contextlib.suppress(OSError):
                    exe = os.readlink(f"/proc/{self.proc.pid}/exe")
                    if exe.endswith("/java"):
                        return self.proc.pid
            await asyncio.sleep(0.02)
        raise AssertionError("the JVM never started")


def _limits(pid: int | str) -> dict[str, str]:
    return {
        line[:26].strip(): line[26:47].strip()
        for line in Path(f"/proc/{pid}/limits").read_text().splitlines()[1:]
    }


def _inet_inodes(pid: int) -> set[str]:
    inodes: set[str] = set()
    for table in ("tcp", "tcp6", "udp", "udp6"):
        with contextlib.suppress(OSError):
            for line in Path(f"/proc/{pid}/net/{table}").read_text().splitlines()[1:]:
                cols = line.split()
                if len(cols) > 9:
                    inodes.add(cols[9])
    return inodes


def _socket_inodes(pid: int) -> set[str]:
    found: set[str] = set()
    with contextlib.suppress(OSError):
        for task in os.listdir(f"/proc/{pid}/task"):
            with contextlib.suppress(OSError):
                for fd in os.listdir(f"/proc/{pid}/task/{task}/fd"):
                    with contextlib.suppress(OSError):
                        m = re.fullmatch(
                            r"socket:\[(\d+)\]", os.readlink(f"/proc/{pid}/task/{task}/fd/{fd}")
                        )
                        if m:
                            found.add(m.group(1))
    return found


async def test_jvm_starts_under_limits(
    freerouting_rt: FreeroutingRuntime,
    ecc83_dsn: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
) -> None:
    spy = _Spy(monkeypatch)
    ses = tmp_path / "ecc83.ses"
    task = asyncio.create_task(
        run_freerouting(
            freerouting_rt, ecc83_dsn, ses, timeout_s=120, passes=5, workdir=tmp_path / "w"
        )
    )
    pid = await spy.java_pid()
    limits = _limits(pid)
    run = await task
    assert limits["Max cpu time"] == str(cpu_limit_s(120))
    assert limits["Max open files"] == "256"
    assert limits["Max file size"] == "536870912"
    assert limits["Max address space"] == _limits("self")["Max address space"]
    assert run.returncode == 0, run.log_tail
    assert not run.timed_out
    assert run.seconds < 60
    assert ses.read_text().lstrip().startswith("(session")
    log = (tmp_path / "w" / "freerouting.log").read_text(errors="replace")
    assert "Unknown settings property" not in log  # every option name exists in 2.5.0
    assert "Freerouting v2.5.0" in log
    record_property("freerouting_ecc83_passes5_s", round(run.seconds, 2))


async def test_timeout_kills(
    freerouting_rt: FreeroutingRuntime,
    ecc83_dsn: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # JVM start-up + DSN load alone take > 1 s (measured ≈ 1.7 s before the board loads)
    spy = _Spy(monkeypatch)
    run = await run_freerouting(
        freerouting_rt,
        ecc83_dsn,
        tmp_path / "x.ses",
        timeout_s=1,
        passes=100,
        workdir=tmp_path / "w",
    )
    assert run.timed_out is True
    assert run.seconds < 10
    assert spy.proc is not None
    assert not Path(f"/proc/{spy.proc.pid}").exists()


async def test_cancel_kills_no_zombie(
    freerouting_rt: FreeroutingRuntime,
    ecc83_dsn: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _Spy(monkeypatch)
    task = asyncio.create_task(
        run_freerouting(
            freerouting_rt,
            ecc83_dsn,
            tmp_path / "x.ses",
            timeout_s=120,
            passes=100,
            workdir=tmp_path / "w",
        )
    )
    pid = await spy.java_pid()
    await asyncio.sleep(1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not Path(f"/proc/{pid}").exists()  # a zombie would still have a /proc entry
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


async def test_no_network_attempt(
    freerouting_rt: FreeroutingRuntime,
    ecc83_dsn: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Any,
) -> None:
    """The listener is the proxy port: should the proxy host ever resolve (e.g. a
    127.0.0.1 regression), every proxied HTTP(S)/SOCKS attempt lands there; the fd scan
    (every 50 ms, all threads) catches any inet socket, proxied or direct. Unix sockets
    are allowed. `test_detector_sees_unhardened_version_check` shows the method detects
    Freerouting's own version check."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    accepted = 0
    inet_seen: set[str] = set()
    spy = _Spy(monkeypatch)
    try:
        task = asyncio.create_task(
            run_freerouting(
                freerouting_rt,
                ecc83_dsn,
                tmp_path / "x.ses",
                timeout_s=120,
                passes=5,
                workdir=tmp_path / "w",
                proxy_port=port,
            )
        )
        while not task.done():
            with contextlib.suppress(BlockingIOError):
                conn, _ = listener.accept()
                conn.close()
                accepted += 1
            if spy.proc is not None:
                inet_seen |= _socket_inodes(spy.proc.pid) & _inet_inodes(spy.proc.pid)
            await asyncio.sleep(0.05)
        run = await task
        with contextlib.suppress(BlockingIOError):
            listener.accept()
            accepted += 1
    finally:
        listener.close()
    assert run.returncode == 0, run.log_tail
    assert accepted == 0
    assert inet_seen == set()
    mode = "unshare" if fr.network_namespace_available() else "options+no-dns"
    if fr.network_namespace_available():
        k = spy.argv.index("--")
        assert spy.argv[k + 1] == "unshare"
    else:
        assert "unshare" not in spy.argv
    print(f"freerouting network mode: {mode}")
    record_property("freerouting_network_mode", mode)


async def test_settings_file_has_analytics_off(
    freerouting_rt: FreeroutingRuntime, ecc83_dsn: Path, tmp_path: Path
) -> None:
    work = tmp_path / "w"
    run = await run_freerouting(
        freerouting_rt, ecc83_dsn, tmp_path / "x.ses", timeout_s=120, passes=1, workdir=work
    )
    assert run.returncode == 0, run.log_tail
    # the file Freerouting reads at start (pre-seeded by the runner) still has them off
    data = json.loads((work / ".config" / "freerouting" / "freerouting.json").read_text())
    assert data["version"] == "2.5.0"
    assert data["profile"]["allow_telemetry"] is False
    assert data["profile"]["allow_contact"] is False
    assert data["usage_and_diagnostic_data"]["disable_analytics"] is True
    assert data["api_server"]["enabled"] is False
    assert data["mcp_server"]["enabled"] is False
    assert data["gui"]["enabled"] is False


async def test_detector_sees_unhardened_version_check(
    freerouting_rt: FreeroutingRuntime, ecc83_dsn: Path, tmp_path: Path
) -> None:
    """Control: the same command without the empty hosts file and with the proxy at a
    local listener → Freerouting's unconditional version check arrives there as a
    CONNECT (nothing leaves the host: the listener closes it)."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    listener.settimeout(0.05)
    port = listener.getsockname()[1]
    work = tmp_path / "w"
    work.mkdir()
    fr.seed_settings(work)
    argv = [
        a.replace(fr.PROXY_HOST, "127.0.0.1")
        for a in fr.build_command(
            freerouting_rt,
            ecc83_dsn,
            tmp_path / "x.ses",
            passes=1,
            workdir=work,
            extra_args=(),
            unshare=False,
            timeout_s=60,
            proxy_port=port,
        )
        if not a.startswith("-Djdk.net.hosts.file=")
    ]
    requests: list[bytes] = []
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=work,
        env={"HOME": str(work), "PATH": "/usr/bin:/bin", "LANG": "C"},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        end = time.monotonic() + 60
        while proc.returncode is None and time.monotonic() < end and not requests:
            try:
                conn, _ = await asyncio.to_thread(listener.accept)
            except TimeoutError:
                continue
            with conn:
                conn.settimeout(2)
                with contextlib.suppress(OSError):
                    requests.append(conn.recv(200))
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, 9)
        await proc.wait()
        listener.close()
    assert any(b"CONNECT api.github.com" in r for r in requests), requests
