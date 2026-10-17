import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from copperchat.config import Settings
from copperchat.errors import MissingCredentialsError
from copperchat.projects import project_id_for
from copperchat.web.app import create_app
from copperchat.web.auth import COOKIE_NAME

GOOD = {"username": "admin", "password": "test-password"}


def _demo(settings: Settings) -> str:
    (settings.projects_root / "demo").mkdir()
    (settings.projects_root / "demo" / "demo.kicad_pro").write_text("")
    return project_id_for("demo/demo.kicad_pro")


def test_startup_requires_credentials_unless_fake_agent(
    make_settings: Callable[..., Settings],
) -> None:
    with (
        pytest.raises(MissingCredentialsError),
        TestClient(create_app(make_settings(claude_code_oauth_token=None))),
    ):
        pass
    settings = make_settings(claude_code_oauth_token=None, dev_fake_agent=True)
    with TestClient(create_app(settings)) as client:
        assert client.app.state.agent_runtime is not None  # type: ignore[attr-defined]


def _round_trip(client: TestClient, pid: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    with client.websocket_connect("/api/ws", headers=headers) as sock:
        sock.send_text(json.dumps({"type": "subscribe", "projectId": pid, "sinceMessageId": 0}))
        sock.send_text(
            json.dumps(
                {
                    "type": "send",
                    "projectId": pid,
                    "clientMessageId": "c1",
                    "text": "hello",
                    "attachments": [],
                }
            )
        )
        seen: list[dict[str, Any]] = []
        while not seen or seen[-1]["type"] != "run-finished":
            frame = json.loads(sock.receive_text())
            if frame["type"] != "ping":
                seen.append(frame)
    return seen


def test_cookie_authenticated_chat_round_trip(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(dev_fake_agent=True)
    pid = _demo(settings)
    with TestClient(create_app(settings)) as client:
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect("/api/ws") as s,
        ):
            s.receive_text()
        assert exc.value.code == 4401
        bogus = {"cookie": f"{COOKIE_NAME}=forged"}
        with (
            pytest.raises(WebSocketDisconnect) as forged,
            client.websocket_connect("/api/ws", headers=bogus) as s,
        ):
            s.receive_text()
        assert forged.value.code == 4401
        assert client.post("/api/auth/login", json=GOOD).status_code == 204
        cookie = {"cookie": f"{COOKIE_NAME}={client.cookies.get(COOKIE_NAME)}"}
        seen = _round_trip(client, pid, cookie)
        assert any(f["type"] == "usage" for f in seen)
        page = client.get(f"/api/projects/{pid}/messages").json()
        assert [m["role"] for m in page["messages"]] == ["user", "assistant"]
        assert page["messages"][1]["content"] == "You said: hello"
        assert client.post("/api/auth/logout").status_code == 204
        with (
            pytest.raises(WebSocketDisconnect) as after_logout,
            client.websocket_connect("/api/ws", headers=cookie) as s,
        ):
            s.receive_text()
        assert after_logout.value.code == 4401


def test_logout_closes_an_open_socket(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(dev_fake_agent=True)
    _demo(settings)
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/auth/login", json=GOOD).status_code == 204
        cookie = {"cookie": f"{COOKIE_NAME}={client.cookies.get(COOKIE_NAME)}"}
        with client.websocket_connect("/api/ws", headers=cookie) as sock:
            sock.send_text(json.dumps({"type": "ping", "projectId": None}))
            assert json.loads(sock.receive_text())["type"] == "pong"
            assert client.post("/api/auth/logout").status_code == 204
            sock.send_text(json.dumps({"type": "ping", "projectId": None}))
            with pytest.raises(WebSocketDisconnect) as exc:
                sock.receive_text()
            assert exc.value.code == 4401


def test_chat_routes_take_precedence_over_frontend(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>CopperChat</title>")
    settings = make_settings(dev_fake_agent=True, frontend_dist=dist)
    pid = _demo(settings)
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/projects/{pid}/messages").status_code == 401
        client.post("/api/auth/login", json=GOOD)
        assert client.get(f"/api/projects/{pid}/messages").json()["hasMore"] is False
        cookie = {"cookie": f"{COOKIE_NAME}={client.cookies.get(COOKIE_NAME)}"}
        assert _round_trip(client, pid, cookie)[-1]["reason"] == "completed"


def test_queued_message_runs_after_restart_without_any_client(
    make_settings: Callable[..., Settings],
) -> None:
    import asyncio
    import time

    from copperchat.db import Database
    from copperchat.db.repos.messages import MessageRepo

    settings = make_settings(dev_fake_agent=True)
    pid = _demo(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    async def seed() -> int:
        db = await Database.open(settings.data_dir / "copperchat.db")
        row, _ = await MessageRepo(db).insert_user(pid, "c1", "left over")
        await db.close()
        return row.id

    mid = asyncio.run(seed())
    with TestClient(create_app(settings)) as client:
        client.post("/api/auth/login", json=GOOD)
        deadline = time.monotonic() + 3
        while True:
            rows = client.get(f"/api/projects/{pid}/messages").json()["messages"]
            if [r["status"] for r in rows] == ["done", "done"]:
                break
            assert time.monotonic() < deadline, rows
            time.sleep(0.02)
        assert rows[0]["id"] == mid and rows[1]["content"] == "You said: left over"
