"""The Board session type is gated by ``COPPERCHAT_BOARD_TOOLS`` (SP4 P1c)."""

from typing import Any

from fastapi.testclient import TestClient


def test_board_400_when_flag_off(build_chat_app: Any) -> None:
    app, pid = build_chat_app(default_conversations=False, settings_kw={"board_tools": False})
    with TestClient(app) as client:
        r = client.post(f"/api/projects/{pid}/conversations", json={"type": "board"})
        assert r.status_code == 400, r.text
        assert r.json()["error"]["code"] == "session_type_unavailable"
        assert client.get(f"/api/projects/{pid}/conversations").json() == []


def test_board_201_when_flag_on(build_chat_app: Any) -> None:
    app, pid = build_chat_app(default_conversations=False, settings_kw={"board_tools": True})
    with TestClient(app) as client:
        r = client.post(f"/api/projects/{pid}/conversations", json={"type": "board"})
        assert r.status_code == 201, r.text
        assert r.json()["type"] == "board"
        listed = client.get(f"/api/projects/{pid}/conversations").json()
        assert [c["type"] for c in listed] == ["board"]


def test_board_201_by_default(build_chat_app: Any) -> None:
    """SP4 P9b: a default install offers the Board session type."""
    app, pid = build_chat_app(default_conversations=False)
    with TestClient(app) as client:
        r = client.post(f"/api/projects/{pid}/conversations", json={"type": "board"})
        assert r.status_code == 201, r.text
