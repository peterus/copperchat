from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Protocol

from copperchat.config import Settings
from copperchat.projects import Project

# KiCad 11 (master) keeps its settings under <KICAD_CONFIG_HOME>/10.99/.
KICAD_SETTINGS_VERSION = "10.99"
_LIBRARY_TABLES = ("sym-lib-table", "fp-lib-table")


def kicad_config_home(settings: Settings) -> Path:
    """CopperChat's isolated KiCad config directory (spike 1C, Q6)."""
    return settings.data_dir / "kicad-config"


def kicad_template_dir(kicad_cli: str) -> Path | None:
    """`share/kicad/template` of the KiCad install that `kicad_cli` belongs to."""
    exe = shutil.which(kicad_cli)
    if exe is None:
        return None
    template = Path(exe).resolve().parent.parent / "share" / "kicad" / "template"
    return template if template.is_dir() else None


def seed_library_tables(config_home: Path, kicad_cli: str) -> None:
    """Give an isolated KiCad config the stock global symbol/footprint library tables.

    Without them ERC reports `footprint_link_issues` and DRC parity
    `footprint_symbol_mismatch` for every stock library part (spike 1C, Q6). The
    tables are byte copies of KiCad's own templates - what KiCad itself installs on
    first run. Existing tables are never replaced.
    """
    template = kicad_template_dir(kicad_cli)
    if template is None:
        return
    target = config_home / KICAD_SETTINGS_VERSION
    for name in _LIBRARY_TABLES:
        source = template / name
        if source.is_file() and not (target / name).exists():
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target / name)


def kicad_cli_env(kicad_cli: str, config_home: Path | None) -> dict[str, str]:
    """Environment for every kicad-cli process: C locale, isolated + seeded config."""
    env = {**os.environ, "LANG": "C", "LC_ALL": "C"}
    if config_home is not None:
        config_home.mkdir(parents=True, exist_ok=True)
        seed_library_tables(config_home, kicad_cli)
        env["KICAD_CONFIG_HOME"] = str(config_home)
    return env


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
        env = kicad_cli_env(self._kicad_cli, self._config_home)
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
