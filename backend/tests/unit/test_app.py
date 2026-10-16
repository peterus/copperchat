from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copperchat.config import Settings
from copperchat.errors import MissingCredentialsError
from copperchat.web.app import create_app


def test_startup_fails_without_credentials(make_settings: Callable[..., Settings]) -> None:
    with (
        pytest.raises(MissingCredentialsError, match="claude setup-token"),
        TestClient(create_app(make_settings(claude_code_oauth_token=None))),
    ):
        pass


def test_health_without_kicad(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "kicad": None}


def test_health_reports_kicad_version(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    fake_cli = tmp_path / "fake-kicad-cli"
    fake_cli.write_text("#!/bin/sh\necho 10.99.0-test\n")
    fake_cli.chmod(0o755)
    with TestClient(create_app(make_settings(kicad_cli=str(fake_cli)))) as client:
        assert client.get("/api/health").json()["kicad"] == "10.99.0-test"


def test_frontend_served_when_dist_exists(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>CopperChat</title>")
    with TestClient(create_app(make_settings(frontend_dist=dist))) as client:
        assert "CopperChat" in client.get("/").text
        assert client.get("/api/health").status_code == 200


def test_no_frontend_returns_404_on_root(make_settings: Callable[..., Settings]) -> None:
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/").status_code == 404
