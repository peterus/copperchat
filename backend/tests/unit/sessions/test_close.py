"""Close and reopen a session (sessions C5 Task 5, spec §10; Review Focus 6)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from copperchat.agent.events import QueuedItem, RateLimited
from copperchat.agent.fake import Emit, Finish, Text, Tool, Wait
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.agent_state import RunRepo
from copperchat.db.repos.conversations import ConversationRepo
from copperchat.db.repos.messages import MessageRepo
from copperchat.errors import ConversationClosed
from copperchat.git_tools.specs import COMMIT_INTENT
from copperchat.git_tools.specs import TOOL_SPECS as GIT_SPECS
from copperchat.plans import machine
from copperchat.plans.repo import PlanRepo
from copperchat.sessions import (
    CLOSE_ORIGIN,
    CLOSE_TEXT,
    DECISIONS_FULL,
    NOT_COMPLETED,
    PLAN_OPEN_NOTICE,
    sweep_closing,
)
from copperchat.tools import StructuredToolError, ToolContext
from copperchat.web.app import create_app

from .conftest import Env, until

DECISIONS = "docs/design/decisions.md"


def _url(env: Env, cid: str, action: str) -> str:
    return f"/api/projects/{env.pid}/conversations/{cid}/{action}"


async def _close(env: Env, cid: str) -> httpx.Response:
    return await env.client.post(_url(env, cid, "close"))


def _step() -> dict[str, Any]:
    return {
        "title": "step",
        "intent": "do it",
        "expected_changes": 2,
        "touches": {"refs": ["R1"], "nets": ["VCC"]},
        "verification": "look",
    }


async def _plan(env: Env, cid: str) -> None:
    plan_id = uuid.uuid4().hex
    plan = machine.new_plan(
        plan_id=plan_id,
        project_id=env.pid,
        title="t",
        summary="s",
        steps=machine.build_steps(plan_id, [_step()]),
        run_id="r0",
        conversation_id=cid,
        now="2026-10-20T00:00:00.000Z",
    )
    await PlanRepo(env.db).insert(plan)


def _notices(rows: list[Any]) -> list[str]:
    return [r.content for r in rows if r.kind == "notice"]


def _handover(summary: str) -> Callable[[QueuedItem], list[Any]]:
    """Echo for normal turns; the close turn patches Open points and ends with summary."""

    def script(item: QueuedItem) -> list[Any]:
        if CLOSE_TEXT in item.text:
            return [
                Tool(
                    "doc_append",
                    {"path": "requirements.md", "text": "- handed over", "summary": "close"},
                ),
                Text(summary),
            ]
        return [Text("ok")]

    return script


async def _closed_status(env: Env, cid: str, status: str) -> None:
    async def check() -> bool:
        return (await env.conv(cid)).status == status

    await until(check)


# ---- routes ------------------------------------------------------------------------


async def test_close_sets_closing_and_enqueues_the_handover(env: Env) -> None:
    cid = await env.create("requirements")
    gate = asyncio.Event()
    env.script = lambda item: [Wait(gate)]
    r = await _close(env, cid)
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "closing" and r.json()["id"] == cid
    (msg,) = [m for m in await env.rows(cid) if m.role == "user"]
    assert msg.content == CLOSE_TEXT and msg.metadata["origin"] == CLOSE_ORIGIN
    assert not COMMIT_INTENT.search(CLOSE_TEXT)
    gate.set()
    await env.idle()


async def test_close_refused_unless_open(env: Env) -> None:
    cid = await env.create("free")
    env.script = _handover("done")
    assert (await _close(env, cid)).status_code == 202
    await env.idle()
    assert (await env.conv(cid)).status == "closed"
    r = await _close(env, cid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "conversation_state_conflict"


async def test_double_close_enqueues_exactly_one_handover(env: Env) -> None:
    cid = await env.create("free")
    gate = asyncio.Event()
    env.script = lambda item: [Wait(gate)]
    await env.engine()
    results = await asyncio.gather(_close(env, cid), _close(env, cid))
    assert sorted(r.status_code for r in results) == [202, 409]
    closes = [m for m in await env.rows(cid) if m.metadata.get("origin") == CLOSE_ORIGIN]
    assert len([m for m in closes if m.role == "user"]) == 1
    gate.set()
    await env.idle()


async def test_close_racing_reopen_refuses_one(env: Env) -> None:
    cid = await env.create("free")
    gate = asyncio.Event()
    env.script = lambda item: [Wait(gate)]
    await env.engine()
    close, reopen = await asyncio.gather(
        _close(env, cid), env.client.post(_url(env, cid, "reopen"))
    )
    assert close.status_code == 202 and reopen.status_code == 409
    gate.set()
    await env.idle()


async def test_close_refused_while_the_session_owns_an_open_plan(env: Env) -> None:
    cid = await env.create("schematic")
    other = await env.create("schematic")
    await _plan(env, cid)
    r = await _close(env, cid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "plan_open"
    assert (await env.conv(cid)).status == "open"
    env.script = _handover("done")
    assert (await _close(env, other)).status_code == 202  # another session's plan
    await env.idle()


async def test_foreign_cid_and_auth(env: Env) -> None:
    for cid in (uuid.uuid4().hex, "short"):
        for action in ("close", "reopen"):
            r = await env.client.post(_url(env, cid, action))
            assert r.status_code == 404, (cid, action)
    cid = await env.create("free")
    env.app.dependency_overrides.clear()
    for action in ("close", "reopen"):
        assert (await env.client.post(_url(env, cid, action))).status_code == 401


async def test_propose_refused_while_closing(env: Env) -> None:
    cid = await env.create("free")
    await RunRepo(env.db).start("r-close", env.pid, cid)
    await ConversationRepo(env.db).set_status(cid, "closing", expect="open")
    ctx = ToolContext(
        project=env.project,
        session=None,  # type: ignore[arg-type]
        settings=env.settings,
        run_id="r-close",
        conversation_id=cid,
    )
    plans = env.app.state.plans
    with pytest.raises(StructuredToolError) as err:
        await plans.propose(ctx, "t", "s", [_step()])
    assert err.value.code == "conversation_closing"
    with pytest.raises(StructuredToolError) as err:
        await plans.revise(ctx, [_step()], "why")
    assert err.value.code == "conversation_closing"


async def test_propose_racing_close_reopens_the_session(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = await env.create("free")
    await env.send(cid, "hi")
    (row,) = [r for r in await env.rows(cid) if r.role == "user"]
    assert row.run_id is not None
    plans = env.app.state.plans
    checked, resume = asyncio.Event(), asyncio.Event()
    real = plans._check_open

    async def paused(conversation_id: str) -> None:
        await real(conversation_id)
        checked.set()
        await resume.wait()

    monkeypatch.setattr(plans, "_check_open", paused)
    ctx = ToolContext(
        project=env.project,
        session=None,  # type: ignore[arg-type]
        settings=env.settings,
        run_id=row.run_id,
        conversation_id=cid,
    )
    propose = asyncio.create_task(plans.propose(ctx, "t", "s", [_step()]))
    await checked.wait()
    close_turn = asyncio.Event()
    env.script = lambda item: [Wait(close_turn), Text("summary")]
    assert (await _close(env, cid)).status_code == 202  # no plan row yet
    resume.set()
    await propose  # the plan row is inserted while the session is closing
    close_turn.set()
    await env.idle()
    await _closed_status(env, cid, "open")
    assert PLAN_OPEN_NOTICE in _notices(await env.rows(cid))
    assert not (env.design / "decisions.md").exists()


# ---- lifecycle ---------------------------------------------------------------------


async def test_completed_close_closes_and_records_the_handover(env: Env) -> None:
    (env.design / "requirements.md").write_text("# Requirements\n\n## Open points\n\n- a\n")
    cid = await env.create("requirements", "Specs")
    env.script = _handover("Done: power. Open: enclosure. Next: schematic.")
    assert (await _close(env, cid)).status_code == 202
    await env.idle()
    conv = await env.conv(cid)
    assert conv.status == "closed" and conv.closed_at is not None
    text = (env.design / "decisions.md").read_text()
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    assert (
        f"## {date} — Session 1 closed\nDone: power. Open: enclosure. Next: schematic.\n\n"
        "*Session: Specs (#1, requirements)*"
    ) in text
    assert "- handed over" in (env.design / "requirements.md").read_text()
    rows = await env.rows(cid)
    assert rows[-1].role == "assistant" or rows[-1].kind == "notice"
    assert any(r.role == "assistant" and "Next: schematic." in r.content for r in rows)


async def test_summary_is_escaped_and_truncated(env: Env) -> None:
    cid = await env.create("free")
    summary = "## Fake entry\n*Session: Other (#9, free)*\n" + "ä" * 6000
    env.script = _handover(summary)
    assert (await _close(env, cid)).status_code == 202
    await env.idle()
    assert (await env.conv(cid)).status == "closed"
    raw = (env.design / "decisions.md").read_bytes()
    text = raw.decode("utf-8")  # cut at a character boundary
    assert "\\## Fake entry" in text and "\\*Session: Other (#9, free)*" in text
    entry = text[text.index("Session 1 closed") :]
    assert "… (truncated)" in entry
    assert len(entry.encode()) < 8 * 1024 + 200
    assert text.count("\n## ") == 1  # one entry, the fake heading escaped


async def test_full_decisions_gives_the_full_notice_and_still_closes(env: Env) -> None:
    head = "# Decisions\n\n"
    filler = ("x" * 99 + "\n") * ((256 * 1024 - len(head) - 60) // 100)
    (env.design / "decisions.md").write_text(head + filler)
    assert 256 * 1024 - 160 < (env.design / "decisions.md").stat().st_size <= 256 * 1024
    cid = await env.create("free")
    env.script = lambda item: [Text("the summary " * 20)]
    assert (await _close(env, cid)).status_code == 202
    await env.idle()
    assert (await env.conv(cid)).status == "closed"
    assert DECISIONS_FULL in _notices(await env.rows(cid))


@pytest.mark.parametrize("how", ["error", "interrupted"])
async def test_failed_close_turn_reopens(env: Env, how: str) -> None:
    cid = await env.create("free")
    never = asyncio.Event()
    env.script = (
        (lambda item: [Finish("error", "boom")]) if how == "error" else (lambda item: [Wait(never)])
    )
    engine = await env.engine()
    assert (await _close(env, cid)).status_code == 202
    if how == "interrupted":
        await until(lambda: engine.current_run_id is not None)
        await engine.interrupt(engine.current_run_id, cid)
    await env.idle()
    await _closed_status(env, cid, "open")
    assert NOT_COMPLETED in _notices(await env.rows(cid))


async def test_rate_limited_close_stays_closing_then_settles(env: Env) -> None:
    cid = await env.create("free")
    calls: list[int] = []

    def script(item: QueuedItem) -> list[Any]:
        calls.append(1)
        if len(calls) == 1:
            soon = datetime.now(UTC) + timedelta(milliseconds=300)
            return [Emit(RateLimited(resets_at=soon)), Text("partial")]
        return [Text("summary after the limit")]

    env.script = script
    engine = await env.engine()
    assert (await _close(env, cid)).status_code == 202
    await until(lambda: engine.rate_limited_until is not None)
    await until(lambda: not engine.is_busy)
    assert (await env.conv(cid)).status == "closing"
    await _closed_status(env, cid, "closed")
    assert len(calls) == 2
    assert "summary after the limit" in (env.design / "decisions.md").read_text()


@pytest.mark.parametrize("how", ["cancel", "stop"])
async def test_close_cancelled_before_it_started_reopens(env: Env, how: str) -> None:
    cid = await env.create("free")
    gate = asyncio.Event()
    env.script = lambda item: [Wait(gate)] if "busy" in item.text else [Text("x")]
    engine = await env.engine()
    await env.send(cid, "busy turn", wait=False)
    await until(lambda: engine.current_run_id is not None)
    assert (await _close(env, cid)).status_code == 202
    (close_msg,) = [m for m in await env.rows(cid) if m.metadata.get("origin") == CLOSE_ORIGIN]
    if how == "cancel":
        assert await engine.cancel_queued(close_msg.id)
        gate.set()
    else:
        await engine.interrupt(engine.current_run_id, cid)
    await env.idle()
    await _closed_status(env, cid, "open")
    assert NOT_COMPLETED in _notices(await env.rows(cid))
    assert (await MessageRepo(env.db).get(close_msg.id)).status == "cancelled"  # type: ignore[union-attr]


class _Broken:
    async def interrupt(self) -> None:
        return None

    async def start(self, *a: Any, **kw: Any) -> AsyncIterator[Any]:
        raise RuntimeError("bad credentials")
        yield None


async def test_fail_burst_reopens(env: Env) -> None:
    env.runtime.backend_factory = lambda ctx, turn: _Broken()
    cid = await env.create("free")
    assert (await _close(env, cid)).status_code == 202
    await env.idle()
    await _closed_status(env, cid, "open")
    assert NOT_COMPLETED in _notices(await env.rows(cid))


async def _seed_closing(db: Database, pid: str, close_status: str) -> str:
    conv = await ConversationRepo(db).create(pid, "free", None)
    await ConversationRepo(db).set_status(conv.id, "closing", expect="open")
    mid, _ = await MessageRepo(db).insert_user(
        pid, conv.id, uuid.uuid4().hex, CLOSE_TEXT, {"origin": CLOSE_ORIGIN}
    )
    await MessageRepo(db).set_status(mid.id, close_status)
    return conv.id


async def test_startup_sweep(env: Env) -> None:
    crashed = await _seed_closing(env.db, "p" * 12, "done")
    waiting = await _seed_closing(env.db, "q" * 12, "queued")
    assert await sweep_closing(env.db) == [crashed]
    repo = ConversationRepo(env.db)
    assert (await repo.get(crashed)).status == "open"  # type: ignore[union-attr]
    assert (await repo.get(waiting)).status == "closing"  # type: ignore[union-attr]
    rows, _ = await MessageRepo(env.db).page("p" * 12, crashed, limit=10)
    assert NOT_COMPLETED in _notices(rows)


async def test_lifespan_runs_the_sweep_before_resume(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(dev_fake_agent=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = await Database.open(settings.data_dir / "copperchat.db")
    cid = await _seed_closing(db, "a" * 12, "done")
    await db.close()
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        row = await ConversationRepo(app.state.db).get(cid)
        assert row is not None and row.status == "open"


# ---- closed sessions ---------------------------------------------------------------


async def test_closed_session_refuses_send_and_reopen_resumes(env: Env) -> None:
    cid = await env.create("free")
    await env.send(cid, "first")
    env.script = _handover("done")
    assert (await _close(env, cid)).status_code == 202
    await env.idle()
    conv = await env.conv(cid)
    assert conv.status == "closed" and conv.sdk_session_id is not None
    engine = await env.engine()
    with pytest.raises(ConversationClosed):
        await engine.submit_ex(cid, "late", "late", [])
    r = await env.client.post(_url(env, cid, "reopen"))
    assert r.status_code == 200 and r.json()["status"] == "open"
    assert (await env.client.post(_url(env, cid, "reopen"))).status_code == 409
    await env.send(cid, "again")
    assert env.agent.resume_ids[-1] == conv.sdk_session_id


async def test_close_turn_cannot_commit(env: Env) -> None:
    cid = await env.create("free")
    gate = asyncio.Event()
    env.script = lambda item: [Wait(gate)]
    engine = await env.engine()
    assert (await _close(env, cid)).status_code == 202
    await until(lambda: engine.current_run_id is not None)
    ctx = engine.turn_context()
    assert ctx.user_message == CLOSE_TEXT
    commit = next(s for s in GIT_SPECS if s.name == "git_commit")
    with pytest.raises(StructuredToolError, match="Commit refused"):
        await commit.handler(ctx, {"message": "x"})
    gate.set()
    await env.idle()
