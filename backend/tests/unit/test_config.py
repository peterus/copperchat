import re
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


@pytest.mark.parametrize(
    ("value", "expected"), [("off", False), ("on", True), ("false", False), ("true", True)]
)
def test_footprint_tools_env(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("COPPERCHAT_FOOTPRINT_TOOLS", value)
    assert Settings(_env_file=None).footprint_tools is expected  # type: ignore[call-arg]


def test_footprint_tools_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COPPERCHAT_FOOTPRINT_TOOLS", raising=False)
    assert Settings(_env_file=None).footprint_tools is True  # type: ignore[call-arg]


@pytest.mark.parametrize(("value", "expected"), [("on", True), ("true", True), ("off", False)])
def test_board_tools_env(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("COPPERCHAT_BOARD_TOOLS", value)
    assert Settings(_env_file=None).board_tools is expected  # type: ignore[call-arg]


def test_board_tools_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COPPERCHAT_BOARD_TOOLS", raising=False)
    assert Settings(_env_file=None).board_tools is False  # type: ignore[call-arg]


def test_freerouting_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COPPERCHAT_FREEROUTING", raising=False)
    monkeypatch.delenv("COPPERCHAT_FREEROUTING_HOME", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.freerouting is True
    assert s.freerouting_home == Path("/opt/freerouting")


def test_freerouting_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COPPERCHAT_FREEROUTING", "off")
    monkeypatch.setenv("COPPERCHAT_FREEROUTING_HOME", "/x")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.freerouting is False
    assert s.freerouting_home == Path("/x")


WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
CI_YML = WORKFLOWS / "ci.yml"
KICAD_BASE_YML = WORKFLOWS / "kicad-base.yml"


def _kicad_job_steps() -> list[str]:
    """The kicad job's steps as raw text blocks (regex over ci.yml: PyYAML is no dependency)."""
    text = CI_YML.read_text()
    job = re.search(r"^  kicad:\n(.*?)(?=^  \S)", text, re.M | re.S)
    assert job, "no kicad job in ci.yml"
    steps = re.search(r"^    steps:\n(.*)", job.group(1), re.M | re.S)
    assert steps, "kicad job has no steps"
    return [s for s in re.split(r"^      - ", steps.group(1), flags=re.M) if s.strip()]


def test_ci_kicad_job_fetches_freerouting() -> None:
    steps = _kicad_job_steps()

    def index(pattern: str) -> int:
        found = [i for i, s in enumerate(steps) if re.search(pattern, s, re.M)]
        assert len(found) == 1, f"expected one kicad step matching {pattern!r}, got {found}"
        return found[0]

    env = index(r"COPPERCHAT_FREEROUTING_HOME=")
    assert re.search(
        r'echo "COPPERCHAT_FREEROUTING_HOME=\$GITHUB_WORKSPACE/\.cache/freerouting"'
        r' >> "\$GITHUB_ENV"',
        steps[env],
    ), steps[env]
    assert "github.workspace" not in steps[env]

    cache = index(r"uses: actions/cache@")
    assert re.search(r"uses: actions/cache@[0-9a-f]{40} # v\d+\.\d+\.\d+$", steps[cache], re.M)
    pinned = re.findall(
        r"actions/cache/(?:restore|save)@([0-9a-f]{40})", KICAD_BASE_YML.read_text()
    )
    assert pinned and set(pinned) == {re.findall(r"actions/cache@([0-9a-f]{40})", steps[cache])[0]}
    # only the hash-verified downloads are cached; the installed tree is always re-extracted
    assert re.search(r"^\s+path: \.cache/freerouting/\.dl\s*$", steps[cache], re.M)
    assert re.search(
        r"^\s+key: freerouting-\$\{\{ hashFiles\('docker/freerouting\.lock'\) \}\}\s*$",
        steps[cache],
        re.M,
    )

    fetch = index(r"scripts/fetch-freerouting\.sh")
    assert 'bash scripts/fetch-freerouting.sh "$COPPERCHAT_FREEROUTING_HOME"' in steps[fetch]
    assert re.search(r"^\s+working-directory: \.\s*$", steps[fetch], re.M)
    assert not re.search(r"^\s*if:", steps[fetch], re.M)

    pytest_step = index(r"uv run pytest")
    assert env < cache < fetch < pytest_step
