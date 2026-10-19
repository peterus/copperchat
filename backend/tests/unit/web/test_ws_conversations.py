"""WS frames with conversationId: ownership checks and per-conversation replay (§6.2)."""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from copperchat.agent.fake import Text, Wait
from copperchat.db.repos.messages import MessageRepo
from copperchat.web import ws as ws_module

from .conftest import OTHER_PID
from .test_ws import recv_until, send


@pytest.fixture(autouse=True)
def fake_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(conn: Any) -> str:
        return "admin"

    monkeypatch.setattr(ws_module, "authenticate_connection", ok)


def _conv(client: TestClient, pid: str) -> str:
    r = client.post(f"/api/projects/{pid}/conversations", json={"type": "free"})
    assert r.status_code == 201
    return str(r.json()["id"])


def _send(sock: Any, pid: str, cid: str, text: str) -> None:
    send(
        sock,
        type="send",
        projectId=pid,
        conversationId=cid,
        clientMessageId=text,
        text=text,
        attachments=[],
    )


def test_subscribe_replays_only_that_conversation_and_live_frames_of_all(
    build_chat_app: Any,
) -> None:
    app, pid = build_chat_app(lambda item: [Text(f"re {item.text}")])
    with TestClient(app) as client:
        a, b = _conv(client, pid), _conv(client, pid)
        with client.websocket_connect("/api/ws") as sock:
            send(sock, type="subscribe", projectId=pid, conversationId=a, sinceMessageId=0)
            _send(sock, pid, a, "a1")
            recv_until(sock, lambda f: f["type"] == "run-finished")
            _send(sock, pid, b, "b1")
            frames = recv_until(sock, lambda f: f["type"] == "run-finished")
            # Live frames of the project's other conversation still arrive.
            assert any(
                f["type"] == "message" and f["message"]["conversationId"] == b for f in frames
            )
            assert all(
                f.get("conversationId", b) == b
                for f in frames
                if f["type"] != "message" and f["type"] != "conversation-updated"
            )
        with client.websocket_connect("/api/ws") as sock2:
            send(sock2, type="subscribe", projectId=pid, conversationId=a, sinceMessageId=0)
            send(sock2, type="ping", projectId=None)
            replay = recv_until(sock2, lambda f: f["type"] == "pong")
            msgs = [f["message"] for f in replay if f["type"] == "message"]
            assert msgs and all(m["conversationId"] == a for m in msgs)
            assert [m["content"] for m in msgs] == ["a1", "re a1"]
            # Re-subscribing with another conversation replaces the replay cursor.
            send(sock2, type="subscribe", projectId=pid, conversationId=b, sinceMessageId=0)
            send(sock2, type="ping", projectId=None)
            replay_b = recv_until(sock2, lambda f: f["type"] == "pong")
            assert [f["message"]["content"] for f in replay_b if f["type"] == "message"] == [
                "b1",
                "re b1",
            ]


def test_foreign_conversation_frames_are_refused(build_chat_app: Any) -> None:
    gate = asyncio.Event()
    app, pid = build_chat_app(lambda item: [Wait(gate), Text("x")])
    with TestClient(app) as client:
        mine = _conv(client, pid)
        foreign = _conv(client, OTHER_PID)  # project B's conversation
        with client.websocket_connect("/api/ws") as sock:
            send(sock, type="subscribe", projectId=pid, conversationId=foreign, sinceMessageId=0)
            err = recv_until(sock, lambda f: f["type"] == "error")[-1]
            assert (err["code"], err["projectId"]) == ("conversation_not_found", pid)
            # No subscription was bound: a live frame of the project does not arrive.
            send(
                sock,
                type="subscribe",
                projectId=OTHER_PID,
                conversationId=foreign,
                sinceMessageId=0,
            )
            _send(sock, pid, foreign, "smuggled")
            err = recv_until(sock, lambda f: f["type"] == "error")[-1]
            assert err["code"] == "conversation_not_found" and err["clientMessageId"] == "smuggled"
            send(sock, type="interrupt", projectId=pid, conversationId=foreign, runId=None)
            err = recv_until(sock, lambda f: f["type"] == "error")[-1]
            assert err["code"] == "conversation_not_found"
            send(sock, type="ping", projectId=None)
            frames = recv_until(sock, lambda f: f["type"] == "pong")
            assert not any(f["type"] == "message" for f in frames)
        db = app.state.db
        rows = client.portal.call(MessageRepo(db).queued_user_rows, pid)  # type: ignore[union-attr]
        assert rows == [], "nothing was enqueued"
        rows_b = client.portal.call(MessageRepo(db).queued_user_rows, OTHER_PID)  # type: ignore[union-attr]
        assert rows_b == []
        assert mine


