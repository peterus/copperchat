from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from copperchat.errors import MissingCredentialsError

MISSING_CREDENTIALS_MESSAGE = (
    "No model credentials configured. Run `claude setup-token` on a machine where the "
    "Claude CLI is logged in to your own subscription and put the printed token into "
    "CLAUDE_CODE_OAUTH_TOKEN in your .env (or set ANTHROPIC_API_KEY). CopperChat has no "
    "login flow of its own; Anthropic's terms apply to your credentials."
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    projects_root: Path = Path("/projects")
    data_dir: Path = Path("/data")
    admin_username: str = "admin"
    admin_password: SecretStr | None = None
    session_secret: SecretStr | None = None
    claude_code_oauth_token: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    kicad_cli: str = "kicad-cli"
    kicad_api_timeout_s: float = 30.0
    kicad_start_timeout_s: float = 60.0
    host: str = "0.0.0.0"
    port: int = 8080
    public_base_url: str | None = None
    dev_fake_agent: bool = False
    # Extensions (Phase 3)
    cookie_secure: bool = True
    login_max_failures: int = 5
    login_window_s: float = 60.0
    frontend_dist: Path = Path("/app/frontend/dist")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def _present(secret: SecretStr | None) -> bool:
    return secret is not None and secret.get_secret_value().strip() != ""


def check_model_credentials(s: Settings) -> None:
    """Fail fast at startup when no model credential is configured.

    Expired tokens can only be detected on the first model call; Phase 5 maps that
    runtime error to the same message.
    """
    if s.dev_fake_agent:
        return
    if _present(s.claude_code_oauth_token) or _present(s.anthropic_api_key):
        return
    raise MissingCredentialsError(MISSING_CREDENTIALS_MESSAGE)
