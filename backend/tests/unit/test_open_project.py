from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.web.app import create_app
from tests.support.fake_kicad import FakeClientFactory, FakeLauncher, FakeServer

GOOD = {"username": "admin", "password": "test-password"}


def markers(root: Path, locked: bool) -> None:
    d = root / "demo"
    d.mkdir(parents=True, exist_ok=True)
    for suffix in ("kicad_pro", "kicad_sch", "kicad_pcb"):
        (d / f"demo.{suffix}").touch()  # empty markers (approved by owner 2026-10-16)
    if locked:
        (d / "~demo.kicad_sch.lck").touch()
        (d / "~demo.kicad_pcb.lck").touch()


def _open(settings: Settings) -> dict[str, object]:
    server = FakeServer()
    app = create_app(
        settings, launcher=FakeLauncher(server), client_factory=FakeClientFactory(server)
    )
    try:
        with TestClient(app) as client:
            client.post("/api/auth/login", json=GOOD)
            project_id = client.get("/api/projects").json()[0]["id"]
            r = client.post(f"/api/projects/{project_id}/open")
            assert r.status_code == 200
            body: dict[str, object] = r.json()
            assert body["id"] == project_id
            assert client.app.state.agent_runtime.get(project_id) is not None  # type: ignore[attr-defined]
            return body
    finally:
        server.release.set()


def test_open_unlocked(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    markers(settings.projects_root, locked=False)
    body = _open(settings)
    assert body["readOnly"] is False
    assert body["lockFiles"] == []
    assert body["kicadVersion"] == "11.0.0-fake"


def test_open_locked_returns_read_only_banner_data(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings()
    markers(settings.projects_root, locked=True)
    body = _open(settings)
    assert body["readOnly"] is True
    assert body["lockFiles"] == ["~demo.kicad_pcb.lck", "~demo.kicad_sch.lck"]
