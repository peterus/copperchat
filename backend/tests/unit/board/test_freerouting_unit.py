"""Freerouting runner without a JVM (SP4 P6a Task 3): command shape, limits, timeouts."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import stat
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from copperchat.board import freerouting as fr
from copperchat.board import heavy
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


_clear_ns_probe = fr.unshare_path.cache_clear


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    fr._RUNTIME_CACHE.clear()
    fr._SLOTS.clear()
    heavy._GATES.clear()
    _clear_ns_probe()
    yield
    fr._RUNTIME_CACHE.clear()
    fr._SLOTS.clear()
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
    monkeypatch.setattr(fr, "unshare_path", lambda: None)


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
        unshare=None,
        timeout_s=300,
    )
    assert cmd[:6] == [
        "/p",
        "--cpu=630:635",
        "--nofile=256:256",
        "--fsize=536870912:536870912",
        "--core=0:0",
        "--",
    ]
    assert cmd[6] == "/fr/jre/bin/java"
    i = 7
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
        "--x=1",
        "--gui.enabled=false",
        *NO_NETWORK_OPTIONS,
    ]
    assert "-XX:ActiveProcessorCount=2" in cmd and "-Xmx1g" in cmd
    assert "-XX:-CreateCoredumpOnCrash" in JAVA_OPTS
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
        unshare=Path("/usr/bin/unshare"),
        timeout_s=10,
    )
    k = cmd.index("--")
    assert cmd[k + 1 : k + 6] == ["/usr/bin/unshare", "-r", "-n", "--", "/j"]


@pytest.mark.parametrize(
    "arg",
    [
        "--gui.enabled=true",
        "--api_server.enabled=true",
        "--mcp_server.enabled",
        "--profile.allow_telemetry=true",
        "--profile.allow_contact=true",
        "--usage_and_diagnostic_data.disable_analytics=false",
    ],
)
def test_extra_args_cannot_override_no_network(arg: str) -> None:
    rt = FreeroutingRuntime(Path("/j"), Path("/x.jar"), "2.5.0", Path("/p"))
    with pytest.raises(ValueError, match="extra_args"):
        build_command(
            rt,
            Path("/d"),
            Path("/s"),
            passes=1,
            workdir=Path("/w"),
            extra_args=("--x=1", arg),
            unshare=None,
            timeout_s=10,
        )


@pytest.mark.parametrize(
    "arg",
    [
        "--router.autorouter.max_passes=500",
        "--router.max_threads=16",
        "--router.autorouter.max_threads=8",
        "--router.optimizer.max_threads=8",
        "-mp",
        "-mt",
    ],
)
def test_settings_cannot_override_locked(arg: str) -> None:
    """R4: the pass count (`passes`, clamped) and every thread count stay ours."""
    rt = FreeroutingRuntime(Path("/j"), Path("/x.jar"), "2.5.0", Path("/p"))
    with pytest.raises(ValueError, match="extra_args"):
        build_command(
            rt,
            Path("/d"),
            Path("/s"),
            passes=1,
            workdir=Path("/w"),
            extra_args=(arg,),
            unshare=None,
            timeout_s=10,
        )


def test_settings_in_command() -> None:
    """`FREEROUTING_SETTINGS` (and any extra argument) reach the command line after the
    pass count and before the locked switches."""
    rt = FreeroutingRuntime(Path("/j"), Path("/x.jar"), "2.5.0", Path("/p"))
    extra = (*fr.FREEROUTING_SETTINGS, "--router.optimizer.enabled=true")
    cmd = build_command(
        rt,
        Path("/d"),
        Path("/s"),
        passes=7,
        workdir=Path("/w"),
        extra_args=extra,
        unshare=None,
        timeout_s=10,
    )
    i = cmd.index("--router.autorouter.max_passes=7")
    assert cmd[i + 1 : i + 1 + len(extra)] == list(extra)
    assert cmd.index("--gui.enabled=false") > i + len(extra)
    for arg in fr.FREEROUTING_SETTINGS:
        assert not arg.startswith(fr.LOCKED_OPTION_KEYS)


async def test_run_rejects_override_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_unshare(monkeypatch)
    spy = _Spy(monkeypatch)
    rt = _rt(tmp_path, "exit 0")
    with pytest.raises(ValueError):
        await run_freerouting(
            rt,
            tmp_path / "b.dsn",
            tmp_path / "b.ses",
            timeout_s=20,
            passes=3,
            workdir=tmp_path / "w",
            extra_args=("--api_server.enabled=true",),
        )
    assert spy.proc is None


def test_proxy_port_parameter() -> None:
    rt = FreeroutingRuntime(Path("/j"), Path("/x.jar"), "2.5.0", Path("/p"))
    cmd = build_command(
        rt,
        Path("/d"),
        Path("/s"),
        passes=1,
        workdir=Path("/w"),
        extra_args=(),
        unshare=None,
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
        unshare=None,
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
        assert limits["Max core file size"] == "0"
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


_WRITE_SES = 'while [ $# -gt 0 ]; do [ "$1" = "-do" ] && echo routed > "$2"; shift; done; exit 0'


async def test_relative_paths_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Relative workdir/dsn/ses resolve against our cwd, not the JVM's cwd (workdir)."""
    _no_unshare(monkeypatch)
    spy = _Spy(monkeypatch)
    rt = _rt(tmp_path, _WRITE_SES)
    monkeypatch.chdir(tmp_path)
    run = await run_freerouting(
        rt, Path("b.dsn"), Path("b.ses"), timeout_s=20, passes=3, workdir=Path("work")
    )
    assert run.returncode == 0
    assert (tmp_path / "b.ses").read_text() == "routed\n"
    assert not (tmp_path / "work" / "b.ses").exists()
    argv = list(spy.argv)
    assert argv[argv.index("-de") + 1] == str(tmp_path / "b.dsn")
    assert argv[argv.index("-do") + 1] == str(tmp_path / "b.ses")
    assert spy.kwargs["cwd"] == tmp_path / "work"


