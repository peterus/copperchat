"""Main-agent PreToolUse hook (sessions C3 Task 6, Review Focus 5, spec §7.5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import HookMatcher, PermissionResultAllow, ToolPermissionContext
from pydantic import SecretStr

from copperchat.agent.guards import DELEGATION_TOOL
from copperchat.agent.sdk_backend import build_options
from copperchat.config import Settings
from copperchat.projects import Project

MCP = {"type": "sdk", "name": "copperchat", "instance": None}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=tmp_path,
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr("tok"),
    )


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "demo"
    d.mkdir()
    return Project(
        "a1b2c3d4e5f6", "demo", "demo/demo.kicad_pro", d, d / "demo.kicad_pro", None, None
    )


def _options(settings: Settings, project: Project, calls: list[str]) -> Any:
    def observe(tool: str, _inp: dict[str, Any], _ctx: ToolPermissionContext) -> None:
        calls.append(tool)

    return build_options(
        settings=settings,
        project=project,
        mcp_server=MCP,  # type: ignore[arg-type]
        tool_names=["research_part", "doc_read"],
        resume_session_id=None,
        fork_session=False,
        on_tool_allowed=observe,
    )


async def _hook(opts: Any, tool: str) -> dict[str, Any]:
    matchers = opts.hooks["PreToolUse"]
    assert len(matchers) == 1 and isinstance(matchers[0], HookMatcher)
    assert matchers[0].matcher is None and len(matchers[0].hooks) == 1
    out: dict[str, Any] = await matchers[0].hooks[0](
        {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"x": 1}},
        "tu-1",
        {"signal": None},
    )
    return out


async def test_hook_denies_out_of_list_tools(settings: Settings, project: Project) -> None:
    opts = _options(settings, project, [])
    for tool in ("Read", "Bash", "mcp__copperchat__sch_connect", "WebFetch"):
        out = await _hook(opts, tool)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", tool
    assert await _hook(opts, "mcp__copperchat__doc_read") == {}
    assert await _hook(opts, "Skill") == {}


async def test_research_call_recorded_once(settings: Settings, project: Project) -> None:
    calls: list[str] = []
    opts = _options(settings, project, calls)
    # the CLI runs the PreToolUse hook and then asks can_use_tool for the same call
    assert await _hook(opts, DELEGATION_TOOL) == {}
    res = await opts.can_use_tool(
        DELEGATION_TOOL, {"mpn": "X"}, ToolPermissionContext(tool_use_id="tu-1")
    )
    assert isinstance(res, PermissionResultAllow)
    assert calls == [DELEGATION_TOOL]


async def test_skills_keep_the_hook(settings: Settings, project: Project, tmp_path: Path) -> None:
    from copperchat.skills.assembly import SkillBundle, apply_skills

    opts = _options(settings, project, [])
    bundle = SkillBundle(plugin_dir=tmp_path, names=["x"], sdk_names=["copperchat:x"])
    merged = apply_skills(opts, bundle)
    assert merged.hooks == opts.hooks
