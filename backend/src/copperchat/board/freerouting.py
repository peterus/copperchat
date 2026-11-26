"""Sandboxed Freerouting runner (SP4 P6a, B-C13).

Freerouting 2.5.0 (GPL-3.0, an unmodified JAR run as a separate process) on the pinned
Temurin 25 JRE, both installed by `scripts/fetch-freerouting.sh` under
`Settings.freerouting_home`. The JVM runs headless with:

- resource limits set by util-linux `prlimit` *before* `exec` (never `preexec_fn`, which
  is unsafe with threads in the server process): CPU seconds `cpu_limit_s(timeout_s)`,
  256 open files, 512 MiB largest written file, no core dumps (and the JVM writes
  none on a crash), no address-space limit (the JVM reserves address space far above
  its heap);
- no network: `unshare -r -n` when user namespaces are permitted (not under Docker's
  default seccomp profile), and always: Freerouting's own switches (telemetry, analytics,
  API and MCP servers off — on the command line and in a pre-seeded settings file); an
  empty `jdk.net.hosts.file`, so the JVM resolves no host name and sends no DNS query —
  Freerouting 2.5.0 starts an unconditional version check against api.github.com that
  no setting switches off, and it now fails before a socket exists; Java proxy
  properties naming an unresolvable host, so a proxied request fails the same way;
- its own process group, killed on timeout and on cancellation (turn stop);
- at most `MAX_CONCURRENT_RUNS` JVMs at once per event loop; waiting for a slot counts
  against the run's timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import math
import os
import resource
import shutil
import signal
import subprocess
import time
import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from copperchat.board.heavy import HEAVY_WAIT_S, heavy_slot
from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session.session import _uncancellable

log = logging.getLogger(__name__)

FREEROUTING_VERSION = "2.5.0"
JAR_NAME = f"freerouting-{FREEROUTING_VERSION}.jar"
JAVA_VERSION_MARK = 'version "25.'

JAVA_OPTS = (
    "-Xmx1g",
    "-XX:MaxMetaspaceSize=256m",
    "-XX:ActiveProcessorCount=2",
    "-Djava.awt.headless=true",
    # no /tmp/hsperfdata_<user> files; a JVM out of heap exits instead of limping on
    "-XX:-UsePerfData",
    "-XX:+ExitOnOutOfMemoryError",
    # no core file on a JVM crash (RLIMIT_CORE is 0 as well)
    "-XX:-CreateCoredumpOnCrash",
)
TIMEOUT_DEFAULT_S = 300
TIMEOUT_MAX_S = 600
CPU_MARGIN_S = 30
NOFILE = 256
FSIZE_BYTES = 512 * 1024 * 1024
PASSES_MIN = 1
PASSES_MAX = 100
LOG_TAIL_BYTES = 4096
JAVA_VERSION_TIMEOUT_S = 15.0
# a failed `java -version` probe is retried after this long; a successful one is kept
FAILURE_TTL_S = 60.0
MAX_CONCURRENT_RUNS = 2  # effectively 1 since R6: every run also holds the heavy gate
UNSHARE_PROBE_TIMEOUT_S = 5.0
# Freerouting 2.5.0 settings that open connections when on (names checked against the
# JAR: an unknown name makes Freerouting log "Unknown settings property: <name>").
NO_NETWORK_OPTIONS: tuple[str, ...] = (
    "--profile.allow_telemetry=false",
    "--profile.allow_contact=false",
    "--usage_and_diagnostic_data.disable_analytics=true",
    "--api_server.enabled=false",
    "--mcp_server.enabled=false",
)
# Option names `extra_args` may not set: the switches above and the GUI switch.
LOCKED_OPTION_KEYS: tuple[str, ...] = (
    *(opt.split("=", 1)[0] for opt in NO_NETWORK_OPTIONS),
    "--gui.enabled",
)
# The same switches in the settings file Freerouting reads first and re-saves on start
# ($HOME/.config/freerouting/freerouting.json; HOME is the work dir). Freerouting keeps a
# file that carries `version` and a valid profile id.
SETTINGS_FILE = Path(".config/freerouting/freerouting.json")
SETTINGS_SEED: dict[str, object] = {
    "version": FREEROUTING_VERSION,
    "profile": {
        "id": "00000000-0000-0000-0000-000000000000",
        "email": "",
        "allow_telemetry": False,
        "allow_contact": False,
    },
    "usage_and_diagnostic_data": {"disable_analytics": True},
    "api_server": {"enabled": False},
    "mcp_server": {"enabled": False},
    "gui": {"enabled": False},
}
# RFC 2606 reserved name: never resolves (and with HOSTS_FILE it is not even looked up)
PROXY_HOST = "proxy.invalid"
PROXY_PORT = 9
# empty hosts file in the work dir: the JVM's only name source (no DNS)
HOSTS_FILE = ".hosts-empty"


def proxy_blackhole(port: int = PROXY_PORT) -> tuple[str, ...]:
    return (
        f"-Dhttp.proxyHost={PROXY_HOST}",
        f"-Dhttp.proxyPort={port}",
        f"-Dhttps.proxyHost={PROXY_HOST}",
        f"-Dhttps.proxyPort={port}",
        f"-DsocksProxyHost={PROXY_HOST}",
        f"-DsocksProxyPort={port}",
        "-Djava.net.useSystemProxies=false",
    )


PROXY_BLACKHOLE = proxy_blackhole()

# Documented fallback for an image without util-linux `prlimit`: start `java` directly
# and set the limits with `resource.prlimit` right after the spawn (a race of a few ms).
# Off: without `prlimit` the runtime counts as unavailable.
ALLOW_PRLIMIT_FALLBACK = False


def cpu_limit_s(timeout_s: float) -> int:
    """CPU seconds: two active processors for the whole wall timeout plus a margin, so
    our own wall-clock timeout normally fires first."""
    return math.ceil(2 * timeout_s) + CPU_MARGIN_S


@dataclass(frozen=True)
class FreeroutingRuntime:
    java: Path
    jar: Path
    version: str
    prlimit: Path | None = None  # None only with ALLOW_PRLIMIT_FALLBACK


@dataclass(frozen=True)
class FreeroutingRun:
    returncode: int
    seconds: float
    log_tail: str
    timed_out: bool


# key -> (runtime or None, monotonic expiry; None = kept until the key changes)
_RUNTIME_CACHE: dict[
    tuple[Path, int, int, str | None], tuple[FreeroutingRuntime | None, float | None]
] = {}
_monotonic = time.monotonic  # patched by tests


def find_runtime(settings: Settings) -> FreeroutingRuntime | None:
    """The installed runtime, or None when switched off, incomplete or the wrong Java.

    Synchronous (B-C13): async callers should call it via `asyncio.to_thread`, since it
    may run `java -version` (up to `JAVA_VERSION_TIMEOUT_S`) once per install; a failed
    probe is retried after `FAILURE_TTL_S`."""
    if not settings.freerouting:
        return None
    # absolute: the probe runs in our cwd, but every run uses cwd=workdir
    home = settings.freerouting_home.absolute()
    java = home / "jre" / "bin" / "java"
    jar = home / JAR_NAME
    try:
        java_st = java.stat()
        jar_st = jar.stat()
    except OSError:
        return None
    if not (java.is_file() and os.access(java, os.X_OK) and jar.is_file()):
        return None
    if not (home / ".manifest.sha256").is_file():
        return None
    prlimit = shutil.which("prlimit")
    if prlimit is None and not ALLOW_PRLIMIT_FALLBACK:
        log.warning("freerouting: util-linux prlimit not on PATH; runtime unavailable")
        return None
    key = (home, java_st.st_mtime_ns, jar_st.st_mtime_ns, prlimit)
    cached = _RUNTIME_CACHE.get(key)
    if cached is not None and (cached[1] is None or _monotonic() < cached[1]):
        return cached[0]
    rt = _probe(java, jar, prlimit)
    _RUNTIME_CACHE[key] = (rt, None if rt is not None else _monotonic() + FAILURE_TTL_S)
    return rt


def _probe(java: Path, jar: Path, prlimit: str | None) -> FreeroutingRuntime | None:
    try:
        out = subprocess.run(
            [str(java), "-version"],
            capture_output=True,
            timeout=JAVA_VERSION_TIMEOUT_S,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("freerouting: %s -version failed: %s", java, exc)
        return None
    text = (out.stdout + out.stderr).decode("utf-8", "replace")
    if out.returncode != 0 or JAVA_VERSION_MARK not in text:
        log.warning("freerouting: %s is not Java 25: %s", java, text[:200])
        return None
    return FreeroutingRuntime(
        java=java,
        jar=jar,
        version=FREEROUTING_VERSION,
        prlimit=Path(prlimit) if prlimit else None,
    )


@functools.cache
def unshare_path() -> Path | None:
    """The absolute path of an `unshare` for which `unshare -r -n` works here (not under
    Docker's default seccomp profile), else None. Probed once; blocking (≤ 5 s)."""
    unshare = shutil.which("unshare")
    if unshare is None:
        return None
    try:
        done = subprocess.run(
            [unshare, "-r", "-n", "--", "true"],
            capture_output=True,
            timeout=UNSHARE_PROBE_TIMEOUT_S,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(unshare).absolute() if done.returncode == 0 else None


def network_namespace_available() -> bool:
    """Whether the runner isolates the JVM in its own network namespace."""
    return unshare_path() is not None


def build_command(
    rt: FreeroutingRuntime,
    dsn: Path,
    ses: Path,
    *,
    passes: int,
    workdir: Path,
    extra_args: Sequence[str],
    unshare: Path | None,
    timeout_s: float,
    proxy_port: int = PROXY_PORT,
) -> list[str]:
    """The full argv (pure): `prlimit … -- [<unshare> -r -n --] java … -jar …`.

    `unshare` is the probed absolute path (None: no network namespace). The no-network
    switches come after `extra_args` (Freerouting keeps the last value), and an extra
    argument naming one of them raises ValueError."""
    for arg in extra_args:
        if arg.startswith(LOCKED_OPTION_KEYS):
            raise ValueError(f"extra_args may not set {arg.split('=', 1)[0]!r}: {arg!r}")
    cpu = cpu_limit_s(timeout_s)
    cmd: list[str] = []
    if rt.prlimit is not None:
        cmd += [
            str(rt.prlimit),
            f"--cpu={cpu}:{cpu + 5}",
            f"--nofile={NOFILE}:{NOFILE}",
            f"--fsize={FSIZE_BYTES}:{FSIZE_BYTES}",
            "--core=0:0",
            "--",
        ]
    if unshare is not None:
        cmd += [str(unshare), "-r", "-n", "--"]
    cmd += [
        str(rt.java),
        *JAVA_OPTS,
        *proxy_blackhole(proxy_port),
        f"-Djdk.net.hosts.file={workdir / HOSTS_FILE}",
        f"-Duser.home={workdir}",
        f"-Djava.io.tmpdir={workdir}",
        "-jar",
        str(rt.jar),
        "-de",
        str(dsn),
        "-do",
        str(ses),
        f"--router.autorouter.max_passes={passes}",
        *extra_args,
        "--gui.enabled=false",
        *NO_NETWORK_OPTIONS,
    ]
    return cmd


def clamp_timeout(timeout_s: float) -> float:
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError(f"timeout_s must be a positive number, got {timeout_s!r}")
    return min(float(timeout_s), float(TIMEOUT_MAX_S))


def clamp_passes(passes: int) -> int:
    return max(PASSES_MIN, min(int(passes), PASSES_MAX))


def seed_settings(workdir: Path) -> Path:
    """Write Freerouting's settings file with every network feature off."""
    path = workdir / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(SETTINGS_SEED, indent=2), encoding="utf-8")
    return path


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _fallback_limits(pid: int, timeout_s: float) -> None:
    cpu = cpu_limit_s(timeout_s)
    for res, value in (
        (resource.RLIMIT_CPU, (cpu, cpu + 5)),
        (resource.RLIMIT_NOFILE, (NOFILE, NOFILE)),
        (resource.RLIMIT_FSIZE, (FSIZE_BYTES, FSIZE_BYTES)),
        (resource.RLIMIT_CORE, (0, 0)),
    ):
        resource.prlimit(pid, res, value)


_SLOTS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _slots() -> asyncio.Semaphore:
    """The running loop's run slots (a Semaphore is bound to one loop)."""
    loop = asyncio.get_running_loop()
    slots = _SLOTS.get(loop)
    if slots is None:
        slots = _SLOTS[loop] = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
    return slots


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL_BYTES))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


