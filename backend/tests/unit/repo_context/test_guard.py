"""The main guard re-checks repo_* paths through both entry points (spec §5.4, §5.5, §8)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext
from pydantic import SecretStr

from copperchat.agent.guards import (
    guard_as_pre_tool_use_hook,
    make_main_guard,
    make_researcher_guard,
    repo_path_refusal,
)
from copperchat.agent.profiles import PROFILES, REPO_TOOLS
from copperchat.agent.sdk_backend import build_options
from copperchat.agent.sdk_tools import sdk_tool_name
from copperchat.config import Settings
from copperchat.projects import Project
from copperchat.repo_context.paths import RepoPolicy, repo_policy
from copperchat.research.net import Resolver

from .conftest import git, init_repo, make_project

MCP = {"type": "sdk", "name": "copperchat", "instance": None}
Decide = Callable[[Any, str, dict[str, Any]], Awaitable[bool]]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = init_repo(tmp_path / "projects" / "repo")
    (root / "README.md").write_text("# Demo\n")
    (root / "docs").mkdir()
    (root / "docs" / "spec.md").write_text("spec\n")
    (root / ".env").write_text("TOKEN=x\n")
    (root / ".env.local").write_text("TOKEN=y\n")
    (root / "board.kicad_pcb").write_text("(kicad_pcb)\n")
    (root / "case.stl").write_bytes(b"solid\x00\x01\x02" * 10)
    (tmp_path / "outside.txt").write_text("secret\n")
    (root / "out.txt").symlink_to(tmp_path / "outside.txt")
    (root / "gitcfg").symlink_to(root / ".git" / "config")
    make_project(root / "hw")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
async def policy(repo: Path) -> RepoPolicy:
    project = make_project(repo / "hw")
    pol = await repo_policy(project, repo.parent)
    assert pol.root == repo.resolve()
    return pol


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=tmp_path / "projects",
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr("tok"),
    )


def _options(settings: Settings, project: Project, repo: RepoPolicy | None) -> Any:
    return build_options(
        settings=settings,
        project=project,
        mcp_server=MCP,  # type: ignore[arg-type]
        tool_names=sorted(REPO_TOOLS | {"doc_read"}),
        resume_session_id=None,
        fork_session=False,
        repo=repo,
    )


async def _via_can_use_tool(options: Any, tool: str, inp: dict[str, Any]) -> bool:
    res = await options.can_use_tool(tool, inp, ToolPermissionContext(tool_use_id="t"))
    return isinstance(res, PermissionResultAllow)


async def _via_hook(options: Any, tool: str, inp: dict[str, Any]) -> bool:
    hook = options.hooks["PreToolUse"][0].hooks[0]
    out = await hook(
        {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inp}, "t", {}
    )
    return out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


ENTRY_POINTS = pytest.mark.parametrize("decide", [_via_can_use_tool, _via_hook])

DENIED_READS = [
    "../x",
    "/etc/passwd",
    "out.txt",  # symlink out of the root
    "gitcfg",  # symlink to .git/config
    ".git/config",
    ".env",
    ".env.local",
    "board.kicad_pcb",
    "case.stl",  # binary
    "missing.md",
]


@ENTRY_POINTS
@pytest.mark.parametrize("path", DENIED_READS)
async def test_repo_read_refused(
    decide: Decide, path: str, policy: RepoPolicy, settings: Settings
) -> None:
    options = _options(settings, make_project(Path(policy.project_dir)), policy)
    assert not await decide(options, sdk_tool_name("repo_read"), {"path": path})


@ENTRY_POINTS
async def test_repo_read_allowed(decide: Decide, policy: RepoPolicy, settings: Settings) -> None:
    options = _options(settings, make_project(Path(policy.project_dir)), policy)
    assert await decide(options, sdk_tool_name("repo_read"), {"path": "README.md"})
    assert await decide(options, sdk_tool_name("repo_read"), {"path": "docs/spec.md"})


@ENTRY_POINTS
@pytest.mark.parametrize(
    ("tool", "inp"),
    [
        ("repo_list", {"pattern": "../*"}),
        ("repo_list", {"pattern": "/etc/*"}),
        ("repo_list", {"path": "../"}),
        ("repo_list", {"path": ".git"}),
        ("repo_list", {"path": "README.md"}),  # not a directory
        ("repo_grep", {"query": "x", "glob": "../*.md"}),
        ("repo_grep", {"query": "x", "path": "/etc"}),
        ("repo_read", {}),
        ("repo_read", {"path": 3}),
        ("repo_list", {"path": ["docs"]}),
        ("repo_grep", {"query": "x", "glob": 1}),
    ],
)
async def test_repo_list_and_grep_refused(
    decide: Decide, tool: str, inp: dict[str, Any], policy: RepoPolicy, settings: Settings
) -> None:
    options = _options(settings, make_project(Path(policy.project_dir)), policy)
    assert not await decide(options, sdk_tool_name(tool), inp)


@ENTRY_POINTS
@pytest.mark.parametrize(
    ("tool", "inp"),
    [
        ("repo_list", {}),
        ("repo_list", {"path": "docs", "pattern": "**/*.md"}),
        ("repo_grep", {"query": "x"}),
        ("repo_grep", {"query": "x", "path": "docs", "glob": "*.md"}),
    ],
)
async def test_repo_list_and_grep_allowed(
    decide: Decide, tool: str, inp: dict[str, Any], policy: RepoPolicy, settings: Settings
) -> None:
    options = _options(settings, make_project(Path(policy.project_dir)), policy)
    assert await decide(options, sdk_tool_name(tool), inp)


@ENTRY_POINTS
@pytest.mark.parametrize("tool", ["Read", "Glob", "Grep"])
async def test_builtin_file_tools_denied_inside_cwd(
    decide: Decide, tool: str, policy: RepoPolicy, settings: Settings
) -> None:
    options = _options(settings, make_project(Path(policy.project_dir)), policy)
    inside = str(policy.root / "README.md")
    inp = {"file_path": inside, "path": str(policy.root), "pattern": "*"}
    assert not await decide(options, tool, inp)


@ENTRY_POINTS
async def test_repo_tools_denied_without_policy(
    decide: Decide, policy: RepoPolicy, settings: Settings
) -> None:
    options = _options(settings, make_project(Path(policy.project_dir)), None)
    for tool in REPO_TOOLS:
        assert not await decide(options, sdk_tool_name(tool), {"path": "README.md"}), tool
    assert await decide(options, sdk_tool_name("doc_read"), {})


async def test_repo_path_refusal_direct(policy: RepoPolicy) -> None:
    assert repo_path_refusal(policy, "repo_read", {"path": "README.md"}) is None
    reason = repo_path_refusal(policy, "repo_read", {"path": ".env"})
    assert reason is not None and "denied" in reason.lower()
    kicad = repo_path_refusal(policy, "repo_read", {"path": "board.kicad_pcb"})
    assert kicad is not None and "sch_" in kicad
    assert repo_path_refusal(policy, "repo_list", {"path": None}) is None


async def test_unprefixed_guard_entry(policy: RepoPolicy) -> None:
    guard = make_main_guard(frozenset({sdk_tool_name("repo_read")}), repo=policy)
    ok = await guard(sdk_tool_name("repo_read"), {"path": "README.md"}, ToolPermissionContext())
    assert isinstance(ok, PermissionResultAllow)
    bad = await guard(sdk_tool_name("repo_read"), {"path": ".env"}, ToolPermissionContext())
    assert not isinstance(bad, PermissionResultAllow)


def test_every_profile_has_repo_tools() -> None:
    for kind, tools in PROFILES.items():
        assert tools >= REPO_TOOLS, kind


# --- researcher regression (spec §8) -------------------------------------------------------


async def _public(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


async def _researcher_allows(guard: Any, via_hook: bool, inp: dict[str, Any]) -> bool:
    if via_hook:
        out = await guard_as_pre_tool_use_hook(guard)(
            {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": inp}, "t", {}
        )
        return out == {}
    res = await guard("Read", inp, ToolPermissionContext(tool_use_id="t"))
    return isinstance(res, PermissionResultAllow)


@pytest.mark.parametrize("via_hook", [False, True])
async def test_researcher_still_reads_datasheets(tmp_path: Path, via_hook: bool) -> None:
    project = make_project(tmp_path / "proj")
    sheet = project.dir / "docs" / "parts" / "ABC123" / "datasheet.pdf"
    sheet.parent.mkdir(parents=True)
    sheet.write_bytes(b"%PDF-1.7\n\x00\x01")
    resolver: Resolver = _public
    guard = make_researcher_guard(project, resolver=resolver)
    assert await _researcher_allows(
        guard, via_hook, {"file_path": "docs/parts/ABC123/datasheet.pdf"}
    )
    assert await _researcher_allows(guard, via_hook, {"file_path": str(sheet)})


async def test_guard_resolves_policy_lazily_per_call(policy: RepoPolicy, repo: Path) -> None:
    fallback = RepoPolicy(policy.project_dir, policy.project_dir, False, policy.projects_root)
    answers: list[RepoPolicy] = [fallback, policy]

    async def provider() -> RepoPolicy:
        return answers.pop(0)

    guard = make_main_guard(frozenset({sdk_tool_name("repo_read")}), repo=provider)
    inp = {"path": "README.md"}  # in the root, not in the project dir
    first = await guard(sdk_tool_name("repo_read"), inp, ToolPermissionContext())
    assert not isinstance(first, PermissionResultAllow)
    second = await guard(sdk_tool_name("repo_read"), inp, ToolPermissionContext())
    assert isinstance(second, PermissionResultAllow)


async def test_guard_provider_failure_fails_closed() -> None:
    async def broken() -> RepoPolicy:
        raise OSError("gone")

    async def missing() -> RepoPolicy | None:
        return None

    for provider in (broken, missing):
        guard = make_main_guard(frozenset({sdk_tool_name("repo_read")}), repo=provider)
        res = await guard(sdk_tool_name("repo_read"), {"path": "x"}, ToolPermissionContext())
        assert not isinstance(res, PermissionResultAllow)
