from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from copperchat.config import Settings
from tests.support.kicad_env import api_server_available
from tests.support.kicad_project import ecc83_session, hier_session

__all__ = ["ecc83_session", "hier_session"]  # real api-server fixtures (tests.support)

# Every Settings field can be set from the environment; a host that exports e.g.
# PORT or DATA_DIR must not change what the tests see.
_SETTINGS_ENV = tuple(name.upper() for name in Settings.model_fields)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never let the developer's real credentials or settings leak into tests."""
    for name in _SETTINGS_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def _make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "projects_root": tmp_path / "projects",
            "data_dir": tmp_path / "data",
            "admin_password": "test-password",
            "claude_code_oauth_token": "test-token",
            "cookie_secure": False,
            "frontend_dist": tmp_path / "no-frontend",
            "kicad_cli": "kicad-cli-not-installed",
            "kicad_api_timeout_s": 2.0,
            "kicad_start_timeout_s": 3.0,
        }
        values.update(overrides)
        (values["projects_root"]).mkdir(parents=True, exist_ok=True)
        return Settings(_env_file=None, **values)  # type: ignore[call-arg]

    return _make


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not any(item.get_closest_marker("kicad") for item in items):
        return
    if api_server_available():
        return
    if os.environ.get("COPPERCHAT_REQUIRE_KICAD") == "1":
        raise pytest.UsageError("COPPERCHAT_REQUIRE_KICAD=1 but `kicad-cli api-server` is missing")
    skip = pytest.mark.skip(reason="`kicad-cli api-server` (KiCad 11 master) not available")
    for item in items:
        if item.get_closest_marker("kicad"):
            item.add_marker(skip)
