from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from kipy.errors import ApiError

from copperchat.config import Settings
from copperchat.projects import agent_dir, discover_projects
from copperchat.web.app import create_app
from tests.support.fake_kicad import FakeClientFactory, FakeDocument, FakeLauncher, FakeServer
from tests.support.kicad_project import copy_fixture

GOOD = {"username": "admin", "password": "test-password"}


@contextmanager
def app_client(
    make_settings: Callable[..., Settings], server: FakeServer | None = None
) -> Iterator[tuple[TestClient, str]]:
    settings = make_settings()
    copy_fixture("ecc83", settings.projects_root)  # KiCad-generated files, served opaque
    project = discover_projects(settings.projects_root)[0]
    server = server or FakeServer()
    app = create_app(
        settings, launcher=FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    with TestClient(app) as client:
        yield client, project.id


def test_open_writes_snapshots_and_lists_view_files(make_settings: Callable[..., Settings]) -> None:
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        body = client.post(f"/api/projects/{pid}/open").json()
        assert body["viewFiles"] == {"sch": ["ecc83-pp.kicad_sch"], "pcb": ["ecc83-pp.kicad_pcb"]}
        r = client.get(f"/api/projects/{pid}/view/pcb")  # X1: 200 right after open
        assert r.status_code == 200 and r.content.startswith(b"(kicad_pcb")
        etag = r.headers["etag"]
        assert etag.startswith('"0.') and r.headers["cache-control"] == "no-cache"
        again = client.get(f"/api/projects/{pid}/view/pcb", headers={"If-None-Match": etag})
        assert again.status_code == 304
        f = client.get(f"/api/projects/{pid}/view/files/ecc83-pp.kicad_sch")
        assert f.status_code == 200 and f.content.startswith(b"(kicad_sch")
        assert client.get(f"/api/projects/{pid}/view/xyz").status_code == 400


def test_view_files_are_traversal_safe(make_settings: Callable[..., Settings]) -> None:
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        client.post(f"/api/projects/{pid}/open")
        for bad in (
            "..%2Fecc83-pp.kicad_sch",
            "..%2F..%2Fecc83%2Fecc83-pp.kicad_sch",
            "%2Fetc%2Fpasswd.kicad_sch",
            "ecc83-pp.kicad_pro",
            "x.txt",
            "missing.kicad_sch",
        ):
            assert client.get(f"/api/projects/{pid}/view/files/{bad}").status_code == 404, bad


def test_view_needs_open_project(make_settings: Callable[..., Settings]) -> None:
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        r = client.get(f"/api/projects/{pid}/view/pcb")
        assert r.status_code == 409 and r.json()["error"]["code"] == "project_not_open"
        assert client.get("/api/projects/000000000000/view/pcb").status_code == 404


def test_renders_traversal_rejected(make_settings: Callable[..., Settings]) -> None:
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        for bad in ("..%2F..%2Fetc%2Fpasswd", ".png", "a%00.png", "A" * 32 + ".png"):
            assert client.get(f"/api/projects/{pid}/renders/{bad}").status_code in (400, 404)
        assert client.get(f"/api/projects/{pid}/renders/x.txt").status_code == 400
        name = "0123456789abcdef0123456789abcdef.png"
        assert client.get(f"/api/projects/{pid}/renders/{name}").status_code == 404
        project = discover_projects(client.app.state.settings.projects_root)[0]  # type: ignore[attr-defined]
        (agent_dir(project) / "renders").mkdir(parents=True)
        (agent_dir(project) / "renders" / name).write_bytes(b"\x89PNG")
        r = client.get(f"/api/projects/{pid}/renders/{name}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_agent_view_validates_parameters(make_settings: Callable[..., Settings]) -> None:
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        client.post(f"/api/projects/{pid}/open")
        for query in ("doc=x", "mode=fancy", "theme=pink"):
            assert client.get(f"/api/projects/{pid}/agent-view?{query}").status_code == 400


def test_requires_login(make_settings: Callable[..., Settings]) -> None:
    with app_client(make_settings) as (client, pid):
        for path in ("view/pcb", "view/files/ecc83-pp.kicad_pcb", "agent-view", "renders/x.png"):
            assert client.get(f"/api/projects/{pid}/{path}").status_code == 401


def test_open_survives_a_failing_snapshot(make_settings: Callable[..., Settings]) -> None:
    # A sub-sheet outside the project directory cannot be copied into the view dir.
    server = FakeServer(hierarchy=["ecc83-pp.kicad_sch", "../elsewhere.kicad_sch"])
    with app_client(make_settings, server) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        r = client.post(f"/api/projects/{pid}/open")
        assert r.status_code == 200
        assert r.json()["viewFiles"] == {"sch": [], "pcb": []}


def test_open_survives_a_kicad_save_error(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: FakeDocument, filename: str, **_: object) -> None:
        raise ApiError("KiCad returned error: cannot save")

    monkeypatch.setattr(FakeDocument, "save_as", refuse)
    with app_client(make_settings) as (client, pid):
        client.post("/api/auth/login", json=GOOD)
        r = client.post(f"/api/projects/{pid}/open")
        assert r.status_code == 200 and r.json()["viewFiles"] == {"sch": [], "pcb": []}
