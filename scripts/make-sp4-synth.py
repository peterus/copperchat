#!/usr/bin/env python3
"""Writes the `sp4_synth` board-phase fixture with KiCad itself (never by hand).

Runs backend/tests/kicad/board/test_sp4_synth_build.py: it starts from KiCad's empty
project (template `.kicad_pro`, a schematic created by a standalone api-server, no
board), builds the circuit of `sp4_synth_steps.py` through CopperChat's schematic tools
on a real headless KiCad, and copies the KiCad-saved `sp4_synth.kicad_pro`,
`sp4_synth.kicad_sch` and `power.kicad_sch` verbatim to `<FIXTURES_DIR>/sp4_synth/`.
The summary of KiCad's netlist and ERC report is compared with
backend/tests/kicad/board/goldens/sp4_synth_summary.json; `UPDATE_GOLDENS=1` rewrites
the golden instead.

Accepted ERC items: none (0 errors, 0 warnings; the test asserts it).

Run inside the KiCad base image with the backend environment (`uv`), like the CI kicad
job:
    [UPDATE_GOLDENS=1] python3 scripts/make-sp4-synth.py [FIXTURES_DIR]
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = ROOT / "backend/tests/fixtures/projects"


def main() -> int:
    dest = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_DEST
    env = {**os.environ, "COPPERCHAT_SP4_SYNTH_OUT": str(dest), "COPPERCHAT_REQUIRE_KICAD": "1"}
    cmd = ["uv", "run", "pytest", "-q", "-m", "kicad", "tests/kicad/board/test_sp4_synth_build.py"]
    done = subprocess.run(cmd, cwd=ROOT / "backend", env=env, check=False, timeout=1800)
    if done.returncode == 0:
        print(f"sp4_synth written to {dest / 'sp4_synth'}")
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
