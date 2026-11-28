"""``GET /api/features``: the feature flags the frontend needs (SP4 P1c)."""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.web.app import create_app
from copperchat.web.routes_conversations import features_router


def test_requires_auth(build_chat_app: Any) -> None:
    app, _ = build_chat_app(auth=True)
    app.include_router(features_router)
    with TestClient(app) as client:
        assert client.get("/api/features").status_code == 401


@pytest.mark.parametrize("flag", [False, True])
def test_reports_flag(build_chat_app: Any, flag: bool) -> None:
    app, _ = build_chat_app(settings_kw={"board_tools": flag})
    app.include_router(features_router)
    with TestClient(app) as client:
        r = client.get("/api/features")
        assert r.status_code == 200, r.text
        assert r.json() == {"boardTools": flag}


@pytest.mark.parametrize("flag", [False, True])
def test_mounted_by_create_app(make_settings: Callable[..., Settings], flag: bool) -> None:
    """The real app serves the route (not only a hand-mounted router)."""
    with TestClient(create_app(make_settings(board_tools=flag))) as client:
        assert client.get("/api/features").status_code == 401
        client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
        r = client.get("/api/features")
        assert r.status_code == 200, r.text
        assert r.json() == {"boardTools": flag}


def test_default_is_on(make_settings: Callable[..., Settings]) -> None:
    """SP4 P9b: board tools are on unless COPPERCHAT_BOARD_TOOLS=off."""
    with TestClient(create_app(make_settings())) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "test-password"})
        assert client.get("/api/features").json() == {"boardTools": True}
