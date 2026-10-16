from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Protocol

from copperchat.projects import Project


class ServerLauncher(Protocol):
    async def start(self, project: Project, socket_path: Path) -> asyncio.subprocess.Process: ...


class KicadCliLauncher:
    """Starts `kicad-cli api-server --socket <path> <project.kicad_pro>` (spike 1C, Q2).

    The server only preloads the project; KicadSession opens the schematic and the
    board over the API once the socket answers. `config_home` isolates KiCad's
    settings (kicad-cli writes `<config>/kicad/<version>/*.json` on first run).
    """

    def __init__(self, kicad_cli: str, log_dir: Path, config_home: Path | None = None) -> None:
        self._kicad_cli = kicad_cli
        self._log_dir = log_dir
        self._config_home = config_home

    async def start(self, project: Project, socket_path: Path) -> asyncio.subprocess.Process:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / f"api-server-{project.id}.log"
        env = {**os.environ, "LANG": "C", "LC_ALL": "C"}
        if self._config_home is not None:
            self._config_home.mkdir(parents=True, exist_ok=True)
            env["KICAD_CONFIG_HOME"] = str(self._config_home)
        with log_path.open("ab") as log:
            return await asyncio.create_subprocess_exec(
                self._kicad_cli,
                "api-server",
                "--socket",
                str(socket_path),
                str(project.pro_file),
                cwd=project.dir,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
