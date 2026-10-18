#!/usr/bin/env python3
"""Writes the edited-schematic fixtures of the Playwright viewer test with KiCad itself.

Runs the golden scenario tests (backend/tests/kicad/edit/test_scenarios.py): each builds
its reference circuit from the KiCad-created `blank` project through the agent tools, and
the schematic files KiCad saves are copied verbatim (plus a `manifest.json` with the
sheet files and references) to `frontend/e2e/fixtures/edited/<scenario>/`. Nothing is
hand-edited or text-generated.

Run inside the KiCad base image with the backend environment, like the CI kicad job:
    python3 scripts/make-edited-fixtures.py [DEST_DIR]
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = ROOT / "frontend/e2e/fixtures/edited"


def main() -> int:
    dest = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_DEST
    env = {**os.environ, "COPPERCHAT_EDITED_FIXTURES": str(dest), "COPPERCHAT_REQUIRE_KICAD": "1"}
    cmd = ["uv", "run", "pytest", "-q", "tests/kicad/edit/test_scenarios.py"]
    done = subprocess.run(cmd, cwd=ROOT / "backend", env=env, check=False, timeout=1800)
    if done.returncode == 0:
        print(f"edited fixtures written to {dest}")
    return done.returncode


if __name__ == "__main__":
    raise SystemExit(main())