async def test_stale_ses_removed_before_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_unshare(monkeypatch)
    rt = _rt(tmp_path, "exit 0")
    ses = tmp_path / "b.ses"
    ses.write_text("stale")
    run = await run_freerouting(
        rt, tmp_path / "b.dsn", ses, timeout_s=20, passes=3, workdir=tmp_path / "w"
    )
    assert run.returncode == 0
    assert not ses.exists()


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
        assert limits["Max core file size"] == "0"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# -- find_runtime -------------------------------------------------------------------------


def test_find_runtime_missing(tmp_path: Path) -> None:
    assert find_runtime(_settings(tmp_path)) is None


def test_find_runtime_flag_off(tmp_path: Path) -> None:
    assert find_runtime(_settings(_home(tmp_path), on=False)) is None


def test_find_runtime_wrong_java(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fr.shutil, "which", lambda name: f"/usr/bin/{name}")
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


def test_find_runtime_needs_executable_java(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fr.shutil, "which", lambda name: f"/usr/bin/{name}")
    home = _home(tmp_path)
    (home / "jre" / "bin" / "java").chmod(0o644)
    assert find_runtime(_settings(home)) is None


def test_find_runtime_relative_home_made_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fr.shutil, "which", lambda name: f"/usr/bin/{name}")
    _home(tmp_path)
    monkeypatch.chdir(tmp_path)
    rt = find_runtime(_settings(Path("home")))
    assert rt is not None
    assert rt.java == tmp_path / "home" / "jre" / "bin" / "java"
    assert rt.jar == tmp_path / "home" / fr.JAR_NAME
    assert rt.java.is_absolute() and rt.jar.is_absolute()


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


@pytest.mark.parametrize("rc", [0, 1])
def test_network_namespace_probe_cached(monkeypatch: pytest.MonkeyPatch, rc: int) -> None:
    calls: list[list[str]] = []

    class Done:
        returncode = rc

    def fake_run(argv: list[str], **_kw: Any) -> Done:
        calls.append(argv)
        return Done()

    monkeypatch.setattr(fr.shutil, "which", lambda name: f"/opt/ul/bin/{name}")
    monkeypatch.setattr(fr.subprocess, "run", fake_run)
    want = Path("/opt/ul/bin/unshare") if rc == 0 else None
    assert fr.unshare_path() == want
    assert fr.unshare_path() == want
    assert fr.network_namespace_available() is (rc == 0)
    assert calls == [["/opt/ul/bin/unshare", "-r", "-n", "--", "true"]]


