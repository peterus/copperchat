"""Opt-in: the real CLI enforces the researcher guard also for Read/Glob inside cwd."""

from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, UserMessage

from copperchat.research.runner import researcher_options, sdk_query
from tests.unit.phase8.conftest import make_project

from .conftest import live, live_settings

pytestmark = live
SECRET = "COPPERCHAT-SECRET-7731"


@pytest.mark.timeout(300)
async def test_read_inside_project_dir_is_denied(tmp_path: Path) -> None:
    project = make_project(tmp_path / "proj")
    (project.dir / "secret.txt").write_text(SECRET)
    (project.dir / "docs/parts/X").mkdir(parents=True)
    (project.dir / "docs/parts/X/notes.md").write_text("allowed-notes-ok")
    opts = researcher_options(project, live_settings(tmp_path))
    opts.max_turns = 6
    prompt = (
        f"Use the Read tool on {project.dir / 'secret.txt'}, then Glob pattern '*' with path "
        f"{project.dir}, then Read {project.dir / 'docs/parts/X/notes.md'}. Report exactly what "
        "each tool returned. Do not use any other tools."
    )
    texts: list[str] = []
    results: list[Any] = []
    async for msg in sdk_query(prompt, opts):
        if isinstance(msg, UserMessage) and not isinstance(msg.content, str):
            results += [b for b in msg.content if isinstance(b, ToolResultBlock)]
        elif isinstance(msg, AssistantMessage):
            texts.append(str(msg.content))
        elif isinstance(msg, ResultMessage):
            texts.append(msg.result or "")
    blob = " ".join(str(r.content) for r in results) + " ".join(texts)
    assert SECRET not in blob and "secret.txt" not in " ".join(
        str(r.content) for r in results if not r.is_error
    )
    assert "allowed-notes-ok" in blob  # allowed reads still work
