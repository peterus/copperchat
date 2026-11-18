"""Generic one-shot `kicad-cli` runner: isolated config, no stdin, kill and reap."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from copperchat.config import Settings
from copperchat.kicad_session.launcher import kicad_cli_env, kicad_config_home


@dataclass(frozen=True)
class CliResult:
    returncode: int
    stdout: str
    stderr: str


async def run_kicad_cli(
    settings: Settings, args: Sequence[str], *, timeout: float, cwd: Path | None = None
) -> CliResult:
    """Runs `settings.kicad_cli <args>` in CopperChat's isolated KiCad config (argv
    list, never a shell). On timeout the process is killed and reaped and
    `TimeoutError` is raised; on cancellation it is killed and reaped too."""
    proc = await asyncio.create_subprocess_exec(
        settings.kicad_cli,
        *args,
        cwd=cwd,
        env=kicad_cli_env(settings.kicad_cli, kicad_config_home(settings)),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        # Reap it before the caller's temp dir goes away; the cancellation still wins.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(proc.wait())
        raise
    return CliResult(
        proc.returncode if proc.returncode is not None else -1,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )
