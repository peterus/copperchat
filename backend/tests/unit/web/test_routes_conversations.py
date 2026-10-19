"""REST /conversations* (spec §6.1, plan S-C4)."""

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from copperchat.agent.fake import Text, Wait
from copperchat.web import ws as ws_module

from .conftest import OTHER_PID


@pytest.fixture(autouse=True)
def fake_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(conn: object) -> str:
        return "admin"

    monkeypatch.setattr(ws_module, "authenticate_connection", ok)


def _create(client: TestClient, pid: str, **body: Any) -> dict[str, Any]:
    r = client.post(f"/api/projects/{pid}/conversations", json={"type": "free", **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_list_rename_archive(build_chat_app: Any) -> None:
    app, pid = build_chat_app(default_conversations=False)
    with TestClient(app) as client:
        assert client.get(f"/api/projects/{pid}/conversations").json() == []
        a = _create(client, pid)
        b = _create(client, pid, type="requirements", title="Specs")
        assert set(a) == {
            "id",
            "seq",
            "title",
            "type",
            "status",
            "archived",
            "createdAt",
            "lastMessageAt",
            "closedAt",
            "usage",
        }
        assert (a["seq"], a["title"], a["type"], a["status"], a["archived"]) == (
            1,
            "Session 1",
            "free",
            "open",
            False,
        )
        assert a["usage"] == {"inputTokens": 0, "outputTokens": 0, "costUsd": 0}
        assert (b["seq"], b["title"], b["type"]) == (2, "Specs", "requirements")
        listed = client.get(f"/api/projects/{pid}/conversations").json()
        assert [c["id"] for c in listed] == [b["id"], a["id"]]  # newest activity first
        r = client.patch(f"/api/projects/{pid}/conversations/{a['id']}", json={"title": "Main"})
        assert r.status_code == 200 and r.json()["title"] == "Main"
        r = client.patch(f"/api/projects/{pid}/conversations/{b['id']}", json={"archived": True})
        assert r.status_code == 200 and r.json()["archived"] is True
        assert [c["id"] for c in client.get(f"/api/projects/{pid}/conversations").json()] == [
            a["id"]
        ]
        archived = client.get(f"/api/projects/{pid}/conversations", params={"archived": 1})
        assert [c["id"] for c in archived.json()] == [b["id"]]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"type": "board"}, 400),
        ({"type": "chat"}, 422),
        ({"type": "free", "title": "a\nb"}, 422),
        ({"type": "free", "title": ""}, 422),
        ({"type": "free", "title": "x" * 121}, 422),
        ({"type": "free", "title": "   "}, 422),
        ({"type": "free", "title": "a\u202eb"}, 422),
        ({"type": "free", "extra": 1}, 422),
        ({}, 422),
    ],
)
def test_create_validation(build_chat_app: Any, body: dict[str, Any], status: int) -> None:
    app, pid = build_chat_app(default_conversations=False)
    with TestClient(app) as client:
        r = client.post(f"/api/projects/{pid}/conversations", json=body)
        assert r.status_code == status, r.text
        assert client.get(f"/api/projects/{pid}/conversations").json() == []


