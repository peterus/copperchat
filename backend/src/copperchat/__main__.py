from __future__ import annotations

import logging
import sys

import uvicorn

from copperchat.config import check_model_credentials, get_settings
from copperchat.errors import ConfigError
from copperchat.web.app import create_app


def configure_logging() -> None:
    """uvicorn configures only its own loggers, so app records had no handler and anything
    below WARNING was dropped. Give the root a stderr handler (unless something already set
    one up) and let copperchat.* log at INFO; other libraries stay at WARNING."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s:     %(name)s: %(message)s")
    app = logging.getLogger("copperchat")
    if app.level == logging.NOTSET:
        app.setLevel(logging.INFO)


def main() -> None:
    configure_logging()
    settings = get_settings()
    try:
        check_model_credentials(settings)
    except ConfigError as exc:
        print(f"CopperChat cannot start: {exc.message}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
    )


if __name__ == "__main__":
    main()
