import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.protocol import RateLimitedFrame, RunFinishedFrame, RunStartedFrame
from copperchat.db import Database
from copperchat.push.notifier import PushNotifier
from copperchat.push.visibility import VisibilityRegistry
from tests.support.conversations import TEST_CID, ensure_test_conversation

PID = "a1b2c3d4e5f6"


class FakeDispatcher:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_all(self, payload: dict[str, Any]) -> int:
        self.sent.append(payload)
        return 1


Env = tuple[Database, FakeDispatcher, VisibilityRegistry, PushNotifier]


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    db = await Database.open(tmp_path / "c.db")
    await ensure_test_conversation(db, PID)
    await db.execute("UPDATE conversations SET title = 'Session 1' WHERE id = ?", (TEST_CID,))
    d = FakeDispatcher()
    vis = VisibilityRegistry()
    n = PushNotifier(db, d, vis, project_names=lambda pid: "ecc83-pp")
    yield db, d, vis, n
    await n.close()
    await db.close()


async def _row(
    db: Database,
    run_id: str,
    text: str,
    *,
    role: str = "assistant",
    kind: str = "text",
    metadata: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        "INSERT INTO messages(project_id, run_id, role, kind, content, status, metadata, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'done', ?, '', '')",
        (PID, run_id, role, kind, text, json.dumps(metadata or {})),
    )


def _finished(run_id: str, reason: str) -> RunFinishedFrame:
    return RunFinishedFrame(
        project_id=PID,
        conversation_id=TEST_CID,
        run_id=run_id,
        reason=reason,  # type: ignore[arg-type]
    )


async def test_run_finished_sends_when_hidden(env: Env) -> None:
    db, d, _vis, n = env
    await _row(db, "r1", "ERC is clean.")
    await n.handle(_finished("r1", "completed"))
    assert d.sent == [
        {
            "title": "ecc83-pp · Session 1: run finished",
            "body": "ERC is clean.",
            "url": f"/p/{PID}/s/{TEST_CID}",
            "tag": f"run-{PID}",
        }
    ]


async def test_question_and_error_need_attention(env: Env) -> None:
    db, d, _vis, n = env
    await _row(db, "r2", "Thinking…")
    await _row(db, "r2", "Should I run DRC too?")
    # a later part-research row (sub-agent) is not the main agent's final text
    await _row(db, "r2", "Saved notes.", metadata={"parentToolUseId": "tu1"})
    await n.handle(_finished("r2", "completed"))
    await _row(db, "r3", "KiCad stopped responding", role="system", kind="error")
    await n.handle(_finished("r3", "error"))
    await n.handle(_finished("r4", "error"))
    assert [p["title"] for p in d.sent] == ["ecc83-pp · Session 1: needs your attention"] * 3
    assert [p["body"] for p in d.sent] == [
        "Should I run DRC too?",
        "KiCad stopped responding",
        "The run ended with an error.",
    ]


async def test_needs_attention_from_mid_run_plan_row(env: Env) -> None:
    db, d, _vis, n = env
    await _row(
        db,
        "r5",
        "Plan paused at step 2: new ERC errors were neither fixed nor justified.",
        role="system",
        kind="error",
        metadata={"code": "plan_needs_attention", "planId": "f" * 32},
    )
    await _row(db, "r5", "I paused the plan; the open pin is intended.")
    await n.handle(_finished("r5", "completed"))
    await _row(db, "r6", "Done.")
    await _row(db, "r6", "other", role="system", kind="error", metadata={"code": "x"})
    await _row(db, "r6", "All good.")
    await n.handle(_finished("r6", "completed"))
    assert [(p["title"], p["body"]) for p in d.sent] == [
        (
            "ecc83-pp · Session 1: needs your attention",
            "I paused the plan; the open pin is intended.",
        ),
        ("ecc83-pp · Session 1: run finished", "All good."),
    ]


async def test_long_text_is_clipped(env: Env) -> None:
    db, d, _vis, n = env
    await _row(db, "r1", "word " * 100)
    await n.handle(_finished("r1", "completed"))
    body = d.sent[0]["body"]
    assert len(body) <= 120 and body.endswith("…")


async def test_suppressed_when_visible_and_for_interrupt(env: Env) -> None:
    _db, d, vis, n = env
    vis.set(1, PID, True)
    await n.handle(_finished("r1", "error"))
    vis.set(1, PID, False)
    await n.handle(_finished("r1", "interrupted"))
    await n.handle(_finished("r1", "rate_limited"))
    assert d.sent == []


async def test_rate_limit_reopened(env: Env) -> None:
    _db, d, _vis, n = env
    soon = (datetime.now(UTC) + timedelta(milliseconds=50)).isoformat()
    await n.handle(RateLimitedFrame(project_id=PID, resets_at=soon))
    # a second frame for the same project replaces the timer (one push only)
    await n.handle(RateLimitedFrame(project_id=PID, resets_at=soon))
    await asyncio.sleep(0.3)
    assert [p["title"] for p in d.sent] == ["ecc83-pp: rate limit window reopened"]
    assert d.sent[0]["tag"] == f"ratelimit-{PID}"
    assert d.sent[0]["url"] == f"/p/{PID}"  # no session: the project page


async def test_rate_limit_reopen_suppressed_when_visible_then(env: Env) -> None:
    _db, d, vis, n = env
    n.schedule_reopen(PID, datetime.now(UTC) + timedelta(milliseconds=50))
    vis.set(7, PID, True)
    await asyncio.sleep(0.3)
    assert d.sent == []


async def test_close_cancels_timers(env: Env) -> None:
    _db, d, _vis, n = env
    n.schedule_reopen(PID, datetime.now(UTC) + timedelta(milliseconds=100))
    await n.close()
    await asyncio.sleep(0.3)
    assert d.sent == []


async def test_broadcaster_listener_never_blocks_or_breaks_publish(env: Env) -> None:
    db, d, _vis, n = env
    await _row(db, "r1", "Done here.")
    b = Broadcaster()
    sub = b.subscribe(PID)

    def broken(_frame: object) -> None:
        raise RuntimeError("listener bug")

    b.add_listener(broken)
    b.add_listener(n.on_frame)
    await b.publish(
        PID, RunStartedFrame(project_id=PID, conversation_id=TEST_CID, run_id="r1")
    )  # ignored by notifier
    await b.publish(PID, _finished("r1", "completed"))
    await n.drain()
    assert [p["body"] for p in d.sent] == ["Done here."]
    sub.close()
    assert [f.type async for f in sub] == []
