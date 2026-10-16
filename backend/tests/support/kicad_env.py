"""Detects a usable KiCad 11 `kicad-cli api-server` for @pytest.mark.kicad tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# Read once at import (collection time): the autouse `_clean_env` fixture removes
# KICAD_CLI from the environment while each test runs.
KICAD_CLI = os.environ.get("KICAD_CLI") or "kicad-cli"


def api_server_available(kicad_cli: str = KICAD_CLI) -> bool:
    if shutil.which(kicad_cli) is None:
        return False
    try:
        result = subprocess.run(
            [kicad_cli, "api-server", "--help"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # KiCad 10's kicad-cli prints its generic usage (exit 0) for unknown commands,
    # so look for the api-server's own option.
    return result.returncode == 0 and b"--socket" in result.stdout + result.stderr


def kicad_demo_dir(kicad_cli: str = KICAD_CLI) -> Path | None:
    exe = shutil.which(kicad_cli)
    if exe is None:
        return None
    candidate = Path(exe).resolve().parent.parent / "share" / "kicad" / "demos"
    return candidate if candidate.is_dir() else None
