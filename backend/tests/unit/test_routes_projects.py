from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.web.app import create_app

GOOD = {"username": "admin", "password": "test-password"}


def marker(root: Path, rel: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).touch()


def test_list_requires_login(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/api/projects").status_code == 401


def test_list_projects_camel_case(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    marker(settings.projects_root, "boards/amp/amp.kicad_pro")
    marker(settings.projects_root, "boards/amp/amp.kicad_sch")
    marker(settings.projects_root, "boards/amp/~amp.kicad_sch.lck")
    with TestClient(create_app(settings)) as client:
        client.post("/api/auth/login", json=GOOD)
        body = client.get("/api/projects").json()
    assert len(body) == 1
    item = body[0]
    assert set(item) == {"id", "name", "relPath", "hasSch", "hasPcb", "locked"}
    assert item["relPath"] == "boards/amp/amp.kicad_pro"
    assert (item["hasSch"], item["hasPcb"], item["locked"]) == (True, False, True)


@pytest.mark.parametrize(
    "bad_id", ["..%2F..%2Fetc", "%2e%2e", "ABCDEF123456", "x" * 12, "a1b2c3d4e5f6%00"]
)
def test_open_rejects_traversal_ids(make_settings: Callable[..., Settings], bad_id: str) -> None:
    with TestClient(create_app(make_settings())) as client:
        client.post("/api/auth/login", json=GOOD)
        r = client.post(f"/api/projects/{bad_id}/open")
        assert r.status_code in (400, 404)
        # Ids with percent-encoding may be decoded/normalised by the router before
        # reaching our validator (404 is acceptable there); plain ids must be 400.
        if "%" not in bad_id:
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "invalid_project_id"


def test_open_unknown_id_404(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        client.post("/api/auth/login", json=GOOD)
        r = client.post("/api/projects/0123456789ab/open")
        assert r.status_code == 404
