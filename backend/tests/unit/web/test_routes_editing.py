"""GET|PUT /api/projects/{id}/settings/editing."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.web.app import create_app

GOOD = {"username": "admin", "password": "test-password"}
DEFAULTS = {
    "planPolicy": "large",
    "changeBudget": 20,
    "annotationBlock": 100,
    "showAgentView": True,
}


def _project(root: Path) -> None:
    for rel in ("demo/demo.kicad_pro", "demo/demo.kicad_sch"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).touch()


def _login(client: TestClient) -> str:
    assert client.post("/api/auth/login", json=GOOD).status_code < 300
    (project,) = client.get("/api/projects").json()
    return str(project["id"])


def test_get_defaults(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    _project(settings.projects_root)
    with TestClient(create_app(settings)) as client:
        pid = _login(client)
        r = client.get(f"/api/projects/{pid}/settings/editing")
    assert r.status_code == 200
    assert r.json() == DEFAULTS


def test_put_partial_update_persists(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    _project(settings.projects_root)
    with TestClient(create_app(settings)) as client:
        pid = _login(client)
        url = f"/api/projects/{pid}/settings/editing"
        r = client.put(url, json={"changeBudget": 50})
        assert r.status_code == 200
        assert r.json() == {**DEFAULTS, "changeBudget": 50}
        r = client.put(url, json={"planPolicy": "always", "showAgentView": False})
        expected = {**DEFAULTS, "changeBudget": 50, "planPolicy": "always", "showAgentView": False}
        assert r.json() == expected
        assert client.get(url).json() == expected


@pytest.mark.parametrize(
    "body",
    [
        {"planPolicy": "sometimes"},
        {"planPolicy": []},
        {"planPolicy": {"x": 1}},
        {"changeBudget": 0},
        {"changeBudget": 501},
        {"annotationBlock": 10},
    ],
)
def test_put_invalid_values_400(
    make_settings: Callable[..., Settings], body: dict[str, Any]
) -> None:
    settings = make_settings()
    _project(settings.projects_root)
    with TestClient(create_app(settings)) as client:
        pid = _login(client)
        url = f"/api/projects/{pid}/settings/editing"
        r = client.put(url, json=body)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_argument"
        assert client.get(url).json() == DEFAULTS


def test_requires_login(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        url = "/api/projects/0123456789ab/settings/editing"
        assert client.get(url).status_code == 401
        assert client.put(url, json={"changeBudget": 5}).status_code == 401


def test_unknown_project_404(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        client.post("/api/auth/login", json=GOOD)
        url = "/api/projects/0123456789ab/settings/editing"
        assert client.get(url).status_code == 404
        assert client.put(url, json={"changeBudget": 5}).status_code == 404