async def run_freerouting(
    rt: FreeroutingRuntime,
    dsn: Path,
    ses: Path,
    *,
    timeout_s: float,
    passes: int,
    workdir: Path,
    extra_args: Sequence[str] = (),
    proxy_port: int = PROXY_PORT,
) -> FreeroutingRun:
    """Route `dsn` into `ses`; returns on any exit code (the caller decides).

    `timed_out` is True when our wall-clock timeout fired or the process died by a
    SIGXCPU/SIGKILL we did not send (the CPU limit). Each run holds the heavy-process gate
    (R6, `heavy.heavy_slot`; its wait is bounded by min(HEAVY_WAIT_S, timeout_s)) and one of
    `MAX_CONCURRENT_RUNS` slots; both waits count against `timeout_s` (not in time:
    returncode -1, timed out, no process started). A cancellation kills the process group,
    reaps it and re-raises."""
    timeout_s = clamp_timeout(timeout_s)
    passes = clamp_passes(passes)
    # the JVM runs with cwd=workdir: relative paths must mean what they mean to us
    dsn, ses, workdir = dsn.absolute(), ses.absolute(), workdir.absolute()
    start = time.monotonic()
    deadline = start + timeout_s
    unshare = await asyncio.to_thread(unshare_path)
    cmd = build_command(
        rt,
        dsn,
        ses,
        passes=passes,
        workdir=workdir,
        extra_args=extra_args,
        unshare=unshare,
        timeout_s=timeout_s,
        proxy_port=proxy_port,
    )
    # R6: the heavy-process gate (one JVM or parity DRC at a time, process-wide), its wait
    # bounded by min(HEAVY_WAIT_S, timeout_s) and counted against timeout_s
    async with contextlib.AsyncExitStack() as stack:
        try:
            await stack.enter_async_context(
                heavy_slot("freerouting", wait_s=min(HEAVY_WAIT_S, deadline - time.monotonic()))
            )
        except StructuredToolError as exc:
            return FreeroutingRun(
                returncode=-1,
                seconds=time.monotonic() - start,
                log_tail="timed out waiting for the heavy-process gate "
                f"({(exc.details or {}).get('waiting_for')} still running)",
                timed_out=True,
            )
        slots = _slots()
        try:
            await asyncio.wait_for(slots.acquire(), max(0.0, deadline - time.monotonic()))
        except TimeoutError:
            return FreeroutingRun(
                returncode=-1,
                seconds=time.monotonic() - start,
                log_tail="timed out waiting for a free Freerouting slot "
                f"(MAX_CONCURRENT_RUNS={MAX_CONCURRENT_RUNS})",
                timed_out=True,
            )
        try:
            # a stale .ses from an earlier run must not pass for this run's result
            ses.unlink(missing_ok=True)
            return await _run(rt, cmd, workdir, timeout_s, deadline, start)
        finally:
            slots.release()


