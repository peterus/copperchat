"""Opt-in live tests against the real CLI/model (``uv run pytest -m live_sdk``).

Credentials are read at import time: the autouse env cleaner of tests/conftest.py removes
them from the environment before each test runs. CI has none, so these are skipped there.
"""

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from copperchat.config import Settings

TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
HAVE_CREDENTIALS = bool(TOKEN or API_KEY)

live = [
    pytest.mark.live_sdk,
    pytest.mark.skipif(not HAVE_CREDENTIALS, reason="needs model credentials"),
]


def live_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        projects_root=tmp_path,
        data_dir=tmp_path / "data",
        claude_code_oauth_token=SecretStr(TOKEN) if TOKEN else None,
        anthropic_api_key=SecretStr(API_KEY) if API_KEY and not TOKEN else None,
    )
