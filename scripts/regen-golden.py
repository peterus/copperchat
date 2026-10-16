#!/usr/bin/env python3
"""Regenerates golden tool JSON + recorded kicad-cli reports from a real api-server.

Run inside the KiCad base image:  cd backend && uv run python ../scripts/regen-golden.py
Review the git diff before committing - goldens are the expected tool output.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from copperchat.config import Settings
from copperchat.kicad_session import DocKind, KicadSessionManager
from copperchat.kicad_tools import checks, pcb, project, schematic
from copperchat.projects import discover_projects
from copperchat.tools.spec import ToolContext

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "backend" / "tests" / "fixtures"
CASES: list[tuple[str, Any, dict[str, Any]]] = [
    ("project_info", project.project_info, {}),
    ("sch_list_symbols_R", schematic.sch_list_symbols, {"filter": "R*"}),
    ("sch_get_symbol_U1", schematic.sch_get_symbol, {"ref": "U1"}),
    ("sch_get_net_GND", schematic.sch_get_net, {"name": "GND"}),
    ("pcb_list_footprints_R", pcb.pcb_list_footprints, {"filter": "R*"}),
    ("pcb_get_footprint_U1", pcb.pcb_get_footprint, {"ref": "U1"}),
    ("pcb_get_net_GND", pcb.pcb_get_net, {"name": "GND"}),
]
# Recorded kicad-cli reports for the parser unit tests: (file, fixture, stem, doc).
REPORTS: list[tuple[str, str, str, DocKind]] = [
    ("drc.json", "ecc83", "ecc83-pp", "pcb"),
    ("erc.json", "complex_hierarchy", "complex_hierarchy", "sch"),
]


def dump(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


async def with_session(tmp: Path, fixture: str, stem: str, fn: Any) -> None:
    root = tmp / fixture / "projects"
    shutil.copytree(FIX / "projects" / fixture, root / fixture)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        projects_root=root,
        data_dir=tmp / fixture / "data",
        dev_fake_agent=True,
        kicad_cli=os.environ.get("KICAD_CLI") or "kicad-cli",
        kicad_api_timeout_s=60.0,
    )
    proj = next(p for p in discover_projects(root) if p.name == stem)
    manager = KicadSessionManager(settings)
    try:
        session = await manager.open(proj)
        await fn(ToolContext(project=proj, session=session, settings=settings))
    finally:
        await manager.close_all()


async def main() -> None:
    golden = FIX / "golden"
    golden.mkdir(exist_ok=True)
    recorded = FIX / "kicad-cli"
    recorded.mkdir(exist_ok=True)

    async def tool_goldens(ctx: ToolContext) -> None:
        for name, fn, args in CASES:
            data = (await fn(ctx, args)).data or {}
            data.pop("kicadVersion", None)
            dump(golden / f"{name}.json", data)

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        await with_session(tmp, "ecc83", "ecc83-pp", tool_goldens)
        for file, fixture, stem, doc in REPORTS:

            async def record(ctx: ToolContext, file: str = file, doc: DocKind = doc) -> None:
                raw = await checks.run_kicad_check(ctx.session, doc, ctx.settings)
                dump(recorded / file, json.loads(raw))

            await with_session(tmp / file, fixture, stem, record)


if __name__ == "__main__":
    asyncio.run(main())
