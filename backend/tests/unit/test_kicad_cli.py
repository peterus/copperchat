from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from copperchat.config import Settings
from copperchat.kicad_tools.cli import CliResult, run_kicad_cli


def _fake(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-kicad-cli"
    script.write_text(f"#!{sys.executable}\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


async def test_argv_env_cwd_and_output(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    body = (
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'cfg': os.environ.get('KICAD_CONFIG_HOME'),"
        " 'lang': os.environ.get('LC_ALL'), 'cwd': os.getcwd()}))\n"
        "sys.stderr.write('err\\xe9')\n"
        "sys.exit(3)\n"
    )
    settings = make_settings(kicad_cli=_fake(tmp_path, body))
    work = tmp_path / "work"
    work.mkdir()
    res = await run_kicad_cli(settings, ["fp", "export", "--fp=-x", "a b"], timeout=10, cwd=work)
    assert isinstance(res, CliResult)
    assert res.returncode == 3
    seen = json.loads(res.stdout)
    assert seen["argv"] == ["fp", "export", "--fp=-x", "a b"]
    assert seen["cfg"] == str(settings.data_dir / "kicad-config")
    assert seen["lang"] == "C"
    assert Path(seen["cwd"]).resolve() == work.resolve()
    assert res.stderr.startswith("err")


async def test_timeout_kills_and_reaps(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    pidfile = tmp_path / "pid"
    body = f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    settings = make_settings(kicad_cli=_fake(tmp_path, body))
    task = asyncio.create_task(run_kicad_cli(settings, ["x"], timeout=3.0))
    # Poll while the runner is still waiting, so a slow interpreter start on CI
    # cannot make the timeout fire before the child wrote its pid.
    for _ in range(50):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.05)
    with pytest.raises(TimeoutError):
        await task
    assert pidfile.exists() and pidfile.read_text(), "fake kicad-cli never started"
    pid = int(pidfile.read_text())
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)  # already reaped by us -> not our child any more
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_cancel_kills_and_reaps(
    tmp_path: Path, make_settings: Callable[..., Settings]
) -> None:
    pidfile = tmp_path / "pid"
    body = f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
    settings = make_settings(kicad_cli=_fake(tmp_path, body))
    task = asyncio.create_task(run_kicad_cli(settings, ["x"], timeout=30))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)
