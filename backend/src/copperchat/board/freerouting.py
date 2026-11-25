"""Sandboxed Freerouting runner (SP4 P6a, B-C13).

Freerouting 2.5.0 (GPL-3.0, an unmodified JAR run as a separate process) on the pinned
Temurin 25 JRE, both installed by `scripts/fetch-freerouting.sh` under
`Settings.freerouting_home`. The JVM runs headless with:

- resource limits set by util-linux `prlimit` *before* `exec` (never `preexec_fn`, which
  is unsafe with threads in the server process): CPU seconds `cpu_limit_s(timeout_s)`,
  256 open files, 512 MiB largest written file, no address-space limit (the JVM
  reserves address space far above its heap);
- no network: `unshare -r -n` when user namespaces are permitted (not under Docker's
  default seccomp profile), and always: Freerouting's own switches (telemetry, analytics,
  API and MCP servers off — on the command line and in a pre-seeded settings file); an
  empty `jdk.net.hosts.file`, so the JVM resolves no host name and sends no DNS query —
  Freerouting 2.5.0 starts an unconditional version check against api.github.com that
  no setting switches off, and it now fails before a socket exists; Java proxy
  properties naming an unresolvable host, so a proxied request fails the same way;
- its own process group, killed on timeout and on cancellation (turn stop).
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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from copperchat.config import Settings
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


_RUNTIME_CACHE: dict[tuple[Path, int, int, str | None], FreeroutingRuntime | None] = {}


def find_runtime(settings: Settings) -> FreeroutingRuntime | None:
    """The installed runtime, or None when switched off, incomplete or the wrong Java."""
    if not settings.freerouting:
        return None
    home = settings.freerouting_home
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
    if key not in _RUNTIME_CACHE:
        _RUNTIME_CACHE[key] = _probe(java, jar, prlimit)
    return _RUNTIME_CACHE[key]


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
def network_namespace_available() -> bool:
    """Whether `unshare -r -n` works here (not under Docker's default seccomp profile)."""
    unshare = shutil.which("unshare")
    if unshare is None:
        return False
    try:
        done = subprocess.run(
            [unshare, "-r", "-n", "--", "true"],
            capture_output=True,
            timeout=UNSHARE_PROBE_TIMEOUT_S,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def build_command(
    rt: FreeroutingRuntime,
    dsn: Path,
    ses: Path,
    *,
    passes: int,
    workdir: Path,
    extra_args: Sequence[str],
    unshare: bool,
    timeout_s: float,
    proxy_port: int = PROXY_PORT,
) -> list[str]:
    """The full argv (pure): `prlimit … -- [unshare -r -n --] java … -jar …`."""
    cpu = cpu_limit_s(timeout_s)
    cmd: list[str] = []
    if rt.prlimit is not None:
        cmd += [
            str(rt.prlimit),
            f"--cpu={cpu}:{cpu + 5}",
            f"--nofile={NOFILE}:{NOFILE}",
            f"--fsize={FSIZE_BYTES}:{FSIZE_BYTES}",
            "--",
        ]
    if unshare:
        cmd += ["unshare", "-r", "-n", "--"]
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
        "--gui.enabled=false",
        *NO_NETWORK_OPTIONS,
        *extra_args,
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
    ):
        resource.prlimit(pid, res, value)


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
    SIGXCPU/SIGKILL we did not send (the CPU limit). A cancellation kills the process
    group, reaps it and re-raises."""
    timeout_s = clamp_timeout(timeout_s)
    passes = clamp_passes(passes)
    workdir.mkdir(parents=True, exist_ok=True)
    seed_settings(workdir)
    (workdir / HOSTS_FILE).write_bytes(b"")
    log_path = workdir / "freerouting.log"
    cmd = build_command(
        rt,
        dsn,
        ses,
        passes=passes,
        workdir=workdir,
        extra_args=extra_args,
        unshare=network_namespace_available(),
        timeout_s=timeout_s,
        proxy_port=proxy_port,
    )
    env = {"HOME": str(workdir), "PATH": "/usr/bin:/bin", "LANG": "C"}
    start = time.monotonic()
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
            await asyncio.wait_for(proc.wait(), timeout_s)
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
