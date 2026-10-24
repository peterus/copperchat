"""Session types as tool profiles (sessions C5 Task 1, spec §8.2, Review Focus 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext
from pydantic import SecretStr

from copperchat.agent import sdk_backend
from copperchat.agent.events import QueuedItem
from copperchat.agent.profiles import (
    DOC_TOOLS,
    PLAN_TOOLS,
    PROFILES,
    REPO_TOOLS,
    SCH_WRITES,
    TODAY,
    profile_specs,
)
from copperchat.agent.sdk_backend import SdkStreamingBackend, system_prompt
from copperchat.agent.sdk_tools import sdk_tool_name
from copperchat.config import Settings
from copperchat.projects import Project
from copperchat.tools import ToolResult, ToolSpec, registry
from tests.support.conversations import conversation_row

SPEC_82 = {
    "requirements": {
        "doc_list",
        "doc_read",
        "doc_write",
        "doc_patch_section",
        "doc_append",
        "research_part",
        "parts_get_notes",
        "lib_search_symbols",
        "lib_get_symbol",
        "lib_search_footprints",
        "project_info",
        "sch_list_symbols",
        "sch_get_symbol",
        "sch_get_net",
        "sch_get_pin",
        "sch_list_items",
        "pcb_list_footprints",
        "pcb_get_footprint",
        "pcb_get_net",
        "render_view",
        "run_erc",
        "run_drc",
        "git_status",
        "git_diff_summary",
        "git_commit",
    }
    | set(REPO_TOOLS),
    "schematic": set(TODAY)
    | {"doc_list", "doc_read", "doc_append", "doc_patch_section"}
    | set(REPO_TOOLS),
    "free": set(TODAY) | set(DOC_TOOLS) | set(REPO_TOOLS),
}
# repo-context 2026-10-21: REPO_TOOLS (repo_list, repo_grep, repo_read) were deliberately
# added to every profile (repo-context spec §5.4).


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "demo"
    d.mkdir()
    return Project(
        "a1b2c3d4e5f6", "demo", "demo/demo.kicad_pro", d, d / "demo.kicad_pro", None, None
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=tmp_path,
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr("tok"),
    )


def test_today_is_the_40_tools_of_spec_81_plus_list_items() -> None:
    assert len(TODAY) == 41  # + sch_list_items (v0.5.0, #3)
    assert len(DOC_TOOLS) == 5


@pytest.mark.parametrize("kind", ["requirements", "schematic", "free"])
def test_profiles_match_spec_82_exactly(kind: str) -> None:
    assert set(PROFILES[kind]) == SPEC_82[kind]


def test_board_profile_is_read_only_plus_doc_append() -> None:
    board = PROFILES["board"]
    assert not board & (SCH_WRITES | PLAN_TOOLS)
    assert board & DOC_TOOLS == {"doc_list", "doc_read", "doc_append"}
    assert {"run_erc", "run_drc", "git_commit", "research_part", "lib_get_symbol"} <= board


def test_free_covers_the_registry_and_every_name_exists() -> None:
    names = {s.name for s in registry.all_specs()}
    assert set(PROFILES["free"]) >= names, sorted(names - PROFILES["free"])
    for kind, tools in PROFILES.items():
        assert tools <= names, (kind, sorted(tools - names))


def _spec(name: str) -> ToolSpec:
    async def handler(_ctx: Any, _args: dict[str, Any]) -> ToolResult:
        return ToolResult(text="ok")

    return ToolSpec(name=name, description="d", input_schema={"type": "object"}, handler=handler)


def test_a_new_registry_tool_appears_in_free_only() -> None:
    specs = [*registry.all_specs(), _spec("brand_new_tool")]
    assert "brand_new_tool" in {s.name for s in profile_specs("free", specs)}
    for kind in ("requirements", "schematic", "board"):
        assert "brand_new_tool" not in {s.name for s in profile_specs(kind, specs)}


def test_unknown_type_fails_closed() -> None:
    with pytest.raises(ValueError):
        profile_specs("chat", registry.all_specs())


def test_profile_specs_keeps_registry_order() -> None:
    specs = registry.all_specs()
    got = [s.name for s in profile_specs("requirements", specs)]
    assert got == [s.name for s in specs if s.name in PROFILES["requirements"]]


# --- the SDK backend per type ---------------------------------------------------------


class _Client:
    """A client that ends the burst at once (only the options matter here)."""

    def __init__(self, options: Any) -> None:
        self.options = options

    async def connect(self, prompt: Any) -> None:
        self.prompt = prompt

    async def receive_messages(self):  # type: ignore[no-untyped-def]
        if False:
            yield None

    async def interrupt(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None


async def _burst(
    settings: Settings, project: Project, kind: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, list[str]]:
    captured: dict[str, Any] = {}
    real = sdk_backend.build_sdk_tools

    def spy(specs: Any, *a: Any, **kw: Any) -> Any:
        captured["names"] = [s.name for s in specs]
        return real(specs, *a, **kw)

    monkeypatch.setattr(sdk_backend, "build_sdk_tools", spy)
    clients: list[_Client] = []

    def factory(options: Any) -> _Client:
        clients.append(_Client(options))
        return clients[-1]

    backend = SdkStreamingBackend(
        settings=settings,
        specs=registry.all_specs(),
        ctx_factory=lambda: None,  # type: ignore[arg-type,return-value]
        client_factory=factory,
    )

    async def items():  # type: ignore[no-untyped-def]
        if False:
            yield QueuedItem(1, "c", "x", "t")
        await asyncio.sleep(0)

    _ = [ev async for ev in backend.start(project, conversation_row(type=kind), items(), None)]
    return clients[0].options, captured["names"]


async def _decision(guard: Any, tool: str) -> bool:
    res = await guard(tool, {}, ToolPermissionContext(tool_use_id="t"))
    return isinstance(res, PermissionResultAllow)


async def _hook_denies(options: Any, tool: str) -> bool:
    hook = options.hooks["PreToolUse"][0].hooks[0]
    out = await hook(
        {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}}, "t", {}
    )
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


async def test_requirements_burst_has_no_schematic_writes_or_plans(
    settings: Settings, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, names = await _burst(settings, project, "requirements", monkeypatch)
    assert set(names) == SPEC_82["requirements"]
    assert not set(names) & (SCH_WRITES | PLAN_TOOLS)
    connect = sdk_tool_name("sch_connect")
    assert not await _decision(options.can_use_tool, connect)
    assert await _hook_denies(options, connect)
    assert not await _decision(options.can_use_tool, sdk_tool_name("plan_propose"))
    assert await _decision(options.can_use_tool, sdk_tool_name("doc_write"))
    assert not await _hook_denies(options, sdk_tool_name("doc_write"))
    denied = await options.can_use_tool(connect, {}, ToolPermissionContext(tool_use_id="t"))
    assert isinstance(denied, PermissionResultDeny)


@pytest.mark.parametrize("kind", ["schematic", "free"])
async def test_schematic_and_free_are_todays_tools_plus_their_doc_tools(
    settings: Settings, project: Project, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    options, names = await _burst(settings, project, kind, monkeypatch)
    assert set(names) == SPEC_82[kind]
    assert await _decision(options.can_use_tool, sdk_tool_name("sch_connect"))
    writes = await _decision(options.can_use_tool, sdk_tool_name("doc_write"))
    assert writes is (kind == "free")


async def test_burst_system_prompt_is_per_type(
    settings: Settings, project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    options, _ = await _burst(settings, project, "requirements", monkeypatch)
    assert options.system_prompt == system_prompt("demo", "requirements")


def test_requirements_prompt_says_the_schematic_cannot_be_changed() -> None:
    text = system_prompt("demo", "requirements")
    assert "requirements session" in text
    assert "cannot change the schematic" in text
    assert "session-requirements" in text and "design-docs" in text
    assert "sch_* tools" not in text


def test_schematic_and_free_prompts_keep_the_editing_paragraph() -> None:
    schematic = system_prompt("demo", "schematic")
    assert "kicad-schematic-editing" in schematic
    assert "doc_list, then doc_read" in schematic and "decisions.md" in schematic
    free = system_prompt("demo", "free")
    assert "kicad-schematic-editing" in free and "doc_" in free
    for text in (schematic, free, system_prompt("demo", "requirements")):
        assert '"demo"' in text
        assert "[CopperChat notice]" in text
        assert "Claude Code" not in text
