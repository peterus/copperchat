"""Doc context wiring and doc-changed notices (sessions C5 Task 3, S-C5, S-C8)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from copperchat.agent.events import TurnFinished
from copperchat.agent.fake import Emit, Tool, Wait
from copperchat.agent.protocol import MessageDeltaFrame
from copperchat.design_docs.repo import DocRepo
from copperchat.design_docs.service import MAX_MUTATIONS_PER_TURN
from copperchat.design_docs.specs import TOOL_SPECS
from copperchat.tools import ToolContext

from .conftest import Env, until

REQ = "docs/design/requirements.md"
BODY = "# Requirements\n\n## Power\n\n5 V USB\n\n## Open points\n\n- tbd\n"


def _results(rows: list[Any]) -> list[dict[str, Any]]:
    return [
        {"error": r.metadata.get("isError"), "content": r.content}
        for r in rows
        if r.kind == "tool_result"
    ]


def _write(path: str = "requirements.md", body: str = BODY, base: str | None = None) -> Tool:
    return Tool("doc_write", {"path": path, "body": body, "base_version": base, "summary": "write"})


async def test_doc_tools_get_conversation_and_run_with_kicad_stopped(env: Env) -> None:
    cid = await env.create("requirements")
    env.script = lambda item: [_write()]
    await env.send(cid, "write it")
    rev = await DocRepo(env.db).latest(env.pid, REQ)
    assert rev is not None and rev.author == "agent"
    assert rev.conversation_id == cid
    assert rev.conversation_label == "Session 1 — Requirements"
    runs = [r for r in await env.rows(cid) if r.run_id]
    assert rev.run_id is not None and rev.run_id in {r.run_id for r in runs}


async def test_schematic_policy_applies(env: Env) -> None:
    (env.design / "requirements.md").write_text(BODY)
    cid = await env.create("schematic")
    env.script = lambda item: [
        Tool(
            "doc_patch_section",
            {
                "path": "requirements.md",
                "heading": "Power",
                "body": "12 V",
                "base_version": env.version("requirements.md"),
                "summary": "x",
            },
        )
    ]
    await env.send(cid, "patch power")
    (res,) = _results(await env.rows(cid))
    assert res["error"] is True and "doc_scope_refused" in res["content"]
    assert "12 V" not in (env.design / "requirements.md").read_text()


async def test_unsolicited_turn_keeps_the_burst_conversation(env: Env) -> None:
    """A turn the CLI starts on its own has no _turn, but its tool calls still get the
    session's fields (burst-scoped, S-C3)."""
    cid = await env.create("schematic")
    gate = asyncio.Event()
    env.script = lambda item: [Emit(TurnFinished(reason="completed")), Wait(gate)]
    engine = await env.engine()
    await env.send(cid, "go", wait=False)
    # the scripted turn end ran: the burst still talks to the backend, without a turn
    await until(lambda: env.agents and env.agent.prompts and engine.current_run_id is None)
    assert engine.is_busy
    ctx = engine.turn_context()
    assert ctx.run_id is None
    assert (ctx.conversation_id, ctx.conversation_type) == (cid, "schematic")
    assert ctx.docs is env.docs and ctx.post_doc_notice is not None
    gate.set()
    await env.idle()


def _doc_notices(rows: list[Any]) -> list[Any]:
    return [r for r in rows if r.kind == "notice" and r.metadata.get("action") == "doc-changed"]


async def test_doc_changed_notice_has_exactly_the_contract_keys(env: Env) -> None:
    cid = await env.create("requirements")
    env.script = lambda item: [_write()]
    await env.send(cid, "write")
    (notice,) = _doc_notices(await env.rows(cid))
    assert set(notice.metadata) == {"action", "path", "revisionId", "added", "removed", "sections"}
    assert notice.metadata["path"] == REQ
    assert notice.metadata["added"] > 0 and isinstance(notice.metadata["revisionId"], int)
    assert notice.role == "system" and notice.run_id is not None
    assert "requirements.md updated" in notice.content


async def test_two_writes_in_one_turn_give_one_card_updated_by_delta(env: Env) -> None:
    (env.design / "requirements.md").write_text(BODY)
    cid = await env.create("requirements")
    env.script = lambda item: [
        Tool(
            "doc_append",
            {"path": "requirements.md", "text": "- more", "section": "Open points", "summary": "a"},
        ),
        Tool(
            "doc_append",
            {"path": "requirements.md", "text": "- even more", "section": "Power", "summary": "b"},
        ),
    ]
    sub = env.subscribe()
    await env.send(cid, "append twice")
    frames = []
    while not sub._q.empty():
        frame = sub._q.get_nowait()
        if frame is not None:
            frames.append(frame)
    sub.close()
    (notice,) = _doc_notices(await env.rows(cid))
    assert notice.metadata["sections"] == ["Open points", "Power"]
    assert notice.metadata["added"] == 2
    revs = await DocRepo(env.db).list(env.pid, REQ, 10)
    assert notice.metadata["revisionId"] == revs[0].id  # the newest revision
    deltas = [f for f in frames if isinstance(f, MessageDeltaFrame) and f.message_id == notice.id]
    assert len(deltas) == 1 and deltas[0].metadata_patch is not None
    assert deltas[0].metadata_patch["sections"] == ["Open points", "Power"]
    assert "(Open points, Power)" in notice.content