def test_lone_surrogate_title_is_422_not_500(build_chat_app: Any) -> None:
    app, pid = build_chat_app(default_conversations=False)
    with TestClient(app) as client:
        r = client.post(
            f"/api/projects/{pid}/conversations",
            content='{"type": "free", "title": "a\\ud800b"}',
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 422, r.text


def test_patch_validation(build_chat_app: Any) -> None:
    app, pid = build_chat_app(default_conversations=False)
    with TestClient(app) as client:
        a = _create(client, pid)
        url = f"/api/projects/{pid}/conversations/{a['id']}"
        assert client.patch(url, json={"title": "two\nlines"}).status_code == 422
        assert client.patch(url, json={"status": "closed"}).status_code == 422
        assert client.patch(url, json={"archived": "yes"}).status_code == 422


def test_foreign_conversation_is_404(build_chat_app: Any) -> None:
    app, pid = build_chat_app(default_conversations=False)
    with TestClient(app) as client:
        other = _create(client, OTHER_PID)
        for method, path, kw in (
            ("patch", f"/conversations/{other['id']}", {"json": {"title": "x"}}),
            ("get", f"/conversations/{other['id']}/messages", {}),
            ("get", f"/conversations/{'0' * 32}/messages", {}),
            ("patch", "/conversations/not-an-id", {"json": {"title": "x"}}),
        ):
            r = getattr(client, method)(f"/api/projects/{pid}{path}", **kw)
            assert r.status_code == 404, (path, r.text)
            assert r.json()["error"]["code"] == "conversation_not_found"
        r = client.get(f"/api/projects/{OTHER_PID}/conversations/{other['id']}/messages")
        assert r.status_code == 200 and r.json() == {"messages": [], "hasMore": False}


def test_unknown_project_is_404(build_chat_app: Any) -> None:
    app, _ = build_chat_app(default_conversations=False)
    with TestClient(app) as client:
        assert client.get("/api/projects/0123456789ab/conversations").status_code == 404


def test_messages_paging_per_conversation(build_chat_app: Any) -> None:
    gate = asyncio.Event()
    app, pid = build_chat_app(
        lambda item: [Text("Hel"), Wait(gate)] if item.text == "a3" else [Text("ok")],
        default_conversations=False,
    )
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        a, b = _create(client, pid), _create(client, pid)
        sock.send_text(
            json.dumps(
                {
                    "type": "subscribe",
                    "projectId": pid,
                    "conversationId": a["id"],
                    "sinceMessageId": 0,
                }
            )
        )
        for cid, text in ((b["id"], "b1"), (a["id"], "a1"), (a["id"], "a2"), (a["id"], "a3")):
            sock.send_text(
                json.dumps(
                    {
                        "type": "send",
                        "projectId": pid,
                        "conversationId": cid,
                        "clientMessageId": text,
                        "text": text,
                        "attachments": [],
                    }
                )
            )
        while json.loads(sock.receive_text()).get("contentDelta") != "Hel":
            pass
        url = f"/api/projects/{pid}/conversations/{a['id']}/messages"
        page = client.get(url, params={"limit": 2}).json()
        assert page["hasMore"] is True and len(page["messages"]) == 2
        assert page["messages"][-1]["content"] == "Hel"  # live overlay of the streaming row
        assert all(m["conversationId"] == a["id"] for m in page["messages"])
        older = client.get(url, params={"limit": 50, "before": page["messages"][0]["id"]}).json()
        assert all(m["conversationId"] == a["id"] for m in older["messages"])
        texts = [m["content"] for m in older["messages"] if m["role"] == "user"]
        assert "b1" not in texts and "a1" in texts
        bpage = client.get(f"/api/projects/{pid}/conversations/{b['id']}/messages").json()
        assert [m["content"] for m in bpage["messages"] if m["role"] == "user"] == ["b1"]
        assert client.get(f"/api/projects/{pid}/messages").status_code in (404, 405)
        gate.set()


def test_routes_need_a_session(build_chat_app: Any) -> None:
    app, pid = build_chat_app(auth=True, default_conversations=False)
    cid = "0" * 32
    with TestClient(app) as client:
        for method, path, kw in (
            ("get", "/conversations", {}),
            ("post", "/conversations", {"json": {"type": "free"}}),
            ("patch", f"/conversations/{cid}", {"json": {"title": "x"}}),
            ("get", f"/conversations/{cid}/messages", {}),
        ):
            r = getattr(client, method)(f"/api/projects/{pid}{path}", **kw)
            assert r.status_code == 401, path


def test_create_rename_archive_publish_conversation_updated(build_chat_app: Any) -> None:
    app, pid = build_chat_app(default_conversations=False)
    with TestClient(app) as client:
        frames: list[Any] = []
        a = _create(client, pid)
        sub = app.state.agent_runtime.broadcaster.subscribe(pid)
        b = _create(client, pid)
        client.patch(f"/api/projects/{pid}/conversations/{a['id']}", json={"title": "Main"})
        client.patch(f"/api/projects/{pid}/conversations/{b['id']}", json={"archived": True})

        async def drain() -> None:
            while True:
                try:
                    frames.append(await asyncio.wait_for(sub.__anext__(), 0.05))
                except (TimeoutError, StopAsyncIteration):
                    return

        client.portal.call(drain)  # type: ignore[union-attr]
        updates = [
            (f.conversation.id, f.conversation.title, f.conversation.archived) for f in frames
        ]
        assert updates == [
            (b["id"], "Session 2", False),
            (a["id"], "Main", False),
            (b["id"], "Session 2", True),
        ]
        assert all(f.type == "conversation-updated" and f.project_id == pid for f in frames)
