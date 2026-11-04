import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from copperchat.agent.profiles import MOUSER_TOOLS, PROFILES
from copperchat.agent.sdk_backend import system_prompt
from copperchat.config import Settings
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadSession
from copperchat.mouser import client as mc
from copperchat.mouser.specs import TOOL_SPECS
from copperchat.projects import Project
from copperchat.research import datasheets, runner
from copperchat.research.agent import PARTS_TOOLS, researcher_prompt
from copperchat.tools import ToolContext, registry

FIXTURES = Path(__file__).parents[2] / "fixtures/mouser"
KEY = "sekr3t-key-0000-1111"


def settings(tmp_path: Path, key: str | None = KEY) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=tmp_path / "projects",
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr("tok"),
        mouser_api_key=SecretStr(key) if key is not None else None,
    )


@pytest.fixture
def project(tmp_path: Path) -> Project:
    d = tmp_path / "projects" / "demo"
    d.mkdir(parents=True)
    return Project(
        "a1b2c3d4e5f6", "demo", "demo/demo.kicad_pro", d, d / "demo.kicad_pro", None, None
    )


@pytest.fixture(autouse=True)
def clean_clients() -> Iterator[None]:
    mc._clients.clear()
    yield
    mc._clients.clear()


def install(handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def h(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return cast(httpx.Response, handler(request))

    mc._clients[KEY] = mc.MouserClient(
        KEY, transport=httpx.MockTransport(h), today=lambda: date(2026, 11, 4)
    )
    return seen


def fixture_response(request: httpx.Request) -> httpx.Response:
    name = "keyword.json" if request.url.path.endswith("keyword") else "partnumber.json"
    return httpx.Response(200, json=json.loads((FIXTURES / name).read_text()))


def ctx(project: Project, s: Settings) -> ToolContext:
    return ToolContext(project=project, session=cast(KicadSession, None), settings=s)


def spec(name: str) -> Any:
    return next(s for s in TOOL_SPECS if s.name == name)


def test_specs_are_read_only_and_registered() -> None:
    names = {s.name for s in registry.all_specs()}
    assert {"mouser_search", "mouser_part"} <= names
    for s in TOOL_SPECS:
        assert not s.mutating and not s.changes_documents and not s.requires_kicad
        assert s.input_schema["additionalProperties"] is False
    assert spec("mouser_part").input_schema["properties"]["mpns"]["maxItems"] == 10


def test_every_profile_has_the_mouser_tools() -> None:
    assert frozenset({"mouser_search", "mouser_part"}) == MOUSER_TOOLS
    for kind, tools in PROFILES.items():
        assert tools >= MOUSER_TOOLS, kind


async def test_part_tool(project: Project, tmp_path: Path) -> None:
    seen = install(fixture_response)
    res = await spec("mouser_part").handler(ctx(project, settings(tmp_path)), {"mpns": ["LM358DR"]})
    assert res.data["parts"][0]["found"] is True
    assert KEY not in json.dumps(res.data)
    assert len(seen) == 1


async def test_search_tool_default_limit(project: Project, tmp_path: Path) -> None:
    seen = install(fixture_response)
    res = await spec("mouser_search").handler(ctx(project, settings(tmp_path)), {"keyword": "buck"})
    assert res.data["hits"][0]["mpn"] == "TPS62133RGTR"
    assert json.loads(seen[0].content)["SearchByKeywordRequest"]["records"] == 10


@pytest.mark.parametrize("key", [None, "", "   "])
async def test_not_configured(project: Project, tmp_path: Path, key: str | None) -> None:
    for name, args in (("mouser_part", {"mpns": ["LM358DR"]}), ("mouser_search", {"keyword": "x"})):
        with pytest.raises(StructuredToolError) as ei:
            await spec(name).handler(ctx(project, settings(tmp_path, key)), args)
        assert ei.value.code == "not_configured"
        assert "research_part" in ei.value.suggestion


def test_client_shared_per_key(tmp_path: Path) -> None:
    s = settings(tmp_path)
    a = mc.client_for(s)
    assert a is mc.client_for(settings(tmp_path))
    assert mc.client_for(settings(tmp_path, None)) is None
    assert mc.client_for(settings(tmp_path, "other-key")) is not a


def test_settings_repr_hides_key(tmp_path: Path) -> None:
    assert KEY not in repr(settings(tmp_path))


# ---- system prompt


def test_system_prompt_routes_quick_sourcing_questions_to_mouser() -> None:
    for kind in ("free", "requirements", "schematic", "board"):
        prompt = system_prompt("demo", kind)
        assert "mouser_part" in prompt and "mouser_search" in prompt, kind
        assert "research_part" in prompt


# ---- part-researcher


def test_researcher_gets_mouser_tools_only_with_a_key(project: Project, tmp_path: Path) -> None:
    assert {"mcp__parts__mouser_search", "mcp__parts__mouser_part"} <= set(PARTS_TOOLS)
    with_key = datasheets.parts_tools(project, mouser=mc.client_for(settings(tmp_path)))
    without = datasheets.parts_tools(project, mouser=None)
    assert {t.name for t in with_key} == {
        "download_datasheet",
        "check_notes",
        "mouser_search",
        "mouser_part",
    }
    assert {t.name for t in without} == {"download_datasheet", "check_notes"}


async def test_researcher_mouser_handlers(project: Project, tmp_path: Path) -> None:
    install(fixture_response)
    tools = {t.name: t for t in datasheets.parts_tools(project, mouser=mc._clients[KEY])}
    out = await tools["mouser_part"].handler({"mpns": ["TPS62133RGTR", "NOPE123"]})
    payload = json.loads(out["content"][0]["text"])
    assert payload["parts"][1]["found"] is False
    assert "is_error" not in out
    out = await tools["mouser_part"].handler({"mpns": "TPS62133RGTR"})
    assert out["is_error"] is True
    err = json.loads(out["content"][0]["text"])["error"]
    assert err["code"] == "invalid_input"
    out = await tools["mouser_search"].handler({"keyword": "buck", "limit": 2})
    assert len(json.loads(out["content"][0]["text"])["hits"]) == 2


async def test_researcher_mouser_error_suggests_web(project: Project, tmp_path: Path) -> None:
    install(lambda _r: httpx.Response(503))
    tools = {t.name: t for t in datasheets.parts_tools(project, mouser=mc._clients[KEY])}
    out = await tools["mouser_part"].handler({"mpns": ["TPS62133RGTR"]})
    err = json.loads(out["content"][0]["text"])["error"]
    assert err["code"] == "upstream_error"
    assert "WebSearch" in err["suggestion"]


def test_researcher_prompt_sourcing_section() -> None:
    plain = researcher_prompt("SKILL")
    with_mouser = researcher_prompt("SKILL", mouser=True)
    assert "Sourcing (Mouser" not in plain
    assert "## Sourcing (Mouser, as of YYYY-MM-DD)" in with_mouser
    assert "mouser_part" in with_mouser
    assert with_mouser.endswith("SKILL")


def test_researcher_options_wire_mouser(project: Project, tmp_path: Path) -> None:
    opts = runner.researcher_options(project, settings(tmp_path), "SKILL")
    assert isinstance(opts.system_prompt, str)
    assert "Sourcing (Mouser" in opts.system_prompt
    assert KEY not in json.dumps(opts.env) and KEY not in opts.system_prompt
    opts = runner.researcher_options(project, settings(tmp_path, None), "SKILL")
    assert isinstance(opts.system_prompt, str)
    assert "Sourcing (Mouser" not in opts.system_prompt


async def test_researcher_guard_allows_mouser_tools(project: Project) -> None:
    from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

    from copperchat.agent.guards import make_researcher_guard

    guard = make_researcher_guard(project)
    res = await guard(
        "mcp__parts__mouser_part", {"mpns": ["X123"]}, ToolPermissionContext(tool_use_id="t")
    )
    assert isinstance(res, PermissionResultAllow)