async def test_run_uses_probed_unshare_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe runs off the event loop and the command uses the probed absolute path."""
    probe_threads: list[threading.Thread] = []

    def probe() -> Path:
        probe_threads.append(threading.current_thread())
        return Path("/opt/ul/bin/unshare")

    seen: dict[str, Any] = {}
    real = fr.build_command

    def spy(*args: Any, **kwargs: Any) -> list[str]:
        seen.update(kwargs)
        return real(*args, **{**kwargs, "unshare": None})

    monkeypatch.setattr(fr, "unshare_path", probe)
    monkeypatch.setattr(fr, "build_command", spy)
    rt = _rt(tmp_path, "exit 0")
    run = await run_freerouting(
        rt, tmp_path / "b.dsn", tmp_path / "b.ses", timeout_s=20, passes=3, workdir=tmp_path
    )
    assert run.returncode == 0
    assert seen["unshare"] == Path("/opt/ul/bin/unshare")
    assert probe_threads and probe_threads[0] is not threading.main_thread()


def test_failed_probe_cached_only_briefly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path)
    monkeypatch.setattr(fr.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[int] = []
    results: list[FreeroutingRuntime | None] = [None, None, rt := _rt(tmp_path, "exit 0")]

    def probe(*_a: Any) -> FreeroutingRuntime | None:
        calls.append(1)
        return results[len(calls) - 1]

    now = [1000.0]
    monkeypatch.setattr(fr, "_probe", probe)
    monkeypatch.setattr(fr, "_monotonic", lambda: now[0])
    assert find_runtime(_settings(home)) is None
    now[0] += fr.FAILURE_TTL_S - 1
    assert find_runtime(_settings(home)) is None
    assert len(calls) == 1  # still cached
    now[0] += 2
    assert find_runtime(_settings(home)) is None
    assert len(calls) == 2  # expired: probed again
    now[0] += fr.FAILURE_TTL_S + 1
    assert find_runtime(_settings(home)) is rt
    now[0] += 10 * fr.FAILURE_TTL_S
    assert find_runtime(_settings(home)) is rt
    assert len(calls) == 3  # a success stays cached


# -- concurrency slots ---------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _no_gate(kind: str, *, wait_s: float) -> AsyncIterator[None]:
    yield


async def test_second_run_times_out_waiting_for_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slot wait itself (the heavy gate, which serialises runs first, taken out)."""
    _no_unshare(monkeypatch)
    monkeypatch.setattr(fr, "MAX_CONCURRENT_RUNS", 1)
    monkeypatch.setattr(fr, "heavy_slot", _no_gate)
    spy = _Spy(monkeypatch)
    slow = _rt(tmp_path / "slow", "exec sleep 30")
    first = asyncio.create_task(
        run_freerouting(
            slow,
            tmp_path / "b.dsn",
            tmp_path / "b.ses",
            timeout_s=40,
            passes=3,
            workdir=tmp_path / "a",
        )
    )
    try:
        await spy.pid("sleep")
        started = spy.proc
        run = await run_freerouting(
            _rt(tmp_path / "fast", "exit 0"),
            tmp_path / "b.dsn",
            tmp_path / "b.ses",
            timeout_s=1,
            passes=3,
            workdir=tmp_path / "b",
        )
        assert spy.proc is started  # no second process
        assert run.returncode == -1
        assert run.timed_out is True and run.started is False
        assert 0.9 <= run.seconds < 3
        assert "free Freerouting slot" in run.log_tail and "MAX_CONCURRENT_RUNS=1" in run.log_tail
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first


