"""Opt-in live eval for repo context (repo-context spec §9, plan Task 8).

A Requirements session of the real app is asked the enclosure question against the
fixture repo (``mechanics/enclosure.scad``: ``cube([80, 50, 25])``). The answer must name
80, 50 and 25 and the agent must have used a ``repo_*`` tool; the outcome goes to
``tests/live/results/<date>.json`` under ``repo_context``.

Costs a few cents:
    uv run pytest -m live_sdk tests/live/test_repo_context_live.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.fixtures.repo_context import build_fixture_repo

from .conftest import live
from .sessions_app import app_settings, live_app
from .test_editing_eval import write_result

pytestmark = live
QUESTION = "Gibt es ein Gehäuse und welche Außenmaße hat es?"


@pytest.mark.timeout(900)
async def test_enclosure_question_live(tmp_path: Path) -> None:
    build_fixture_repo(tmp_path)
    settings = app_settings(tmp_path, kicad_cli="kicad-cli-not-installed")
    async with live_app(settings) as app:
        cid = await app.create("requirements")
        await app.send(cid, QUESTION)
        turn = await app.turn(cid)
    reply = "\n".join(turn.texts())
    repo_calls = turn.succeeded("repo_")
    result: dict[str, Any] = {
        "question": QUESTION,
        "tools": turn.main_tools(),
        "repo_calls": repo_calls,
        "errors": turn.errors(),
        "reply": reply[:600],
    }
    write_result("repo_context", result)
    assert repo_calls, result
    for dim in ("80", "50", "25"):
        assert dim in reply, result
