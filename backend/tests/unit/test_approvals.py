"""Shared approval helpers (`copperchat.approvals`, P3b Task 1): the quote rule of
`fp_approve` / `pcb_approve`, the user messages of the current run and the plan check."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio

from copperchat.approvals import (
    MIN_QUOTE_CHARS,
    MIN_QUOTE_WORDS,
    human_messages_after,
    normalise,
    plan_running,
    quote_in_message,
)
from copperchat.db import Database
from copperchat.db.repos.messages import MessageRepo
from copperchat.kicad_tools.footprints import approve as fp_approve
from copperchat.tools import ToolContext
from tests.support.conversations import TEST_CID, ensure_test_conversation

PID = "a1b2c3d4e5f6"
RUN = "run-1"


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "a.db")
    await ensure_test_conversation(d, PID)
    try:
        yield d
    finally:
        await d.close()


def _ctx(db: Database | None, *, run_id: str | None = RUN, cid: str | None = TEST_CID) -> Any:
    editing = None if db is None else SimpleNamespace(db=db)
    return cast(
        ToolContext,
        SimpleNamespace(
            editing=editing, run_id=run_id, conversation_id=cid, project=SimpleNamespace(id=PID)
        ),
    )


def test_footprint_module_keeps_names() -> None:
    assert fp_approve.normalise is normalise
    assert fp_approve.quote_in_message is quote_in_message
    assert (MIN_QUOTE_WORDS, MIN_QUOTE_CHARS) == (2, 8)


@pytest.mark.parametrize(
    ("quote", "message", "ok"),
    [
        ("ok!", "ok!", True),  # the whole (short) message
        ("  ok! ", "ok!\n", True),
        ("ok", "ok, but move C3 first", False),  # trivial quote inside a longer message
        ("ok go", "look good, go on", False),  # 2 words, < 8 characters
        ("look good", "Thanks, look good!", True),
        ("ook good", "Thanks, look good!", False),  # starts inside a word
        ("looks good to me", "Looks good to me", False),  # case-sensitive
        ("looks good", "it looks goodish", False),  # ends inside a word
        ("good - approve L201", "the render looks good - approve L201 please", True),
        ("", "", False),
    ],
)
def test_quote_rules(quote: str, message: str, ok: bool) -> None:
    assert quote_in_message(quote, message) is ok


async def _msg(db: Database, text: str, *, origin: str | None, run_id: str = RUN) -> None:
    await asyncio.sleep(0.003)  # created_at has millisecond resolution
    meta = {} if origin is None else {"origin": origin}
    await MessageRepo(db).insert(
        PID, TEST_CID, role="user", kind="text", content=text, run_id=run_id, metadata=meta
    )


async def test_human_messages_after_filters_origin(db: Database) -> None:
    await _msg(db, "typed early", origin="user")
    early = (await MessageRepo(db).run_user_messages(TEST_CID, RUN))[0].created_at
    await _msg(db, "plan text", origin="plan")
    await _msg(db, "closing", origin="close-session")
    await _msg(db, "no origin", origin=None)
    await _msg(db, "other run", origin="user", run_id="run-2")
    await _msg(db, "typed later", origin="user")
    await MessageRepo(db).insert(
        PID, TEST_CID, role="assistant", kind="text", content="assistant", run_id=RUN,
        metadata={"origin": "user"},
    )  # fmt: skip
    every = await human_messages_after(_ctx(db), None)
    assert [m.content for m in every] == ["typed early", "typed later"]
    later = await human_messages_after(_ctx(db), early)
    assert [m.content for m in later] == ["typed later"]
    assert await human_messages_after(_ctx(db, run_id=None), None) == []
    assert await human_messages_after(_ctx(db, cid=None), None) == []
    assert await human_messages_after(_ctx(None), None) == []


async def test_plan_running(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from copperchat import approvals

    seen: list[str] = []

    class FakePlans:
        def __init__(self, _db: Database) -> None:
            pass

        async def running_in_conversation(self, cid: str) -> bool:
            seen.append(cid)
            return cid == TEST_CID

    monkeypatch.setattr(approvals, "PlanRepo", FakePlans)
    assert await plan_running(_ctx(db)) is True
    assert await plan_running(_ctx(db, cid="e" * 32)) is False
    assert await plan_running(_ctx(db, cid=None)) is False
    assert await plan_running(_ctx(None)) is False
    assert seen == [TEST_CID, "e" * 32]


async def test_plan_running_real_repo(db: Database) -> None:
    assert await plan_running(_ctx(db)) is False
