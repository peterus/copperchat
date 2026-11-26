"""The end-to-end board flow on `sp4_synth` (SP4 P9a): one api-server and one full flow
per test module, shared by the module's tests (they run on the module event loop)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from copperchat.board.freerouting import FreeroutingRuntime
from copperchat.config import Settings
from copperchat.projects import discover_projects
from tests.kicad.board.conftest import freerouting_rt as freerouting_rt
from tests.kicad_e2e.flow import SYNTH_ANSWERS, FlowResult, Rig, open_rig, run_flow
from tests.support.kicad_env import KICAD_CLI
from tests.support.kicad_project import copy_fixture


@dataclass
class SynthFlow:
    rig: Rig
    result: FlowResult


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def synth_flow(
    tmp_path_factory: pytest.TempPathFactory, freerouting_rt: FreeroutingRuntime
) -> AsyncIterator[SynthFlow]:
    """`run_flow(SYNTH_ANSWERS)` on a fresh copy of `sp4_synth` (schematic only), board
    tools and Freerouting on; the rig stays open for the module's tests."""
    tmp: Path = tmp_path_factory.mktemp("synth-e2e")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        projects_root=tmp / "projects",
        data_dir=tmp / "data",
        admin_password="test-password",
        claude_code_oauth_token="test-token",
        cookie_secure=False,
        frontend_dist=tmp / "no-frontend",
        kicad_cli=KICAD_CLI,
        kicad_api_timeout_s=30.0,
        kicad_start_timeout_s=60.0,
        board_tools=True,
    )
    settings.projects_root.mkdir(parents=True)
    copy_fixture("sp4_synth", settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "sp4_synth")
    async with open_rig(settings, project, tmp / "e2e.db") as rig:
        yield SynthFlow(rig, await run_flow(rig, SYNTH_ANSWERS))