async def test_per_turn_cap_of_40_holds(env: Env) -> None:
    (env.design / "requirements.md").write_text(BODY)
    cid = await env.create("requirements")
    n = MAX_MUTATIONS_PER_TURN + 1
    env.script = lambda item: [
        Tool("doc_append", {"path": "requirements.md", "text": f"- p{i}", "summary": "a"})
        for i in range(n)
    ]
    await env.send(cid, "many")
    results = _results(await env.rows(cid))
    assert [r["error"] for r in results[:-1]] == [False] * MAX_MUTATIONS_PER_TURN
    assert results[-1]["error"] is True and "doc_limit" in results[-1]["content"]
    assert len(_doc_notices(await env.rows(cid))) == 1  # still one card for the doc


async def test_both_ctx_factories_set_the_conversation_type(env: Env) -> None:
    cid = await env.create("requirements")
    gate = asyncio.Event()
    env.script = lambda item: [Wait(gate)]
    engine = await env.engine()
    await env.send(cid, "hold", wait=False)
    await until(lambda: engine.current_run_id is not None)
    kicadless = engine.turn_context()
    session = SimpleNamespace(project=env.project, closed=False)
    engine._sessions = SimpleNamespace(current=lambda: session)  # type: ignore[assignment]
    try:
        kicad = engine._make_ctx()
    finally:
        engine._sessions = env.app.state.sessions
    for ctx in (kicadless, kicad):
        assert ctx.conversation_type == "requirements" and ctx.conversation_id == cid
        assert ctx.conversation_seq == 1 and ctx.conversation_title == "Session 1"
        assert ctx.run_id == engine.current_run_id and ctx.docs is env.docs
        assert ctx.post_doc_notice is not None
    assert kicad.session is session and kicad.on_mutation is not None
    gate.set()
    await env.idle()


async def test_conversation_without_type_fails_closed(env: Env) -> None:
    tools = {s.name: s for s in TOOL_SPECS}
    ctx = ToolContext(
        project=env.project,
        session=None,  # type: ignore[arg-type]
        settings=env.settings,
        run_id="r1",
        conversation_id="c" * 32,
        conversation_type=None,
        docs=env.docs,
    )
    try:
        await tools["doc_write"].handler(
            ctx, {"path": "requirements.md", "body": "x", "base_version": None, "summary": "s"}
        )
    except Exception as exc:
        assert getattr(exc, "code", None) == "doc_scope_refused"
    else:
        raise AssertionError("not refused")
    free = replace(ctx, conversation_id=None)
    out = await tools["doc_write"].handler(
        free, {"path": "requirements.md", "body": "x\n", "base_version": None, "summary": "s"}
    )
    assert out.data is not None and out.data["version"].startswith("v")


async def test_three_conflicts_in_one_turn_say_stop_and_ask(env: Env) -> None:
    (env.design / "requirements.md").write_text(BODY)
    cid = await env.create("requirements")
    stale = "v" + "0" * 16
    env.script = lambda item: [
        Tool(
            "doc_patch_section",
            {
                "path": "requirements.md",
                "heading": "Power",
                "body": f"try {i}",
                "base_version": stale,
                "summary": "x",
            },
        )
        for i in range(3)
    ]
    await env.send(cid, "conflict")
    results = _results(await env.rows(cid))
    assert all(r["error"] for r in results) and len(results) == 3
    hints = [json.loads(r["content"])["error"]["suggestion"] for r in results]
    assert "stop and ask the user" not in hints[0] and "stop and ask the user" not in hints[1]
    assert "stop and ask the user" in hints[2]


async def test_approval_lost_is_a_plain_notice(env: Env) -> None:
    (env.design / "requirements.md").write_text(BODY)
    version = env.version("requirements.md")
    assert version is not None
    await env.docs.approve(env.project, "requirements.md", version)
    cid = await env.create("requirements", "Specs")
    env.script = lambda item: [
        Tool(
            "doc_append",
            {"path": "requirements.md", "text": "- new", "section": "Open points", "summary": "a"},
        )
    ]
    await env.send(cid, "change it")
    rows = await env.rows(cid)
    lost = [r for r in rows if r.kind == "notice" and "after approval" in r.content]
    assert [r.content for r in lost] == [
        "requirements.md changed after approval (Specs) — review and re-approve"
    ]
    assert lost[0].metadata == {"path": REQ}
    assert len(_doc_notices(rows)) == 1  # plus the doc-changed card
    conv = await env.conv(cid)
    assert conv.docs_seen[REQ] == env.version("requirements.md")


async def test_stale_own_write_does_not_hide_a_user_edit(env: Env) -> None:
    """docs_seen advances over an own change only if it had seen its base: a user edit
    made during the turn, under a stale doc_append, is still reported next turn."""
    (env.design / "requirements.md").write_text(BODY)
    cid = await env.create("requirements")
    env.script = lambda item: []
    await env.send(cid, "first")  # index; docs_seen = v1
    v1 = env.version("requirements.md")
    gate = asyncio.Event()
    env.script = lambda item: [
        Wait(gate),
        Tool("doc_append", {"path": "requirements.md", "text": "- agent", "summary": "a"}),
    ]
    engine = await env.engine()
    await env.send(cid, "append without reading", wait=False)
    await until(lambda: engine.current_run_id is not None and len(env.agent.prompts) == 2)
    await env.docs.write(env.project, "requirements.md", BODY + "user line\n", v1, author="user")
    gate.set()
    await env.idle()
    assert (await env.conv(cid)).docs_seen[REQ] == v1
    env.script = lambda item: []
    await env.send(cid, "next")
    prompt = [b for b in env.agent.bursts if b.conversation_id == cid][-1].prompts[-1]
    assert "requirements.md changed since your last turn (user edit, " in prompt
