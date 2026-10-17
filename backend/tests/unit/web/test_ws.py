import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from copperchat.agent.broadcaster import Broadcaster
from copperchat.agent.events import RateLimited
from copperchat.agent.fake import Emit, Finish, Text, Wait
from copperchat.agent.protocol import PongFrame
from copperchat.web import ws as ws_module

from .conftest import OTHER_PID


@pytest.fixture(autouse=True)
def fake_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(conn: Any) -> str:
        return "admin"

    monkeypatch.setattr(ws_module, "authenticate_connection", ok)


def recv_until(sock: Any, pred: Any, limit: int = 200) -> list[dict[str, Any]]:
    frames = []
    for _ in range(limit):
        frame = json.loads(sock.receive_text())
        if frame["type"] != "ping":
            frames.append(frame)
        if pred(frame):
            return frames
    raise AssertionError("expected frame not received")


def send(sock: Any, **frame: Any) -> None:
    sock.send_text(json.dumps(frame))


def test_ping_pong_and_invalid_frames(build_chat_app: Any) -> None:
    app, pid = build_chat_app()
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        send(sock, type="ping", projectId=None)
        assert json.loads(sock.receive_text()) == {"type": "pong", "projectId": None}
        sock.send_text("not json")
        err = json.loads(sock.receive_text())
        assert err["type"] == "error" and err["projectId"] is None
        assert err["code"] == "invalid_frame"
        send(sock, type="send", projectId=pid, clientMessageId="c", text="", attachments=[])
        err2 = json.loads(sock.receive_text())
        assert err2["projectId"] == pid and err2["code"] == "invalid_frame"
        assert err2["clientMessageId"] == "c"
        send(sock, type="subscribe", projectId="../../etc", sinceMessageId=0)
        err3 = json.loads(sock.receive_text())
        assert err3["projectId"] is None and err3["code"] == "invalid_frame"
        sock.send_text("[1, 2]")
        assert json.loads(sock.receive_text())["code"] == "invalid_frame"


def test_unknown_project_is_an_error_frame(build_chat_app: Any) -> None:
    app, _ = build_chat_app()
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        send(sock, type="subscribe", projectId="0123456789ab", sinceMessageId=0)
        err = json.loads(sock.receive_text())
        assert err["type"] == "error" and err["code"] == "project_not_found"
        assert err["projectId"] == "0123456789ab"
        send(sock, type="ping", projectId=None)  # the connection survives
        assert json.loads(sock.receive_text())["type"] == "pong"


def test_subscribe_send_streams_and_every_frame_has_project_id(build_chat_app: Any) -> None:
    app, pid = build_chat_app()
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        send(sock, type="subscribe", projectId=pid, sinceMessageId=0)
        send(sock, type="send", projectId=pid, clientMessageId="c1", text="hi", attachments=[])
        frames = recv_until(sock, lambda f: f["type"] == "run-finished")
        assert all(f["projectId"] == pid for f in frames)
        assert frames[0]["type"] == "message" and frames[0]["message"]["status"] == "queued"
        text = "".join(f.get("contentDelta", "") for f in frames if f["type"] == "message-delta")
        assert text == "You said: hi"
        assert any(f["type"] == "usage" for f in frames)


def test_one_connection_subscribes_to_several_projects(build_chat_app: Any) -> None:
    app, pid = build_chat_app()
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        send(sock, type="subscribe", projectId=pid, sinceMessageId=0)
        send(sock, type="subscribe", projectId=OTHER_PID, sinceMessageId=0)
        send(sock, type="send", projectId=pid, clientMessageId="a", text="one", attachments=[])
        recv_until(sock, lambda f: f["type"] == "run-finished" and f["projectId"] == pid)
        send(
            sock, type="send", projectId=OTHER_PID, clientMessageId="b", text="two", attachments=[]
        )
        frames = recv_until(
            sock, lambda f: f["type"] == "run-finished" and f["projectId"] == OTHER_PID
        )
        assert {f["projectId"] for f in frames} == {OTHER_PID}
        # Re-subscribing the same project replaces its subscription (no duplicate frames).
        send(sock, type="subscribe", projectId=pid, sinceMessageId=10_000)
        send(sock, type="send", projectId=pid, clientMessageId="c", text="x", attachments=[])
        frames = recv_until(sock, lambda f: f["type"] == "run-finished")
        user_rows = [f for f in frames if f["type"] == "message" and f["message"]["role"] == "user"]
        assert len(user_rows) == 1


def test_unknown_attachment_is_an_error_frame(build_chat_app: Any) -> None:
    app, pid = build_chat_app()
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        send(
            sock,
            type="send",
            projectId=pid,
            clientMessageId="c9",
            text="x",
            attachments=[{"id": "up-9", "kind": "image", "name": "a.png"}],
        )
        err = json.loads(sock.receive_text())
        assert err == {
            "type": "error",
            "projectId": pid,
            "code": "unknown_attachment",
            "message": "unknown attachment: up-9",
            "clientMessageId": "c9",
        }


