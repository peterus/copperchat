"""The enclosure question against the fixture repo, through the fake backend's guard (§9)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import SecretStr

from copperchat.agent.events import QueuedItem, ToolResultEvt, ToolUse
from copperchat.agent.fake import FakeAgent, Text, Tool
from copperchat.config import Settings
from copperchat.kicad_session import KicadSession
from copperchat.repo_context.specs import TOOL_SPECS
from copperchat.tools import ToolContext
from tests.fixtures.repo_context import build_fixture_repo
from tests.support.conversations import conversation_row

QUESTION = "Is there an enclosure and what are its outer dimensions?"


async def _items(text: str):  # type: ignore[no-untyped-def]
    yield QueuedItem(
        message_id=1,
        client_message_id="c1",
        conversation_id="c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0",
        text=text,
    )


async def test_fake_agent_answers_enclosure_question(tmp_path: Path) -> None:
    fixture = build_fixture_repo(tmp_path)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=fixture.projects_root,
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr("tok"),
    )

    def script(_item: QueuedItem) -> list[Any]:
        return [
            Tool("repo_grep", {"query": "enclosure"}),
            Tool("repo_read", {"path": "mechanics/enclosure.scad"}),
            Tool("repo_read", {"path": ".env"}),
            Tool("repo_read", {"path": "docs/outside.txt"}),
            Text("The enclosure is 80 x 50 x 25 mm."),
        ]

    agent = FakeAgent(
        script,
        specs=TOOL_SPECS,
        ctx_factory=lambda: ToolContext(
            project=fixture.project, session=cast(KicadSession, None), settings=settings
        ),
        settings=settings,
    )
    events = [
        e
        async for e in agent.start(
            fixture.project, conversation_row(type="requirements"), _items(QUESTION), None
        )
    ]
    uses = [e for e in events if isinstance(e, ToolUse)]
    results = [e for e in events if isinstance(e, ToolResultEvt)]
    assert [u.name for u in uses] == [
        "mcp__copperchat__repo_grep",
        "mcp__copperchat__repo_read",
        "mcp__copperchat__repo_read",
        "mcp__copperchat__repo_read",
    ]
    grep, read, env, outside = results
    assert not grep.is_error and "mechanics/enclosure.scad" in grep.content
    assert not read.is_error
    for needle in ("width = 80", "depth = 50", "height = 25"):
        assert needle in read.content
    assert env.is_error and "API_TOKEN" not in env.content
    assert env.content.startswith("repo_read refused:")  # by the guard, before the handler
    assert outside.is_error and "TOP-SECRET-OUTSIDE" not in outside.content
