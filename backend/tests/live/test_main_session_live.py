"""The real CLI offers the main agent exactly Skill + our MCP tools (incl. research_part)."""

from pathlib import Path

import pytest
from claude_agent_sdk import SystemMessage, query

from copperchat.agent.sdk_backend import build_options
from copperchat.agent.sdk_tools import build_sdk_tools
from copperchat.db import Database
from copperchat.skills.assembly import assemble_skills
from copperchat.tools import registry
from tests.unit.phase8.conftest import make_project

from .conftest import live, live_settings

pytestmark = live


@pytest.mark.timeout(300)
async def test_init_lists_skills_and_research_part_only(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    project = make_project(tmp_path / "proj")
    db = await Database.open(tmp_path / "t.db")
    try:
        bundle = await assemble_skills(settings, project, db)
    finally:
        await db.close()
    specs = registry.all_specs()
    opts = build_options(
        settings=settings,
        project=project,
        mcp_server=build_sdk_tools(specs, lambda: None),  # type: ignore[arg-type,return-value]
        tool_names=[s.name for s in specs],
        resume_session_id=None,
        fork_session=False,
        skills=bundle,
    )
    opts.max_turns = 1

    async def prompt():  # type: ignore[no-untyped-def]
        yield {
            "type": "user",
            "message": {"role": "user", "content": "Reply with OK."},
            "parent_tool_use_id": None,
        }

    init = None
    async for msg in query(prompt=prompt(), options=opts):
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            init = msg.data
            break
    assert init is not None, "no init message"
    tools = set(init.get("tools", []))
    assert "Skill" in tools and "mcp__copperchat__research_part" in tools
    builtins = {t for t in tools if not t.startswith("mcp__")}
    assert builtins == {"Skill"}, builtins
    skills = set(init.get("skills", []))
    assert {"copperchat:copperchat-tools", "copperchat:kicad-review-discipline"} <= skills
    assert not any("part-notes" in s for s in skills)
