import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from copperchat.agent.fake import Text, Wait
from copperchat.web import ws as ws_module

from .conftest import CID

IMAGE = "0123456789abcdef0123456789abcdef.png"


@pytest.fixture(autouse=True)
def fake_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok(conn: object) -> str:
        return "admin"

    monkeypatch.setattr(ws_module, "authenticate_connection", ok)


def test_messages_page_has_more_before_and_live_overlay(build_chat_app: Any) -> None:
    gate = asyncio.Event()
    app, pid = build_chat_app(
        lambda item: [Text("Hel"), Wait(gate)] if item.text == "m3" else [Text("ok")]
    )
    with TestClient(app) as client, client.websocket_connect("/api/ws") as sock:
        sock.send_text(
            json.dumps(
                {"type": "subscribe", "projectId": pid, "conversationId": CID, "sinceMessageId": 0}
            )
        )
        for i in (1, 2, 3):
            sock.send_text(
                json.dumps(
                    {
                        "type": "send",
                        "projectId": pid,
                        "conversationId": CID,
                        "clientMessageId": f"c{i}",
                        "text": f"m{i}",
                        "attachments": [],
                    }
                )
            )
        while json.loads(sock.receive_text()).get("contentDelta") != "Hel":
            pass
        page = client.get(
            f"/api/projects/{pid}/conversations/{CID}/messages", params={"limit": 2}
        ).json()
        assert page["hasMore"] is True
        ids = [m["id"] for m in page["messages"]]
        assert ids == sorted(ids) and len(ids) == 2
        assert page["messages"][-1]["content"] == "Hel"  # live overlay of the streaming row
        assert page["messages"][-1]["status"] == "streaming"
        older = client.get(
            f"/api/projects/{pid}/conversations/{CID}/messages",
            params={"limit": 50, "before": ids[0]},
        ).json()
        assert older["hasMore"] is False and all(m["id"] < ids[0] for m in older["messages"])
        assert set(older["messages"][0]) == {
            "id",
            "projectId",
            "conversationId",
            "clientMessageId",
            "runId",
            "role",
            "kind",
            "content",
            "status",
            "metadata",
            "createdAt",
        }
        client.portal.call(gate.set)
        r = client.get(f"/api/projects/{pid}/conversations/{CID}/messages", params={"limit": 0})
        assert r.status_code == 422
        assert (
            client.get(
                f"/api/projects/{pid}/conversations/{CID}/messages", params={"limit": 201}
            ).status_code
            == 422
        )


def test_messages_without_engine_and_bad_project(build_chat_app: Any) -> None:
    app, pid = build_chat_app()
    with TestClient(app) as client:
        page = client.get(f"/api/projects/{pid}/conversations/{CID}/messages").json()
        assert page == {"messages": [], "hasMore": False}
        assert client.app.state.agent_runtime.get(pid) is None  # type: ignore[attr-defined]
        r = client.get(f"/api/projects/0123456789ab/conversations/{CID}/messages")
        assert r.status_code == 404 and r.json()["error"]["code"] == "project_not_found"
        assert (
            client.get(f"/api/projects/NOT-AN-ID/conversations/{CID}/messages").status_code == 400
        )


def test_messages_require_login(build_chat_app: Any) -> None:
    app, pid = build_chat_app(auth=True)
    with TestClient(app) as client:
        assert client.get(f"/api/projects/{pid}/conversations/{CID}/messages").status_code == 401
        assert client.get(f"/api/projects/{pid}/images/{IMAGE}").status_code == 401


def _images_dir(tmp_path: Path) -> Path:
    d = tmp_path / "projects" / "demo" / ".kicad-agent" / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_image_download(build_chat_app: Any, tmp_path: Path) -> None:
    app, pid = build_chat_app()
    (_images_dir(tmp_path) / IMAGE).write_bytes(b"\x89PNG fake")
    with TestClient(app) as client:
        r = client.get(f"/api/projects/{pid}/images/{IMAGE}")
        assert r.status_code == 200 and r.content == b"\x89PNG fake"
        assert r.headers["content-type"] == "image/png"
        assert r.headers["x-content-type-options"] == "nosniff"
        missing = "f" * 32 + ".png"
        assert client.get(f"/api/projects/{pid}/images/{missing}").status_code == 404


@pytest.mark.parametrize(
    "name",
    [
        "..%2F..%2Fdemo.kicad_pro",
        "%2e%2e",
        "secret.png",
        "0123456789ABCDEF0123456789ABCDEF.png",
        "0123456789abcdef0123456789abcdef.png.txt",
        "0123456789abcdef0123456789abcdef.svg",
        "link.png",
    ],
)
def test_image_names_are_traversal_safe(build_chat_app: Any, tmp_path: Path, name: str) -> None:
    app, pid = build_chat_app()
    images = _images_dir(tmp_path)
    (images / "secret.png").write_bytes(b"x")
    (images / "link.png").symlink_to(tmp_path / "projects" / "demo" / "demo.kicad_pro")
    with TestClient(app) as client:
        assert client.get(f"/api/projects/{pid}/images/{name}").status_code == 404


def test_symlinked_image_outside_dir_is_not_served(build_chat_app: Any, tmp_path: Path) -> None:
    app, pid = build_chat_app()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    (_images_dir(tmp_path) / IMAGE).symlink_to(outside)
    with TestClient(app) as client:
        assert client.get(f"/api/projects/{pid}/images/{IMAGE}").status_code == 404
