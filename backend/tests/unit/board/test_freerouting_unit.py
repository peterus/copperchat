"""Freerouting runner without a JVM (SP4 P6a Task 3): command shape, limits, timeouts."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from copperchat.board import freerouting as fr
from copperchat.board.freerouting import (
    FSIZE_BYTES,
    JAVA_OPTS,
    NO_NETWORK_OPTIONS,
    PROXY_BLACKHOLE,
    FreeroutingRuntime,
    build_command,
    clamp_passes,
    clamp_timeout,
    cpu_limit_s,
    find_runtime,
    run_freerouting,
)
from copperchat.config import Settings

PRLIMIT = shutil.which("prlimit")
needs_prlimit = pytest.mark.skipif(PRLIMIT is None, reason="util-linux prlimit not on PATH")


_clear_ns_probe = fr.network_namespace_available.cache_clear


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    fr._RUNTIME_CACHE.clear()
    _clear_ns_probe()
    yield
    fr._RUNTIME_CACHE.clear()
    _clear_ns_probe()


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _rt(tmp_path: Path, body: str, *, prlimit: str | None = PRLIMIT) -> FreeroutingRuntime:
    java = _script(tmp_path / "rt" / "jre" / "bin" / "java", body)
    jar = tmp_path / "rt" / fr.JAR_NAME
    jar.write_bytes(b"")
    return FreeroutingRuntime(
        java=java, jar=jar, version="2.5.0", prlimit=Path(prlimit) if prlimit else None
    )


def _home(tmp_path: Path, version: str = "25.0.1", *, manifest: bool = True) -> Path:
    home = tmp_path / "home"
    _script(home / "jre" / "bin" / "java", f"echo 'openjdk version \"{version}\" 2026-10-21' >&2")
    (home / fr.JAR_NAME).write_bytes(b"")
    if manifest:
        (home / ".manifest.sha256").write_text("")
    return home


def _settings(home: Path, *, on: bool = True) -> Settings:
    return Settings(_env_file=None, freerouting=on, freerouting_home=home)  # type: ignore[call-arg]


def _no_unshare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fr, "network_namespace_available", lambda: False)


class _Spy:
    """Wraps asyncio.create_subprocess_exec; keeps the process and the keywords."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.kwargs: dict[str, Any] = {}
        self.argv: tuple[str, ...] = ()
        real = asyncio.create_subprocess_exec

        async def spy(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
            self.argv, self.kwargs = argv, kwargs
            self.proc = await real(*argv, **kwargs)
            return self.proc

        monkeypatch.setattr(fr.asyncio, "create_subprocess_exec", spy)

    async def pid(self, marker: str, timeout: float = 5.0) -> int:
        """PID once the process has exec'd into the program whose cmdline has `marker`."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc is not None:
                try:
                    cmdline = Path(f"/proc/{self.proc.pid}/cmdline").read_bytes()
                except OSError:
                    cmdline = b""
                if marker.encode() in cmdline:
                    return self.proc.pid
            await asyncio.sleep(0.02)
        raise AssertionError(f"process never ran {marker!r}")


def _limits(pid: int | str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in Path(f"/proc/{pid}/limits").read_text().splitlines()[1:]:
        name = line[:26].strip()
        out[name] = line[26:47].strip()  # soft limit column
    return out


# -- command -------------------------------------------------------------------------


def test_command_shape(tmp_path: Path) -> None:
    rt = FreeroutingRuntime(Path("/fr/jre/bin/java"), Path("/fr/x.jar"), "2.5.0", Path("/p"))
    cmd = build_command(
        rt,
        Path("/w/b.dsn"),
        Path("/w/b.ses"),
        passes=7,
        workdir=Path("/w"),
        extra_args=("--x=1",),
        unshare=False,
        timeout_s=300,
    )
    assert cmd[:5] == [
        "/p",
        "--cpu=630:635",
        "--nofile=256:256",
        "--fsize=536870912:536870912",
        "--",
    ]
    assert cmd[5] == "/fr/jre/bin/java"
    i = 6
    assert tuple(cmd[i : i + len(JAVA_OPTS)]) == JAVA_OPTS
    i += len(JAVA_OPTS)
    assert tuple(cmd[i : i + len(PROXY_BLACKHOLE)]) == PROXY_BLACKHOLE
    i += len(PROXY_BLACKHOLE)
    assert cmd[i:] == [
        "-Djdk.net.hosts.file=/w/.hosts-empty",
        "-Duser.home=/w",
        "-Djava.io.tmpdir=/w",
        "-jar",
        "/fr/x.jar",
        "-de",
        "/w/b.dsn",
        "-do",
        "/w/b.ses",
        "--router.autorouter.max_passes=7",
        "--gui.enabled=false",
        *NO_NETWORK_OPTIONS,
        "--x=1",
    ]
    assert "-XX:ActiveProcessorCount=2" in cmd and "-Xmx1g" in cmd
    assert not any(a.startswith("-Xms") for a in cmd)
    assert not any(a.startswith("--as") for a in cmd)
    assert cpu_limit_s(300) == 630
    assert FSIZE_BYTES == 536870912
    assert "--api_server.enabled=false" in NO_NETWORK_OPTIONS
    assert "--usage_and_diagnostic_data.disable_analytics=true" in NO_NETWORK_OPTIONS


def test_command_with_unshare() -> None:
    rt = FreeroutingRuntime(Path("/j"), Path("/x.jar"), "2.5.0", Path("/p"))
    cmd = build_command(
        rt,
        Path("/d"),
        Path("/s"),
        passes=1,
        workdir=Path("/w"),
        extra_args=(),
        unshare=True,
        timeout_s=10,
    )
    k = cmd.index("--")
    assert cmd[k + 1 : k + 6] == ["unshare", "-r", "-n", "--", "/j"]


def test_proxy_port_parameter() -> None:
    rt = FreeroutingRuntime(Path("/j"), Path("/x.jar"), "2.5.0", Path("/p"))
    cmd = build_command(
        rt,
        Path("/d"),
        Path("/s"),
        passes=1,
        workdir=Path("/w"),
        extra_args=(),
        unshare=False,
        timeout_s=10,
        proxy_port=4711,
    )
    assert "-Dhttps.proxyPort=4711" in cmd and "-DsocksProxyPort=4711" in cmd
    assert "-Dhttps.proxyHost=proxy.invalid" in cmd
    assert "-Djava.net.useSystemProxies=false" in cmd


def test_command_without_prlimit_starts_with_java() -> None:
    rt = FreeroutingRuntime(Path("/j"), Path("/x.jar"), "2.5.0", None)
    cmd = build_command(
        rt,
        Path("/d"),
        Path("/s"),
        passes=1,
        workdir=Path("/w"),
        extra_args=(),
        unshare=False,
        timeout_s=10,
    )
    assert cmd[0] == "/j"


def test_timeout_and_passes_clamps() -> None:
    assert clamp_timeout(900) == 600
    assert clamp_timeout(12.5) == 12.5
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            clamp_timeout(bad)
    assert (clamp_passes(0), clamp_passes(5), clamp_passes(1000)) == (1, 5, 100)


# -- running (fake java) ----------------------------------------------------------------


@needs_prlimit
async def test_limits_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_unshare(monkeypatch)
    spy = _Spy(monkeypatch)
    rt = _rt(tmp_path, "exec sleep 5")
    task = asyncio.create_task(
        run_freerouting(
            rt, tmp_path / "b.dsn", tmp_path / "b.ses", timeout_s=40, passes=3, workdir=tmp_path
        )
    )
    try:
        pid = await spy.pid("sleep")
        limits = _limits(pid)
        mine = _limits("self")
        assert limits["Max cpu time"] == str(cpu_limit_s(40))
        assert limits["Max open files"] == "256"
        assert limits["Max file size"] == "536870912"
        assert limits["Max address space"] == mine["Max address space"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_no_preexec_fn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_unshare(monkeypatch)
    spy = _Spy(monkeypatch)
    rt = _rt(tmp_path, "exit 0")
    run = await run_freerouting(
        rt, tmp_path / "b.dsn", tmp_path / "b.ses", timeout_s=20, passes=3, workdir=tmp_path
    )
    assert run.returncode == 0 and not run.timed_out
    assert "preexec_fn" not in spy.kwargs
    assert spy.kwargs["start_new_session"] is True
    assert spy.kwargs["env"] == {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "LANG": "C"}
    assert spy.kwargs["cwd"] == tmp_path


@needs_prlimit
async def test_cpu_limit_maps_to_timed_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_unshare(monkeypatch)
    monkeypatch.setattr(fr, "cpu_limit_s", lambda _t: 1)
    rt = _rt(tmp_path, 'exec python3 -c "while True: pass"')
    run = await run_freerouting(
        rt, tmp_path / "b.dsn", tmp_path / "b.ses", timeout_s=20, passes=3, workdir=tmp_path
    )
    assert run.returncode == -signal.SIGXCPU
    assert run.timed_out is True
    assert run.seconds < 10


async def test_external_sigkill_maps_to_timed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_unshare(monkeypatch)
    spy = _Spy(monkeypatch)
    rt = _rt(tmp_path, "exec sleep 30")
    task = asyncio.create_task(
        run_freerouting(
            rt, tmp_path / "b.dsn", tmp_path / "b.ses", timeout_s=40, passes=3, workdir=tmp_path
        )
    )
    pid = await spy.pid("sleep")
    os.kill(pid, signal.SIGKILL)
    run = await asyncio.wait_for(task, 10)
    assert run.returncode == -signal.SIGKILL
    assert run.timed_out is True


async def test_normal_nonzero_exit_not_timed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_unshare(monkeypatch)
    rt = _rt(tmp_path, "echo routing failed; exit 3")
    run = await run_freerouting(
        rt, tmp_path / "b.dsn", tmp_path / "b.ses", timeout_s=20, passes=3, workdir=tmp_path
    )
    assert run.returncode == 3
    assert run.timed_out is False
    assert "routing failed" in run.log_tail


async def test_wall_timeout_kills_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_unshare(monkeypatch)
    spy = _Spy(monkeypatch)
    # a child in the same process group survives unless the whole group is killed
    rt = _rt(tmp_path, f"sleep 30 & echo $! > {tmp_path}/child.pid; wait")
    run = await run_freerouting(
        rt, tmp_path / "b.dsn", tmp_path / "b.ses", timeout_s=1, passes=3, workdir=tmp_path
    )
    assert run.timed_out is True
    assert spy.proc is not None and not Path(f"/proc/{spy.proc.pid}").exists()
    child = int((tmp_path / "child.pid").read_text())
    for _ in range(100):  # init reaps the orphan shortly after the kill
        if not Path(f"/proc/{child}").exists():
            break
        await asyncio.sleep(0.05)
    assert not Path(f"/proc/{child}").exists()


async def test_cancel_kills_and_reaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_unshare(monkeypatch)
    spy = _Spy(monkeypatch)
    rt = _rt(tmp_path, "exec sleep 30")
    task = asyncio.create_task(
        run_freerouting(
            rt, tmp_path / "b.dsn", tmp_path / "b.ses", timeout_s=40, passes=3, workdir=tmp_path
        )
    )
    pid = await spy.pid("sleep")
    t0 = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - t0 < 5  # killed, not waited out
    assert not Path(f"/proc/{pid}").exists()
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


async def test_timeout_clamped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_unshare(monkeypatch)
    seen: dict[str, Any] = {}
    real = fr.build_command

    def spy(*args: Any, **kwargs: Any) -> list[str]:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(fr, "build_command", spy)
    rt = _rt(tmp_path, "exit 0")
    await run_freerouting(
        rt, tmp_path / "b.dsn", tmp_path / "b.ses", timeout_s=900, passes=500, workdir=tmp_path
    )
    assert seen["timeout_s"] == 600
    assert seen["passes"] == 100


async def test_settings_seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_unshare(monkeypatch)
    rt = _rt(tmp_path, "exit 0")
    await run_freerouting(
        rt, tmp_path / "b.dsn", tmp_path / "b.ses", timeout_s=20, passes=3, workdir=tmp_path
    )
    assert (tmp_path / fr.HOSTS_FILE).read_bytes() == b""
    data = json.loads((tmp_path / fr.SETTINGS_FILE).read_text())
    assert data["profile"]["allow_telemetry"] is False
    assert data["usage_and_diagnostic_data"]["disable_analytics"] is True
    assert data["api_server"]["enabled"] is False
    assert data["mcp_server"]["enabled"] is False


async def test_fallback_limits_without_prlimit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_unshare(monkeypatch)
    spy = _Spy(monkeypatch)
    rt = _rt(tmp_path, "exec sleep 5", prlimit=None)
    task = asyncio.create_task(
        run_freerouting(
            rt, tmp_path / "b.dsn", tmp_path / "b.ses", timeout_s=40, passes=3, workdir=tmp_path
        )
    )
    try:
        pid = await spy.pid("sleep")
        assert spy.argv[0] == str(rt.java)
        limits = _limits(pid)
        assert limits["Max cpu time"] == str(cpu_limit_s(40))
        assert limits["Max open files"] == "256"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# -- find_runtime -------------------------------------------------------------------------


def test_find_runtime_missing(tmp_path: Path) -> None:
    assert find_runtime(_settings(tmp_path)) is None


def test_find_runtime_flag_off(tmp_path: Path) -> None:
    assert find_runtime(_settings(_home(tmp_path), on=False)) is None


def test_find_runtime_wrong_java(tmp_path: Path) -> None:
    assert find_runtime(_settings(_home(tmp_path, "21.0.2"))) is None


@needs_prlimit
def test_find_runtime_ok_fake(tmp_path: Path) -> None:
    home = _home(tmp_path)
    rt = find_runtime(_settings(home))
    assert rt is not None
    assert rt.java == home / "jre" / "bin" / "java"
    assert rt.jar == home / "freerouting-2.5.0.jar"
    assert rt.version == "2.5.0"
    assert rt.prlimit == Path(str(PRLIMIT))


@needs_prlimit
def test_find_runtime_needs_manifest(tmp_path: Path) -> None:
    assert find_runtime(_settings(_home(tmp_path, manifest=False))) is None


def test_find_runtime_needs_executable_java(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "jre" / "bin" / "java").chmod(0o644)
    assert find_runtime(_settings(home)) is None


def test_find_runtime_needs_prlimit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path)
    monkeypatch.setattr(fr.shutil, "which", lambda name: None)
    assert find_runtime(_settings(home)) is None
    monkeypatch.setattr(fr, "ALLOW_PRLIMIT_FALLBACK", True)
    rt = find_runtime(_settings(home))
    assert rt is not None and rt.prlimit is None


@needs_prlimit
def test_find_runtime_cached_until_java_changes(tmp_path: Path) -> None:
    home = _home(tmp_path)
    assert find_runtime(_settings(home)) is not None
    java = home / "jre" / "bin" / "java"
    _script(java, "echo 'openjdk version \"21.0.2\"' >&2")
    os.utime(java, ns=(time.time_ns(), time.time_ns() + 10**9))
    assert find_runtime(_settings(home)) is None


def test_network_namespace_probe_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Done:
        returncode = 1

    def fake_run(argv: list[str], **_kw: Any) -> Done:
        calls.append(argv)
        return Done()

    monkeypatch.setattr(fr.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(fr.subprocess, "run", fake_run)
    assert fr.network_namespace_available() is False
    assert fr.network_namespace_available() is False
    assert calls == [["/usr/bin/unshare", "-r", "-n", "--", "true"]]