def test_send_to_closed_conversation_is_refused(build_chat_app: Any) -> None:
    from copperchat.db.repos.conversations import ConversationRepo

    app, pid = build_chat_app()
    with TestClient(app) as client:
        cid = _conv(client, pid)
        repo = ConversationRepo(app.state.db)
        client.portal.call(lambda: repo.set_status(cid, "closing", expect="open"))  # type: ignore[union-attr]
        with client.websocket_connect("/api/ws") as sock:
            _send(sock, pid, cid, "late")
            err = recv_until(sock, lambda f: f["type"] == "error")[-1]
            assert err["code"] == "conversation_closed" and err["clientMessageId"] == "late"


def test_interrupt_carries_the_conversation(build_chat_app: Any) -> None:
    gate = asyncio.Event()
    app, pid = build_chat_app(lambda item: [Text("a"), Wait(gate)])
    with TestClient(app) as client:
        a, b = _conv(client, pid), _conv(client, pid)
        with client.websocket_connect("/api/ws") as sock:
            send(sock, type="subscribe", projectId=pid, conversationId=b, sinceMessageId=0)
            _send(sock, pid, a, "a1")
            started = recv_until(sock, lambda f: f["type"] == "run-started")[-1]
            assert started["conversationId"] == a
            _send(sock, pid, b, "b1")
            # Stop pressed in B's view on A's running chip.
            send(sock, type="interrupt", projectId=pid, conversationId=a, runId=started["runId"])
            done = recv_until(sock, lambda f: f["type"] == "run-finished")[-1]
            assert done["reason"] == "interrupted" and done["conversationId"] == a
            nxt = recv_until(sock, lambda f: f["type"] == "run-started")[-1]
            assert nxt["conversationId"] == b, "B's message was not cancelled"
            gate.set()


def test_subscribe_during_run_restores_the_running_chip(build_chat_app: Any) -> None:
    gate = asyncio.Event()
    app, pid = build_chat_app(lambda item: [Text("a"), Wait(gate)])
    with TestClient(app) as client:
        a, b = _conv(client, pid), _conv(client, pid)
        with client.websocket_connect("/api/ws") as sock:
            send(sock, type="subscribe", projectId=pid, conversationId=a, sinceMessageId=0)
            _send(sock, pid, a, "a1")
            recv_until(sock, lambda f: f["type"] == "run-started")
            with client.websocket_connect("/api/ws") as sock2:
                send(sock2, type="subscribe", projectId=pid, conversationId=b, sinceMessageId=0)
                started = recv_until(sock2, lambda f: f["type"] == "run-started")[-1]
                assert started["conversationId"] == a
            gate.set()
            recv_until(sock, lambda f: f["type"] == "run-finished")


def test_conversation_updated_on_first_and_last_message(build_chat_app: Any) -> None:
    app, pid = build_chat_app(lambda item: [Text("ok")])
    with TestClient(app) as client:
        cid = _conv(client, pid)
        with client.websocket_connect("/api/ws") as sock:
            send(sock, type="subscribe", projectId=pid, conversationId=cid, sinceMessageId=0)
            _send(sock, pid, cid, "hi")
            frames = recv_until(sock, lambda f: f["type"] == "run-finished")
            updates = [f for f in frames if f["type"] == "conversation-updated"]
            assert len(updates) >= 2
            assert updates[0]["conversation"]["lastMessageAt"] is not None
            assert updates[-1]["conversation"]["usage"]["inputTokens"] == 1
