"""``GET /api/features``: the feature flags the frontend needs (SP4 P1c)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

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
