#!/usr/bin/env bash
# Re-records the board snapshot from the real api-server and rewrites golden PNGs.
# Run inside the KiCad base image; review the PNG diff before committing.
set -euo pipefail
cd "$(dirname "$0")/../backend"
uv run python - <<'PY'
import asyncio, json, os, shutil, tempfile
from pathlib import Path

from copperchat.config import Settings
from copperchat.kicad_session import KicadSessionManager
from copperchat.kicad_tools.pcb import board_snapshot
from copperchat.projects import discover_projects


async def main() -> None:
    fix = Path("tests/fixtures")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "p"
        shutil.copytree(fix / "projects" / "ecc83", root / "ecc83")
        s = Settings(_env_file=None, projects_root=root, data_dir=Path(tmp) / "d",
                     dev_fake_agent=True, kicad_cli=os.environ.get("KICAD_CLI") or "kicad-cli")
        proj = next(p for p in discover_projects(root) if p.name == "ecc83-pp")
        m = KicadSessionManager(s)
        try:
            snap = await (await m.open(proj)).call(board_snapshot)
        finally:
            await m.close_all()
    out = fix / "golden" / "board_snapshot.json"
    out.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n")


asyncio.run(main())
PY
UPDATE_GOLDENS=1 uv run pytest tests/unit/test_agent_render.py -q
find tests/fixtures/golden/png -name '*.actual.png' -delete
