from collections.abc import Callable
from pathlib import Path

import pytest

from copperchat.config import Settings, check_model_credentials
from copperchat.errors import MissingCredentialsError, StructuredToolError


def test_defaults_match_contract(tmp_path: Path) -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.projects_root == Path("/projects")
    assert s.data_dir == Path("/data")
    assert s.admin_username == "admin"
    assert s.kicad_cli == "kicad-cli"
    assert s.kicad_api_timeout_s == 30.0
    assert s.kicad_start_timeout_s == 60.0
    assert (s.host, s.port) == ("0.0.0.0", 8080)
    assert s.cookie_secure is True
    assert s.dev_fake_agent is False


def test_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-xyz")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.claude_code_oauth_token is not None
    assert s.claude_code_oauth_token.get_secret_value() == "sk-ant-oat-xyz"
    assert s.cookie_secure is False


def test_missing_credentials_points_to_setup_token(
    make_settings: Callable[..., Settings],
) -> None:
    s = make_settings(claude_code_oauth_token=None, anthropic_api_key=None)
    with pytest.raises(MissingCredentialsError) as info:
        check_model_credentials(s)
    assert "claude setup-token" in str(info.value)


def test_blank_token_counts_as_missing(make_settings: Callable[..., Settings]) -> None:
    s = make_settings(claude_code_oauth_token="   ")
    with pytest.raises(MissingCredentialsError):
        check_model_credentials(s)


@pytest.mark.parametrize("field", ["claude_code_oauth_token", "anthropic_api_key"])
def test_either_credential_is_enough(make_settings: Callable[..., Settings], field: str) -> None:
    values: dict[str, str | None] = {"claude_code_oauth_token": None, field: "secret"}
    s = make_settings(**values)
    check_model_credentials(s)


def test_fake_agent_skips_check(make_settings: Callable[..., Settings]) -> None:
    check_model_credentials(make_settings(claude_code_oauth_token=None, dev_fake_agent=True))


def test_structured_tool_error_dict() -> None:
    err = StructuredToolError(
        "Symbol R99 not found", "No symbol has that reference", "Did you mean: R1, R9?"
    )
    assert err.to_dict() == {
        "what": "Symbol R99 not found",
        "why": "No symbol has that reference",
        "suggestion": "Did you mean: R1, R9?",
    }