def test_cancel_queued_and_interrupt(build_chat_app: Any) -> None:
    gate = asyncio.Event()
    app, pid = build_chat_app(lambda item: [Text("a"), Wait(gate), Text("b")])
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        send(sock, type="subscribe", projectId=pid, sinceMessageId=0)
        send(sock, type="send", projectId=pid, clientMessageId="c1", text="m1", attachments=[])
        started = recv_until(sock, lambda f: f["type"] == "run-started")
        first_id = started[0]["message"]["id"]
        send(sock, type="send", projectId=pid, clientMessageId="c2", text="m2", attachments=[])
        queued = recv_until(
            sock, lambda f: f["type"] == "message" and f["message"]["clientMessageId"] == "c2"
        )
        second_id = queued[-1]["message"]["id"]
        send(sock, type="cancel-queued", projectId=pid, messageId=second_id)
        recv_until(
            sock, lambda f: f.get("statusPatch") == "cancelled" and f["messageId"] == second_id
        )
        send(sock, type="cancel-queued", projectId=pid, messageId=first_id)
        err = recv_until(sock, lambda f: f["type"] == "error")[-1]
        assert err["code"] == "not_cancellable" and err["projectId"] == pid
        send(sock, type="interrupt", projectId=pid, runId=started[-1]["runId"])
        done = recv_until(sock, lambda f: f["type"] == "run-finished")[-1]
        assert done["reason"] == "interrupted"


def test_reconnect_mid_run_gets_half_streamed_row_then_deltas(build_chat_app: Any) -> None:
    gate = asyncio.Event()
    app, pid = build_chat_app(lambda item: [Text("Hel"), Wait(gate), Text("lo")])
    with TestClient(app) as client:
        with client.websocket_connect("/api/ws") as first:
            send(first, type="subscribe", projectId=pid, sinceMessageId=0)
            send(first, type="send", projectId=pid, clientMessageId="c1", text="hi", attachments=[])
            recv_until(first, lambda f: f.get("contentDelta") == "Hel")
        with client.websocket_connect("/api/ws") as second:
            send(second, type="subscribe", projectId=pid, sinceMessageId=0)
            replay = recv_until(second, lambda f: f["type"] == "run-started")
            rows = [f["message"] for f in replay if f["type"] == "message"]
            assistant = rows[-1]
            assert assistant["role"] == "assistant" and assistant["status"] == "streaming"
            assert assistant["content"] == "Hel", "missed deltas arrive as the full row"
            client.portal.call(gate.set)
            live = recv_until(second, lambda f: f["type"] == "run-finished")
            more = "".join(
                f.get("contentDelta", "")
                for f in live
                if f["type"] == "message-delta" and f["messageId"] == assistant["id"]
            )
            assert assistant["content"] + more == "Hello", "no gap, no duplicate"


def test_subscribe_while_parked_resends_rate_limited(build_chat_app: Any) -> None:
    resets = datetime.now(UTC) + timedelta(hours=1)
    app, pid = build_chat_app(
        lambda item: [Emit(RateLimited(resets_at=resets)), Finish("error", "rate limited")]
    )
    with TestClient(app) as client:
        with client.websocket_connect("/api/ws") as first:
            send(first, type="subscribe", projectId=pid, sinceMessageId=0)
            send(first, type="send", projectId=pid, clientMessageId="c1", text="hi", attachments=[])
            recv_until(first, lambda f: f["type"] == "rate-limited")
        with client.websocket_connect("/api/ws") as second:
            send(second, type="subscribe", projectId=pid, sinceMessageId=0)
            frames = recv_until(second, lambda f: f["type"] == "rate-limited")
            assert frames[-1]["resetsAt"] == resets.isoformat()
            assert not any(f["type"] == "run-started" for f in frames)


def test_heartbeat_ping(build_chat_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_module, "HEARTBEAT_S", 0.05)
    app, _ = build_chat_app()
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        assert json.loads(sock.receive_text()) == {"type": "ping", "projectId": None}


def test_heartbeat_default_is_25_seconds() -> None:
    assert ws_module.HEARTBEAT_S == 25.0


def test_rejects_unauthenticated_and_foreign_origin(
    build_chat_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = build_chat_app()
    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as bad_origin,
            client.websocket_connect("/api/ws", headers={"origin": "https://evil.example"}) as sock,
        ):
            sock.receive_text()
        assert bad_origin.value.code == 4403
        with client.websocket_connect("/api/ws", headers={"origin": "http://testserver"}) as ok:
            send(ok, type="ping", projectId=None)
            assert json.loads(ok.receive_text())["type"] == "pong"

        async def nobody(conn: Any) -> None:
            return None

        monkeypatch.setattr(ws_module, "authenticate_connection", nobody)
        with (
            pytest.raises(WebSocketDisconnect) as unauth,
            client.websocket_connect("/api/ws") as sock,
        ):
            sock.receive_text()
        assert unauth.value.code == 4401


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: int | None = None
        self.app = SimpleNamespace(state=SimpleNamespace(agent_runtime=None, settings=None))

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


async def test_dropped_slow_subscriber_closes_with_4408() -> None:
    broadcaster = Broadcaster(max_queue=2)
    sub = broadcaster.subscribe("a1b2c3d4e5f6")
    for _ in range(3):  # one more than the queue holds: the subscriber is dropped
        await broadcaster.publish("a1b2c3d4e5f6", PongFrame(project_id=None))
    assert sub.dropped
    sock = _FakeSocket()
    conn = ws_module._Connection(sock)  # type: ignore[arg-type]
    await conn._forward(sub)
    assert sock.closed == 4408