async def test_second_run_waits_and_uses_remaining_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_unshare(monkeypatch)
    monkeypatch.setattr(fr, "MAX_CONCURRENT_RUNS", 1)
    first = asyncio.create_task(
        run_freerouting(
            _rt(tmp_path / "s", "sleep 1.5; exit 0"),
            tmp_path / "b.dsn",
            tmp_path / "b.ses",
            timeout_s=20,
            passes=3,
            workdir=tmp_path / "a",
        )
    )
    await asyncio.sleep(0.2)
    # waits ≈ 1.3 s for the slot, then its JVM gets only the remaining ≈ 1.7 s
    second = await run_freerouting(
        _rt(tmp_path / "l", "exec sleep 30"),
        tmp_path / "b.dsn",
        tmp_path / "b.ses",
        timeout_s=3,
        passes=3,
        workdir=tmp_path / "b",
    )
    assert (await first).returncode == 0
    assert second.timed_out is True
    assert second.returncode == -signal.SIGKILL
    assert 2.5 <= second.seconds < 4.5


async def test_slot_released_after_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_unshare(monkeypatch)
    monkeypatch.setattr(fr, "MAX_CONCURRENT_RUNS", 1)
    spy = _Spy(monkeypatch)
    slow = _rt(tmp_path / "slow", "exec sleep 30")
    holder = asyncio.create_task(
        run_freerouting(
            slow,
            tmp_path / "b.dsn",
            tmp_path / "b.ses",
            timeout_s=40,
            passes=3,
            workdir=tmp_path / "a",
        )
    )
    await spy.pid("sleep")
    waiter = asyncio.create_task(
        run_freerouting(
            slow,
            tmp_path / "b.dsn",
            tmp_path / "b.ses",
            timeout_s=40,
            passes=3,
            workdir=tmp_path / "b",
        )
    )
    await asyncio.sleep(0.2)
    waiter.cancel()  # cancelled while waiting for the slot
    with pytest.raises(asyncio.CancelledError):
        await waiter
    holder.cancel()  # cancelled while running
    with pytest.raises(asyncio.CancelledError):
        await holder
    run = await asyncio.wait_for(
        run_freerouting(
            _rt(tmp_path / "fast", "exit 0"),
            tmp_path / "b.dsn",
            tmp_path / "b.ses",
            timeout_s=5,
            passes=3,
            workdir=tmp_path / "c",
        ),
        5,
    )
    assert run.returncode == 0 and not run.timed_out


# -- R6: the heavy-process gate ---------------------------------------------------------


async def test_freerouting_holds_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is held while the JVM runs and free once it has exited."""
    _no_unshare(monkeypatch)
    spy = _Spy(monkeypatch)
    run = asyncio.create_task(
        run_freerouting(
            _rt(tmp_path, "exec sleep 0.5"),
            tmp_path / "b.dsn",
            tmp_path / "b.ses",
            timeout_s=20,
            passes=3,
            workdir=tmp_path / "w",
        )
    )
    await spy.pid("sleep")
    assert heavy.holder() == "freerouting"
    assert (await run).returncode == 0
    assert heavy.holder() is None


async def test_freerouting_gate_timeout_is_timed_out_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate held by a parity DRC: the run waits min(HEAVY_WAIT_S, timeout_s), then
    returns timed out without starting a process (the fill reports its timeout)."""
    _no_unshare(monkeypatch)
    spy = _Spy(monkeypatch)
    monkeypatch.setattr(fr, "HEAVY_WAIT_S", 0.2)
    async with heavy.heavy_slot("parity", wait_s=1):
        run = await run_freerouting(
            _rt(tmp_path, "exit 0"),
            tmp_path / "b.dsn",
            tmp_path / "b.ses",
            timeout_s=20,
            passes=3,
            workdir=tmp_path / "w",
        )
    assert spy.proc is None
    assert run.returncode == -1 and run.timed_out is True and run.started is False
    assert 0.15 <= run.seconds < 2
    assert "heavy-process gate (parity still running)" in run.log_tail


def test_fill_timeout_why_for_a_run_that_never_started() -> None:
    """A gate/slot timeout is not reported as "did not finish within timeout_s" (which
    would tell the agent to raise timeout_s)."""
    from copperchat.kicad_tools.board.fill import timeout_why

    ran = fr.FreeroutingRun(returncode=-9, seconds=300, log_tail="", timed_out=True)
    waited = fr.FreeroutingRun(
        returncode=-1, seconds=120, log_tail="", timed_out=True, started=False
    )
    assert timeout_why(ran, 300) == "Freerouting did not finish within 300 s"
    assert timeout_why(waited, 300).startswith("Freerouting did not start")