async def _run(
    rt: FreeroutingRuntime,
    cmd: list[str],
    workdir: Path,
    timeout_s: float,
    deadline: float,
    start: float,
) -> FreeroutingRun:
    """One JVM run holding a slot; `timeout_s` sets the CPU limit, `deadline` the wait."""
    workdir.mkdir(parents=True, exist_ok=True)
    seed_settings(workdir)
    (workdir / HOSTS_FILE).write_bytes(b"")
    log_path = workdir / "freerouting.log"
    env = {"HOME": str(workdir), "PATH": "/usr/bin:/bin", "LANG": "C"}
    with log_path.open("wb") as log_fh:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=log_fh,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    killed = False
    try:
        if rt.prlimit is None:
            _fallback_limits(proc.pid, timeout_s)
        try:
            await asyncio.wait_for(proc.wait(), max(0.0, deadline - time.monotonic()))
        except TimeoutError:
            killed = True
            _kill_group(proc.pid)
            await proc.wait()
    except BaseException:
        # cancellation (turn stop) or a failed fallback limit: never leave the JVM running
        _kill_group(proc.pid)
        await _uncancellable(proc.wait())
        raise
    returncode = proc.returncode if proc.returncode is not None else -signal.SIGKILL
    if returncode in (-signal.SIGXCPU, -signal.SIGKILL):
        killed = True
    return FreeroutingRun(
        returncode=returncode,
        seconds=time.monotonic() - start,
        log_tail=_tail(log_path),
        timed_out=killed,
    )
